"""
Lambda handlers for the AIG enrollment Step Functions state machine.

Single Lambda, action-routed. Each Step Function state passes an
"action" field that determines which handler runs.
"""
import json
import io
import os
import re
import time
import logging
import traceback
import urllib.request
import urllib.error

import paramiko
import pyte
import boto3

from libs.tui.paramiko_session import ParamikoTUISession, ParamikoConfig, ChannelWrapper
from libs.tui.tui_actions import TUIActions

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ── Netskope API helpers ──

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


def get_netskope_creds():
    secret_arn = os.environ['NETSKOPE_SECRET_ARN']
    client = boto3.client('secretsmanager')
    resp = client.get_secret_value(SecretId=secret_arn)
    return json.loads(resp['SecretString'])


def get_ssh_private_key():
    secret_arn = os.environ['SSH_KEY_SECRET_ARN']
    client = boto3.client('secretsmanager')
    resp = client.get_secret_value(SecretId=secret_arn)
    return resp['SecretString']


# ── TUI session helper ──

def create_session(instance_ip, private_key_pem):
    config = ParamikoConfig(
        host=instance_ip,
        username='nsadmin',
        private_key_pem=private_key_pem,
        screen_load_delay=3.0,
        input_delay=0.3,
    )
    session = ParamikoTUISession(config)
    if not session.connect():
        raise RuntimeError(f'Failed to connect to {instance_ip}')
    return session


# ── Action handlers ──

def handle_register(event):
    """Register appliance with Netskope tenant, return appliance_id + enrollment_token."""
    creds = get_netskope_creds()
    tenant_url = creds['tenant_url']
    api_token = creds['api_token']
    instance_ip = event['instance_ip']
    appliance_name = event.get('appliance_name', f'aig-gw-{instance_ip}')

    appliance = api_request(
        tenant_url, '/api/v2/aig/appliances', api_token,
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
    logger.info('Registered appliance %s', appliance_id)

    if not enrollment_token:
        token_resp = api_request(
            tenant_url,
            f'/api/v2/aig/appliances/{appliance_id}/enrollmenttokens',
            api_token, method='POST',
        )
        enrollment_token = token_resp.get('token') or token_resp.get('enrollment_token', '')

    if not enrollment_token:
        # Cleanup orphan
        api_request(tenant_url, f'/api/v2/aig/appliances/{appliance_id}', api_token, method='DELETE')
        raise ValueError('Failed to get enrollment token')

    return {
        'appliance_id': appliance_id,
        'enrollment_token': enrollment_token,
        'instance_ip': event['instance_ip'],
    }


def handle_deregister(event):
    """Deregister appliance from Netskope tenant."""
    creds = get_netskope_creds()
    appliance_id = event['appliance_id']
    api_request(
        creds['tenant_url'],
        f'/api/v2/aig/appliances/{appliance_id}',
        creds['api_token'],
        method='DELETE',
    )
    logger.info('Deregistered appliance %s', appliance_id)
    return {'appliance_id': appliance_id, 'status': 'deregistered'}


def handle_check_ssh(event):
    """Check if SSH is reachable. Returns ssh_ready: true/false."""
    instance_ip = event['instance_ip']
    private_key_pem = get_ssh_private_key()

    try:
        key = paramiko.RSAKey.from_private_key(io.StringIO(private_key_pem))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=instance_ip, username='nsadmin',
            pkey=key, timeout=10,
            allow_agent=False, look_for_keys=False,
        )
        client.close()
        logger.info('SSH ready on %s', instance_ip)
        return {**event, 'ssh_ready': True}
    except Exception as e:
        logger.info('SSH not ready on %s: %s', instance_ip, e)
        return {**event, 'ssh_ready': False}


def handle_start_enrollment(event):
    """Navigate to enrollment screen and press Enter. Returns enrollment_state."""
    instance_ip = event['instance_ip']
    private_key_pem = get_ssh_private_key()
    session = create_session(instance_ip, private_key_pem)

    try:
        actions = TUIActions(session)
        found = actions.navigate_to_menu_item("Enroll this AI Gateway")
        if not found:
            return {**event, 'enrollment_state': 'menu_not_found',
                    'screen': session.get_screen_text()[:500]}

        session.press_enter()
        time.sleep(2)
        session.child._drain(timeout=3)

        # Quick check — did we immediately get token prompt or completed?
        idx = session.child.expect(
            [r'Enter enrollment token:', r'Enrollment completed', ChannelWrapper.TIMEOUT],
            timeout=10,
        )
        screen = session.get_screen_text()

        if idx == 0:
            return {**event, 'enrollment_state': 'token_prompt_ready',
                    'screen': screen[:500]}
        elif idx == 1:
            return {**event, 'enrollment_state': 'already_enrolled',
                    'screen': screen[:500]}
        else:
            return {**event, 'enrollment_state': 'pre_enrollment_running',
                    'screen': screen[:500]}
    finally:
        session.disconnect()


def handle_check_enrollment(event):
    """Check enrollment screen state — is token prompt ready?

    Each poll reconnects from the main menu, navigates to enrollment,
    and presses Enter. The TUI briefly shows the pre-enrollment system
    check screen before transitioning to the current state (progress bar,
    token prompt, or completed). We wait up to 60s for the transition.
    """
    instance_ip = event['instance_ip']
    private_key_pem = get_ssh_private_key()
    session = create_session(instance_ip, private_key_pem)

    try:
        actions = TUIActions(session)
        found = actions.navigate_to_menu_item("Enroll this AI Gateway")
        if not found:
            return {**event, 'enrollment_state': 'menu_not_found'}

        session.press_enter()

        # Wait up to 60s — the TUI shows the system check screen briefly
        # before transitioning to the current enrollment state
        idx = session.child.expect(
            [r'Enter enrollment token:', r'Enrollment completed', ChannelWrapper.TIMEOUT],
            timeout=60,
        )
        screen = session.get_screen_text()

        if idx == 0:
            return {**event, 'enrollment_state': 'token_prompt_ready',
                    'screen': screen[:500]}
        elif idx == 1:
            return {**event, 'enrollment_state': 'already_enrolled',
                    'screen': screen[:500]}
        else:
            return {**event, 'enrollment_state': 'pre_enrollment_running',
                    'screen': screen[:500]}
    finally:
        session.disconnect()


def handle_submit_token(event):
    """Submit enrollment token via TUI."""
    instance_ip = event['instance_ip']
    enrollment_token = event['enrollment_token']
    private_key_pem = get_ssh_private_key()
    session = create_session(instance_ip, private_key_pem)

    try:
        actions = TUIActions(session)
        found = actions.navigate_to_menu_item("Enroll this AI Gateway")
        if not found:
            return {**event, 'enrollment_state': 'menu_not_found'}

        session.press_enter()

        # Wait for token prompt
        idx = session.child.expect(
            [r'Enter enrollment token:', r'Enrollment completed', ChannelWrapper.TIMEOUT],
            timeout=30,
        )

        if idx == 1:
            return {**event, 'enrollment_state': 'already_enrolled'}
        if idx == 2:
            return {**event, 'enrollment_state': 'pre_enrollment_running',
                    'screen': session.get_screen_text()[:500]}

        # Send token
        session.child.send(enrollment_token)
        time.sleep(0.5)
        session.press_enter()

        # Wait for result
        idx = session.child.expect(
            [r'Enrollment completed', r'[Ee]rror', ChannelWrapper.TIMEOUT],
            timeout=120,
        )
        screen = session.get_screen_text()

        if idx == 0:
            return {**event, 'enrollment_state': 'completed',
                    'screen': screen[:500]}
        elif idx == 1:
            return {**event, 'enrollment_state': 'error',
                    'screen': screen[:500]}
        else:
            return {**event, 'enrollment_state': 'enrollment_timeout',
                    'screen': screen[:500]}
    finally:
        session.disconnect()


def handle_check_dlpod_cert(event):
    """Check if the DLPoD certificate is available in SSM."""
    cert_param = os.environ.get('DLPOD_CERT_PARAM', '')
    if not cert_param:
        return {**event, 'cert_ready': False, 'reason': 'no_cert_param'}

    ssm = boto3.client('ssm')
    try:
        resp = ssm.get_parameter(Name=cert_param)
        value = resp['Parameter']['Value']
        cert_ready = value != 'pending' and '-----BEGIN CERTIFICATE-----' in value
        return {**event, 'cert_ready': cert_ready}
    except ssm.exceptions.ParameterNotFound:
        return {**event, 'cert_ready': False, 'reason': 'param_not_found'}


def handle_configure_aig_dlp(event):
    """Configure DLP on the AI Gateway via TUI.

    Navigates: Configure Content Inspection Services → Data Loss Prevention Service
    → Configure DLP Service Certificate (paste cert)
    → Configure DLP Service Host (enter URL)
    """
    instance_ip = event['instance_ip']
    dlp_host_url = event.get('dlp_host_url', os.environ.get('DLP_HOST_URL', ''))
    private_key_pem = get_ssh_private_key()

    if not dlp_host_url:
        return {**event, 'dlp_configured': False, 'reason': 'no_dlp_host_url'}

    # Read cert from SSM
    cert_param = os.environ.get('DLPOD_CERT_PARAM', '')
    ssm = boto3.client('ssm')
    resp = ssm.get_parameter(Name=cert_param)
    cert_pem = resp['Parameter']['Value']
    if '-----BEGIN CERTIFICATE-----' not in cert_pem:
        return {**event, 'dlp_configured': False, 'reason': 'cert_not_ready'}

    session = create_session(instance_ip, private_key_pem)
    try:
        actions = TUIActions(session)

        # Navigate to Configure Content Inspection Services
        found = actions.select_menu_item("Configure Content Inspection Services")
        if not found:
            # Try shorter pattern
            found = actions.select_menu_item("Configure AI Services")
        if not found:
            return {**event, 'dlp_configured': False, 'reason': 'cis_menu_not_found',
                    'screen': session.get_screen_text()[:500]}

        time.sleep(2)
        session.child._drain(timeout=2)

        # Select Data Loss Prevention Service
        found = actions.select_menu_item("Data Loss Prevention Service")
        if not found:
            return {**event, 'dlp_configured': False, 'reason': 'dlp_menu_not_found',
                    'screen': session.get_screen_text()[:500]}

        time.sleep(2)
        session.child._drain(timeout=2)

        # Step 1: Configure DLP Service Certificate
        found = actions.select_menu_item("Configure DLP Service Certificate")
        if not found:
            return {**event, 'dlp_configured': False, 'reason': 'cert_menu_not_found',
                    'screen': session.get_screen_text()[:500]}

        time.sleep(2)
        session.child._drain(timeout=2)

        # Wait for cert input prompt
        idx = session.child.expect(
            [r'[Ee]nter new certificate', r'certificate', ChannelWrapper.TIMEOUT],
            timeout=15,
        )
        if idx == 2:
            return {**event, 'dlp_configured': False, 'reason': 'cert_prompt_timeout',
                    'screen': session.get_screen_text()[:500]}

        screen_before = session.get_screen_text()
        logger.info('CERT_INPUT_SCREEN: [%s]', repr(screen_before[:500]))

        # Collapse the cert PEM: BEGIN/END on own lines, base64 on one line.
        cert_lines = cert_pem.strip().split('\n')
        begin_line = cert_lines[0]
        end_line = cert_lines[-1]
        b64_body = ''.join(cert_lines[1:-1])
        cert_collapsed = f'{begin_line}\n{b64_body}\n{end_line}'
        logger.info('CERT_COLLAPSED: %d chars (body: %d)', len(cert_collapsed), len(b64_body))

        # Use bracketed paste mode to send the cert as a "paste" event.
        # TUI frameworks (Bubble Tea, etc.) recognize this as clipboard paste
        # and insert the full content into the active text input.
        PASTE_START = '\x1b[200~'
        PASTE_END = '\x1b[201~'
        session.child.send(PASTE_START + cert_collapsed + PASTE_END)
        time.sleep(3)
        session.child._drain(timeout=2)

        screen_after_paste = session.get_screen_text()
        logger.info('AFTER_PASTE: [%s]', repr(screen_after_paste[:500]))

        # Press Enter to submit
        session.press_enter()
        time.sleep(8)
        session.child._drain(timeout=5)

        # Check for success or retry prompt
        screen = session.get_screen_text()
        logger.info('AFTER_SUBMIT: [%s]', repr(screen[:500]))
        if re.search(r'[Ff]ailed|[Ii]nvalid|response code', screen):
            return {**event, 'dlp_configured': False, 'reason': 'cert_validation_failed',
                    'screen': screen[:1500]}

        logger.info('DLP certificate configured on AIG')

        # Go back to DLP Service menu
        session.press_escape()
        time.sleep(2)
        session.child._drain(timeout=2)

        # Step 2: Configure DLP Service Host
        found = actions.select_menu_item("Configure DLP Service Host")
        if not found:
            return {**event, 'dlp_configured': False, 'reason': 'host_menu_not_found',
                    'screen': session.get_screen_text()[:500]}

        time.sleep(2)
        session.child._drain(timeout=2)

        # Wait for host URL input prompt
        idx = session.child.expect(
            [r'[Ee]nter new [Hh]ost', r'[Hh]ost URL', ChannelWrapper.TIMEOUT],
            timeout=15,
        )
        if idx == 2:
            return {**event, 'dlp_configured': False, 'reason': 'host_prompt_timeout',
                    'screen': session.get_screen_text()[:500]}

        # Enter DLP host URL
        session.child.send(dlp_host_url)
        time.sleep(0.5)
        session.press_enter()
        time.sleep(3)
        session.child._drain(timeout=3)

        logger.info('DLP host configured on AIG: %s', dlp_host_url)

        return {**event, 'dlp_configured': True}
    finally:
        session.disconnect()


def handle_check_guardrails_cert(event):
    """Check if the guardrails certificate is available in SSM."""
    cert_param = os.environ.get('GUARDRAILS_CERT_PARAM', '')
    if not cert_param:
        return {**event, 'cert_ready': False, 'reason': 'no_cert_param'}

    ssm = boto3.client('ssm')
    try:
        resp = ssm.get_parameter(Name=cert_param)
        value = resp['Parameter']['Value']
        cert_ready = value != 'pending' and '-----BEGIN CERTIFICATE-----' in value
        return {**event, 'cert_ready': cert_ready}
    except ssm.exceptions.ParameterNotFound:
        return {**event, 'cert_ready': False, 'reason': 'param_not_found'}


def handle_configure_aig_guardrails(event):
    """Configure Guardrails LLM on the AI Gateway via TUI.

    Navigates: Configure Content Inspection Services → Configure Guardrails LLM
    → Configure LLM Service Certificate (paste cert)
    → Configure LLM Service Host (enter URL)
    """
    instance_ip = event['instance_ip']
    guardrails_host_url = event.get('guardrails_host_url', os.environ.get('GUARDRAILS_HOST_URL', ''))
    private_key_pem = get_ssh_private_key()

    if not guardrails_host_url:
        return {**event, 'guardrails_configured': False, 'reason': 'no_guardrails_host_url'}

    # Read cert from SSM
    cert_param = os.environ.get('GUARDRAILS_CERT_PARAM', '')
    if not cert_param:
        return {**event, 'guardrails_configured': False, 'reason': 'no_cert_param'}

    ssm = boto3.client('ssm')
    resp = ssm.get_parameter(Name=cert_param)
    cert_pem = resp['Parameter']['Value']
    if '-----BEGIN CERTIFICATE-----' not in cert_pem:
        return {**event, 'guardrails_configured': False, 'reason': 'cert_not_ready'}

    session = create_session(instance_ip, private_key_pem)
    try:
        actions = TUIActions(session)

        # Navigate to Configure Content Inspection Services
        found = actions.select_menu_item("Configure Content Inspection Services")
        if not found:
            found = actions.select_menu_item("Configure AI Services")
        if not found:
            return {**event, 'guardrails_configured': False, 'reason': 'cis_menu_not_found',
                    'screen': session.get_screen_text()[:500]}

        time.sleep(2)
        session.child._drain(timeout=2)

        # Select Configure Guardrails LLM (AIG 1.3+ naming)
        found = actions.select_menu_item("Configure Guardrails LLM")
        if not found:
            # Try alternate names
            found = actions.select_menu_item("LLM Guardrails Service")
        if not found:
            found = actions.select_menu_item("AI Guardrails")
        if not found:
            return {**event, 'guardrails_configured': False, 'reason': 'guardrails_menu_not_found',
                    'screen': session.get_screen_text()[:500]}

        time.sleep(2)
        session.child._drain(timeout=2)

        # Step 1: Configure Guardrails LLM Certificate
        found = actions.select_menu_item("Configure Guardrails LLM Certificate")
        if not found:
            found = actions.select_menu_item("Configure LLM Service Certificate")
        if not found:
            return {**event, 'guardrails_configured': False, 'reason': 'cert_menu_not_found',
                    'screen': session.get_screen_text()[:500]}

        time.sleep(2)
        session.child._drain(timeout=2)

        # Wait for cert input prompt
        idx = session.child.expect(
            [r'[Ee]nter new certificate', r'certificate', ChannelWrapper.TIMEOUT],
            timeout=15,
        )
        if idx == 2:
            return {**event, 'guardrails_configured': False, 'reason': 'cert_prompt_timeout',
                    'screen': session.get_screen_text()[:500]}

        screen_before = session.get_screen_text()
        logger.info('GUARDRAILS_CERT_INPUT_SCREEN: [%s]', repr(screen_before[:500]))

        # Collapse cert PEM and paste via bracketed paste mode
        cert_lines = cert_pem.strip().split('\n')
        begin_line = cert_lines[0]
        end_line = cert_lines[-1]
        b64_body = ''.join(cert_lines[1:-1])
        cert_collapsed = f'{begin_line}\n{b64_body}\n{end_line}'
        logger.info('GUARDRAILS_CERT_COLLAPSED: %d chars (body: %d)', len(cert_collapsed), len(b64_body))

        PASTE_START = '\x1b[200~'
        PASTE_END = '\x1b[201~'
        session.child.send(PASTE_START + cert_collapsed + PASTE_END)
        time.sleep(3)
        session.child._drain(timeout=2)

        # Press Enter to submit
        session.press_enter()
        time.sleep(8)
        session.child._drain(timeout=5)

        screen = session.get_screen_text()
        logger.info('GUARDRAILS_AFTER_CERT_SUBMIT: [%s]', repr(screen[:500]))
        if re.search(r'[Ff]ailed|[Ii]nvalid|response code', screen):
            return {**event, 'guardrails_configured': False, 'reason': 'cert_validation_failed',
                    'screen': screen[:1500]}

        logger.info('Guardrails certificate configured on AIG')

        # Go back to guardrails menu
        session.press_escape()
        time.sleep(2)
        session.child._drain(timeout=2)

        # Step 2: Configure Guardrails LLM Host
        found = actions.select_menu_item("Configure Guardrails LLM Host")
        if not found:
            found = actions.select_menu_item("Configure LLM Service Host")
        if not found:
            return {**event, 'guardrails_configured': False, 'reason': 'host_menu_not_found',
                    'screen': session.get_screen_text()[:500]}

        time.sleep(2)
        session.child._drain(timeout=2)

        # Wait for host URL input prompt
        idx = session.child.expect(
            [r'[Ee]nter new [Hh]ost', r'[Hh]ost URL', ChannelWrapper.TIMEOUT],
            timeout=15,
        )
        if idx == 2:
            return {**event, 'guardrails_configured': False, 'reason': 'host_prompt_timeout',
                    'screen': session.get_screen_text()[:500]}

        # Enter guardrails host URL
        session.child.send(guardrails_host_url)
        time.sleep(0.5)
        session.press_enter()
        time.sleep(3)
        session.child._drain(timeout=3)

        logger.info('Guardrails host configured on AIG: %s', guardrails_host_url)

        return {**event, 'guardrails_configured': True}
    finally:
        session.disconnect()


def handle_configure_aig_dlp_api(event):
    """Configure DLP on the AI Gateway via its local REST API.

    Uses paramiko exec_command() to run curl on the AIG instance,
    calling the local API endpoints:
      PUT /aiapi/dlp/cert     — upload DLPoD server certificate
      PUT /aiapi/dlp/hostconfig — set DLP host URL
    """
    instance_ip = event['instance_ip']
    dlp_host_url = event.get('dlp_host_url', os.environ.get('DLP_HOST_URL', ''))
    private_key_pem = get_ssh_private_key()

    if not dlp_host_url:
        return {**event, 'dlp_configured': False, 'reason': 'no_dlp_host_url'}

    # Read cert from SSM
    cert_param = os.environ.get('DLPOD_CERT_PARAM', '')
    ssm = boto3.client('ssm')
    resp = ssm.get_parameter(Name=cert_param)
    cert_pem = resp['Parameter']['Value']
    if '-----BEGIN CERTIFICATE-----' not in cert_pem:
        return {**event, 'dlp_configured': False, 'reason': 'cert_not_ready'}

    # SSH to the AIG and run curl commands
    pkey = paramiko.RSAKey.from_private_key(io.StringIO(private_key_pem))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=instance_ip, username='nsadmin',
        pkey=pkey, timeout=30,
        allow_agent=False, look_for_keys=False,
    )

    try:
        # Step 1: Upload certificate
        # Escape the cert for shell — use heredoc
        cert_cmd = f"""curl -sk -X PUT https://localhost/aiapi/dlp/cert \
  -H 'Content-Type: application/json' \
  -d '{{"cert": "{cert_pem.replace(chr(10), "\\\\n")}"}}'"""

        logger.info('Running cert upload on AIG...')
        stdin, stdout, stderr = client.exec_command(cert_cmd, timeout=30)
        cert_result = stdout.read().decode('utf-8', errors='replace')
        cert_err = stderr.read().decode('utf-8', errors='replace')
        cert_exit = stdout.channel.recv_exit_status()
        logger.info('Cert upload: exit=%d, out=%s, err=%s',
                     cert_exit, cert_result[:500], cert_err[:200])

        if cert_exit != 0:
            return {**event, 'dlp_configured': False, 'reason': 'cert_upload_failed',
                    'exit_code': cert_exit, 'output': cert_result[:500],
                    'error': cert_err[:200]}

        # Step 2: Configure DLP host
        host_cmd = f"""curl -sk -X PUT https://localhost/aiapi/dlp/hostconfig \
  -H 'Content-Type: application/json' \
  -d '{{"host": "{dlp_host_url}"}}'"""

        logger.info('Running host config on AIG...')
        stdin, stdout, stderr = client.exec_command(host_cmd, timeout=30)
        host_result = stdout.read().decode('utf-8', errors='replace')
        host_err = stderr.read().decode('utf-8', errors='replace')
        host_exit = stdout.channel.recv_exit_status()
        logger.info('Host config: exit=%d, out=%s, err=%s',
                     host_exit, host_result[:500], host_err[:200])

        if host_exit != 0:
            return {**event, 'dlp_configured': False, 'reason': 'host_config_failed',
                    'exit_code': host_exit, 'output': host_result[:500],
                    'error': host_err[:200]}

        return {**event, 'dlp_configured': True,
                'cert_response': cert_result[:500],
                'host_response': host_result[:500]}
    finally:
        client.close()


def handle_complete_lifecycle(event):
    """Complete the ASG lifecycle action after enrollment succeeds."""
    lifecycle = event.get('lifecycle', {})
    hook_name = lifecycle.get('hook_name', '')
    asg_name = lifecycle.get('asg_name', '')
    action_token = lifecycle.get('action_token', '')

    if not hook_name or not asg_name or not action_token:
        logger.info('No lifecycle details — skipping CompleteLifecycleAction')
        return {**event, 'lifecycle_completed': False}

    asg_client = boto3.client('autoscaling')
    asg_client.complete_lifecycle_action(
        LifecycleHookName=hook_name,
        AutoScalingGroupName=asg_name,
        LifecycleActionToken=action_token,
        LifecycleActionResult='CONTINUE',
    )
    logger.info('Completed lifecycle action: CONTINUE')
    return {**event, 'lifecycle_completed': True}


# ── Lambda entry point ──

SENSITIVE_KEYS = {'enrollment_token', 'password', 'api_token'}


def redact(obj):
    """Redact sensitive fields for logging."""
    if not isinstance(obj, dict):
        return obj
    return {k: '***' if k in SENSITIVE_KEYS else v for k, v in obj.items()}


def handler(event, context):
    logger.info('Event: %s', json.dumps(redact(event), default=str))

    action = event.get('action', '')
    handlers = {
        'register': handle_register,
        'deregister': handle_deregister,
        'check_ssh': handle_check_ssh,
        'start_enrollment': handle_start_enrollment,
        'check_enrollment': handle_check_enrollment,
        'submit_token': handle_submit_token,
        'check_dlpod_cert': handle_check_dlpod_cert,
        'configure_aig_dlp': handle_configure_aig_dlp,
        'check_guardrails_cert': handle_check_guardrails_cert,
        'configure_aig_guardrails': handle_configure_aig_guardrails,
        'configure_aig_dlp_api': handle_configure_aig_dlp_api,
        'complete_lifecycle': handle_complete_lifecycle,
    }

    if action not in handlers:
        raise ValueError(f'Unknown action: {action}. Valid: {list(handlers.keys())}')

    result = handlers[action](event)
    logger.info('Result: %s', json.dumps(redact(result), default=str))
    return result
