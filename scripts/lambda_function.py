"""
Lambda function that reads an SSH private key from Secrets Manager,
connects to an EC2 instance via SSH tunnel, and calls localhost:8080
management APIs on the instance.

Invoke from the Lambda console with a test event:
{
  "instance_ip": "<public or private IP of the test instance>"
}
"""
import json
import io
import http.client
import logging
import boto3
import os
import paramiko

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def get_ssh_private_key():
    """Read the SSH private key from Secrets Manager."""
    secret_arn = os.environ['SSH_KEY_SECRET_ARN']
    client = boto3.client('secretsmanager')
    resp = client.get_secret_value(SecretId=secret_arn)
    return resp['SecretString']


def ssh_connect(host, private_key_pem, username='lambda-automation'):
    """Establish SSH connection to the instance."""
    key = paramiko.RSAKey.from_private_key(io.StringIO(private_key_pem))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=username, pkey=key, timeout=10)
    logger.info('SSH connected to %s@%s', username, host)
    return client


def call_api(ssh_client, method, path, body=None):
    """Call a localhost:8080 API on the remote host via SSH tunnel."""
    transport = ssh_client.get_transport()
    channel = transport.open_channel(
        'direct-tcpip',
        dest_addr=('127.0.0.1', 8080),
        src_addr=('127.0.0.1', 0),
    )

    conn = http.client.HTTPConnection('127.0.0.1', 8080)
    conn.sock = channel

    headers = {'Content-Type': 'application/json'}
    body_bytes = json.dumps(body).encode() if body else None
    conn.request(method, path, body=body_bytes, headers=headers)

    resp = conn.getresponse()
    data = resp.read().decode()
    status = resp.status
    parsed = json.loads(data) if data else {}
    logger.info('%s %s -> %s: %s', method, path, status, parsed)
    return status, parsed


def handler(event, context):
    instance_ip = event['instance_ip']
    username = event.get('username', 'lambda-automation')
    results = []

    logger.info('Starting SSH tunnel test to %s', instance_ip)

    # Read SSH key from Secrets Manager
    private_key_pem = get_ssh_private_key()
    logger.info('Retrieved SSH private key from Secrets Manager')

    # Connect via SSH
    ssh = ssh_connect(instance_ip, private_key_pem, username)

    try:
        tests = [
            ('GET',  '/health',                        None,                          'Health check'),
            ('GET',  '/internal/machine-spec',         None,                          'Machine spec'),
            ('GET',  '/internal/pre-enrollment',       None,                          'Pre-enrollment status'),
            ('POST', '/internal/pre-enrollment',       None,                          'Trigger pre-enrollment'),
            ('PUT',  '/enrollment',                    {'token': 'test-token-abc123'},'Submit enrollment token'),
            ('GET',  '/enrollment',                    None,                          'Check enrollment status'),
            ('POST', '/internal/mgmt/restart-service', {'service_names': ['test']},   'Restart services'),
        ]

        for method, path, body, description in tests:
            status, resp = call_api(ssh, method, path, body)
            results.append({
                'test': description,
                'method': method,
                'path': path,
                'status': status,
                'response': resp,
                'passed': status == 200,
            })

    finally:
        ssh.close()
        logger.info('SSH connection closed')

    all_passed = all(r['passed'] for r in results)
    return {
        'statusCode': 200 if all_passed else 500,
        'body': {
            'instance_ip': instance_ip,
            'all_passed': all_passed,
            'tests': results,
        },
    }
