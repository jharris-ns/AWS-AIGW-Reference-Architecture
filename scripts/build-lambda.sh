#!/bin/bash
set -e

# Build the Lambda deployment package with paramiko bundled in.
# Output: scripts/lambda-ssh-tunnel.zip

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/.build"
ZIP_FILE="$SCRIPT_DIR/lambda-ssh-tunnel.zip"

echo "==> Cleaning previous build"
rm -rf "$BUILD_DIR" "$ZIP_FILE"
mkdir -p "$BUILD_DIR"

echo "==> Installing paramiko for Lambda (linux/x86_64)"
pip3 install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --only-binary=:all: \
  --target "$BUILD_DIR" \
  paramiko 2>&1 | tail -3

echo "==> Adding Lambda function code"
cp "$SCRIPT_DIR/lambda_function.py" "$BUILD_DIR/"

echo "==> Creating zip"
cd "$BUILD_DIR"
zip -r "$ZIP_FILE" . -x '*.pyc' '__pycache__/*' '*.dist-info/*' > /dev/null

echo "==> Done: $ZIP_FILE ($(du -h "$ZIP_FILE" | cut -f1))"

# Cleanup
rm -rf "$BUILD_DIR"
