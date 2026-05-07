# AI Gateway ASG Operations Guide

This document covers the operational aspects of the Auto Scaling Gateway template (`templates/gateway-asg.yaml`). It is written for AWS DevOps engineers and SREs who will deploy, operate, and troubleshoot the stack.

---

## Architecture Overview

```
                     Internet
                        |
                   ALB (HTTPS:443)
                   ACM Certificate
                        |
                   Target Group
                    /        \
              Instance-1   Instance-2    (ASG, multi-AZ)
              (us-west-1b) (us-west-1c)
```

Each gateway instance is a Netskope AI Gateway appliance that must be individually enrolled with the Netskope tenant and configured with DLP connectivity before it can serve traffic. The ASG template automates this entirely through lifecycle hooks.

### Key Components

| Component | Resource | Purpose |
|-----------|----------|---------|
| ALB | `ApplicationLoadBalancer` | HTTPS ingress, distributes traffic across enrolled instances |
| ASG | `GatewayAutoScalingGroup` | Manages instance count, multi-AZ placement |
| Launch Template | `GatewayLaunchTemplate` | Instance configuration (AMI, type, SG, IAM) |
| Lifecycle Hooks | `LaunchLifecycleHook`, `TerminateLifecycleHook` | Hold instances in wait state during enrollment/cleanup |
| SNS Topic | `LifecycleSnsTopic` | Delivers lifecycle events to the Lambda |
| Lambda | `ActivationLambdaFunction` | Registers/deregisters appliances, triggers SSM automation |
| SSM Document | `GatewaySetupDocument` | On-instance enrollment and DLP configuration script |
| Secrets Manager | `NetskopeSecret` | Stores tenant URL and API token |

---

## Instance Lifecycle

### Launch Flow

When the ASG launches a new instance (initial deployment, scale-out, or instance replacement), the following sequence executes automatically:

```
ASG launches instance
        |
        v
Lifecycle hook: Pending:Wait
        |
        v
SNS delivers EC2_INSTANCE_LAUNCHING event
        |
        v
Lambda invoked
  1. Reads Netskope API credentials from Secrets Manager
  2. Calls POST /api/v2/aig/appliances (Netskope tenant API)
     - Registers appliance with instance's public IP
     - Receives enrollment token in response
  3. Writes enrollment token to SSM Parameter Store
     /aig/{stack-name}/{instance-id}/enrollment-token (SecureString)
  4. Stores appliance ID mapping
     /aig/{stack-name}/{instance-id}/appliance-id (String)
  5. Waits for SSM agent to come online (polls every 15s, 240s timeout)
  6. Starts SSM Automation execution (fire-and-forget)
        |
        v
SSM Automation runs on the instance
  Step 1: RunSetupScript
    - Waits for VAM service (internal appliance manager)
    - Triggers pre-enrollment install (~5-10 min on fresh instance)
    - Reads enrollment token from Parameter Store
    - Submits token to local DMS service
    - Polls enrollment until status=completed
    - Restarts appliance services
    - Configures DLP (cert fetch + host config) if DlpHostUrl provided
    - Restarts services again after DLP config
  Step 2: CheckLifecycleHook
    - Branches: if lifecycle hook params present, continue; else skip
  Step 3: CompleteLifecycleAction
    - Calls autoscaling:CompleteLifecycleAction with CONTINUE
        |
        v
Instance moves to InService
        |
        v
ALB health check passes (HTTPS 443)
        |
        v
Instance receives traffic
```

**Total time from launch to InService:** ~10-15 minutes (dominated by pre-enrollment install and enrollment polling).

**Failure handling:** If the SSM automation fails, the lifecycle hook's `HeartbeatTimeout` (900s) expires and the `DefaultResult: ABANDON` triggers, causing the ASG to terminate the instance and try again.

### Terminate Flow

When the ASG terminates an instance (scale-in, instance refresh, or stack deletion):

```
ASG begins terminating instance
        |
        v
Lifecycle hook: Terminating:Wait
        |
        v
SNS delivers EC2_INSTANCE_TERMINATING event
        |
        v
Lambda invoked
  1. Reads appliance ID from SSM Parameter Store
     /aig/{stack-name}/{instance-id}/appliance-id
  2. Calls DELETE /api/v2/aig/appliances/{id} (Netskope tenant API)
     - Deregisters appliance from tenant
  3. Deletes SSM parameters (enrollment-token and appliance-id)
  4. Calls CompleteLifecycleAction with CONTINUE
        |
        v
Instance terminated
```

**Total time:** ~5 seconds. The `DefaultResult: CONTINUE` with a 300s timeout means the instance will be terminated even if the Lambda fails.

---

## Scaling Operations

### Scale Out (Add Instances)

```bash
# Set desired count
aws autoscaling update-auto-scaling-group \
  --auto-scaling-group-name <stack-name>-asg \
  --desired-capacity <N>

# Or set a new max and desired together
aws autoscaling update-auto-scaling-group \
  --auto-scaling-group-name <stack-name>-asg \
  --max-size 5 \
  --desired-capacity 3
```

Each new instance goes through the full launch lifecycle above. Instances will not receive ALB traffic until enrollment completes and the lifecycle action is completed.

### Scale In (Remove Instances)

```bash
aws autoscaling update-auto-scaling-group \
  --auto-scaling-group-name <stack-name>-asg \
  --desired-capacity <N>
```

The ASG selects instances to terminate based on its [termination policy](https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-termination-policies.html). Each terminated instance goes through the terminate lifecycle, deregistering from the Netskope tenant.

### Instance Refresh (Rolling Replacement)

To replace all instances (e.g., after an AMI update):

```bash
aws autoscaling start-instance-refresh \
  --auto-scaling-group-name <stack-name>-asg \
  --preferences '{"MinHealthyPercentage": 50}'
```

This terminates and replaces instances in batches. Each new instance is fully enrolled before the next batch is terminated.

---

## Secrets Management

### Why Lambda Instead of UserData

The Netskope tenant API requires an API token (`NetskopeApiToken`) to register appliances and generate enrollment tokens. There are two possible approaches:

**Approach 1: UserData script on the instance (rejected)**

The instance would fetch the API token from Secrets Manager or Parameter Store, then call the Netskope API directly via `curl`. This approach has several problems:

- **API credentials on the instance.** The token must be fetched to the instance's memory and used in a `curl` command. Even with `set +x`, credentials can leak through:
  - SSM RunCommand output (captured in CloudWatch and the SSM console, capped at 24KB but often includes the full script output)
  - `/var/log/cloud-init-output.log` (UserData output is logged)
  - Process listings (`ps aux` shows command arguments)
  - Core dumps
- **Instance IAM role needs Secrets Manager access.** Every gateway instance would need `secretsmanager:GetSecretValue` on the API credential secret. A compromised instance could read the tenant API token.
- **No cleanup on termination.** UserData only runs on launch. There is no built-in mechanism to deregister the appliance when an instance is terminated.

**Approach 2: Lambda handles the API call (implemented)**

The Lambda runs in its own execution environment, completely separate from the gateway instances:

- **API credentials never touch the instance.** The Lambda reads from Secrets Manager and calls the Netskope API. Only the enrollment token (a one-time-use value specific to that instance) is written to Parameter Store for the instance to consume.
- **Instance IAM role is minimal.** The gateway role only has `ssm:GetParameter` scoped to its own enrollment token path (`/aig/{stack}/*/enrollment-token`). It cannot read the API credentials.
- **Lambda logs are isolated.** CloudWatch logs for the Lambda are in a separate log group with controlled retention. The API token is never logged (the Lambda only logs appliance IDs and status messages).
- **Clean termination.** The same Lambda handles both launch and terminate events, deregistering the appliance from the tenant when an instance is removed.

### Secret Storage Layout

| Secret | Location | Who reads it |
|--------|----------|-------------|
| Netskope tenant URL + API token | Secrets Manager: `{stack}-netskope-credentials` | Lambda only |
| Enrollment token (per instance) | SSM Parameter Store: `/aig/{stack}/{instance-id}/enrollment-token` (SecureString) | Gateway instance |
| Appliance ID mapping (per instance) | SSM Parameter Store: `/aig/{stack}/{instance-id}/appliance-id` (String) | Lambda (on terminate) |

### IAM Boundaries

| Role | Can access | Cannot access |
|------|-----------|---------------|
| `{stack}-gateway-role` (EC2) | Own enrollment token in Parameter Store | Netskope API credentials in Secrets Manager |
| `{stack}-activation-lambda-role` (Lambda) | Secrets Manager, Parameter Store, SSM automation, ASG lifecycle | Nothing beyond what's explicitly granted |
| `{stack}-lifecycle-sns-role` (ASG) | Publish to the lifecycle SNS topic | Nothing else |

---

## Accessing Gateway Instances

Gateway instances run in private subnets with no direct inbound SSH access from the internet. Use AWS Systems Manager Session Manager to connect — no bastion host, SSH key, or open security group port required.

### Start an interactive session

```bash
aws ssm start-session --target <instance-id>
```

This opens a shell on the instance through the SSM agent, which is already installed in the AI Gateway AMI and authorized by the instance IAM role.

### Run a one-off command

```bash
aws ssm send-command \
  --instance-ids <instance-id> \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl status aig-*"]' \
  --output text \
  --query "Command.CommandId"

# Retrieve the output
aws ssm get-command-invocation \
  --command-id <command-id> \
  --instance-id <instance-id> \
  --query "StandardOutputContent" \
  --output text
```

### Requirements

- The VPC must have the SSM interface endpoints (`ssm`, `ssmmessages`, `ec2messages`) — the template creates these automatically for new VPCs. For existing VPCs, see [VPC Requirements](#vpc-requirements).
- The `KeyPairName` parameter is optional and only needed if you require traditional SSH access through a bastion host or VPN.

---

## Monitoring and Troubleshooting

### Log Locations

| Component | Log location | What to look for |
|-----------|-------------|-----------------|
| Lambda | CloudWatch: `/aws/lambda/{stack}-activation` | Appliance registration, token writes, SSM agent wait, automation start |
| SSM Automation | SSM console > Automation executions | Step status (RunSetupScript, CheckLifecycleHook, CompleteLifecycleAction) |
| Instance setup script | CloudWatch: `/aws/ssm/AWS-RunShellScript` and on-instance at `/var/log/aig-automation.log` | Pre-enrollment progress, enrollment polling, DLP config |
| ASG activity | ASG console > Activity tab | Launch/terminate events, lifecycle hook timeouts |

### Common Failure Scenarios

**Instance stuck in Pending:Wait**

The lifecycle hook has a 900s heartbeat timeout. If the instance is still in `Pending:Wait` after several minutes:

1. Check the Lambda logs — did it receive the SNS event? Did it start the automation?
2. Check the SSM automation execution — is `RunSetupScript` still running or did it fail?
3. Check if the SSM agent came online — the Lambda polls for 240s. If the agent never registers, the VPC may be missing SSM endpoints.

```bash
# Check automation status
aws ssm get-automation-execution \
  --automation-execution-id <exec-id> \
  --query "AutomationExecution.{Status:AutomationExecutionStatus,Steps:StepExecutions[*].{Name:StepName,Status:StepStatus}}"
```

**Lifecycle hook times out (ABANDON)**

If `DefaultResult: ABANDON` fires, the instance is terminated and the ASG launches a replacement. Check:

- Lambda CloudWatch logs for errors during appliance registration
- SSM automation output for script failures (enrollment timeout, DLP cert issues)
- Instance may not have network connectivity (check VPC endpoints, security groups, route tables)

**Lambda receives TEST_NOTIFICATION errors**

These are normal — ASG sends test notifications when the SNS subscription is created. The Lambda skips them automatically. You'll see `Skipping test notification` in the logs.

**DLP host config fails**

The DLP certificate fetch requires network connectivity from the gateway instance to the DLPoD server. If the DLPoD is in a different VPC, you need VPC peering or place the gateway in the same VPC. The script will fail if it can't reach the DLPoD on port 443.

### Useful Commands

```bash
# Check ASG instance states
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack>-asg \
  --query "AutoScalingGroups[0].Instances[*].{Id:InstanceId,State:LifecycleState,AZ:AvailabilityZone}" \
  --output table

# Check recent scaling activity
aws autoscaling describe-scaling-activities \
  --auto-scaling-group-name <stack>-asg \
  --max-items 5 \
  --query "Activities[*].{Status:StatusCode,Description:Description}" \
  --output table

# Check SSM automation executions for a stack
aws ssm describe-automation-executions \
  --filters Key=DocumentNamePrefix,Values=<stack>-setup \
  --query "AutomationExecutionMetadataList[*].{Id:AutomationExecutionId,Status:AutomationExecutionStatus,Start:ExecutionStartTime}" \
  --output table

# Check DLP config on a running instance via SSM
aws ssm send-command \
  --instance-ids <instance-id> \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["curl -s http://aig-dp-mgmt-service.aig-dp.svc.cluster.local:8080/aiapi/dlp/config | jq ."]'

# Check enrollment status on a running instance
aws ssm send-command \
  --instance-ids <instance-id> \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["curl -s http://aig-dp-mgmt-service.aig-dp.svc.cluster.local:8080/enrollment | jq ."]'

# Manually re-run the setup automation on an instance
aws ssm start-automation-execution \
  --document-name <stack>-setup \
  --parameters "InstanceId=<instance-id>,StackName=<stack>,DlpHostUrl=<url>"
```

---

## VPC Requirements

### When using an existing VPC

The template does not create VPC endpoints when deploying into an existing VPC. The following endpoints must exist in the VPC for the stack to function:

| Endpoint | Type | Required by |
|----------|------|------------|
| `com.amazonaws.<region>.ssm` | Interface | SSM agent on gateway instances |
| `com.amazonaws.<region>.ssmmessages` | Interface | SSM Session Manager / RunCommand |
| `com.amazonaws.<region>.ec2messages` | Interface | SSM agent communication |
| `com.amazonaws.<region>.secretsmanager` | Interface | Lambda (if running in VPC) |
| `com.amazonaws.<region>.s3` | Gateway | General AWS SDK operations |

The endpoint security groups must allow HTTPS (443) inbound from the VPC CIDR or from the gateway security group.

### When creating a new VPC

The template automatically creates all required VPC endpoints with a dedicated security group (`{stack}-vpce-sg`) allowing HTTPS from the VPC CIDR.

---

## Stack Parameters Reference

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `NetskopeTenantUrl` | Yes | - | Netskope tenant URL (e.g., `https://tenant.goskope.com`) |
| `NetskopeApiToken` | Yes | - | API token (NoEcho, stored in Secrets Manager) |
| `DlpHostUrl` | No | '' | DLP host URL; empty skips DLP configuration |
| `AcmCertificateArn` | Yes | - | ACM certificate ARN for ALB HTTPS listener |
| `ExistingVpcId` | No | '' | Existing VPC ID; empty creates a new VPC |
| `ExistingSubnetId` | No | '' | Existing subnet ID (AZ 1); empty creates new |
| `ExistingSubnet2Id` | No | '' | Existing subnet ID (AZ 2); empty creates new |
| `VpcCidr` | No | 10.0.0.0/16 | CIDR for new VPC |
| `PublicSubnetCidr` | No | 10.0.1.0/24 | CIDR for new subnet 1 |
| `PublicSubnet2Cidr` | No | 10.0.2.0/24 | CIDR for new subnet 2 |
| `GatewayAmiId` | No | ami-0010b83013995a493 | Netskope AI Gateway AMI |
| `InstanceType` | No | m5.4xlarge | Instance type (16 vCPU, 64 GiB minimum) |
| `KeyPairName` | No | - | SSH key pair (optional, for bastion/VPN access) |
| `MinCapacity` | No | 1 | ASG minimum instances |
| `MaxCapacity` | No | 3 | ASG maximum instances |
| `DesiredCapacity` | No | 1 | ASG desired instances |
| `Project` | No | netskope-ai-gateway | Project tag value |
| `Environment` | No | dev | Environment tag value |

---

## Stack Outputs

| Output | Description | Typical use |
|--------|-------------|------------|
| `ALBDnsName` | ALB DNS name | Point DNS (Route 53 alias or CNAME) to this |
| `ALBHostedZoneId` | ALB hosted zone ID | Required for Route 53 alias records |
| `AutoScalingGroupName` | ASG name | Scaling operations, monitoring |
| `VpcId` | VPC ID (created or existing) | Cross-stack references |
| `PublicSubnetId` | Subnet 1 ID (created or existing) | Cross-stack references |
| `GatewaySecurityGroupId` | Gateway security group | Adding rules for additional consumers |
