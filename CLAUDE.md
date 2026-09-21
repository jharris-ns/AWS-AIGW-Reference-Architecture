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

## Template

This repository contains a single CloudFormation template: `templates/gateway-combined.yaml`.

It deploys AIG + DLP On Demand (+ optional AI Guardrails) together in one stack,
automatically wiring the DLP certificate and endpoint into the AIG bootstrap configuration
before any instances launch. A new VPC is created — no pre-existing networking required.

> **Standalone templates** for deploying AIG or DLPoD individually are in a separate
> repository: [AWS-POV-Templates-CFT](https://github.com/jharris-ns/AWS-POV-Templates-CFT).

---

## Using Docs to Deploy or Operate

The simplest way to perform any task is to ask Claude to read the relevant document and follow it.

| Task | Instruction to give |
|---|---|
| Deploy the stack | "Read docs/DEPLOYMENT.md and deploy" |
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
- [ ] IAM permissions for CloudFormation, EC2, IAM, ELB, Auto Scaling, Lambda,
  Secrets Manager, SNS, Route 53, ACM, SSM, CloudWatch — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#5-aws-permissions)
- [ ] AI Gateway AMI subscribed in AWS Marketplace (search "Netskope AI Gateway")
- [ ] DLP On Demand AMI subscribed in AWS Marketplace (search "Netskope DLP On Demand") — combined and DLPoD templates only
- [ ] Netskope tenant URL (`https://<tenant>.goskope.com`)
- [ ] Netskope RBAC v3 API token with AIG Administrator role
- [ ] DLP On Demand license key — combined and DLPoD templates only
- [ ] *(Optional AI Guardrails, combined template only)* `aisecurity-llm.tgz` image tarball uploaded to an S3 bucket in the
  target region, a Deep Learning Base GPU AMI ID for the region, and EC2 quota for
  "Running On-Demand G and VT instances" ≥ 4 vCPU — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#ai-guardrails-optional)

> **Region:** AMI defaults are for **us-west-1 only**. For other regions, look up the AMI IDs
> after subscribing and pass them as `GatewayAmiId` / `DlpodAmiId` parameters.

---

## Deployment — Combined Template (Quick Reference)

Full instructions: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)

```bash
# Step 1 — Create S3 bucket and upload template (required — exceeds 51 KB limit)
scripts/deploy-artifacts.sh <region>
# Creates bucket netskope-aigw-templates-<account-id>
# Override bucket: LAMBDA_BUCKET=<name> scripts/deploy-artifacts.sh <region>
BUCKET=netskope-aigw-templates-<account-id>
REGION=<region>
aws s3 cp templates/gateway-combined.yaml \
  s3://$BUCKET/templates/gateway-combined.yaml --region $REGION

# Step 2 — Deploy
aws cloudformation create-stack \
  --stack-name <stack-name> \
  --template-url https://$BUCKET.s3.$REGION.amazonaws.com/templates/gateway-combined.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=DlpodLicenseKey,ParameterValue=<license-key> \
  --tags Key=Project,Value=aigw Key=Environment,Value=prod Key=ManagedBy,Value=CloudFormation \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $REGION
# Optional AI Guardrails tier — add:
#   ParameterKey=GuardrailsImageS3Bucket,ParameterValue=<bucket-holding-aisecurity-llm.tgz> \
#   ParameterKey=GuardrailsAmiId,ParameterValue=<deep-learning-base-gpu-ami-id> \
```

Stack creation takes **12–18 minutes**. After `CREATE_COMPLETE`, both services are enrolled and
serving. Check progress: `aws cloudformation describe-stacks --stack-name <name> --query 'Stacks[0].StackStatus' --output text`

---

## Operations Quick Reference

Full reference: [docs/OPERATIONS.md](docs/OPERATIONS.md)

| Task | Command |
|---|---|
| Check AIG instance states | `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <stack>-aig-asg --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" --output table` |
| Check AIG enrollment | `aws logs tail /aws/lambda/<stack>-aig-activation --since 30m` (look for `Registered appliance`) and `aws elbv2 describe-target-health` on `<stack>-aig-tg` |
| Scale AIG | `aws autoscaling update-auto-scaling-group --auto-scaling-group-name <stack>-aig-asg --desired-capacity <N>` |
| View AIG activation logs | `aws logs tail /aws/lambda/<stack>-aig-activation --since 30m` |
| Check DLPoD instance states | `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <stack>-dlpod-asg --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" --output table` |
| Check DLPoD bootstrap logs | `aws logs tail /aws/lambda/<stack>-dlpod-bootstrap-builder --since 30m` |
| Check readiness gate (stack create) | `aws logs tail /aws/lambda/<stack>-dlpod-readiness --since 30m` |
| Scale DLPoD | `aws autoscaling update-auto-scaling-group --auto-scaling-group-name <stack>-dlpod-asg --desired-capacity <N>` |
| Check DLPoD target health | `aws elbv2 describe-target-health --target-group-arn $(aws elbv2 describe-target-groups --query "TargetGroups[?contains(TargetGroupName,'<stack>-dlpod')].TargetGroupArn" --output text) --output table` |
| Check Guardrails target health *(if deployed)* | `aws elbv2 describe-target-health --target-group-arn $(aws elbv2 describe-target-groups --query "TargetGroups[?contains(TargetGroupName,'<stack>-guardrails')].TargetGroupArn" --output text) --output table` |
| Guardrails container logs *(if deployed)* | `aws ssm start-session --target <instance-id>` then `sudo docker logs guardrails` / `cat /var/log/user-data.log` |
| Scale Guardrails *(if deployed)* | `aws autoscaling update-auto-scaling-group --auto-scaling-group-name <stack>-guardrails-asg --desired-capacity <N>` |
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

         [optional] AI Gateway ASG → Guardrails ALB (HTTP:8080, internal, guardrails.aigw.internal)
                                        ↓
                                  Guardrails GPU ASG (aisecurityllm container, private subnets)
```

**AI Guardrails (optional):** set `GuardrailsImageS3Bucket` (S3 bucket holding `aisecurity-llm.tgz`) and `GuardrailsAmiId` (Deep Learning Base GPU AMI).
The activation Lambda then adds `ai_guardrails.host` to the AIG bootstrap secret and AIG launch waits for the
Guardrails ALB targets to be healthy. Leave `GuardrailsImageS3Bucket` empty to skip the tier.

**Lifecycle automation (AIG):** ASG launch hook → SNS → Activation Lambda → registers appliance
with Netskope API → writes enrollment token to Secrets Manager → `CompleteLifecycleAction` → InService.
AIG reads the bootstrap secret at boot and self-enrolls, including DLPoD TLS config.

**DLPoD bootstrap:** `nsbootstrap.service` reads `bootstrap.json` from EC2 UserData at first boot
and applies TLS certs, license key, DNS, and persona — no SSH, no Step Functions, no lifecycle hook.

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
| [CLAUDE_DEV.md](CLAUDE_DEV.md) | Template conventions, resource inventory, development rules |

---

## Rules

- **The template (~71 KB) must be deployed via S3 `--template-url`** — it exceeds the
  51 KB direct-upload limit. Run `scripts/deploy-artifacts.sh <region>` to create the S3 bucket.
- **All Lambda functions use inline `ZipFile` code** — no S3 Lambda artifacts are required.
- **AMI defaults are us-west-1 only** — override `GatewayAmiId` and `DlpodAmiId` for other regions.
