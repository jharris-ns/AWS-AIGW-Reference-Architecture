# Operations Guide — AI Gateway + DLP On Demand

Day-2 operations reference for `templates/gateway-combined.yaml`. Written for DevOps engineers
who know AWS well but may be new to Netskope. Covers the Netskope side of both services before
diving into operational procedures.

## Table of Contents

- [What Is the AI Gateway?](#what-is-the-ai-gateway)
- [What Is DLP On Demand?](#what-is-dlp-on-demand)
- [Architecture](#architecture)
- [Startup Sequence](#startup-sequence)
- [Monitoring and Alerts](#monitoring-and-alerts)
- [Key Operational Commands](#key-operational-commands)
- [Scaling](#scaling)
- [AMI Upgrade Procedure](#ami-upgrade-procedure)
- [IAM Roles](#iam-roles)
- [Secrets and SSM Parameters](#secrets-and-ssm-parameters)
- [Troubleshooting](#troubleshooting)

---

## What Is the AI Gateway?

The Netskope AI Gateway is a software appliance that runs as an EC2 instance in your VPC. It
acts as an inline proxy between AI-powered applications and large language model providers
(AWS Bedrock, OpenAI, Anthropic, etc.). Every prompt and response passes through the gateway
before reaching its destination.

**What it does operationally:**
- Presents an OpenAI-compatible HTTPS API to clients — no application code changes required
- Enrolls with the Netskope management plane at boot using a one-time token from Secrets Manager
- Forwards content to DLP On Demand for scanning before passing it to the LLM provider
- Receives security policies, DLP profiles, and access controls from the Netskope management plane
- Records all AI interactions (prompts, responses, blocks) to the Netskope management plane

**How enrollment works:**
When an instance launches, a lifecycle hook holds it in `Pending:Wait`. The Activation Lambda
registers the appliance with the Netskope API, receives an enrollment token, and writes it to
the bootstrap secret in Secrets Manager. The instance reads the bootstrap secret at boot and
self-enrolls. The lifecycle hook completes, the instance enters `InService`, and the ALB
health check passes once the gateway service is up.

---

## What Is DLP On Demand?

DLP On Demand is a content inspection appliance that the AI Gateway forwards content to for
data loss prevention analysis. It runs in your VPC — content never leaves your AWS account for
DLP scanning. It exposes a REST API on HTTPS port 443 and applies Netskope DLP policies to the
content it receives.

**How tethering works:**
DLP On Demand uses a process called "tethering" to connect to the Netskope management plane and
receive its configuration and DLP profiles. Tethering is automated by a Step Functions state
machine that SSHs into the instance, changes the default password, configures DNS, applies the
license key, and waits for the tethering callhome to complete. This takes 15–25 minutes per
instance and runs automatically when the instance launches.

Once tethered, DLP On Demand receives DLP profiles from the management plane and begins
inspecting content forwarded from AI Gateway instances.

---

## Architecture

```
                     VPC (10.0.0.0/16)
                     ┌────────────────────────────────────────────────────────┐
Internet             │  Public subnets (AZ1/AZ2)                              │
    │                │  ┌──────────────────────────────────────────────────┐  │
    └── AIG ALB ─────┤  │ AIG ALB nodes  │  NAT Gateway (AZ1)             │  │
        HTTPS:443    │  └──────────────────────────────────────────────────┘  │
                     │                                                         │
                     │  Private subnets (AZ1/AZ2)                            │
                     │  ┌──────────────────────────────────────────────────┐  │
                     │  │ AIG instances (ASG, min 1 / max 4)               │  │
                     │  │   ↓ DLP inspection                               │  │
                     │  │ DLPoD ALB (internal, dlp.aigw.internal)          │  │
                     │  │   ↓                                               │  │
                     │  │ DLPoD instances (ASG, default 1)                 │  │
                     │  └──────────────────────────────────────────────────┘  │
                     └────────────────────────────────────────────────────────┘

Route 53 private zone: aigw.internal
  dlp.aigw.internal → DLPoD internal ALB

AIG internet-facing ALB:  HTTPS:443 → AIG instances → LLM providers (via NAT GW)
                                            ↕ dlp.aigw.internal → DLPoD instances
```

### Component separation

| Component | Subnet | ALB | DNS |
|---|---|---|---|
| AIG ASG | Private AZ1 + AZ2 | Internet-facing (public subnets) | `AigAlbDnsName` output or custom CNAME |
| DLPoD ASG | Private AZ1 + AZ2 | Internal (private subnets) | `dlp.aigw.internal` (Route 53 private zone) |

---

## Startup Sequence

Both DLPoD tethering and AIG enrollment run concurrently after stack creation. DLPoD takes longer
(15–25 min vs 5–15 min for AIG), so there is a window where AIG is enrolled but DLPoD is not yet
tethered. DLP traffic fails gracefully in this window — the AI Gateway remains operational.

For the full traffic-flow context of each sequence, see
[ARCHITECTURE.md — Traffic Flows](ARCHITECTURE.md#traffic-flows).

### Pre-launch: certificate and bootstrap secret (stack creation only)

Before any instances launch, the `CertGeneratorFunction` custom resource runs and establishes the
shared configuration that both services depend on:

```
Stack creation
  └─ CertGeneratorFunction custom resource
       1. Generates a self-signed TLS certificate (CN=dlp.aigw.internal, 10-year validity)
       2. Imports the certificate to ACM → receives CertificateArn
       3. Writes the certificate PEM to SSM Parameter Store: /<stack>/dlpod-cert
       4. Pre-populates the AIG bootstrap secret with the DLP block:
            { "dlp": { "host": "dlp.aigw.internal", "certificate": "<PEM>" } }
```

This ordering guarantee means the DLP endpoint and certificate are already present in the AIG
bootstrap secret before the first AIG instance boots. The AIG Activation Lambda only needs to add
the per-instance enrollment token — the DLP configuration is already there.

> If this step fails, AIG instances will boot without DLP forwarding configured. See
> [TROUBLESHOOTING.md — Certificate Issues](TROUBLESHOOTING.md#certificate-issues).

---

### DLPoD tethering flow (per instance, ~15–25 min)

Triggered for every DLPoD instance that launches — at stack creation and on every scale-out or
replacement. Each instance runs its own independent Step Functions execution.

```
Instance launch
  │
  ├─ 1. ASG launches DLPoD instance
  │       └─ Lifecycle hook holds instance in Pending:Wait (HeartbeatTimeout: 1800s / 30 min)
  │           If tethering does not complete within 30 min → instance ABANDONED, replacement launches
  │
  ├─ 2. ASG lifecycle event → SNS topic → DlpodActivationFunction (Lambda)
  │       └─ Reads instance private IP from EC2
  │       └─ Starts Step Functions execution named: tether-<instance-id>
  │       └─ Returns immediately (does not wait for tethering)
  │
  └─ 3. Step Functions execution orchestrates SSH automation via DlpodTetherFunction (Lambda):
         │
         ├─ WaitForDlpodSSH (up to ~8 min)
         │     Polls SSH port 22 on the instance every 25 seconds until it accepts connections.
         │     The instance needs time to boot and start sshd before SSH is available.
         │
         ├─ DlpodChangePassword
         │     SSH connects with the factory default password.
         │     Generates a unique random 24-character password for this instance.
         │     Changes the password via the DLPoD CLI. The new password exists only in
         │     Step Functions execution state — it is never persisted to storage.
         │
         ├─ MergePassword
         │     Internal Step Functions state — propagates the new password into the
         │     execution context so subsequent states can SSH with it.
         │
         ├─ DlpodSetDNS
         │     SSH connects with the new password.
         │     Configures the VPC DNS resolver (DnsServer parameter, default 10.0.0.2)
         │     via the DLPoD CLI. Required so DLPoD can resolve AWS service endpoints
         │     for the tethering callhome.
         │
         ├─ DlpodSetLicense
         │     Reads the DLPoD license key from Secrets Manager (<stack>-dlpod-credentials).
         │     Applies the license key via the DLPoD CLI.
         │     License application triggers DLPoD to initiate tethering to the Netskope
         │     management plane (callhome).
         │
         ├─ WaitForDlpodTetheringInit (fixed 120s wait)
         │     A fixed delay that gives DLPoD time to initiate its outbound tethering
         │     connection before polling begins.
         │
         ├─ CheckDlpodTethering (up to ~15 min)
         │     Polls the DLPoD tethering status via SSH every 60 seconds.
         │     Continues until tethering reports complete or the 30-min lifecycle hook
         │     heartbeat expires.
         │
         └─ DlpodCompleteLifecycle
               Calls CompleteLifecycleAction: CONTINUE on the ASG lifecycle hook.
               Instance moves from Pending:Wait → InService.
               DLPoD ALB health check passes → instance begins receiving DLP inspection traffic.
```

**On termination:** The DLPoD termination lifecycle hook fires but completes immediately — there
is no tethering cleanup. The Netskope management plane detects the DLPoD instance disconnect and
stops routing configuration updates to it.

---

### AIG enrollment flow (per instance, ~5–15 min)

Triggered for every AIG instance that launches — at stack creation and on every scale-out or
replacement. The lifecycle hook heartbeat is only 120 seconds, so the Activation Lambda must
complete quickly.

```
Instance launch
  │
  ├─ 1. ASG launches AIG instance
  │       └─ Lifecycle hook holds instance in Pending:Wait (HeartbeatTimeout: 120s / 2 min)
  │           If the Activation Lambda does not complete within 2 min → instance ABANDONED
  │
  ├─ 2. ASG lifecycle event → SNS topic → AigActivationFunction (Lambda)
  │       │
  │       ├─ a. Reads API credentials from Secrets Manager (<stack>-api-credentials)
  │       │         { "api_token": "...", "tenant_url": "..." }
  │       │
  │       ├─ b. Calls Netskope REST API: POST /api/v2/aig/appliances
  │       │         Registers the appliance with the tenant → receives:
  │       │           - appliance_id  (Netskope's identifier for this appliance)
  │       │           - enrollment_token  (one-time token; used by the instance at boot)
  │       │
  │       ├─ c. Writes enrollment_token to Secrets Manager bootstrap secret (<stack>-aig-bootstrap)
  │       │         The DLP block (host + certificate) is already present from CertGeneratorFunction.
  │       │         After this write, the bootstrap secret contains:
  │       │           { "bootstrap": true,
  │       │             "enrollment_token": "<token>",
  │       │             "dlp": { "host": "dlp.aigw.internal", "certificate": "<PEM>" } }
  │       │
  │       ├─ d. Writes appliance_id to AWS Systems Manager Parameter Store
  │       │         Path: /aig/<stack>/<instance-id>/appliance-id
  │       │         Purpose: the termination Lambda invocation is a fresh execution with no memory
  │       │         of the launch — it needs the appliance_id to call DELETE /api/v2/aig/appliances/{id}.
  │       │         SSM is the bridge between the two invocations.
  │       │
  │       └─ e. Calls CompleteLifecycleAction: CONTINUE
  │                 Instance moves from Pending:Wait → InService.
  │
  └─ 3. AIG instance boots and reads bootstrap secret from Secrets Manager
           └─ Self-enrolls with Netskope tenant using enrollment_token
           └─ Configures DLP forwarding to dlp.aigw.internal using the DLP block
           └─ Starts the AI Gateway service
           └─ ALB health check passes (HTTPS GET / on port 443) → serving requests
```

**On termination:** The AIG termination lifecycle hook fires → AigActivationFunction reads the
`appliance_id` from SSM → calls `DELETE /api/v2/aig/appliances/{id}` to deregister from the
Netskope tenant → deletes the SSM parameter → completes the lifecycle hook.

> **DLP traffic in the startup window:** AIG instances configure DLP forwarding from their first
> boot (the DLP block is in the bootstrap secret before any instance launches). If DLPoD has not
> yet tethered, DLP traffic fails gracefully — the AI Gateway continues serving requests but DLP
> inspection is not applied until a healthy DLPoD target is available.

---

## Monitoring and Alerts

### Log Groups

| Log group | Contents | Typical volume |
|---|---|---|
| `/aws/lambda/<stack>-aig-activation` | AIG enrollment/deregistration events, Netskope API calls, lifecycle hook completion | ~10 lines per instance launch/termination |
| `/aws/lambda/<stack>-dlpod-activation` | DLPoD ASG lifecycle events, Step Functions execution start | ~5 lines per instance launch/termination |
| `/aws/lambda/<stack>-dlpod` | DLPoD SSH tethering steps, license application, tethering status polls | ~50–100 lines per tethering run |
| `/aws/lambda/<stack>-cert-generator` | Self-signed cert generation, ACM import, SSM writes | ~20 lines at stack creation only |

Tail any log group in real time:
```bash
aws logs tail /aws/lambda/<stack>-aig-activation --follow --region <region>
```

### Built-in CloudWatch Alarm

The stack creates one CloudWatch alarm automatically:

| Alarm | Metric | Threshold | Action |
|---|---|---|---|
| `<stack>-aig-high-cpu` | `AWS/EC2 CPUUtilization` on AIG ASG | Average ≥ `ScaleOutCpuThreshold` (default 70%) for 2 consecutive 5-min periods | Step scaling — adds one AIG instance |

Check alarm state:
```bash
aws cloudwatch describe-alarms --alarm-names <stack>-aig-high-cpu \
  --query "MetricAlarms[0].[StateValue,StateReason]" --output table --region <region>
```

`INSUFFICIENT_DATA` is normal at minimum capacity with low traffic. `ALARM` triggers scale-out.

### Recommended Additional Alarms

These alarms are not created by the stack but are useful for production deployments:

```bash
STACK=<stack-name>
REGION=<region>

# Alert when AIG ASG has fewer instances than desired (instance failures)
aws cloudwatch put-metric-alarm \
  --alarm-name "$STACK-aig-below-desired" \
  --metric-name GroupInServiceInstances \
  --namespace AWS/AutoScaling \
  --dimensions Name=AutoScalingGroupName,Value=$STACK-aig-asg \
  --statistic Minimum --period 300 --threshold 1 \
  --comparison-operator LessThanThreshold --evaluation-periods 1 \
  --alarm-description "AIG ASG in-service count below desired" \
  --region $REGION

# Alert when DLPoD ALB has no healthy targets
aws cloudwatch put-metric-alarm \
  --alarm-name "$STACK-dlpod-no-healthy-targets" \
  --metric-name HealthyHostCount \
  --namespace AWS/ApplicationELB \
  --dimensions \
    Name=LoadBalancer,Value=<dlpod-alb-arn-suffix> \
    Name=TargetGroup,Value=<dlpod-tg-arn-suffix> \
  --statistic Minimum --period 300 --threshold 1 \
  --comparison-operator LessThanThreshold --evaluation-periods 1 \
  --alarm-description "DLPoD has no healthy targets — DLP inspection unavailable" \
  --region $REGION

# Alert on AIG Activation Lambda errors
aws cloudwatch put-metric-alarm \
  --alarm-name "$STACK-aig-activation-errors" \
  --metric-name Errors \
  --namespace AWS/Lambda \
  --dimensions Name=FunctionName,Value=$STACK-aig-activation \
  --statistic Sum --period 300 --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold --evaluation-periods 1 \
  --alarm-description "AIG Activation Lambda errors — enrollment failures" \
  --region $REGION
```

---

## Key Operational Commands

### AI Gateway

| Task | Command |
|---|---|
| AIG ASG instance states | `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <stack>-aig-asg --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" --output table --region <region>` |
| AIG ALB target health | `aws elbv2 describe-target-health --target-group-arn <aig-tg-arn> --output table --region <region>` |
| AIG activation Lambda logs | `aws logs tail /aws/lambda/<stack>-aig-activation --since 30m --region <region>` |
| AIG bootstrap secret | `aws secretsmanager get-secret-value --secret-id <stack>-aig-bootstrap --query SecretString --output text --region <region>` |
| AIG enrolled appliances | Check Netskope portal: **Settings → Security Cloud Platform → AI Gateway** |

### DLP On Demand

| Task | Command |
|---|---|
| DLPoD ASG instance states | `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <stack>-dlpod-asg --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" --output table --region <region>` |
| DLPoD tethering executions | `aws stepfunctions list-executions --state-machine-arn <dlpod-sfn-arn> --output table --region <region>` |
| DLPoD ALB target health | `aws elbv2 describe-target-health --target-group-arn <dlpod-tg-arn> --output table --region <region>` |
| DLPoD activation Lambda logs | `aws logs tail /aws/lambda/<stack>-dlpod-activation --since 30m --region <region>` |
| DLPoD tethering Lambda logs | `aws logs tail /aws/lambda/<stack>-dlpod --since 30m --region <region>` |
| DLPoD cert (SSM) | `aws ssm get-parameter --name /<stack>/dlpod-cert --query Parameter.Value --output text --region <region>` |

**Get the DLPoD tethering state machine ARN from stack outputs:**
```bash
STACK=<stack-name>
REGION=<region>

DLPOD_SFN=$(aws cloudformation describe-stacks --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='DlpodTetheringStateMachineArn'].OutputValue" \
  --output text --region $REGION)
```

---

## Scaling

### Scale AIG out manually

```bash
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name <stack>-aig-asg \
  --desired-capacity <new-count> \
  --region <region>
```

Each new AIG instance goes through the full enrollment flow (~5–15 minutes). The activation
Lambda writes the enrollment token to the bootstrap secret and completes the lifecycle hook —
enrollment happens autonomously on the instance from there.

### Scale DLPoD out manually

```bash
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name <stack>-dlpod-asg \
  --desired-capacity <new-count> \
  --region <region>
```

Each new DLPoD instance tethers independently (~15–25 minutes). DLPoD instances do not conflict —
each has its own Step Functions execution and its own SSH password.

### Scale in

```bash
# AIG scale in
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name <stack>-aig-asg \
  --desired-capacity <new-count> --region <region>

# DLPoD scale in
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name <stack>-dlpod-asg \
  --desired-capacity <new-count> --region <region>
```

AIG termination lifecycle hook fires — the activation Lambda deregisters the appliance from the
Netskope tenant and deletes the SSM appliance ID parameter. DLPoD termination hook fires but
completes immediately (no tethering cleanup required).

### Update MinSize/MaxSize via stack update

```bash
aws cloudformation update-stack \
  --stack-name <stack-name> \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-combined.yaml \
  --parameters \
    ParameterKey=NetskopeApiToken,UsePreviousValue=true \
    ParameterKey=DlpodLicenseKey,UsePreviousValue=true \
    ParameterKey=LambdaCodeBucket,UsePreviousValue=true \
    ParameterKey=AcmCertificateArn,UsePreviousValue=true \
    ParameterKey=MaxSize,ParameterValue=<new-max> \
    ParameterKey=DlpodMaxCapacity,ParameterValue=<new-dlpod-max> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

---

## AMI Upgrade Procedure

When Netskope releases a new AI Gateway or DLP On Demand AMI version, upgrade by updating the
stack parameter. The ASG performs a rolling instance refresh — existing instances are replaced
one at a time with new instances running the updated AMI.

**1. Find the new AMI ID:**
```bash
aws ec2 describe-images \
  --filters 'Name=name,Values=*Netskope AI Gateway*' \
  --query 'sort_by(Images, &CreationDate)[-1].[ImageId,Name,CreationDate]' \
  --output table --region <region>
```

**2. Update the stack with the new AMI:**
```bash
aws cloudformation update-stack \
  --stack-name <stack-name> \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-combined.yaml \
  --parameters \
    ParameterKey=NetskopeApiToken,UsePreviousValue=true \
    ParameterKey=DlpodLicenseKey,UsePreviousValue=true \
    ParameterKey=LambdaCodeBucket,UsePreviousValue=true \
    ParameterKey=AcmCertificateArn,UsePreviousValue=true \
    ParameterKey=GatewayAmiId,ParameterValue=<new-aig-ami-id> \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

**3. What happens:**
- The ASG launch template is updated with the new AMI
- CloudFormation triggers an instance refresh on the ASG
- Each existing AIG instance is terminated; a replacement launches with the new AMI
- The replacement goes through the full enrollment flow (~5–15 min per instance)
- The ALB maintains traffic to healthy instances during the rolling replacement

**4. Monitor the refresh:**
```bash
aws autoscaling describe-instance-refreshes \
  --auto-scaling-group-name <stack>-aig-asg \
  --query "InstanceRefreshes[0].[Status,PercentageComplete,StatusReason]" \
  --output table --region <region>
```

> **DLP On Demand AMI upgrade:** Use `DlpodAmiId` parameter instead of `GatewayAmiId`. The same
> process applies — each replaced DLPoD instance goes through full tethering (~15–25 min).
> Plan for reduced DLP capacity during the rolling upgrade.

---

## IAM Roles

| Role | Principal | Purpose |
|---|---|---|
| `<stack>-aig-role` | `ec2.amazonaws.com` | AIG instance role: read bootstrap secret, CloudWatch logs |
| `<stack>-aig-activation-role` | `lambda.amazonaws.com` | Register/deregister AIG in tenant API, write enrollment token to bootstrap secret, read DLPoD cert from SSM, complete lifecycle hooks |
| `<stack>-aig-lifecycle-sns-role` | `autoscaling.amazonaws.com` | Publish AIG lifecycle events to SNS |
| `<stack>-dlpod-role` | `ec2.amazonaws.com` | DLPoD instance role: CloudWatch logs only |
| `<stack>-dlpod-activation-role` | `lambda.amazonaws.com` | Get instance IP, start DLPoD tethering Step Functions, complete termination lifecycle |
| `<stack>-dlpod-sfn-role` | `states.amazonaws.com` | Invoke DLPoD tethering Lambda |
| `<stack>-dlpod-lambda-role` | `lambda.amazonaws.com` | SSH to DLPoD (VPC-attached), read license from Secrets Manager, write cert to SSM, complete launch lifecycle |
| `<stack>-dlpod-lifecycle-sns-role` | `autoscaling.amazonaws.com` | Publish DLPoD lifecycle events to SNS |
| `<stack>-cert-generator-role` | `lambda.amazonaws.com` | Generate self-signed cert, import to ACM, write PEM to SSM, update bootstrap secret |

---

## Secrets and SSM Parameters

| Resource | Who reads it | Contents |
|---|---|---|
| `<stack>-aig-bootstrap` (Secrets Manager) | AIG instances at boot; AIG activation Lambda writes | `{"bootstrap": true, "enrollment_token": "...", "dlp": {"certificate": "...", "host": "..."}}` |
| `<stack>-api-credentials` (Secrets Manager) | AIG activation Lambda only | `{"api_token": "...", "tenant_url": "..."}` |
| `<stack>-dlpod-credentials` (Secrets Manager) | DLPoD tethering Lambda only | `{"license_key": "..."}` |
| `/<stack>/dlpod-cert` (SSM Parameter) | AIG activation Lambda; DLPoD cert verification | PEM-encoded DLPoD ALB self-signed cert (10-year validity) |
| `/<stack>/appliances/<instance-id>` (SSM) | AIG activation Lambda (termination cleanup) | AIG appliance ID in Netskope tenant |

---

## Troubleshooting

For full Issue/Cause/Solution troubleshooting, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Quick reference for the most common issues:

| Symptom | Where to look |
|---|---|
| AIG instance stuck in `Pending:Wait` more than 2 minutes | `/aws/lambda/<stack>-aig-activation` logs |
| AIG instance ABANDONED | Activation Lambda logs; fix root cause before next replacement launches |
| DLPoD instance stuck in `Pending:Wait` | Step Functions console → `<stack>-dlpod-tethering`; DLPoD tethering Lambda logs |
| DLP inspection not working (AIG enrolled but no DLP) | Check DLPoD ALB target health; check bootstrap secret has `dlp` block |
| Scale-out alarm not triggering | `aws cloudwatch describe-alarms --alarm-names <stack>-aig-high-cpu` |
| Stack deletion hanging | Check for instances in `Terminating:Wait`; may need manual `CompleteLifecycleAction` |
