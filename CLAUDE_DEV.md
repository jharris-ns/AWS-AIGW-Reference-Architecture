# CLAUDE_DEV.md

Development instructions for Claude Code when modifying code or templates in this repository.

## Templates

Three production templates exist. Each is self-contained — pick the one that matches the
deployment scenario and read only that template's directory.

| Template | File | Use case |
|----------|------|----------|
| Combined (AIG + DLPoD) | `templates/gateway-combined.yaml` | Full deployment: gateway + inline DLP |
| AIG standalone | `aig/template/gateway-aig.yaml` | Gateway only, DLP handled separately |
| DLPoD standalone | `dlpod/template/gateway-dlpod.yaml` | DLP appliance only |

Legacy templates (`templates/gateway-asg.yaml`, `templates/gateway-asg-ssm.yaml`) remain in the
repo for reference. Do not create new stacks with them — they predate the three-template split.

Test templates (`templates/test-*.yaml`) are debug/bisect tools for specific subsystems.

## Directory Structure

```
templates/
  gateway-combined.yaml       # Combined AIG + DLPoD (primary production template)
  gateway-asg.yaml            # Legacy monolithic template (do not use for new deployments)
  gateway-asg-ssm.yaml        # Legacy SSM variant
  test-*.yaml                 # Debug and subsystem test templates

aig/
  template/
    gateway-aig.yaml          # AIG standalone template
  docs/
    DEPLOYMENT.md             # AIG standalone deployment guide
    OPERATIONS.md             # AIG operations reference

dlpod/
  template/
    gateway-dlpod.yaml        # DLPoD standalone template
  docs/
    DEPLOYMENT.md             # DLPoD standalone deployment guide
    OPERATIONS.md             # DLPoD operations reference
  scripts/
    dlpod_handlers.py         # DLPoD tethering Lambda (packaged)
    build-dlpod-lambda.sh     # Packages dlpod_handlers.py
    build-tui-layer.sh        # Builds paramiko/pyte Lambda Layer (shared)
    deploy-artifacts.sh       # Upload DLPoD Lambda + layer to S3

docs/
  DEPLOYMENT.md               # Combined template deployment guide
  OPERATIONS.md               # Combined template operations reference
  ARCHITECTURE.md             # Architecture narrative
  QUICKSTART.md               # Condensed quick-start reference
  SECURITY.md                 # Secret handling, IAM, network security
  TROUBLESHOOTING.md          # Failure diagnosis and recovery

libs/tui/
  paramiko_session.py         # Paramiko-based TUI session (Lambda-compatible)
  cli_session.py              # DLPoD CLI session helpers
  tui_actions.py              # Menu navigation helpers
  tui_screen.py               # pyte screen parsing
  tui_session.py              # pexpect-based TUI session (local testing only)
  menu_config.py              # TUI menu definitions
  config.py                   # Shared configuration

scripts/
  activation_handler.py       # AIG Activation Lambda (used by legacy gateway-asg.yaml)
  step_function_handlers.py   # AIG Enrollment Lambda (used by legacy gateway-asg.yaml)
  build-activation-lambda.sh  # Packages activation_handler.py
  build-step-function-lambda.sh  # Packages step_function_handlers.py
  build-dlpod-lambda.sh       # Packages dlpod_handlers.py (root-level copy)
  build-tui-layer.sh          # Builds paramiko/pyte Lambda Layer
  deploy-artifacts.sh         # Upload all artifacts (activation + step-fn + dlpod + layer)

dist/
  lambda-activation.zip       # Pre-built activation Lambda (legacy gateway-asg.yaml)
  lambda-step-function.zip    # Pre-built enrollment Lambda (legacy gateway-asg.yaml)
  lambda-dlpod.zip            # Pre-built DLPoD Lambda (combined + dlpod standalone)
  pexpect-layer.zip           # Pre-built paramiko/pyte Lambda Layer
```

## Lifecycle Flows

### AIG Standalone and Combined template (AIG side)

The AIG lifecycle uses an inline Activation Lambda — no Step Functions, no SSH:

1. ASG launches instance → lifecycle hook holds it in `Pending:Wait`
2. SNS delivers event to `ActivationLambdaFunction` (inline)
3. Lambda registers appliance with Netskope API → receives enrollment token
4. Lambda writes bootstrap secret with `enrollment_token` (+ DLP cert and host in combined)
5. Lambda calls `CompleteLifecycleAction(CONTINUE)` → instance moves to `InService`
6. Instance reads bootstrap secret on first boot → self-enrolls using aig-cli
7. On termination: Lambda deregisters appliance, deletes SSM parameter

### DLPoD (standalone and combined)

DLPoD uses a packaged Lambda + Step Functions for tethering:

1. ASG launches DLPoD instance → lifecycle hook holds it
2. SNS delivers event to `DlpodActivationFunction` → starts `DlpodTetheringStateMachine`
3. State machine calls `DlpodLambdaFunction` (VPC-attached, paramiko + pyte):
   waits for SSH → navigates CLI → submits license key → polls completion
4. State machine calls `CompleteLifecycleAction(CONTINUE)` → DLPoD moves to `InService`
5. On termination: Activation Lambda deregisters appliance

### Legacy gateway-asg.yaml (AIG only)

Uses a different flow — packaged Activation Lambda + Step Functions + Enrollment Lambda
for SSH-based AIG enrollment. Do not model new development on this pattern.

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
| `DlpodActivationFunction` | Lambda::Function | DLPoD registration + starts tethering |
| `DlpodLambdaFunction` | Lambda::Function | DLPoD tethering actions (VPC-attached) |
| `DlpodTetheringStateMachine` | StepFunctions::StateMachine | Orchestrates DLPoD tethering |
| `ParamikoLayer` | Lambda::LayerVersion | paramiko + pyte for SSH/TUI automation |
| `DlpodAlbCertificate` | Custom::AlbCertificate | Self-signed cert for DLPoD ALB |
| `DlpodCredentialsSecret` | SecretsManager::Secret | DLPoD license key |
| `DlpodPrivateHostedZone` | Route53::HostedZone | Private DNS for DLPoD service endpoint |
| `CertGeneratorFunction` | Lambda::Function | Generates self-signed certs (shared) |

### AIG standalone (`aig/template/gateway-aig.yaml`)

| Resource | Type | Purpose |
|----------|------|---------|
| `GatewayAutoScalingGroup` | AutoScaling::AutoScalingGroup | AIG instance management |
| `GatewayLaunchTemplate` | EC2::LaunchTemplate | AIG instance config |
| `GatewayAlb` | ELBv2::LoadBalancer | Internet-facing HTTPS ingress |
| `ActivationLambdaFunction` | Lambda::Function | Registration + bootstrap (inline) |
| `AIGBootstrapSecret` | SecretsManager::Secret | Enrollment token (written at launch) |
| `NetskopeSecret` | SecretsManager::Secret | Tenant URL + API token |
| `LifecycleSnsTopic` | SNS::Topic | Lifecycle hook → Lambda delivery |

AIG standalone supports existing VPC (pass `ExistingVpcId`, `ExistingPublicSubnetId`,
`ExistingPrivateSubnetId`). Combined and DLPoD standalone always create a new VPC.

### DLPoD standalone (`dlpod/template/gateway-dlpod.yaml`)

| Resource | Type | Purpose |
|----------|------|---------|
| `DlpodAutoScalingGroup` | AutoScaling::AutoScalingGroup | DLPoD instance management |
| `DlpodLaunchTemplate` | EC2::LaunchTemplate | DLPoD instance config |
| `DlpodAlb` | ELBv2::LoadBalancer | Private ALB for DLPoD traffic |
| `DlpodActivationFunction` | Lambda::Function | Registration + starts tethering |
| `DlpodLambdaFunction` | Lambda::Function | Tethering actions (VPC-attached) |
| `DlpodTetheringStateMachine` | StepFunctions::StateMachine | Orchestrates DLPoD tethering |
| `ParamikoLayer` | Lambda::LayerVersion | paramiko + pyte for SSH/TUI automation |
| `DlpodAlbCertificate` | Custom::AlbCertificate | Self-signed cert for DLPoD ALB |
| `DlpodCredentialsSecret` | SecretsManager::Secret | DLPoD license key |
| `CertGeneratorFunction` | Lambda::Function | Generates self-signed cert |
| `DlpodPrivateHostedZone` | Route53::HostedZone | Private DNS for DLPoD endpoint |

## Template Conventions

- **YAML only**, two-space indent
- All named resources use `!Sub '${AWS::StackName}-<role>'`
- All taggable resources have `Project` (from parameter), `Environment`, `ManagedBy: CloudFormation`
- IAM follows least-privilege — separate statements per permission grant, no `Resource: '*'`
  except where required (DescribeInstances, VPC networking)
- Sensitive values in Secrets Manager; gateway instances have no Secrets Manager access
- Lifecycle hooks must be **inline** on the ASG (`LifecycleHookSpecificationList`) — separate
  resources create a race condition where instances launch before hooks exist
- AIG templates: inline Lambda (`ZipFile`) for the Activation Lambda — keeps the template
  self-contained without a packaging step
- DLPoD Lambda is packaged (`dlpod/scripts/dlpod_handlers.py` + libs) — code is too large
  for inline; referenced via `LambdaCodeBucket` + `DlpodLambdaCodeKey` parameters

## Artifacts

### What gets built

| Artifact | Source | Used by |
|----------|--------|---------|
| `dist/lambda-activation.zip` | `scripts/activation_handler.py` | `gateway-asg.yaml` (legacy) |
| `dist/lambda-step-function.zip` | `scripts/step_function_handlers.py` + `libs/` | `gateway-asg.yaml` (legacy) |
| `dist/lambda-dlpod.zip` | `dlpod/scripts/dlpod_handlers.py` + `libs/` | Combined + DLPoD standalone |
| `dist/pexpect-layer.zip` | `scripts/build-tui-layer.sh` | All templates with packaged Lambda |

### Upload scripts

- `scripts/deploy-artifacts.sh` — uploads all four artifacts (for combined template and legacy)
- `dlpod/scripts/deploy-artifacts.sh` — uploads `lambda-dlpod.zip` + `pexpect-layer.zip` only
  (for DLPoD standalone deployments); reads artifacts from root `dist/`

Pre-built artifacts in `dist/` are committed to the repo. Set `REBUILD=1` to rebuild from source.

## Development Rules

- **Lifecycle hooks must be inline** on the ASG — separate `AWS::AutoScaling::LifecycleHook`
  resources create a race condition (instances launch before hooks exist).
- **ASG must DependsOn SNS subscription and Lambda permission** — prevents instances from
  launching before the lifecycle event delivery chain is wired.
- **AIG uses inline Lambda only** — the Activation Lambda in the AIG standalone and combined
  templates is inline (`ZipFile`). The enrollment flow is bootstrap-secret-based (instance
  self-enrolls on boot). Do not introduce Step Functions or SSH into the AIG enrollment path
  unless reverting to the legacy pattern.
- **DLPoD Lambda must be VPC-attached** (private subnets, both AZs) to SSH to DLPoD instances.
  It needs the Secrets Manager VPC endpoint and NAT gateway for outbound access.
- **Build the Lambda Layer on x86_64** — use `--platform linux/amd64` with Docker/Podman when
  building on Apple Silicon. Lambda runs on x86_64.
- **Keep CloudFormation skill conventions** — `Project` and `Environment` as required tag
  parameters, `!Ref Project` in all tags, explicit IAM policies with no `Action: '*'`, VPC
  endpoints when creating a new VPC.
- **Do not hardcode AMI IDs or IP addresses** in documentation — environment-specific, passed
  as parameters.
- **Do not store API credentials on instances** — Activation Lambda handles all Netskope API
  calls. Enrollment token is passed via bootstrap secret and never persisted elsewhere.
- **`ParamikoTUISession` supports `mode='cli'`** for DLPoD CLI automation — bypasses TUI menu
  navigation and operates in direct command mode.
- **TUI cert paste requires bracket paste mode** — wrap certificate content with `\x1b[200~`
  and `\x1b[201~` escape sequences.
- **Cert must have `CA:TRUE` basicConstraints** for the AIG DLP service to accept it.
- **DlpodLambdaCodeKey and LambdaLayerKey are separate parameters** — combined and DLPoD
  templates take the S3 key for the DLPoD Lambda zip and the layer zip as parameters (not
  hardcoded paths), so multiple artifact versions can coexist in S3.
- **Sensitive fields are redacted in Lambda logs** — `password`, `enrollment_token`, and
  `license_key` values must be masked before logging.
- **Template size**: combined and DLPoD standalone exceed 51 KB → must be deployed via
  `--template-url` referencing S3. AIG standalone (~34 KB) can be uploaded directly or via S3.
