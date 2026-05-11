# CLAUDE.md

Project instructions for Claude Code — deployment and operations.

For development and template modification guidelines, see [CLAUDE_DEV.md](CLAUDE_DEV.md).

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

## Deployment

Build Lambda artifacts and deploy:

```bash
# Build and upload Lambda packages to S3
scripts/deploy-artifacts.sh us-west-1

# Deploy with a new VPC (omit Existing* parameters)
aws cloudformation create-stack \
  --stack-name <name> \
  --template-body file://templates/gateway-asg.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=AcmCertificateArn,ParameterValue=<acm-arn> \
    ParameterKey=GatewayAmiId,ParameterValue=<ami-id> \
    ParameterKey=LambdaCodeBucket,ParameterValue=<s3-bucket> \
  --capabilities CAPABILITY_NAMED_IAM
```

The template is under 51KB and can be deployed directly with `--template-body` — no S3 upload needed for the template itself. Only the Lambda packages and Layer need S3.

See [DEPLOYMENT.md](docs/DEPLOYMENT.md) for full instructions including existing VPC deployments and artifact updates.

## Operations Quick Reference

| Task | Command |
|------|---------|
| Check instance states | `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <stack>-asg --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState]" --output table` |
| Check enrollment progress | `aws stepfunctions list-executions --state-machine-arn arn:aws:states:<region>:<account>:stateMachine:<stack>-enrollment --output table` |
| Scale out | `aws autoscaling update-auto-scaling-group --auto-scaling-group-name <stack>-asg --desired-capacity <N>` |
| View enrollment logs | `aws logs tail /aws/lambda/<stack>-enrollment --since 30m` |
| View activation logs | `aws logs tail /aws/lambda/<stack>-activation --since 30m` |

## Rules

- **Lambda artifacts must be uploaded to S3** before deployment. The S3 bucket must be in the same region as the stack. The template itself is under 51KB and can be deployed directly with `--template-body`.
- **CUDA NVIDIA GPU required** for advanced AI guardrails — standard guardrails work on CPU instances (m5.4xlarge), but advanced guardrails need GPU instances (g4dn, g5).

## Documentation

| Document | Purpose |
|----------|---------|
| [README.md](README.md) | Prerequisites, parameters, deployment |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Build artifacts, upload to S3, deploy |
| [DEVOPS.md](docs/DEVOPS.md) | Lifecycle, scaling, secrets, VPC requirements |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Claude Code usage, AWS credentials, failure diagnosis, manual recovery |
| [CERTIFICATE_MANAGEMENT.md](docs/CERTIFICATE_MANAGEMENT.md) | ACM certificate creation and import |
| [CLAUDE_DEV.md](CLAUDE_DEV.md) | Template conventions, resource inventory, development rules |

## Related Resources

- [Netskope AI Gateway Documentation](https://docs.netskope.com/en/ai-gateway/)
- [Netskope RBAC V3 Overview](https://docs.netskope.com/en/netskope-rbac-v3-overview/)
- [DLP On Demand Documentation](https://docs.netskope.com/en/data-loss-prevention-on-demand/)
- [AI Gateway Sizing Guidelines](https://docs.netskope.com/en/ai-gateway-sizing-guidelines/)
