# CLAUDE_DEV.md

Development instructions for Claude Code when modifying code or templates in this repository.

## Template

This repository contains a single production template: `templates/gateway-combined.yaml`.
It deploys AIG + DLP On Demand + optional AI Guardrails in one stack.

> **Standalone templates** (AIG-only, DLPoD-only) are in [AWS-POV-Templates-CFT](https://github.com/jharris-ns/AWS-POV-Templates-CFT).

## Directory Structure

```
templates/
  gateway-combined.yaml       # Combined AIG + DLPoD + optional Guardrails

docs/
  DEPLOYMENT.md               # Combined template deployment guide
  OPERATIONS.md               # Combined template operations reference
  ARCHITECTURE.md             # Architecture narrative
  QUICKSTART.md               # Condensed quick-start reference
  SECURITY.md                 # Secret handling, IAM, network security
  TROUBLESHOOTING.md          # Failure diagnosis and recovery

scripts/
  deploy-artifacts.sh         # Creates S3 bucket for template upload

dist/
  (empty — no packaged Lambda artifacts; all Lambdas are inline)
```

## Lifecycle Flows

### AIG (combined template)

The AIG lifecycle uses an inline Activation Lambda — no Step Functions, no SSH:

1. ASG launches instance → lifecycle hook holds it in `Pending:Wait`
2. SNS delivers event to `ActivationLambdaFunction` (inline)
3. Lambda registers appliance with Netskope API → receives enrollment token
4. Lambda writes bootstrap secret with `enrollment_token` (+ DLP cert and host in combined)
5. Lambda calls `CompleteLifecycleAction(CONTINUE)` → instance moves to `InService`
6. Instance reads bootstrap secret on first boot → self-enrolls using aig-cli
7. On termination: Lambda deregisters appliance, deletes SSM parameter

### DLPoD (combined template)

DLPoD uses nsbootstrap.service with EC2 UserData — no SSH, no Step Functions, no paramiko:

1. `CertGeneratorFunction` (inline Lambda) generates self-signed TLS certs at stack create
2. `DlpodBootstrapBuilderFunction` (inline Lambda) reads certs + license key from Secrets Manager,
   assembles `bootstrap.json`, and returns base64-encoded UserData
3. ASG launches DLPoD instance with UserData containing the encoded `bootstrap.json`
4. `nsbootstrap.service` reads `bootstrap.json` at first boot and configures TLS certs,
   license key, DNS, and persona — instance is ready without any external orchestration

## Key Resources

### Combined template (`templates/gateway-combined.yaml`)

| Resource | Type | Purpose |
|----------|------|---------|
| `GatewayAutoScalingGroup` | AutoScaling::AutoScalingGroup | AIG instance management |
| `GatewayLaunchTemplate` | EC2::LaunchTemplate | AIG instance config (AMI, SG, IAM, EBS) |
| `GatewayAlb` | ELBv2::LoadBalancer | Internet-facing HTTPS ingress |
| `AigActivationLambdaFunction` | Lambda::Function | AIG registration + bootstrap (inline) |
| `AigBootstrapSecret` | SecretsManager::Secret | Enrollment token + DLP cert (written at launch) |
| `NetskopeSecret` | SecretsManager::Secret | Tenant URL + API token |
| `AigAlbCertificate` | Custom::AlbCertificate | Self-signed cert for AIG ALB (if no ACM cert) |
| `DlpodAutoScalingGroup` | AutoScaling::AutoScalingGroup | DLPoD instance management |
| `DlpodBootstrapBuilderFunction` | Lambda::Function | Assembles bootstrap.json UserData (inline) |
| `CertGeneratorFunction` | Lambda::Function | Generates self-signed TLS certs (inline, shared) |
| `DlpodAlbCertificate` | Custom::AlbCertificate | Self-signed cert for DLPoD ALB |
| `DlpodCredentialsSecret` | SecretsManager::Secret | DLPoD license key |
| `DlpodPrivateHostedZone` | Route53::HostedZone | Private zone `aigw.internal` (DLPoD + Guardrails records) |
| `DlpodReadinessGateFunction` | Lambda::Function | Target-group readiness poller (inline, shared by both gates) |
| `DlpodReadinessGate` | Custom::DlpodReadiness | Blocks AIG launch until DLPoD targets healthy |
| `GuardrailsAutoScalingGroup` | AutoScaling::AutoScalingGroup | *(Condition: DeployGuardrails)* GPU instances running `aisecurityllm` |
| `GuardrailsLaunchTemplate` | EC2::LaunchTemplate | *(conditional)* DL Base GPU AMI; UserData does `aws s3 cp` + `docker load` + `docker run` |
| `GuardrailsAlb` | ELBv2::LoadBalancer | *(conditional)* Internal HTTP ALB at `guardrails.aigw.internal` |
| `GuardrailsReadinessGate` | Custom::DlpodReadiness | *(conditional)* Blocks AIG launch until Guardrails targets healthy |

## Template Conventions

- **YAML only**, two-space indent
- All named resources use `!Sub '${AWS::StackName}-<role>'`
- No tag parameters — `Project`, `Environment`, and `ManagedBy` are passed as stack-level `--tags`
- IAM follows least-privilege — separate statements per permission grant, no `Resource: '*'`
  except where required (DescribeInstances, VPC networking)
- Sensitive values in Secrets Manager; gateway instances have no Secrets Manager access
- Lifecycle hooks must be **inline** on the ASG (`LifecycleHookSpecificationList`) — separate
  resources create a race condition where instances launch before hooks exist
- All Lambda functions use inline `ZipFile` code — no S3 Lambda artifacts are required.
  This keeps templates self-contained without a packaging step.

## Artifacts

All Lambda functions are inline (`ZipFile`) — no packaged artifacts to build or upload.
The only script is `scripts/deploy-artifacts.sh`, which creates the S3 bucket used for
template upload (required because the combined template, ~68 KB, exceeds 51 KB).

## Development Rules

- **Lifecycle hooks must be inline** on the ASG — separate `AWS::AutoScaling::LifecycleHook`
  resources create a race condition (instances launch before hooks exist).
- **ASG must DependsOn SNS subscription and Lambda permission** — prevents instances from
  launching before the lifecycle event delivery chain is wired.
- **AIG uses inline Lambda only** — the Activation Lambda is inline (`ZipFile`). The enrollment flow is bootstrap-secret-based (instance
  self-enrolls on boot). Do not introduce Step Functions or SSH into the AIG enrollment path
  unless reverting to the legacy pattern.
- **DLPoD uses nsbootstrap.service** — instances self-configure at first boot using
  `bootstrap.json` delivered via EC2 UserData. No SSH, no paramiko, no Step Functions.
- **Keep CloudFormation conventions** — explicit IAM policies with no `Action: '*'`; no tag
  parameters (pass `Project`/`Environment`/`ManagedBy` via `--tags` at deploy time).
- **Do not hardcode AMI IDs or IP addresses** in documentation — environment-specific, passed
  as parameters.
- **Do not store API credentials on instances** — Activation Lambda handles all Netskope API
  calls. Enrollment token is passed via bootstrap secret and never persisted elsewhere.
- **Cert must have `CA:TRUE` basicConstraints** for the AIG DLP service to accept it.
- **Sensitive fields are redacted in Lambda logs** — `password`, `enrollment_token`, and
  `license_key` values must be masked before logging.
- **Template size**: the template (~71 KB) exceeds 51 KB → must be deployed via
  `--template-url` referencing S3.
- **Guardrails is optional and conditional** — every Guardrails resource carries
  `Condition: DeployGuardrails`. `DependsOn` cannot target a conditional resource (cfn-lint
  E3005), so `GatewayAutoScalingGroup` depends on `GuardrailsReadinessGate` via a
  `!If [DeployGuardrails, !Ref GuardrailsReadinessGate, disabled]` tag value instead.
- **Custom::AlbCertificate must `!Ref` its SSM placeholder** (`CertParameterName: !Ref
  DlpodCertParameter`), not `!Sub` the name. The Lambda writes the parameter with
  `Overwrite=True`; without the implicit dependency CloudFormation can try to create the
  placeholder afterwards and fail with `ParameterAlreadyExists`.
- **Run `cfn-lint templates/gateway-combined.yaml` before committing** — the template is
  currently lint-clean (W2506/W1030 suppressed for the String-typed optional `GuardrailsAmiId`).
