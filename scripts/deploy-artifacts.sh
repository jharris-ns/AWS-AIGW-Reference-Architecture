#!/bin/bash
set -euo pipefail
#
# Create the S3 bucket used to host CloudFormation templates.
#
# Usage (run from the repository root):
#   scripts/deploy-artifacts.sh [region]
#
# Override the default bucket name with:
#   LAMBDA_BUCKET=<name> scripts/deploy-artifacts.sh [region]
#
# All Lambda functions in the combined and standalone templates use inline ZipFile code —
# no Lambda artifacts need to be uploaded. The bucket is required only because CloudFormation
# requires an S3 URL for templates that exceed the 51 KB direct-upload limit.
#

REGION="${1:-us-west-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${LAMBDA_BUCKET:-netskope-aigw-templates-${ACCOUNT_ID}}"

echo "Account:  $ACCOUNT_ID"
echo "Region:   $REGION"
echo "Bucket:   $BUCKET"
echo ""

# Create bucket if it doesn't exist
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "Creating S3 bucket..."
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
  fi
else
  echo "Bucket exists"
fi

echo ""
echo "=== Bucket ready ==="
echo ""
echo "Upload and deploy the combined template:"
echo ""
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
echo "      ParameterKey=Project,ParameterValue=aigw \\"
echo "      ParameterKey=Environment,ParameterValue=prod \\"
echo "    --capabilities CAPABILITY_NAMED_IAM \\"
echo "    --region ${REGION}"
