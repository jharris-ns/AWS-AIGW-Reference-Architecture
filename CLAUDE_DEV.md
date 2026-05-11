# CLAUDE_DEV.md

Development instructions for Claude Code when modifying code or templates in this repository.

## Directory Structure

```
templates/
  gateway-asg.yaml           # Production CloudFormation template (all resources)
  test-dlpod.yaml            # DLPoD standalone test template
libs/tui/
  paramiko_session.py        # Paramiko-based TUI session (Lambda-compatible)
  cli_session.py             # DLPoD CLI session helpers
  tui_actions.py             # Menu navigation helpers
  tui_screen.py              # pyte screen parsing
  tui_session.py             # pexpect-based TUI session (local testing only)
scripts/
  step_function_handlers.py  # Enrollment Lambda handlers (action-routed)
  dlpod_handlers.py          # DLPoD tethering Lambda handler
  deploy-artifacts.sh        # Build and upload all deployment artifacts
  build-tui-layer.sh         # Builds paramiko/pyte Lambda Layer
  build-step-function-lambda.sh  # Packages enrollment Lambda
  build-dlpod-lambda.sh      # DLPoD Lambda build script
docs/
  DEPLOYMENT.md              # Build artifacts, upload to S3, deploy the stack
  DEVOPS.md                  # Operations guide — lifecycle, scaling, secrets
  TROUBLESHOOTING.md         # Claude Code usage, AWS credentials, failure diagnosis, manual recovery
  CERTIFICATE_MANAGEMENT.md  # ACM certificate creation and import (Mac/Win/Linux)
```

## Template Conventions

- **YAML only**, two-space indent
- All named resources use `!Sub '${AWS::StackName}-<role>'`
- All taggable resources have `Project` (from parameter), `Environment`, `ManagedBy: CloudFormation`
- IAM follows least-privilege — separate statements per permission grant, no `Resource: '*'` except where required (DescribeInstances, VPC networking)
- Sensitive values stored in Secrets Manager; gateway instances have no access to Secrets Manager or Parameter Store
- Supports both new VPC (creates public/private subnets, IGW, NAT gateway, VPC endpoints) and existing VPC (user provides public + private subnet IDs)
- Activation Lambda is inline (`ZipFile`) for the dispatcher; Enrollment Lambda is S3-packaged with a paramiko/pyte Layer
- Step Functions ASL is defined inline in the template using `!Sub` for Lambda ARN interpolation — use `${Resource.Arn}` syntax, not `$.` (which is JSONPath for state machine input)

## Key Resources in the Template

| Resource | Type | Purpose |
|----------|------|---------|
| `GatewayAutoScalingGroup` | AutoScaling::AutoScalingGroup | Instance management with inline lifecycle hooks |
| `GatewayLaunchTemplate` | EC2::LaunchTemplate | Instance config (AMI, type, SG, IAM, EBS) |
| `ApplicationLoadBalancer` | ELBv2::LoadBalancer | Internet-facing HTTPS ingress (public subnets) |
| `ActivationLambdaFunction` | Lambda::Function | Appliance registration, starts Step Functions |
| `EnrollmentLambdaFunction` | Lambda::Function | SSH/TUI enrollment actions (VPC-attached) |
| `EnrollmentStateMachine` | StepFunctions::StateMachine | Orchestrates enrollment flow with polling |
| `ParamikoLayer` | Lambda::LayerVersion | paramiko + pyte for SSH/TUI automation |
| `SSHKeyPair` | Custom::SSHKeyPair | Generates SSH key pair for Lambda→gateway access |
| `NetskopeSecret` | SecretsManager::Secret | Tenant URL + API token |
| `SSHPrivateKeySecret` | SecretsManager::Secret | SSH private key for Lambda automation |
| `LifecycleSnsTopic` | SNS::Topic | Lifecycle hook → Lambda delivery |
| `DlpodAutoScalingGroup` | AutoScaling::AutoScalingGroup | DLPoD instance management with lifecycle hooks |
| `DlpodLaunchTemplate` | EC2::LaunchTemplate | DLPoD instance config |
| `DlpodAlb` | ELBv2::LoadBalancer | Private ALB for DLPoD traffic |
| `DlpodTargetGroup` | ELBv2::TargetGroup | DLPoD ALB target group |
| `DlpodTetheringStateMachine` | StepFunctions::StateMachine | Orchestrates DLPoD tethering flow |
| `DlpodTetheringLambda` | Lambda::Function | DLPoD tethering actions (VPC-attached) |
| `DlpodActivationLambda` | Lambda::Function | DLPoD registration, starts tethering |
| `CertGeneratorLambda` | Lambda::Function | Generates certificates (shared) |
| `PrivateHostedZone` | Route53::HostedZone | Private DNS for internal resolution |

## Development Rules

- **Lifecycle hooks must be inline** on the ASG (`LifecycleHookSpecificationList`), not separate resources — separate resources create a race condition where the ASG launches instances before the hooks exist.
- **ASG must DependsOn SNS subscription and Lambda permission** — prevents the ASG from launching instances before the lifecycle event delivery chain is fully wired.
- **When modifying the Activation Lambda code**, keep both `handle_lifecycle_event` (ASG mode) and `handle_cfn_event` (single-instance mode) handlers — the same Lambda code is shared with the single-instance `gateway.yaml` template.
- **The Enrollment Lambda must be in the VPC** (private subnets, both AZs) to SSH to gateway instances. It needs the Secrets Manager VPC endpoint and NAT gateway for outbound access.
- **Build the Lambda Layer on x86_64** — use `--platform linux/amd64` with Docker/Podman when building on Apple Silicon. Lambda runs on x86_64.
- **Keep the CloudFormation skill conventions** — `Project` and `Environment` as required tag parameters, `!Ref Project` in all tags, explicit IAM policies with no `Action: '*'`, VPC endpoints when creating a new VPC.
- **Do not hardcode AMI IDs or IP addresses** in documentation — these are environment-specific and passed as parameters.
- **Do not store API credentials on instances** — the Activation Lambda handles all Netskope API calls. The enrollment token is passed directly over SSH and never persisted.
- **`ParamikoTUISession` supports `mode='cli'`** for DLPoD CLI automation — this bypasses TUI menu navigation and operates in direct command mode.
- **TUI cert paste requires bracket paste mode** — wrap certificate content with `\x1b[200~` and `\x1b[201~` escape sequences.
- **Cert must have `CA:TRUE` basicConstraints** for the AIG DLP service to accept it.
- **DLPoD resources are conditional** on the `DeployDlpod` condition — all DLPoD resources in the template are gated by this condition.
- **Sensitive fields are redacted in Lambda logs** — `password`, `enrollment_token`, and `license_key` values are masked before logging.
