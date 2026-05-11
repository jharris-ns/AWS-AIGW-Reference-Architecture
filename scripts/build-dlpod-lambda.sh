#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT="$SCRIPT_DIR/lambda-dlpod.zip"

echo "=== Packaging DLPoD Lambda ==="

cd "$PROJECT_DIR"
zip -j "$OUTPUT" scripts/dlpod_handlers.py
zip -r "$OUTPUT" libs/__init__.py libs/tui/__init__.py libs/tui/paramiko_session.py libs/tui/cli_session.py libs/tui/tui_actions.py libs/tui/tui_screen.py libs/tui/tui_helpers.py libs/tui/menu_config.py libs/tui/config.py

echo "Output: $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
