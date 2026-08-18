"""
Activation Lambda — handles ASG lifecycle events and Step Functions polling actions.

Enrollment flow (EC2_INSTANCE_LAUNCHING):
  1. Register the appliance with Netskope API → get appliance_id + enrollment_token.
  2. Write the bootstrap secret (token + optional DLP / guardrails config).
     The AIG appliance reads this secret at first boot and self-enrolls — no SSH needed.
  3. Store appliance_id in SSM for cleanup on termination.
  4. Start the polling state machine, which calls back into this Lambda via
     check_status / complete_lifecycle actions until the appliance is 'connected'.

Termination flow (EC2_INSTANCE_TERMINATING):
  Deregister the appliance and delete the SSM appliance-ID parameter.

Step Functions actions (invoked from EnrollmentPollingStateMachine):
  check_status       — GET /api/v2/aig/appliances/{id}, returns connected bool.
  complete_lifecycle — calls CompleteLifecycleAction CONTINUE or ABANDON.
"""
import json
import os
import logging
import urllib.request
import urllib.error
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2_client = boto3.client('ec2')
asg_client = boto3.client('autoscaling')
sfn_client = boto3.client('stepfunctions')
ssm_client = boto3.client('ssm')
sm_client  = boto3.client('secretsmanager')

# Keys whose values are masked before any logging.
_SENSITIVE = {'enrollment_token', 'api_token', 'password', 'license_key', 'client_secret'}


def _redact(obj, _depth=0):
    """Recursively mask sensitive keys so they never appear in CloudWatch Logs."""
    if _depth > 8:
        return obj
    if isinstance(obj, dict):
        return {k: '***' if k in _SENSITIVE else _redact(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(i, _depth + 1) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_secret(secret_arn):
    resp = sm_client.get_secret_value(SecretId=secret_arn)
    return json.loads(resp['SecretString'])


def get_instance_private_ip(instance_id):
    resp = ec2_client.describe_instances(InstanceIds=[instance_id])
    try:
        return resp['Reservations'][0]['Instances'][0].get('PrivateIpAddress', 'unknown')
    except (IndexError, KeyError):
        return 'unknown'


def get_ssm_parameter(name):
    """Return an SSM parameter value, or '' if it does not exist or is still 'pending'."""
    try:
        resp = ssm_client.get_parameter(Name=name)
        value = resp['Parameter']['Value']
        return '' if value == 'pending' else value
    except ssm_client.exceptions.ParameterNotFound:
        return ''


def api_request(tenant_url, path, api_token, method='GET', body=None):
    url = f"{tenant_url.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Netskope-Api-Token', api_token)
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode() if exc.fp else ''
        logger.error('API %s %s -> %s: %s', method, path, exc.code, body_text)
        raise


# ---------------------------------------------------------------------------
# Netskope appliance registration
# ---------------------------------------------------------------------------

def register_appliance(tenant_url, api_token, appliance_name, instance_ip):
    """
    Register a new appliance with the Netskope tenant.
    Returns (appliance_id, enrollment_token).

    If the POST response does not include an enrollment token, a second call
    to /enrollmenttokens is made. On any failure the orphan appliance record
    is deleted before re-raising so the tenant stays clean.
    """
    appliance = api_request(
        tenant_url, '/api/v2/aig/appliances', api_token,
        method='POST',
        body={
            'name': appliance_name,
            'host': instance_ip,
            'ports': {
                'https': {'port': 443, 'enable': True},
                'http':  {'port': 80,  'enable': False},
            },
        },
    )
    appliance_id     = str(appliance['id'])
    enrollment_token = appliance.get('enrollment_token', '')
    logger.info('Registered appliance id=%s name=%s', appliance_id, appliance_name)

    if not enrollment_token:
        try:
            token_resp = api_request(
                tenant_url,
                f'/api/v2/aig/appliances/{appliance_id}/enrollmenttokens',
                api_token, method='POST',
            )
            enrollment_token = token_resp.get('token') or token_resp.get('enrollment_token', '')
        except Exception:
            logger.exception('Token fetch failed — deleting orphan appliance %s', appliance_id)
            try:
                api_request(tenant_url, f'/api/v2/aig/appliances/{appliance_id}',
                            api_token, method='DELETE')
            except Exception:
                logger.exception('Orphan cleanup also failed for %s', appliance_id)
            raise

    if not enrollment_token:
        raise ValueError(f'Enrollment token absent in API response for appliance {appliance_id}')

    return appliance_id, enrollment_token


def deregister_appliance(tenant_url, api_token, appliance_id):
    try:
        api_request(tenant_url, f'/api/v2/aig/appliances/{appliance_id}', api_token, method='DELETE')
        logger.info('Deregistered appliance %s', appliance_id)
    except Exception:
        logger.exception('Deregister failed for appliance %s — continuing', appliance_id)


# ---------------------------------------------------------------------------
# Bootstrap secret
# ---------------------------------------------------------------------------

def write_bootstrap_secret(secret_arn, enrollment_token, stack_name):
    """
    Build the bootstrap payload and overwrite the AIGBootstrapSecret.

    The AIG appliance reads this secret at first boot (via UserData → Secrets Manager).
    Optional DLP on-demand and AI Guardrails blocks are included when the corresponding
    environment variables are set and the supporting SSM parameters are ready.
    """
    payload = {'bootstrap': True, 'enrollment_token': enrollment_token}

    dlp_host_url = os.environ.get('DLP_HOST_URL', '')
    if dlp_host_url:
        cert = get_ssm_parameter(f'/{stack_name}/dlpod-cert')
        if cert:
            payload['dlp'] = {'certificate': cert, 'host': dlp_host_url}
            logger.info('DLP on-demand config included in bootstrap payload')
        else:
            logger.warning('DLP_HOST_URL is set but /%s/dlpod-cert is not ready — '
                           'omitting dlp block; configure DLP on the appliance after enrollment',
                           stack_name)

    guardrails_host_url = os.environ.get('GUARDRAILS_HOST_URL', '')
    if guardrails_host_url:
        entry = {'host': guardrails_host_url}
        cert = get_ssm_parameter(f'/{stack_name}/guardrails-cert')
        if cert:
            entry['certificate'] = cert
        payload['ai_guardrails'] = entry
        logger.info('AI Guardrails config included in bootstrap payload (cert=%s)',
                    'present' if cert else 'absent')

    sm_client.put_secret_value(SecretId=secret_arn, SecretString=json.dumps(payload))
    # Do NOT log payload — it contains the enrollment token.
    logger.info('Bootstrap secret written successfully (token redacted)')


# ---------------------------------------------------------------------------
# Step Functions — start polling execution
# ---------------------------------------------------------------------------

def start_polling(instance_id, appliance_id, lifecycle_detail):
    """
    Start the EnrollmentPollingStateMachine.
    The state machine polls Netskope until the appliance reaches 'connected',
    then calls complete_lifecycle via this same Lambda.
    """
    sfn_input = {
        'appliance_id': appliance_id,
        'attempt': 0,
        'lifecycle': {
            'hook_name':    lifecycle_detail.get('LifecycleHookName', ''),
            'asg_name':     lifecycle_detail.get('AutoScalingGroupName', ''),
            'action_token': lifecycle_detail.get('LifecycleActionToken', ''),
        },
    }
    resp = sfn_client.start_execution(
        stateMachineArn=os.environ['STATE_MACHINE_ARN'],
        name=f'enroll-{instance_id}',
        input=json.dumps(sfn_input),
    )
    logger.info('Started polling execution %s for instance %s appliance %s',
                resp['executionArn'], instance_id, appliance_id)


# ---------------------------------------------------------------------------
# Step Functions action handlers
# ---------------------------------------------------------------------------

def handle_check_status(event, context):
    """
    Called by the polling state machine's CheckApplianceStatus task.

    Input:  {"action": "check_status", "appliance_id": "...", "attempt": N, ...}
    Output: {"connected": bool, "status": str}
            (merged into $.status_result by the state machine's ResultPath)
    """
    appliance_id = event['appliance_id']
    attempt      = event.get('attempt', 0)

    secret = get_secret(os.environ['SECRET_ARN'])
    try:
        data   = api_request(secret['tenant_url'],
                             f'/api/v2/aig/appliances/{appliance_id}',
                             secret['api_token'])
        status = data.get('status', 'unknown')
    except Exception:
        logger.exception('Status check failed for appliance %s (attempt %d)', appliance_id, attempt)
        status = 'unknown'

    connected = (status == 'connected')
    logger.info('Appliance %s status=%s connected=%s attempt=%d',
                appliance_id, status, connected, attempt)
    return {'connected': connected, 'status': status}


def handle_complete_lifecycle_action(event, context):
    """
    Called by the polling state machine's CompleteSuccess / CompleteAbandon tasks.

    Input:  {"action": "complete_lifecycle", "lifecycle": {...}, "success": bool}
    Output: {"completed": true, "result": "CONTINUE"|"ABANDON"}
    """
    lc     = event['lifecycle']
    result = 'CONTINUE' if event.get('success', True) else 'ABANDON'
    asg_client.complete_lifecycle_action(
        LifecycleHookName=lc['hook_name'],
        AutoScalingGroupName=lc['asg_name'],
        LifecycleActionToken=lc['action_token'],
        LifecycleActionResult=result,
    )
    logger.info('CompleteLifecycleAction: %s', result)
    return {'completed': True, 'result': result}


# ---------------------------------------------------------------------------
# ASG lifecycle event handler
# ---------------------------------------------------------------------------

def handle_lifecycle_event(detail, context):
    """
    Handle a single ASG lifecycle event (launch or terminate).
    `detail` is the raw lifecycle hook message — the dict that arrives inside
    the SNS Message payload.
    """
    instance_id = detail['EC2InstanceId']
    transition  = detail['LifecycleTransition']
    stack_name  = os.environ['STACK_NAME']

    try:
        if transition == 'autoscaling:EC2_INSTANCE_LAUNCHING':
            secret      = get_secret(os.environ['SECRET_ARN'])
            instance_ip = get_instance_private_ip(instance_id)
            appliance_name = f'{stack_name}-gw-{instance_id}'

            appliance_id, enrollment_token = register_appliance(
                secret['tenant_url'], secret['api_token'],
                appliance_name, instance_ip,
            )

            # Persist appliance ID so the termination hook can deregister it.
            ssm_client.put_parameter(
                Name=f'/aig/{stack_name}/{instance_id}/appliance-id',
                Value=appliance_id, Type='String', Overwrite=True,
            )

            # Write the bootstrap secret before starting the polling machine.
            # The appliance reads this secret at first boot; the token must be
            # present before the instance OS finishes initialising.
            write_bootstrap_secret(
                os.environ['BOOTSTRAP_SECRET_ARN'],
                enrollment_token, stack_name,
            )

            # Hand off to Step Functions — it will call back via check_status
            # and complete_lifecycle actions until the appliance is connected.
            try:
                start_polling(instance_id, appliance_id, detail)
            except Exception:
                logger.exception('Step Functions start failed — abandoning lifecycle for %s',
                                 instance_id)
                try:
                    asg_client.complete_lifecycle_action(
                        LifecycleHookName=detail['LifecycleHookName'],
                        AutoScalingGroupName=detail['AutoScalingGroupName'],
                        LifecycleActionToken=detail['LifecycleActionToken'],
                        LifecycleActionResult='ABANDON',
                    )
                except Exception:
                    logger.exception('Failed to abandon lifecycle after SFN failure')

        elif transition == 'autoscaling:EC2_INSTANCE_TERMINATING':
            appliance_id = None
            try:
                param = ssm_client.get_parameter(
                    Name=f'/aig/{stack_name}/{instance_id}/appliance-id')
                appliance_id = param['Parameter']['Value']
            except Exception:
                logger.warning('No appliance-id SSM parameter for %s — '
                               'appliance may already be deregistered', instance_id)

            if appliance_id:
                secret = get_secret(os.environ['SECRET_ARN'])
                deregister_appliance(secret['tenant_url'], secret['api_token'], appliance_id)

            try:
                ssm_client.delete_parameter(
                    Name=f'/aig/{stack_name}/{instance_id}/appliance-id')
            except Exception:
                pass  # parameter may not exist; that is fine

            asg_client.complete_lifecycle_action(
                LifecycleHookName=detail['LifecycleHookName'],
                AutoScalingGroupName=detail['AutoScalingGroupName'],
                LifecycleActionToken=detail['LifecycleActionToken'],
                LifecycleActionResult='CONTINUE',
            )
            logger.info('Termination lifecycle completed for %s', instance_id)

        else:
            logger.warning('Unhandled lifecycle transition: %s', transition)

    except Exception:
        logger.exception('Lifecycle handler failed for instance %s', instance_id)
        try:
            asg_client.complete_lifecycle_action(
                LifecycleHookName=detail['LifecycleHookName'],
                AutoScalingGroupName=detail['AutoScalingGroupName'],
                LifecycleActionToken=detail['LifecycleActionToken'],
                LifecycleActionResult='ABANDON',
            )
        except Exception:
            logger.exception('Failed to complete lifecycle action after top-level error')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handler(event, context):
    logger.info('Event: %s', json.dumps(_redact(event), default=str))

    # --- Step Functions action routing ---
    # Must be checked first: SFN events have no 'Records' or 'RequestType'.
    if 'action' in event:
        action = event['action']
        if action == 'check_status':
            return handle_check_status(event, context)
        if action == 'complete_lifecycle':
            return handle_complete_lifecycle_action(event, context)
        logger.error('Unknown action: %s', action)
        return

    # --- ASG lifecycle hook via SNS ---
    if 'Records' in event:
        record = event['Records'][0]
        if record.get('EventSource') == 'aws:sns':
            message = json.loads(record['Sns']['Message'])
            if message.get('Event') == 'autoscaling:TEST_NOTIFICATION':
                logger.info('Skipping ASG test notification')
                return
            return handle_lifecycle_event(message, context)

    # --- ASG lifecycle hook delivered directly (EventBridge / manual) ---
    if 'LifecycleTransition' in event:
        return handle_lifecycle_event(event, context)

    logger.error('Unrecognised event shape — keys: %s', list(event.keys()))
