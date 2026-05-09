#!/bin/bash
set -euo pipefail
#
# Upload CloudFormation template and Lambda artifacts to S3.
#
# Usage:
#   scripts/deploy-artifacts.sh [region]
#
# Creates the S3 bucket if it doesn't exist, builds the Lambda
# package and Layer if not already built, and uploads everything.
#

REGION="${1:-us-west-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="netskope-aigw-templates-${ACCOUNT_ID}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

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

# Build Lambda package if missing
if [ ! -f "$SCRIPT_DIR/lambda-step-function.zip" ]; then
  echo ""
  echo "Building Lambda package..."
  bash "$SCRIPT_DIR/build-step-function-lambda.sh"
fi

# Build Layer if missing
if [ ! -f "$SCRIPT_DIR/pexpect-layer.zip" ]; then
  echo ""
  echo "Building Lambda Layer (requires Docker/Podman)..."
  podman run --rm --platform linux/amd64 --entrypoint bash \
    -v "$SCRIPT_DIR:/build" -w /build \
    public.ecr.aws/lambda/python:3.12 ./build-tui-layer.sh
fi

# Upload
echo ""
echo "Uploading artifacts..."

aws s3 cp "$PROJECT_DIR/templates/gateway-asg.yaml" \
  "s3://${BUCKET}/templates/gateway-asg.yaml" --region "$REGION"

aws s3 cp "$SCRIPT_DIR/lambda-step-function.zip" \
  "s3://${BUCKET}/lambda-step-function.zip" --region "$REGION"

aws s3 cp "$SCRIPT_DIR/pexpect-layer.zip" \
  "s3://${BUCKET}/layers/pexpect-layer.zip" --region "$REGION"

echo ""
echo "=== Upload complete ==="
echo ""
echo "Bucket:       s3://${BUCKET}"
echo "Template URL: https://${BUCKET}.s3.${REGION}.amazonaws.com/templates/gateway-asg.yaml"
echo ""
echo "Deploy with:"
echo "  aws cloudformation create-stack \\"
echo "    --stack-name <name> \\"
echo "    --template-url https://${BUCKET}.s3.${REGION}.amazonaws.com/templates/gateway-asg.yaml \\"
echo "    --parameters \\"
echo "      ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \\"
echo "      ParameterKey=NetskopeApiToken,ParameterValue=<token> \\"
echo "      ParameterKey=AcmCertificateArn,ParameterValue=<acm-arn> \\"
echo "      ParameterKey=GatewayAmiId,ParameterValue=<ami-id> \\"
echo "      ParameterKey=LambdaCodeBucket,ParameterValue=${BUCKET} \\"
echo "    --capabilities CAPABILITY_NAMED_IAM \\"
echo "    --region ${REGION}"
