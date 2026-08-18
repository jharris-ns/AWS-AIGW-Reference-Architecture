# AI Gateway + DLP On Demand — Deployment Guide

`templates/gateway-combined.yaml` deploys Netskope AI Gateway and DLP On Demand together in a
single CloudFormation stack. A new VPC is created — no pre-existing networking is required. DLP
On Demand tethers automatically via SSH/CLI automation. AI Gateway enrolls via native Secrets
Manager bootstrap, with the DLP On Demand certificate and endpoint already written into the
bootstrap configuration before any instances launch.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Preflight Checks](#preflight-checks)
- [Parameters](#parameters)
- [Deploy](#deploy)
- [Startup Ordering](#startup-ordering)
- [Verify Deployment](#verify-deployment)
- [Stack Outputs](#stack-outputs)
- [Update](#update)
- [Teardown](#teardown)

---

## Prerequisites

Complete all six items before deploying.

### 1. AI Gateway AMI

- [ ] Subscribe to the AI Gateway product in [AWS Marketplace](https://aws.amazon.com/marketplace)
  (search "Netskope AI Gateway")

After subscribing, the AMI is available in your AWS account.

> **Region:** The AI Gateway AMI is currently available in **us-west-1 only**. The template
> default is `ami-0a66805d7fb085df4` (AI Gateway v1.7.54, us-west-1).

Verify your subscribed AMI before deploying:
```bash
aws ec2 describe-images \
  --filters 'Name=name,Values=*Netskope AI Gateway*' \
  --query 'sort_by(Images, &CreationDate)[-1].[ImageId,Name,State]' \
  --output table --region us-west-1
```

### 2. DLP On Demand AMI

- [ ] Subscribe to the DLP On Demand product in [AWS Marketplace](https://aws.amazon.com/marketplace)
  (search "Netskope DLP On Demand")

The template default is `ami-0973780ab75c2fb28` (DLP On Demand, us-west-1). Look up the AMI ID
for your region:
```bash
aws ec2 describe-images \
  --filters 'Name=name,Values=*Netskope DLP*' \
  --query 'sort_by(Images, &CreationDate)[-1].[ImageId,Name,State]' \
  --output table --region <region>
```

### 3. Netskope Credentials

- [ ] **Tenant URL** — your Netskope tenant URL, e.g. `https://tenant.goskope.com`

- [ ] **RBAC v3 API token** — service account token with the `AIG Administrator` role:
  1. **Settings → Administration → Administrators & Roles → Roles** — create a role with AI Gateway /
     On-Premises Infrastructure permissions
  2. **Settings → Administration → Administrators & Roles → Administrators** — add a Service Account,
     assign the role, and copy the token (displayed once)

  > Existing REST API v2 tokens continue to work until expiry but cannot be renewed. New
  > deployments should use RBAC v3 service accounts.
  >
  > If you use environment variables: `NETSKOPE_SERVER_URL` → `NetskopeTenantUrl` (strip any
  > `/api/v2` suffix). `NETSKOPE_API_TOKEN` → `NetskopeApiToken`.

- [ ] **DLP On Demand license key** — in your Netskope tenant:
  **Settings → Security Cloud Platform → On-Premises Infrastructure**

### 4. ACM Certificate (optional)

The `AcmCertificateArn` parameter is optional. Leave it empty and the stack auto-generates a
self-signed certificate for the internet-facing AI Gateway ALB (consistent with how the DLP On
Demand internal ALB certificate is handled). The generated cert uses `aig.aigw.internal` as its
CN/SAN and is imported to ACM by the stack.

> **Self-signed cert limitations:** API clients must be configured to trust the cert or disable
> TLS certificate verification. Browsers will show a security warning. For deployments where
> clients cannot be configured to skip verification, provide an ACM certificate ARN.

**Option A — auto-generate (default):** Omit `AcmCertificateArn` from the deploy command.

**Option B — bring your own cert:** Provide an ACM certificate ARN with
`extendedKeyUsage=serverAuth`. For public-facing deployments with a custom domain, use an
ACM-issued certificate with DNS validation via Route 53:

```bash
aws acm request-certificate \
  --domain-name <your-domain> \
  --validation-method DNS \
  --region <region>
# → complete DNS validation in Route 53, then use the certificate ARN
```

For testing without a public domain, import a self-signed cert:
```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout key.pem -out cert.pem -days 365 \
  -subj "/CN=aigw.example.internal" \
  -addext 'subjectAltName=DNS:aigw.example.internal' \
  -addext 'extendedKeyUsage=serverAuth,clientAuth'

aws acm import-certificate \
  --certificate fileb://cert.pem \
  --private-key fileb://key.pem \
  --region <region>
# → use the output CertificateArn as AcmCertificateArn
```

### 5. Lambda Packages in S3

- [ ] Upload Lambda artifacts to an S3 bucket in your target region.

**Why this step:** CloudFormation reads Lambda code from S3 at deploy time. The four artifacts
below must exist in a bucket in **the same region as your stack** before `create-stack` runs.
Pre-built packages are included in the repository's `dist/` folder — no Docker or build tools
are needed.

**Required S3 layout:**
```
s3://<bucket>/
  lambda-activation.zip         # AI Gateway enrollment Lambda
  lambda-step-function.zip      # Enrollment Step Functions Lambda
  lambda-dlpod.zip              # DLP On Demand tethering Lambda
  layers/
    pexpect-layer.zip           # paramiko + pyte Lambda layer (x86_64 Linux)
```

#### Option A — Script

```bash
scripts/deploy-artifacts.sh <region>
```

Creates a bucket named `netskope-aigw-templates-<account-id>` (override with
`LAMBDA_BUCKET=<name>`), uploads all four pre-built artifacts from `dist/`, and prints the bucket
name at the end.

> **Rebuild from source:** Set `REBUILD=1` to rebuild all packages locally instead of using
> pre-built artifacts. The Lambda layer build requires Docker or Podman:
> `REBUILD=1 scripts/deploy-artifacts.sh <region>`

#### Option B — Manual (S3 Console)

**1. Create the bucket** — Open the [S3 Console](https://s3.console.aws.amazon.com/s3/), click
**Create bucket**, and set:
- **Bucket name:** `netskope-aigw-templates-<account-id>` (your 12-digit AWS account ID)
- **Region:** Your target deployment region
- Leave **Block Public Access** fully enabled (default)

**2. Upload the function packages** — Open the bucket. Click **Upload** → **Add files** and
select these three files from the repository:
- `dist/lambda-activation.zip`
- `dist/lambda-step-function.zip`
- `dist/lambda-dlpod.zip`

Click **Upload**. These go in the bucket root.

**3. Upload the Lambda layer** — Click **Create folder**, name it `layers`, click **Create
folder**. Open `layers/`, click **Upload** → **Add files**, select `dist/pexpect-layer.zip`,
click **Upload**.

Note the bucket name — it is required as the `LambdaCodeBucket` stack parameter.

### 6. AWS Permissions

- [ ] The deploying IAM principal has `CAPABILITY_NAMED_IAM` and the required service permissions.

<details>
<summary>IAM policy JSON (click to expand)</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFormation",
      "Effect": "Allow",
      "Action": "cloudformation:*",
      "Resource": "*"
    },
    {
      "Sid": "EC2",
      "Effect": "Allow",
      "Action": "ec2:*",
      "Resource": "*"
    },
    {
      "Sid": "ELB",
      "Effect": "Allow",
      "Action": "elasticloadbalancing:*",
      "Resource": "*"
    },
    {
      "Sid": "AutoScaling",
      "Effect": "Allow",
      "Action": "autoscaling:*",
      "Resource": "*"
    },
    {
      "Sid": "Lambda",
      "Effect": "Allow",
      "Action": "lambda:*",
      "Resource": "*"
    },
    {
      "Sid": "StepFunctions",
      "Effect": "Allow",
      "Action": "states:*",
      "Resource": "*"
    },
    {
      "Sid": "IAM",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PassRole",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:CreateInstanceProfile",
        "iam:DeleteInstanceProfile",
        "iam:GetInstanceProfile",
        "iam:AddRoleToInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SecretsManager",
      "Effect": "Allow",
      "Action": "secretsmanager:*",
      "Resource": "*"
    },
    {
      "Sid": "SSM",
      "Effect": "Allow",
      "Action": [
        "ssm:PutParameter",
        "ssm:GetParameter",
        "ssm:DeleteParameter",
        "ssm:AddTagsToResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SNS",
      "Effect": "Allow",
      "Action": "sns:*",
      "Resource": "*"
    },
    {
      "Sid": "Route53",
      "Effect": "Allow",
      "Action": "route53:*",
      "Resource": "*"
    },
    {
      "Sid": "ACM",
      "Effect": "Allow",
      "Action": "acm:*",
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": "logs:*",
      "Resource": "*"
    },
    {
      "Sid": "S3Bucket",
      "Effect": "Allow",
      "Action": ["s3:CreateBucket", "s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::netskope-aigw-templates-*"
    },
    {
      "Sid": "S3Objects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::netskope-aigw-templates-*/*"
    },
    {
      "Sid": "STS",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
```

> **Production hardening:** The `IAM` statement above uses `Resource: "*"`. For production
> deployments, scope it to your stack name prefix to prevent the deployer from creating roles
> outside the stack's scope:
> ```
> "Resource": [
>   "arn:aws:iam::*:role/<stack-prefix>-*",
>   "arn:aws:iam::*:instance-profile/<stack-prefix>-*"
> ]
> ```
> For example, if your stack name is `aigw-prod`, use `arn:aws:iam::*:role/aigw-prod-*`.

</details>

---

## Preflight Checks

Run these before deploying to catch common blockers early:

```bash
# Verify AWS identity and region
aws sts get-caller-identity
aws configure get region

# Verify the AI Gateway AMI is accessible in your account
aws ec2 describe-images --image-ids <gateway-ami-id> \
  --query 'Images[0].[ImageId,Name,State]' --output table --region <region>

# Verify the DLP On Demand AMI
aws ec2 describe-images --image-ids <dlpod-ami-id> \
  --query 'Images[0].[ImageId,Name,State]' --output table --region <region>

# Verify Netskope API connectivity and token
curl -sf -o /dev/null -w "HTTP %{http_code}\n" \
  -H "Netskope-Api-Token: $NETSKOPE_API_TOKEN" \
  https://<tenant>.goskope.com/api/v2/aig/appliances

# Verify Lambda packages are in S3
aws s3 ls s3://<bucket>/lambda-activation.zip --region <region>
aws s3 ls s3://<bucket>/lambda-step-function.zip --region <region>
aws s3 ls s3://<bucket>/lambda-dlpod.zip --region <region>
aws s3 ls s3://<bucket>/layers/pexpect-layer.zip --region <region>
```

---

## Parameters

### Netskope tenant

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `NetskopeTenantUrl` | String | — | Yes | Netskope tenant URL, e.g. `https://tenant.goskope.com`. |
| `NetskopeApiToken` | String (NoEcho) | — | Yes | RBAC v3 API token with AIG Administrator role. |

### AI Gateway appliance

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `GatewayAmiId` | AWS::EC2::Image::Id | `ami-0a66805d7fb085df4` (us-west-1) | No* | AI Gateway AMI v1.7 or later. Default is us-west-1 only — override for other regions. |
| `InstanceType` | String | `m5.4xlarge` | No | Allowed: `m5.4xlarge`, `m6i.4xlarge`, `c5.4xlarge`. |
| `AcmCertificateArn` | String | `''` (auto-generate) | No | ACM certificate ARN for the internet-facing AI Gateway ALB. Leave empty to auto-generate a self-signed cert. |
| `MinSize` | Number | `1` | No | Minimum AI Gateway instances. |
| `MaxSize` | Number | `4` | No | Maximum AI Gateway instances. |
| `ScaleOutCpuThreshold` | Number | `70` | No | Average CPU % that triggers AI Gateway scale-out (+1 instance). |

### DLP On Demand appliance

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `DlpodAmiId` | AWS::EC2::Image::Id | `ami-0973780ab75c2fb28` (us-west-1) | No* | DLP On Demand AMI. Default is us-west-1 — override for other regions. |
| `DlpodInstanceType` | String | `c5a.4xlarge` | No | Allowed: `c5a.4xlarge`, `c5a.8xlarge`, `c5a.16xlarge`, `c5ad.4xlarge`, `c5ad.8xlarge`, `c5ad.16xlarge`. |
| `DlpodLicenseKey` | String (NoEcho) | — | Yes | DLP On Demand license key. |
| `DlpodMinCapacity` | Number | `1` | No | Minimum DLP On Demand instances. |
| `DlpodMaxCapacity` | Number | `4` | No | Maximum DLP On Demand instances. |
| `DlpodDesiredCapacity` | Number | `1` | No | Initial desired DLP On Demand instances. |
| `DlpDomainName` | String | `dlp.aigw.internal` | No | Private DNS name for the DLP On Demand internal ALB. |

### VPC

The template creates a new VPC. All CIDR parameters have sensible defaults.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `VpcCidr` | String | `10.0.0.0/16` | CIDR for the new VPC. |
| `DnsServer` | String | `10.0.0.2` | VPC DNS resolver — always VPC base address + 2. Override when changing `VpcCidr`. |
| `PublicSubnet1Cidr` | String | `10.0.1.0/24` | Public subnet AZ 1 (AI Gateway ALB + NAT gateway). |
| `PublicSubnet2Cidr` | String | `10.0.2.0/24` | Public subnet AZ 2 (AI Gateway ALB). |
| `PrivateSubnet1Cidr` | String | `10.0.10.0/24` | Private subnet AZ 1 (AI Gateway + DLP On Demand instances + DLP On Demand internal ALB). |
| `PrivateSubnet2Cidr` | String | `10.0.11.0/24` | Private subnet AZ 2. |

### Lambda code (S3)

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `LambdaCodeBucket` | String | — | Yes | S3 bucket containing Lambda artifacts. Must be in the same region as the stack. |
| `DlpodLambdaCodeKey` | String | `lambda-dlpod.zip` | No | S3 key for the DLP On Demand tethering Lambda package. |
| `LambdaLayerKey` | String | `layers/pexpect-layer.zip` | No | S3 key for the paramiko/pyte Lambda layer. |

### Tags

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `Project` | String | — | Yes | Lowercase alphanumeric tag value. |
| `Environment` | String | — | Yes | `dev`, `staging`, or `prod`. |

---

## Deploy

**Why this step:** CloudFormation reads the template from S3 because it exceeds the 51 KB limit
for direct upload. The template URL tells CloudFormation where to find it. Once submitted,
CloudFormation provisions all resources in the correct dependency order automatically — VPC,
subnets, security groups, IAM roles, Lambda functions, ASGs — and wires up the lifecycle hooks
that trigger enrollment and tethering when each instance launches.

### Option A — AWS CLI

```bash
# Upload template to S3
aws s3 cp templates/gateway-combined.yaml \
  s3://<bucket>/templates/gateway-combined.yaml --region <region>

# Deploy
aws cloudformation create-stack \
  --stack-name <stack-name> \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-combined.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=DlpodLicenseKey,ParameterValue=<license-key> \
    ParameterKey=LambdaCodeBucket,ParameterValue=<bucket> \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>

# To use a custom ACM certificate instead of the auto-generated self-signed cert:
#   add: ParameterKey=AcmCertificateArn,ParameterValue=<arn>
```

All other parameters take their defaults. Override `GatewayAmiId` and `DlpodAmiId` when
deploying outside us-west-1.

### Option B — AWS Console

**1. Upload the template to S3** — In your Lambda artifacts bucket from Prerequisites step 5,
click **Create folder**, name it `templates`, open the folder, click **Upload** → **Add files**,
and select `templates/gateway-combined.yaml` from this repository. Click **Upload**.

Your template URL will be:
```
https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-combined.yaml
```

**2. Open CloudFormation** — Open the
[AWS CloudFormation Console](https://console.aws.amazon.com/cloudformation/), confirm the correct
region (top-right corner), and click **Create stack** → **With new resources (standard)**.

**3. Specify the template** — Select **Amazon S3 URL**, paste the template URL from above, and
click **Next**.

**4. Fill in parameters** — Enter a stack name and the required parameters:

| Parameter | Value |
|---|---|
| Stack name | Your chosen name, e.g. `aigw-prod` |
| `NetskopeTenantUrl` | `https://<tenant>.goskope.com` |
| `NetskopeApiToken` | Your RBAC v3 API token |
| `DlpodLicenseKey` | Your DLP On Demand license key |
| `LambdaCodeBucket` | Bucket name from Prerequisites step 5 |
| `Project` | Lowercase label, e.g. `aigw` |
| `Environment` | `dev`, `staging`, or `prod` |

Leave all other parameters at their defaults. Leave `AcmCertificateArn` blank to auto-generate
a self-signed certificate. Override `GatewayAmiId` and `DlpodAmiId` for regions other than
us-west-1.

**5. Configure options** — No changes required. Click **Next**.

**6. Review and submit** — On the review page scroll to the bottom and check:

> ☑ **I acknowledge that AWS CloudFormation might create IAM resources with custom names.**

Click **Submit**.

### Watch stack creation

```bash
aws cloudformation describe-stacks \
  --stack-name <stack-name> \
  --query 'Stacks[0].StackStatus' --output text --region <region>
```

Stack resource creation takes approximately **12–18 minutes**. The DLP On Demand ASG is the last
resource created — CloudFormation waits for tethering to complete and the instance to become
ALB-healthy before marking the stack `CREATE_COMPLETE`. Both enrollment flows run concurrently
once instances launch:

| Service | Time to load balancer healthy | What's happening |
|---|---|---|
| AI Gateway | 5–15 min from instance launch | Reads bootstrap secret at boot and self-enrolls autonomously |
| DLP On Demand | 10–20 min from instance launch | SSH-based tethering automation via Step Functions |

DLP forwarding becomes active once at least one AI Gateway instance and one DLP On Demand
instance are both load balancer healthy.

---

## Startup Ordering

The template enforces this ordering to guarantee the DLP On Demand configuration is in the AI
Gateway bootstrap secret before any AI Gateway instance can launch:

```
1. CertGeneratorFunction custom resource runs
   → Generates DLP On Demand ALB self-signed certificate
   → Imports to ACM
   → Writes PEM to SSM /<stack>/dlpod-cert
   → Pre-populates AIG bootstrap secret with DLP block {certificate, host}

2. GatewayAutoScalingGroup and DlpodAutoScalingGroup launch concurrently
   AIG (DependsOn DlpodAlbCertificate):
   → Activation Lambda registers with Netskope API, writes enrollment token to bootstrap secret
   → AI Gateway reads bootstrap secret at boot: enrolls + configures DLP forwarding
   → DLP forwarding is active from first AI Gateway boot
   DLPoD:
   → Each instance: SNS → DlpodActivationFunction → Step Functions tethering
   → ~10–20 minutes to tether and become load balancer healthy
   → DLP traffic fails gracefully until DLP On Demand tethering completes
```

---

## Verify Deployment

### 1. Stack status

```bash
aws cloudformation describe-stacks --stack-name <stack-name> \
  --query 'Stacks[0].StackStatus' --output text --region <region>
```

Expect `CREATE_COMPLETE`.

### 2. DLP On Demand tethering

```bash
SFN_ARN=$(aws cloudformation describe-stacks --stack-name <stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='DlpodTetheringStateMachineArn'].OutputValue" \
  --output text --region <region>)

aws stepfunctions list-executions --state-machine-arn "$SFN_ARN" \
  --query "executions[*].[name,status,startDate]" \
  --output table --region <region>
```

`SUCCEEDED` = tethered. `RUNNING` = in progress (normal for up to 25 minutes).

### 3. DLP On Demand ASG state

```bash
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack-name>-dlpod-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region <region>
```

`InService` / `Healthy` means tethered and load balancer healthy.

### 4. AI Gateway bootstrap secret

```bash
aws secretsmanager get-secret-value \
  --secret-id <stack-name>-aig-bootstrap \
  --query SecretString --output text --region <region>
```

The secret should contain `enrollment_token`, `dlp.certificate`, and `dlp.host`.

### 5. AI Gateway ASG state

```bash
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack-name>-aig-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region <region>
```

`InService` / `Healthy` means enrolled and serving HTTPS on port 443.

### 6. AI Gateway ALB target health

```bash
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'<stack-name>-aig-tg')].TargetGroupArn" \
  --output text --region <region>)

aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
  --query "TargetHealthDescriptions[*].[Target.Id,TargetHealth.State]" \
  --output table --region <region>
```

`healthy` = enrolled and serving HTTPS on port 443.

---

## Stack Outputs

| Output | Description |
|---|---|
| `AigAlbDnsName` | AI Gateway internet-facing ALB DNS name — create a CNAME to this value |
| `AigAutoScalingGroupName` | AI Gateway ASG name |
| `AigBootstrapSecretName` | AI Gateway Secrets Manager bootstrap secret name |
| `AigAlbCertParameterName` | SSM parameter containing the AI Gateway ALB self-signed cert PEM |
| `AigActivationLogGroup` | AI Gateway activation Lambda log group |
| `AigScaleOutAlarmName` | CloudWatch alarm that triggers AI Gateway scale-out |
| `DlpodServiceUrl` | DLP On Demand private HTTPS URL (`https://dlp.aigw.internal`) |
| `DlpodCertParameterName` | SSM parameter containing the DLP On Demand ALB self-signed cert PEM |
| `DlpodTetheringStateMachineArn` | DLP On Demand tethering Step Functions ARN |
| `DlpodActivationLogGroup` | DLP On Demand activation Lambda log group |
| `DlpodLambdaLogGroup` | DLP On Demand tethering Lambda log group |
| `VpcId` | VPC ID |

**Retrieve all outputs at once:**
```bash
aws cloudformation describe-stacks --stack-name <stack-name> \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table --region <region>
```

---

## Update

```bash
aws cloudformation update-stack \
  --stack-name <stack-name> \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-combined.yaml \
  --parameters \
    ParameterKey=NetskopeApiToken,UsePreviousValue=true \
    ParameterKey=DlpodLicenseKey,UsePreviousValue=true \
    ParameterKey=LambdaCodeBucket,UsePreviousValue=true \
    ParameterKey=AcmCertificateArn,UsePreviousValue=true \
    ParameterKey=<changed-parameter>,ParameterValue=<new-value> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

Changing `GatewayAmiId` triggers an AI Gateway ASG instance refresh — each replaced instance
re-enrolls autonomously. Changing `DlpodAmiId` triggers a DLP On Demand ASG instance refresh —
each replaced instance goes through full tethering. See [OPERATIONS.md — AMI Upgrade Procedure](OPERATIONS.md#ami-upgrade-procedure)
for the step-by-step upgrade process.

---

## Teardown

```bash
aws cloudformation delete-stack --stack-name <stack-name> --region <region>
```

Deletes both ASGs (lifecycle hooks fire and complete gracefully), both ALBs, all Lambda functions,
the DLP On Demand tethering Step Functions state machine, Route 53 hosted zone, ACM certificate
(DLP On Demand ALB), IAM roles, Secrets Manager secrets, SSM parameters, and the entire VPC with
subnets and NAT gateway.

Stack deletion takes approximately **8–15 minutes**, dominated by NAT gateway and VPC deletion.

> The ACM certificate passed as `AcmCertificateArn` (for the AI Gateway ALB) is **not** deleted
> — it was imported externally and is not owned by this stack.
