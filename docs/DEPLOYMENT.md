# Deployment Guide

This guide covers building and uploading the deployment artifacts, and deploying the CloudFormation stack.

---

## Prerequisites

- AWS CLI configured with credentials that have the permissions listed in the [README](../README.md)
- Docker or Podman (for building the Lambda Layer on the correct architecture)
- Netskope tenant URL and RBAC v3 API token (see [README](../README.md#2-netskope-tenant-credentials))
- ACM certificate ARN (see [Certificate Management](CERTIFICATE_MANAGEMENT.md))
- AI Gateway AMI ID in the target region

---

## Quick Start

The `deploy-artifacts.sh` script handles everything — creates the S3 bucket, builds all three Lambda packages + Layer + template, and uploads them:

```bash
# Default region (us-west-1)
scripts/deploy-artifacts.sh

# Specify region
scripts/deploy-artifacts.sh us-east-1
```

Then deploy the stack. The template exceeds 51KB and must be deployed via S3 using `--template-url`:

```bash
aws cloudformation create-stack \
  --stack-name my-aigw \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-asg.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=GatewayAmiId,ParameterValue=<ami-id> \
    ParameterKey=LambdaCodeBucket,ParameterValue=<bucket-from-script-output> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

To deploy with DLPoD (DLP on Demand), add the following parameters:

```bash
aws cloudformation create-stack \
  --stack-name my-aigw \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-asg.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=GatewayAmiId,ParameterValue=<ami-id> \
    ParameterKey=LambdaCodeBucket,ParameterValue=<bucket-from-script-output> \
    ParameterKey=DlpodAmiId,ParameterValue=<dlpod-ami> \
    ParameterKey=DlpodLicenseKey,ParameterValue=<license-key> \
    ParameterKey=DnsServer,ParameterValue=<vpc-dns-ip> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

---

## Manual Steps

If you prefer to run each step individually:

### 1. Create the S3 bucket

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-1
BUCKET="netskope-aigw-templates-${ACCOUNT_ID}"

aws s3 mb "s3://${BUCKET}" --region "${REGION}"
```

The bucket must be in the **same region** as the CloudFormation stack.

### 2. Build the Lambda Layer

The Layer contains `paramiko` and `pyte` (Python packages for SSH and terminal emulation). It must be built on **x86_64 Linux** to match the Lambda runtime.

```bash
podman run --rm --platform linux/amd64 --entrypoint bash \
  -v "$PWD/scripts:/build" -w /build \
  public.ecr.aws/lambda/python:3.12 ./build-tui-layer.sh
```

This produces `scripts/pexpect-layer.zip` (~10 MB).

**Note:** On Apple Silicon (M1/M2/M3), the `--platform linux/amd64` flag is required. Without it, the Layer will contain ARM binaries that fail in Lambda.

### 3. Build the Lambda packages

```bash
bash scripts/build-step-function-lambda.sh
bash scripts/build-dlpod-lambda.sh
```

This produces:
- `scripts/lambda-step-function.zip` (~20 KB) — Step Functions handler code and TUI automation libraries
- `scripts/lambda-dlpod.zip` — DLPoD CLI automation Lambda

### 4. Upload to S3

```bash
aws s3 cp scripts/lambda-activation.zip "s3://${BUCKET}/lambda-activation.zip" --region "${REGION}"
aws s3 cp scripts/lambda-step-function.zip "s3://${BUCKET}/lambda-step-function.zip" --region "${REGION}"
aws s3 cp scripts/lambda-dlpod.zip "s3://${BUCKET}/lambda-dlpod.zip" --region "${REGION}"
aws s3 cp scripts/pexpect-layer.zip "s3://${BUCKET}/layers/pexpect-layer.zip" --region "${REGION}"
aws s3 cp templates/gateway-asg.yaml "s3://${BUCKET}/templates/gateway-asg.yaml" --region "${REGION}"
```

The template exceeds 51KB and must be uploaded to S3 and deployed using `--template-url`.

### S3 bucket layout

```
s3://<bucket>/
  lambda-activation.zip         # Activation Lambda package (~4 KB)
  lambda-step-function.zip      # Enrollment Lambda package (~20 KB)
  lambda-dlpod.zip              # DLPoD CLI automation Lambda
  layers/
    pexpect-layer.zip           # paramiko/pyte Lambda Layer (~10 MB)
  templates/
    gateway-asg.yaml            # CloudFormation template (>51 KB)
```

### 5. Deploy the stack

See the [README](../README.md#deployment) for deployment commands (new VPC or existing VPC).

---

## Updating an Existing Deployment

To update the CloudFormation template, re-upload it to S3 first:

```bash
aws s3 cp templates/gateway-asg.yaml "s3://${BUCKET}/templates/gateway-asg.yaml" --region "${REGION}"

aws cloudformation update-stack \
  --stack-name <stack-name> \
  --template-url https://${BUCKET}.s3.${REGION}.amazonaws.com/templates/gateway-asg.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "${REGION}"
```

To update the Lambda code without replacing the stack:

```bash
# Rebuild and upload
bash scripts/build-step-function-lambda.sh
aws s3 cp scripts/lambda-step-function.zip "s3://${BUCKET}/lambda-step-function.zip" --region "${REGION}"

# Update the function directly
aws lambda update-function-code \
  --function-name <stack-name>-enrollment \
  --s3-bucket "${BUCKET}" \
  --s3-key lambda-step-function.zip \
  --region "${REGION}"
```

To update the Lambda Layer:

```bash
# Rebuild and upload
podman run --rm --platform linux/amd64 --entrypoint bash \
  -v "$PWD/scripts:/build" -w /build \
  public.ecr.aws/lambda/python:3.12 ./build-tui-layer.sh
aws s3 cp scripts/pexpect-layer.zip "s3://${BUCKET}/layers/pexpect-layer.zip" --region "${REGION}"

# Publish new version and update function
LAYER_ARN=$(aws lambda publish-layer-version \
  --layer-name <stack-name>-paramiko \
  --content S3Bucket="${BUCKET}",S3Key=layers/pexpect-layer.zip \
  --compatible-runtimes python3.12 \
  --region "${REGION}" \
  --query LayerVersionArn --output text)

aws lambda update-function-configuration \
  --function-name <stack-name>-enrollment \
  --layers "${LAYER_ARN}" \
  --region "${REGION}"
```
