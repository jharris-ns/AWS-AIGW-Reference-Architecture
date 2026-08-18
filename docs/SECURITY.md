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

1. **Least privilege IAM** — Nine dedicated IAM roles; each has exactly the permissions its function
   requires. No wildcards on resource ARNs for sensitive operations.

2. **Secrets never touch instances directly** — The Netskope API token flows from Secrets Manager
   to the Activation Lambda only (in memory). The Lambda exchanges the token for a short-lived
   enrollment token, which it writes to the bootstrap secret. Instances read the bootstrap secret
   at boot — they never have access to the raw API token.

3. **No public IP on compute** — All EC2 instances run in private subnets. Inbound access is
   exclusively through the ALBs. There is no SSH inbound path from the internet to any instance.

4. **All sensitive parameters are `NoEcho`** — `NetskopeApiToken` and `DlpodLicenseKey` are
   never shown in CloudFormation events, stack outputs, or the console after submission.

---

## Network Security

### Security Group Rules

**AIG ALB Security Group** (`<stack>-aig-alb-sg`)

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | `0.0.0.0/0` | HTTPS from internet clients |
| Outbound | TCP | 443 | AIG Instance SG | Forward to AIG instances |

**AIG Instance Security Group** (`<stack>-aig-sg`)

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | AIG ALB SG | HTTPS from ALB only |
| Outbound | All | All | `0.0.0.0/0` | Outbound via NAT GW (Netskope API, LLM providers, DLPoD ALB) |

**DLPoD ALB Security Group** (`<stack>-dlpod-alb-sg`)

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | AIG Instance SG | HTTPS from AIG instances only |
| Outbound | TCP | 443 | DLPoD Instance SG | Forward to DLPoD instances |

**DLPoD Instance Security Group** (`<stack>-dlpod-sg`)

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | DLPoD ALB SG | HTTPS for DLP inspection |
| Inbound | TCP | 22 | DLPoD Lambda SG | SSH for tethering automation only |
| Outbound | All | All | `0.0.0.0/0` | Tethering callhome via NAT GW |

**DLPoD Lambda Security Group** (`<stack>-dlpod-lambda-sg`)

| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Outbound | TCP | 22 | DLPoD Instance SG | SSH for tethering |
| Outbound | TCP | 443 | DLPoD Instance SG | HTTPS for tethering status checks |

### Network Isolation

- **No direct internet inbound to instances.** The internet-facing AIG ALB is the only inbound
  internet path. It terminates TLS and forwards to AIG instances in private subnets.
- **DLPoD is fully internal.** The DLPoD ALB is internal-only (private subnets). DLPoD instances
  are reachable only from the DLPoD ALB (DLP inspection) and the DLPoD Lambda (tethering).
- **Outbound internet via NAT Gateway only.** All instance and Lambda outbound internet traffic
  traverses the single NAT Gateway. There is no direct internet gateway route to private subnets.
- **AIG → DLPoD via private DNS.** AIG instances resolve `dlp.aigw.internal` via a Route 53
  private hosted zone — this alias always resolves to the DLPoD internal ALB, never to a public
  address.

---

## IAM Roles and Permissions

### Role Summary

| Role | Assumed by | Purpose |
|---|---|---|
| `<stack>-aig-role` | `ec2.amazonaws.com` | AIG instance profile |
| `<stack>-aig-activation-role` | `lambda.amazonaws.com` | AIG lifecycle management |
| `<stack>-aig-lifecycle-sns-role` | `autoscaling.amazonaws.com` | AIG lifecycle event delivery |
| `<stack>-dlpod-role` | `ec2.amazonaws.com` | DLPoD instance profile |
| `<stack>-dlpod-activation-role` | `lambda.amazonaws.com` | DLPoD lifecycle initiation |
| `<stack>-dlpod-sfn-role` | `states.amazonaws.com` | Step Functions → Lambda invocation |
| `<stack>-dlpod-lambda-role` | `lambda.amazonaws.com` | DLPoD tethering (VPC-attached) |
| `<stack>-dlpod-lifecycle-sns-role` | `autoscaling.amazonaws.com` | DLPoD lifecycle event delivery |
| `<stack>-cert-generator-role` | `lambda.amazonaws.com` | Self-signed cert generation |

### Key Principle: AIG Instances Never Hold API Credentials

AIG instances have an IAM role (`<stack>-aig-role`) that allows only two actions:
1. `secretsmanager:GetSecretValue` on the specific bootstrap secret ARN
2. `logs:*` on the instance's CloudWatch log group

The Netskope API token is in a separate secret (`<stack>-api-credentials`) that the AIG instance
role has **no access to**. Only the Activation Lambda reads it.

### Role Permissions Detail

**`<stack>-aig-role` (AIG instance profile)**
- `secretsmanager:GetSecretValue` — bootstrap secret only (ARN-scoped)
- `logs:CreateLogStream`, `logs:PutLogEvents` — CloudWatch logging

**`<stack>-aig-activation-role` (AIG Activation Lambda)**
- `secretsmanager:GetSecretValue` — `<stack>-api-credentials` (reads API token)
- `secretsmanager:PutSecretValue` — `<stack>-aig-bootstrap` (writes enrollment token)
- `ssm:GetParameter` — `/<stack>/dlpod-cert` (reads DLPoD cert for bootstrap secret)
- `ssm:PutParameter`, `ssm:DeleteParameter` — `/<stack>/appliances/*` (appliance ID tracking)
- `ec2:DescribeInstances` — describe launching instance to get IP
- `autoscaling:CompleteLifecycleAction` — complete launch/termination hooks
- `logs:*` — CloudWatch logging

**`<stack>-dlpod-lambda-role` (DLPoD tethering Lambda, VPC-attached)**
- `secretsmanager:GetSecretValue` — `<stack>-dlpod-credentials` (license key)
- `ssm:PutParameter` — `/<stack>/dlpod-cert` (writes cert PEM after generation)
- `autoscaling:CompleteLifecycleAction` — complete DLPoD launch hook
- `ec2:CreateNetworkInterface`, `ec2:DescribeNetworkInterfaces`, `ec2:DeleteNetworkInterface` — VPC attachment
- `logs:*` — CloudWatch logging

**`<stack>-cert-generator-role` (cert generator custom resource Lambda)**
- `acm:ImportCertificate`, `acm:DeleteCertificate` — import/delete self-signed certs
- `ssm:PutParameter`, `ssm:DeleteParameter` — `/<stack>/dlpod-cert`, `/<stack>/aig-cert`
- `secretsmanager:PutSecretValue` — `<stack>-aig-bootstrap` (pre-populates DLP block)
- `logs:*` — CloudWatch logging

---

## Secret and Credential Management

### What's Stored and Where

| Secret name | Type | Contents | Who writes | Who reads |
|---|---|---|---|---|
| `<stack>-api-credentials` | Secrets Manager | `{"api_token": "...", "tenant_url": "..."}` | CloudFormation (from `NoEcho` parameter) | AIG Activation Lambda only |
| `<stack>-aig-bootstrap` | Secrets Manager | `{"bootstrap": true, "enrollment_token": "...", "dlp": {"certificate": "...", "host": "..."}}` | Cert generator (DLP block); Activation Lambda (token) | AIG instances at boot |
| `<stack>-dlpod-credentials` | Secrets Manager | `{"license_key": "..."}` | CloudFormation (from `NoEcho` parameter) | DLPoD tethering Lambda only |
| `/<stack>/dlpod-cert` | SSM Parameter (SecureString) | PEM-encoded DLPoD ALB self-signed cert | Cert generator Lambda | AIG Activation Lambda |
| `/<stack>/appliances/<id>` | SSM Parameter | AIG appliance ID in Netskope tenant | AIG Activation Lambda at launch | AIG Activation Lambda at termination |

### Credential Flow

```
1. User provides API token as NoEcho CloudFormation parameter
2. CloudFormation creates <stack>-api-credentials in Secrets Manager
3. ASG launches AIG instance → lifecycle hook → Activation Lambda fires
4. Activation Lambda reads API token from Secrets Manager (encrypted in transit, AWS SDK TLS)
5. Activation Lambda calls Netskope REST API → receives enrollment token (exists in Lambda memory only)
6. Activation Lambda writes enrollment token to <stack>-aig-bootstrap (separate secret)
7. AIG instance reads bootstrap secret at boot over HTTPS → self-enrolls
8. Enrollment token is consumed; it is not persisted beyond the bootstrap secret write
```

The API token (`<stack>-api-credentials`) and the enrollment token (`<stack>-aig-bootstrap`) are
in separate secrets. Compromise of the bootstrap secret does not expose the API token.

---

## Encryption

### In Transit

| Path | Protocol | Notes |
|---|---|---|
| Internet → AIG ALB | TLS 1.2+ | Certificate from ACM (auto-generated or user-provided) |
| AIG ALB → AIG instances | TLS (HTTPS:443) | ALB health checks and traffic forwarding |
| AIG instances → DLPoD ALB | TLS (HTTPS:443) | Self-signed cert; AIG trusts cert via bootstrap secret |
| DLPoD ALB → DLPoD instances | TLS (HTTPS:443) | |
| DLPoD instances → Netskope management plane | TLS (HTTPS:443) | Tethering callhome |
| Lambda → Secrets Manager / SSM | TLS (AWS SDK) | All AWS SDK calls use TLS |
| Lambda → Netskope REST API | TLS (HTTPS:443) | AIG enrollment and deregistration |
| DLPoD Lambda → DLPoD instance | SSH (port 22) | Tethering automation; password-based (see Known Limitations) |

### At Rest

| Resource | Encryption |
|---|---|
| Secrets Manager secrets | AES-256, AWS-managed KMS key (default) |
| SSM Parameter Store parameters | AES-256, AWS-managed KMS key (SecureString type) |
| EBS root volumes | Encrypted if the AWS account has default EBS encryption enabled; not explicitly enforced by the template |
| CloudWatch Logs | Encrypted at rest by default (AWS-managed) |

---

## CloudFormation Security Practices

✅ **`NoEcho: true`** on all sensitive parameters (`NetskopeApiToken`, `DlpodLicenseKey`) — values
are never shown in CloudFormation events, stack output, or the console.

✅ **Resource-scoped IAM policies** — No `Resource: "*"` on sensitive actions. Secrets Manager
and SSM access is scoped to specific resource ARNs constructed with `!Sub`.

✅ **No secrets in user data** — AIG instance user data is empty. The instance reads its
configuration from Secrets Manager at boot using its IAM role.

✅ **No secrets in Lambda environment variables** — Lambda functions retrieve credentials at
runtime from Secrets Manager using the execution role. Credentials are not present in the
function configuration.

✅ **Separate secrets for separate purposes** — API credentials (`<stack>-api-credentials`),
bootstrap data (`<stack>-aig-bootstrap`), and license key (`<stack>-dlpod-credentials`) are
in separate Secrets Manager secrets with separate access controls.

✅ **Conditions prevent unnecessary resource creation** — `UseAutoGeneratedAigCert` condition
avoids creating the cert generator invocation when a user-provided ACM ARN is given.

⚠️ **AIG ALB uses a self-signed certificate by default** — The auto-generated cert has
`aig.aigw.internal` as its CN/SAN. API clients must be configured to trust it or skip TLS
verification. For production deployments with external clients, provide a trusted ACM certificate
via `AcmCertificateArn`. See [DEPLOYMENT.md — ACM Certificate](DEPLOYMENT.md#4-acm-certificate-optional).

⚠️ **EBS encryption is not explicitly enforced by the template** — EBS root volumes are
encrypted only if the AWS account has default EBS encryption enabled at the account level.
Enable default EBS encryption in your account before deploying if this is required.

---

## Audit and Visibility

### CloudWatch Log Groups

| Log group | Contents | Retention |
|---|---|---|
| `/aws/lambda/<stack>-aig-activation` | AIG registration/deregistration with Netskope API, lifecycle hook events | 7 days |
| `/aws/lambda/<stack>-dlpod-activation` | DLPoD ASG lifecycle events, Step Functions execution start | 7 days |
| `/aws/lambda/<stack>-dlpod` | DLPoD tethering SSH automation steps, license application, tethering status | 7 days |
| `/aws/lambda/<stack>-cert-generator` | Self-signed cert generation, ACM import, SSM parameter writes | 7 days |

### Netskope Audit Log

All AI Gateway activity — requests, responses, blocked events, policy decisions — is logged to
the Netskope management plane. Access logs from the Netskope UI under
**Analytics → SkopeIT → AI Gateway** or via the Netskope Events API.

### What to Monitor

- Lambda function errors → CloudWatch Metrics → filter on `Errors` for each log group
- AIG enrollment failures → check `/aws/lambda/<stack>-aig-activation` for `ERROR` lines
- DLPoD tethering failures → Step Functions console → look for `FAILED` executions
- ALB target health → `aws elbv2 describe-target-health` — unhealthy targets indicate enrollment/tethering problems

---

## Known Limitations and Accepted Risks

| Item | Detail | Mitigation |
|---|---|---|
| DLPoD tethering uses password-based SSH | The tethering Lambda connects to DLPoD via SSH with a randomly generated 24-character password. The password is held in Step Functions execution state during tethering and discarded afterward. | Password is unique per instance, randomly generated, and not persisted after tethering completes. Network path is restricted to the DLPoD Lambda SG → DLPoD instance SG (port 22 only). |
| Auto-generated AIG ALB cert is self-signed | CN/SAN is `aig.aigw.internal`, which does not resolve publicly. Clients must trust the cert or skip TLS verification. | Provide a valid ACM certificate via `AcmCertificateArn` for production deployments where clients require trusted TLS. |
| EBS encryption not explicitly enforced | The template does not set `Encrypted: true` on the EC2 launch template EBS volumes. | Enable AWS account-level default EBS encryption (`aws ec2 enable-ebs-encryption-by-default`) before deploying. |
| Secrets Manager secrets deleted on stack teardown | Deleting the stack permanently deletes all Secrets Manager secrets including API credentials. | If you need to preserve credentials, remove them from the template's `DeletionPolicy` before deploying, or back up secret values before teardown. |
