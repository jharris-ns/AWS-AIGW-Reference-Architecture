# CLAUDE.md

Project instructions for Claude Code — deployment and operations.

For template development and modification guidelines, see [CLAUDE_DEV.md](CLAUDE_DEV.md).

---

## What This Project Does

This is an **AWS CloudFormation reference architecture for Netskope AI Gateway and DLP On Demand**.
It provisions, enrolls, and operates both services automatically — no manual steps after deployment.

The AI Gateway sits inline between applications and LLM providers (Bedrock, OpenAI, etc.),
enforcing DLP, prompt injection detection, access control, rate limiting, and audit logging.
DLP On Demand runs the content inspection locally inside the VPC so no data leaves the AWS account.

---

## Template Options

Three CloudFormation templates are available. Use the one that matches the deployment goal.

| Template | File | Use when |
|---|---|---|
| **Combined** | `templates/gateway-combined.yaml` | Deploying AIG + DLP On Demand together (recommended) |
| **AIG only** | `aig/template/gateway-aig.yaml` | AIG without DLP On Demand, or adding AIG to an existing VPC |
| **DLPoD only** | `dlpod/template/gateway-dlpod.yaml` | DLP On Demand standalone, or before deploying AIG separately |

Each template creates its own VPC — no pre-existing networking is required.
The combined template automatically wires the DLP certificate and endpoint into the AIG bootstrap
configuration before any instances launch. The individual templates are for cases where only one
service is needed, or where they are being added to an existing environment.

---

## Using Docs to Deploy or Operate

The simplest way to perform any task is to ask Claude to read the relevant document and follow it.

| Task | Instruction to give |
|---|---|
| Deploy AIG + DLPoD together | "Read docs/DEPLOYMENT.md and deploy the combined stack" |
| Deploy AIG only | "Read aig/docs/DEPLOYMENT.md and deploy" |
| Deploy DLPoD only | "Read dlpod/docs/DEPLOYMENT.md and deploy" |
| Quick deploy (minimal guidance) | "Read docs/QUICKSTART.md and deploy" |
| Operate a running stack | "Read docs/OPERATIONS.md" |
| Troubleshoot a failing stack | "Read docs/TROUBLESHOOTING.md" |
| Understand the architecture | "Read docs/ARCHITECTURE.md" |
| Review security posture | "Read docs/SECURITY.md" |

Providing credentials as environment variables avoids them appearing in the conversation:

```bash
export NETSKOPE_API_KEY=<token>
export DLPOD_LICENSE_KEY=<license-key>
```

Then reference them by name: "use $NETSKOPE_API_KEY for the API token".

---

## Prerequisites

Before deploying, confirm:

- [ ] AWS CLI configured (`aws sts get-caller-identity` returns your account)
- [ ] IAM permissions for CloudFormation, EC2, IAM, ELB, Auto Scaling, Lambda, Step Functions,
  Secrets Manager, SNS, Route 53, ACM, SSM, CloudWatch — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#6-aws-permissions)
- [ ] AI Gateway AMI subscribed in AWS Marketplace (search "Netskope AI Gateway")
- [ ] DLP On Demand AMI subscribed in AWS Marketplace (search "Netskope DLP On Demand") — combined and DLPoD templates only
- [ ] Netskope tenant URL (`https://<tenant>.goskope.com`)
- [ ] Netskope RBAC v3 API token with AIG Administrator role
- [ ] DLP On Demand license key — combined and DLPoD templates only

> **Region:** AMI defaults are for **us-west-1 only**. For other regions, look up the AMI IDs
> after subscribing and pass them as `GatewayAmiId` / `DlpodAmiId` parameters.

---

## Deployment — Combined Template (Quick Reference)

Full instructions: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)

```bash
# Step 1 — Upload Lambda artifacts to S3
scripts/deploy-artifacts.sh <region>
# Creates bucket netskope-aigw-templates-<account-id> and uploads 4 artifacts from dist/
# Override bucket: LAMBDA_BUCKET=<name> scripts/deploy-artifacts.sh <region>

# Step 2 — Upload template to S3 (required — exceeds 51 KB limit)
BUCKET=netskope-aigw-templates-<account-id>
REGION=<region>
aws s3 cp templates/gateway-combined.yaml \
  s3://$BUCKET/templates/gateway-combined.yaml --region $REGION

# Step 3 — Deploy
aws cloudformation create-stack \
  --stack-name <stack-name> \
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

Stack creation takes **12–18 minutes**. After `CREATE_COMPLETE`, both services are enrolled and
serving. Check progress: `aws cloudformation describe-stacks --stack-name <name> --query 'Stacks[0].StackStatus' --output text`

---

## Deployment — Individual Templates

**AIG only** — full instructions: [aig/docs/DEPLOYMENT.md](aig/docs/DEPLOYMENT.md)

The AIG template uses an inline Lambda (no S3 artifact upload required). The template is ~34 KB
and can be uploaded directly in the CloudFormation console.

```bash
BUCKET=netskope-aigw-templates-<account-id>
REGION=<region>
aws s3 cp aig/template/gateway-aig.yaml \
  s3://$BUCKET/templates/gateway-aig.yaml --region $REGION

aws cloudformation create-stack \
  --stack-name <stack-name> \
  --template-url https://$BUCKET.s3.$REGION.amazonaws.com/templates/gateway-aig.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=AcmCertificateArn,ParameterValue=<acm-arn> \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $REGION
```

**DLPoD only** — full instructions: [dlpod/docs/DEPLOYMENT.md](dlpod/docs/DEPLOYMENT.md)

```bash
# Upload Lambda artifacts
dlpod/scripts/deploy-artifacts.sh <region>

# Upload template
aws s3 cp dlpod/template/gateway-dlpod.yaml \
  s3://$BUCKET/templates/gateway-dlpod.yaml --region $REGION

aws cloudformation create-stack \
  --stack-name <stack-name> \
  --template-url https://$BUCKET.s3.$REGION.amazonaws.com/templates/gateway-dlpod.yaml \
  --parameters \
    ParameterKey=DlpodLicenseKey,ParameterValue=<license-key> \
    ParameterKey=LambdaCodeBucket,ParameterValue=$BUCKET \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $REGION
```

---

## Operations Quick Reference

Full reference: [docs/OPERATIONS.md](docs/OPERATIONS.md)

| Task | Command |
|---|---|
| Check AIG instance states | `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <stack>-aig-asg --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" --output table` |
| Check AIG enrollment | `aws stepfunctions list-executions --state-machine-arn arn:aws:states:<region>:<account>:stateMachine:<stack>-aig-enrollment --output table` |
| Scale AIG | `aws autoscaling update-auto-scaling-group --auto-scaling-group-name <stack>-aig-asg --desired-capacity <N>` |
| View AIG activation logs | `aws logs tail /aws/lambda/<stack>-aig-activation --since 30m` |
| Check DLPoD instance states | `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <stack>-dlpod-asg --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" --output table` |
| Check DLPoD tethering | `aws stepfunctions list-executions --state-machine-arn arn:aws:states:<region>:<account>:stateMachine:<stack>-dlpod-tethering --output table` |
| Scale DLPoD | `aws autoscaling update-auto-scaling-group --auto-scaling-group-name <stack>-dlpod-asg --desired-capacity <N>` |
| View DLPoD tethering logs | `aws logs tail /aws/lambda/<stack>-dlpod --since 30m` |
| Get stack outputs | `aws cloudformation describe-stacks --stack-name <stack> --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" --output table` |
| Delete stack | `aws cloudformation delete-stack --stack-name <stack> --region <region>` |

---

## Architecture Summary

```
Internet → AIG ALB (HTTPS:443, internet-facing)
               ↓
         AI Gateway ASG (private subnets)
               ↓ inline DLP inspection
         DLPoD ALB (HTTPS:443, internal, dlp.aigw.internal)
               ↓
         DLP On Demand ASG (private subnets)
```

**Lifecycle automation (AIG):** ASG launch hook → SNS → Activation Lambda → Step Functions →
Enrollment Lambda (SSH/TUI) → enrollment complete → `CompleteLifecycleAction` → InService.

**Lifecycle automation (DLPoD):** ASG launch hook → SNS → Activation Lambda → Step Functions →
Tethering Lambda (SSH/CLI, paramiko) → tethering complete → `CompleteLifecycleAction` → InService.

**Secret handling:** API credentials never reach instances. The Activation Lambda exchanges the
API token for a short-lived enrollment token in memory, writes it to Secrets Manager, and instances
read only the bootstrap secret. Instance IAM roles have no access to the API credentials secret.

---

## Documentation Index

| Document | Contents |
|---|---|
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | Prerequisites checklist, three-step deploy, console alternative |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Full parameter reference, preflight checks, deploy options, verification |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | VPC design, traffic flows, IAM roles, HA, cost estimate |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Scaling, monitoring, log groups, AMI upgrade procedure |
| [docs/SECURITY.md](docs/SECURITY.md) | IAM least privilege, secrets handling, encryption |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Issue/Cause/Solution format, diagnostic commands |
| [aig/docs/DEPLOYMENT.md](aig/docs/DEPLOYMENT.md) | AIG standalone: ACM cert, deploy options, verification |
| [aig/docs/OPERATIONS.md](aig/docs/OPERATIONS.md) | AIG standalone: enrollment flow, scaling, troubleshooting |
| [dlpod/docs/DEPLOYMENT.md](dlpod/docs/DEPLOYMENT.md) | DLPoD standalone: S3 artifacts, deploy options, verification |
| [dlpod/docs/OPERATIONS.md](dlpod/docs/OPERATIONS.md) | DLPoD standalone: tethering flow, scaling, troubleshooting |
| [CLAUDE_DEV.md](CLAUDE_DEV.md) | Template conventions, resource inventory, development rules |

---

## Rules

- **Templates over 51 KB must be deployed via S3 `--template-url`** — the combined and DLPoD
  templates exceed this limit. The AIG template (~34 KB) can be uploaded directly in the console.
- **Lambda artifacts must be in S3 before `create-stack`** — the S3 bucket must be in the same
  region as the stack. Use `scripts/deploy-artifacts.sh` (combined) or
  `dlpod/scripts/deploy-artifacts.sh` (DLPoD standalone).
- **Pre-built artifacts in `dist/` are ready to use** — no Docker or build tools required.
  Set `REBUILD=1` to rebuild from source (Lambda layer requires Docker or Podman for x86_64).
- **AMI defaults are us-west-1 only** — override `GatewayAmiId` and `DlpodAmiId` for other regions.
