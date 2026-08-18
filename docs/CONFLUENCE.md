# Netskope AI Gateway — AWS CloudFormation Reference Architecture

**Document audience:** Engineering, InfoSec, Cloud Architecture, Compliance  
**Template:** `templates/gateway-combined.yaml`  
**Services covered:** Netskope AI Gateway (AIG) + DLP On Demand (DLPoD)

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Security Considerations](#2-security-considerations)
3. [Network Architecture](#3-network-architecture)
4. [Automation Flow](#4-automation-flow)
5. [Troubleshooting](#5-troubleshooting)

---

## 1. Introduction

### What This Template Does

This CloudFormation reference architecture deploys Netskope AI Gateway and DLP On Demand together in a single stack. A new VPC is created automatically — no pre-existing networking is required. Both services are configured, enrolled, and wired together before any instances enter service. There are no manual steps after `create-stack`.

The AI Gateway sits inline between applications and LLM providers (AWS Bedrock, OpenAI, etc.), inspecting every prompt and response before it crosses the network boundary. Applications point at the gateway's HTTPS endpoint and require no code changes — the gateway presents an OpenAI-compatible API regardless of the upstream model.

### Controls Applied to Every Request and Response

| Control | What It Enforces |
|---|---|
| **Data Loss Prevention** | Detects and blocks sensitive data in prompts and responses — PII, credentials, regulated content — using Netskope DLP policies. Content is scanned locally inside your VPC; no data leaves your AWS account for DLP processing. |
| **Prompt Injection Detection** | Identifies attempts to override system instructions or exfiltrate data through the model. Runs inline on the AI Gateway using built-in rules. |
| **Access Control** | Enforces which applications and users can reach which models, based on Netskope policy. Requests that fail policy are rejected at the gateway before reaching the LLM provider. |
| **Rate Limiting** | Caps request volume per application or user to control cost and prevent abuse. |
| **Audit Logging** | Records all requests and responses — including blocked ones — to Netskope's management plane for visibility and compliance review. |

### Template Options

Three CloudFormation templates are available. The combined template is recommended for most deployments.

| Template | File | Use when |
|---|---|---|
| **Combined (recommended)** | `templates/gateway-combined.yaml` | Deploying AI Gateway + DLP On Demand together — single stack, automatic wiring between services |
| **AI Gateway only** | `aig/template/gateway-aig.yaml` | Deploying AIG without DLP On Demand, or adding AIG to an existing VPC |
| **DLP On Demand only** | `dlpod/template/gateway-dlpod.yaml` | Deploying DLPoD as a standalone service, or before deploying AIG separately |

### AWS Services Used

| Service | Purpose |
|---|---|
| **EC2** | AI Gateway and DLP On Demand appliance instances |
| **Auto Scaling** | Instance lifecycle management with launch hooks for automated enrollment |
| **Elastic Load Balancing** | Internet-facing ALB (AI Gateway) and internal ALB (DLP On Demand) |
| **VPC** | Isolated network: public subnets (ALBs, NAT Gateway), private subnets (instances) |
| **Lambda** | AI Gateway activation/deregistration; DLP On Demand tethering; certificate generation |
| **Step Functions** | Orchestrates DLP On Demand SSH-based tethering automation |
| **SNS** | Delivers Auto Scaling lifecycle events to Lambda functions |
| **Secrets Manager** | AI Gateway bootstrap secret, Netskope API credentials, DLP On Demand license key |
| **Systems Manager Parameter Store** | DLP On Demand ALB certificate PEM, AI Gateway appliance IDs |
| **ACM** | TLS certificates — auto-generated for both ALBs, or user-provided for the AI Gateway ALB |
| **Route 53** | Private hosted zone (`aigw.internal`) for DLP On Demand internal DNS |
| **CloudWatch Logs** | Lambda and instance log groups |
| **IAM** | Nine least-privilege roles — one per functional component |

### Prerequisites

Before deploying, the following must be in place:

- AWS CLI configured with credentials (`aws sts get-caller-identity` returns your account)
- IAM permissions for CloudFormation, EC2, IAM, ELB, Auto Scaling, Lambda, Step Functions, Secrets Manager, SNS, Route 53, ACM, SSM, CloudWatch
- **AI Gateway AMI** subscribed in AWS Marketplace — search "Netskope AI Gateway"
- **DLP On Demand AMI** subscribed in AWS Marketplace — search "Netskope DLP On Demand"
- Netskope tenant URL (`https://<tenant>.goskope.com`)
- Netskope RBAC v3 API token with AIG Administrator role
- DLP On Demand license key

> **Region note:** Default AMI IDs are for **us-west-1 only**. For other regions, obtain AMI IDs from Netskope support and pass them as `GatewayAmiId` / `DlpodAmiId` parameters.

---

## 2. Security Considerations

This deployment is aligned with the [AWS Well-Architected Framework Security Pillar](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/welcome.html). The design decisions below map directly to Well-Architected best practices.

### 2.1 Identity and Access Management

**Design principle: Least privilege IAM** — [AWS Well-Architected SEC03-BP01](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_permissions_define.html)

Nine dedicated IAM roles enforce least privilege. No role has more access than its specific function requires. No `Action: "*"` or `Resource: "*"` is granted on sensitive services.

| Role | Assumed by | Purpose |
|---|---|---|
| `<stack>-aig-role` | EC2 (AIG instances) | Read bootstrap secret at boot only. No access to API credentials. |
| `<stack>-aig-activation-role` | Lambda | Register/deregister AIG appliance; write enrollment token; read DLPoD cert; complete lifecycle hook |
| `<stack>-aig-lifecycle-sns-role` | Auto Scaling | Publish AIG lifecycle events to SNS |
| `<stack>-dlpod-role` | EC2 (DLPoD instances) | CloudWatch Agent logging only. No secrets access. |
| `<stack>-dlpod-activation-role` | Lambda | Describe EC2 instance; start tethering Step Functions; complete termination hook |
| `<stack>-dlpod-sfn-role` | Step Functions | Invoke DLPoD tethering Lambda |
| `<stack>-dlpod-lambda-role` | Lambda (VPC-attached) | SSH to DLPoD; read license key from Secrets Manager; complete launch hook |
| `<stack>-dlpod-lifecycle-sns-role` | Auto Scaling | Publish DLPoD lifecycle events to SNS |
| `<stack>-cert-generator-role` | Lambda | Generate self-signed cert; import to ACM; write PEM to SSM |

**Key principle:** AIG instances never hold Netskope API credentials. The `<stack>-aig-role` grants access only to the bootstrap secret — not to the API credentials secret. The Activation Lambda reads the API token, calls the Netskope API, and writes only the enrollment token to the bootstrap secret. Compromise of an AIG instance does not expose the Netskope API token.

> **AWS Best Practice:** For production deployments, scope the deployer IAM policy's `IAM` statement to your stack name prefix (e.g. `arn:aws:iam::*:role/aigw-*`) to prevent the deployer from creating roles outside the stack's scope — this closes a privilege escalation path.

### 2.2 Credential and Secret Management

**Design principle: Secrets never touch instances directly** — [AWS Well-Architected SEC08-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_protect_data_rest.html)

| Secret | Service | Contents | Who writes | Who reads |
|---|---|---|---|---|
| `<stack>-api-credentials` | Secrets Manager | Netskope API token + tenant URL | CloudFormation (from `NoEcho` parameter) | AIG Activation Lambda only |
| `<stack>-aig-bootstrap` | Secrets Manager | Enrollment token, DLP cert PEM, DLP host | Cert generator + Activation Lambda | AIG instances at boot |
| `<stack>-dlpod-credentials` | Secrets Manager | DLP On Demand license key | CloudFormation (from `NoEcho` parameter) | DLPoD tethering Lambda only |
| `/<stack>/dlpod-cert` | SSM SecureString | DLPoD ALB self-signed cert PEM | Cert generator Lambda | AIG Activation Lambda |
| `/<stack>/appliances/<id>` | SSM Parameter | AIG appliance ID in Netskope tenant | AIG Activation Lambda at launch | AIG Activation Lambda at termination |

**Credential flow — API token to enrollment:**

```
User → NoEcho CloudFormation parameter
     → <stack>-api-credentials (Secrets Manager, AES-256 encrypted)
     → AIG Activation Lambda (reads at invocation, in memory only)
     → Netskope REST API → enrollment token (exists in Lambda memory only)
     → <stack>-aig-bootstrap (written by Lambda)
     → AIG instance reads at boot → self-enrolls
```

The API token and enrollment token are in separate secrets with separate access controls. Compromise of the bootstrap secret does not expose the API token.

**What is never done:**
- Secrets are never placed in EC2 user data
- Secrets are never in Lambda environment variables
- `NetskopeApiToken` and `DlpodLicenseKey` are `NoEcho: true` — never shown in CloudFormation events, outputs, or the console

### 2.3 Network Security

**Design principle: No public IP on compute** — [AWS Well-Architected SEC05-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_network_protection_create_layers.html)

All EC2 instances run in private subnets. There is no SSH inbound path from the internet to any instance. The only inbound internet path is through the internet-facing AI Gateway ALB.

**Security Group Summary:**

| Security Group | Inbound | Outbound |
|---|---|---|
| AIG ALB SG | TCP 443 from `0.0.0.0/0` | TCP 443 to AIG instance SG |
| AIG Instance SG | TCP 443 from AIG ALB SG only | All (NAT Gateway → internet; DLPoD ALB SG) |
| DLPoD ALB SG | TCP 443 from AIG instance SG only | TCP 443 to DLPoD instance SG |
| DLPoD Instance SG | TCP 443 from DLPoD ALB SG; TCP 22 from DLPoD Lambda SG | All (NAT Gateway for tethering callhome) |
| DLPoD Lambda SG | — (Lambda, no inbound) | TCP 22 + 443 to DLPoD instance SG |

### 2.4 Encryption

**In transit:**

| Path | Protocol |
|---|---|
| Internet → AIG ALB | TLS 1.2+ (ACM certificate, auto-generated or user-provided) |
| AIG ALB → AIG instances | HTTPS:443 |
| AIG instances → DLPoD ALB | HTTPS:443 (self-signed cert; AIG trusts via bootstrap secret) |
| DLPoD ALB → DLPoD instances | HTTPS:443 |
| Lambda → Secrets Manager / SSM / Netskope API | TLS (AWS SDK) |
| DLPoD Lambda → DLPoD instance | SSH port 22 (tethering automation) |

**At rest:**

| Resource | Encryption |
|---|---|
| Secrets Manager secrets | AES-256, AWS-managed KMS |
| SSM Parameter Store (SecureString) | AES-256, AWS-managed KMS |
| CloudWatch Logs | Encrypted by default (AWS-managed) |
| EBS root volumes | Encrypted if account-level default EBS encryption is enabled |

> **Action required for production:** Enable AWS account-level default EBS encryption (`aws ec2 enable-ebs-encryption-by-default`) before deploying to ensure EBS volumes are encrypted. The template does not explicitly enforce EBS encryption.

### 2.5 Known Limitations and Accepted Risks

| Item | Detail | Mitigation |
|---|---|---|
| AIG ALB self-signed cert (default) | Auto-generated cert has CN `aig.aigw.internal`. API clients must trust the cert or skip TLS verification. | Provide a valid ACM certificate via `AcmCertificateArn` for production deployments with external clients. |
| DLPoD tethering uses password-based SSH | Tethering Lambda connects via SSH with a randomly-generated 24-character password. Password is held in Step Functions execution state during tethering and discarded afterward. | Password is unique per instance, randomly generated at tethering time. Network path is locked to DLPoD Lambda SG → DLPoD instance SG port 22 only. |
| EBS encryption not explicitly enforced | The template does not set `Encrypted: true` on launch template EBS volumes. | Enable account-level default EBS encryption before deploying. |
| Secrets Manager secrets deleted on teardown | Deleting the stack permanently deletes all Secrets Manager secrets. | Back up credential values before teardown if needed. |

### 2.6 Well-Architected Alignment Summary

| Pillar | Design Decision |
|---|---|
| **Security** | Nine dedicated IAM roles, no shared credentials |
| **Security** | API token never reaches EC2 instances — Lambda exchanges it for an enrollment token in memory |
| **Security** | No public IP on any compute instance |
| **Security** | All sensitive parameters are `NoEcho: true` |
| **Security** | Separate Secrets Manager secrets for separate purposes, with separate access controls |
| **Reliability** | Multi-AZ deployment — both ASGs and both ALBs span AZ1 + AZ2 |
| **Reliability** | Auto-replacement on failure — ASG lifecycle hooks handle both launch and termination |
| **Reliability** | Startup ordering enforced — cert generator runs before instances launch; DLP config pre-populated in bootstrap secret |
| **Operational Excellence** | Single CloudFormation template, all resources version-controlled |
| **Operational Excellence** | Full lifecycle automation via SNS → Lambda → Step Functions |
| **Cost Optimization** | Step scaling on CPU threshold; scale in when load drops |

---

## 3. Network Architecture

### 3.1 Architecture Overview

```
                         Internet
                             │
                   ┌─────────▼─────────┐
                   │  AIG ALB (HTTPS)  │  ← Public subnets AZ1 + AZ2
                   │  Internet-facing  │
                   └─────────┬─────────┘
                             │
               ┌─────────────┼─────────────┐
               │             │             │
       ┌───────▼──────┐             ┌──────▼────────┐
       │  AIG Instance│             │  AIG Instance │  ← Private subnets
       │     AZ1      │             │     AZ2       │
       └───────┬──────┘             └──────┬────────┘
               └─────────────┬─────────────┘
                             │ DLP inspection
                   ┌─────────▼─────────┐
                   │  DLPoD ALB        │  ← Private subnets AZ1 + AZ2
                   │  dlp.aigw.internal│    (Route 53 private zone)
                   └─────────┬─────────┘
                             │
               ┌─────────────┼─────────────┐
       ┌───────▼──────┐             ┌──────▼────────┐
       │ DLPoD Instance│            │ DLPoD Instance│
       │     AZ1       │            │     AZ2       │
       └───────────────┘            └───────────────┘

NAT Gateway (public subnet AZ1) → Netskope API, LLM providers
```

### 3.2 VPC and Subnet Design

The template creates a new VPC — no pre-existing networking is required.

| Subnet Tier | Availability Zones | Hosts | Default CIDR |
|---|---|---|---|
| Public | AZ1 + AZ2 | AIG ALB nodes, NAT Gateway (AZ1 only) | `10.0.1.0/24`, `10.0.2.0/24` |
| Private | AZ1 + AZ2 | AIG instances, DLPoD instances, DLPoD internal ALB | `10.0.10.0/24`, `10.0.11.0/24` |

No compute resources run in public subnets. The NAT Gateway provides outbound internet access for private-subnet instances and Lambda functions — Netskope API calls, LLM provider traffic, and DLPoD tethering callhome all exit through the NAT Gateway.

### 3.3 AI Gateway Tier

| Attribute | Value |
|---|---|
| Instance type (default) | `m5.4xlarge` (16 vCPU / 64 GB RAM) |
| AMI | Netskope AI Gateway (AWS Marketplace) |
| Deployment | Auto Scaling Group, private subnets AZ1 + AZ2 |
| ALB | Internet-facing, public subnets, HTTPS port 443 |
| Desired capacity | Configurable (1–4); defaults to 1 |
| Auto-scale trigger | Average CPU ≥ 70% for two consecutive 5-minute periods |
| Enrollment | Reads bootstrap secret from Secrets Manager at boot; self-enrolls autonomously |

### 3.4 DLP On Demand Tier

| Attribute | Value |
|---|---|
| Instance type (default) | `c5a.4xlarge` (16 vCPU / 32 GB RAM) |
| AMI | Netskope DLP On Demand (AWS Marketplace) |
| Deployment | Auto Scaling Group, private subnets AZ1 + AZ2 |
| ALB | Internal (private subnets), HTTPS port 443 |
| DNS | `dlp.aigw.internal` (Route 53 private hosted zone) |
| Desired capacity | Configurable (1–4); defaults to 1 |
| Tethering | SSH-based CLI automation via Step Functions (~15–25 minutes per instance) |

DLP On Demand receives content from the AI Gateway over HTTPS, applies DLP policies entirely inside the VPC, and returns a verdict. Content never leaves your AWS account for DLP processing.

### 3.5 Certificate Management

Both ALBs use TLS certificates managed by the stack. A `CertGeneratorFunction` custom resource runs at stack creation before any instances launch.

| Certificate | ALB | Source | Storage |
|---|---|---|---|
| AIG ALB cert | Internet-facing | Auto-generated self-signed (default), or user-provided ACM ARN | ACM |
| DLPoD ALB cert | Internal | Always auto-generated self-signed (10-year validity) | ACM + SSM `/<stack>/dlpod-cert` |

The cert generator pre-populates the AIG bootstrap secret with the DLP endpoint URL and DLPoD certificate PEM before any AIG instances launch. This ensures the first AIG instance already has everything it needs to forward DLP traffic immediately at enrollment.

### 3.6 High Availability

Both ASGs and both ALBs span AZ1 and AZ2. An AZ failure reduces capacity but does not interrupt service — the healthy AZ continues serving all traffic.

| Scenario | Impact | Recovery |
|---|---|---|
| Single AIG instance failure | Reduced capacity; remaining instances continue | ASG replaces automatically; new instance re-enrolls in 5–15 min |
| Single DLPoD instance failure | Reduced DLP capacity; ALB routes to healthy instances | ASG replaces; new instance re-tethers in 15–25 min |
| AZ failure | Reduced capacity; other AZ continues immediately | No action required |
| NAT Gateway failure | Outbound internet loss; internal DLP traffic unaffected | AWS 99.99% SLA; auto-recovers |

**Recovery time objectives:**

| Scope | RTO |
|---|---|
| Single AIG instance | 5–15 minutes (auto-replaced and re-enrolled) |
| Single DLPoD instance | 15–25 minutes (auto-replaced and re-tethered) |
| AZ failure | 0 seconds (healthy AZ continues immediately) |
| Full stack recreate | 30–45 minutes |

---

## 4. Automation Flow

No manual steps are required after `create-stack`. The following describes what happens automatically.

### 4.1 Stack Creation Sequence

```
1. CloudFormation creates VPC, subnets, NAT Gateway, ALBs, security groups, IAM roles
2. CertGeneratorFunction (custom resource) runs:
   - Generates DLPoD self-signed TLS certificate
   - Imports cert to ACM
   - Writes cert PEM to SSM Parameter Store (/<stack>/dlpod-cert)
   - Pre-populates AIG bootstrap secret with DLP endpoint + cert
3. AIG ASG and DLPoD ASG launch concurrently
4. AIG instances: enrollment flow (see 4.2)
5. DLPoD instances: tethering flow (see 4.3)
6. CloudFormation reaches CREATE_COMPLETE once both ASGs have passed health checks
```

### 4.2 AI Gateway Enrollment Flow (Per Instance)

Triggered at every AIG instance launch — at stack creation and at every scale-out event.

```
1. ASG launches AIG instance
   └─ Lifecycle hook holds instance in Pending:Wait (120-second window)

2. SNS delivers lifecycle event to AIG Activation Lambda

3. AIG Activation Lambda executes:
   a. Reads Netskope API token from <stack>-api-credentials (Secrets Manager)
   b. Calls Netskope REST API → registers appliance → receives enrollment token
   c. Reads DLPoD cert from SSM /<stack>/dlpod-cert
   d. Writes enrollment token + DLP cert + DLP host to <stack>-aig-bootstrap (Secrets Manager)
   e. Writes appliance ID to SSM /<stack>/appliances/<instance-id>
   f. Calls CompleteLifecycleAction: CONTINUE

4. Instance moves to InService
   └─ AIG instance reads <stack>-aig-bootstrap at boot
   └─ Self-enrolls with Netskope tenant autonomously
   └─ Configures DLP forwarding to dlp.aigw.internal

5. ALB health check passes → instance serves traffic
   (typically 5–15 minutes from instance launch)
```

**On termination:**
- Activation Lambda deregisters the appliance from Netskope
- Deletes the SSM parameter `/<stack>/appliances/<instance-id>`
- Calls `CompleteLifecycleAction: CONTINUE`

### 4.3 DLP On Demand Tethering Flow (Per Instance)

Triggered at every DLPoD instance launch. Orchestrated by AWS Step Functions.

```
1. ASG launches DLPoD instance
   └─ Lifecycle hook holds instance in Pending:Wait (1800-second / 30-minute window)

2. SNS delivers lifecycle event to DLPoD Activation Lambda

3. DLPoD Activation Lambda starts a Step Functions execution: tether-<instance-id>

4. Step Functions orchestrates SSH automation (DLPoD Lambda, VPC-attached):

   State                    | Action                                       | Duration
   ─────────────────────────┼──────────────────────────────────────────────┼──────────────
   WaitForDlpodSSH          | Polls SSH port 22 every 25s until open       | 5–8 min
   DlpodChangePassword      | Sets unique 24-char random password via CLI  | <30 sec
   DlpodSetDNS              | Configures VPC DNS resolver (CIDR+2)         | <30 sec
   DlpodSetLicense          | Reads license from Secrets Manager, applies  | <60 sec
   WaitForDlpodTetheringInit| Fixed 120s wait for DLPoD callhome to start  | 2 min
   CheckDlpodTethering      | Polls tethering status every 60s until done  | 5–15 min
   DlpodCompleteLifecycle   | CompleteLifecycleAction: CONTINUE            | <5 sec

5. Instance moves to InService
   └─ DLPoD ALB health check passes
   └─ DLP inspection becomes active (total ~15–25 minutes from instance launch)
```

### 4.4 Runtime Traffic Flow (Per Request)

```
1. Application sends HTTPS request to AIG ALB DNS name

2. AIG ALB (public subnets) → AIG instance (private subnet)

3. AIG instance inspects request:
   - Access control: is this application/user allowed to use this model?
   - Rate limiting: has this application exceeded its quota?
   - Prompt injection detection: is this prompt attempting to override system instructions?

4. AIG → dlp.aigw.internal (Route 53 private zone)
        → DLPoD internal ALB (HTTPS:443)
        → DLPoD instance (least-connections)
   DLPoD scans content against DLP policies → returns allow/block/redact verdict

5. Allowed requests: AIG forwards to upstream LLM provider via NAT Gateway
   Blocked requests: AIG returns error to client; event logged to Netskope

6. LLM response passes back through AIG:
   - DLP inspection on response content
   - Audit log entry written to Netskope management plane
   - Response returned to client
```

### 4.5 Auto Scale-Out Flow

```
1. CloudWatch alarm fires: AIG ASG average CPU ≥ 70% for two consecutive 5-minute periods

2. Step scaling policy: +1 AIG instance

3. New instance enters enrollment flow (same as section 4.2)

4. New instance: InService → ALB registers → serving requests
   (5–15 minutes from scale-out trigger to traffic-ready)
```

---

## 5. Troubleshooting

### 5.1 Diagnostic Commands

Run these first to understand current state before diagnosing a specific issue.

```bash
STACK=<stack-name>
REGION=<region>

# Stack status
aws cloudformation describe-stacks --stack-name $STACK \
  --query 'Stacks[0].StackStatus' --output text --region $REGION

# All stack outputs
aws cloudformation describe-stacks --stack-name $STACK \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table --region $REGION

# AIG instance states
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names $STACK-aig-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region $REGION

# DLPoD instance states
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names $STACK-dlpod-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region $REGION

# DLPoD tethering Step Functions executions
DLPOD_SFN=$(aws cloudformation describe-stacks --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='DlpodTetheringStateMachineArn'].OutputValue" \
  --output text --region $REGION)
aws stepfunctions list-executions --state-machine-arn $DLPOD_SFN \
  --query "executions[*].[name,status,startDate]" --output table --region $REGION

# AIG bootstrap secret (check DLP block is present)
aws secretsmanager get-secret-value \
  --secret-id $STACK-aig-bootstrap \
  --query SecretString --output text --region $REGION

# AIG activation Lambda logs (last 15 min)
aws logs tail /aws/lambda/$STACK-aig-activation --since 15m --region $REGION

# DLPoD tethering Lambda logs (last 30 min)
aws logs tail /aws/lambda/$STACK-dlpod --since 30m --region $REGION
```

### 5.2 AI Gateway Issues

#### AIG instance stuck in `Pending:Wait`

The AIG lifecycle hook heartbeat is 120 seconds. The Activation Lambda must complete within that window or the instance is ABANDONED.

```bash
aws logs tail /aws/lambda/$STACK-aig-activation --since 10m --region $REGION
```

| Log Pattern | Cause | Solution |
|---|---|---|
| `401 Unauthorized` / `403 Forbidden` | API token is wrong, expired, or lacks AIG Administrator permissions | Verify token in Netskope portal: **Settings → Administration → Administrators & Roles** |
| `ConnectionError` / `timeout` | Lambda cannot reach Netskope API | Check NAT Gateway is `available`; check private subnet route table has route to NAT GW |
| `Parameter /<stack>/dlpod-cert not found` | Cert generator custom resource failed at stack creation | See Certificate Issues below |
| `ResourceNotFoundException` on bootstrap secret | Bootstrap secret not created | Check CloudFormation events for failure on `AigBootstrapSecret` resource |

If the Lambda fails, the instance is ABANDONED within 2 minutes and a replacement launches automatically. Resolve the root cause before the replacement arrives.

#### AIG ALB target stuck unhealthy

```bash
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'$STACK-aig-tg')].TargetGroupArn" \
  --output text --region $REGION)
aws elbv2 describe-target-health --target-group-arn $TG_ARN --output table --region $REGION
```

| Health State | Likely Cause | Solution |
|---|---|---|
| `initial` | Enrollment not yet complete | Wait 5–15 minutes from instance launch |
| `unhealthy` — connection refused | AIG service not running or enrollment failed | Check AIG activation Lambda logs |
| `unhealthy` — timeout | Security group misconfiguration | Check AIG ALB SG allows outbound to AIG instance SG port 443 |

#### DLP inspection not working after AIG enrolls

1. Check the AIG bootstrap secret contains a `dlp` block:
```bash
aws secretsmanager get-secret-value --secret-id $STACK-aig-bootstrap \
  --query SecretString --output text --region $REGION | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('dlp',{}), indent=2))"
```
The output should contain `certificate` and `host` keys. If missing, the cert generator failed — see Certificate Issues.

2. Check DLPoD ALB has healthy targets:
```bash
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'$STACK-dlpod-tg')].TargetGroupArn" \
  --output text --region $REGION)
aws elbv2 describe-target-health --target-group-arn $TG_ARN --output table --region $REGION
```

### 5.3 DLP On Demand Issues

#### DLPoD instance stuck in `Pending:Wait`

The DLPoD lifecycle hook heartbeat is 30 minutes. Tethering automation runs via Step Functions.

```bash
# Check if a Step Functions execution started for this instance
aws stepfunctions list-executions --state-machine-arn $DLPOD_SFN \
  --query "executions[*].[name,status,startDate]" --output table --region $REGION

# Check DLPoD activation Lambda logs
aws logs tail /aws/lambda/$STACK-dlpod-activation --since 30m --region $REGION
```

If no execution exists, the Activation Lambda failed to start Step Functions — check activation Lambda logs for a permissions issue or missing state machine ARN.

#### Step Functions execution FAILED

```bash
EXEC_ARN=$(aws stepfunctions list-executions --state-machine-arn $DLPOD_SFN \
  --query "executions[?status=='FAILED'].executionArn | [0]" --output text --region $REGION)

aws stepfunctions get-execution-history --execution-arn $EXEC_ARN \
  --query "events[?type=='TaskFailed'].[taskFailedEventDetails.error,taskFailedEventDetails.cause]" \
  --output table --region $REGION

aws logs tail /aws/lambda/$STACK-dlpod --since 60m --region $REGION
```

| Failed State | Likely Cause | Solution |
|---|---|---|
| `WaitForDlpodSSH` | Instance not yet accepting SSH, or Lambda SG cannot reach instance port 22 | Check DLPoD Lambda SG allows outbound to DLPoD instance SG port 22 |
| `DlpodChangePassword` | SSH connected but CLI automation failed | Check Lambda logs for `pexpect` timeout or unexpected CLI output |
| `DlpodSetDNS` | DNS configuration step failed | `DnsServer` parameter must be VPC CIDR base + 2 (e.g. `10.0.0.2` for `10.0.0.0/16`) |
| `DlpodSetLicense` | License key invalid or Secrets Manager access failed | Verify license key in Netskope portal: **Settings → Security Cloud Platform → On-Premises Infrastructure** |
| `CheckDlpodTethering` | DLPoD cannot reach Netskope management plane | Check NAT Gateway; verify DLPoD instance SG allows all outbound |
| `DlpodCompleteLifecycle` | `CompleteLifecycleAction` failed | Check `<stack>-dlpod-lambda-role` has `autoscaling:CompleteLifecycleAction` |

### 5.4 Certificate Issues

#### DLPoD cert missing from SSM (`/<stack>/dlpod-cert`)

```bash
# Check CloudFormation events for cert generator resource
aws cloudformation describe-stack-events --stack-name $STACK --region $REGION \
  --query "StackEvents[?LogicalResourceId=='DlpodAlbCertificate'].[ResourceStatus,ResourceStatusReason]" \
  --output table

# Check cert generator Lambda logs
aws logs tail /aws/lambda/$STACK-cert-generator --since 60m --region $REGION
```

| Log Pattern | Cause | Solution |
|---|---|---|
| `AccessDenied` on `acm:ImportCertificate` | Cert generator Lambda role missing ACM permissions | Check `<stack>-cert-generator-role` policy |
| `AccessDenied` on `ssm:PutParameter` | Cert generator Lambda role missing SSM permissions | Check `<stack>-cert-generator-role` policy |

**Impact:** If the cert generator fails, the DLP block is not written to the AIG bootstrap secret. AIG instances that boot without the DLP block enroll without DLP forwarding configured. Fix the cert generator issue and trigger a stack update to re-run the custom resource.

### 5.5 Stack Issues

#### Stack stuck at `CREATE_IN_PROGRESS` for more than 30 minutes

```bash
# Find the stuck resource
aws cloudformation describe-stack-events --stack-name $STACK --region $REGION \
  --query "StackEvents[?ResourceStatus=='CREATE_IN_PROGRESS'].[LogicalResourceId,ResourceType,Timestamp]" \
  --output table
```

| Stuck Resource | Cause | Solution |
|---|---|---|
| `DlpodAlbCertificate` / `AigAlbCertificate` | Cert generator Lambda failed without sending a response to CloudFormation | Check cert generator Lambda logs; CloudFormation waits up to 1 hour then rolls back |
| `GatewayAutoScalingGroup` | ASG waiting for AIG lifecycle hook to complete | Check AIG activation Lambda logs |
| `DlpodAutoScalingGroup` | ASG waiting for DLPoD lifecycle hook to complete | Check DLPoD activation Lambda logs and Step Functions execution |

#### Stack deletion hangs

```bash
# Check for instances stuck in Terminating:Wait
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names $STACK-aig-asg $STACK-dlpod-asg \
  --query "AutoScalingGroups[*].Instances[?LifecycleState=='Terminating:Wait'].[InstanceId,LifecycleState]" \
  --output table --region $REGION
```

If instances are stuck, force-complete the lifecycle action:
```bash
aws autoscaling complete-lifecycle-action \
  --lifecycle-hook-name <stack>-aig-asg-launch-hook \
  --auto-scaling-group-name <stack>-aig-asg \
  --lifecycle-action-result CONTINUE \
  --instance-id <instance-id> \
  --region $REGION
```

### 5.6 Log Reference

#### Successful AIG enrollment (`/aws/lambda/<stack>-aig-activation`)
```
[INFO] Lifecycle event received: instance-id=i-abc123 transition=autoscaling:EC2_INSTANCE_LAUNCHING
[INFO] Reading API credentials from Secrets Manager
[INFO] Calling Netskope API to register appliance
[INFO] Appliance registered: appliance_id=xxxxxxxx
[INFO] Writing enrollment token to bootstrap secret
[INFO] Completing lifecycle action: CONTINUE
```

#### Successful DLPoD tethering (`/aws/lambda/<stack>-dlpod`)
```
[INFO] Connecting to DLPoD instance at 10.0.10.x:22
[INFO] SSH connected
[INFO] Password changed successfully
[INFO] DNS configured
[INFO] License applied
[INFO] Waiting for tethering to initialize (120s)
[INFO] Tethering complete
[INFO] Completing lifecycle action: CONTINUE
```

#### Common failure patterns
```
# AIG Activation Lambda
[ERROR] Netskope API returned 401 Unauthorized
[ERROR] SSM parameter /<stack>/dlpod-cert not found
[ERROR] Timeout waiting for Netskope API response

# DLPoD Tethering Lambda
[ERROR] SSH connection refused (instance may not be ready yet)
[ERROR] pexpect timeout waiting for CLI prompt
[ERROR] License key rejected: Invalid license
[ERROR] Tethering check failed after 10 attempts
```

---

*For deployment instructions, see [DEPLOYMENT.md](DEPLOYMENT.md). For day-to-day operations, see [OPERATIONS.md](OPERATIONS.md).*
