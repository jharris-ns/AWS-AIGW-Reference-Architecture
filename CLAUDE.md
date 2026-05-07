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
2. **SNS** delivers lifecycle event to **Lambda**
3. **Lambda** reads Netskope API credentials from Secrets Manager, registers appliance with tenant API, writes enrollment token to SSM Parameter Store, waits for SSM agent, starts SSM Automation
4. **SSM Automation Document** runs on-instance: pre-enrollment install, token retrieval, enrollment submission, DLP configuration, service restart
5. **SSM Automation** calls `CompleteLifecycleAction` → instance moves to `InService`
6. On termination: Lambda deregisters appliance from tenant, cleans up SSM parameters

### Secret Handling

Netskope API credentials never touch the gateway instances. The Lambda reads from Secrets Manager and only writes the one-time enrollment token to SSM Parameter Store (SecureString). The instance IAM role can only read its own enrollment token — not the API credentials.

## Directory Structure

```
templates/
  gateway-asg.yaml       # CloudFormation template (single file, all resources)
                         # Lambda function is inline (ZipFile) in the template
                         # SSM Automation Document script is inline in the template
docs/
  ASG_README.md          # Cloud architect guide — prerequisites, parameters, deployment
  DEVOPS.md              # Operations guide — lifecycle, scaling, secrets, troubleshooting
  CERTIFICATE_MANAGEMENT.md  # ACM certificate creation and import (Mac/Win/Linux)
```

## Key Resources in the Template

| Resource | Type | Purpose |
|----------|------|---------|
| `GatewayAutoScalingGroup` | AutoScaling::AutoScalingGroup | Instance management with inline lifecycle hooks |
| `GatewayLaunchTemplate` | EC2::LaunchTemplate | Instance config (AMI, type, SG, IAM, EBS) |
| `ApplicationLoadBalancer` | ELBv2::LoadBalancer | Internet-facing HTTPS ingress (public subnets) |
| `ActivationLambdaFunction` | Lambda::Function | Appliance registration, SSM automation trigger |
| `GatewaySetupDocument` | SSM::Document | On-instance enrollment + DLP config script |
| `NetskopeSecret` | SecretsManager::Secret | Tenant URL + API token |
| `LifecycleSnsTopic` | SNS::Topic | Lifecycle hook → Lambda delivery |

## Template Conventions

- **YAML only**, two-space indent
- All named resources use `!Sub '${AWS::StackName}-<role>'`
- All taggable resources have `Project` (from parameter), `Environment`, `ManagedBy: CloudFormation`
- IAM follows least-privilege — separate statements per permission grant, no `Resource: '*'` except where required (DescribeInstanceInformation, DescribeInstances)
- Sensitive values stored in Secrets Manager; instances only access their own enrollment token via SSM Parameter Store SecureString
- Supports both new VPC (creates public/private subnets, IGW, NAT gateway, VPC endpoints) and existing VPC (user provides public + private subnet IDs)
- Lambda function is inline (`ZipFile`) — if it exceeds ~3,500 chars, move to packaged deployment
- SSM Document bash script uses plain `|` block scalar (NOT `!Sub`) to avoid CloudFormation variable interpolation corrupting shell variables

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
  --capabilities CAPABILITY_NAMED_IAM
```

## Rules

- **Do not use `!Sub` in the SSM Document script block** — it corrupts bash variable syntax (`${VAR}` gets interpreted as CloudFormation references). Use plain `|` block scalar. SSM Automation parameters (`{{ ParamName }}`) handle variable injection.
- **Do not store API credentials on instances** — the Lambda handles all Netskope API calls. Instances only receive the one-time enrollment token.
- **Lifecycle hooks must be inline** on the ASG (`LifecycleHookSpecificationList`), not separate resources — separate resources create a race condition where the ASG launches instances before the hooks exist.
- **SSM Automation parameters don't accept empty strings** — omit optional parameters from `start_automation_execution` rather than passing `['']`.
- **Template must be uploaded to S3** before deployment (exceeds 51KB inline limit). Bucket must be in the same region as the stack.
- **Do not hardcode AMI IDs, key pair names, or IP addresses** in documentation — these are environment-specific and passed as parameters.
- **CUDA NVIDIA GPU required** for advanced AI guardrails — standard guardrails work on CPU instances (m5.4xlarge), but advanced guardrails need GPU instances (g4dn, g5).
- **Keep the CloudFormation skill conventions** — `Project` and `Environment` as required tag parameters, `!Ref Project` in all tags, explicit IAM policies with no `Action: '*'`, VPC endpoints when creating a new VPC.
- **When modifying the Lambda code**, keep both `handle_lifecycle_event` (ASG mode) and `handle_cfn_event` (single-instance mode) handlers — the same Lambda code is shared with the single-instance `gateway.yaml` template.

## Related Resources

- [Netskope AI Gateway Documentation](https://docs.netskope.com/en/ai-gateway/)
- [Netskope RBAC V3 Overview](https://docs.netskope.com/en/netskope-rbac-v3-overview/)
- [DLP On Demand Documentation](https://docs.netskope.com/en/data-loss-prevention-on-demand/)
- [AI Gateway Sizing Guidelines](https://docs.netskope.com/en/ai-gateway-sizing-guidelines/)
