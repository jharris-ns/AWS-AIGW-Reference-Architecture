# AI Gateway + DLP On Demand — Deployment Guide

`templates/gateway-combined.yaml` deploys Netskope AI Gateway and DLP On Demand together in a
single CloudFormation stack. A new VPC is created — no pre-existing networking is required. DLP
On Demand configures itself automatically via `nsbootstrap.service` at first boot using EC2
UserData. AI Gateway enrolls via native Secrets Manager bootstrap, with the DLP On Demand
certificate and endpoint already written into the bootstrap configuration before any instances
launch.

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

Complete all five items before deploying.

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

### 5. AI Guardrails (optional — skip if not using Guardrails)

- [ ] **Guardrails Docker image in S3** — upload `aisecurity-llm.tgz` (supplied by Netskope) to an
  S3 bucket in the same region as the stack:
  ```bash
  aws s3 mb s3://<bucket> --region <region>          # skip if it already exists
  aws s3 cp aisecurity-llm.tgz s3://<bucket>/aisecurity-llm.tgz --region <region>
  ```

- [ ] **Deep Learning Base GPU AMI** — find the latest for your region:
  ```bash
  aws ec2 describe-images --owners amazon --region <region> \
    --filters "Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*" \
    --query 'sort_by(Images,&CreationDate)[-1].[ImageId,Name]' --output text
  ```

- [ ] **GPU instance quota** — AWS Console → Service Quotas → Amazon EC2 → search
  "Running On-Demand G and VT instances". Minimum **4 vCPU** for `g4dn.xlarge` (default).

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

# Verify the template S3 bucket exists
aws s3api head-bucket --bucket <bucket> 2>/dev/null && echo "Bucket exists" || echo "Bucket missing — run scripts/deploy-artifacts.sh <region>"
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
| `DesiredCapacity` | Number | `1` | No | Desired AI Gateway instances (1–4). ASG min is fixed at 1, max at 4. |
| `ScaleOutCpuThreshold` | Number | `70` | No | Average CPU % that triggers AI Gateway scale-out (+1 instance). |

### DLP On Demand appliance

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `DlpodAmiId` | AWS::EC2::Image::Id | `ami-0973780ab75c2fb28` (us-west-1) | No* | DLP On Demand AMI. Default is us-west-1 — override for other regions. |
| `DlpodInstanceType` | String | `c5a.4xlarge` | No | Allowed: `c5a.4xlarge`, `c5a.8xlarge`, `c5a.16xlarge`, `c5ad.4xlarge`, `c5ad.8xlarge`, `c5ad.16xlarge`. |
| `DlpodLicenseKey` | String (NoEcho) | — | Yes | DLP On Demand license key. |
| `DlpodDesiredCapacity` | Number | `1` | No | Desired DLP On Demand instances (1–4). ASG min is fixed at 1, max at 4. |

The DLP On Demand service name is fixed at `dlp.aigw.internal` (private hosted zone `aigw.internal`).

### AI Guardrails (optional)

The Guardrails tier is deployed only when `GuardrailsImageS3Bucket` is set. Leave it empty for the
standard AIG + DLPoD deployment. When enabled, the AIG bootstrap secret gains an
`ai_guardrails.host` entry pointing at `http://guardrails.aigw.internal:<port>/invocations` and
no AIG instance launches until the Guardrails ALB reports every target healthy.

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `GuardrailsImageS3Bucket` | String | `''` (disabled) | No | S3 bucket holding the Netskope `aisecurity-llm.tgz` Docker image tarball. Must be in the same region as the stack. Empty disables the tier. |
| `GuardrailsImageS3Key` | String | `aisecurity-llm.tgz` | No | Object key of the tarball (a `docker save` archive). |
| `GuardrailsAmiId` | String | `''` | Yes, if bucket set | AWS Deep Learning Base GPU AMI (Ubuntu 22.04) — ships NVIDIA driver, Docker and NVIDIA Container Toolkit. Enforced by a template `Rules` assertion. |
| `GuardrailsInstanceType` | String | `g4dn.xlarge` | No | Allowed: `g4dn.xlarge`, `g4dn.2xlarge`, `g5.xlarge`, `g5.2xlarge`. Needs "Running On-Demand G and VT instances" quota ≥ 4 vCPU per instance. |
| `GuardrailsDesiredCapacity` | Number | `1` | No | Desired Guardrails instances (1–4). |
| `GuardrailsContainerPort` | Number | `8080` | No | Container port; also the internal ALB listener port. |
| `GuardrailsHealthCheckPath` | String | `/ping` | No | ALB health check path (expects HTTP 200). |

**Finding the Deep Learning Base GPU AMI for your region:**
```bash
aws ec2 describe-images --owners amazon --region <region> \
  --filters "Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*" \
  --query 'sort_by(Images,&CreationDate)[-1].[ImageId,Name]' --output text
```

**Uploading the Guardrails image tarball to S3** (same procedure as the Terraform POV):
```bash
aws s3 mb s3://<bucket> --region <region>          # skip if it already exists; must be in the stack's region
aws s3 cp aisecurity-llm.tgz s3://<bucket>/aisecurity-llm.tgz --region <region>
```
The tarball is supplied by Netskope. At first boot each Guardrails instance downloads it, runs
`docker load`, and starts the image tag reported by `docker load`.

### VPC

The template creates a new VPC. Four `/24` subnets (two public, two private, across two AZs) are
derived automatically from `VpcCidr` with `Fn::Cidr`; instances use the Amazon-provided DNS
resolver (`169.254.169.253`), so no DNS or subnet parameters are needed.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `VpcCidr` | String | `10.0.0.0/16` | CIDR for the new VPC. Must be large enough for four `/24` subnets. |

---

## Deploy

**Why this step:** CloudFormation reads the template from S3 because it exceeds the 51 KB limit
for direct upload. The template URL tells CloudFormation where to find it. Once submitted,
CloudFormation provisions all resources in the correct dependency order automatically — VPC,
subnets, security groups, IAM roles, Lambda functions, and ASGs. DLPoD instances self-configure
at first boot via `nsbootstrap.service`; AIG instances self-enroll at first boot via Secrets Manager.

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
  --tags Key=Project,Value=aigw Key=Environment,Value=prod Key=ManagedBy,Value=CloudFormation \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>

# To use a custom ACM certificate instead of the auto-generated self-signed cert:
#   add: ParameterKey=AcmCertificateArn,ParameterValue=<arn>
```

All other parameters take their defaults. Override `GatewayAmiId` and `DlpodAmiId` when
deploying outside us-west-1. `Project` and `Environment` are not template parameters — pass them
as `--tags` (shown above) so they propagate to all stack resources.

### Option B — AWS Console

**1. Upload the template to S3** — In your S3 bucket (`netskope-aigw-templates-<account-id>`),
click **Create folder**, name it `templates`, open the folder, click **Upload** → **Add files**,
and select `templates/gateway-combined.yaml` from this repository. Click **Upload**.

If the bucket does not exist yet, run `scripts/deploy-artifacts.sh <region>` to create it.

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

Leave all other parameters at their defaults. Leave `AcmCertificateArn` blank to auto-generate
a self-signed certificate. Override `GatewayAmiId` and `DlpodAmiId` for regions other than
us-west-1.

**5. Configure options** — Under **Tags**, add `Project`, `Environment`, and `ManagedBy` tags
(e.g. `aigw`, `prod`, `CloudFormation`). These propagate to all stack resources. Click **Next**.

**6. Review and submit** — On the review page scroll to the bottom and check:

> ☑ **I acknowledge that AWS CloudFormation might create IAM resources with custom names.**

Click **Submit**.

### Watch stack creation

```bash
aws cloudformation describe-stacks \
  --stack-name <stack-name> \
  --query 'Stacks[0].StackStatus' --output text --region <region>
```

Stack resource creation takes approximately **12–18 minutes**. DLP On Demand instances launch
first; `DlpodReadinessGate` waits (up to 14 minutes) for every DLP On Demand target to be
ALB-healthy, and only then does the AI Gateway ASG create instances. `CREATE_COMPLETE` is
reported once all resources exist — AI Gateway enrollment finishes shortly after.

| Service | Time to load balancer healthy | What's happening |
|---|---|---|
| DLP On Demand | 5–10 min from instance launch | `nsbootstrap.service` applies `bootstrap.json` from UserData (TLS cert, license, DNS) |
| AI Gateway | 5–15 min from instance launch | Lifecycle hook → Activation Lambda registers appliance; instance reads bootstrap secret at boot and self-enrolls |

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

2. DlpodBootstrapResource custom resource assembles bootstrap.json UserData (cert + key + license key)
   DlpodAutoScalingGroup launches DLPoD instances:
   → nsbootstrap.service applies TLS certs, license, DNS, and persona at first boot
   → DlpodReadinessGate polls ALB target health (up to 14 min) before allowing AIG to launch

2b. (Optional, when GuardrailsImageS3Bucket is set — runs in parallel with step 2)
   GuardrailsAutoScalingGroup launches GPU instances:
   → UserData downloads the tarball from S3, `docker load`s it, starts the container on the configured port
   → GuardrailsReadinessGate polls the Guardrails ALB target group (up to 14 min)

3. GatewayAutoScalingGroup launches AIG instances (DependsOn DlpodReadinessGate, and
   GuardrailsReadinessGate via a !Ref when Guardrails is deployed):
   → Activation Lambda registers with Netskope API, writes enrollment token to bootstrap secret
     (plus ai_guardrails.host when Guardrails is deployed)
   → AI Gateway reads bootstrap secret at boot: enrolls + configures DLP (and Guardrails) forwarding
   → DLP forwarding is active from first AI Gateway boot
```

---

## Verify Deployment

### 1. Stack status

```bash
aws cloudformation describe-stacks --stack-name <stack-name> \
  --query 'Stacks[0].StackStatus' --output text --region <region>
```

Expect `CREATE_COMPLETE`.

### 2. DLP On Demand bootstrap

```bash
# nsbootstrap progress (on the instance via SSM Session Manager)
journalctl -u nsbootstrap.service --no-pager

# ALB target health — healthy means nsbootstrap completed and HTTPS is serving
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'<stack-name>-dlpod-tg')].TargetGroupArn" \
  --output text --region <region>)
aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
  --query "TargetHealthDescriptions[*].[Target.Id,TargetHealth.State]" \
  --output table --region <region>
```

`healthy` = nsbootstrap completed and DLP On Demand is serving HTTPS on port 443.

### 3. DLP On Demand ASG state

```bash
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack-name>-dlpod-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region <region>
```

`InService` / `Healthy` means bootstrap completed and load balancer healthy.

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
| `DlpodCertParameterName` | SSM parameter containing the DLP On Demand CA certificate PEM |
| `DlpodBootstrapLogGroup` | DLP On Demand bootstrap builder Lambda log group |
| `GuardrailsServiceUrl` | *(Guardrails only)* Inference URL written to the bootstrap secret as `ai_guardrails.host` |
| `GuardrailsAlbDnsName` | *(Guardrails only)* Guardrails internal ALB DNS name |
| `GuardrailsAutoScalingGroupName` | *(Guardrails only)* Guardrails ASG name |
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
    ParameterKey=AcmCertificateArn,UsePreviousValue=true \
    ParameterKey=<changed-parameter>,ParameterValue=<new-value> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

Changing `GatewayAmiId` triggers an AI Gateway ASG instance refresh — each replaced instance
re-enrolls autonomously. Changing `DlpodAmiId` triggers a DLP On Demand ASG instance refresh —
each replaced instance re-runs `nsbootstrap` from its UserData. See [OPERATIONS.md — AMI Upgrade Procedure](OPERATIONS.md#ami-upgrade-procedure)
for the step-by-step upgrade process.

---

## Teardown

```bash
aws cloudformation delete-stack --stack-name <stack-name> --region <region>
```

Deletes both ASGs, both ALBs, all Lambda functions, Route 53 hosted zone, ACM certificate
(DLP On Demand ALB), IAM roles, Secrets Manager secrets, SSM parameters, and the entire VPC with
subnets and NAT gateway.

Stack deletion takes approximately **8–15 minutes**, dominated by NAT gateway and VPC deletion.

> The ACM certificate passed as `AcmCertificateArn` (for the AI Gateway ALB) is **not** deleted
> — it was imported externally and is not owned by this stack.
