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
S3 Gateway Endpoint (both route tables) → S3 (no NAT Gateway cost)

[optional] AIG instances → Guardrails ALB (HTTP:8080, guardrails.aigw.internal) → Guardrails GPU ASG
```

---

## Component Overview

### VPC and Subnet Design

The template creates a new VPC — no pre-existing networking is required. The only network
parameter is `VpcCidr` (default `10.0.0.0/16`); the four subnets are derived from it with
`Fn::Cidr` as consecutive `/24` blocks.

| Subnet tier | AZs | Hosts | CIDR (default) |
|---|---|---|---|
| Public | AZ1 + AZ2 | AIG ALB, NAT Gateway (AZ1 only) | `10.0.0.0/24`, `10.0.1.0/24` |
| Private | AZ1 + AZ2 | AIG instances, DLPoD instances, DLPoD internal ALB, Guardrails ALB + instances (optional) | `10.0.2.0/24`, `10.0.3.0/24` |

No compute resources run in public subnets. The NAT Gateway provides outbound internet access
for private-subnet instances — enrollment, API calls to the Netskope management plane, DLPoD's
management-plane connection, and LLM provider traffic all exit through the NAT Gateway. An S3
Gateway Endpoint is attached to both the public and private route tables so S3 traffic (e.g.
Guardrails image download, template reads) stays on the AWS backbone and does not traverse the
NAT Gateway. The stack's Lambda functions are not VPC-attached and do not use the NAT Gateway.

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
| Health check | ELB (HTTPS `GET /` on 443, 200–499 accepted), 1800 s grace period |
| Bootstrap | `bootstrap.json` delivered via EC2 UserData; applied by the appliance's `nsbootstrap.service` at first boot (~5–10 minutes to healthy) |

DLP On Demand receives content from the AI Gateway over HTTPS, applies DLP policies locally
inside the VPC, and returns a verdict. Content never leaves your AWS account for DLP processing.

There is no lifecycle hook, SNS topic, or per-instance Lambda on the DLPoD tier. The launch
template UserData already contains everything the appliance needs (TLS server cert + key, license
key, DNS resolver `169.254.169.253`, `dlp-on-demand` persona), so scale-outs and replacements
need no orchestration.

> *Well-Architected [SEC09-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_protect_data_transit.html):
> Enforce encryption in transit and keep sensitive data within your network boundary.*

### Certificate Management

Both ALBs use TLS certificates managed by the stack:

| Certificate | ALB | Source | Storage |
|---|---|---|---|
| AIG ALB cert | Internet-facing | Auto-generated self-signed (default), or user-provided ACM ARN | ACM (imported by stack when auto-generated) + SSM `/<stack>/aig-cert` (PEM for clients) |
| DLPoD ALB cert | Internal | Always auto-generated: stack CA + leaf for `dlp.aigw.internal` (365-day validity) | ACM (leaf, imported) + SSM `/<stack>/dlpod-cert` (CA PEM) + Secrets Manager `<stack>-dlpod-cert-key` (CA + leaf + private key) |

The `CertGeneratorFunction` custom resource (`<stack>-certgen`, inline Lambda) runs at stack
creation before any instances launch. For DLPoD it generates a two-tier hierarchy, imports the leaf
cert to ACM for the DLPoD ALB listener, writes the CA PEM to SSM, and writes the full CA + leaf +
key to `<stack>-dlpod-cert-key`. Two consumers pick this up:

- `DlpodBootstrapBuilderFunction` (`<stack>-dlpod-bootstrap-builder`) reads the cert-key secret and
  the license key and assembles the DLPoD `bootstrap.json` UserData — so every DLPoD instance
  serves the same leaf cert on 443.
- The AIG Activation Lambda reads the CA PEM from `/<stack>/dlpod-cert` at every AIG launch and
  writes it into the bootstrap secret as `dlp.certificate` — so the first AIG instance that starts
  already trusts the DLPoD endpoint.

The same function generates the AIG ALB cert when `AcmCertificateArn` is left empty.

> **Note:** The self-signed AIG ALB certificate causes browser security warnings and requires
> clients to trust the cert or disable TLS verification. For production deployments where clients
> cannot be configured to skip certificate verification, provide an ACM-issued certificate ARN
> via `AcmCertificateArn`.

### IAM Design

Seven IAM roles (eight with the optional Guardrails tier) enforce least privilege. No role has
more access than its specific function requires.

| Role | Assumed by | Purpose |
|---|---|---|
| `<stack>-gateway-role` | `ec2.amazonaws.com` | Read `<stack>-aig-bootstrap` at boot; CloudWatch Agent. No access to API credentials. |
| `<stack>-aig-activation-role` | `lambda.amazonaws.com` | Call Netskope API to register/deregister AIG; write enrollment token + DLP (and Guardrails) block to bootstrap secret; read DLPoD CA cert from SSM; track appliance IDs in `/aig/<stack>/*`; complete lifecycle hooks on `<stack>-aig-asg`. |
| `<stack>-aig-lifecycle-sns-role` | `autoscaling.amazonaws.com` | Publish AIG Auto Scaling lifecycle events to SNS. |
| `<stack>-dlpod-role` | `ec2.amazonaws.com` | CloudWatch Agent only. No secrets or SSM access. |
| `<stack>-dlpod-bootstrap-builder-role` | `lambda.amazonaws.com` | Read `<stack>-dlpod-cert-key` and `<stack>-dlpod-credentials` to assemble `bootstrap.json` UserData (stack create/update only). |
| `<stack>-dlpod-readiness-role` | `lambda.amazonaws.com` | `elasticloadbalancing:DescribeTargetHealth` — readiness gate that blocks AIG launch until DLPoD (and Guardrails) targets are healthy. |
| `<stack>-certgen-role` | `lambda.amazonaws.com` | Generate cert hierarchy; import leaf to ACM; write PEMs to SSM `/<stack>/*`; write `<stack>-dlpod-cert-key`. |
| `<stack>-guardrails-role` *(Guardrails only)* | `ec2.amazonaws.com` | `s3:GetObject` on the `aisecurity-llm.tgz` image tarball; SSM Session Manager; CloudWatch Agent. |

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

### 2. AIG enrollment (stack creation and every scale-out, per instance)

```
1. ASG launches AIG instance → lifecycle hook holds it in Pending:Wait (120s)
2. SNS delivers lifecycle event → AIG Activation Lambda (<stack>-aig-activation)
3. Activation Lambda:
   a. Reads API token from <stack>-netskope-credentials (Secrets Manager)
   b. Calls Netskope REST API → registers appliance → receives enrollment token
   c. Writes appliance ID to SSM /aig/<stack>/<instance-id>
   d. Reads the DLPoD CA cert PEM from SSM /<stack>/dlpod-cert
   e. Writes <stack>-aig-bootstrap (Secrets Manager): enrollment token + dlp {host, certificate}
      (+ ai_guardrails {host} when the Guardrails tier is deployed)
   f. Calls CompleteLifecycleAction: CONTINUE (any exception → ABANDON, instance replaced)
4. AIG instance reads bootstrap secret at boot → self-enrolls with Netskope tenant
5. AIG configures DLP forwarding to https://dlp.aigw.internal
```

### 3. DLPoD bootstrap (stack creation and every scale-out, per instance)

Two custom resources run once, before the DLPoD ASG exists:

```
Stack creation (pre-launch)
1. DlpodAlbCertificate → CertGeneratorFunction (<stack>-certgen):
   generates CA + leaf for dlp.aigw.internal, imports leaf to ACM, writes CA PEM to
   SSM /<stack>/dlpod-cert, writes CA + leaf + key to <stack>-dlpod-cert-key
2. DlpodBootstrapPart1 / Part2 → DlpodBootstrapBuilderFunction (<stack>-dlpod-bootstrap-builder):
   reads <stack>-dlpod-cert-key and <stack>-dlpod-credentials (license key), assembles
   bootstrap.json {dlpaas: cert/key/CA chain, dns: 169.254.169.253, system: licensekey,
   persona: dlp-on-demand}, base64-encodes it in two halves (4 KB custom-resource limit)
   → joined as DlpodLaunchTemplate UserData
```

Then, for each DLPoD instance — no lifecycle hook, SNS, or Lambda involved:

```
Instance launch
1. ASG launches DLPoD instance from DlpodLaunchTemplate → InService immediately
   (HealthCheckType: ELB, HealthCheckGracePeriod: 1800s)
2. nsbootstrap.service reads bootstrap.json from EC2 UserData at first boot:
   installs TLS server cert + key + CA chain, sets DNS resolver, applies license key,
   sets the dlp-on-demand persona; appliance connects outbound to the Netskope
   management plane via the NAT Gateway
3. DLPoD listens on HTTPS:443 → ALB health check (GET /, 200–499, 2 passes at 30 s) → healthy
   (typically 5–10 minutes after launch)
```

**Readiness gate (stack creation only):** `GatewayAutoScalingGroup` has
`DependsOn: DlpodReadinessGate`, a custom resource backed by the inline Lambda
`<stack>-dlpod-readiness` that polls `describe-target-health` on the DLPoD target group every
30 s. It succeeds when all targets are healthy and fails (rolling the stack back) if that has not
happened within 14 minutes. AIG validates the DLPoD HTTPS endpoint during enrollment, so this
guarantees DLPoD is serving before the first AIG instance boots. When Guardrails is deployed, a
second gate (`GuardrailsReadinessGate`, same Lambda) does the same for the Guardrails target group.
The gate is a no-op on stack update/delete and does not apply to later scale-outs.

### 4. DLP inspection (runtime, per request)

```
1. AIG instance resolves dlp.aigw.internal via Route 53 private zone
2. AIG → DLPoD internal ALB (HTTPS:443, stack-generated leaf cert; AIG trusts the stack CA)
3. DLPoD ALB → DLPoD instance (HTTPS:443, same leaf cert served by the appliance)
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
| AIG ALB security group (`<stack>-aig-alb-sg`) | `0.0.0.0/0` port 443 (internet clients) | VPC CIDR port 443 (AIG instances) |
| AIG instance security group (`<stack>-aig-gw-sg`) | AIG ALB SG port 443 | All (NAT Gateway → internet, DLPoD ALB, Guardrails ALB) |
| DLPoD ALB security group (`<stack>-dlpod-alb-sg`) | AIG instance SG port 443 | VPC CIDR port 443 (DLPoD instances) |
| DLPoD instance security group (`<stack>-dlpod-sg`) | DLPoD ALB SG port 443 only — no SSH ingress | All (NAT Gateway → Netskope management plane) |
| Guardrails ALB security group *(optional)* | AIG instance SG port `GuardrailsContainerPort` (8080) | VPC CIDR port 8080 |
| Guardrails instance security group *(optional)* | Guardrails ALB SG port 8080 | All (NAT Gateway → S3) |

AIG and DLPoD instances have no public IP addresses. The only path from the internet to AIG
instances is through the internet-facing ALB. DLPoD instances are reachable only from the
DLPoD ALB on port 443 — no Lambda, bastion, or SSH path exists, and none of the stack's Lambda
functions is VPC-attached. All launch templates require IMDSv2 and set `Encrypted: true` on the
root volume.

### Secrets and Credentials

| Secret | Service | Contents | Who reads it | Lifecycle |
|---|---|---|---|---|
| `<stack>-netskope-credentials` | Secrets Manager | Netskope API token + tenant URL | AIG Activation Lambda only | Created at stack creation; deleted at teardown |
| `<stack>-aig-bootstrap` | Secrets Manager | Enrollment token (per instance), DLP CA cert, DLP host, Guardrails host (optional) | AIG instances at boot; Activation Lambda writes | Created at stack creation; deleted at teardown |
| `<stack>-dlpod-credentials` | Secrets Manager | DLP On Demand license key | DLPoD bootstrap builder Lambda only (stack create/update) | Created at stack creation; deleted at teardown |
| `<stack>-dlpod-cert-key` | Secrets Manager | DLPoD CA cert, leaf cert, leaf private key (PEM) | DLPoD bootstrap builder Lambda only; cert generator writes | Created at stack creation; deleted at teardown |
| `/<stack>/dlpod-cert` | SSM Parameter Store | DLPoD CA cert PEM (365-day validity) | AIG Activation Lambda at every AIG launch | Written by cert generator custom resource |
| `/<stack>/aig-cert` | SSM Parameter Store | AIG ALB self-signed cert PEM (only when `AcmCertificateArn` is empty) | Operators, to distribute to clients | Written by cert generator custom resource |
| `/aig/<stack>/<instance-id>` | SSM Parameter Store | AIG appliance ID in Netskope tenant | AIG Activation Lambda (termination cleanup) | Written at launch; deleted at termination |

DLPoD instances never read Secrets Manager or SSM. Their TLS key and license key are embedded in
the launch template UserData (`bootstrap.json`), which is readable by principals with
`ec2:DescribeLaunchTemplateVersions` — see [SECURITY.md](SECURITY.md#known-limitations-and-accepted-risks).

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
| Single DLPoD instance failure | Reduced DLP capacity; ALB routes to healthy instances | ASG replaces; new instance self-configures from UserData via `nsbootstrap` (~5–10 min) |
| AZ failure (AIG) | Reduced capacity; other AZ continues | No action required; ASG may launch replacement in healthy AZ |
| AZ failure (DLPoD) | Reduced DLP capacity | Same as above |
| NAT Gateway failure | Instances lose outbound internet; DLP traffic unaffected (internal) | AWS SLA 99.99%; auto-recovers |
| AIG enrollment failure | Instance ABANDONED by the launch hook; replacement launches automatically | Check Activation Lambda logs; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| DLPoD bootstrap failure | Target never healthy; ASG marks the instance unhealthy after the 30-min grace period and replaces it (no lifecycle hook, so no ABANDON state) | Check DLPoD target health and `/aws/lambda/<stack>-dlpod-bootstrap-builder`; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#dlp-on-demand-issues) |
| DLPoD not healthy within 14 min at stack creation | `DlpodReadinessGate` fails; stack rolls back before any AIG instance launches | Re-create with `--disable-rollback` to inspect; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |

**RPO:** Zero — both services are stateless. Configuration is stored in CloudFormation and
Netskope's management plane.

**RTO per component:**
| Scope | RTO |
|---|---|
| Single AIG instance | 5–15 minutes (auto-replaced and re-enrolled) |
| Single DLPoD instance | 5–10 minutes (auto-replaced; `nsbootstrap` applies UserData at first boot) |
| AZ failure | 0 seconds (healthy AZ continues immediately) |
| Full stack recreate | 12–18 minutes to `CREATE_COMPLETE` (DLPoD ~5–10 min, then AIG ~5–15 min); longer with Guardrails |

### Instance Sizing and Throughput

| Service | Instance type | vCPU | Memory | Notes |
|---|---|---|---|---|
| AI Gateway | `m5.4xlarge` (default) | 16 | 64 GB | Standard DLP + guardrails (CPU-based) |
| AI Gateway | `m6i.4xlarge` | 16 | 64 GB | Alternative; newer generation |
| AI Gateway | `c5.4xlarge` | 16 | 32 GB | Compute-optimized; lower memory |
| AI Guardrails (optional) | `g4dn.xlarge` (default) | 4 | 16 GB | `aisecurityllm` container tier (NVIDIA T4); `GuardrailsInstanceType` |
| AI Guardrails (optional) | `g5.xlarge` | 4 | 16 GB | `aisecurityllm` container tier (NVIDIA A10G) |
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
| Secrets Manager | 4 secrets | ~$1.60 |
| SSM Parameter Store | Standard parameters | Free |
| Route 53 private hosted zone | 1 zone | ~$0.50 |
| Lambda (4 inline functions) | Custom resources at create/update + AIG lifecycle events | <$1 |
| CloudWatch Logs | Lambda + instance logs | ~$1–5 |
| ACM certificates | 2 certs (imported) | Free |
| S3 Gateway Endpoint | 1 endpoint | Free |
| **Total (default, on-demand)** | | **~$1,070–$1,140/month** |

**Scaling impact:**
- Each additional AIG instance (`m5.4xlarge`): +~$550/month
- Each additional DLPoD instance (`c5a.4xlarge`): +~$445/month
- Example higher-capacity deployment (4 AIG + 2 DLPoD): ~$3,200–$3,500/month

**Advanced GPU guardrails (optional, `GuardrailsImageS3Bucket` set):**
- `g4dn.xlarge` per GPU instance: +~$380/month
- `g5.xlarge` per GPU instance: +~$760/month
- Guardrails internal ALB: +~$18–30/month
- S3 storage for the `aisecurity-llm.tgz` tarball: ~$0.023/GB-month

When enabled, the Guardrails tier adds an internal HTTP ALB (`guardrails.aigw.internal`, port 8080)
in the private subnets and a GPU Auto Scaling Group (1–4 instances) running the container. AIG reaches
it via the `ai_guardrails.host` entry in its bootstrap secret; the AIG ASG does not launch until the
Guardrails ALB reports all targets healthy, mirroring the DLPoD readiness gate.

> These are estimates for planning purposes. Use [AWS Pricing Calculator](https://calculator.aws/)
> with your actual region, instance counts, and expected traffic volumes for a precise figure.
> Reserved Instances or Savings Plans reduce EC2 costs by 30–60% for steady-state workloads.

---

## Well-Architected Alignment

| Pillar | Design decision | Implementation |
|---|---|---|
| **Security** | Least-privilege IAM — 7 dedicated roles (8 with Guardrails), no shared credentials | Each role scoped to its specific function; AIG instances never hold API credentials; DLPoD instances hold no secrets access at all |
| **Security** | API credentials never in user data or environment variables | API token → Secrets Manager → Lambda (memory only) → bootstrap secret → AIG instance reads at boot |
| **Security** | No public IP on compute instances; no SSH | AIG and DLPoD instances in private subnets; inbound only through ALBs; no security group opens port 22 |
| **Security** | Encryption in transit and at rest | All external traffic TLS; AIG→DLPoD HTTPS with a stack-generated CA/leaf; `Encrypted: true` on all EBS root volumes; IMDSv2 required |
| **Security** | Sensitive parameters masked | `NetskopeApiToken` and `DlpodLicenseKey` are `NoEcho: true` |
| **Reliability** | Multi-AZ deployment | Both ASGs and both ALBs span AZ1 + AZ2 |
| **Reliability** | Auto-replacement on failure | ELB health checks on both ASGs (AIG also has a launch lifecycle hook); failed instances replaced automatically |
| **Reliability** | Startup ordering enforced | `CertGeneratorFunction` and bootstrap builder run before instances launch; `DlpodReadinessGate` blocks the AIG ASG until DLPoD targets are healthy |
| **Reliability** | No manual enrollment steps | Activation Lambda handles AIG enrollment; DLPoD self-configures from `bootstrap.json` via `nsbootstrap.service` |
| **Operational Excellence** | Infrastructure as code | Single CloudFormation template, all Lambda code inline (`ZipFile`); all resources version-controlled |
| **Operational Excellence** | Lifecycle automation | ASG hook → SNS → Lambda handles every AIG lifecycle event; DLPoD needs none — its launch template UserData is complete |
| **Cost Optimization** | Step scaling | Scale-out triggered by CPU threshold; scale in when load drops |
| **Cost Optimization** | S3 Gateway Endpoint | S3 traffic (Guardrails image download, template reads) stays on the AWS backbone — no NAT Gateway data-processing charges |

> *References: [AWS Well-Architected Framework](https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html),
> [Security Pillar](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/welcome.html),
> [Reliability Pillar](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html)*
