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

**How bootstrap works:**
DLP On Demand self-configures at first boot. The stack delivers a `bootstrap.json` document to
each instance through EC2 UserData; the appliance's `nsbootstrap.service` reads it at first boot
and applies the TLS server certificate and key, the license key, the DNS resolver, and the
`dlp-on-demand` persona. No SSH, no lifecycle hook, and no orchestration service is involved —
the instance is `InService` in the ASG as soon as it launches and becomes usable once the DLPoD
ALB health check passes (typically 5–10 minutes after launch).

Once licensed, DLP On Demand connects outbound to the Netskope management plane (via the NAT
Gateway), receives DLP profiles, and begins inspecting content forwarded from AI Gateway
instances.

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

Stack creation is strictly ordered: DLPoD must be healthy before the first AIG instance launches.
`GatewayAutoScalingGroup` has `DependsOn: DlpodReadinessGate`, a custom resource that polls the
DLPoD ALB target group until every target is healthy. The AI Gateway validates the DLPoD HTTPS
endpoint during enrollment, so this gate guarantees the endpoint is serving before AIG boots.
Total creation time is 12–18 minutes (DLPoD bootstrap ~5–10 min, then AIG enrollment ~5–15 min).

For the full traffic-flow context of each sequence, see
[ARCHITECTURE.md — Traffic Flows](ARCHITECTURE.md#traffic-flows). The same ordering is described
from the deployment perspective in [DEPLOYMENT.md — Startup Ordering](DEPLOYMENT.md#startup-ordering).

### Pre-launch: certificate and bootstrap UserData (stack creation only)

Before any instances launch, two custom resources establish the shared configuration that both
services depend on:

```
Stack creation
  ├─ 1. DlpodAlbCertificate (Custom::AlbCertificate → CertGeneratorFunction, <stack>-certgen)
  │       a. Generates a two-tier cert hierarchy: CA + leaf (CN=dlp.aigw.internal, 365-day validity)
  │       b. Imports the leaf cert + key to ACM → CertificateArn used by the DLPoD ALB listener
  │       c. Writes the CA cert PEM to SSM Parameter Store: /<stack>/dlpod-cert
  │       d. Writes CA cert + leaf cert + leaf key to Secrets Manager: <stack>-dlpod-cert-key
  │
  └─ 2. DlpodBootstrapPart1 / DlpodBootstrapPart2 (Custom::DlpodBootstrap → DlpodBootstrapBuilderFunction)
          a. Reads leaf cert + key + CA cert from <stack>-dlpod-cert-key
          b. Reads the license key from <stack>-dlpod-credentials
          c. Assembles bootstrap.json:
               { "dlpaas": { "server-cert", "server-key", "server-intermediate-ca-chain" },
                 "dns": { "primary": "169.254.169.253" },
                 "system": { "licensekey": "<key>" },
                 "persona": "dlp-on-demand" }
          d. Base64-encodes it and returns it in two halves (custom resource responses are
             limited to 4 KB); DlpodLaunchTemplate re-joins them as the instance UserData
```

The AIG side does not read the bootstrap UserData. Instead, the AIG Activation Lambda reads the
CA cert from `/<stack>/dlpod-cert` at every AIG instance launch and writes it, together with the
fixed DLP host `https://dlp.aigw.internal`, into the AIG bootstrap secret (see the AIG flow below).

> If the certificate step fails the stack rolls back — nothing else can be created without it. If
> the bootstrap builder fails, DLPoD instances never receive a valid `bootstrap.json` and never
> become healthy. See [TROUBLESHOOTING.md — Certificate Issues](TROUBLESHOOTING.md#certificate-issues)
> and [TROUBLESHOOTING.md — DLP On Demand Issues](TROUBLESHOOTING.md#dlp-on-demand-issues).

---

### DLPoD bootstrap flow (per instance, ~5–10 min)

Runs for every DLPoD instance that launches — at stack creation and on every scale-out or
replacement. There is no lifecycle hook, Lambda, or orchestration per instance: the launch
template UserData already contains everything the appliance needs.

```
Instance launch
  │
  ├─ 1. ASG launches DLPoD instance from DlpodLaunchTemplate
  │       └─ Instance enters InService immediately (no lifecycle hook)
  │       └─ HealthCheckType: ELB, HealthCheckGracePeriod: 1800s / 30 min
  │           If the ALB health check is still failing after 30 min → ASG marks the instance
  │           Unhealthy and launches a replacement
  │
  ├─ 2. nsbootstrap.service reads bootstrap.json from EC2 UserData at first boot
  │       └─ Installs the TLS server cert, key, and CA chain (dlpaas block)
  │       └─ Sets the DNS resolver to 169.254.169.253 (Route 53 Resolver — resolves AWS
  │           endpoints and the aigw.internal private zone)
  │       └─ Applies the license key (system.licensekey)
  │       └─ Sets the persona to dlp-on-demand
  │       └─ Appliance connects outbound to the Netskope management plane via the NAT Gateway
  │
  └─ 3. DLPoD service starts listening on HTTPS:443
           └─ DLPoD ALB health check (HTTPS GET / on 443, any 200–499 response,
               2 consecutive passes at 30 s) → target healthy
           └─ Instance begins receiving DLP inspection traffic from AIG
```

**Readiness gate (stack creation only):** after `DlpodAutoScalingGroup` is created, the
`DlpodReadinessGate` custom resource (inline Lambda `<stack>-dlpod-readiness`, log group
`/aws/lambda/<stack>-dlpod-readiness`) polls `describe-target-health` on the DLPoD target group
every 30 seconds. It returns `SUCCESS` when all targets are healthy and `FAILED` (rolling the stack
back) if that has not happened within 14 minutes. `GatewayAutoScalingGroup` depends on this
resource, so no AIG instance launches until DLPoD is serving. The gate only runs on stack create —
it is a no-op on update and delete, and does not apply to later scale-outs.

**On termination:** There is no termination hook. The instance is terminated by the ASG and the
ALB deregisters the target. The Netskope management plane detects the appliance disconnect.

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
  │       ├─ a. Reads API credentials from Secrets Manager (<stack>-netskope-credentials)
  │       │         { "tenant_url": "...", "api_token": "..." }
  │       │
  │       ├─ b. Calls Netskope REST API: POST /api/v2/aig/appliances
  │       │         Registers the appliance (name <stack>-gw-<instance-id>, host = private IP) → receives:
  │       │           - id  (Netskope's identifier for this appliance)
  │       │           - enrollment_token  (one-time token; used by the instance at boot)
  │       │
  │       ├─ c. Writes appliance id to AWS Systems Manager Parameter Store
  │       │         Path: /aig/<stack>/<instance-id>
  │       │         Purpose: the termination Lambda invocation is a fresh execution with no memory
  │       │         of the launch — it needs the id to call DELETE /api/v2/aig/appliances/{id}.
  │       │         SSM is the bridge between the two invocations.
  │       │
  │       ├─ d. Reads the DLPoD CA cert PEM from SSM: /<stack>/dlpod-cert
  │       │         (written by CertGeneratorFunction at stack creation)
  │       │
  │       ├─ e. Writes the bootstrap secret (<stack>-aig-bootstrap) — full overwrite:
  │       │           { "bootstrap": true,
  │       │             "enrollment_token": "<token>",
  │       │             "dlp": { "host": "https://dlp.aigw.internal", "certificate": "<CA PEM>" },
  │       │             "ai_guardrails": { "host": "http://guardrails.aigw.internal:8080/invocations" } }
  │       │         The ai_guardrails block is present only when GuardrailsImageS3Bucket was set.
  │       │
  │       └─ f. Calls CompleteLifecycleAction: CONTINUE
  │                 Instance moves from Pending:Wait → InService.
  │                 Any exception → CompleteLifecycleAction: ABANDON (instance is replaced).
  │
  └─ 3. AIG instance boots and reads bootstrap secret from Secrets Manager
           └─ UserData: {"bootstrap_secret": "<stack>-aig-bootstrap"}
           └─ Self-enrolls with Netskope tenant using enrollment_token
           └─ Configures DLP forwarding to https://dlp.aigw.internal using the DLP block
           └─ Starts the AI Gateway service
           └─ ALB health check passes (HTTPS GET / on port 443) → serving requests
```

**On termination:** The AIG termination lifecycle hook fires → AigActivationFunction reads the
appliance id from `/aig/<stack>/<instance-id>` → calls `DELETE /api/v2/aig/appliances/{id}` to
deregister from the Netskope tenant → deletes the SSM parameter → completes the lifecycle hook
(`CONTINUE` even if deregistration fails, so termination is never blocked).

> **Shared bootstrap secret:** every launch overwrites `<stack>-aig-bootstrap` with that instance's
> enrollment token. Scale AIG one instance at a time — two instances launching concurrently can
> read each other's token. The DLP (and Guardrails) block is identical for all instances, so it is
> unaffected by this.

> **DLP traffic:** AIG instances configure DLP forwarding from their first boot. At stack creation
> the readiness gate guarantees DLPoD is healthy first. On later AIG scale-outs, if the DLPoD ALB
> has no healthy targets, the AI Gateway continues serving requests but DLP inspection is not
> applied until a healthy DLPoD target is available.

---

## Monitoring and Alerts

### Log Groups

| Log group | Contents | Typical volume |
|---|---|---|
| `/aws/lambda/<stack>-aig-activation` | AIG enrollment/deregistration events, Netskope API calls, lifecycle hook completion | ~10 lines per instance launch/termination |
| `/aws/lambda/<stack>-certgen` | Cert hierarchy generation, ACM import, SSM and Secrets Manager writes | ~5 lines at stack creation (and delete) only |
| `/aws/lambda/<stack>-dlpod-bootstrap-builder` | `bootstrap.json` assembly — size of the base64 UserData and each half | ~2 lines at stack creation only |
| `/aws/lambda/<stack>-dlpod-readiness` | Readiness gate polls: `N/M target(s) healthy — waiting 30s...` for DLPoD (and Guardrails, if deployed) | ~1 line per 30 s during stack creation only |

DLPoD instances themselves write no CloudWatch logs from the stack's perspective —
`nsbootstrap.service` runs on the appliance. Its status is observable only through the DLPoD ALB
target health (below) or on the appliance itself; consult the Netskope DLP On Demand
documentation for appliance-side diagnostics.

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
| DLPoD ALB target health | `aws elbv2 describe-target-health --target-group-arn $(aws elbv2 describe-target-groups --names <stack>-dlpod-tg --query "TargetGroups[0].TargetGroupArn" --output text --region <region>) --output table --region <region>` |
| DLPoD bootstrap builder logs | `aws logs tail /aws/lambda/<stack>-dlpod-bootstrap-builder --since 1h --region <region>` |
| DLPoD readiness gate logs (stack create) | `aws logs tail /aws/lambda/<stack>-dlpod-readiness --since 1h --region <region>` |
| Readiness gate result | `aws cloudformation describe-stack-events --stack-name <stack> --query "StackEvents[?LogicalResourceId=='DlpodReadinessGate'].[Timestamp,ResourceStatus,ResourceStatusReason]" --output table --region <region>` |
| DLPoD CA cert (SSM) | `aws ssm get-parameter --name /<stack>/dlpod-cert --query Parameter.Value --output text --region <region>` |
| DLPoD service URL | `https://dlp.aigw.internal` (fixed; `DlpodServiceUrl` stack output) — resolvable only inside the VPC |

### AI Guardrails (only when `GuardrailsImageS3Bucket` was set)

| Task | Command |
|---|---|
| Guardrails ASG instance states | `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <stack>-guardrails-asg --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" --output table --region <region>` |
| Guardrails ALB target health | `aws elbv2 describe-target-health --target-group-arn $(aws elbv2 describe-target-groups --query "TargetGroups[?contains(TargetGroupName,'<stack>-guardrails')].TargetGroupArn" --output text --region <region>) --output table --region <region>` |
| Shell on a Guardrails instance | `aws ssm start-session --target <instance-id> --region <region>` (no SSH key; role has `AmazonSSMManagedInstanceCore`) |
| Container status / logs | on the instance: `sudo docker ps`, `sudo docker logs guardrails`, `cat /var/log/user-data.log` |
| Local health check | on the instance: `curl -s http://localhost:8080/ping` → `Healthy` |
| Health check from an AIG instance | `curl -s http://guardrails.aigw.internal:8080/ping` |
| Scale Guardrails | `aws autoscaling update-auto-scaling-group --auto-scaling-group-name <stack>-guardrails-asg --desired-capacity <N> --region <region>` |
| Roll to a new image | Upload the new tarball to S3 (new key), `aws cloudformation update-stack` with the new `GuardrailsImageS3Key`, then start an ASG instance refresh: `aws autoscaling start-instance-refresh --auto-scaling-group-name <stack>-guardrails-asg --region <region>` |

Guardrails instances have no lifecycle hook: a replacement instance pulls the image and starts the
container from UserData, and the ALB health check (`/ping`, HTTP 200) governs when it receives traffic.
Grace period is 20 minutes to allow for a multi-GB image pull.

**Get the target group ARNs once and reuse them:**
```bash
STACK=<stack-name>
REGION=<region>

AIG_TG=$(aws elbv2 describe-target-groups --names $STACK-aig-tg \
  --query "TargetGroups[0].TargetGroupArn" --output text --region $REGION)
DLPOD_TG=$(aws elbv2 describe-target-groups --names $STACK-dlpod-tg \
  --query "TargetGroups[0].TargetGroupArn" --output text --region $REGION)
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
enrollment happens autonomously on the instance from there. Increase desired capacity by one at
a time: all AIG instances share the single `<stack>-aig-bootstrap` secret, and concurrent launches
can pick up each other's enrollment token.

### Scale DLPoD out manually

```bash
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name <stack>-dlpod-asg \
  --desired-capacity <new-count> \
  --region <region>
```

Each new DLPoD instance bootstraps independently from the same launch template UserData
(~5–10 minutes until the ALB health check passes). DLPoD instances do not conflict — the
`bootstrap.json` is identical for every instance and contains no per-instance state. The readiness
gate does not run on scale-out; watch the DLPoD ALB target health to know when the new instance is
serving. Concurrent DLPoD launches are safe.

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
Netskope tenant and deletes the SSM appliance ID parameter. DLPoD has no termination hook — the
instance is simply terminated and deregistered from the ALB.

### Update desired capacity via stack update

Both ASGs are fixed at `MinSize: 1` / `MaxSize: 4` in the template. The desired counts are
parameters and can be changed persistently with a stack update (a manual `set-desired-capacity`
is reverted on the next stack update that touches the ASG):

```bash
aws cloudformation update-stack \
  --stack-name <stack-name> \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-combined.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,UsePreviousValue=true \
    ParameterKey=NetskopeApiToken,UsePreviousValue=true \
    ParameterKey=DlpodLicenseKey,UsePreviousValue=true \
    ParameterKey=AcmCertificateArn,UsePreviousValue=true \
    ParameterKey=GuardrailsImageS3Bucket,UsePreviousValue=true \
    ParameterKey=GuardrailsAmiId,UsePreviousValue=true \
    ParameterKey=DesiredCapacity,ParameterValue=<new-aig-count> \
    ParameterKey=DlpodDesiredCapacity,ParameterValue=<new-dlpod-count> \
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
    ParameterKey=NetskopeTenantUrl,UsePreviousValue=true \
    ParameterKey=NetskopeApiToken,UsePreviousValue=true \
    ParameterKey=DlpodLicenseKey,UsePreviousValue=true \
    ParameterKey=AcmCertificateArn,UsePreviousValue=true \
    ParameterKey=GuardrailsImageS3Bucket,UsePreviousValue=true \
    ParameterKey=GuardrailsAmiId,UsePreviousValue=true \
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
> process applies — each replaced DLPoD instance bootstraps from the existing launch template
> UserData and passes the ALB health check in ~5–10 min. Plan for reduced DLP capacity during
> the rolling upgrade. Monitor with `describe-instance-refreshes` on `<stack>-dlpod-asg` and
> `describe-target-health` on the DLPoD target group.

---

## IAM Roles

| Role | Principal | Purpose |
|---|---|---|
| `<stack>-gateway-role` | `ec2.amazonaws.com` | AIG instance role: read `<stack>-aig-bootstrap` secret, `CloudWatchAgentServerPolicy` |
| `<stack>-aig-activation-role` | `lambda.amazonaws.com` | Read `<stack>-netskope-credentials`, write `<stack>-aig-bootstrap`, read `/<stack>/dlpod-cert`, put/get/delete `/aig/<stack>/*`, `ec2:DescribeInstances`, complete lifecycle hooks on `<stack>-aig-asg` |
| `<stack>-aig-lifecycle-sns-role` | `autoscaling.amazonaws.com` | Publish AIG lifecycle events to SNS |
| `<stack>-dlpod-role` | `ec2.amazonaws.com` | DLPoD instance role: `CloudWatchAgentServerPolicy` only — no access to any secret or parameter |
| `<stack>-dlpod-bootstrap-builder-role` | `lambda.amazonaws.com` | Read `<stack>-dlpod-cert-key` and `<stack>-dlpod-credentials` to assemble `bootstrap.json` UserData |
| `<stack>-dlpod-readiness-role` | `lambda.amazonaws.com` | `elasticloadbalancing:DescribeTargetHealth` — used by the DLPoD (and Guardrails) readiness gates |
| `<stack>-certgen-role` | `lambda.amazonaws.com` | Import/delete cert in ACM, write `/<stack>/*` SSM parameters, write `<stack>-dlpod-cert-key` |
| `<stack>-guardrails-role` *(Guardrails only)* | `ec2.amazonaws.com` | Guardrails instance role: `s3:GetObject` on the image tarball, `AmazonSSMManagedInstanceCore`, CloudWatch |

No IAM role in the stack can SSH to or otherwise log in to a DLPoD instance — DLPoD instances
accept only HTTPS:443 from the DLPoD ALB security group.

---

## Secrets and SSM Parameters

| Resource | Who reads it | Contents |
|---|---|---|
| `<stack>-aig-bootstrap` (Secrets Manager) | AIG instances at boot; AIG activation Lambda writes | `{"bootstrap": true, "enrollment_token": "...", "dlp": {"certificate": "<CA PEM>", "host": "https://dlp.aigw.internal"}}` plus `"ai_guardrails": {"host": "..."}` when Guardrails is deployed |
| `<stack>-netskope-credentials` (Secrets Manager) | AIG activation Lambda only | `{"tenant_url": "...", "api_token": "..."}` |
| `<stack>-dlpod-credentials` (Secrets Manager) | DLPoD bootstrap builder Lambda only (stack create/update) | `{"license_key": "..."}` |
| `<stack>-dlpod-cert-key` (Secrets Manager) | Cert generator Lambda writes; DLPoD bootstrap builder Lambda reads | `{"ca_cert_pem": "...", "leaf_cert_pem": "...", "leaf_key_pem": "..."}` — the leaf cert + key are what DLPoD serves on 443 |
| `/<stack>/dlpod-cert` (SSM Parameter) | AIG activation Lambda at every AIG launch | PEM-encoded DLPoD CA certificate (365-day validity) — AIG trusts the DLPoD ALB leaf via this CA |
| `/<stack>/aig-cert` (SSM Parameter) | Operators | AIG ALB self-signed cert PEM — only exists when `AcmCertificateArn` was left empty |
| `/aig/<stack>/<instance-id>` (SSM) | AIG activation Lambda (termination cleanup) | AIG appliance ID in Netskope tenant |

The DLPoD instance never reads Secrets Manager or SSM: its configuration (including the TLS
private key and license key) is embedded in the launch template UserData, which is readable by
anyone with `ec2:DescribeLaunchTemplateVersions` on the account. Treat launch template access
accordingly.

> **Certificate renewal:** the DLPoD CA and leaf certs are valid for 365 days from stack creation.
> A stack update that re-runs `DlpodAlbCertificate` (e.g. changing its properties) regenerates the
> hierarchy, rewrites SSM and `<stack>-dlpod-cert-key`, and rebuilds the DLPoD UserData; DLPoD and
> AIG instances then need to be replaced (instance refresh) to pick up the new certs.

---

## Troubleshooting

For full Issue/Cause/Solution troubleshooting, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Quick reference for the most common issues:

| Symptom | Where to look |
|---|---|
| AIG instance stuck in `Pending:Wait` more than 2 minutes | `/aws/lambda/<stack>-aig-activation` logs |
| AIG instance ABANDONED | Activation Lambda logs; fix root cause before next replacement launches |
| DLPoD target never becomes healthy | DLPoD ALB target health; `/aws/lambda/<stack>-dlpod-bootstrap-builder` logs; DLPoD SG/ALB SG rules |
| Stack rolled back at `DlpodReadinessGate` ("did not become healthy within 14 minutes") | `describe-stack-events`; `/aws/lambda/<stack>-dlpod-readiness` logs; re-create with `--disable-rollback` to inspect |
| DLP inspection not working (AIG enrolled but no DLP) | Check DLPoD ALB target health; check bootstrap secret has `dlp` block |
| Scale-out alarm not triggering | `aws cloudwatch describe-alarms --alarm-names <stack>-aig-high-cpu` |
| Stack deletion hanging | Check for instances in `Terminating:Wait`; may need manual `CompleteLifecycleAction` |
