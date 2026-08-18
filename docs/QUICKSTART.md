# Quick Start — AI Gateway + DLP On Demand

Get both services deployed and traffic flowing in under 20 minutes. This guide is written for
Netskope customers and sales engineers — AWS CLI experience helpful but not required. A console
alternative is provided for deploy.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full parameter reference and advanced options.

## Table of Contents

- [What You'll Need](#what-youll-need)
- [Step 1 — Subscribe to AMIs](#step-1--subscribe-to-amis)
- [Step 2 — Build and Upload Lambda Packages](#step-2--build-and-upload-lambda-packages)
- [Step 3 — Deploy the Stack](#step-3--deploy-the-stack)
- [What to Expect](#what-to-expect)
- [Verify Deployment](#verify-deployment)
- [Next Steps](#next-steps)

---

## What You'll Need

Complete this checklist before starting. All five items are required.

- [ ] **AWS account** with IAM permissions to deploy CloudFormation stacks with `CAPABILITY_NAMED_IAM`.
  A minimal IAM policy is in [DEPLOYMENT.md](DEPLOYMENT.md#6-aws-permissions).

- [ ] **AWS CLI** installed and configured (`aws configure` or environment variables set).
  Verify with: `aws sts get-caller-identity`

- [ ] **Netskope tenant URL** — your tenant URL in the form `https://tenant.goskope.com`.
  Find this in your browser when logged into the Netskope portal.

- [ ] **Netskope RBAC v3 API token** — a service account token with the `AIG Administrator` role.

  > **Where to find it in the Netskope portal:**
  > 1. **Settings → Administration → Administrators & Roles → Roles** — create a role with
  >    AI Gateway / On-Premises Infrastructure permissions (or use an existing AIG Administrator role)
  > 2. **Settings → Administration → Administrators & Roles → Administrators** — add a Service Account,
  >    assign the role, and copy the token shown (it is displayed once only)

- [ ] **DLP On Demand license key** — your DLPoD license key.

  > **Where to find it in the Netskope portal:**
  > **Settings → Security Cloud Platform → On-Premises Infrastructure**

> **Apple Silicon (M1/M2/M3):** Docker or Podman is required to build the Lambda layer in Step 2.
> Install [Docker Desktop](https://docs.docker.com/desktop/install/mac-install/) or Podman before
> continuing.

---

## Step 1 — Subscribe to AMIs

Both the AI Gateway and DLP On Demand AMIs must be subscribed to in AWS Marketplace before the
stack can launch instances. Subscription is free — you pay only for EC2 instance hours.

**AI Gateway:**
1. Go to [AWS Marketplace](https://aws.amazon.com/marketplace) and search for **Netskope AI Gateway**
2. Click **Continue to Subscribe**
3. Accept the terms and click **Accept Terms**
4. Wait for the subscription to activate (typically 1–2 minutes)

**DLP On Demand:**
1. In AWS Marketplace, search for **Netskope DLP On Demand**
2. Click **Continue to Subscribe**
3. Accept the terms and click **Accept Terms**

> **Region note:** The AI Gateway AMI default (`ami-0a66805d7fb085df4`) is for **us-west-1 only**.
> If deploying in a different region, look up the AMI ID after subscribing:
> ```bash
> aws ec2 describe-images \
>   --filters 'Name=name,Values=*Netskope AI Gateway*' \
>   --query 'sort_by(Images, &CreationDate)[-1].[ImageId,Name]' \
>   --output table --region <your-region>
> ```
> Pass the result as `GatewayAmiId` (and similarly `DlpodAmiId`) in Step 3.

---

## Step 2 — Upload Lambda Packages

**Why this step:** CloudFormation cannot create the Lambda functions until their code packages
exist in S3. The stack needs four artifacts — three Lambda function packages and one Lambda layer —
uploaded to an S3 bucket **in the same region as your stack**. The bucket name becomes the
`LambdaCodeBucket` stack parameter. Pre-built artifacts are included in the repository's `dist/`
folder, so no build tools are required.

### Option A — Script (recommended)

```bash
scripts/deploy-artifacts.sh <region>
```

This creates a bucket named `netskope-aigw-templates-<account-id>` in the target region, uploads
all four pre-built artifacts from `dist/`, and prints the bucket name at the end.

> **Need to rebuild from source?** Set `REBUILD=1` before running the script. This requires
> Docker Desktop or Podman for the Lambda layer build step:
> `REBUILD=1 scripts/deploy-artifacts.sh <region>`

### Option B — Manual (AWS Console)

**1. Create the S3 bucket**

Open the [S3 Console](https://s3.console.aws.amazon.com/s3/) and click **Create bucket**.

- **Bucket name:** `netskope-aigw-templates-<your-account-id>` (replace with your 12-digit AWS account ID)
- **Region:** Select your target deployment region
- **Block Public Access:** Leave all four checkboxes enabled (default)
- All other settings: leave as defaults

Click **Create bucket**.

> Note the bucket name — it is the value you will enter for `LambdaCodeBucket` in Step 3.

**2. Upload the Lambda function packages**

Open the bucket you just created. Click **Upload** → **Add files**, then select all three files
from the `dist/` folder in this repository:

- `dist/lambda-activation.zip`
- `dist/lambda-step-function.zip`
- `dist/lambda-dlpod.zip`

Click **Upload**. These three files go in the bucket root.

**3. Upload the Lambda layer**

The Lambda layer must be in a `layers/` prefix (subfolder) inside the bucket.

Click **Create folder**, enter `layers`, then click **Create folder**.

Open the `layers/` folder, click **Upload** → **Add files**, and select:

- `dist/pexpect-layer.zip`

Click **Upload**.

**4. Verify the layout**

Your bucket should contain:
```
netskope-aigw-templates-<account-id>/
  lambda-activation.zip
  lambda-step-function.zip
  lambda-dlpod.zip
  layers/
    pexpect-layer.zip
```

---

## Step 3 — Deploy the Stack

**Why this step:** The CloudFormation template is over 51 KB, which exceeds the limit for direct
upload. It must be stored in S3 first, then CloudFormation reads it from there. You can deploy
from the AWS CLI or entirely from the AWS Console.

### Option A — AWS CLI

```bash
BUCKET=netskope-aigw-templates-<account-id>   # from Step 2
REGION=<region>
STACK=<stack-name>

# Upload the template to S3
aws s3 cp templates/gateway-combined.yaml \
  s3://$BUCKET/templates/gateway-combined.yaml --region $REGION

# Deploy the stack
aws cloudformation create-stack \
  --stack-name $STACK \
  --template-url https://$BUCKET.s3.$REGION.amazonaws.com/templates/gateway-combined.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=DlpodLicenseKey,ParameterValue=<license-key> \
    ParameterKey=LambdaCodeBucket,ParameterValue=$BUCKET \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $REGION
```

All other parameters use defaults. The stack auto-generates a self-signed certificate for the
AI Gateway ALB — omitting `AcmCertificateArn` is intentional. To use a custom ACM certificate,
add: `ParameterKey=AcmCertificateArn,ParameterValue=<arn>`.

Override `GatewayAmiId` and `DlpodAmiId` when deploying outside us-west-1.

### Option B — AWS Console (no CLI required)

**1. Upload the template to S3**

CloudFormation cannot accept a template this large directly — it must be stored in S3 first.
In your S3 bucket from Step 2, click **Create folder**, enter `templates`, click **Create folder**.
Open the `templates/` folder, click **Upload** → **Add files**, and select
`templates/gateway-combined.yaml` from this repository. Click **Upload**.

Your template URL will be:
```
https://netskope-aigw-templates-<account-id>.s3.<region>.amazonaws.com/templates/gateway-combined.yaml
```

**2. Open CloudFormation**

Open the [AWS CloudFormation Console](https://console.aws.amazon.com/cloudformation/), confirm
you are in the correct region (top-right corner), and click **Create stack** →
**With new resources (standard)**.

**3. Specify the template**

Select **Amazon S3 URL** and paste the template URL from above. Click **Next**.

**4. Fill in stack parameters**

Enter a stack name and fill in the required parameters:

| Parameter | Value |
|---|---|
| Stack name | Your chosen stack name (e.g. `aigw-prod`) |
| `NetskopeTenantUrl` | `https://<tenant>.goskope.com` |
| `NetskopeApiToken` | Your RBAC v3 API token |
| `DlpodLicenseKey` | Your DLP On Demand license key |
| `LambdaCodeBucket` | Bucket name from Step 2 |
| `Project` | Lowercase label, e.g. `aigw` |
| `Environment` | `dev`, `staging`, or `prod` |

Leave all other parameters at their defaults. In particular:
- `AcmCertificateArn` — leave **blank** to auto-generate a self-signed certificate
- `GatewayAmiId` / `DlpodAmiId` — override only when deploying outside **us-west-1**

Click **Next**.

**5. Configure stack options**

No changes are required on this page. Click **Next**.

**6. Review and submit**

On the review page, scroll to the bottom and check the box:

> ☑ **I acknowledge that AWS CloudFormation might create IAM resources with custom names.**

Click **Submit**. CloudFormation opens the stack events view — refresh to watch progress.

---

## What to Expect

Stack resource creation takes approximately **8–12 minutes**. After `CREATE_COMPLETE`, appliances
continue bootstrapping in the background — both flows run concurrently:

| Service | Time to load balancer healthy | What's happening |
|---|---|---|
| AI Gateway | 5–15 min from instance launch | Reads bootstrap secret at boot and self-enrolls autonomously |
| DLP On Demand | 15–25 min from instance launch | SSH-based tethering automation via Step Functions |

DLP inspection becomes active once at least one AI Gateway instance and one DLP On Demand instance
are both load balancer healthy — typically 20–30 minutes after `CREATE_COMPLETE`.

> **About the self-signed certificate:** The AI Gateway ALB presents a self-signed certificate
> (`aig.aigw.internal` as CN/SAN). API clients must be configured to trust the cert or skip TLS
> verification. Browsers will show a security warning. This is expected behavior for the default
> deployment. See [DEPLOYMENT.md — ACM Certificate](DEPLOYMENT.md#4-acm-certificate-optional) to
> use a trusted certificate instead.

---

## Verify Deployment

Run these checks after `CREATE_COMPLETE`:

**1. Stack outputs (get the ALB DNS name and other values):**
```bash
aws cloudformation describe-stacks --stack-name <stack-name> \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table --region <region>
```

**2. AI Gateway instances in service:**
```bash
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack-name>-aig-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region <region>
```
Look for `InService` / `Healthy`.

**3. DLP On Demand tethering:**
```bash
aws stepfunctions list-executions \
  --state-machine-arn <DlpodTetheringStateMachineArn from outputs> \
  --query "executions[*].[name,status]" --output table --region <region>
```
`SUCCEEDED` = tethered. `RUNNING` = still tethering (normal for up to 25 minutes).

**4. DLP On Demand instances in service:**
```bash
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack-name>-dlpod-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region <region>
```

**5. Quick connectivity test:**
```bash
AIG_ALB=$(aws cloudformation describe-stacks --stack-name <stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='AigAlbDnsName'].OutputValue" \
  --output text --region <region>)

curl -sk -o /dev/null -w "HTTP %{http_code}\n" https://$AIG_ALB/
```
`HTTP 200` or `HTTP 401` (auth required) confirms the AI Gateway is serving requests.

---

## Next Steps

**Point your application at the gateway:**
Create a DNS CNAME (or Route 53 alias) from your application's LLM endpoint to the
`AigAlbDnsName` stack output.

**Configure AI Gateway policy in the Netskope portal:**
After the gateway enrolls, it appears in your Netskope tenant under
**Settings → Security Cloud Platform → AI Gateway**. Configure DLP profiles, access policies,
and rate limits from there.

**Test a request:**
Send a test prompt through the gateway to verify DLP inspection is active:
```bash
curl -sk https://$AIG_ALB/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-app-token>" \
  -d '{"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}'
```

**Set up monitoring:**
See [OPERATIONS.md — Monitoring and Alerts](OPERATIONS.md#monitoring-and-alerts) for recommended
CloudWatch alarms and log group references.

**If something looks wrong:**
See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — the Diagnostic Commands section runs in under
2 minutes and pinpoints most issues.
