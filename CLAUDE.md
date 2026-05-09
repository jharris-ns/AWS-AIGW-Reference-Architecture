# CLAUDE.md

Project-level instructions for Claude Code in the AWS-AIGW-Reference-Architecture repository.

## Project Overview

This is an **AWS reference architecture for Netskope AI Gateway** — a CloudFormation-based deployment that automates the provisioning, enrollment, and lifecycle management of Netskope AI Gateway appliances in an Auto Scaling Group behind an Application Load Balancer.

The AI Gateway acts as an inline security enforcement layer between AI-powered applications and LLM providers (AWS Bedrock, OpenAI, etc.), providing DLP, content moderation, authentication, rate limiting, and compliance logging.

## Architecture

Inbound traffic from the internet arrives at an internet-facing Application Load Balancer listening on HTTPS port 443, deployed across two public subnets. The ALB forwards requests to a target group backed by an Auto Scaling Group of Netskope AI Gateway instances running in private subnets. These instances reach the internet through a NAT gateway for outbound API calls. Each gateway instance optionally forwards content to a DLPoD (DLP on Demand) appliance for inline data loss prevention inspection before proxying requests onward to upstream LLM providers such as AWS Bedrock or OpenAI.

### Lifecycle Automation

Each gateway instance is automatically enrolled and configured through this chain:

1. **ASG launches instance** → inline lifecycle hook holds it in `Pending:Wait`
2. **SNS** delivers lifecycle event to **Activation Lambda**
3. **Activation Lambda** reads Netskope API credentials from Secrets Manager, registers appliance with tenant API, receives enrollment token (held in memory only), starts **Step Functions** execution
4. **Step Functions** orchestrates enrollment via the **Enrollment Lambda** (VPC-attached, paramiko + pyte): waits for SSH → navigates aig-cli TUI → polls pre-enrollment → submits token → polls completion → calls `CompleteLifecycleAction`
5. Instance moves to `InService`
6. On termination: Activation Lambda deregisters appliance from tenant, cleans up SSM parameter (appliance ID only)

### Secret Handling

Netskope API credentials never touch the gateway instances. The Activation Lambda reads from Secrets Manager and calls the Netskope API to generate an enrollment token, which exists only in Lambda memory. The token is passed directly to the instance over SSH and never persisted to any AWS storage service. The instance IAM role only needs CloudWatch Logs permissions — no access to Secrets Manager or Parameter Store.

## Directory Structure

```
templates/
  gateway-asg.yaml           # Production CloudFormation template (all resources)
  gateway-asg-ssm.yaml       # Archived SSM-based template (pre-SSH migration)
  test-ssh-tunnel.yaml       # SSH tunnel proof-of-concept test stack
  test-step-function.yaml    # Step Functions enrollment test stack
libs/tui/
  paramiko_session.py        # Paramiko-based TUI session (Lambda-compatible)
  tui_actions.py             # Menu navigation helpers
  tui_screen.py              # pyte screen parsing
  tui_session.py             # pexpect-based TUI session (local testing only)
scripts/
  step_function_handlers.py  # Enrollment Lambda handlers (action-routed)
  build-tui-layer.sh         # Builds paramiko/pyte Lambda Layer
  build-step-function-lambda.sh  # Packages enrollment Lambda
docs/
  DEVOPS.md                  # Operations guide — lifecycle, scaling, secrets, troubleshooting
  CERTIFICATE_MANAGEMENT.md  # ACM certificate creation and import (Mac/Win/Linux)
  TUI_ENROLLMENT_PLAN.md     # Implementation plan and test results
```

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

## Template Conventions

- **YAML only**, two-space indent
- All named resources use `!Sub '${AWS::StackName}-<role>'`
- All taggable resources have `Project` (from parameter), `Environment`, `ManagedBy: CloudFormation`
- IAM follows least-privilege — separate statements per permission grant, no `Resource: '*'` except where required (DescribeInstances, VPC networking)
- Sensitive values stored in Secrets Manager; gateway instances have no access to Secrets Manager or Parameter Store
- Supports both new VPC (creates public/private subnets, IGW, NAT gateway, VPC endpoints) and existing VPC (user provides public + private subnet IDs)
- Activation Lambda is inline (`ZipFile`) for the dispatcher; Enrollment Lambda is S3-packaged with a paramiko/pyte Layer
- Step Functions ASL is defined inline in the template using `!Sub` for Lambda ARN interpolation — use `${Resource.Arn}` syntax, not `$.` (which is JSONPath for state machine input)

## Deployment

The template exceeds 51KB and must be uploaded to S3 before deployment:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-1
BUCKET="netskope-aigw-templates-${ACCOUNT_ID}"

aws s3 mb "s3://${BUCKET}" --region "${REGION}"
aws s3 cp templates/gateway-asg.yaml "s3://${BUCKET}/templates/gateway-asg.yaml"

aws cloudformation create-stack \
  --stack-name <name> \
  --template-url "https://${BUCKET}.s3.${REGION}.amazonaws.com/templates/gateway-asg.yaml" \
  --parameters \
    ParameterKey=ExistingVpcId,ParameterValue=<vpc-id> \
    ParameterKey=ExistingPublicSubnetId,ParameterValue=<pub-subnet-1> \
    ParameterKey=ExistingPublicSubnet2Id,ParameterValue=<pub-subnet-2> \
    ParameterKey=ExistingPrivateSubnetId,ParameterValue=<priv-subnet-1> \
    ParameterKey=ExistingPrivateSubnet2Id,ParameterValue=<priv-subnet-2> \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=AcmCertificateArn,ParameterValue=<acm-arn> \
    ParameterKey=GatewayAmiId,ParameterValue=<ami-id> \
    ParameterKey=LambdaCodeBucket,ParameterValue=<s3-bucket> \
  --capabilities CAPABILITY_NAMED_IAM
```

## Rules

- **Do not store API credentials on instances** — the Activation Lambda handles all Netskope API calls. The enrollment token is passed directly over SSH and never persisted.
- **Lifecycle hooks must be inline** on the ASG (`LifecycleHookSpecificationList`), not separate resources — separate resources create a race condition where the ASG launches instances before the hooks exist.
- **ASG must DependsOn SNS subscription and Lambda permission** — prevents the ASG from launching instances before the lifecycle event delivery chain is fully wired.
- **Template must be uploaded to S3** before deployment (exceeds 51KB inline limit). Bucket must be in the same region as the stack. The Lambda package and Layer zip must also be in the same bucket.
- **Do not hardcode AMI IDs or IP addresses** in documentation — these are environment-specific and passed as parameters.
- **CUDA NVIDIA GPU required** for advanced AI guardrails — standard guardrails work on CPU instances (m5.4xlarge), but advanced guardrails need GPU instances (g4dn, g5).
- **Keep the CloudFormation skill conventions** — `Project` and `Environment` as required tag parameters, `!Ref Project` in all tags, explicit IAM policies with no `Action: '*'`, VPC endpoints when creating a new VPC.
- **When modifying the Activation Lambda code**, keep both `handle_lifecycle_event` (ASG mode) and `handle_cfn_event` (single-instance mode) handlers — the same Lambda code is shared with the single-instance `gateway.yaml` template.
- **The Enrollment Lambda must be in the VPC** (private subnets, both AZs) to SSH to gateway instances. It needs the Secrets Manager VPC endpoint and NAT gateway for outbound access.
- **Build the Lambda Layer on x86_64** — use `--platform linux/amd64` with Docker/Podman when building on Apple Silicon. Lambda runs on x86_64.

## Related Resources

- [Netskope AI Gateway Documentation](https://docs.netskope.com/en/ai-gateway/)
- [Netskope RBAC V3 Overview](https://docs.netskope.com/en/netskope-rbac-v3-overview/)
- [DLP On Demand Documentation](https://docs.netskope.com/en/data-loss-prevention-on-demand/)
- [AI Gateway Sizing Guidelines](https://docs.netskope.com/en/ai-gateway-sizing-guidelines/)
