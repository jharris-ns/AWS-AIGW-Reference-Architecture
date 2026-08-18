#!/bin/bash
set -euo pipefail
#
# Upload Lambda artifacts to S3 for the combined AI Gateway + DLP On Demand template.
#
# Usage (run from the repository root):
#   scripts/deploy-artifacts.sh [region]
#
# Pre-built artifacts in dist/ are used by default — no Docker or build tools required.
# To rebuild from source instead, set REBUILD=1:
#   REBUILD=1 scripts/deploy-artifacts.sh [region]
#
# Override the default bucket name with:
#   LAMBDA_BUCKET=<name> scripts/deploy-artifacts.sh [region]
#

REGION="${1:-us-west-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${LAMBDA_BUCKET:-netskope-aigw-templates-${ACCOUNT_ID}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
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

# Resolve activation Lambda
ACTIVATION_ZIP="$DIST_DIR/lambda-activation.zip"
if [[ "$REBUILD" == "1" ]] || [[ ! -f "$ACTIVATION_ZIP" ]]; then
  echo ""
  echo "Building Activation Lambda..."
  ACTIVATION_ZIP="$SCRIPT_DIR/lambda-activation.zip"
  bash "$SCRIPT_DIR/build-activation-lambda.sh"
else
  echo "Using pre-built Activation Lambda: dist/lambda-activation.zip"
fi

# Resolve enrollment (Step Function) Lambda
ENROLLMENT_ZIP="$DIST_DIR/lambda-step-function.zip"
if [[ "$REBUILD" == "1" ]] || [[ ! -f "$ENROLLMENT_ZIP" ]]; then
  echo ""
  echo "Building Enrollment Lambda..."
  ENROLLMENT_ZIP="$SCRIPT_DIR/lambda-step-function.zip"
  bash "$SCRIPT_DIR/build-step-function-lambda.sh"
else
  echo "Using pre-built Enrollment Lambda: dist/lambda-step-function.zip"
fi

# Resolve DLPoD Lambda
DLPOD_ZIP="$DIST_DIR/lambda-dlpod.zip"
if [[ "$REBUILD" == "1" ]] || [[ ! -f "$DLPOD_ZIP" ]]; then
  echo ""
  echo "Building DLPoD Lambda..."
  DLPOD_ZIP="$SCRIPT_DIR/lambda-dlpod.zip"
  bash "$SCRIPT_DIR/build-dlpod-lambda.sh"
else
  echo "Using pre-built DLPoD Lambda: dist/lambda-dlpod.zip"
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
  echo "Using pre-built Lambda Layer: dist/pexpect-layer.zip"
fi

# Upload
echo ""
echo "Uploading artifacts..."

aws s3 cp "$ACTIVATION_ZIP" \
  "s3://${BUCKET}/lambda-activation.zip" --region "$REGION"

aws s3 cp "$ENROLLMENT_ZIP" \
  "s3://${BUCKET}/lambda-step-function.zip" --region "$REGION"

aws s3 cp "$DLPOD_ZIP" \
  "s3://${BUCKET}/lambda-dlpod.zip" --region "$REGION"

aws s3 cp "$LAYER_ZIP" \
  "s3://${BUCKET}/layers/pexpect-layer.zip" --region "$REGION"

echo ""
echo "=== Upload complete ==="
echo ""
echo "Bucket: s3://${BUCKET}"
echo ""
echo "Deploy with:"
echo "  aws s3 cp templates/gateway-combined.yaml \\"
echo "    s3://${BUCKET}/templates/gateway-combined.yaml --region ${REGION}"
echo ""
echo "  aws cloudformation create-stack \\"
echo "    --stack-name <name> \\"
echo "    --template-url https://${BUCKET}.s3.${REGION}.amazonaws.com/templates/gateway-combined.yaml \\"
echo "    --parameters \\"
echo "      ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \\"
echo "      ParameterKey=NetskopeApiToken,ParameterValue=<token> \\"
echo "      ParameterKey=DlpodLicenseKey,ParameterValue=<license-key> \\"
echo "      ParameterKey=LambdaCodeBucket,ParameterValue=${BUCKET} \\"
echo "      ParameterKey=Project,ParameterValue=aigw \\"
echo "      ParameterKey=Environment,ParameterValue=prod \\"
echo "    --capabilities CAPABILITY_NAMED_IAM \\"
echo "    --region ${REGION}"
echo ""
echo "  # Optional: add ParameterKey=AcmCertificateArn,ParameterValue=<arn> to use a custom cert."
echo "  # Omit it to auto-generate a self-signed certificate."
