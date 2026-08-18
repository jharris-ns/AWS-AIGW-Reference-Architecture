# AI Gateway — Deployment Guide

`aig/template/gateway-aig.yaml` deploys a Netskope AI Gateway Auto Scaling Group with native
Secrets Manager bootstrap enrollment and step scaling.

The template includes one Lambda function — the **Activation Lambda** — which runs on every
instance launch and termination. On launch it calls the Netskope API to register the appliance
and writes the enrollment token to Secrets Manager; the instance reads that token at boot and
self-enrolls autonomously. On termination it calls the Netskope API to deregister the appliance.

**No S3 upload is required for Lambda code.** The Activation Lambda code is embedded directly
in the template (`ZipFile`), so there is no artifact build or upload step. The template itself
is ~34 KB and can be deployed with `--template-body` from a local file — no S3 template upload
is needed either. Compare with the combined template (`templates/gateway-combined.yaml`) or the
DLPoD standalone template, which both require pre-built Lambda packages uploaded to S3 because
they use larger, multi-file Lambda code with third-party library layers.

## Table of Contents

- [What You'll Need](#what-youll-need)
- [Step 1 — Subscribe to the AMI](#step-1--subscribe-to-the-ami)
- [Step 2 — Prepare an ACM Certificate](#step-2--prepare-an-acm-certificate)
- [Step 3 — Preflight Checks](#step-3--preflight-checks)
- [Parameters](#parameters)
- [Stack Outputs](#stack-outputs)
- [Step 4 — Deploy the Stack](#step-4--deploy-the-stack)
- [Step 5 — Verify Enrollment](#step-5--verify-enrollment)
- [Update](#update)
- [Teardown](#teardown)

---

## What You'll Need

Before starting, confirm you have all of the following:

- [ ] **AWS account** with permissions for CloudFormation, EC2, IAM, ELB, Auto Scaling, Lambda,
  Secrets Manager, SNS, CloudWatch, and SSM
- [ ] **AWS CLI** installed and configured (`aws sts get-caller-identity` should return your account)
- [ ] **Netskope tenant URL** — `https://<tenant>.goskope.com`
- [ ] **RBAC v3 API token** — service account token with the `AIG Administrator` role. Not the
  legacy API key. See [Step 1](#step-1--subscribe-to-the-ami) for where to find it.
- [ ] **ACM certificate ARN** in the target region — see [Step 2](#step-2--prepare-an-acm-certificate)
- [ ] **AIG AMI subscribed** in your account — see [Step 1](#step-1--subscribe-to-the-ami)

> **No Lambda artifact upload required.** Unlike the combined or DLP On Demand standalone
> templates, this template embeds the Activation Lambda code inline. There is no build step and
> no S3 bucket required — the template deploys directly from a local file.

---

## Step 1 — Subscribe to the AMI

The AI Gateway AMI must be subscribed to in AWS Marketplace before the stack can launch instances.
Subscription is free — you pay only for EC2 instance hours.

1. Go to [AWS Marketplace](https://aws.amazon.com/marketplace) and search for **Netskope AI Gateway**
2. Click **Continue to Subscribe**
3. Accept the terms and click **Accept Terms**
4. Wait for the subscription to activate (typically 1–2 minutes)

**Find the AMI ID for your region after subscribing:**

```bash
aws ec2 describe-images \
  --filters 'Name=name,Values=*Netskope AI Gateway*' \
  --query 'sort_by(Images, &CreationDate)[-1].[ImageId,Name,CreationDate]' \
  --output table --region <region>
```

The template default (`ami-0a66805d7fb085df4`) is AIG Gateway v1.7.54 in **us-west-1 only** —
override `GatewayAmiId` when deploying in any other region.

> **Version requirement:** AIG v1.7 or later is required for native Secrets Manager bootstrap
> enrollment. Earlier versions will fail to enroll silently. Verify the AMI name includes `v1.7`
> or later before proceeding.

**Get your RBAC v3 API token** (if you don't have one):
1. Log in to your Netskope tenant
2. **Settings → Administration → Administrators & Roles → Roles** — create a role with AI Gateway /
   On-Premises Infrastructure permissions (or use an existing `AIG Administrator` role)
3. **Settings → Administration → Administrators & Roles → Administrators** — add a Service Account,
   assign the role, and copy the token (displayed once only)

---

## Step 2 — Prepare an ACM Certificate

**Why this step:** The AI Gateway ALB terminates HTTPS on port 443. ACM provides the TLS
certificate the ALB presents to clients. The certificate must exist in ACM in the same region as
your stack before deployment, because CloudFormation references the `CertificateArn` when
creating the HTTPS listener.

The certificate must have `extendedKeyUsage=serverAuth`. Two options:

### Option A — Public certificate (production, custom domain)

Use this when clients need to trust the certificate without any special configuration.

**Via AWS Console:**
1. Open the [ACM Console](https://console.aws.amazon.com/acm/) in your target region
2. Click **Request a certificate** → **Request a public certificate** → **Next**
3. Enter your domain name (e.g. `aigw.example.com`)
4. Select **DNS validation** → **Request**
5. Follow the prompts to add the CNAME record to your DNS (Route 53: click **Create records in
   Route 53** for automatic validation)
6. Wait for status to change from **Pending validation** to **Issued** (typically 1–5 minutes
   with Route 53, longer with other DNS providers)
7. Copy the **Certificate ARN** — it is required as `AcmCertificateArn` in Step 4

**Via AWS CLI:**
```bash
aws acm request-certificate \
  --domain-name <your-domain> \
  --validation-method DNS \
  --region <region>
# Complete DNS validation in Route 53, then use the CertificateArn output
```

### Option B — Self-signed certificate (testing/internal use)

Use this for non-production deployments where clients can be configured to skip TLS verification
or trust the certificate manually.

> **Self-signed cert limitation:** API clients must be configured to trust the cert or disable
> TLS certificate verification. Browsers will show a security warning.

**Step 1 — Generate the certificate (requires openssl, run locally):**

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout key.pem -out cert.pem -days 365 \
  -subj "/CN=aigw.example.internal" \
  -addext 'subjectAltName=DNS:aigw.example.internal' \
  -addext 'extendedKeyUsage=serverAuth,clientAuth' \
  -addext 'keyUsage=digitalSignature,keyEncipherment'
```

**Step 2 — Import to ACM via Console:**
1. Open the [ACM Console](https://console.aws.amazon.com/acm/) in your target region
2. Click **Import a certificate**
3. Paste the contents of `cert.pem` into **Certificate body**
4. Paste the contents of `key.pem` into **Certificate private key**
5. Leave **Certificate chain** blank
6. Click **Next** → **Import**
7. Copy the **Certificate ARN** — it is required as `AcmCertificateArn` in Step 4

**Step 2 (alternative) — Import to ACM via CLI:**
```bash
aws acm import-certificate \
  --certificate fileb://cert.pem \
  --private-key fileb://key.pem \
  --region <region>
# Use the CertificateArn from the output as AcmCertificateArn
```

---

## Step 3 — Preflight Checks

Run these before deploying to catch common blockers:

```bash
# Verify AWS identity and region
aws sts get-caller-identity
aws configure get region

# Verify the AMI is accessible in your account
aws ec2 describe-images --image-ids <gateway-ami-id> \
  --query 'Images[0].[ImageId,Name,State]' --output table --region <region>

# Verify the ACM certificate exists and is ISSUED
aws acm describe-certificate --certificate-arn <cert-arn> \
  --query 'Certificate.[DomainName,Status,KeyUsages]' --output table --region <region>

# Verify Netskope API connectivity and token
curl -sf -o /dev/null -w "HTTP %{http_code}\n" \
  -H "Netskope-Api-Token: $NETSKOPE_API_TOKEN" \
  https://<tenant>.goskope.com/api/v2/aig/appliances
# Expect HTTP 200
```

---

## Parameters

### Netskope credentials

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `NetskopeTenantUrl` | String | — | Yes | Tenant URL, e.g. `https://tenant.goskope.com` |
| `NetskopeApiToken` | String (NoEcho) | — | Yes | RBAC v3 API token (service account) |

### Gateway configuration

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `GatewayAmiId` | AWS::EC2::Image::Id | `ami-0a66805d7fb085df4` (v1.7.54, us-west-1) | No* | AIG appliance AMI v1.7 or later. Default is us-west-1 only — override for other regions. |
| `InstanceType` | String | `m5.4xlarge` | No | Allowed: `m5.4xlarge`, `m6i.4xlarge`, `c5.4xlarge` |
| `DesiredCapacity` | Number | `1` | No | Desired instances (1–4). ASG min is fixed at 1, max at 4. |
| `ScaleOutCpuThreshold` | Number | `70` | No | CPU % that triggers +1 instance scale-out |
| `AcmCertificateArn` | String | — | Yes | ACM cert ARN for the ALB HTTPS listener |

### VPC — new (default)

Leave all `Existing*` parameters empty to have the stack create a new VPC.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `VpcCidr` | String | `10.0.0.0/16` | CIDR block for the new VPC |
| `PublicSubnetCidr` | String | `10.0.1.0/24` | Public subnet AZ 1 CIDR |
| `PublicSubnet2Cidr` | String | `10.0.2.0/24` | Public subnet AZ 2 CIDR |
| `PrivateSubnetCidr` | String | `10.0.10.0/24` | Private subnet AZ 1 CIDR |
| `PrivateSubnet2Cidr` | String | `10.0.11.0/24` | Private subnet AZ 2 CIDR |

### VPC — existing

Supply all five to use an existing VPC instead of creating a new one.

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `ExistingVpcId` | String | `''` | No | Existing VPC ID. Leave empty to create a new VPC. |
| `ExistingPublicSubnetId` | String | `''` | No | Public subnet AZ 1 (ALB) |
| `ExistingPublicSubnet2Id` | String | `''` | No | Public subnet AZ 2 (ALB) |
| `ExistingPrivateSubnetId` | String | `''` | No | Private subnet AZ 1 (gateway instances) |
| `ExistingPrivateSubnet2Id` | String | `''` | No | Private subnet AZ 2 (gateway instances) |

### Tags

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `Project` | String | — | Yes | Lowercase alphanumeric tag value |
| `Environment` | String | — | Yes | `dev`, `staging`, or `prod` |

---

## Stack Outputs

| Output | Description |
|---|---|
| `AlbDnsName` | DNS name of the internet-facing ALB — configure a CNAME to this |
| `VpcId` | VPC used by the stack (created or existing) |
| `AutoScalingGroupName` | ASG name for operational commands |
| `ScaleOutAlarmName` | CloudWatch alarm that triggers scale-out |
| `BootstrapSecretName` | Secrets Manager secret name — verify enrollment token was written |
| `ActivationLogGroup` | CloudWatch log group for the activation Lambda |

**Retrieve all outputs:**

```bash
aws cloudformation describe-stacks --stack-name <stack-name> \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table --region <region>
```

---

## Step 4 — Deploy the Stack

**Why this step:** Submitting the stack tells CloudFormation to provision all resources in
dependency order — VPC (if new), subnets, security groups, IAM roles, Secrets Manager secret,
Lambda function, SNS topic, and the Auto Scaling Group. The lifecycle hook on the ASG fires
for each instance and triggers the Activation Lambda, which registers the appliance with the
Netskope API and writes the enrollment token to Secrets Manager. The instance reads that token
at boot and self-enrolls autonomously.

The template is ~34 KB — small enough to deploy from a local file via the CLI, or upload directly
in the CloudFormation console.

### Option A — CLI, new VPC (stack creates all networking)

```bash
aws cloudformation create-stack \
  --stack-name <stack-name> \
  --template-body file://aig/template/gateway-aig.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=GatewayAmiId,ParameterValue=<ami-id> \
    ParameterKey=AcmCertificateArn,ParameterValue=<cert-arn> \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

All `Existing*` and CIDR parameters take their defaults — the stack creates a `10.0.0.0/16` VPC
with public and private subnets in two AZs and a NAT gateway.

### Option B — CLI, existing VPC

Supply all five `Existing*` parameters. The private subnets must have a route to a NAT gateway —
AIG needs internet access for Netskope enrollment and Secrets Manager reads.

```bash
aws cloudformation create-stack \
  --stack-name <stack-name> \
  --template-body file://aig/template/gateway-aig.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=GatewayAmiId,ParameterValue=<ami-id> \
    ParameterKey=AcmCertificateArn,ParameterValue=<cert-arn> \
    ParameterKey=ExistingVpcId,ParameterValue=<vpc-id> \
    ParameterKey=ExistingPublicSubnetId,ParameterValue=<pub-subnet-az1> \
    ParameterKey=ExistingPublicSubnet2Id,ParameterValue=<pub-subnet-az2> \
    ParameterKey=ExistingPrivateSubnetId,ParameterValue=<priv-subnet-az1> \
    ParameterKey=ExistingPrivateSubnet2Id,ParameterValue=<priv-subnet-az2> \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

### Option C — CLI, deploy from S3

Use this if you want to store the template in S3 for repeatable updates.

```bash
# Upload template to S3 first
aws s3 cp aig/template/gateway-aig.yaml \
  s3://<bucket>/templates/gateway-aig.yaml --region <region>

# Deploy using template URL
aws cloudformation create-stack \
  --stack-name <stack-name> \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-aig.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=GatewayAmiId,ParameterValue=<ami-id> \
    ParameterKey=AcmCertificateArn,ParameterValue=<cert-arn> \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

### Option D — AWS Console (no CLI required for deploy)

**1. Open CloudFormation** — Open the
[AWS CloudFormation Console](https://console.aws.amazon.com/cloudformation/), confirm the correct
region (top-right corner), and click **Create stack** → **With new resources (standard)**.

**2. Specify the template** — Select **Upload a template file**, click **Choose file**, and select
`aig/template/gateway-aig.yaml` from this repository. Click **Next**.

**3. Fill in parameters** — Enter a stack name and the required parameters:

| Parameter | Value |
|---|---|
| Stack name | Your chosen name, e.g. `aig-prod` |
| `NetskopeTenantUrl` | `https://<tenant>.goskope.com` |
| `NetskopeApiToken` | Your RBAC v3 API token |
| `GatewayAmiId` | AMI ID from Step 1 (default is us-west-1 only) |
| `AcmCertificateArn` | Certificate ARN from Step 2 |
| `Project` | Lowercase label, e.g. `aigw` |
| `Environment` | `dev`, `staging`, or `prod` |

For an **existing VPC**, also fill in all five `ExistingVpcId`, `ExistingPublicSubnetId`,
`ExistingPublicSubnet2Id`, `ExistingPrivateSubnetId`, and `ExistingPrivateSubnet2Id` parameters.
Leave all `Existing*` fields blank to create a new VPC.

Click **Next**.

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

Stack resource creation takes approximately **2–3 minutes** with an existing VPC, or **5–7
minutes** when creating a new VPC (NAT gateway provisioning adds time). After `CREATE_COMPLETE`,
bootstrapping continues in the background:

| Phase | Time | What's happening |
|---|---|---|
| Instance launch | 0–2 min | ASG launches first instance; lifecycle hook holds it in `Pending:Wait` |
| Activation Lambda | ~1–2 min | Registers appliance with Netskope API; writes enrollment token to Secrets Manager |
| Instance self-enrollment | 5–15 min | Instance reads Secrets Manager at boot, enrolls, and the ALB health check passes |

The instance is serving traffic once its ALB target health shows `healthy`. Use Step 5 to verify.

---

## Step 5 — Verify Enrollment

Run these checks in order after `CREATE_COMPLETE`.

### 1. Instance lifecycle state

```bash
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack-name>-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region <region>
```

`InService` / `Healthy` = enrolled. `Pending:Wait` = Activation Lambda running (completes within
2 minutes). `ABANDONED` = Activation Lambda failed — check logs immediately.

### 2. Activation Lambda logs

```bash
aws logs tail /aws/lambda/<stack-name>-aig-activation \
  --since 30m --region <region>
```

A successful enrollment logs:
```
[INFO] Registered appliance id=<id> name=<name>
[INFO] Completing lifecycle action: CONTINUE
```

### 3. ALB target health

```bash
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'<stack-name>')].TargetGroupArn" \
  --output text --region <region>)

aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
  --query "TargetHealthDescriptions[*].[Target.Id,TargetHealth.State,TargetHealth.Description]" \
  --output table --region <region>
```

`healthy` = enrolled and serving HTTPS on port 443.

### 4. Netskope portal

Log in to your Netskope tenant → **Settings → Security Cloud Platform → AI Gateway**. The
appliance should appear with status **Connected** within 5–15 minutes of the lifecycle hook
completing.

### 5. Quick connectivity test

```bash
ALB=$(aws cloudformation describe-stacks --stack-name <stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='AlbDnsName'].OutputValue" \
  --output text --region <region>)

curl -sk -o /dev/null -w "HTTP %{http_code}\n" https://$ALB/
```

`HTTP 200` or `HTTP 401` confirms the AI Gateway is serving requests.

---

## Update

```bash
aws cloudformation update-stack \
  --stack-name <stack-name> \
  --template-body file://aig/template/gateway-aig.yaml \
  --parameters \
    ParameterKey=NetskopeApiToken,UsePreviousValue=true \
    ParameterKey=AcmCertificateArn,UsePreviousValue=true \
    ParameterKey=<changed-parameter>,ParameterValue=<new-value> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

Changing `GatewayAmiId` triggers an ASG instance refresh — the ASG terminates each existing
instance and replaces it with the new AMI, cycling through full enrollment for each replacement.

---

## Teardown

```bash
aws cloudformation delete-stack --stack-name <stack-name> --region <region>
```

Stack deletion deregisters all appliances from the Netskope tenant (termination lifecycle hook →
Activation Lambda → `DELETE /api/v2/aig/appliances/{id}`), then deletes the ALB, ASG, IAM roles,
Secrets Manager secrets, SNS topic, Lambda function, and VPC (if created by this stack).

**Check for orphaned appliances** if the stack delete fails or was interrupted:

```bash
curl -s -H "Netskope-Api-Token: <token>" \
  "https://<tenant>.goskope.com/api/v2/aig/appliances" | \
  python3 -c "import json,sys; [print(a['id'], a['name'], a['status']) for a in json.load(sys.stdin)['data'] if '<stack-name>' in a.get('name','')]"

# Delete an orphaned appliance
curl -s -X DELETE -H "Netskope-Api-Token: <token>" \
  "https://<tenant>.goskope.com/api/v2/aig/appliances/<appliance-id>"
```
