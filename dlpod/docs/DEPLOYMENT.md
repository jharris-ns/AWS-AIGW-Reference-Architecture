# DLP On Demand — Deployment Guide

`dlpod/template/gateway-dlpod.yaml` deploys an Auto Scaling Group of Netskope DLP On Demand
appliances behind a private internal ALB. Each instance is automatically tethered to the Netskope
management plane via SSH/CLI automation orchestrated by a Step Functions state machine. The
tethered service is reachable at a private Route 53 DNS name (`dlp.aigw.internal` by default)
within the stack's VPC.

The template creates its own VPC — no pre-existing networking is required.

## Table of Contents

- [What You'll Need](#what-youll-need)
- [Step 1 — Subscribe to the AMI](#step-1--subscribe-to-the-ami)
- [Step 2 — Upload to S3](#step-2--upload-to-s3)
- [Step 3 — Preflight Checks](#step-3--preflight-checks)
- [Parameters](#parameters)
- [Stack Outputs](#stack-outputs)
- [Step 4 — Deploy the Stack](#step-4--deploy-the-stack)
- [Step 5 — Verify Tethering](#step-5--verify-tethering)
- [Deploying AI Gateway in the Same VPC](#deploying-ai-gateway-in-the-same-vpc)
- [Configuring AIG to Use DLPoD](#configuring-aig-to-use-dlpod)
- [Update](#update)
- [Teardown](#teardown)

---

## What You'll Need

Before starting, confirm you have all of the following:

- [ ] **AWS account** with permissions for CloudFormation, EC2, IAM, ELB, Auto Scaling, Lambda,
  Step Functions, Secrets Manager, SNS, Route 53, ACM, SSM, and CloudWatch
- [ ] **AWS CLI** installed and configured (`aws sts get-caller-identity` should return your account)
- [ ] **Docker or Podman** — only needed if rebuilding Lambda artifacts from source (`REBUILD=1`).
  Pre-built artifacts in `dist/` are used by default and require no build tools.
- [ ] **DLPoD AMI subscribed** in your account — see [Step 1](#step-1--subscribe-to-the-ami)
- [ ] **DLP On Demand license key** — from your Netskope tenant:
  **Settings → Security Cloud Platform → On-Premises Infrastructure**

---

## Step 1 — Subscribe to the AMI

The DLP On Demand AMI must be subscribed to in AWS Marketplace before the stack can launch
instances. Subscription is free — you pay only for EC2 instance hours.

1. Go to [AWS Marketplace](https://aws.amazon.com/marketplace) and search for **Netskope DLP On Demand**
2. Click **Continue to Subscribe**
3. Accept the terms and click **Accept Terms**
4. Wait for the subscription to activate (typically 1–2 minutes)

**Find the AMI ID for your region after subscribing:**

```bash
aws ec2 describe-images \
  --filters 'Name=name,Values=*Netskope DLP*' \
  --query 'sort_by(Images, &CreationDate)[-1].[ImageId,Name,CreationDate]' \
  --output table --region <region>
```

The template default (`ami-0973780ab75c2fb28`) is for **us-west-1 only** — override `DlpodAmiId`
when deploying in any other region.

---

## Step 2 — Upload to S3

**Why this step:** Three files must be in S3 before `create-stack` runs — all in the same region
as your stack. The Lambda function package and layer are read by CloudFormation when it creates
the tethering Lambda. The template itself must be in S3 because CloudFormation requires a URL
for templates this size. Pre-built Lambda artifacts are included in `dist/` — no Docker or
build tools are needed.

**Required S3 layout:**
```
s3://<bucket>/
  lambda-dlpod.zip
  layers/
    pexpect-layer.zip
  templates/
    gateway-dlpod.yaml
```

### Option A — Script + one CLI command (recommended)

```bash
# Run from the repository root — creates bucket and uploads Lambda artifacts
dlpod/scripts/deploy-artifacts.sh <region>

# Then upload the template (use the bucket name printed by the script above)
BUCKET=netskope-aigw-templates-<account-id>
REGION=<region>
aws s3 cp dlpod/template/gateway-dlpod.yaml \
  s3://$BUCKET/templates/gateway-dlpod.yaml --region $REGION
```

The script creates a bucket named `netskope-aigw-templates-<account-id>` if it doesn't exist
(override with `LAMBDA_BUCKET=<name>`). Note the bucket name — it is the `LambdaCodeBucket`
stack parameter.

> **Rebuilding from source:** Set `REBUILD=1` to rebuild Lambda artifacts instead of using the
> pre-built versions. The layer build requires Docker or Podman:
> ```bash
> REBUILD=1 dlpod/scripts/deploy-artifacts.sh <region>
> ```

### Option B — Manual (S3 Console)

**1. Create the bucket** — Open the [S3 Console](https://s3.console.aws.amazon.com/s3/), click
**Create bucket**, and set:
- **Bucket name:** `netskope-aigw-templates-<account-id>` (your 12-digit AWS account ID)
- **Region:** Your target deployment region
- Leave **Block Public Access** fully enabled (default)

Click **Create bucket**. Note the bucket name — it is the `LambdaCodeBucket` parameter value.

**2. Upload the Lambda function package** — Open the bucket. Click **Upload** → **Add files**,
select `dist/lambda-dlpod.zip` from this repository, and click **Upload**. This file goes
in the bucket root.

**3. Upload the Lambda layer** — Click **Create folder**, name it `layers`, click **Create
folder**. Open the `layers/` folder, click **Upload** → **Add files**, select
`dist/pexpect-layer.zip`, and click **Upload**.

**4. Upload the template** — Navigate back to the bucket root. Click **Create folder**, name it
`templates`, click **Create folder**. Open the `templates/` folder, click **Upload** →
**Add files**, select `dlpod/template/gateway-dlpod.yaml`, and click **Upload**.

Your template URL (needed in Step 4) will be:
```
https://netskope-aigw-templates-<account-id>.s3.<region>.amazonaws.com/templates/gateway-dlpod.yaml
```

---

## Step 3 — Preflight Checks

Run these before deploying to catch common blockers:

```bash
# Verify AWS identity and region
aws sts get-caller-identity
aws configure get region

# Verify the DLPoD AMI is accessible in your account
aws ec2 describe-images --image-ids <dlpod-ami-id> \
  --query 'Images[0].[ImageId,Name,State]' --output table --region <region>

# Verify Lambda artifacts are in S3
aws s3 ls s3://<bucket>/lambda-dlpod.zip --region <region>
aws s3 ls s3://<bucket>/layers/pexpect-layer.zip --region <region>
```

---

## Parameters

### DLPoD appliance

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `DlpodAmiId` | AWS::EC2::Image::Id | `ami-0973780ab75c2fb28` (us-west-1) | No* | DLPoD AMI. Default is us-west-1 — override for all other regions. |
| `DlpodInstanceType` | String | `c5a.4xlarge` | No | Allowed: `c5a.4xlarge`, `c5a.8xlarge`, `c5a.16xlarge`, `c5ad.4xlarge`, `c5ad.8xlarge`, `c5ad.16xlarge`. |
| `DlpodLicenseKey` | String (NoEcho) | — | Yes | DLPoD license key. Never logged or stored in plaintext. |

### Capacity

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `DlpodMinCapacity` | Number | `1` | No | Minimum instances. |
| `DlpodMaxCapacity` | Number | `4` | No | Maximum instances. |
| `DlpodDesiredCapacity` | Number | `1` | No | Initial desired instances. |

### Service DNS

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `DlpDomainName` | String | `dlp.aigw.internal` | No | Private DNS name for the DLPoD ALB. Must be a subdomain of `aigw.internal`. |

### VPC

The template creates a new VPC. All CIDR parameters take sensible defaults.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `VpcCidr` | String | `10.0.0.0/16` | CIDR for the new VPC. |
| `DnsServer` | String | `10.0.0.2` | VPC DNS resolver — always VPC base address + 2. Override when changing `VpcCidr`. |
| `PublicSubnet1Cidr` | String | `10.0.1.0/24` | Public subnet AZ 1 (NAT gateway). |
| `PublicSubnet2Cidr` | String | `10.0.2.0/24` | Public subnet AZ 2. |
| `PrivateSubnet1Cidr` | String | `10.0.10.0/24` | Private subnet AZ 1 (DLPoD instances + internal ALB). |
| `PrivateSubnet2Cidr` | String | `10.0.11.0/24` | Private subnet AZ 2. |

### Lambda code (S3)

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `LambdaCodeBucket` | String | — | Yes | S3 bucket containing Lambda artifacts. Must be in the same region as the stack. |
| `DlpodLambdaCodeKey` | String | `lambda-dlpod.zip` | No | S3 key for the tethering Lambda package. |
| `LambdaLayerKey` | String | `layers/pexpect-layer.zip` | No | S3 key for the paramiko/pyte Lambda layer. |

### Tags

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `Project` | String | — | Yes | Lowercase alphanumeric tag value. |
| `Environment` | String | — | Yes | `dev`, `staging`, or `prod`. |

---

## Stack Outputs

### Networking (feed into `gateway-aig.yaml` `Existing*` parameters)

| Output | Description | AIG parameter |
|---|---|---|
| `VpcId` | VPC ID | `ExistingVpcId` |
| `PublicSubnet1Id` | Public subnet AZ 1 | `ExistingPublicSubnetId` |
| `PublicSubnet2Id` | Public subnet AZ 2 | `ExistingPublicSubnet2Id` |
| `PrivateSubnet1Id` | Private subnet AZ 1 | `ExistingPrivateSubnetId` |
| `PrivateSubnet2Id` | Private subnet AZ 2 | `ExistingPrivateSubnet2Id` |
| `PrivateHostedZoneId` | Route 53 hosted zone ID (`aigw.internal`) | — |

### DLPoD service (feed into AIG DLP configuration)

| Output | Description |
|---|---|
| `DlpodServiceUrl` | `https://dlp.aigw.internal` — use as the DLP endpoint in AIG config |
| `DlpodCertParameterName` | SSM parameter name containing the self-signed cert PEM |
| `DlpodAlbSecurityGroupId` | ALB SG ID — add AIG instance ingress rule to allow DLP traffic |
| `DlpodAlbDnsName` | Internal ALB DNS name (verification only — use `DlpodServiceUrl` in config) |

### Operational

| Output | Description |
|---|---|
| `TetheringStateMachineArn` | Step Functions ARN — monitor tethering in the console |
| `DlpodActivationLogGroup` | Activation Lambda log group |
| `DlpodLambdaLogGroupName` | Tethering Lambda log group |

**Retrieve all outputs at once:**

```bash
aws cloudformation describe-stacks --stack-name <stack-name> \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table --region <region>
```

---

## Step 4 — Deploy the Stack

**Why this step:** Submitting the stack tells CloudFormation to provision all resources in
dependency order — VPC, subnets, security groups, IAM roles, Lambda functions, Step Functions
state machine, and the Auto Scaling Group. The lifecycle hook on the ASG fires for each instance
and triggers the automated tethering flow via the Lambda and state machine.

### Option A — AWS CLI

```bash
BUCKET=netskope-aigw-templates-<account-id>
REGION=<region>
STACK=<stack-name>

aws cloudformation create-stack \
  --stack-name $STACK \
  --template-url https://$BUCKET.s3.$REGION.amazonaws.com/templates/gateway-dlpod.yaml \
  --parameters \
    ParameterKey=DlpodLicenseKey,ParameterValue=<license-key> \
    ParameterKey=LambdaCodeBucket,ParameterValue=$BUCKET \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $REGION
```

All other parameters take their defaults. Override `DlpodAmiId` when deploying outside us-west-1.

### Option B — AWS Console

**1. Open CloudFormation** — Open the
[AWS CloudFormation Console](https://console.aws.amazon.com/cloudformation/), confirm the correct
region (top-right corner), and click **Create stack** → **With new resources (standard)**.

**2. Specify the template** — Select **Amazon S3 URL** and paste the template URL from Step 2:
```
https://netskope-aigw-templates-<account-id>.s3.<region>.amazonaws.com/templates/gateway-dlpod.yaml
```
Click **Next**.

**3. Fill in parameters** — Enter a stack name and the required parameters:

| Parameter | Value |
|---|---|
| Stack name | Your chosen name, e.g. `dlpod-prod` |
| `DlpodLicenseKey` | Your DLP On Demand license key |
| `LambdaCodeBucket` | Bucket name from Step 2 |
| `Project` | Lowercase label, e.g. `aigw` |
| `Environment` | `dev`, `staging`, or `prod` |

Leave all other parameters at their defaults. Override `DlpodAmiId` for regions other than
us-west-1. Click **Next**.

**4. Configure options** — No changes required. Click **Next**.

**5. Review and submit** — Scroll to the bottom and check:

> ☑ **I acknowledge that AWS CloudFormation might create IAM resources with custom names.**

Click **Submit**.

### Watch stack creation

```bash
aws cloudformation describe-stacks \
  --stack-name <stack-name> \
  --query 'Stacks[0].StackStatus' --output text --region <region>
```

Stack resource creation takes approximately **5–8 minutes** to reach `CREATE_COMPLETE`. After the
stack finishes, the DLPoD instance continues tethering in the background. Allow **15–25 minutes**
from instance launch for tethering to complete and the instance to become ALB-healthy.

---

## Step 5 — Verify Tethering

Run these checks in order after `CREATE_COMPLETE`.

### 1. Instance lifecycle state

```bash
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack-name>-dlpod-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region <region>
```

`InService` / `Healthy` = tethered. `Pending:Wait` = tethering in progress (normal for up to
25 minutes). `ABANDONED` = tethering failed — check Step Functions and Lambda logs.

### 2. Step Functions tethering execution

```bash
SFN_ARN=$(aws cloudformation describe-stacks --stack-name <stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='TetheringStateMachineArn'].OutputValue" \
  --output text --region <region>)

aws stepfunctions list-executions --state-machine-arn "$SFN_ARN" \
  --query "executions[*].[name,status,startDate]" \
  --output table --region <region>
```

`SUCCEEDED` = tethered. `RUNNING` = in progress. `FAILED` = tethering failed — check the
tethering Lambda logs.

### 3. Tethering Lambda logs

```bash
aws logs tail /aws/lambda/<stack-name>-dlpod \
  --since 30m --region <region>
```

Successful tethering ends with:
```
[INFO] Tethering complete
[INFO] Completing lifecycle action: CONTINUE
```

### 4. ALB target health

```bash
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'<stack-name>')].TargetGroupArn" \
  --output text --region <region>)

aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
  --query "TargetHealthDescriptions[*].[Target.Id,TargetHealth.State]" \
  --output table --region <region>
```

`healthy` = tethered and serving HTTPS on port 443.

---

## Deploying AI Gateway in the Same VPC

After the DLPoD stack is up and tethered, deploy the AI Gateway template
(`aig/template/gateway-aig.yaml`) into the same VPC using the DLPoD stack outputs.

```bash
DLPOD_STACK=<dlpod-stack-name>
REGION=<region>

# Read networking outputs from the DLPoD stack
VPC_ID=$(aws cloudformation describe-stacks --stack-name $DLPOD_STACK \
  --query "Stacks[0].Outputs[?OutputKey=='VpcId'].OutputValue" \
  --output text --region $REGION)
PUB1=$(aws cloudformation describe-stacks --stack-name $DLPOD_STACK \
  --query "Stacks[0].Outputs[?OutputKey=='PublicSubnet1Id'].OutputValue" \
  --output text --region $REGION)
PUB2=$(aws cloudformation describe-stacks --stack-name $DLPOD_STACK \
  --query "Stacks[0].Outputs[?OutputKey=='PublicSubnet2Id'].OutputValue" \
  --output text --region $REGION)
PRIV1=$(aws cloudformation describe-stacks --stack-name $DLPOD_STACK \
  --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnet1Id'].OutputValue" \
  --output text --region $REGION)
PRIV2=$(aws cloudformation describe-stacks --stack-name $DLPOD_STACK \
  --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnet2Id'].OutputValue" \
  --output text --region $REGION)

# Upload the AIG template to S3 (required — ~34 KB)
BUCKET=netskope-aigw-templates-<account-id>
aws s3 cp aig/template/gateway-aig.yaml \
  s3://$BUCKET/templates/gateway-aig.yaml --region $REGION

# Deploy AIG into the DLPoD VPC
aws cloudformation create-stack \
  --stack-name <aig-stack-name> \
  --template-url https://$BUCKET.s3.$REGION.amazonaws.com/templates/gateway-aig.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=AcmCertificateArn,ParameterValue=<cert-arn> \
    ParameterKey=ExistingVpcId,ParameterValue=$VPC_ID \
    ParameterKey=ExistingPublicSubnetId,ParameterValue=$PUB1 \
    ParameterKey=ExistingPublicSubnet2Id,ParameterValue=$PUB2 \
    ParameterKey=ExistingPrivateSubnetId,ParameterValue=$PRIV1 \
    ParameterKey=ExistingPrivateSubnet2Id,ParameterValue=$PRIV2 \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $REGION
```

> The DLPoD private subnets (`10.0.10.0/24`, `10.0.11.0/24`) host both DLPoD instances and the
> internal ALB. AIG instances placed in the same private subnets resolve `dlp.aigw.internal`
> directly via the Route 53 private zone — no VPC peering or additional routing required.

---

## Configuring AIG to Use DLPoD

For AIG to forward traffic to DLPoD for inline DLP inspection, the AIG bootstrap secret in
Secrets Manager must include a `dlp` block with the DLPoD service URL and TLS certificate.

> **Note:** The `gateway-combined.yaml` combined template handles this automatically — the cert
> generator custom resource writes the DLP block into the AIG bootstrap secret before any instances
> launch. The steps below are only needed when operating AIG and DLPoD as separate stacks.

**The AIG bootstrap secret format with DLP configured:**

```json
{
  "bootstrap": true,
  "enrollment_token": "<token>",
  "dlp": {
    "certificate": "-----BEGIN CERTIFICATE-----\n<base64>\n-----END CERTIFICATE-----",
    "host": "https://dlp.aigw.internal"
  }
}
```

Both `certificate` and `host` are required. Newlines in the PEM must be encoded as `\n`.

**Read the DLPoD certificate from SSM and write it to the AIG bootstrap secret:**

```bash
DLPOD_STACK=<dlpod-stack-name>
AIG_SECRET=<aig-bootstrap-secret-name>
REGION=<region>

# Get the SSM parameter name from stack outputs
CERT_PARAM=$(aws cloudformation describe-stacks --stack-name $DLPOD_STACK \
  --query "Stacks[0].Outputs[?OutputKey=='DlpodCertParameterName'].OutputValue" \
  --output text --region $REGION)

# Read the cert PEM
CERT=$(aws ssm get-parameter --name "$CERT_PARAM" \
  --query Parameter.Value --output text --region $REGION)

# Get the DLPoD service URL
DLP_HOST=$(aws cloudformation describe-stacks --stack-name $DLPOD_STACK \
  --query "Stacks[0].Outputs[?OutputKey=='DlpodServiceUrl'].OutputValue" \
  --output text --region $REGION)

# Encode cert newlines for JSON and write the bootstrap secret
CERT_JSON=$(echo "$CERT" | awk '{printf "%s\\n", $0}')

aws secretsmanager update-secret \
  --secret-id $AIG_SECRET \
  --secret-string "{\"bootstrap\":true,\"enrollment_token\":\"<token>\",\"dlp\":{\"certificate\":\"${CERT_JSON}\",\"host\":\"${DLP_HOST}\"}}" \
  --region $REGION
```

---

## Update

```bash
# Re-upload the template before updating if it was modified
aws s3 cp dlpod/template/gateway-dlpod.yaml \
  s3://<bucket>/templates/gateway-dlpod.yaml --region <region>

aws cloudformation update-stack \
  --stack-name <stack-name> \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-dlpod.yaml \
  --parameters \
    ParameterKey=DlpodLicenseKey,UsePreviousValue=true \
    ParameterKey=LambdaCodeBucket,UsePreviousValue=true \
    ParameterKey=<changed-parameter>,ParameterValue=<new-value> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

Changing `DlpodAmiId` triggers an ASG instance refresh — each replaced instance goes through the
full tethering flow (~15–25 minutes per instance).

---

## Teardown

```bash
aws cloudformation delete-stack --stack-name <stack-name> --region <region>
```

The stack deletes the ASG (termination lifecycle hook fires and completes gracefully), internal
ALB, Lambda functions, Step Functions state machine, Route 53 hosted zone, ACM certificate, IAM
roles, Secrets Manager secret, SSM parameters, and the entire VPC with subnets and NAT gateway.

Stack deletion takes approximately **5–10 minutes**, dominated by NAT gateway and VPC deletion.
