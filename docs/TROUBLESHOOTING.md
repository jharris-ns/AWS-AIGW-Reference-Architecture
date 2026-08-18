# Troubleshooting Guide — AI Gateway + DLP On Demand

Issue/Cause/Solution reference for `templates/gateway-combined.yaml`. Start with the
[Diagnostic Commands](#diagnostic-commands) section to gather state, then jump to the
specific issue.

## Table of Contents

- [Diagnostic Commands](#diagnostic-commands)
- [AI Gateway Issues](#ai-gateway-issues)
- [DLP On Demand Issues](#dlp-on-demand-issues)
- [Certificate Issues](#certificate-issues)
- [Stack Issues](#stack-issues)
- [Log Patterns Reference](#log-patterns-reference)

---

## Diagnostic Commands

Run these to understand the current state before diagnosing a specific issue.

```bash
STACK=<stack-name>
REGION=<region>

# Stack status
aws cloudformation describe-stacks --stack-name $STACK \
  --query 'Stacks[0].StackStatus' --output text --region $REGION

# All stack outputs at once
aws cloudformation describe-stacks --stack-name $STACK \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table --region $REGION

# AIG ASG instance states
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names $STACK-aig-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region $REGION

# DLPoD ASG instance states
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names $STACK-dlpod-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table --region $REGION

# DLPoD tethering Step Functions executions
DLPOD_SFN=$(aws cloudformation describe-stacks --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='DlpodTetheringStateMachineArn'].OutputValue" \
  --output text --region $REGION)
aws stepfunctions list-executions --state-machine-arn $DLPOD_SFN \
  --query "executions[*].[name,status,startDate]" --output table --region $REGION

# AIG bootstrap secret contents
aws secretsmanager get-secret-value \
  --secret-id $STACK-aig-bootstrap \
  --query SecretString --output text --region $REGION

# AIG activation Lambda logs (last 15 min)
aws logs tail /aws/lambda/$STACK-aig-activation --since 15m --region $REGION

# DLPoD activation Lambda logs (last 30 min)
aws logs tail /aws/lambda/$STACK-dlpod-activation --since 30m --region $REGION

# DLPoD tethering Lambda logs (last 30 min)
aws logs tail /aws/lambda/$STACK-dlpod --since 30m --region $REGION
```

---

## AI Gateway Issues

### Issue: AIG instance stuck in `Pending:Wait`

**Cause:** The AIG lifecycle hook heartbeat is 120 seconds. The Activation Lambda must register
the appliance with the Netskope API and complete the lifecycle action within that window.

**Diagnosis:**
```bash
aws logs tail /aws/lambda/$STACK-aig-activation --since 10m --region $REGION
```

**Common causes and solutions:**

| Log pattern | Cause | Solution |
|---|---|---|
| `401 Unauthorized` or `403 Forbidden` | `NetskopeApiToken` is wrong, expired, or lacks AIG Administrator permissions | Verify token in Netskope portal: **Settings → Administration → Administrators & Roles → Administrators** |
| `ConnectionError` or `timeout` | Lambda cannot reach Netskope API (NAT Gateway or routing issue) | Check NAT Gateway is in `available` state; check private subnet route table has route to NAT GW |
| `Parameter /stack/dlpod-cert not found` | Cert generator custom resource failed at stack creation | See [Certificate Issues — DLPoD cert missing from SSM](#issue-dlpod-cert-missing-from-ssm) |
| `ResourceNotFoundException` on bootstrap secret | Bootstrap secret not created | Check CloudFormation events for failure on `AigBootstrapSecret` resource |

If the Lambda fails, the instance is ABANDONED within 2 minutes and a replacement launches
automatically. Check that the underlying issue is resolved before the replacement arrives.

---

### Issue: AIG instance ABANDONED after 2 minutes

**Cause:** The Activation Lambda failed or timed out during the 120-second lifecycle hook window.

**Diagnosis:**
```bash
# Check Lambda errors
aws logs tail /aws/lambda/$STACK-aig-activation --since 30m --region $REGION

# Check CloudFormation events for error context
aws cloudformation describe-stack-events --stack-name $STACK --region $REGION \
  --query "StackEvents[?ResourceStatus=='CREATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
  --output table
```

**Solution:** The ASG automatically launches a replacement. If replacements are also being
ABANDONED (systematic failure), resolve the root cause — typically a bad API token, network
connectivity, or missing SSM parameter — before further replacements launch.

---

### Issue: AIG ALB target stuck unhealthy

**Cause:** The instance is `InService` in the ASG but the ALB health check (HTTPS GET / on port 443) fails.

**Diagnosis:**
```bash
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'$STACK-aig-tg')].TargetGroupArn" \
  --output text --region $REGION)
aws elbv2 describe-target-health --target-group-arn $TG_ARN \
  --query "TargetHealthDescriptions[*].[Target.Id,TargetHealth.State,TargetHealth.Description]" \
  --output table --region $REGION
```

**Common causes:**

| Health check state | Likely cause | Solution |
|---|---|---|
| `initial` | Instance just launched; enrollment not yet complete | Wait 5–15 minutes from instance launch |
| `unhealthy` - connection refused | AIG service not running or enrollment incomplete | Check AIG activation Lambda logs; enrollment may have failed |
| `unhealthy` - timeout | Security group not allowing AIG ALB SG → AIG instance port 443 | Check `AigToDlpodAlbIngress` resource in CloudFormation |

---

### Issue: AIG enrolled but DLP inspection not working

**Cause:** DLP On Demand ALB has no healthy targets, the AIG bootstrap secret is missing the DLP
block, or the security group cross-reference is broken.

**Diagnosis — step by step:**

**Step 1: Check DLPoD ALB has healthy targets**
```bash
# Get DLPoD target group ARN
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'$STACK-dlpod-tg')].TargetGroupArn" \
  --output text --region $REGION)
aws elbv2 describe-target-health --target-group-arn $TG_ARN \
  --query "TargetHealthDescriptions[*].[Target.Id,TargetHealth.State]" \
  --output table --region $REGION
```

If there are no healthy DLPoD targets, DLP inspection fails. Wait for DLPoD tethering to complete
(up to 25 minutes from stack creation). See [DLP On Demand Issues](#dlp-on-demand-issues).

**Step 2: Verify AIG bootstrap secret contains the DLP block**
```bash
aws secretsmanager get-secret-value \
  --secret-id $STACK-aig-bootstrap \
  --query SecretString --output text --region $REGION | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('dlp',{}), indent=2))"
```

The output should contain `certificate` and `host` keys. If the DLP block is empty or missing,
the cert generator custom resource failed. See [Certificate Issues](#certificate-issues).

**Step 3: Verify security group allows AIG → DLPoD ALB**
```bash
# Get the DLPoD ALB security group ID from CloudFormation resources
aws cloudformation list-stack-resources --stack-name $STACK --region $REGION \
  --query "StackResourceSummaries[?LogicalResourceId=='DlpodAlbSg'].PhysicalResourceId" \
  --output text

# Check its ingress rules
aws ec2 describe-security-group-rules \
  --filters Name=group-id,Values=<dlpod-alb-sg-id> \
  --query "SecurityGroupRules[?!IsEgress].[IpProtocol,FromPort,ToPort,ReferencedGroupInfo.GroupId]" \
  --output table --region $REGION
```

The AIG instance SG should appear as a source for port 443.

---

## DLP On Demand Issues

### Issue: DLPoD instance stuck in `Pending:Wait`

**Cause:** The DLPoD lifecycle hook heartbeat is 1800 seconds (30 minutes). Tethering automation
runs via Step Functions. If the Step Functions execution hasn't started or is stuck, the instance
waits until the heartbeat times out.

**Diagnosis:**
```bash
# Check if a Step Functions execution started
aws stepfunctions list-executions --state-machine-arn $DLPOD_SFN \
  --query "executions[*].[name,status,startDate]" --output table --region $REGION

# Check DLPoD activation Lambda logs
aws logs tail /aws/lambda/$STACK-dlpod-activation --since 30m --region $REGION
```

If no execution exists for the instance, the Activation Lambda failed to start Step Functions.
Check the activation Lambda logs for errors — typically a permissions issue or missing state
machine ARN.

---

### Issue: Step Functions execution FAILED

Each Step Functions state corresponds to a tethering step. Identifying which state failed narrows
the cause.

```bash
# Get execution ARN
EXEC_ARN=$(aws stepfunctions list-executions --state-machine-arn $DLPOD_SFN \
  --query "executions[?status=='FAILED'].executionArn | [0]" --output text --region $REGION)

# Get execution history with error details
aws stepfunctions get-execution-history --execution-arn $EXEC_ARN \
  --query "events[?type=='TaskFailed' || type=='ExecutionFailed'].[type,taskFailedEventDetails.error,taskFailedEventDetails.cause]" \
  --output table --region $REGION

# Check tethering Lambda logs
aws logs tail /aws/lambda/$STACK-dlpod --since 60m --region $REGION
```

**Failure by state:**

| State | Likely cause | Solution |
|---|---|---|
| `WaitForDlpodSSH` | Instance not yet accepting SSH, or DLPoD Lambda cannot reach instance on port 22 | Check DLPoD Lambda SG allows outbound to DLPoD instance SG port 22; check DLPoD instance SG allows inbound port 22 from Lambda SG |
| `DlpodChangePassword` | SSH connected but CLI automation failed | Check DLPoD Lambda logs for `pexpect` timeout or unexpected CLI output |
| `DlpodSetDNS` | DNS configuration step failed | Check Lambda logs; DNS server value (`DnsServer` parameter) must match VPC CIDR base + 2 |
| `DlpodSetLicense` | License key invalid or Secrets Manager access failed | Verify `DlpodLicenseKey` in Netskope portal: **Settings → Security Cloud Platform → On-Premises Infrastructure** |
| `CheckDlpodTethering` | DLPoD instance cannot reach Netskope management plane | Check NAT Gateway; verify DLPoD instance SG allows outbound; check DLPoD Lambda logs for tethering status |
| `DlpodCompleteLifecycle` | `CompleteLifecycleAction` failed | Check DLPoD Lambda role has `autoscaling:CompleteLifecycleAction` permission |

---

### Issue: DLPoD ALB target unhealthy after tethering completes

**Cause:** Step Functions execution shows `SUCCEEDED` but the DLPoD ALB health check fails.

**Diagnosis:**
```bash
aws elbv2 describe-target-health --target-group-arn $TG_ARN \
  --query "TargetHealthDescriptions[*].[Target.Id,TargetHealth.State,TargetHealth.Description]" \
  --output table --region $REGION
```

**Common causes:**

| State | Cause | Solution |
|---|---|---|
| `initial` | ALB just saw the target; health check in progress | Wait 2–3 minutes |
| `unhealthy` - connection refused | DLPoD service not fully started after tethering | Wait an additional 3–5 minutes; DLP service may still be initializing |
| `unhealthy` - timeout | DLPoD ALB SG not allowing outbound to DLPoD instance port 443 | Check DLPoD ALB SG egress rules |

---

## Certificate Issues

### Issue: DLPoD cert missing from SSM (`/<stack>/dlpod-cert`)

**Cause:** The `CertGeneratorFunction` custom resource failed during stack creation.

**Diagnosis:**
```bash
# Check CloudFormation events for the cert generator resource
aws cloudformation describe-stack-events --stack-name $STACK --region $REGION \
  --query "StackEvents[?LogicalResourceId=='DlpodAlbCertificate'].[ResourceStatus,ResourceStatusReason]" \
  --output table

# Check cert generator Lambda logs
aws logs tail /aws/lambda/$STACK-cert-generator --since 60m --region $REGION
```

**Common causes:**

| Log pattern | Cause | Solution |
|---|---|---|
| `openssl: command not found` | Lambda environment does not have openssl | This indicates a Lambda runtime issue; check the Lambda layer or runtime configuration |
| `AccessDenied` on `acm:ImportCertificate` | Cert generator Lambda role missing ACM permissions | Check `<stack>-cert-generator-role` policy |
| `AccessDenied` on `ssm:PutParameter` | Cert generator Lambda role missing SSM permissions | Check `<stack>-cert-generator-role` policy |

**Impact:** If the cert generator fails, the DLP block is not written to the AIG bootstrap secret.
AIG instances that boot without the DLP block will enroll without DLP forwarding configured. Fix
the cert generator issue and trigger a stack update to re-run the custom resource.

---

### Issue: AIG cannot verify DLPoD TLS certificate

**Cause:** The DLPoD ALB cert PEM in the bootstrap secret does not match the cert currently on the
DLPoD ALB, or the cert is missing from the bootstrap secret.

**Diagnosis:**
```bash
# Check if dlp block exists in bootstrap secret
aws secretsmanager get-secret-value \
  --secret-id $STACK-aig-bootstrap \
  --query SecretString --output text --region $REGION | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print('DLP block present' if d.get('dlp') else 'DLP block MISSING')"

# Check SSM parameter has the cert
aws ssm get-parameter --name /$STACK/dlpod-cert \
  --query "Parameter.Value" --output text --region $REGION | head -3
```

If the SSM parameter exists but the bootstrap secret's DLP block doesn't match, the cert generator
wrote the cert but the Activation Lambda read an old value. Trigger a new instance launch via
scale-out to refresh the bootstrap secret.

---

## Stack Issues

### Issue: Stack stuck at `CREATE_IN_PROGRESS` for more than 30 minutes

**Cause:** A custom resource or Lambda is not responding to CloudFormation.

**Diagnosis:**
```bash
# Find the stuck resource
aws cloudformation describe-stack-events --stack-name $STACK --region $REGION \
  --query "StackEvents[?ResourceStatus=='CREATE_IN_PROGRESS'].[LogicalResourceId,ResourceType,Timestamp]" \
  --output table

# If the stuck resource is a custom resource, check the cert generator logs
aws logs tail /aws/lambda/$STACK-cert-generator --since 60m --region $REGION
```

**Common causes:**

| Stuck resource | Cause | Solution |
|---|---|---|
| `DlpodAlbCertificate` or `AigAlbCertificate` | Cert generator Lambda failed and did not send a response to CloudFormation | CloudFormation waits up to 1 hour for a custom resource response; check Lambda logs; the stack will roll back after timeout |
| `GatewayAutoScalingGroup` | ASG waiting for lifecycle hook to complete | Check AIG activation Lambda logs |
| `DlpodAutoScalingGroup` | ASG waiting for lifecycle hook to complete | Check DLPoD activation Lambda logs and Step Functions |

---

### Issue: Stack deletion hangs

**Cause:** Lifecycle hooks are still active (instance in `Terminating:Wait`) during stack deletion.

**Diagnosis:**
```bash
# Check for instances still in Terminating:Wait
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names $STACK-aig-asg $STACK-dlpod-asg \
  --query "AutoScalingGroups[*].Instances[?LifecycleState=='Terminating:Wait'].[InstanceId,LifecycleState]" \
  --output table --region $REGION
```

**Solution:** If instances are stuck in `Terminating:Wait`, the termination lifecycle hook Lambda
failed. Force-complete the lifecycle action:

```bash
INSTANCE_ID=<stuck-instance-id>
ASG_NAME=<stack>-aig-asg   # or dlpod-asg

aws autoscaling complete-lifecycle-action \
  --lifecycle-hook-name $ASG_NAME-launch-hook \
  --auto-scaling-group-name $ASG_NAME \
  --lifecycle-action-result CONTINUE \
  --instance-id $INSTANCE_ID \
  --region $REGION
```

Stack deletion typically completes 8–15 minutes after the `delete-stack` command, dominated by
NAT Gateway and VPC deletion.

---

## Log Patterns Reference

### AIG Activation Lambda (`/aws/lambda/<stack>-aig-activation`)

**Successful launch:**
```
[INFO] Lifecycle event received: instance-id=i-abc123 transition=autoscaling:EC2_INSTANCE_LAUNCHING
[INFO] Reading API credentials from Secrets Manager
[INFO] Calling Netskope API to register appliance
[INFO] Appliance registered: appliance_id=xxxxxxxx
[INFO] Writing enrollment token to bootstrap secret
[INFO] Completing lifecycle action: CONTINUE
```

**Successful termination:**
```
[INFO] Lifecycle event received: instance-id=i-abc123 transition=autoscaling:EC2_INSTANCE_TERMINATING
[INFO] Deregistering appliance from Netskope: appliance_id=xxxxxxxx
[INFO] Deleting SSM parameter: /<stack>/appliances/i-abc123
[INFO] Completing lifecycle action: CONTINUE
```

**Failure patterns:**
```
[ERROR] Failed to get API credentials: AccessDeniedException
[ERROR] Netskope API returned 401 Unauthorized
[ERROR] SSM parameter /<stack>/dlpod-cert not found
[ERROR] Timeout waiting for Netskope API response
```

---

### DLPoD Tethering Lambda (`/aws/lambda/<stack>-dlpod`)

**Successful tethering:**
```
[INFO] Connecting to DLPoD instance at 10.0.10.x:22
[INFO] SSH connected
[INFO] Changing password
[INFO] Password changed successfully
[INFO] Configuring DNS: 10.0.0.2
[INFO] DNS configured
[INFO] Reading license key from Secrets Manager
[INFO] Applying license key
[INFO] License applied
[INFO] Waiting for tethering to initialize (120s)
[INFO] Checking tethering status (attempt 1)
[INFO] Tethering complete
[INFO] Completing lifecycle action: CONTINUE
```

**Failure patterns:**
```
[ERROR] SSH connection refused (instance may not be ready yet)
[ERROR] pexpect timeout waiting for CLI prompt
[ERROR] Tethering check failed after 10 attempts
[ERROR] License key rejected: Invalid license
[ERROR] DNS configuration failed: unexpected CLI output
```

---

### Step Functions Execution States

Navigate to the Step Functions console → State Machines → `<stack>-dlpod-tethering` →
Executions to view the visual execution graph and per-state input/output.

| State | Description | Typical duration |
|---|---|---|
| `WaitForDlpodSSH` | Polls SSH every 25s until instance accepts connection | 5–8 minutes |
| `DlpodChangePassword` | Changes default password via CLI | <30 seconds |
| `MergePassword` | Internal state transition | <1 second |
| `DlpodSetDNS` | Configures DNS resolver | <30 seconds |
| `DlpodSetLicense` | Applies license key | <60 seconds |
| `WaitForDlpodTetheringInit` | Fixed 120s wait for DLPoD callhome | 2 minutes |
| `CheckDlpodTethering` | Polls tethering status every 60s | 5–15 minutes |
| `DlpodCompleteLifecycle` | Completes lifecycle hook | <5 seconds |
| **Total** | | **15–25 minutes** |
