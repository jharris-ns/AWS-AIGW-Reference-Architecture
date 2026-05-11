"""
Lambda handlers for the DLPoD tethering Step Functions state machine.

Single Lambda, action-routed. Each Step Function state passes an
"action" field that determines which handler runs.

DLPoD uses a traditional CLI shell (nsappliance> prompt), not a TUI.

Password handling: each DLPoD instance gets a unique password generated
by the activation Lambda and passed through the state machine execution
state. The license key is shared across all instances and read from
Secrets Manager (DLPOD_SECRET_ARN).
"""
import base64
import json
import os
import re
import ssl
import socket
import time
import logging
import secrets
import string

import paramiko
import boto3

from libs.tui.paramiko_session import ParamikoTUISession, ParamikoConfig
from libs.tui.cli_session import CLISession

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ── Helpers ──

def get_password(event):
    """Get password from event state. Falls back to default."""
    return event.get('password', 'nsappliance')


def get_license_key():
    """Read license key from Secrets Manager (shared across instances)."""
    secret_arn = os.environ['DLPOD_SECRET_ARN']
    client = boto3.client('secretsmanager')
    resp = client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(resp['SecretString'])
    return creds['license_key']


def generate_password(length=24):
    """Generate a random password for DLPoD.

    Must satisfy DLPoD complexity requirements (not 'too simple').
    Ensures at least one uppercase, lowercase, digit, and special char.
    Avoids characters that may cause terminal escape issues.
    """
    special = '!@#%+=_-'
    alphabet = string.ascii_letters + string.digits + special
    while True:
        pw = ''.join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.isupper() for c in pw)
                and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in special for c in pw)):
            return pw


def create_cli_session(instance_ip, password):
    """Create a ParamikoTUISession in CLI mode for DLPoD."""
    config = ParamikoConfig(
        host=instance_ip,
        username='nsadmin',
        password=password,
        mode='cli',
        screen_load_delay=3.0,
        input_delay=0.3,
    )
    session = ParamikoTUISession(config)
    if not session.connect():
        raise RuntimeError(f'Failed to connect to DLPoD at {instance_ip}')
    return session


def extract_cert_via_tls(host, port=443, timeout=10):
    """Extract the server certificate from a TLS handshake.

    Returns the PEM-encoded certificate string.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der_cert = ssock.getpeercert(binary_form=True)

    # Convert DER to PEM
    pem_lines = ['-----BEGIN CERTIFICATE-----']
    b64 = base64.b64encode(der_cert).decode('ascii')
    for i in range(0, len(b64), 64):
        pem_lines.append(b64[i:i + 64])
    pem_lines.append('-----END CERTIFICATE-----')
    return '\n'.join(pem_lines)


# ── Action handlers ──

def handle_dlpod_check_ssh(event):
    """Check if SSH is reachable on the DLPoD instance with password auth."""
    instance_ip = event['dlpod_ip']
    password = get_password(event)

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=instance_ip, username='nsadmin',
            password=password, timeout=10,
            allow_agent=False, look_for_keys=False,
        )
        client.close()
        logger.info('SSH ready on DLPoD %s', instance_ip)
        return {**event, 'ssh_ready': True}
    except Exception as e:
        logger.info('SSH not ready on DLPoD %s: %s', instance_ip, e)
        return {**event, 'ssh_ready': False}


def handle_dlpod_change_password(event):
    """SSH to DLPoD, change the default password.

    Returns the new password in the event for subsequent handlers.
    """
    instance_ip = event['dlpod_ip']
    current_password = get_password(event)
    new_password = generate_password()

    session = create_cli_session(instance_ip, current_password)
    try:
        cli = CLISession(session)
        cli.change_password(new_password)
        logger.info('DLPoD password changed')
        return {**event, 'password': new_password, 'password_changed': True}
    finally:
        session.disconnect()


def handle_dlpod_set_dns(event):
    """SSH to DLPoD, configure DNS servers."""
    instance_ip = event['dlpod_ip']
    password = get_password(event)
    dns_primary = event.get('dns_primary', '')
    dns_secondary = event.get('dns_secondary')

    if not dns_primary:
        logger.info('No DNS server configured, skipping')
        return {**event, 'dns_set': False, 'reason': 'no_dns_primary'}

    session = create_cli_session(instance_ip, password)
    try:
        cli = CLISession(session)
        cli.set_dns(dns_primary, dns_secondary)
        logger.info('DLPoD DNS configured: primary=%s', dns_primary)
        return {**event, 'dns_set': True}
    finally:
        session.disconnect()


def handle_dlpod_set_license(event):
    """SSH to DLPoD, set the license key, save."""
    instance_ip = event['dlpod_ip']
    password = get_password(event)
    license_key = get_license_key()

    session = create_cli_session(instance_ip, password)
    try:
        cli = CLISession(session)
        cli.set_license_key(license_key)
        logger.info('DLPoD license key set')
        return {**event, 'license_set': True}
    finally:
        session.disconnect()


def handle_dlpod_check_tethering(event):
    """SSH to DLPoD, check tethering status."""
    instance_ip = event['dlpod_ip']
    password = get_password(event)

    session = create_cli_session(instance_ip, password)
    try:
        cli = CLISession(session)
        status = cli.check_tethering_status()
        return {
            **event,
            'tethered': status['tethered'],
            'callhome_reachable': status['callhome_reachable'],
            'tenant_url': status['tenant_url'],
            'serial': status['serial'],
        }
    finally:
        session.disconnect()


def handle_dlpod_generate_cert(event):
    """SSH to DLPoD, generate a self-signed certificate."""
    instance_ip = event['dlpod_ip']
    password = get_password(event)
    dlpod_hostname = event.get('dlpod_hostname', instance_ip)

    session = create_cli_session(instance_ip, password)
    try:
        cli = CLISession(session)
        cli.generate_self_signed_cert(common_name=dlpod_hostname)
        logger.info('DLPoD self-signed cert generated for CN=%s', dlpod_hostname)
        # Reboot needed for HTTPS daemon to pick up new cert
        logger.info('Rebooting DLPoD to activate new certificate...')
        cli.send_command('request system reboot', timeout=5)
        time.sleep(1)
        session.child._drain(timeout=1)
        screen = session.get_screen_text()
        if re.search(r'[Yy]es|[Cc]onfirm|[Yy]/[Nn]', screen):
            session.child.send('yes\n')
            time.sleep(1)
        return {**event, 'cert_generated': True, 'rebooting': True}
    finally:
        session.disconnect()


def handle_dlpod_extract_cert(event):
    """Extract the DLPoD server certificate via TLS and store in SSM."""
    instance_ip = event['dlpod_ip']
    cert_param_name = os.environ['DLPOD_CERT_PARAM']

    cert_pem = None
    for attempt in range(10):
        try:
            cert_pem = extract_cert_via_tls(instance_ip, port=443, timeout=10)
            logger.info('Extracted DLPoD cert (attempt %d)', attempt + 1)
            break
        except Exception as e:
            logger.info('TLS connect failed (attempt %d): %s', attempt + 1, e)
            time.sleep(15)

    if not cert_pem:
        raise RuntimeError(
            f'Failed to extract cert from {instance_ip}:443 after 10 attempts')

    ssm = boto3.client('ssm')
    ssm.put_parameter(
        Name=cert_param_name, Value=cert_pem,
        Type='String', Overwrite=True,
    )
    logger.info('DLPoD cert stored in SSM param: %s', cert_param_name)
    return {**event, 'cert_extracted': True, 'cert_param': cert_param_name}


def handle_dlpod_complete_lifecycle(event):
    """Complete the ASG lifecycle action after tethering succeeds."""
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
    logger.info('Completed DLPoD lifecycle action: CONTINUE')
    return {**event, 'lifecycle_completed': True}


# ── Lambda entry point ──

SENSITIVE_KEYS = {'password', 'license_key'}


def redact(obj):
    """Redact sensitive fields for logging."""
    if not isinstance(obj, dict):
        return obj
    return {k: '***' if k in SENSITIVE_KEYS else v for k, v in obj.items()}


def handler(event, context):
    logger.info('Event: %s', json.dumps(redact(event), default=str))

    action = event.get('action', '')
    handlers = {
        'dlpod_check_ssh': handle_dlpod_check_ssh,
        'dlpod_change_password': handle_dlpod_change_password,
        'dlpod_set_dns': handle_dlpod_set_dns,
        'dlpod_set_license': handle_dlpod_set_license,
        'dlpod_check_tethering': handle_dlpod_check_tethering,
        'dlpod_generate_cert': handle_dlpod_generate_cert,
        'dlpod_extract_cert': handle_dlpod_extract_cert,
        'dlpod_complete_lifecycle': handle_dlpod_complete_lifecycle,
    }

    if action not in handlers:
        raise ValueError(f'Unknown action: {action}. Valid: {list(handlers.keys())}')

    result = handlers[action](event)
    logger.info('Result: %s', json.dumps(redact(result), default=str))
    return result
