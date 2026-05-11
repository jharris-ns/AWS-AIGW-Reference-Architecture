#!/bin/bash
set -euo pipefail
#
# Package the activation Lambda function.
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT="$SCRIPT_DIR/lambda-activation.zip"

echo "=== Packaging Activation Lambda ==="

cd "$SCRIPT_DIR"
zip -j "$OUTPUT" activation_handler.py

echo "Output: $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
