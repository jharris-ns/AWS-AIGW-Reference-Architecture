"""
Activation Lambda — handles ASG lifecycle events.

On launch: registers appliance with Netskope tenant, starts Step Functions enrollment.
On terminate: deregisters appliance, cleans up SSM parameter.
Also handles CloudFormation Custom Resource events (single-instance template).
"""
import json
import os
import logging
import urllib.request
import urllib.error
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client('ssm')
ec2_client = boto3.client('ec2')
asg_client = boto3.client('autoscaling')
sfn_client = boto3.client('stepfunctions')


def get_instance_private_ip(instance_id):
    resp = ec2_client.describe_instances(InstanceIds=[instance_id])
    reservations = resp.get('Reservations', [])
    if reservations:
        instances = reservations[0].get('Instances', [])
        if instances:
            return instances[0].get('PrivateIpAddress', 'unknown')
    return 'unknown'


def complete_lifecycle_action(detail, result='CONTINUE'):
    asg_client.complete_lifecycle_action(
        LifecycleHookName=detail['LifecycleHookName'],
        AutoScalingGroupName=detail['AutoScalingGroupName'],
        LifecycleActionToken=detail['LifecycleActionToken'],
        LifecycleActionResult=result,
    )
    logger.info('Completed lifecycle action: %s', result)


def get_secret(secret_arn):
    client = boto3.client('secretsmanager')
    resp = client.get_secret_value(SecretId=secret_arn)
    return json.loads(resp['SecretString'])


def api_request(tenant_url, path, token, method='GET', body=None):
    url = f"{tenant_url.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Netskope-Api-Token', token)
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ''
        logger.error('API %s %s -> %s: %s', method, path, e.code, error_body)
        raise


def register_appliance(tenant_url, api_token, appliance_name, instance_ip):
    """Register appliance with Netskope tenant and return (appliance_id, enrollment_token)."""
    appliance = api_request(
        tenant_url,
        '/api/v2/aig/appliances',
        api_token,
        method='POST',
        body={
            'name': appliance_name,
            'host': instance_ip,
            'ports': {
                'https': {'port': 443, 'enable': True},
                'http': {'port': 80, 'enable': False},
            },
        },
    )
    appliance_id = str(appliance['id'])
    enrollment_token = appliance.get('enrollment_token', '')
    logger.info('Created appliance %s', appliance_id)

    if not enrollment_token:
        try:
            token_resp = api_request(
                tenant_url,
                f'/api/v2/aig/appliances/{appliance_id}/enrollmenttokens',
                api_token,
                method='POST',
            )
            enrollment_token = token_resp.get('token') or token_resp.get('enrollment_token', '')
        except Exception:
            logger.exception('Token creation failed — deleting orphan appliance %s', appliance_id)
            try:
                api_request(tenant_url, f'/api/v2/aig/appliances/{appliance_id}', api_token, method='DELETE')
            except Exception:
                logger.exception('Cleanup delete also failed')
            raise

    if not enrollment_token:
        raise ValueError('Enrollment token not found in API response')

    return appliance_id, enrollment_token


def deregister_appliance(tenant_url, api_token, appliance_id):
    """Deregister appliance from Netskope tenant."""
    try:
        api_request(tenant_url, f'/api/v2/aig/appliances/{appliance_id}', api_token, method='DELETE')
        logger.info('Deleted appliance %s', appliance_id)
    except Exception:
        logger.exception('Delete failed for appliance %s — continuing', appliance_id)


def start_enrollment(instance_id, instance_ip, appliance_id, enrollment_token,
                     lifecycle_detail=None):
    """Start the Step Functions enrollment state machine."""
    state_machine_arn = os.environ['STATE_MACHINE_ARN']
    sfn_input = {
        'instance_ip': instance_ip,
        'appliance_id': appliance_id,
        'enrollment_token': enrollment_token,
    }
    if lifecycle_detail:
        sfn_input['lifecycle'] = {
            'hook_name': lifecycle_detail.get('LifecycleHookName', ''),
            'asg_name': lifecycle_detail.get('AutoScalingGroupName', ''),
            'action_token': lifecycle_detail.get('LifecycleActionToken', ''),
        }
    resp = sfn_client.start_execution(
        stateMachineArn=state_machine_arn,
        name=f'enroll-{instance_id}',
        input=json.dumps(sfn_input),
    )
    logger.info('Started enrollment %s for %s', resp['executionArn'], instance_id)
    return resp['executionArn']


def handle_lifecycle_event(event, context):
    """Handle ASG lifecycle hook events (launch/terminate)."""
    detail = event.get('detail', event)
    instance_id = detail['EC2InstanceId']
    transition = detail['LifecycleTransition']

    stack_name = os.environ['STACK_NAME']
    secret_arn = os.environ['SECRET_ARN']

    try:
        if transition == 'autoscaling:EC2_INSTANCE_LAUNCHING':
            secret = get_secret(secret_arn)
            instance_ip = get_instance_private_ip(instance_id)
            appliance_name = f'{stack_name}-gw-{instance_id}'

            appliance_id, enrollment_token = register_appliance(
                secret['tenant_url'], secret['api_token'],
                appliance_name, instance_ip,
            )

            # Store appliance ID for cleanup on termination
            ssm.put_parameter(
                Name=f'/aig/{stack_name}/{instance_id}/appliance-id',
                Value=appliance_id, Type='String', Overwrite=True,
            )

            # Start Step Functions enrollment — it will complete
            # the lifecycle action after enrollment succeeds
            try:
                start_enrollment(
                    instance_id, instance_ip,
                    appliance_id, enrollment_token,
                    lifecycle_detail=detail,
                )
            except Exception:
                logger.exception('Step Functions start failed — abandoning lifecycle')
                try:
                    complete_lifecycle_action(detail, 'ABANDON')
                except Exception:
                    logger.exception('Failed to abandon lifecycle action')

        elif transition == 'autoscaling:EC2_INSTANCE_TERMINATING':
            appliance_id = None
            try:
                resp = ssm.get_parameter(Name=f'/aig/{stack_name}/{instance_id}/appliance-id')
                appliance_id = resp['Parameter']['Value']
            except Exception:
                logger.warning('No appliance ID found for %s', instance_id)

            if appliance_id:
                secret = get_secret(secret_arn)
                deregister_appliance(secret['tenant_url'], secret['api_token'], appliance_id)

            # Clean up SSM parameters
            try:
                ssm.delete_parameter(Name=f'/aig/{stack_name}/{instance_id}/appliance-id')
            except Exception:
                pass

            complete_lifecycle_action(detail, 'CONTINUE')

    except Exception:
        logger.exception('Lifecycle handler failed for %s', instance_id)
        try:
            complete_lifecycle_action(detail, 'ABANDON')
        except Exception:
            logger.exception('Failed to complete lifecycle action')


def handle_cfn_event(event, context):
    """Handle CloudFormation Custom Resource events (used by single-instance template)."""
    # Import cfnresponse only when needed (not available outside CFN context)
    import cfnresponse

    request_type = event['RequestType']
    props = event['ResourceProperties']

    try:
        if request_type == 'Create':
            secret = get_secret(props['SecretArn'])
            instance_id = props['InstanceId']
            stack_name = props['StackName']
            instance_ip = props.get('InstanceIp', props.get('PublicIp', 'unknown'))
            appliance_name = props['ApplianceName']

            appliance_id, enrollment_token = register_appliance(
                secret['tenant_url'], secret['api_token'],
                appliance_name, instance_ip,
            )

            try:
                start_enrollment(instance_id, instance_ip, appliance_id, enrollment_token)
            except Exception:
                logger.exception('Step Functions start failed — instance may need manual enrollment')

            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'ApplianceId': appliance_id,
            }, physicalResourceId=appliance_id)

        elif request_type == 'Delete':
            appliance_id = event.get('PhysicalResourceId', '')
            if appliance_id and not appliance_id.startswith('arn:'):
                secret = get_secret(props['SecretArn'])
                deregister_appliance(secret['tenant_url'], secret['api_token'], appliance_id)

            cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physicalResourceId=appliance_id)

        else:
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {},
                             physicalResourceId=event.get('PhysicalResourceId', ''))

    except Exception:
        logger.exception('Handler failed')
        cfnresponse.send(event, context, cfnresponse.FAILED, {},
                         physicalResourceId=event.get('PhysicalResourceId', ''))


def handler(event, context):
    logger.info('Event: %s', json.dumps(event, default=str))

    # SNS wrapper from lifecycle hook
    if 'Records' in event and event['Records'][0].get('EventSource') == 'aws:sns':
        message = json.loads(event['Records'][0]['Sns']['Message'])
        logger.info('SNS lifecycle message: %s', json.dumps(message, default=str))
        if message.get('Event') == 'autoscaling:TEST_NOTIFICATION':
            logger.info('Skipping test notification')
            return
        return handle_lifecycle_event(message, context)
    elif 'detail' in event and 'LifecycleTransition' in event.get('detail', {}):
        return handle_lifecycle_event(event.get('detail', event), context)
    elif 'RequestType' in event:
        return handle_cfn_event(event, context)
    else:
        logger.error('Unknown event type')
