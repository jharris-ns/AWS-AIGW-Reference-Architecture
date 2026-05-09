#!/bin/bash
set -euo pipefail
#
# Build the paramiko/pyte Lambda Layer.
#
# Usage:
#   podman run --rm --platform linux/amd64 --entrypoint bash \
#     -v "$PWD/scripts:/build" -w /build \
#     public.ecr.aws/lambda/python:3.12 \
#     ./build-tui-layer.sh
#

mkdir -p /tmp/layer/python

pip install paramiko pyte -t /tmp/layer/python/ -q
dnf install -y zip 2>&1 | tail -1

cd /tmp/layer
zip -r /build/pexpect-layer.zip python/ -q

echo "Built: /build/pexpect-layer.zip ($(du -h /build/pexpect-layer.zip | cut -f1))"
