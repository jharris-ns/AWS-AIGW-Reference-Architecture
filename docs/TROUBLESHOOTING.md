# Troubleshooting Guide

This guide covers using Claude Code to assist with deployment and operations, configuring AWS credentials, and diagnosing common failure scenarios.

---

## Using Claude Code with This Project

This repository includes a `CLAUDE.md` file at the project root. When you open the project in Claude Code, this file is loaded automatically, giving Claude full context about the architecture, template conventions, deployment procedures, and operational rules.

### What Claude Code Can Help With

- **Deploying stacks** — build artifacts, upload to S3, run `aws cloudformation create-stack`
- **Reading logs** — pull CloudWatch logs for the activation and enrollment Lambdas
- **Diagnosing failures** — inspect Step Functions execution history, identify failed states
- **Modifying templates** — update CloudFormation resources while following project conventions
- **Scaling operations** — adjust ASG capacity, trigger instance refresh
- **Cleanup** — delete stacks, deregister appliances from the Netskope tenant

### Example Prompts

| Task | Prompt |
|------|--------|
| Deploy a new stack | "Deploy the gateway-asg template to us-west-1 with a new VPC" |
| Check enrollment status | "Check the Step Functions execution for stack my-aigw" |
| Read Lambda logs | "Show me the last 30 minutes of logs for the enrollment Lambda in stack my-aigw" |
| Diagnose a stuck instance | "Instance i-xxx is stuck in Pending:Wait — what went wrong?" |
| Scale the cluster | "Scale my-aigw to 3 instances" |
| Clean up | "Delete the my-aigw stack and deregister any orphaned appliances" |

### Tips

- Tell Claude which AWS profile to use: "use profile my-profile" or set `AWS_PROFILE` in your shell before starting Claude Code.
- Claude Code runs CLI commands in your terminal session — it uses whatever AWS credentials are active.
- For multi-step operations (deploy, wait, verify), Claude Code will run commands in the background and notify you when they complete.

---

## AWS Credentials

Claude Code executes `aws` CLI commands in your shell. It needs valid AWS credentials to interact with your account. Choose the method that fits your organization.

### AWS IAM Identity Center / SSO (Recommended)

IAM Identity Center provides short-lived, automatically-rotated credentials. This is the recommended approach for organizations.

```bash
# Configure SSO (one-time setup)
aws configure sso
# Follow prompts: SSO start URL, region, account, role

# Login before using Claude Code
aws sso login --profile my-sso-profile
export AWS_PROFILE=my-sso-profile
```

Credentials expire after the session duration configured in Identity Center (typically 1-12 hours). Re-run `aws sso login` when they expire.

**Reference:** [Configuring AWS CLI with IAM Identity Center](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)

### Named Profiles

For environments without SSO, use named profiles in `~/.aws/credentials`:

```ini
[my-profile]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
region = us-west-1
```

```bash
export AWS_PROFILE=my-profile
```

**Reference:** [AWS CLI Named Profiles](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html)

### Assumed Roles

For cross-account deployments or restricted permissions, assume a deployment role:

```bash
# Configure a profile that assumes a role
# In ~/.aws/config:
[profile deploy]
role_arn = arn:aws:iam::123456789012:role/CloudFormationDeployRole
source_profile = my-profile
region = us-west-1
```

```bash
export AWS_PROFILE=deploy
```

**Reference:** [Assuming a Role with the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html)

### What to Avoid

- **Long-lived access keys in environment variables** — these persist in shell history and process listings. Prefer SSO or assumed roles with short-lived credentials.
- **Root account credentials** — never use root credentials for deployment. Create an IAM user or role with the minimum required permissions.

---

## IAM Permissions for Deployment

The CloudFormation stack creates IAM roles with explicit names, requiring `CAPABILITY_NAMED_IAM`. The deploying principal needs permissions across CloudFormation, EC2, ELB, Lambda, Step Functions, IAM, Secrets Manager, SNS, and CloudWatch Logs.

The full IAM policy is in the [README](../README.md#6-aws-services-and-iam-permissions).

### Verify Before Deploying

```bash
# Confirm your identity
aws sts get-caller-identity

# Validate the template (checks syntax, not permissions)
aws cloudformation validate-template \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-asg.yaml

# Dry-run the stack creation (checks permissions and parameter validation)
aws cloudformation create-change-set \
  --stack-name test-dry-run \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-asg.yaml \
  --parameters ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=test \
    ParameterKey=GatewayAmiId,ParameterValue=ami-xxx \
    ParameterKey=LambdaCodeBucket,ParameterValue=my-bucket \
  --capabilities CAPABILITY_NAMED_IAM \
  --change-set-name dry-run
```

---

## Troubleshooting Enrollment

### Decision Tree

When an instance is stuck in `Pending:Wait`:

```
Instance stuck in Pending:Wait
    |
    +-- Check Activation Lambda logs
    |   (/aws/lambda/<stack>-activation)
    |       |
    |       +-- No log entries? --> SNS not triggering Lambda (see below)
    |       +-- "401: Unauthorized"? --> Wrong API token (see "API token rejected" below)
    |       +-- Error registering appliance? --> Check Netskope API creds / permissions
    |       +-- "Started enrollment" logged? --> Continue below
    |
    +-- Check Step Functions execution
    |   (Step Functions console > <stack>-enrollment)
    |       |
    |       +-- No execution? --> Lambda failed to start it (check Lambda logs)
    |       +-- Stuck in WaitForSSH? --> SSH connectivity issue (see below)
    |       +-- Stuck in PollPreEnrollment? --> Pre-enrollment still running (wait)
    |       +-- Failed state? --> Check state input/output for error details
    |
    +-- Check Enrollment Lambda logs
        (/aws/lambda/<stack>-enrollment)
            |
            +-- "Connect timeout on secretsmanager" --> VPC endpoint issue (see below)
            +-- "SSH not ready" --> Instance still booting or SG misconfigured
            +-- "TUI detected" but enrollment fails --> Check TUI screen captures in logs
```

### Using the Step Functions Console

1. Open the **Step Functions** console in the AWS region where the stack is deployed
2. Click on the **`<stack>-enrollment`** state machine
3. Click on the execution (named `enroll-<instance-id>`)
4. The **Graph view** shows which state succeeded, failed, or is in progress
5. Click any state to see:
   - **Input** — what was passed to the Lambda
   - **Output** — what the Lambda returned (includes `enrollment_state` and `screen` captures)
   - **Exception** — error details if the state failed
6. For polling states (`PollPreEnrollment`), check the output's `screen` field to see the TUI progress

### CloudWatch Log Groups

| Log group | Component | What to look for |
|-----------|-----------|-----------------|
| `/aws/lambda/<stack>-activation` | Activation Lambda | SNS event received, appliance registered, Step Functions started, deregistration on terminate |
| `/aws/lambda/<stack>-enrollment` | Enrollment Lambda | SSH connection status, TUI screen content, enrollment state transitions, token submission |

Filter logs by request ID to isolate a single invocation:

```bash
# Tail recent logs
aws logs tail /aws/lambda/<stack>-enrollment --since 30m --follow

# Filter for errors
aws logs filter-log-events \
  --log-group-name /aws/lambda/<stack>-enrollment \
  --filter-pattern "ERROR" \
  --start-time $(date -d '1 hour ago' +%s000)
```

### Common Failures

**API token rejected (401 Unauthorized)**

| | |
|---|---|
| Symptom | Gateway instances launch and immediately get abandoned. Activation Lambda logs show `API POST /api/v2/aig/appliances -> 401: Unauthorized`. No Step Functions enrollment execution is created. The ASG enters a launch/abandon/retry loop. |
| Cause | The `NetskopeApiToken` parameter contains the wrong credential. A common mistake is using `NETSKOPE_API_TOKEN` instead of `NETSKOPE_API_KEY` from your environment — these are different credentials. The token may also be expired or lack the required RBAC v3 permissions. |
| Diagnose | Check the Activation Lambda logs: `aws logs tail /aws/lambda/<stack>-activation --since 30m`. Look for `401: Unauthorized` in the output. |
| Fix | 1. Verify the token works: `curl -sf -o /dev/null -w "HTTP %{http_code}\n" -H "Netskope-Api-Token: <token>" https://<tenant>.goskope.com/api/v2/aig/appliances`. A 200 response means the token is valid. 2. Update the stack with the correct token, or delete and redeploy. See [DEPLOYMENT.md](DEPLOYMENT.md#environment-variable-mapping) for the env var to parameter mapping. |

**SNS not triggering Lambda**

| | |
|---|---|
| Symptom | Instance stuck in `Pending:Wait`, no Activation Lambda log entries |
| Cause | ASG launched instance before SNS subscription was created (race condition) |
| Fix | The template uses `DependsOn: [LifecycleSnsSubscription, LambdaLifecyclePermission]` on the ASG to prevent this. If the template was modified and this was removed, add it back. |
| Workaround | Manually invoke the activation Lambda with the lifecycle event (see Manual Recovery below) |

**Secrets Manager timeout (Enrollment Lambda)**

| | |
|---|---|
| Symptom | Enrollment Lambda times out at 120s with `Connect timeout on endpoint URL: "https://secretsmanager.<region>.amazonaws.com/"` |
| Cause | The VPC endpoint security group does not allow HTTPS (443) from the Lambda security group |
| Fix | Verify the `VpcEndpointSecurityGroup` has an ingress rule for the Lambda SG. The template creates this automatically, but if using an existing VPC, the endpoint SG must allow traffic from the Lambda SG or VPC CIDR. |

**SSH connection refused**

| | |
|---|---|
| Symptom | Step Functions stuck in `WaitForSSH` retry loop, Enrollment Lambda logs show `SSH not ready` |
| Cause | Instance is still booting (normal for first 2-3 minutes), or the gateway security group is missing the SSH rule |
| Fix | Wait — the Step Function retries every 15 seconds. If it persists beyond 5 minutes, check that `GatewaySecurityGroup` has an ingress rule allowing port 22 from `LambdaSecurityGroup`. |

**TUI not detected after SSH connect**

| | |
|---|---|
| Symptom | Enrollment Lambda logs show SSH connected but `session.connect() returned False` |
| Cause | The TUI takes several seconds to render after SSH auth. The screen load delay may be too short, or the terminal dimensions don't match what the TUI expects. |
| Fix | The `ParamikoConfig` `screen_load_delay` defaults to 3.0 seconds. If the gateway AMI is slow to start the TUI, increase it in `step_function_handlers.py`. |

**Pre-enrollment stuck**

| | |
|---|---|
| Symptom | Step Functions cycles through `PollPreEnrollment` → `WaitForPreEnrollment` indefinitely |
| Cause | Pre-enrollment takes 10-15 minutes on a fresh gateway. The polling loop is working correctly — it's just waiting. If it exceeds 20 minutes, the gateway may have a network or AMI issue. |
| Fix | Check the TUI screen content in the Enrollment Lambda logs — it shows the pre-enrollment progress bar. If the progress is stuck at 0%, the gateway may not have internet access (check NAT gateway and route tables). |

**Token submission fails**

| | |
|---|---|
| Symptom | `SubmitToken` state fails, screen shows error or TUI returns to main menu |
| Cause | Token expired (tokens are valid for ~30 days), appliance already enrolled, or token was for a different appliance |
| Fix | Check the Netskope tenant API — is the appliance still registered? If it was deleted and re-created, the old token is invalid. Delete and re-register the appliance. |

**CompleteLifecycleAction fails**

| | |
|---|---|
| Symptom | Enrollment succeeds but instance stays in `Pending:Wait`. Enrollment Lambda logs show `Connect timeout on endpoint URL: "https://autoscaling.<region>.amazonaws.com/"` |
| Cause | The Enrollment Lambda is in a VPC without internet access (no NAT gateway). It can SSH to instances but can't reach AWS API endpoints. |
| Fix | The private subnets must route to a NAT gateway. The template creates one when deploying a new VPC. For existing VPCs, verify the private subnet route table has a `0.0.0.0/0` route to a NAT gateway. |

**Lambda Layer architecture mismatch**

| | |
|---|---|
| Symptom | Enrollment Lambda fails with `cannot execute binary file: Exec format error` or import errors |
| Cause | The Lambda Layer was built on ARM (e.g., Apple Silicon Mac) but Lambda runs on x86_64 |
| Fix | Rebuild the Layer with `--platform linux/amd64`: `podman run --rm --platform linux/amd64 --entrypoint bash -v "$PWD/scripts:/build" -w /build public.ecr.aws/lambda/python:3.12 ./build-tui-layer.sh` |

---

## Troubleshooting Stack Deployment

**Template validation error**

```bash
aws cloudformation validate-template \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-asg.yaml
```

If the template exceeds 51,200 bytes, it must be uploaded to S3 and referenced via `--template-url`. Using `--template-body` with a large file will fail.

**S3 bucket region mismatch**

| | |
|---|---|
| Symptom | `PermanentRedirect: The bucket is in this region: us-west-1. Please use this region to retry the request` |
| Cause | The S3 bucket is in a different region than the CloudFormation stack |
| Fix | The Lambda code bucket, Layer, and template must all be in the same region as the stack. Use `scripts/deploy-artifacts.sh <region>` to create and upload to the correct region. |

**ROLLBACK_FAILED cleanup**

If a stack gets stuck in `ROLLBACK_FAILED`:

```bash
# Delete with retained resources (if some resources can't be deleted)
aws cloudformation delete-stack --stack-name <stack>

# If delete also fails, force delete
aws cloudformation delete-stack --stack-name <stack> \
  --deletion-mode FORCE_DELETE_STACK
```

Check for resources that may block deletion: non-empty S3 buckets, ENIs attached to Lambda VPC functions (these clean up after ~15 minutes), or EC2 key pairs.

**VPC endpoint limit**

Each VPC has a default limit of 20 interface endpoints. If deploying multiple stacks into the same VPC, you may hit this limit. Request a limit increase through the AWS Service Quotas console.

---

## Manual Recovery Procedures

### Complete a lifecycle action manually

If the Step Functions execution fails and the instance is stuck in `Pending:Wait`:

```bash
# Complete with CONTINUE (allow instance to serve traffic)
aws autoscaling complete-lifecycle-action \
  --lifecycle-hook-name <stack>-launch-hook \
  --auto-scaling-group-name <stack>-asg \
  --instance-id <instance-id> \
  --lifecycle-action-result CONTINUE

# Or ABANDON (terminate and replace the instance)
aws autoscaling complete-lifecycle-action \
  --lifecycle-hook-name <stack>-launch-hook \
  --auto-scaling-group-name <stack>-asg \
  --instance-id <instance-id> \
  --lifecycle-action-result ABANDON
```

### Start a Step Functions enrollment manually

If the activation Lambda failed to start the Step Function, you can start it manually. You need the instance IP, appliance ID, and enrollment token:

```bash
# Get the instance private IP
aws ec2 describe-instances --instance-ids <instance-id> \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text

# Get the state machine ARN
aws stepfunctions list-state-machines \
  --query "stateMachines[?contains(name,'<stack>-enrollment')].stateMachineArn" --output text

# Start the execution
aws stepfunctions start-execution \
  --state-machine-arn <state-machine-arn> \
  --input '{
    "instance_ip": "<private-ip>",
    "appliance_id": "<appliance-id>",
    "enrollment_token": "<token>",
    "lifecycle": {
      "hook_name": "<stack>-launch-hook",
      "asg_name": "<stack>-asg",
      "action_token": "<lifecycle-action-token>"
    }
  }'
```

### Register or deregister an appliance manually

```bash
# Register
curl -s -X POST "https://<tenant>.goskope.com/api/v2/aig/appliances" \
  -H "Netskope-Api-Token: <api-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "<appliance-name>",
    "host": "<instance-ip>",
    "ports": {"https": {"port": 443, "enable": true}, "http": {"port": 80, "enable": false}}
  }'

# Generate enrollment token (if not returned in registration response)
curl -s -X POST "https://<tenant>.goskope.com/api/v2/aig/appliances/<appliance-id>/enrollmenttokens" \
  -H "Netskope-Api-Token: <api-token>"

# Deregister
curl -s -X DELETE "https://<tenant>.goskope.com/api/v2/aig/appliances/<appliance-id>" \
  -H "Netskope-Api-Token: <api-token>"

# List all appliances
curl -s "https://<tenant>.goskope.com/api/v2/aig/appliances" \
  -H "Netskope-Api-Token: <api-token>" | python3 -m json.tool
```

### SSH to an instance for manual TUI enrollment

Retrieve the SSH key from Secrets Manager and connect:

```bash
# Get the SSH private key
aws secretsmanager get-secret-value \
  --secret-id <stack>-ssh-private-key \
  --query SecretString --output text > /tmp/gw-key.pem
chmod 400 /tmp/gw-key.pem

# SSH to the instance (from a bastion in the same VPC)
ssh -i /tmp/gw-key.pem nsadmin@<instance-private-ip>

# The nsadmin user drops directly into the aig-cli TUI menu
# Navigate to "Enroll this AI Gateway" and follow the prompts

# Clean up
rm /tmp/gw-key.pem
```
