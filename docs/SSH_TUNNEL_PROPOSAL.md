# Proposal: Replace SSM with SSH Tunnel for Gateway Management

## Background

The current architecture uses AWS Systems Manager (SSM) to run enrollment and configuration commands on gateway instances after launch. Security have raised a concern about exposing the SSM control plane to the gateway instances. This document proposes replacing SSM with direct SSH access from the Lambda function, using an SSH tunnel to reach the appliance's local management APIs.

---

## How It Works Today

When the Auto Scaling Group launches a new gateway instance, a Lambda function:

1. Registers the appliance with the Netskope tenant API and receives an enrollment token
2. Writes the enrollment token to AWS SSM Parameter Store
3. Waits for the SSM agent on the instance to come online
4. Triggers an SSM Automation Document that runs a bash script on the instance

That bash script (running on the instance) does the following:

- Reads the enrollment token from Parameter Store using the AWS CLI
- Calls the appliance's local management APIs on `localhost:8080` to submit the token, poll for completion, configure DLP, and restart services

This requires the SSM agent running on every gateway instance, three SSM VPC endpoints, and IAM permissions for the instance to read from Parameter Store.

---

## How It Would Work with SSH

The Lambda function does all the same work, but instead of delegating to SSM, it connects directly to the instance over SSH and calls the local management APIs through an SSH tunnel.

### The Flow

```
1. ASG launches instance → lifecycle hook holds it in Pending:Wait

2. Lambda is invoked (same as today)
   a. Reads Netskope API credentials from Secrets Manager
   b. Reads SSH private key from Secrets Manager
   c. Registers appliance with Netskope tenant API → receives enrollment token
   d. Looks up the instance's private IP via EC2 API

3. Lambda opens an SSH connection to the instance's private IP (port 22)
   - Authenticates using the SSH private key
   - Connects as the default user on the appliance (same user that operators SSH into today)

4. Lambda opens an SSH tunnel: forwards traffic through the SSH connection
   to localhost:8080 on the instance
   - This is the same mechanism as: ssh -L 8080:localhost:8080 user@instance
   - The tunnel gives the Lambda direct access to the appliance's local
     management APIs (VAM, DMS) as if it were running on the instance

5. Lambda calls the management APIs through the tunnel:
   a. Wait for VAM service to be ready          (GET  /internal/machine-spec)
   b. Trigger pre-enrollment install             (POST /internal/pre-enrollment)
   c. Poll until install completes               (GET  /internal/pre-enrollment)
   d. Submit enrollment token                    (PUT  /enrollment)
   e. Poll until enrollment completes            (GET  /enrollment)
   f. Restart services                           (POST /internal/mgmt/restart-service)
   g. Configure DLP if applicable                (PUT  /aiapi/dlp/cert, /aiapi/dlp/hostconfig)
   h. Restart services again after DLP config

6. Lambda closes the SSH connection

7. Lambda calls CompleteLifecycleAction → instance moves to InService
```

The enrollment token never touches any AWS storage service. It exists only in Lambda memory, is passed directly to the appliance's enrollment API over the SSH tunnel, and is discarded when the Lambda finishes.

### What the SSH Tunnel Replaces

| Current (SSM) | Proposed (SSH) |
|----------------|----------------|
| Lambda writes token to Parameter Store | Lambda passes token directly over SSH tunnel |
| SSM agent reads commands from AWS control plane | Lambda connects directly to instance on port 22 |
| Bash script on instance calls `curl localhost:8080` | Lambda calls the same APIs through the SSH tunnel |
| SSM Automation completes lifecycle action | Lambda completes lifecycle action directly |
| Instance needs IAM access to Parameter Store | Instance needs no IAM access to any secrets service |

---

## SSH Key Management

### The Problem with EC2 Key Pairs

The appliance already uses standard EC2 key pairs for operator SSH access. When an instance launches with a key pair name in the Launch Template, EC2 injects the public key into the default user's `~/.ssh/authorized_keys` automatically. No UserData or AMI changes are involved.

However, EC2 key pairs are designed for human operators who download a `.pem` file once. They are not designed for programmatic access by a Lambda function:

- **Console-created key pairs**: The private key is shown once at creation and never stored by AWS. There is no API to retrieve it later.
- **CloudFormation-created key pairs** (`AWS::EC2::KeyPair`): AWS stores the private key in SSM Parameter Store under `/ec2/keypair/{key-pair-id}`. The Lambda would need `ssm:GetParameter` to read it -- reintroducing the SSM dependency.
- **Imported key pairs** (`aws ec2 import-key-pair`): You control the private key, but you must store it somewhere the Lambda can access it.

### The Solution

The stack generates its own key pair at creation time using a small helper Lambda (a CloudFormation Custom Resource):

1. Generates an RSA 4096-bit key pair
2. Stores the **private key** in Secrets Manager (where the Lambda can read it)
3. Imports the **public key** into EC2 as a named key pair (so EC2 injects it into instances at boot)

This requires **no changes to the appliance**. The instance sees a normal EC2 key pair, just like the one operators use today. The only difference is that the private key lives in Secrets Manager instead of on someone's laptop.

```
Stack creation:
  Helper Lambda generates key pair
    → private key → Secrets Manager
    → public key  → EC2 ImportKeyPair

Instance launch:
  EC2 injects public key into default user's authorized_keys
  (standard platform behavior, no UserData, no AMI change)

Enrollment:
  Activation Lambda reads private key from Secrets Manager
  Activation Lambda SSH connects to instance using that key
```

The Activation Lambda already reads Netskope API credentials from Secrets Manager. The SSH private key is a second secret in the same service, accessed with the same IAM permission, the same VPC endpoint, and the same CloudTrail audit trail.

---

## Host Requirements

### Required change: dedicated automation user

The default SSH user on the appliance is `nsadmin`. This user drops into a restricted management menu on login, which does not provide a standard shell or allow SSH port forwarding. It cannot be used for the SSH tunnel approach.

A new user is required on the AMI specifically for Lambda automation. This user must:

| Requirement | Detail |
|-------------|--------|
| Have a standard shell (e.g., `/bin/bash`) | The `nsadmin` restricted menu blocks SSH tunneling |
| Allow local TCP forwarding | sshd must permit `AllowTcpForwarding local` for this user so the Lambda can tunnel to `localhost:8080` |
| Accept EC2 key pair injection | The public key must be written to this user's `~/.ssh/authorized_keys` at boot |
| Have no sudo or root access | The user only needs to open SSH tunnels -- it never runs commands on the host |

**Note on EC2 key pair injection:** By default, EC2 injects the key pair into the `nsadmin` user (whatever user the AMI designates as default). For the new automation user to receive the key, the AMI build process must configure cloud-init or the EC2 metadata agent to also write the public key for the automation user. An alternative is to have a boot-time script that copies the key from `nsadmin`'s `authorized_keys` to the automation user's `authorized_keys`.

### Everything else already works

Once the automation user exists, the remaining capabilities are already present on the appliance:

| Capability | Why it's needed | Already present? |
|------------|----------------|-----------------|
| sshd running on port 22 | Lambda connects via SSH | Yes -- operators SSH in today |
| Management APIs on localhost:8080 | Lambda calls these through the tunnel | Yes -- the current SSM script calls the same APIs |
| TCP forwarding in sshd | SSH tunnel requires `AllowTcpForwarding` | Yes (sshd default is `AllowTcpForwarding yes`) |

### Recommended hardening (optional)

These are not required but would improve security posture:

**1. Lock down the automation user in sshd_config**

Restrict the user to tunneling only -- no interactive shell, no agent forwarding:

```
Match User lambda-automation
    AllowTcpForwarding local
    X11Forwarding no
    AllowAgentForwarding no
    PermitTTY no
    ForceCommand /usr/sbin/nologin
```

This allows the SSH tunnel (port forwarding to `localhost:8080`) but prevents the user from getting an interactive shell. The Lambda only needs the tunnel -- it never needs to execute commands directly on the host.

**2. Rate-limit or restrict SSH source**

The AWS security group already restricts port 22 to the Lambda's security group. If the appliance has its own firewall (`iptables`/`nftables`), an equivalent rule limiting SSH connections to the VPC CIDR would add defence in depth.

---

## Step Functions Replaces the Long-Running Lambda

The current SSM approach offloads the long-running enrollment work to an SSM Automation Document, which has its own execution timeout independent of the Lambda. With SSH, the Lambda itself would need to hold the connection open for the entire 10-15 minute enrollment process. AWS Lambda has a maximum timeout of 15 minutes (900 seconds), which is tight and leaves no margin for retries or slow pre-enrollment installs.

To solve this, the orchestration moves from a single long-running Lambda to an **AWS Step Functions state machine**. A lightweight dispatcher Lambda receives the lifecycle event and starts a Step Functions execution. The state machine breaks the work into discrete steps, each handled by a short-lived Lambda (seconds, not minutes). Polling loops use Step Functions `Wait` states -- no Lambda is running or paying during the wait.

```
ASG Lifecycle Hook
    → SNS → Dispatcher Lambda (starts Step Functions execution)
          → RegisterAppliance           Lambda: Netskope API call
          → WaitForSSH                  Lambda: attempt SSH, retry with Wait
          → WaitForVAM                  Lambda: SSH tunnel → GET /internal/machine-spec
          → PreEnrollmentInstall        Lambda: SSH tunnel → POST /internal/pre-enrollment
          → PollPreEnrollment           Lambda: SSH tunnel → GET, loop with Wait
          → SubmitEnrollmentToken       Lambda: SSH tunnel → PUT /enrollment
          → PollEnrollment              Lambda: SSH tunnel → GET, loop with Wait
          → RestartServices             Lambda: SSH tunnel → POST restart
          → ConfigureDLP                Conditional: SSH tunnel → cert + hostconfig
          → CompleteLifecycleAction     Lambda: ASG API call
```

Step Functions executions can run for up to one year, so the 15-minute Lambda limit is no longer a constraint. Each individual Lambda invocation opens an SSH connection, makes one or two API calls through the tunnel, and exits. The state machine handles the wait intervals, retry logic, and error paths.

This also replaces the visibility lost by removing SSM. The SSM Automation console currently shows step-by-step execution history for each instance enrollment. Step Functions provides the same visibility -- each execution is inspectable in the console with per-step status, timing, input/output, and error details.

---

## What Gets Removed

With SSH handling all instance communication, the following AWS components are no longer needed:

- **SSM agent** on the gateway instances (can be disabled or removed from the AMI)
- **SSM Automation Document** (the `GatewaySetupDocument` resource in the template)
- **Three SSM VPC endpoints** (`ssm`, `ssmmessages`, `ec2messages`) -- reduces cost and network surface
- **Instance IAM permissions** for SSM and Parameter Store -- the instance role reduces to CloudWatch logging only
- **Enrollment token in Parameter Store** -- never persisted; passed directly over the SSH tunnel

---

## Performance Metrics Without SSM

With SSM removed, the instance cannot be queried on-demand for metrics. Metrics must be pushed from the instance rather than pulled. There are several options:

### Option 1: CloudWatch Agent (recommended)

The CloudWatch agent is installed on the AMI and configured with a static JSON config file at build time. It pushes OS-level metrics (CPU, memory, disk, network) to CloudWatch on a schedule (e.g., every 60 seconds). No SSM or inbound connections required -- the agent makes outbound HTTPS calls to the CloudWatch API endpoint.

- **Instance IAM:** Requires `cloudwatch:PutMetricData` (already granted by `CloudWatchAgentServerPolicy` on the current instance role)
- **VPC requirement:** The CloudWatch VPC endpoint (`com.amazonaws.<region>.monitoring`) must exist, or the instance must have outbound internet access via NAT gateway
- **AMI change:** Agent must be installed and configured at AMI build time. No runtime configuration needed
- **Visibility:** Metrics appear in CloudWatch Metrics console, available for dashboards and alarms

### Option 2: EC2 default metrics (no agent, no AMI change)

Every EC2 instance automatically publishes hypervisor-level metrics to CloudWatch with no agent or configuration:

- CPU utilization, network in/out, disk read/write ops, status checks
- 5-minute granularity (1-minute with detailed monitoring enabled on the Launch Template)
- **Limitation:** No memory, disk usage percentage, or application-level metrics -- only what the hypervisor can observe externally

This works today with no changes and provides basic health monitoring. It does not require SSM, an agent, or any IAM permissions on the instance.

### Recommendation

Use **Option 2 (EC2 default metrics) immediately** -- it requires no changes and covers basic health monitoring. Add **Option 1 (CloudWatch agent)** to the AMI in a future release for memory, disk, and application-level metrics.

---

## Open Questions

1. **Automation user on the AMI** -- The `nsadmin` user's restricted menu rules it out. The appliance team needs to add a dedicated automation user (e.g., `lambda-automation`) to the AMI with a standard shell, local TCP forwarding, and EC2 key pair injection. What is the preferred mechanism -- cloud-init config, a boot-time key copy script, or a change to the AMI's default user configuration?

2. **Operational access** -- With SSM removed, how will operations teams access instances for troubleshooting? The `nsadmin` user and its management menu remain available via bastion host or VPN. Options: (a) SSH via bastion host using the same or separate key pair, (b) keep SSM Session Manager installed alongside SSH for human access only, (c) VPN/Direct Connect.

3. **Host key verification** -- On first SSH connection, the Lambda must accept the instance's host key without prior knowledge (trust on first connect). For stricter verification, the instance could publish its host key fingerprint via EC2 `GetConsoleOutput`. Is this level of verification required?

4. **Key algorithm** -- EC2 `ImportKeyPair` supports RSA and Ed25519. Ed25519 is faster and more modern but requires OpenSSH 6.5+ on the appliance. What version of OpenSSH does the appliance ship?