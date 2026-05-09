"""
Lambda handlers for the AIG enrollment Step Functions state machine.

Single Lambda, action-routed. Each Step Function state passes an
"action" field that determines which handler runs.
"""
import json
import io
import os
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

def handler(event, context):
    logger.info('Event: %s', json.dumps(event, default=str))

    action = event.get('action', '')
    handlers = {
        'register': handle_register,
        'deregister': handle_deregister,
        'check_ssh': handle_check_ssh,
        'start_enrollment': handle_start_enrollment,
        'check_enrollment': handle_check_enrollment,
        'submit_token': handle_submit_token,
        'complete_lifecycle': handle_complete_lifecycle,
    }

    if action not in handlers:
        raise ValueError(f'Unknown action: {action}. Valid: {list(handlers.keys())}')

    result = handlers[action](event)
    logger.info('Result: %s', json.dumps(result, default=str))
    return result
