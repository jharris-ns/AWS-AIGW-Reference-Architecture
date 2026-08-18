#!/bin/bash
set -euo pipefail
#
# Upload DLP On Demand Lambda artifacts to S3.
#
# Usage (run from the repository root):
#   dlpod/scripts/deploy-artifacts.sh [region]
#
# Pre-built artifacts in dist/ are used by default — no Docker or
# build tools required. To rebuild from source instead, set REBUILD=1:
#   REBUILD=1 dlpod/scripts/deploy-artifacts.sh [region]
#
# Override the default bucket name with:
#   LAMBDA_BUCKET=<name> dlpod/scripts/deploy-artifacts.sh [region]
#

REGION="${1:-us-west-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${LAMBDA_BUCKET:-netskope-aigw-templates-${ACCOUNT_ID}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
DIST_DIR="$PROJECT_DIR/dist"
REBUILD="${REBUILD:-0}"

echo "Account:  $ACCOUNT_ID"
echo "Region:   $REGION"
echo "Bucket:   $BUCKET"
echo ""

# Create bucket if it doesn't exist
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "Creating S3 bucket..."
  aws s3 mb "s3://${BUCKET}" --region "$REGION"
else
  echo "Bucket exists"
fi

# Resolve Lambda package
LAMBDA_ZIP="$DIST_DIR/lambda-dlpod.zip"
if [[ "$REBUILD" == "1" ]] || [[ ! -f "$LAMBDA_ZIP" ]]; then
  echo ""
  echo "Building DLP On Demand Lambda..."
  LAMBDA_ZIP="$SCRIPT_DIR/lambda-dlpod.zip"
  rm -f "$LAMBDA_ZIP"
  cd "$PROJECT_DIR"
  zip -j "$LAMBDA_ZIP" dlpod/scripts/dlpod_handlers.py
  zip -r "$LAMBDA_ZIP" \
    libs/__init__.py \
    libs/tui/__init__.py \
    libs/tui/paramiko_session.py \
    libs/tui/cli_session.py \
    libs/tui/tui_actions.py \
    libs/tui/tui_screen.py \
    libs/tui/tui_helpers.py \
    libs/tui/menu_config.py \
    libs/tui/config.py
else
  echo "Using pre-built Lambda: dist/lambda-dlpod.zip"
fi

# Resolve Lambda layer
LAYER_ZIP="$DIST_DIR/pexpect-layer.zip"
if [[ "$REBUILD" == "1" ]] || [[ ! -f "$LAYER_ZIP" ]]; then
  echo ""
  echo "Building Lambda Layer (requires Docker/Podman)..."
  LAYER_ZIP="$SCRIPT_DIR/pexpect-layer.zip"
  podman run --rm --platform linux/amd64 --entrypoint bash \
    -v "$SCRIPT_DIR:/build" -w /build \
    public.ecr.aws/lambda/python:3.12 ./build-tui-layer.sh
else
  echo "Using pre-built layer: dist/pexpect-layer.zip"
fi

# Upload
echo ""
echo "Uploading artifacts..."
aws s3 cp "$LAMBDA_ZIP" \
  "s3://${BUCKET}/lambda-dlpod.zip" --region "$REGION"
aws s3 cp "$LAYER_ZIP" \
  "s3://${BUCKET}/layers/pexpect-layer.zip" --region "$REGION"

echo ""
echo "=== Upload complete ==="
echo ""
echo "Bucket: s3://${BUCKET}"
echo ""
echo "Deploy with:"
echo "  aws s3 cp dlpod/template/gateway-dlpod.yaml \\"
echo "    s3://${BUCKET}/templates/gateway-dlpod.yaml --region ${REGION}"
echo ""
echo "  aws cloudformation create-stack \\"
echo "    --stack-name <name> \\"
echo "    --template-url https://${BUCKET}.s3.${REGION}.amazonaws.com/templates/gateway-dlpod.yaml \\"
echo "    --parameters \\"
echo "      ParameterKey=DlpodLicenseKey,ParameterValue=<license-key> \\"
echo "      ParameterKey=LambdaCodeBucket,ParameterValue=${BUCKET} \\"
echo "      ParameterKey=Project,ParameterValue=aigw \\"
echo "      ParameterKey=Environment,ParameterValue=prod \\"
echo "    --capabilities CAPABILITY_NAMED_IAM \\"
echo "    --region ${REGION}"
