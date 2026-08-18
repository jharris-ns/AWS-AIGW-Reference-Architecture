# DLPoD Gateway — Operations Guide

Operations reference for `templates/gateway-dlpod.yaml` — DLPoD appliances in an ASG, tethered
automatically via Step Functions SSH/CLI automation (paramiko).

---

## Architecture

```
                      VPC (10.0.0.0/16)
                      ┌──────────────────────────────────────────────────┐
Internet              │  Public subnets (AZ1/AZ2)                        │
    │                 │  ┌──────────┐   ┌──────────┐                     │
    └──── NAT EIP ────┤  │ Public   │   │ Public   │                     │
                      │  │ AZ1      │   │ AZ2      │                     │
                      │  └────┬─────┘   └────┬─────┘                     │
                      │       └──── NAT GW ───┘                           │
                      │  Private subnets (AZ1/AZ2)                       │
                      │  ┌─────────────────────────────────────────┐     │
                      │  │ Internal ALB (Scheme: internal)          │     │
                      │  │  HTTPS:443 → DLPoD Target Group         │     │
                      │  └──────────┬─────────────┬────────────────┘     │
                      │             │             │                        │
                      │      DLPoD-1 (ASG)  DLPoD-2 ...                  │
                      │      Pending:Wait         InService                │
                      └──────────────────────────────────────────────────┘

Route 53 private zone: aigw.internal
  dlp.aigw.internal → Internal ALB (alias)

Activation Lambda (not VPC-attached)
  ← SNS lifecycle events
  → starts Step Functions execution per instance

Tethering Lambda (VPC-attached, paramiko)
  ← Step Functions task invocations
  → SSH into DLPoD instance for each tethering step
```

---

## Tethering flow

When the ASG launches a new DLPoD instance:

```
1. Instance enters Pending:Wait (lifecycle hook fires, HeartbeatTimeout: 1800s)
2. SNS → Activation Lambda (~1–3 seconds)
   a. Gets instance private IP from EC2
   b. Starts Step Functions execution: tether-<instance-id>
      Input: {dlpod_ip, password: "nsappliance", dns_primary, lifecycle: {...}}
3. Step Functions orchestrates tethering:
   a. WaitForDlpodSSH       → polls SSH every 25s until reachable (typically 3–8 min)
   b. DlpodChangePassword   → SSHs in, generates random 24-char password, changes it
   c. MergePassword         → Pass state: propagates new password through execution
   d. DlpodSetDNS           → SSHs in, configures VPC DNS resolver
   e. DlpodSetLicense       → SSHs in, reads license key from Secrets Manager, applies it
   f. WaitForDlpodTetheringInit → waits 120s for DLPoD to initiate callhome
   g. CheckDlpodTethering   → polls tethering status every 60s until tethered
   h. DlpodCompleteLifecycle → calls CompleteLifecycleAction: CONTINUE
4. Instance moves to InService
5. ALB health check passes (HTTPS GET / on port 443)
   → instance enters ALB InService pool
```

Total time from lifecycle hook to ALB healthy: typically **15–25 minutes**, dominated by DLPoD
boot time (~5–8 min) and tethering initiation (~2 min).

> **Lifecycle hook heartbeat:** 1800 seconds (30 minutes). The tethering state machine typically
> completes in 10–20 minutes. If tethering fails before the heartbeat expires, the instance is
> ABANDONED and terminated — no manual intervention required.

### Password handling

Each DLPoD instance gets a unique randomly generated password (24 chars, alphanumeric + `!@#%+=_-`).
The password is generated during `DlpodChangePassword` and carried through the Step Functions
execution state in memory. It is never written to Secrets Manager, SSM, or logs. After tethering
completes, the password is no longer needed — DLPoD configuration is managed through the Netskope
tenant.

### License key

The license key is stored in a Secrets Manager secret (`<stack>-dlpod-credentials`) and read at
runtime by the tethering Lambda. It is never stored in Lambda environment variables or logs (the
key is redacted in all log output).

---

## Key operational commands

| Task | Command |
|---|---|
| Instance lifecycle states | `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <stack>-dlpod-asg --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" --output table` |
| Step Functions executions | `aws stepfunctions list-executions --state-machine-arn <sfn-arn> --query "executions[*].[name,status,startDate]" --output table` |
| ALB target health | `aws elbv2 describe-target-health --target-group-arn <tg-arn> --output table` |
| Activation Lambda logs | `aws logs tail /aws/lambda/<stack>-dlpod-activation --since 30m` |
| Tethering Lambda logs | `aws logs tail /aws/lambda/<stack>-dlpod --since 30m` |
| DLPoD credentials secret | `aws secretsmanager get-secret-value --secret-id <stack>-dlpod-credentials --query SecretString --output text` |
| DLPoD ALB cert (SSM) | `aws ssm get-parameter --name /<stack>/dlpod-cert --query Parameter.Value --output text` |

**Get state machine ARN from stack outputs:**

```bash
SFN_ARN=$(aws cloudformation describe-stacks --stack-name <stack> \
  --query "Stacks[0].Outputs[?OutputKey=='TetheringStateMachineArn'].OutputValue" \
  --output text --region <region>)
```

---

## Scaling

### Manual scale-out

```bash
# Check current capacity
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack>-dlpod-asg \
  --query "AutoScalingGroups[0].[MinSize,MaxSize,DesiredCapacity]" \
  --output table --region <region>

# Add one instance
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name <stack>-dlpod-asg \
  --desired-capacity <current+1> \
  --region <region>
```

Each new instance goes through the full tethering flow (~15–25 minutes). Unlike AIG, multiple
DLPoD instances can tether concurrently — each has its own Step Functions execution and its own
password. There is no shared bootstrap secret contention.

### Scale-in

```bash
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name <stack>-dlpod-asg \
  --desired-capacity <new-count> \
  --region <region>
```

The termination lifecycle hook fires but the activation Lambda only calls
`CompleteLifecycleAction: CONTINUE` for terminations — no tethering cleanup is performed.

### Update MaxSize or MinSize

```bash
aws cloudformation update-stack \
  --stack-name <stack> \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-dlpod.yaml \
  --parameters \
    ParameterKey=DlpodLicenseKey,UsePreviousValue=true \
    ParameterKey=LambdaCodeBucket,UsePreviousValue=true \
    ParameterKey=DlpodMaxCapacity,ParameterValue=4 \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

---

## IAM roles

| Role | Principal | Purpose |
|---|---|---|
| `<stack>-dlpod-role` | ec2.amazonaws.com | DLPoD instance role: CloudWatch Agent |
| `<stack>-dlpod-activation-role` | lambda.amazonaws.com | Read instance IP, start Step Functions execution, complete termination lifecycle |
| `<stack>-dlpod-sfn-role` | states.amazonaws.com | Invoke tethering Lambda |
| `<stack>-dlpod-lambda-role` | lambda.amazonaws.com | SSH to DLPoD (VPC-attached), read license from Secrets Manager, write cert to SSM, complete launch lifecycle |
| `<stack>-dlpod-lifecycle-sns-role` | autoscaling.amazonaws.com | Publish lifecycle events to SNS |
| `<stack>-cert-generator-role` | lambda.amazonaws.com | Generate self-signed cert, import to ACM, write to SSM |

---

## Secrets and SSM parameters

| Resource | Who reads it | Contents |
|---|---|---|
| `<stack>-dlpod-credentials` (Secrets Manager) | Tethering Lambda | `{"license_key": "..."}` |
| `/<stack>/dlpod-cert` (SSM Parameter) | AIG activation Lambda / combined stack | PEM-encoded self-signed cert for the DLPoD ALB |

The ALB cert is generated at stack creation time by the `CertGeneratorFunction` Custom Resource.
It is a self-signed certificate valid for 10 years. The PEM is written to SSM so that AIG can
include it in the bootstrap secret's `dlp.certificate` field, enabling AIG to trust the DLPoD ALB
without a public CA.

---

## Troubleshooting

### Instance stuck in `Pending:Wait`

The Step Functions execution is running. Check which state it's in:

```bash
aws stepfunctions get-execution-history \
  --execution-arn <execution-arn> \
  --query 'events[-5:].[type,stateEnteredEventDetails.name,taskFailedEventDetails.error,taskFailedEventDetails.cause]' \
  --output table --region <region>
```

Common states and wait times:
- `WaitForDlpodSSH` — normal for first 5–8 minutes (DLPoD is booting)
- `WaitForDlpodTetheringInit` — 120-second fixed wait after license key is set
- `CheckDlpodTethering` → retrying — DLPoD hasn't tethered yet; retries every 60s

### SSH `timed out` in tethering Lambda logs

```
SSH not ready on DLPoD 10.0.10.x: timed out
```

Normal if seen in the first 10 minutes. If still appearing after 15 minutes:

**Check 1 — Security group allows SSH from Lambda:**
```bash
aws ec2 describe-security-groups \
  --group-ids <dlpod-sg-id> \
  --query "SecurityGroups[0].IpPermissions[?ToPort==\`22\`]"
```
The DLPoD SG should allow port 22 from the Lambda security group.

**Check 2 — Instance is running:**
```bash
aws ec2 describe-instances --instance-ids <instance-id> \
  --query "Reservations[0].Instances[0].[State.Name,PrivateIpAddress]" \
  --output table
```

**Check 3 — NAT gateway is routing outbound traffic** (Lambda needs internet for AWS APIs):
```bash
aws ec2 describe-nat-gateways \
  --query "NatGateways[?VpcId=='<vpc-id>'].[NatGatewayId,State]" \
  --output table
```

### Step Functions execution `FAILED`

```bash
aws stepfunctions get-execution-history \
  --execution-arn <execution-arn> \
  --query 'events[?type==`ExecutionFailed` || type==`TaskFailed`].[type,executionFailedEventDetails,taskFailedEventDetails]' \
  --output json --region <region>
```

Common causes:
- `DlpodChangePassword` failed — password complexity not met or SSH connected but CLI didn't respond. The tethering Lambda uses `CLISession` which drives the `nsappliance>` prompt.
- `DlpodSetLicense` failed — license key is wrong or expired. Verify in the Netskope console.
- `CheckDlpodTethering` failing repeatedly — DLPoD can't reach Netskope callhome. Verify the DLPoD instance has outbound internet (NAT gateway route). Default DNS must be resolvable.

### Instance ABANDONED after 30 minutes

The lifecycle hook timed out — tethering did not complete within 1800 seconds. The ASG terminates
the instance and launches a replacement. The replacement goes through tethering from scratch.

If this is happening repeatedly, check:
1. Lambda logs for the failure state
2. Whether DLPoD boot is consistently slow (may need a larger instance type)
3. Whether tethering itself is failing (check the Step Functions execution)

### ALB target stuck `unhealthy`

DLPoD opens port 443 only after tethering completes. `unhealthy` with `Target.Timeout` means
the instance tethered (lifecycle hook completed) but the ALB health check can't connect.

```bash
# Check DLPoD SG allows 443 from ALB SG
aws ec2 describe-security-groups \
  --group-ids <dlpod-sg-id> \
  --query "SecurityGroups[0].IpPermissions[?ToPort==\`443\`]"
```

### Checking tethering status manually via SSH

If you need to verify tethering from the instance itself:

```bash
# SSH to DLPoD (requires being in the same VPC or using SSM Session Manager)
ssh nsadmin@<private-ip>
# At the nsappliance> prompt:
show dlp status
```

Look for `tethered: yes` or equivalent in the output.
