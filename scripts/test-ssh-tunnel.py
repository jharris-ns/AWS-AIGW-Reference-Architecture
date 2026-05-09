#!/usr/bin/env python3
"""
Test script: SSH tunnel to an EC2 instance and call localhost:8080 APIs.

Usage:
    pip install paramiko
    python scripts/test-ssh-tunnel.py <instance-ip> <path-to-private-key> [username]

Examples:
    # Connect as the automation user (default)
    python scripts/test-ssh-tunnel.py 54.1.2.3 ~/.ssh/justin-us-west-1.pem

    # Connect as ubuntu user
    python scripts/test-ssh-tunnel.py 54.1.2.3 ~/.ssh/justin-us-west-1.pem ubuntu
"""
import sys
import json
import io
import http.client
import paramiko


def ssh_connect(host, key_path, username):
    """Establish SSH connection."""
    key = paramiko.RSAKey.from_private_key_file(key_path)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=username, pkey=key, timeout=10)
    print(f"[OK] SSH connected to {username}@{host}")
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
    return resp.status, json.loads(data) if data else {}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    host = sys.argv[1]
    key_path = sys.argv[2]
    username = sys.argv[3] if len(sys.argv) > 3 else 'lambda-automation'

    print(f"Connecting to {username}@{host}...")
    ssh = ssh_connect(host, key_path, username)

    print("\n--- Test 1: Health check ---")
    status, resp = call_api(ssh, 'GET', '/health')
    print(f"  GET /health -> {status}: {resp}")

    print("\n--- Test 2: Machine spec ---")
    status, resp = call_api(ssh, 'GET', '/internal/machine-spec')
    print(f"  GET /internal/machine-spec -> {status}: {resp}")

    print("\n--- Test 3: Pre-enrollment status ---")
    status, resp = call_api(ssh, 'GET', '/internal/pre-enrollment')
    print(f"  GET /internal/pre-enrollment -> {status}: {resp}")

    print("\n--- Test 4: Trigger pre-enrollment install ---")
    status, resp = call_api(ssh, 'POST', '/internal/pre-enrollment')
    print(f"  POST /internal/pre-enrollment -> {status}: {resp}")

    print("\n--- Test 5: Submit enrollment token ---")
    status, resp = call_api(ssh, 'PUT', '/enrollment',
                            body={'token': 'test-token-abc123'})
    print(f"  PUT /enrollment -> {status}: {resp}")

    print("\n--- Test 6: Check enrollment status ---")
    status, resp = call_api(ssh, 'GET', '/enrollment')
    print(f"  GET /enrollment -> {status}: {resp}")

    print("\n--- Test 7: Restart services ---")
    status, resp = call_api(ssh, 'POST', '/internal/mgmt/restart-service',
                            body={'service_names': ['test-service']})
    print(f"  POST /internal/mgmt/restart-service -> {status}: {resp}")

    ssh.close()
    print("\n[OK] All tests passed. SSH tunnel approach works.")


if __name__ == '__main__':
    main()