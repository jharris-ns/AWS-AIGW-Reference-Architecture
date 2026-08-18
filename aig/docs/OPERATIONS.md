# AIG Gateway — Operations Guide

Operations reference for `aig/template/gateway-aig.yaml` — AIG-only deployment with native Secrets
Manager bootstrap, step scaling, and ALB health-check gating.

---

## Architecture

```
Internet
    |
    v
ALB (HTTPS:443, ACM cert)          ← public subnets (2 AZs)
    |
    v
Target Group (HTTPS health check, path /, codes 200-499)
    |                  |
Instance-1         Instance-2   ...   (ASG, private subnets, multi-AZ)
                                      ↓ NAT gateway → internet
                                      ↓ reads Secrets Manager at boot
                                      ↓ enrolls with Netskope cloud

Each instance UserData:  {"bootstrap_secret": "<stack>-aig-bootstrap"}
Bootstrap secret:        {"bootstrap": true, "enrollment_token": "<jwt>"}

Activation Lambda  ← not VPC-attached; calls Netskope REST API and
                     Secrets Manager directly over the internet

VPC:  created by the stack OR supplied via ExistingVpcId parameter
```

---

## Enrollment flow

When the ASG launches a new instance:

```
1. Instance enters Pending:Wait (lifecycle hook fires, HeartbeatTimeout: 120s)
2. SNS → Activation Lambda (~3–10 seconds)
   a. POST /api/v2/aig/appliances  →  appliance_id + enrollment_token
   b. SSM PutParameter /aig/<stack>/<instance-id> = appliance_id
   c. SecretsManager PutSecretValue <stack>-aig-bootstrap
      → {"bootstrap": true, "enrollment_token": "<token>"}
   d. CompleteLifecycleAction: CONTINUE
3. Instance moves to InService
4. AIG appliance reads bootstrap secret at boot, calls Netskope with token
5. Netskope enrolls appliance → status: not-registered → connected
6. AIG begins serving HTTPS on port 443
7. ALB health check passes (HTTPS GET / → 2xx-499)
   → instance enters ALB InService pool
```

Total time from lifecycle hook to ALB healthy: typically 8–15 minutes, depending on instance
boot time and Netskope enrollment latency.

> **Lifecycle hook heartbeat:** The launch hook has a 120-second timeout. The activation Lambda
> completes in ~3–10 seconds under normal conditions. If the Lambda fails to call
> `CompleteLifecycleAction` within 120 seconds, the hook defaults to `ABANDON` and the instance
> is terminated.

### Why the ALB health check gates enrollment (Option A)

The AIG appliance only opens port 443 after successfully connecting to Netskope. The ALB HTTPS
health check therefore fails until enrollment completes — unenrolled instances are never added to
the pool. This eliminates the need for Step Functions polling.

### Bootstrap secret sharing constraint

All instances in the ASG read the same Secrets Manager secret name (set at Launch Template
creation time, part of UserData). Only one enrollment token can be in the secret at a time.
The step scaling policy is intentionally set to `ScalingAdjustment: 1` to prevent concurrent
launches from overwriting each other's token.

**Never manually set desired capacity +2 or more in a single operation.** Increase by 1,
wait for the instance to reach InService, then increase again.

---

## Scaling

### Automatic scale-out

A CloudWatch alarm (`<stack>-high-cpu`) monitors average CPU utilisation across the ASG:
- **Metric:** `CPUUtilization` (namespace `AWS/EC2`, dimension `AutoScalingGroupName`)
- **Trigger:** Average > `ScaleOutCpuThreshold` (default 70%) for **2 consecutive 5-minute periods**
- **Action:** Step scaling policy adds **1 instance**

The `EstimatedInstanceWarmup: 600` on the scaling policy prevents the alarm from firing again
during the new instance's enrollment period.

### Manual scale-out

```bash
# Check current state first
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack>-asg \
  --query "AutoScalingGroups[0].[MinSize,MaxSize,DesiredCapacity]" \
  --output table --region <region>

# Scale up by 1
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name <stack>-asg \
  --desired-capacity <current+1> \
  --region <region>
```

Wait for the new instance to reach InService before scaling again.

### Scale-in

Scale-in is manual only (no scale-in policy). To reduce capacity:

```bash
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name <stack>-asg \
  --desired-capacity <new-count> \
  --region <region>
```

The termination lifecycle hook fires, the activation Lambda deregisters the appliance from Netskope,
then the instance terminates.

---

## Key operational commands

| Task | Command |
|---|---|
| Instance lifecycle states | `aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <stack>-asg --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" --output table` |
| ALB target health | `aws elbv2 describe-target-health --target-group-arn <tg-arn> --output table` |
| Activation Lambda logs | `aws logs tail /aws/lambda/<stack>-activation --since 30m` |
| Bootstrap secret (check token written) | `aws secretsmanager get-secret-value --secret-id <stack>-aig-bootstrap --query SecretString --output text` |
| Netskope appliance list | `curl -s -H "Netskope-Api-Token: <token>" https://<tenant>.goskope.com/api/v2/aig/appliances` |
| CPU alarm state | `aws cloudwatch describe-alarms --alarm-names <stack>-high-cpu --query "MetricAlarms[0].[StateValue,StateReason]" --output table` |
| Force alarm evaluation | `aws cloudwatch set-alarm-state --alarm-name <stack>-high-cpu --state-value ALARM --state-reason test` |

---

## Troubleshooting

### ALB target stuck `unhealthy` with `Target.Timeout`

TCP connection to port 443 is timing out. Port 443 is only open after AIG has completed enrollment.

**Check 1 — AMI version:**
```bash
INSTANCE_ID=$(aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <stack>-asg \
  --query "AutoScalingGroups[0].Instances[0].InstanceId" --output text)
aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query "Reservations[0].Instances[0].ImageId" --output text
aws ec2 describe-images --image-ids <ami-id> \
  --query "Images[0].Name" --output text
```
AMI must be **v1.7 or later**. Earlier versions do not support native Secrets Manager bootstrap
and will never enroll, cycling through ASG replacements every ~10 minutes.

**Check 2 — Activation Lambda logs:**
```bash
aws logs tail /aws/lambda/<stack>-activation --since 60m
```
Expect: `Registered appliance <id> for <instance-id>, completing CONTINUE`

If absent: Lambda never fired. Check SNS subscription and Lambda permission.

If present with error: check the error message — Netskope API connectivity, invalid token, etc.

**Check 3 — Bootstrap secret has a token:**
```bash
aws secretsmanager get-secret-value --secret-id <stack>-aig-bootstrap \
  --query SecretString --output text
```
The `enrollment_token` field should be a non-empty JWT. If it's an empty string, the Lambda
failed to write it (check Lambda logs).

**Check 4 — Appliance status in Netskope:**
```bash
curl -s -H "Netskope-Api-Token: <token>" \
  https://<tenant>.goskope.com/api/v2/aig/appliances | \
  python3 -c "import json,sys; [print(a['name'], a['status']) for a in json.load(sys.stdin)['data']]"
```
- `connected` — enrolled, AIG should be serving. If ALB still unhealthy, check security groups.
- `not-registered` — appliance created but token not yet consumed. Wait a few minutes; if stuck
  for >15 minutes, the instance may have read an old/wrong token (check if concurrent launches
  raced to write the secret).
- Missing entirely — Lambda failed to register the appliance.

**Check 5 — Security groups:**
```bash
# Gateway SG should allow HTTPS from AlbSecurityGroup
aws ec2 describe-security-groups \
  --group-ids <gateway-sg-id> \
  --query "SecurityGroups[0].IpPermissions"
```

### Activation Lambda errors

**`An error occurred (AccessDeniedException)`** — IAM policy doesn't cover the resource. Check
the `ActivationLambdaRole` policy statements. Common cause: SSM parameter path mismatch
(stack name changed, or region mismatch).

**`URLError: <urlopen error [Errno -2] Name or service not known>`** — Lambda can't resolve the
Netskope tenant hostname. Lambda is not VPC-attached, so this means the Lambda's underlying
compute has no internet. This is rare but can happen if AWS's managed environment has issues —
retry the failed lifecycle action.

**`KeyError: 'enrollment_token'`** — Netskope API returned an appliance without an enrollment
token field. Try fetching the token separately:
`GET /api/v2/aig/appliances/{id}/enrollmenttokens`

### ASG keeps replacing instances (cycling)

Symptom: Lambda logs show a new instance registered every ~10–12 minutes, each deregistered
shortly after.

Cause: the instance is failing the ALB health check after the `HealthCheckGracePeriod` (600 seconds).
ASG terminates the unhealthy instance and launches a replacement.

Root cause is almost always one of:
1. Wrong AMI version (v1.3.x — fix: update stack with correct AMI)
2. AIG can't read the bootstrap secret (no route to Secrets Manager — verify NAT or VPC endpoint)
3. Two instances launched concurrently and raced to write the secret (avoid concurrent scale-out)

### Orphaned SSM parameters after failed termination

If the termination hook fails, the appliance ID SSM parameter at `/aig/<stack>/<instance-id>`
may persist. Clean up:

```bash
aws ssm get-parameters-by-path --path /aig/<stack>/ --region <region>
aws ssm delete-parameter --name /aig/<stack>/<instance-id> --region <region>
```

---

## IAM roles

| Role | Principal | Purpose |
|---|---|---|
| `<stack>-gateway-role` | ec2.amazonaws.com | Instance role: CloudWatch Agent + read bootstrap secret |
| `<stack>-activation-role` | lambda.amazonaws.com | Write bootstrap secret, SSM appliance ID, complete lifecycle, call Netskope API |
| `<stack>-lifecycle-sns-role` | autoscaling.amazonaws.com | Publish lifecycle events to SNS |

---

## Secrets

| Secret | Who reads it | Contents |
|---|---|---|
| `<stack>-netskope-credentials` | Activation Lambda only | `{"tenant_url": "...", "api_token": "..."}` |
| `<stack>-aig-bootstrap` | AIG appliance at boot | `{"bootstrap": true, "enrollment_token": "<jwt>"}` |

The enrollment token is an appliance-specific JWT. It is written by the activation Lambda on each
instance launch and consumed (once) by the AIG appliance. After enrollment, the token in the
secret is stale — this is expected.
