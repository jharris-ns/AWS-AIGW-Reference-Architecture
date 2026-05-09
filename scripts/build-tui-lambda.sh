#!/bin/bash
set -euo pipefail
#
# Package the TUI test Lambda function with libs/tui/ modules.
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT="$SCRIPT_DIR/lambda-tui-test.zip"

echo "=== Packaging TUI test Lambda ==="

cd "$PROJECT_DIR"
zip -j "$OUTPUT" scripts/lambda_tui_test.py
zip -r "$OUTPUT" libs/__init__.py libs/tui/__init__.py libs/tui/paramiko_session.py libs/tui/tui_actions.py libs/tui/tui_screen.py libs/tui/tui_helpers.py libs/tui/menu_config.py libs/tui/config.py

echo "Output: $OUTPUT"
echo "Size:   $(du -h "$OUTPUT" | cut -f1)"
