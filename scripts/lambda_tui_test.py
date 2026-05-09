"""
Lambda function that tests ParamikoTUISession — including TUI enrollment.

Invoke with:
{
  "instance_ip": "172.31.7.56",
  "username": "nsadmin",
  "auth_method": "key",
  "action": "enroll",
  "enrollment_token": "<token>"
}

Or just navigate (no enrollment):
{
  "instance_ip": "172.31.7.56",
  "username": "nsadmin",
  "auth_method": "key",
  "action": "navigate"
}
"""
import json
import os
import sys
import time
import logging
import traceback

logger = logging.getLogger()
logger.setLevel(logging.INFO)

import paramiko
import pyte

from libs.tui.paramiko_session import ParamikoTUISession, ParamikoConfig, ChannelWrapper
from libs.tui.tui_actions import TUIActions


def get_ssh_private_key():
    """Read the SSH private key from Secrets Manager."""
    import boto3
    secret_arn = os.environ['SSH_KEY_SECRET_ARN']
    client = boto3.client('secretsmanager')
    resp = client.get_secret_value(SecretId=secret_arn)
    return resp['SecretString']


def do_navigate(session, response):
    """Navigate test — verify TUI menu interaction."""
    actions = TUIActions(session)

    capture(session, 'main_menu', response)

    current = session.screen.get_current_line()
    log_step('current_line', current, response)

    # Navigate through all menu items
    for i in range(6):
        session.navigate_down()
        time.sleep(0.3)
    capture(session, 'after_navigate_all', response)

    # Back to top
    for i in range(6):
        session.navigate_up()
        time.sleep(0.3)
    capture(session, 'back_to_top', response)

    found = actions.navigate_to_menu_item("Enroll this AI Gateway")
    log_step('navigate_to_enroll', str(found), response)
    capture(session, 'at_enroll', response)


def do_enroll(session, enrollment_token, response):
    """Perform enrollment via TUI."""
    actions = TUIActions(session)

    capture(session, 'main_menu', response)

    # Navigate to enrollment
    log_step('navigate_to_enroll', '', response)
    found = actions.navigate_to_menu_item("Enroll this AI Gateway")
    if not found:
        response['errors'].append('Could not find "Enroll this AI Gateway" menu item')
        capture(session, 'enroll_not_found', response)
        return

    log_step('pressing_enter', '', response)
    session.press_enter()

    # Wait for pre-enrollment / token prompt / already enrolled
    log_step('waiting_for_enrollment_screen', 'timeout=840s', response)
    idx = session.child.expect(
        [r'Enter enrollment token:', r'Enrollment completed', ChannelWrapper.TIMEOUT],
        timeout=840,
    )
    capture(session, 'after_enter_enrollment', response)

    if idx == 1:
        log_step('already_enrolled', '', response)
        response['enrollment_status'] = 'already_enrolled'
        return

    if idx == 2:
        # Timeout — might still be in pre-enrollment
        screen = session.get_screen_text()
        log_step('timeout_waiting_for_token_prompt', screen[:200], response)
        response['enrollment_status'] = 'pre_enrollment_timeout'
        response['errors'].append('Timeout waiting for enrollment token prompt (pre-enrollment may still be running)')
        return

    # Got token prompt
    log_step('got_token_prompt', '', response)

    # Send the enrollment token
    log_step('sending_token', enrollment_token[:20] + '...', response)
    session.child.send(enrollment_token)
    time.sleep(0.5)
    session.press_enter()

    capture(session, 'after_token_submitted', response)

    # Wait for enrollment result
    log_step('waiting_for_result', 'timeout=120s', response)
    idx = session.child.expect(
        [r'Enrollment completed', r'[Ee]rror', ChannelWrapper.TIMEOUT],
        timeout=120,
    )
    capture(session, 'enrollment_result', response)

    if idx == 0:
        log_step('enrollment_completed', '', response)
        response['enrollment_status'] = 'success'
    elif idx == 1:
        screen = session.get_screen_text()
        log_step('enrollment_error', screen[:300], response)
        response['enrollment_status'] = 'error'
        response['errors'].append(f'Enrollment error on screen')
    else:
        screen = session.get_screen_text()
        log_step('enrollment_timeout', screen[:300], response)
        response['enrollment_status'] = 'timeout'
        response['errors'].append('Timeout waiting for enrollment result')


def log_step(name, detail, response):
    entry = {'step': name, 'time': time.time(), 'detail': detail}
    response['steps'].append(entry)
    logger.info('STEP: %s — %s', name, detail)


def capture(session, label, response):
    text = session.get_screen_text()
    pos = session.screen.get_cursor_position()
    entry = {'label': label, 'cursor': pos, 'text': text}
    response['screen_captures'].append(entry)
    logger.info('SCREEN [%s] cursor=%s:\n%s', label, pos, text)
    return text


def handler(event, context):
    logger.info('Event: %s', json.dumps(event, default=str))

    instance_ip = event['instance_ip']
    username = event.get('username', 'nsadmin')
    auth_method = event.get('auth_method', 'key')
    action = event.get('action', 'navigate')
    enrollment_token = event.get('enrollment_token', '')

    response = {
        'instance_ip': instance_ip,
        'username': username,
        'action': action,
        'steps': [],
        'screen_captures': [],
        'errors': [],
    }

    # Get credentials
    log_step('get_credentials', auth_method, response)
    private_key_pem = None
    password = None
    try:
        if auth_method == 'key':
            private_key_pem = get_ssh_private_key()
        elif auth_method == 'password':
            password = event.get('password', 'nsadmin')
    except Exception as e:
        response['status'] = 'FAILED'
        response['error'] = f'Credentials failed: {e}'
        return response

    config = ParamikoConfig(
        host=instance_ip,
        username=username,
        password=password,
        private_key_pem=private_key_pem,
        screen_load_delay=3.0,
        input_delay=0.3,
    )

    session = ParamikoTUISession(config)

    try:
        log_step('connecting', f'{username}@{instance_ip}', response)
        ok = session.connect()
        if not ok:
            response['status'] = 'FAILED'
            response['error'] = 'session.connect() returned False'
            return response
        log_step('connected', '', response)

        # Detect session type
        text = session.get_screen_text()
        tui_indicators = ['Enroll this AI Gateway', 'Configuration Wizard']
        is_tui = any(ind in text for ind in tui_indicators)

        if not is_tui:
            response['status'] = 'FAILED'
            response['error'] = 'TUI not detected after connect'
            response['session_type'] = 'not_tui'
            capture(session, 'no_tui', response)
            return response

        response['session_type'] = 'tui'
        log_step('tui_detected', '', response)

        if action == 'enroll':
            if not enrollment_token:
                response['status'] = 'FAILED'
                response['error'] = 'enrollment_token required for enroll action'
                return response
            do_enroll(session, enrollment_token, response)
        else:
            do_navigate(session, response)

        response['status'] = 'PASSED' if not response['errors'] else 'FAILED'

    except Exception as e:
        response['status'] = 'FAILED'
        response['error'] = str(e)
        response['traceback'] = traceback.format_exc()
        logger.exception('Test failed')

    finally:
        log_step('disconnecting', '', response)
        session.disconnect()
        log_step('disconnected', '', response)

    logger.info('=== FINAL STATUS: %s ===', response['status'])
    return response
