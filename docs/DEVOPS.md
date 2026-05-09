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
| Activation Lambda | `ActivationLambdaFunction` | Registers/deregisters appliances, starts Step Functions |
| Enrollment Lambda | `EnrollmentLambdaFunction` | SSH/TUI enrollment actions (VPC-attached, paramiko + pyte) |
| Step Functions | `EnrollmentStateMachine` | Orchestrates enrollment: SSH wait → TUI enrollment → lifecycle completion |
| Lambda Layer | `ParamikoLayer` | paramiko + pyte for SSH/TUI automation |
| SSH Key Pair | `SSHKeyPair` (Custom Resource) | Generates key pair for Lambda→gateway SSH access |
| Secrets Manager | `NetskopeSecret`, `SSHPrivateKeySecret` | Tenant credentials and SSH private key |

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
Activation Lambda invoked
  1. Reads Netskope API credentials from Secrets Manager
  2. Calls POST /api/v2/aig/appliances (Netskope tenant API)
     - Registers appliance with instance's private IP
     - Receives enrollment token in response (held in memory only)
  3. Stores appliance ID mapping in SSM Parameter Store
     /aig/{stack-name}/{instance-id}/appliance-id (String)
  4. Starts Step Functions execution with instance IP, token, lifecycle details
        |
        v
Step Functions state machine runs
  WaitForSSH         → SSH connect test (retry every 15s until ready)
  StartEnrollment    → SSH to nsadmin, navigate aig-cli TUI, press Enter on enrollment
  PollPreEnrollment  → Poll every 30s until "Enter enrollment token:" appears (~10-15 min)
  SubmitToken        → Enter token via TUI, wait for "Enrollment completed"
  CompleteLifecycle   → Calls CompleteLifecycleAction with CONTINUE
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

**Total time from launch to InService:** ~15-20 minutes (dominated by pre-enrollment install and enrollment polling).

**Failure handling:** If the Step Functions execution fails, the lifecycle hook's `HeartbeatTimeout` (1200s / 20 min) expires and the `DefaultResult: ABANDON` triggers, causing the ASG to terminate the instance and try again. Step Functions provides detailed per-step error logs for diagnosis.

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
Activation Lambda invoked
  1. Reads appliance ID from SSM Parameter Store
     /aig/{stack-name}/{instance-id}/appliance-id
  2. Calls DELETE /api/v2/aig/appliances/{id} (Netskope tenant API)
     - Deregisters appliance from tenant
  3. Deletes SSM parameter (appliance-id)
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

- **API credentials never touch the instance.** The Activation Lambda reads from Secrets Manager and calls the Netskope API. The enrollment token exists only in Lambda memory and is passed directly to the instance over SSH — it is never persisted to any AWS storage service.
- **Instance IAM role is minimal.** The gateway role only has CloudWatch Logs permissions. It cannot read API credentials, SSH keys, or any secrets.
- **Lambda logs are isolated.** CloudWatch logs for both Lambdas are in separate log groups with controlled retention. The API token is never logged.
- **Clean termination.** The Activation Lambda handles both launch and terminate events, deregistering the appliance from the tenant when an instance is removed.

### Secret Storage Layout

| Secret | Location | Who reads it |
|--------|----------|-------------|
| Netskope tenant URL + API token | Secrets Manager: `{stack}-netskope-credentials` | Activation Lambda only |
| SSH private key | Secrets Manager: `{stack}-ssh-private-key` | Enrollment Lambda (for SSH to gateway instances) |
| Appliance ID mapping (per instance) | SSM Parameter Store: `/aig/{stack}/{instance-id}/appliance-id` (String) | Activation Lambda (on terminate) |

### IAM Boundaries

| Role | Can access | Cannot access |
|------|-----------|---------------|
| `{stack}-gateway-role` (EC2) | CloudWatch Logs | Everything else (no Secrets Manager, no Parameter Store, no Netskope API) |
| `{stack}-activation-lambda-role` (Lambda) | Secrets Manager (Netskope creds), Parameter Store (appliance ID), ASG lifecycle, Step Functions | SSH key secret, VPC resources |
| `{stack}-enrollment-lambda-role` (Lambda, VPC) | Secrets Manager (SSH key + Netskope creds), ASG lifecycle, VPC networking | Nothing beyond what's explicitly granted |
| `{stack}-sfn-role` (Step Functions) | Invoke Enrollment Lambda | Nothing else |
| `{stack}-lifecycle-sns-role` (ASG) | Publish to the lifecycle SNS topic | Nothing else |

---

## Accessing Gateway Instances

Gateway instances run in private subnets with no direct inbound SSH access from the internet. For interactive access, use SSH via a bastion host or VPN. The `nsadmin` user drops into the aig-cli TUI menu on login.

### SSH via bastion host

```bash
# From a bastion in the same VPC
ssh -i /path/to/key.pem nsadmin@<instance-private-ip>
```

The EC2 key pair is generated at stack creation and stored in Secrets Manager (`{stack}-ssh-private-key`). Retrieve it for manual SSH access:

```bash
aws secretsmanager get-secret-value \
  --secret-id <stack>-ssh-private-key \
  --query SecretString --output text > /tmp/key.pem
chmod 400 /tmp/key.pem
```

### Note on SSM Session Manager

The SSM agent is not installed on gateway instances. Interactive access requires SSH via a bastion host, VPN, or Direct Connect.

---

## Monitoring and Troubleshooting

### Log Locations

| Component | Log location | What to look for |
|-----------|-------------|-----------------|
| Activation Lambda | CloudWatch: `/aws/lambda/{stack}-activation` | Appliance registration, Step Functions start, deregistration |
| Enrollment Lambda | CloudWatch: `/aws/lambda/{stack}-enrollment` | SSH connection, TUI screen captures, token submission |
| Step Functions | Step Functions console > Executions | Per-step status, timing, input/output, errors |
| ASG activity | ASG console > Activity tab | Launch/terminate events, lifecycle hook timeouts |

### Common Failure Scenarios

**Instance stuck in Pending:Wait**

The lifecycle hook has a 1200s (20 min) heartbeat timeout. If the instance is still in `Pending:Wait` after 15+ minutes:

1. Check the Activation Lambda logs — did it receive the SNS event? Did it register the appliance and start Step Functions?
2. Check the Step Functions execution — which state is it in? Did any state fail?
3. Check SSH connectivity — can the Enrollment Lambda reach the instance on port 22? Check security groups and VPC routing.
4. Check TUI state — SSH to the instance manually and run `aig-cli` to see enrollment progress.

```bash
# Check Step Functions execution status
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:<region>:<account>:stateMachine:<stack>-enrollment \
  --query "executions[*].{Name:name,Status:status,Start:startDate}" \
  --output table
```

**Lifecycle hook times out (ABANDON)**

If `DefaultResult: ABANDON` fires, the instance is terminated and the ASG launches a replacement. Check:

- Activation Lambda logs for errors during appliance registration or Step Functions start
- Step Functions execution history for the specific state that failed
- Enrollment Lambda logs for SSH connection errors or TUI parsing issues
- Instance may not have network connectivity (check NAT gateway, security groups, route tables)

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

# Check Step Functions execution history
aws stepfunctions get-execution-history \
  --execution-arn <execution-arn> \
  --query "events[?type=='TaskStateEntered'].stateEnteredEventDetails.name" \
  --output text

# Check enrollment Lambda logs (latest invocation)
aws logs tail /aws/lambda/<stack>-enrollment --since 30m

# Manually start a Step Functions enrollment for an instance
aws stepfunctions start-execution \
  --state-machine-arn arn:aws:states:<region>:<account>:stateMachine:<stack>-enrollment \
  --input '{"instance_ip":"<ip>","appliance_id":"<id>","enrollment_token":"<token>"}'
```

---

## VPC Requirements

### When using an existing VPC

The template creates a Secrets Manager VPC endpoint in all deployments (both new and existing VPCs). For existing VPCs, the following must also be available:

| Endpoint | Type | Required by |
|----------|------|------------|
| `com.amazonaws.<region>.secretsmanager` | Interface | Enrollment Lambda (reads SSH key from Secrets Manager) |
| `com.amazonaws.<region>.s3` | Gateway | Lambda Layer downloads |

The endpoint security groups must allow HTTPS (443) inbound from the Lambda security group or VPC CIDR. The template creates a dedicated security group (`{stack}-vpce-sg`) with rules for both.

**Important:** The Enrollment Lambda must be in private subnets with a NAT gateway for outbound internet access (needed to call `CompleteLifecycleAction` and for general AWS API access).

### When creating a new VPC

The template automatically creates the VPC with public subnets (ALB, NAT gateway), private subnets (gateways, Lambda), internet gateway, NAT gateway, S3 gateway endpoint, and Secrets Manager interface endpoint.

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
| `LambdaCodeBucket` | Yes | - | S3 bucket containing Lambda package and Layer |
| `LambdaCodeKey` | No | lambda-step-function.zip | S3 key for enrollment Lambda package |
| `LambdaLayerKey` | No | layers/pexpect-layer.zip | S3 key for paramiko/pyte Lambda Layer |
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
