# Security Reference — AI Gateway + DLP On Demand

Security posture reference for `templates/gateway-combined.yaml`. Intended for InfoSec reviewers,
security architects, and compliance teams evaluating this deployment.

## Table of Contents

- [Security Design Principles](#security-design-principles)
- [Network Security](#network-security)
- [IAM Roles and Permissions](#iam-roles-and-permissions)
- [Secret and Credential Management](#secret-and-credential-management)
- [Encryption](#encryption)
- [CloudFormation Security Practices](#cloudformation-security-practices)
- [Audit and Visibility](#audit-and-visibility)
- [Known Limitations and Accepted Risks](#known-limitations-and-accepted-risks)

---

## Security Design Principles

1. **Least privilege IAM** — Seven dedicated IAM roles (eight when the optional Guardrails tier is
   deployed); each has exactly the permissions its function requires. Secrets Manager, SSM, and
   Auto Scaling actions are scoped to specific resource ARNs.

2. **Secrets never touch instances directly** — The Netskope API token flows from Secrets Manager
   to the AIG Activation Lambda only (in memory). The Lambda exchanges the token for a short-lived
   enrollment token, which it writes to the bootstrap secret. AIG instances read the bootstrap
   secret at boot — they never have access to the raw API token. DLPoD instances read no secret
   at all: their configuration is assembled once at stack creation by an inline Lambda and
   delivered as launch template UserData.

3. **No public IP on compute** — All EC2 instances run in private subnets. Inbound access is
   exclusively through the ALBs. There is no SSH inbound path to any instance — from the internet
   or from inside the VPC. No security group in the stack opens port 22.

4. **All sensitive parameters are `NoEcho`** — `NetskopeApiToken` and `DlpodLicenseKey` are
   never shown in CloudFormation events, stack outputs, or the console after submission.

5. **No orchestration or remote-login automation** — DLPoD configures itself from `bootstrap.json`
   via its own `nsbootstrap.service` at first boot. There is no orchestration service, no
   remote-login automation, and no lifecycle hook on the DLPoD Auto Scaling Group.

---

## Network Security

### Security Group Rules

**AIG ALB Security Group** (`<stack>-aig-alb-sg`)

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | `0.0.0.0/0` | HTTPS from internet clients |
| Outbound | TCP | 443 | VPC CIDR | Forward to AIG instances |

**AIG Instance Security Group** (`<stack>-aig-gw-sg`)

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | AIG ALB SG | HTTPS from ALB only |
| Outbound | All | All | `0.0.0.0/0` | Outbound via NAT GW (Netskope API, LLM providers, DLPoD ALB, Guardrails ALB) |

**DLPoD ALB Security Group** (`<stack>-dlpod-alb-sg`)

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | AIG Instance SG | HTTPS from AIG instances only (`AigToDlpodAlbIngress`) |
| Outbound | TCP | 443 | VPC CIDR | Forward to DLPoD instances |

**DLPoD Instance Security Group** (`<stack>-dlpod-sg`)

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | DLPoD ALB SG | HTTPS for DLP inspection (only ingress rule — no SSH) |
| Outbound | All | All | `0.0.0.0/0` | Netskope management plane connection via NAT GW |

There is no Lambda security group: none of the stack's four Lambda functions is VPC-attached.
They call AWS APIs (ACM, SSM, Secrets Manager, ELBv2, Auto Scaling, EC2) and the Netskope REST
API over the public Lambda network path only.

**Guardrails ALB Security Group** (`<stack>-guardrails-alb-sg`) — *only when Guardrails is deployed*

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | `GuardrailsContainerPort` (8080) | AIG Instance SG | HTTP inference requests from AIG instances only |
| Outbound | TCP | `GuardrailsContainerPort` (8080) | VPC CIDR | Forward to Guardrails instances |

**Guardrails Instance Security Group** (`<stack>-guardrails-sg`) — *only when Guardrails is deployed*

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | `GuardrailsContainerPort` (8080) | Guardrails ALB SG | HTTP from ALB only |
| Outbound | All | All | `0.0.0.0/0` | S3 image download via NAT GW |

### Network Isolation

- **No direct internet inbound to instances.** The internet-facing AIG ALB is the only inbound
  internet path. It terminates TLS and forwards to AIG instances in private subnets.
- **DLPoD is fully internal.** The DLPoD ALB is internal-only (private subnets). DLPoD instances
  are reachable only from the DLPoD ALB on port 443 — nothing else in the stack can connect to
  them.
- **Outbound internet via NAT Gateway only.** All instance outbound internet traffic traverses
  the single NAT Gateway. There is no direct internet gateway route to private subnets. An S3
  Gateway Endpoint routes S3 traffic via the AWS backbone (no NAT Gateway traversal).
- **AIG → DLPoD via private DNS.** AIG instances resolve `dlp.aigw.internal` via a Route 53
  private hosted zone — this alias always resolves to the DLPoD internal ALB, never to a public
  address. DLPoD instances use the Route 53 Resolver (`169.254.169.253`) set in `bootstrap.json`.
- **IMDSv2 enforced.** All three launch templates set `HttpTokens: required`, so instance
  credentials cannot be retrieved with unauthenticated IMDSv1 requests.

---

## IAM Roles and Permissions

### Role Summary

| Role | Assumed by | Purpose |
|---|---|---|
| `<stack>-gateway-role` | `ec2.amazonaws.com` | AIG instance profile |
| `<stack>-aig-activation-role` | `lambda.amazonaws.com` | AIG lifecycle management (enroll / deregister) |
| `<stack>-aig-lifecycle-sns-role` | `autoscaling.amazonaws.com` | AIG lifecycle event delivery to SNS |
| `<stack>-dlpod-role` | `ec2.amazonaws.com` | DLPoD instance profile (CloudWatch Agent only) |
| `<stack>-dlpod-bootstrap-builder-role` | `lambda.amazonaws.com` | Assemble DLPoD `bootstrap.json` UserData at stack create/update |
| `<stack>-dlpod-readiness-role` | `lambda.amazonaws.com` | Readiness gate — poll ALB target health before AIG launches |
| `<stack>-certgen-role` | `lambda.amazonaws.com` | Self-signed cert generation (DLPoD ALB, and AIG ALB when auto-generated) |
| `<stack>-guardrails-role` *(Guardrails only)* | `ec2.amazonaws.com` | Guardrails instance profile (S3 image download, SSM Session Manager, CloudWatch) |

### Key Principle: AIG Instances Never Hold API Credentials

AIG instances have an IAM role (`<stack>-gateway-role`) that allows only:
1. `secretsmanager:GetSecretValue` on the specific bootstrap secret ARN
2. The AWS-managed `CloudWatchAgentServerPolicy` (metrics and logs)

The Netskope API token is in a separate secret (`<stack>-netskope-credentials`) that the AIG
instance role has **no access to**. Only the Activation Lambda reads it.

DLPoD instances (`<stack>-dlpod-role`) have **no** Secrets Manager or SSM permissions at all.

### Role Permissions Detail

**`<stack>-gateway-role` (AIG instance profile)**
- `secretsmanager:GetSecretValue` — `<stack>-aig-bootstrap` only (ARN-scoped)
- `CloudWatchAgentServerPolicy` (AWS managed) — CloudWatch metrics and logs

**`<stack>-aig-activation-role` (AIG Activation Lambda)**
- `secretsmanager:GetSecretValue` — `<stack>-netskope-credentials` (reads API token)
- `secretsmanager:PutSecretValue` — `<stack>-aig-bootstrap` (writes enrollment token + DLP / Guardrails block)
- `ssm:GetParameter` — `/<stack>/dlpod-cert` (reads DLPoD CA cert for the bootstrap secret)
- `ssm:PutParameter`, `ssm:GetParameter`, `ssm:DeleteParameter` — `/aig/<stack>/*` (appliance ID tracking)
- `ec2:DescribeInstances` — describe the launching instance to get its private IP (`Resource: "*"`; EC2 describe calls cannot be ARN-scoped)
- `autoscaling:CompleteLifecycleAction` — `<stack>-aig-asg` only
- `logs:CreateLogStream`, `logs:PutLogEvents` — its own log group only

**`<stack>-aig-lifecycle-sns-role` (Auto Scaling → SNS)**
- `sns:Publish` — `<stack>-aig-lifecycle` topic only

**`<stack>-dlpod-role` (DLPoD instance profile)**
- `CloudWatchAgentServerPolicy` (AWS managed) — nothing else

**`<stack>-dlpod-bootstrap-builder-role` (DLPoD bootstrap builder custom resource Lambda)**
- `secretsmanager:GetSecretValue` — `<stack>-dlpod-cert-key` (leaf cert, leaf key, CA cert)
- `secretsmanager:GetSecretValue` — `<stack>-dlpod-credentials` (license key)
- `logs:CreateLogStream`, `logs:PutLogEvents` — its own log group only

**`<stack>-dlpod-readiness-role` (readiness gate custom resource Lambda)**
- `elasticloadbalancing:DescribeTargetHealth` — `Resource: "*"` (read-only; used for the DLPoD and Guardrails target groups)
- `logs:CreateLogStream`, `logs:PutLogEvents` — its own log group only

**`<stack>-certgen-role` (cert generator custom resource Lambda)**
- `acm:ImportCertificate`, `acm:DeleteCertificate`, `acm:AddTagsToCertificate` — `Resource: "*"` (ACM import has no pre-existing ARN to scope to)
- `ssm:PutParameter` — `/<stack>/*` (`/<stack>/dlpod-cert`, `/<stack>/aig-cert`)
- `secretsmanager:PutSecretValue` — `<stack>-dlpod-cert-key`
- `logs:CreateLogStream`, `logs:PutLogEvents` — its own log group only

**`<stack>-guardrails-role` (Guardrails instance profile — only when deployed)**
- `s3:GetObject` — the single object `arn:aws:s3:::<GuardrailsImageS3Bucket>/<GuardrailsImageS3Key>`
- `s3:ListBucket` — the bucket, with `s3:prefix` limited to the image key
- `AmazonSSMManagedInstanceCore` (AWS managed) — Session Manager access for container diagnostics
- `CloudWatchAgentServerPolicy` (AWS managed)

---

## Secret and Credential Management

### What's Stored and Where

| Secret name | Type | Contents | Who writes | Who reads |
|---|---|---|---|---|
| `<stack>-netskope-credentials` | Secrets Manager | `{"tenant_url": "...", "api_token": "..."}` | CloudFormation (from `NoEcho` parameter) | AIG Activation Lambda only |
| `<stack>-aig-bootstrap` | Secrets Manager | `{"bootstrap": true, "enrollment_token": "...", "dlp": {"certificate": "<CA PEM>", "host": "https://dlp.aigw.internal"}}` plus `"ai_guardrails": {"host": "..."}` when Guardrails is deployed | AIG Activation Lambda (full overwrite at every AIG launch) | AIG instances at boot |
| `<stack>-dlpod-credentials` | Secrets Manager | `{"license_key": "..."}` | CloudFormation (from `NoEcho` parameter) | DLPoD bootstrap builder Lambda only (stack create/update) |
| `<stack>-dlpod-cert-key` | Secrets Manager | `{"ca_cert_pem": "...", "leaf_cert_pem": "...", "leaf_key_pem": "..."}` — DLPoD TLS hierarchy incl. leaf private key | Cert generator Lambda | DLPoD bootstrap builder Lambda only |
| `/<stack>/dlpod-cert` | SSM Parameter (`String`) | PEM-encoded DLPoD CA certificate (public material, 365-day validity) | Cert generator Lambda | AIG Activation Lambda at every AIG launch |
| `/<stack>/aig-cert` | SSM Parameter (`String`) | PEM-encoded AIG ALB self-signed cert — only when `AcmCertificateArn` is empty | Cert generator Lambda | Operators (to distribute the cert to clients) |
| `/aig/<stack>/<instance-id>` | SSM Parameter (`String`) | AIG appliance ID in the Netskope tenant | AIG Activation Lambda at launch | AIG Activation Lambda at termination |

### Credential Flow

**AIG (per instance launch):**
```
1. User provides API token as NoEcho CloudFormation parameter
2. CloudFormation creates <stack>-netskope-credentials in Secrets Manager
3. ASG launches AIG instance → lifecycle hook → Activation Lambda fires
4. Activation Lambda reads API token from Secrets Manager (encrypted in transit, AWS SDK TLS)
5. Activation Lambda calls Netskope REST API → receives enrollment token (exists in Lambda memory only)
6. Activation Lambda reads the DLPoD CA cert from SSM and writes enrollment token + DLP block
   to <stack>-aig-bootstrap (separate secret)
7. AIG instance reads bootstrap secret at boot over HTTPS → self-enrolls
8. Enrollment token is consumed; it is not persisted beyond the bootstrap secret write
```

The API token (`<stack>-netskope-credentials`) and the enrollment token (`<stack>-aig-bootstrap`)
are in separate secrets. Compromise of the bootstrap secret does not expose the API token.

**DLPoD (once, at stack creation or update):**
```
1. User provides license key as NoEcho CloudFormation parameter
2. CloudFormation creates <stack>-dlpod-credentials in Secrets Manager
3. CertGeneratorFunction (<stack>-certgen) generates a CA + leaf cert for dlp.aigw.internal,
   imports the leaf to ACM, writes the CA PEM to SSM /<stack>/dlpod-cert and the full
   CA + leaf + private key to <stack>-dlpod-cert-key
4. DlpodBootstrapBuilderFunction (<stack>-dlpod-bootstrap-builder) reads both secrets and
   assembles bootstrap.json (TLS cert + key, license key, DNS 169.254.169.253, persona)
5. The base64-encoded bootstrap.json becomes the DlpodLaunchTemplate UserData
6. Every DLPoD instance's nsbootstrap.service applies bootstrap.json at first boot —
   the instance never calls Secrets Manager or SSM
```

Because the license key and the DLPoD leaf private key are embedded in the launch template
UserData, anyone with `ec2:DescribeLaunchTemplateVersions` on the account (or code running on the
instance, via IMDSv2) can read them. See Known Limitations.

---

## Encryption

### In Transit

| Path | Protocol | Notes |
|---|---|---|
| Internet → AIG ALB | TLS (ALB default security policy) | Certificate from ACM (auto-generated self-signed or user-provided). No `SslPolicy` is set on the listener; add one to enforce TLS 1.2+ only |
| AIG ALB → AIG instances | TLS (HTTPS:443) | ALB health checks and traffic forwarding |
| AIG instances → DLPoD ALB | TLS (HTTPS:443) | Stack-generated leaf cert signed by a stack-generated CA; AIG trusts the CA via `dlp.certificate` in the bootstrap secret |
| DLPoD ALB → DLPoD instances | TLS (HTTPS:443) | DLPoD serves the same leaf cert + key delivered in `bootstrap.json` |
| DLPoD instances → Netskope management plane | TLS (HTTPS:443) | Outbound via NAT Gateway after licensing |
| AIG instances → Guardrails ALB → Guardrails instances *(if deployed)* | HTTP (`GuardrailsContainerPort`) | Plain HTTP inside private subnets — see Known Limitations |
| Lambda → Secrets Manager / SSM / ACM / ELBv2 | TLS (AWS SDK) | All AWS SDK calls use TLS |
| Lambda → Netskope REST API | TLS (HTTPS:443) | AIG enrollment and deregistration |

No component of the stack uses SSH. Guardrails instances are reachable for diagnostics only via
SSM Session Manager (`AmazonSSMManagedInstanceCore`), which is IAM-authenticated and logged in
CloudTrail; AIG and DLPoD instance roles do not include Session Manager.

### At Rest

| Resource | Encryption |
|---|---|
| Secrets Manager secrets | AES-256, AWS-managed KMS key (`aws/secretsmanager`) |
| SSM Parameter Store parameters | `String` type — hold only public certificate PEMs and appliance IDs; no private keys or credentials are stored in SSM |
| EBS root volumes (AIG, DLPoD, Guardrails) | `Encrypted: true` set explicitly in all three launch templates (AWS-managed EBS key unless the account default key is customer-managed) |
| CloudWatch Logs | Encrypted at rest by default (AWS-managed) |
| Launch template UserData (DLPoD) | Stored by EC2; not separately encrypted — contains the DLPoD TLS key and license key (see Known Limitations) |

---

## CloudFormation Security Practices

✅ **`NoEcho: true`** on all sensitive parameters (`NetskopeApiToken`, `DlpodLicenseKey`) — values
are never shown in CloudFormation events, stack output, or the console.

✅ **Resource-scoped IAM policies** — Secrets Manager, SSM, SNS, and Auto Scaling access is scoped
to specific resource ARNs constructed with `!Ref` / `!Sub`. The only `Resource: "*"` grants are on
actions that cannot be ARN-scoped (`ec2:DescribeInstances`, `elasticloadbalancing:DescribeTargetHealth`,
`acm:ImportCertificate`).

✅ **No secrets in AIG user data** — AIG instance user data contains only the bootstrap secret
*name* (`{"bootstrap_secret": "<stack>-aig-bootstrap"}`). The instance reads the value from
Secrets Manager at boot using its IAM role.

✅ **No secrets in Lambda environment variables** — The Activation Lambda's environment holds only
ARNs, parameter names, and host names. Credentials are retrieved at runtime from Secrets Manager
using the execution role. The other three Lambdas have no environment variables at all.

✅ **All Lambda code is inline** — Every function uses `ZipFile` code embedded in the template. No
external Lambda artifacts, layers, or S3 code buckets are fetched at deploy time, so the reviewed
template is the complete supply chain. (An S3 bucket is still used to *host the template itself*
because it exceeds the 51 KB `--template-body` limit.)

✅ **Separate secrets for separate purposes** — API credentials (`<stack>-netskope-credentials`),
bootstrap data (`<stack>-aig-bootstrap`), license key (`<stack>-dlpod-credentials`), and the
DLPoD TLS key material (`<stack>-dlpod-cert-key`) are in separate Secrets Manager secrets with
separate, role-specific access.

✅ **IMDSv2 required** — All launch templates set `MetadataOptions.HttpTokens: required`.

✅ **Conditions prevent unnecessary resource creation** — `UseAutoGeneratedAigCert` skips the AIG
cert generation when a user-provided ACM ARN is given; `DeployGuardrails` creates the GPU tier
(role, SGs, ALB, ASG) only when `GuardrailsImageS3Bucket` is set, and a template `Rules` assertion
requires `GuardrailsAmiId` alongside it.

⚠️ **AIG ALB uses a self-signed certificate by default** — The auto-generated cert has
`aig.aigw.internal` as its CN/SAN. API clients must be configured to trust it or skip TLS
verification. For production deployments with external clients, provide a trusted ACM certificate
via `AcmCertificateArn`. See [DEPLOYMENT.md — ACM Certificate](DEPLOYMENT.md#4-acm-certificate-optional).

⚠️ **DLPoD configuration travels in UserData** — The DLPoD `bootstrap.json` (TLS private key and
license key) is base64-encoded, not encrypted, in the launch template. Restrict
`ec2:DescribeLaunchTemplateVersions` and `ec2:DescribeInstanceAttribute` (userData) to operators.

---

## Audit and Visibility

### CloudWatch Log Groups

| Log group | Contents | Retention |
|---|---|---|
| `/aws/lambda/<stack>-aig-activation` | AIG registration/deregistration with Netskope API, lifecycle hook events | 30 days |
| `/aws/lambda/<stack>-certgen` | Cert hierarchy generation, ACM import, SSM and Secrets Manager writes (stack create/delete only) | 30 days |
| `/aws/lambda/<stack>-dlpod-bootstrap-builder` | `bootstrap.json` assembly — logs sizes only, never contents (stack create/update only) | 30 days |
| `/aws/lambda/<stack>-dlpod-readiness` | Readiness gate polls (`N/M target(s) healthy`) for DLPoD and, if deployed, Guardrails (stack create only) | 7 days |

DLPoD appliances do not write CloudWatch logs from the stack's perspective; `nsbootstrap.service`
status is observable through DLPoD ALB target health or on the appliance per the Netskope DLP On
Demand documentation.

### Netskope Audit Log

All AI Gateway activity — requests, responses, blocked events, policy decisions — is logged to
the Netskope management plane. Access logs from the Netskope UI under
**Analytics → SkopeIT → AI Gateway** or via the Netskope Events API.

### What to Monitor

- Lambda function errors → CloudWatch Metrics → filter on `Errors` for each function
- AIG enrollment failures → check `/aws/lambda/<stack>-aig-activation` for `Traceback` / `ABANDON` lines
- DLPoD bootstrap failures → DLPoD ALB target health (`aws elbv2 describe-target-health`); a target
  that never becomes healthy within the 30-minute ASG grace period indicates `nsbootstrap` did not
  complete (bad license key, no outbound path) — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#dlp-on-demand-issues)
- Stack rollback at `DlpodReadinessGate` / `GuardrailsReadinessGate` → `describe-stack-events` and
  `/aws/lambda/<stack>-dlpod-readiness`
- ALB target health → `aws elbv2 describe-target-health` — unhealthy targets indicate enrollment/bootstrap problems
- CloudTrail → `secretsmanager:GetSecretValue` on `<stack>-netskope-credentials` from any principal
  other than `<stack>-aig-activation-role`; `ec2:DescribeLaunchTemplateVersions` on `<stack>-dlpod-lt`

---

## Known Limitations and Accepted Risks

| Item | Detail | Mitigation |
|---|---|---|
| DLPoD TLS private key and license key are in launch template UserData | `bootstrap.json` (leaf cert + private key for `dlp.aigw.internal`, DLPoD license key) is base64-encoded into `<stack>-dlpod-lt` UserData so `nsbootstrap.service` can apply it at first boot with no credentials on the instance. UserData is readable by any principal with `ec2:DescribeLaunchTemplateVersions` / `ec2:DescribeInstanceAttribute`, and by code on the instance via IMDSv2. | The key is stack-generated, valid 365 days, and only ever trusted by this stack's AIG instances for `dlp.aigw.internal` (an internal-only ALB). Restrict launch-template read permissions to operators; a stack update that re-runs `DlpodAlbCertificate` regenerates the hierarchy. |
| AIG → Guardrails traffic is plain HTTP | The AIG bootstrap `ai_guardrails` block carries a host only (no certificate field), so the Guardrails internal ALB listens on HTTP. Prompts and responses under inspection traverse this hop unencrypted. | Path is confined to private subnets and restricted by security group to AIG instances → Guardrails ALB → Guardrails instances; no internet or cross-VPC exposure. If a future AIG build accepts `ai_guardrails.certificate`, switch the listener to HTTPS using the existing `Custom::AlbCertificate` pattern. |
| Auto-generated AIG ALB cert is self-signed | CN/SAN is `aig.aigw.internal`, which does not resolve publicly. Clients must trust the cert or skip TLS verification. | Provide a valid ACM certificate via `AcmCertificateArn` for production deployments where clients require trusted TLS. |
| Shared AIG bootstrap secret | Every AIG launch overwrites `<stack>-aig-bootstrap` with that instance's enrollment token; two AIG instances launching concurrently can read each other's token. | Scale AIG one instance at a time (the CPU scale-out policy adds +1 per alarm). The DLP / Guardrails blocks are identical for all instances and unaffected. |
| Stack-generated certificates expire after 365 days | The DLPoD CA + leaf (and the auto-generated AIG ALB cert) are valid for one year from stack creation. AIG → DLPoD TLS fails after expiry. | Plan a stack update that re-runs `DlpodAlbCertificate` and an instance refresh before expiry — see [OPERATIONS.md](OPERATIONS.md#secrets-and-ssm-parameters). |
| Secrets Manager secrets deleted on stack teardown | Deleting the stack permanently deletes all Secrets Manager secrets including API credentials. | If you need to preserve credentials, add a `DeletionPolicy: Retain` to the secret resources before deploying, or back up secret values before teardown. |
