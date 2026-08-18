# Architecture Overview — AI Gateway + DLP On Demand

AWS reference architecture for deploying Netskope AI Gateway and DLP On Demand together using
CloudFormation. This document explains each design decision through the lens of AWS best practices
and the [AWS Well-Architected Framework](https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html).

## Table of Contents

- [Architecture Diagram](#architecture-diagram)
- [Component Overview](#component-overview)
- [Traffic Flows](#traffic-flows)
- [Security Design](#security-design)
- [High Availability Design](#high-availability-design)
- [Cost Estimate](#cost-estimate)
- [Well-Architected Alignment](#well-architected-alignment)

---

## Architecture Diagram

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
       ┌───────▼──────┐      │      ┌──────▼────────┐
       │  AIG Instance│      │      │  AIG Instance │  ← Private subnets
       │     AZ1      │      │      │     AZ2       │
       └───────┬──────┘      │      └──────┬────────┘
               │             │             │
               └─────────────┼─────────────┘
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

---

## Component Overview

### VPC and Subnet Design

The template creates a new VPC — no pre-existing networking is required.

| Subnet tier | AZs | Hosts | CIDR (default) |
|---|---|---|---|
| Public | AZ1 + AZ2 | AIG ALB, NAT Gateway (AZ1 only) | `10.0.1.0/24`, `10.0.2.0/24` |
| Private | AZ1 + AZ2 | AIG instances, DLPoD instances, DLPoD internal ALB | `10.0.10.0/24`, `10.0.11.0/24` |

No compute resources run in public subnets. The NAT Gateway provides outbound internet access
for private-subnet instances and Lambda functions — enrollment, API calls to the Netskope
management plane, and LLM provider traffic all exit through the NAT Gateway.

> *Well-Architected [SEC05-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_network_protection_create_layers.html):
> Place workloads in private subnets unless they require direct inbound internet access.*

### AI Gateway (AIG) — Internet-Facing Tier

| Attribute | Value |
|---|---|
| Instance type (default) | `m5.4xlarge` (16 vCPU / 64 GB RAM) |
| AMI | Netskope AI Gateway (AWS Marketplace) |
| Deployment | Auto Scaling Group across private AZ1 + AZ2 |
| ALB | Internet-facing, public subnets AZ1 + AZ2, HTTPS port 443 |
| Default capacity | Min 1, Max 4 |
| Auto-scale trigger | Average CPU ≥ 70% for two consecutive 5-minute periods |
| Enrollment | Reads bootstrap secret from Secrets Manager at boot; self-enrolls autonomously |

The AI Gateway presents an OpenAI-compatible HTTPS API to clients. Every request and response
passes through the gateway's inline inspection — DLP, prompt injection detection, access control,
rate limiting, and audit logging — before reaching the upstream LLM provider.

### DLP On Demand (DLPoD) — Internal Inspection Tier

| Attribute | Value |
|---|---|
| Instance type (default) | `c5a.4xlarge` (16 vCPU / 32 GB RAM) |
| AMI | Netskope DLP On Demand (AWS Marketplace) |
| Deployment | Auto Scaling Group across private AZ1 + AZ2 |
| ALB | Internal, private subnets, HTTPS port 443 |
| DNS | `dlp.aigw.internal` (Route 53 private hosted zone) |
| Default capacity | Min 1, Max 4 |
| Tethering | SSH-based CLI automation via Step Functions (~15–25 minutes per instance) |

DLP On Demand receives content from the AI Gateway over HTTPS, applies DLP policies locally
inside the VPC, and returns a verdict. Content never leaves your AWS account for DLP processing.

> *Well-Architected [SEC09-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_protect_data_transit.html):
> Enforce encryption in transit and keep sensitive data within your network boundary.*

### Certificate Management

Both ALBs use TLS certificates managed by the stack:

| Certificate | ALB | Source | Storage |
|---|---|---|---|
| AIG ALB cert | Internet-facing | Auto-generated self-signed (default), or user-provided ACM ARN | ACM (imported by stack when auto-generated) |
| DLPoD ALB cert | Internal | Always auto-generated self-signed (10-year validity) | ACM (imported) + SSM `/<stack>/dlpod-cert` |

The `CertGeneratorFunction` custom resource runs at stack creation before any instances launch.
It generates the DLPoD certificate, imports it to ACM, stores the PEM in SSM Parameter Store,
and pre-populates the AIG bootstrap secret with the DLP endpoint and certificate — so the first
AIG instance that starts already has everything it needs to forward DLP traffic.

> **Note:** The self-signed AIG ALB certificate causes browser security warnings and requires
> clients to trust the cert or disable TLS verification. For production deployments where clients
> cannot be configured to skip certificate verification, provide an ACM-issued certificate ARN
> via `AcmCertificateArn`.

### IAM Design

Nine IAM roles enforce least privilege. No role has more access than its specific function requires.

| Role | Assumed by | Purpose |
|---|---|---|
| `<stack>-aig-role` | `ec2.amazonaws.com` | Read bootstrap Secrets Manager secret at boot. No access to API credentials. |
| `<stack>-aig-activation-role` | `lambda.amazonaws.com` | Call Netskope API to register/deregister AIG; write enrollment token to bootstrap secret; read DLPoD cert from SSM; complete lifecycle hook. |
| `<stack>-aig-lifecycle-sns-role` | `autoscaling.amazonaws.com` | Publish AIG Auto Scaling lifecycle events to SNS. |
| `<stack>-dlpod-role` | `ec2.amazonaws.com` | CloudWatch Agent only. No secrets access. |
| `<stack>-dlpod-activation-role` | `lambda.amazonaws.com` | Describe EC2 instance (get private IP); start DLPoD tethering Step Functions execution; complete termination lifecycle hook. |
| `<stack>-dlpod-sfn-role` | `states.amazonaws.com` | Invoke DLPoD tethering Lambda. |
| `<stack>-dlpod-lambda-role` | `lambda.amazonaws.com` | SSH to DLPoD instance (VPC-attached); read license key from Secrets Manager; write cert PEM to SSM; complete launch lifecycle hook. |
| `<stack>-dlpod-lifecycle-sns-role` | `autoscaling.amazonaws.com` | Publish DLPoD Auto Scaling lifecycle events to SNS. |
| `<stack>-cert-generator-role` | `lambda.amazonaws.com` | Generate self-signed cert; import to ACM; write PEM to SSM Parameter Store; update bootstrap secret. |

**Key principle:** AIG instances never hold Netskope API credentials. The Activation Lambda reads
the API token from Secrets Manager, calls the Netskope API, and receives an enrollment token in
memory — which it writes to the bootstrap secret. The AIG instance reads only the bootstrap
secret (enrollment token + DLP endpoint), not the raw API token.

> *Well-Architected [SEC03-BP01](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_permissions_define.html):
> Define access requirements and enforce least privilege.*

---

## Traffic Flows

### 1. Inbound client traffic (runtime)

```
1. Client sends HTTPS request to AIG ALB DNS name
2. AIG ALB (public subnets) → AIG instance (private subnet)
3. AIG instance inspects request:
   - Access control, rate limiting, prompt injection detection
4. AIG → dlp.aigw.internal (Route 53) → DLPoD internal ALB → DLPoD instance
   - DLPoD applies DLP policies; returns allow/block verdict
5. AIG forwards allowed requests to upstream LLM provider (via NAT Gateway)
6. LLM response returns through AIG → DLP inspection → client
```

### 2. AIG enrollment (stack creation, per instance)

```
1. ASG launches AIG instance → lifecycle hook holds it in Pending:Wait (120s)
2. SNS delivers lifecycle event → AIG Activation Lambda
3. Activation Lambda:
   a. Reads API token from <stack>-api-credentials (Secrets Manager)
   b. Calls Netskope REST API → registers appliance → receives enrollment token
   c. Writes enrollment token to <stack>-aig-bootstrap (Secrets Manager)
      (DLP cert + host already present — written at stack creation)
   d. Writes appliance ID to SSM /<stack>/appliances/<instance-id>
   e. Calls CompleteLifecycleAction: CONTINUE
4. AIG instance reads bootstrap secret at boot → self-enrolls with Netskope tenant
5. AIG configures DLP forwarding to dlp.aigw.internal
```

### 3. DLPoD tethering (stack creation, per instance)

```
1. ASG launches DLPoD instance → lifecycle hook holds it in Pending:Wait (1800s)
2. SNS delivers lifecycle event → DLPoD Activation Lambda
3. Activation Lambda → starts Step Functions execution: tether-<instance-id>
4. Step Functions orchestrates SSH automation:
   a. WaitForDlpodSSH — polls SSH every 25s (typically 5–8 min for boot)
   b. DlpodChangePassword — generates unique 24-char random password via CLI
   c. DlpodSetDNS — configures VPC DNS resolver
   d. DlpodSetLicense — reads license from Secrets Manager, applies via CLI
   e. WaitForDlpodTetheringInit — 120s fixed wait (DLPoD initiates callhome)
   f. CheckDlpodTethering — polls tethering status every 60s until complete
   g. DlpodCompleteLifecycle — CompleteLifecycleAction: CONTINUE
5. DLPoD: InService → ALB health check passes → DLP inspection active
```

### 4. DLP inspection (runtime, per request)

```
1. AIG instance resolves dlp.aigw.internal via Route 53 private zone
2. AIG → DLPoD internal ALB (HTTPS:443, self-signed cert)
3. DLPoD ALB → DLPoD instance (least-connections)
4. DLPoD scans content; returns verdict
5. AIG applies verdict (allow / block / redact)
```

### 5. AIG scale-out (runtime, automatic)

```
1. CloudWatch alarm: AIG ASG average CPU ≥ 70% for two 5-minute periods
2. Step scaling policy adds one AIG instance
3. New instance → Pending:Wait → Activation Lambda (same flow as #2)
4. New instance: InService → ALB healthy → serving requests
   (enrollment typically completes in 5–15 minutes)
```

---

## Security Design

### Network Isolation

| Resource | Ingress allowed from | Egress allowed to |
|---|---|---|
| AIG ALB security group | `0.0.0.0/0` port 443 (internet clients) | AIG instance SG port 443 |
| AIG instance security group | AIG ALB SG port 443 | All (NAT Gateway → internet, DLPoD ALB SG) |
| DLPoD ALB security group | AIG instance SG port 443 | DLPoD instance SG port 443 |
| DLPoD instance security group | DLPoD ALB SG port 443; DLPoD Lambda SG port 22 | All (NAT Gateway for tethering callhome) |
| DLPoD Lambda security group | — (Lambda, no inbound) | DLPoD instance SG port 22 + 443 |

AIG and DLPoD instances have no public IP addresses. The only path from the internet to AIG
instances is through the internet-facing ALB. DLPoD instances are reachable only from the
DLPoD ALB (DLP inspection) and the DLPoD Lambda (tethering automation).

### Secrets and Credentials

| Secret | Service | Contents | Who reads it | Lifecycle |
|---|---|---|---|---|
| `<stack>-api-credentials` | Secrets Manager | Netskope API token + tenant URL | AIG Activation Lambda only | Created at stack creation; deleted at teardown |
| `<stack>-aig-bootstrap` | Secrets Manager | Enrollment token (per instance), DLP cert, DLP host | AIG instances at boot; Activation Lambda writes | Created at stack creation; deleted at teardown |
| `<stack>-dlpod-credentials` | Secrets Manager | DLP On Demand license key | DLPoD tethering Lambda only | Created at stack creation; deleted at teardown |
| `/<stack>/dlpod-cert` | SSM Parameter Store | DLPoD ALB self-signed cert PEM | AIG Activation Lambda, cert verification | Written by cert generator custom resource |
| `/<stack>/appliances/<id>` | SSM Parameter Store | AIG appliance ID in Netskope tenant | AIG Activation Lambda (termination cleanup) | Written at launch; deleted at termination |

---

## High Availability Design

### Multi-AZ Architecture

Both the AIG ASG and DLPoD ASG span AZ1 and AZ2. Both ALBs are deployed across both AZs.
An AZ failure reduces capacity but does not interrupt service — the healthy AZ continues serving
all traffic.

```
AZ1                              AZ2
┌────────────────────────────┐   ┌────────────────────────────┐
│ Public subnet              │   │ Public subnet              │
│   AIG ALB node             │   │   AIG ALB node             │
│   NAT Gateway              │   │                            │
│                            │   │                            │
│ Private subnet             │   │ Private subnet             │
│   AIG Instance(s)          │   │   AIG Instance(s)          │
│   DLPoD Instance(s)        │   │   DLPoD Instance(s)        │
│   DLPoD ALB node           │   │   DLPoD ALB node           │
└────────────────────────────┘   └────────────────────────────┘
```

### Failure Scenarios

| Scenario | Impact | Recovery |
|---|---|---|
| Single AIG instance failure | Reduced capacity; remaining instances continue | ASG replaces automatically; new instance re-enrolls (~5–15 min) |
| Single DLPoD instance failure | Reduced DLP capacity; ALB routes to healthy instances | ASG replaces; new instance re-tethers (~15–25 min) |
| AZ failure (AIG) | Reduced capacity; other AZ continues | No action required; ASG may launch replacement in healthy AZ |
| AZ failure (DLPoD) | Reduced DLP capacity | Same as above |
| NAT Gateway failure | Instances lose outbound internet; DLP traffic unaffected (internal) | AWS SLA 99.99%; auto-recovers |
| AIG enrollment failure | Instance ABANDONED; replacement launches automatically | Check Activation Lambda logs; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| DLPoD tethering failure | Instance ABANDONED after 30 min; replacement launches | Check Step Functions execution; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |

**RPO:** Zero — both services are stateless. Configuration is stored in CloudFormation and
Netskope's management plane.

**RTO per component:**
| Scope | RTO |
|---|---|
| Single AIG instance | 5–15 minutes (auto-replaced) |
| Single DLPoD instance | 15–25 minutes (auto-replaced and re-tethered) |
| AZ failure | 0 seconds (healthy AZ continues immediately) |
| Full stack recreate | 30–45 minutes |

### Instance Sizing and Throughput

| Service | Instance type | vCPU | Memory | Notes |
|---|---|---|---|---|
| AI Gateway | `m5.4xlarge` (default) | 16 | 64 GB | Standard DLP + guardrails (CPU-based) |
| AI Gateway | `m6i.4xlarge` | 16 | 64 GB | Alternative; newer generation |
| AI Gateway | `c5.4xlarge` | 16 | 32 GB | Compute-optimized; lower memory |
| AI Gateway (GPU) | `g4dn.xlarge` | 4 | 16 GB | Advanced ML guardrails (NVIDIA T4) |
| AI Gateway (GPU) | `g5.xlarge` | 4 | 16 GB | Advanced ML guardrails (NVIDIA A10G) |
| DLP On Demand | `c5a.4xlarge` (default) | 16 | 32 GB | Baseline DLP throughput |
| DLP On Demand | `c5a.8xlarge` | 32 | 64 GB | Higher throughput |
| DLP On Demand | `c5a.16xlarge` | 64 | 128 GB | Maximum throughput |

See [AI Gateway Sizing Guidelines](https://docs.netskope.com/en/ai-gateway-sizing-guidelines/)
for request throughput guidance per instance type.

---

## Cost Estimate

Approximate monthly cost for the default deployment in us-west-1 (1 AIG + 1 DLPoD instance),
on-demand pricing. Costs vary by region and actual traffic volume.

| Resource | Quantity | Estimated monthly cost |
|---|---|---|
| EC2 — AIG (`m5.4xlarge`) | 1 instance, on-demand | ~$550 |
| EC2 — DLPoD (`c5a.4xlarge`) | 1 instance, on-demand | ~$445 |
| NAT Gateway (data processing + hours) | 1 NAT GW | ~$35–65 |
| ALB — AIG (internet-facing) | 1 ALB | ~$20–40 |
| ALB — DLPoD (internal) | 1 ALB | ~$18–30 |
| Secrets Manager | 3 secrets | ~$1.20 |
| Route 53 private hosted zone | 1 zone | ~$0.50 |
| Lambda + Step Functions | Invocations at launch/scale | <$1 |
| CloudWatch Logs | Lambda + instance logs | ~$1–5 |
| ACM certificates | 2 certs (imported) | Free |
| **Total (default, on-demand)** | | **~$1,070–$1,140/month** |

**Scaling impact:**
- Each additional AIG instance (`m5.4xlarge`): +~$550/month
- Each additional DLPoD instance (`c5a.4xlarge`): +~$445/month
- Example higher-capacity deployment (4 AIG + 2 DLPoD): ~$3,200–$3,500/month

**Advanced GPU guardrails (optional):**
- `g4dn.xlarge` per GPU instance: +~$380/month
- `g5.xlarge` per GPU instance: +~$760/month

> These are estimates for planning purposes. Use [AWS Pricing Calculator](https://calculator.aws/)
> with your actual region, instance counts, and expected traffic volumes for a precise figure.
> Reserved Instances or Savings Plans reduce EC2 costs by 30–60% for steady-state workloads.

---

## Well-Architected Alignment

| Pillar | Design decision | Implementation |
|---|---|---|
| **Security** | Least-privilege IAM — 9 dedicated roles, no shared credentials | Each role scoped to its specific function; AIG instances never hold API credentials |
| **Security** | Secrets never in user data or environment variables | API token → Secrets Manager → Lambda (memory only) → bootstrap secret → instance reads at boot |
| **Security** | No public IP on compute instances | AIG and DLPoD instances in private subnets; inbound only through ALBs |
| **Security** | Encryption in transit | All external traffic TLS; AIG→DLPoD HTTPS with ACM cert; SSH for tethering |
| **Security** | Sensitive parameters masked | `NetskopeApiToken` and `DlpodLicenseKey` are `NoEcho: true` |
| **Reliability** | Multi-AZ deployment | Both ASGs and both ALBs span AZ1 + AZ2 |
| **Reliability** | Auto-replacement on failure | Auto Scaling lifecycle hooks; failed instances replaced automatically |
| **Reliability** | Startup ordering enforced | `CertGeneratorFunction` runs before instances launch; DLP config pre-populated in bootstrap secret |
| **Reliability** | No manual enrollment steps | Activation Lambda handles full enrollment; instances self-configure from Secrets Manager |
| **Operational Excellence** | Infrastructure as code | Single CloudFormation template; all resources version-controlled |
| **Operational Excellence** | Lifecycle automation | SNS → Lambda → Step Functions handles every instance lifecycle event |
| **Cost Optimization** | Step scaling | Scale-out triggered by CPU threshold; scale in when load drops |

> *References: [AWS Well-Architected Framework](https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html),
> [Security Pillar](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/welcome.html),
> [Reliability Pillar](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html)*
