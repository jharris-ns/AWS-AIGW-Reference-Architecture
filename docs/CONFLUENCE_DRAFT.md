# Netskope AI Gateway — AWS Reference Architecture

**Audience:** Internal Product Management and Sales Engineering  
**Purpose:** Explain how the AWS deployment works, why each AWS design decision was made, and how to answer common customer security and cost questions. Links to GitHub for technical depth.

---

## Table of Contents

1. [What Gets Deployed](#1-what-gets-deployed)
2. [Why CloudFormation](#2-why-cloudformation)
3. [Network Design — Keeping Servers Off the Internet](#3-network-design--keeping-servers-off-the-internet)
4. [Network Architecture — The Layout and How Traffic Moves](#4-network-architecture--the-layout-and-how-traffic-moves)
5. [Credential Security — How We Handle API Tokens and License Keys](#5-credential-security--how-we-handle-api-tokens-and-license-keys)
6. [IAM — Limiting What Each Component Can Do](#6-iam--limiting-what-each-component-can-do)
7. [Automated Enrollment — How Instances Configure Themselves](#7-automated-enrollment--how-instances-configure-themselves)
8. [High Availability — What Happens When Something Fails](#8-high-availability--what-happens-when-something-fails)
9. [AWS Best Practices — The Short Answer](#9-aws-best-practices--the-short-answer)
10. [What Permissions Are Needed to Deploy](#10-what-permissions-are-needed-to-deploy)
11. [Cost](#11-cost)
12. [Technical Documentation](#12-technical-documentation)

---

## 1. What Gets Deployed

A single CloudFormation template (`templates/gateway-combined.yaml`) creates the complete environment for running AI Gateway and DLP On Demand in AWS. It builds the private network, launches and sizes the servers, configures all security policies, generates TLS certificates, wires the two services together, and enrolls both appliances with the Netskope tenant — automatically, in the correct order.

After the deployment completes (15–20 minutes), both services are enrolled and serving traffic. No engineer needs to log into any server or run any configuration commands.

**What the template creates:**

- A private AWS network (VPC) with subnets across two data centers for redundancy
- An internet-facing HTTPS entry point (load balancer) for the AI Gateway
- AI Gateway server fleet, automatically enrolled with Netskope at launch
- DLP On Demand server fleet, automatically tethered at launch, running entirely inside the customer's AWS account
- All firewall rules, encryption certificates, DNS records, access policies, and automation required to operate both services

Three templates are available depending on the deployment scenario:

| Template | Use Case |
|---|---|
| **Combined** (recommended) | AI Gateway + DLP On Demand + optional AI Guardrails, single deployment, services wired together automatically |

> Standalone templates for AIG-only or DLPoD-only deployments are in a separate repository: [AWS-POV-Templates-CFT](https://github.com/jharris-ns/AWS-POV-Templates-CFT).

---

## 2. Why CloudFormation

**Customer question: Why are you using a template instead of just setting this up manually?**

CloudFormation is Amazon's infrastructure-as-code service. Instead of an engineer clicking through the AWS console to create servers, networks, and policies, everything is described in a file and deployed as a single command. The same template produces the same environment every time.

**Why this matters for customers:**

- **Repeatability.** A customer can deploy into a second region, or a second account (production vs staging), with identical configuration. There is no risk of human error producing a different result.
- **Auditability.** Every resource created — the server, the firewall rule, the access policy — is defined in the template file, which can be version-controlled and reviewed. Security teams can read the template to understand exactly what was built, rather than clicking around the console.
- **Consistency.** The template encodes security defaults that would otherwise require documentation and manual discipline. An engineer deploying manually might forget a firewall rule or mistype a permission. The template cannot.
- **Clean teardown.** Deleting the CloudFormation stack removes everything it created — no orphaned resources left behind.

**Is this the AWS best practice?** Yes. AWS explicitly recommends infrastructure-as-code over manual console deployments for any workload that needs to be reproducible, auditable, or operated by more than one person.

---

## 3. Network Design — Keeping Servers Off the Internet

### The Security Concern

If a server has a public IP address and is reachable directly from the internet, every port it exposes is a potential attack surface. Attackers scan the entire public internet for open ports continuously. A server with a public IP running on any cloud provider will receive probing attempts within minutes of launch.

For an AI Gateway or DLP On Demand appliance, a direct internet path would allow:
- Brute-force attacks against any exposed service
- Exploitation of any unpatched vulnerability in the appliance software
- Attempts to bypass the gateway's own inspection by connecting to it directly

### How the Design Addresses This

All AI Gateway and DLP On Demand servers run in **private subnets** — sections of the network that have no direct internet path. There is no route from the internet to any server. The only way to reach an AIG server is through the load balancer, which only forwards HTTPS traffic on port 443.

The architecture has two distinct network tiers:

**Public subnets** hold only the AI Gateway load balancer — which accepts HTTPS traffic from applications and forwards it to the private servers. The load balancer has no operating system to attack and runs no application code; it is a managed AWS service.

**Private subnets** hold all servers — both AI Gateway and DLP On Demand. Servers here have no public IP addresses. They cannot be reached from the internet by any path.

**Outbound internet access** (needed for servers to call Netskope APIs and LLM providers) goes through a **NAT Gateway** — a managed AWS component in the public subnet that allows outbound connections from private servers without opening any inbound path. Outbound traffic exits on the NAT Gateway's IP; the servers' private IPs are never exposed.

**DLP On Demand is double-isolated.** Its load balancer is internal-only — it can only be reached from within the same private network. DLP inspection traffic travels from AI Gateway servers to DLP On Demand servers entirely inside the customer's AWS network and never touches the internet.

### Is This AWS Best Practice?

Yes. The AWS Well-Architected Framework Security Pillar ([SEC05-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_network_protection_create_layers.html)) explicitly states: *"Place workloads in private subnets unless they require direct inbound internet access."* The AI Gateway load balancer is the only component that requires internet access; everything else is private.

**In plain terms for a customer:** The servers running your AI Gateway and DLP On Demand cannot be reached from the internet. The only open door is the load balancer, which only speaks HTTPS.

---

## 4. Network Architecture — The Layout and How Traffic Moves

### 4.1 Architecture Diagram

```
                              INTERNET
                                  │
                     ╔════════════▼════════════╗
                     ║  AIG Application Load   ║  ← Public Subnet AZ1 + AZ2
                     ║  Balancer  (HTTPS:443)  ║    Internet-facing
                     ║  Security Group:        ║    Accepts port 443 only
                     ║  Inbound: 443 from ANY  ║
                     ╚════════════╤════════════╝
                                  │  Port 443 only
                     ┌────────────▼────────────┐
                     │   PRIVATE SUBNET AZ1    │   PRIVATE SUBNET AZ2
                     │  ┌──────────────────┐   │  ┌──────────────────┐
                     │  │  AI Gateway      │   │  │  AI Gateway      │
                     │  │  Instance        │   │  │  Instance        │
                     │  │  Security Group: │   │  │  (same SG)       │
                     │  │  In:  443 from   │   │  │                  │
                     │  │       ALB only   │   │  │                  │
                     │  │  Out: all        │   │  │                  │
                     │  └────────┬─────────┘   │  └────────┬─────────┘
                     └───────────┼─────────────┘           │
                                 └──────────┬──────────────┘
                                            │  Port 443 only
                                            │  to dlp.aigw.internal (private DNS)
                     ╔══════════════════════▼═══════════════════════╗
                     ║  DLPoD Application Load Balancer (HTTPS:443) ║
                     ║  INTERNAL — not reachable from internet       ║
                     ║  Security Group:                              ║
                     ║  Inbound: 443 from AIG instances only        ║
                     ╚══════════════════════╤═══════════════════════╝
                                            │  Port 443 only
                     ┌──────────────────────▼──────────────────────┐
                     │  ┌───────────────┐       ┌───────────────┐  │
                     │  │ DLPoD Instance│       │ DLPoD Instance│  │
                     │  │ AZ1           │       │ AZ2           │  │
                     │  │ Security Group│       │ (same SG)     │  │
                     │  │ In: 443 from  │       │               │  │
                     │  │    DLPoD ALB  │       │               │  │
                     │  │ In: 22 from   │       │               │  │
                     │  │    Lambda only│       │               │  │
                     │  └───────────────┘       └───────────────┘  │
                     └─────────────────────────────────────────────┘

────────────────────────── OUTBOUND INTERNET ──────────────────────────
AIG instances, DLPoD instances, Lambda functions
     │
     └──→ NAT Gateway (Public Subnet AZ1)
               │
               ├──→ Netskope management plane  (enrollment, audit logs, tethering callhome)
               └──→ LLM providers  (OpenAI, Anthropic, Bedrock, etc.)

────────────────────── ENROLLMENT AUTOMATION (VPC-internal) ───────────
DLPoD Tethering Lambda (runs inside private subnet)
     └──SSH:22──→ DLPoD Instance  (tethering only — locked to Lambda security group)
```

---

### 4.2 The Components and What They Do

**VPC (Virtual Private Cloud)**
A logically isolated private network inside AWS. Think of it as the customer's own data center network. All components of this deployment live inside this VPC. Nothing from the internet can enter unless we explicitly open a path.

**Public Subnets (AZ1 and AZ2)**
Network segments that have a route to the internet. These hold only the AI Gateway load balancer and the NAT Gateway. No servers run here.

**Private Subnets (AZ1 and AZ2)**
Network segments with no direct internet path — unreachable from outside the VPC. All AI Gateway and DLP On Demand servers run here, along with the internal DLPoD load balancer.

**Internet Gateway**
The attachment point between the VPC and the internet. Traffic can only enter the VPC through the internet gateway if the destination has a public IP address. The AIG load balancer has one; the servers do not.

**NAT Gateway**
Sits in the public subnet. Allows servers in private subnets to initiate outbound connections to the internet (to call Netskope APIs, reach LLM providers) without the internet being able to initiate connections back. Only outbound; never inbound.

**AIG Application Load Balancer (internet-facing)**
The only public entry point into the deployment. Accepts HTTPS connections from applications on port 443. Terminates TLS, performs a health check on each AIG server, and routes requests only to servers that are confirmed healthy. Internet-facing — has a public DNS name and public IP.

**AI Gateway Servers**
EC2 instances running the Netskope AIG appliance software, in private subnets. Receive requests from the AIG ALB, apply inline controls (access control, rate limiting, prompt injection detection), forward content to DLPoD for DLP inspection, then forward approved requests to the upstream LLM provider via the NAT Gateway.

**DLPoD Application Load Balancer (internal)**
Accepts DLP inspection requests from AIG servers only, on port 443. Internal-only — has no public IP, no public DNS name, and cannot be reached from the internet. DNS name is `dlp.aigw.internal`, which resolves only within the VPC via Route 53 private DNS.

**DLP On Demand Servers**
EC2 instances running the Netskope DLPoD appliance software, in private subnets. Receive content from the DLPoD ALB, apply DLP policies locally, and return a verdict (allow/block/redact). Content never leaves the VPC.

**Security Groups**
AWS security groups are virtual firewalls. Every network component — each load balancer and each server — has a security group that defines exactly which traffic is allowed in and out. Traffic that doesn't match an explicit rule is dropped silently. Crucially, security groups can reference other security groups as sources, rather than IP addresses — so "allow traffic from the AIG load balancer" means exactly that load balancer, regardless of what IP address it uses.

---

### 4.3 Security Group Rules — The Enforcement Points

Each tier of the architecture has its own security group. Together, they create a chain where traffic can only flow in one direction, and only through the expected paths.

**AIG Load Balancer Security Group**

| Direction | Protocol | Port | Source / Destination | Effect |
|---|---|---|---|---|
| Inbound | HTTPS | 443 | Any IP address (0.0.0.0/0) | Accepts application traffic from anywhere on the internet |
| Outbound | HTTPS | 443 | AIG Instance Security Group | Forwards to AIG servers only — no other destination permitted |

Everything else is dropped. An attacker cannot use this load balancer to reach anything other than AIG servers on port 443.

**AIG Instance Security Group**

| Direction | Protocol | Port | Source / Destination | Effect |
|---|---|---|---|---|
| Inbound | HTTPS | 443 | AIG Load Balancer SG only | Receives traffic from the ALB only — direct connections from anywhere else are dropped |
| Outbound | All | All | Any | Allows outbound — to NAT Gateway (Netskope API, LLM providers) and to DLPoD ALB |

The outbound-all rule is intentional: AIG servers need to reach LLM providers (which have dynamic IP ranges) and the Netskope management plane. The NAT Gateway provides the actual internet boundary — outbound goes through NAT, inbound from NAT is blocked at the internet gateway.

**DLPoD Load Balancer Security Group**

| Direction | Protocol | Port | Source / Destination | Effect |
|---|---|---|---|---|
| Inbound | HTTPS | 443 | AIG Instance SG only | Only AIG servers can send DLP inspection requests — nothing else can reach this ALB |
| Outbound | HTTPS | 443 | DLPoD Instance SG | Forwards to DLPoD servers only |

This is the key isolation for DLPoD: its load balancer is unreachable from the internet, and it only accepts connections from AIG servers.

**DLPoD Instance Security Group**

| Direction | Protocol | Port | Source / Destination | Effect |
|---|---|---|---|---|
| Inbound | HTTPS | 443 | DLPoD ALB SG | DLP inspection traffic from the ALB |
| Inbound | SSH | 22 | DLPoD Lambda SG only | Tethering automation — from the Lambda function only, nothing else |
| Outbound | All | All | Any | Tethering callhome to Netskope via NAT Gateway |

SSH port 22 is open, but only to the DLPoD Lambda security group. No engineer, no internet host, no other server in the VPC can reach DLPoD on port 22 — only the tethering Lambda.

**DLPoD Lambda Security Group**

| Direction | Protocol | Port | Source / Destination | Effect |
|---|---|---|---|---|
| Outbound | SSH | 22 | DLPoD Instance SG | SSH for tethering automation |
| Outbound | HTTPS | 443 | DLPoD Instance SG | Status checks during tethering |

This Lambda has no inbound rules (Lambda functions don't receive inbound connections). Its outbound is locked to DLPoD instances only.

---

### 4.4 Packet Flow — Application Request to LLM Provider

This is the normal production path: an application sends an LLM API request through the AI Gateway.

```
Step 1  Application → AIG ALB DNS name (HTTPS:443)
        ─────────────────────────────────────────
        The application resolves the AIG ALB's public DNS name to an IP address
        in one of the public subnets. It sends an HTTPS POST request
        (e.g. POST /v1/chat/completions with a JSON body containing the prompt).

        AIG ALB Security Group check:
        ✓ Inbound port 443 from internet — allowed

Step 2  AIG ALB → AIG Instance (HTTPS:443, private subnet)
        ─────────────────────────────────────────────────────
        The ALB terminates TLS (decrypts the HTTPS connection), selects a
        healthy AIG instance, and opens a new HTTPS connection to it on port 443.
        The ALB performs health checks on instances every 30 seconds and only
        routes to instances that pass.

        AIG Instance Security Group check:
        ✓ Inbound port 443 from AIG ALB SG — allowed

Step 3  AIG Instance — Inline Inspection
        ──────────────────────────────────
        The AI Gateway software inspects the request:
        • Access control — is this application/user authorized to reach this model?
          If no → return 403 Forbidden to the application (request stops here)
        • Rate limiting — has this application exceeded its request quota?
          If yes → return 429 Too Many Requests (request stops here)
        • Prompt injection detection — does the prompt attempt to override system
          instructions or manipulate the model?
          If yes → return 400 with rejection reason (request stops here)

        If all checks pass, the AIG instance proceeds to DLP inspection.

Step 4  AIG Instance → DLPoD Internal ALB (HTTPS:443)
        ────────────────────────────────────────────────
        The AIG instance resolves "dlp.aigw.internal" via Route 53 private DNS.
        This name resolves to the DLPoD ALB's private IP address — and only
        within the VPC. From outside the VPC, this name does not resolve at all.

        The AIG instance sends the prompt content to the DLPoD ALB over HTTPS,
        using the self-signed TLS certificate that was placed in its bootstrap
        configuration when it launched.

        DLPoD ALB Security Group check:
        ✓ Inbound port 443 from AIG Instance SG — allowed

Step 5  DLPoD ALB → DLPoD Instance (HTTPS:443, private subnet)
        ──────────────────────────────────────────────────────────
        The DLPoD ALB routes the inspection request to a healthy DLPoD instance
        using a least-connections algorithm.

        DLPoD Instance Security Group check:
        ✓ Inbound port 443 from DLPoD ALB SG — allowed

Step 6  DLPoD Instance — DLP Inspection
        ────────────────────────────────
        The DLPoD appliance scans the content against Netskope DLP policies
        (fingerprinting, exact data match, ML classifiers, regex) entirely
        locally — no content leaves the VPC.

        DLPoD returns a verdict to the AIG instance:
        • ALLOW  — content is clean, proceed
        • BLOCK  — content matched a DLP policy, reject the request
        • REDACT — specific content matched, mask it and proceed

        If BLOCK → AIG returns an error to the application, logs the event
        If REDACT → AIG replaces the flagged content with a placeholder and continues
        If ALLOW → AIG proceeds to Step 7

Step 7  AIG Instance → LLM Provider (HTTPS:443, via NAT Gateway)
        ─────────────────────────────────────────────────────────
        The AIG instance sends the approved (or redacted) prompt to the
        upstream LLM provider API (OpenAI, Anthropic, Bedrock, etc.).

        Traffic path:
        AIG Instance → Private subnet route table → NAT Gateway → Internet Gateway → LLM Provider

        The LLM provider sees the connection coming from the NAT Gateway's public IP.
        The AIG instance's private IP is never exposed.

Step 8  LLM Provider → AIG Instance (response)
        ────────────────────────────────────────
        The LLM response returns to the AIG instance through the same NAT path.
        The AIG instance sends the response content through DLP inspection again
        (Steps 4–6 repeat for the response body).

Step 9  AIG Instance → AIG ALB → Application
        ────────────────────────────────────────
        The inspected (and possibly redacted) response is returned to the AIG ALB,
        which re-encrypts it with TLS and returns it to the application.

        The full round trip — from application to LLM provider and back,
        including DLP inspection — typically adds under 100ms of latency
        for standard DLP policies.
```

---

### 4.5 Packet Flow — Enrollment Automation (What Happens When a Server Launches)

This flow happens automatically every time an AI Gateway server launches — at initial deployment and when Auto Scaling adds capacity.

```
Step 1  Auto Scaling Group launches a new AIG EC2 instance
        ─────────────────────────────────────────────────────
        The instance starts booting in a private subnet.
        A lifecycle hook immediately pauses it in "Pending:Wait" state —
        it is not yet registered with the load balancer and receives no traffic.

Step 2  Auto Scaling → SNS Topic → Lambda Function
        ─────────────────────────────────────────────
        Auto Scaling publishes a "instance launching" event to an SNS topic.
        The AIG Activation Lambda function is subscribed to this topic and
        is invoked automatically within seconds.

Step 3  Lambda → Netskope API (via NAT Gateway)
        ───────────────────────────────────────────
        The Lambda function reads the Netskope API token from Secrets Manager
        (the Lambda's IAM role permits this; the server's does not).

        The Lambda calls the Netskope REST API:
        POST https://<tenant>.goskope.com/api/v2/infrastructure/aig/appliances

        Netskope responds with an enrollment token scoped to this one appliance.
        The token exists only in Lambda memory — it is never logged.

Step 4  Lambda → Secrets Manager (writes bootstrap secret)
        ────────────────────────────────────────────────────
        The Lambda writes the enrollment token plus the DLP configuration
        (DLPoD certificate and endpoint) to the AIG bootstrap secret in
        Secrets Manager. This is the only secret the AIG server can read.

Step 5  Lambda signals "ready" → instance enters service
        ────────────────────────────────────────────────────
        The Lambda calls CompleteLifecycleAction:CONTINUE. Auto Scaling
        moves the instance from "Pending:Wait" to "InService" and registers
        it with the AIG load balancer.

Step 6  AIG Instance reads bootstrap secret and self-enrolls
        ──────────────────────────────────────────────────────
        At boot, the AIG appliance software reads the bootstrap secret from
        Secrets Manager using the instance's own AWS identity. It extracts
        the enrollment token and calls home to the Netskope tenant to complete
        enrollment — no credentials were passed to the instance at launch.

        The instance begins passing ALB health checks when enrollment completes
        (typically 5–15 minutes from instance launch).
```

DLP On Demand tethering follows a similar pattern but uses AWS Step Functions to orchestrate a longer multi-step SSH automation workflow — covered in detail in [Section 7](#7-automated-enrollment--how-instances-configure-themselves)

.

---

## 5. Credential Security — How We Handle API Tokens and License Keys

### The Security Concern

The deployment needs several sensitive credentials: a Netskope API token (to register appliances), a DLP On Demand license key, and TLS certificates. How these are stored and passed to servers is a common source of security vulnerabilities.

The most common insecure patterns are:
- **Credentials in configuration files baked into a server image** — if the image is copied, the credentials go with it
- **Credentials passed as environment variables to servers or functions** — environment variables are often logged unintentionally and are visible to anyone who can inspect the process
- **Credentials in deployment scripts or startup scripts** — these appear in logs and are frequently committed to source control by mistake
- **One credential giving access to everything** — if a single API token is used everywhere and it leaks, the entire Netskope tenant is exposed

### How the Design Addresses This

**AWS Secrets Manager** is an encrypted vault for storing credentials. Values stored in Secrets Manager are encrypted at rest with AES-256, and access is controlled by AWS IAM policies — only the specific components you authorize can read each secret. A server or function that doesn't have explicit IAM permission to read a secret will receive an "Access Denied" error if it tries.

We store three separate secrets, each accessible to only the one component that needs it:

| Secret | Who Can Read It | Who Cannot |
|---|---|---|
| Netskope API token + tenant URL | AIG enrollment Lambda function only | AI Gateway servers, DLPoD servers, DLPoD Lambda |
| AI Gateway bootstrap data (enrollment token, DLP cert, DLP endpoint) | AI Gateway servers at boot only | DLPoD servers, Lambda functions |
| DLP On Demand license key | DLPoD tethering Lambda only | AI Gateway servers, AIG Lambda |

**The API token never touches a server.** This is a critical design point. When an AI Gateway server launches, it does not receive the Netskope API token. Instead, a Lambda function reads the API token, calls the Netskope API in memory, receives a short-lived enrollment token in return, and writes only the enrollment token to the server's bootstrap secret. The server reads the enrollment token — not the API token. If a server were compromised, the attacker could not retrieve the Netskope API token from it.

**`NoEcho` parameters.** When the API token and license key are submitted at deployment time, CloudFormation marks them as `NoEcho: true`. This means their values are masked immediately — they never appear in CloudFormation event logs, stack outputs, or the AWS console. After the values are submitted once, they cannot be retrieved through CloudFormation.

**Credentials are never in server startup scripts.** AI Gateway servers have effectively empty startup scripts. They retrieve their configuration at boot by calling Secrets Manager using their AWS identity — no credential is baked in.

### Is This AWS Best Practice?

Yes. AWS recommends Secrets Manager specifically for application credentials, API keys, and tokens that need access controls and audit logging. The alternative — storing credentials in environment variables or configuration files — is explicitly listed as an anti-pattern in the AWS Security Best Practices documentation.

**In plain terms for a customer:** Your Netskope API token is stored in an encrypted vault that only one Lambda function can open. Your servers never see it. If a server were compromised, the attacker would find an enrollment token scoped to that one appliance — not your API credentials.

---

## 6. IAM — Limiting What Each Component Can Do

### The Security Concern

In AWS, every server, function, and service needs permissions to call other AWS services. If those permissions are too broad, a compromised component can affect the entire account.

The risk of overprivileged IAM roles:
- A compromised server with admin-level permissions could read all secrets in the account, delete other stacks, or create backdoor users
- A compromised Lambda function with broad permissions could exfiltrate data from S3 buckets it was never meant to access
- A single leaked credential with wildcard permissions is catastrophic; a single leaked credential with narrow permissions is contained

### How the Design Addresses This

We create **nine separate IAM roles** — one for each distinct component — and each role has only the permissions that component specifically needs to do its job. No role has wildcard (`*`) permissions on sensitive operations.

What an AI Gateway server can do with its AWS identity:
- Read its own bootstrap secret from Secrets Manager
- Write logs to CloudWatch

What an AI Gateway server **cannot** do:
- Read the Netskope API token
- Read the DLPoD license key
- Start or stop other servers
- Modify any IAM role or policy
- Access any S3 bucket

If an AI Gateway server were fully compromised, the attacker's AWS access is limited to reading that one bootstrap secret and writing logs. That is the blast radius of a worst-case AIG instance compromise.

The same principle applies to every component. The DLPoD tethering Lambda can read the license key and complete the lifecycle hook — and nothing else. The certificate generator Lambda can write to ACM and two SSM parameters — and nothing else.

### Is This AWS Best Practice?

Yes. AWS Well-Architected ([SEC03-BP01](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_permissions_define.html)) defines this as *"Grant least privilege"* — every identity should have only the permissions required to perform its specific task, and no more.

**A note for customer security reviews:** In production environments, we recommend also scoping the IAM permissions used to *deploy* the stack to the stack name prefix (e.g., `arn:aws:iam::*:role/aigw-*` for role creation). This prevents the deploying user from inadvertently creating roles outside the expected namespace. The full deployer IAM policy is published in the [Deployment Guide](https://github.com/jharris-ns/AWS-AIGW-Reference-Architecture/blob/master/docs/DEPLOYMENT.md).

---

## 7. Automated Enrollment — How Instances Configure Themselves

When a new server launches, it needs to register with the Netskope tenant before it can do its job. This has to work reliably, without manual steps, and without ever logging credentials.

Three AWS services work together to make this happen:

---

### Auto Scaling Lifecycle Hooks — Holding New Instances Until They're Ready

**What it does:** When an Auto Scaling Group launches a new server, a lifecycle hook pauses the server in a "waiting" state before it starts receiving traffic. The server is running but invisible to the load balancer. The hook gives automation time to run. Only after the automation signals "done" does the server enter service.

**Why we use it:** Without this pause, a new server could start receiving production traffic before it has finished enrolling with Netskope — which would either pass traffic un-inspected or fail completely. The lifecycle hook is the guarantee that enrollment is complete before traffic arrives.

---

### AWS SNS and Lambda — Triggering Enrollment Automatically

**What it does:** When the lifecycle hook fires (server entering the waiting state), Auto Scaling publishes a notification to an SNS topic. A Lambda function subscribed to that topic receives the event within seconds and begins enrollment.

Lambda is a "serverless" function — it runs code in response to an event, without a persistent server. The enrollment Lambda runs for 30–120 seconds, registers the appliance with Netskope, writes the enrollment token to Secrets Manager, and tells the Auto Scaling Group the server is ready to proceed.

**Why we use it:** The enrollment logic needs to run exactly once per server launch, needs access to Secrets Manager, and must not run on the servers being enrolled (which would require credentials on the instance). Lambda is the right tool: it's event-driven, short-lived, and can have its own IAM identity separate from the servers.

**DLP On Demand takes longer.** DLPoD tethering requires navigating a CLI interface over SSH — it takes 15–25 minutes and involves multiple steps with waits between them. A single Lambda function cannot run for that long. Which is why we add Step Functions.

---

### AWS Step Functions — Orchestrating Multi-Step Tethering

**What it does:** Step Functions is a workflow engine. You define a sequence of steps — call this function, wait, check a condition, retry if it fails, move to the next step — and Step Functions manages the execution state for as long as needed.

For DLPoD tethering, Step Functions runs the following sequence automatically:
1. Wait for the DLPoD instance to accept SSH connections (can take 5–8 minutes during boot)
2. Set a unique random password on the instance via SSH
3. Configure DNS so the instance can call home to Netskope
4. Apply the license key from Secrets Manager
5. Wait for the appliance to initiate its callhome to Netskope
6. Poll until tethering is confirmed complete
7. Signal Auto Scaling to release the instance into service

If any step fails transiently (SSH not ready yet, network momentarily unavailable), Step Functions retries it automatically before marking the execution failed.

**Why we use it:** The DLPoD tethering process is too long and too multi-step for a single Lambda function. Step Functions lets us break it into discrete steps, each with its own timeout and retry policy, while maintaining execution state across the full 15–25 minutes. It also gives us full visibility into exactly which step succeeded or failed — critical for troubleshooting.

---

**For a customer asking "so how does the server know what to do?":**

AI Gateway servers have empty startup scripts. At boot, the server reads a "bootstrap" value from Secrets Manager using its own AWS identity. That bootstrap contains the enrollment token and the DLP configuration. The server uses those to register itself with Netskope automatically. No credential was passed to it at launch — it fetched what it needed, using a permission scoped to exactly that one secret.

---

## 8. High Availability — What Happens When Something Fails

The deployment runs across **two AWS Availability Zones** — physically separate data centers in the same AWS region, connected by high-speed private links. If one data center goes down, the other continues serving all traffic immediately. No action is required.

| Failure | What Happens | Recovery |
|---|---|---|
| Single AI Gateway server fails health check | Load balancer stops sending traffic to it; other servers absorb the load | Auto Scaling terminates and replaces it; re-enrollment completes in 5–15 min |
| Single DLP On Demand server fails | Remaining DLPoD servers continue inspection | Auto Scaling replaces it; re-tethering completes in 15–25 min |
| Entire data center (AZ) goes offline | Capacity drops by ~50%; other AZ handles all traffic | Immediate; no action required |
| Full stack deleted and redeployed | 15–20 min outage during redeploy | Re-deploy from the same template |

**Stateless by design.** Neither service stores any data locally on the server. All configuration lives in Netskope's management plane (policy, enrollment records) or in the CloudFormation template (infrastructure definition). When a server is replaced, the replacement reads the same configuration from the same sources and is indistinguishable from what it replaced.

---

## 9. AWS Best Practices — The Short Answer

**Customer question: Are you following AWS best practices?**

Yes. The design maps directly to the [AWS Well-Architected Framework](https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html), which is Amazon's published set of cloud architecture best practices organized into pillars: Security, Reliability, Operational Excellence, Cost Optimization.

| Practice | How We Implement It |
|---|---|
| **Servers in private subnets** (SEC05-BP02) | All EC2 instances in private subnets; no public IP addresses on any server |
| **Least-privilege IAM** (SEC03-BP01) | Nine separate IAM roles; each scoped to the minimum permissions for its function |
| **Secrets in a managed vault, not in code** (SEC08-BP02) | API token and license key in AWS Secrets Manager; never in config files, scripts, or environment variables |
| **Encryption in transit** (SEC09-BP02) | All paths use TLS; AIG→DLPoD uses HTTPS on a private internal path; no unencrypted traffic |
| **Encryption at rest** | Secrets Manager and SSM Parameter Store use AES-256 with AWS-managed KMS keys |
| **Multi-AZ deployment** (Reliability) | Both server fleets and both load balancers span two Availability Zones |
| **Auto-replace on failure** (Reliability) | Auto Scaling detects unhealthy instances and replaces them automatically |
| **Infrastructure as code** (Operational Excellence) | Everything defined in CloudFormation; no manual configuration steps |

---

## 10. What Permissions Are Needed to Deploy

**Customer question: What AWS permissions does our team need to run this template?**

Deploying the CloudFormation stack requires permissions to create the resources the template builds. These are the categories of permissions required:

| Permission Category | Why It's Needed |
|---|---|
| **CloudFormation** | To create, update, and delete the stack itself |
| **EC2 and VPC** | To create servers, the private network, subnets, security groups, and the NAT Gateway |
| **Auto Scaling** | To create the server fleets that manage AIG and DLPoD instances |
| **Elastic Load Balancing** | To create the internet-facing and internal load balancers |
| **IAM** | To create the nine IAM roles the template defines |
| **Lambda** | To create the enrollment and tethering Lambda functions |
| **Step Functions** | To create the DLPoD tethering workflow |
| **Secrets Manager** | To create the secrets that store the API token, bootstrap data, and license key |
| **SNS** | To create the notification topics that connect Auto Scaling to Lambda |
| **ACM** | To import the self-signed TLS certificates for the load balancers |
| **Route 53** | To create the private DNS zone for internal DLP traffic routing |
| **SSM Parameter Store** | To store the DLPoD certificate and appliance tracking parameters |
| **CloudWatch** | To create log groups for Lambda function output |
| **S3** | To upload Lambda deployment packages and the template file before deploying |
| **STS** | To confirm account identity when naming the S3 artifact bucket |

A minimum IAM policy with the exact actions and resource scopes is published in the [Deployment Guide](https://github.com/jharris-ns/AWS-AIGW-Reference-Architecture/blob/master/docs/DEPLOYMENT.md). Customers should review this policy with their security team before deploying.

**Important:** The IAM permission to create roles (`iam:CreateRole`) is required because the template creates the nine least-privilege roles. For customers with strict controls on role creation, we recommend scoping this permission to the stack name prefix (for example, `arn:aws:iam::*:role/aigw-*`) to prevent the deployer from creating roles outside the expected namespace.

---

## 11. Cost

**Customer question: What will this cost?**

Costs are driven almost entirely by EC2 instance hours — the virtual servers running AIG and DLPoD. All other components (load balancers, NAT Gateway, Lambda, Secrets Manager) are minor by comparison.

**Baseline estimate (1 AIG + 1 DLPoD instance, us-west-1, on-demand pricing):**

| Resource | Monthly Cost |
|---|---|
| AI Gateway — `m5.4xlarge` (16 vCPU, 64 GB RAM) | ~$550 |
| DLP On Demand — `c5a.4xlarge` (16 vCPU, 32 GB RAM) | ~$445 |
| NAT Gateway | ~$35–65 |
| 2 × Application Load Balancer | ~$38–70 |
| Secrets Manager, Route 53, Lambda, CloudWatch | < $10 |
| **Total** | **~$1,070–$1,140/month** |

**Scaling costs:**
- Each additional AIG instance: approximately +$550/month
- Each additional DLPoD instance: approximately +$445/month
- A 4 AIG + 2 DLPoD deployment runs approximately $3,200–$3,500/month

**GPU-accelerated AIG (for advanced ML guardrails):**
- `g4dn.xlarge` (NVIDIA T4): approximately +$380/month per instance
- `g5.xlarge` (NVIDIA A10G): approximately +$760/month per instance

**Reducing costs:**
- AWS Reserved Instances or Savings Plans reduce EC2 costs by 30–60% for steady-state production workloads. A 1-year Reserved Instance commitment on the default AIG + DLPoD pair would bring monthly EC2 costs from approximately $995 to approximately $600.
- DLP On Demand can be right-sized up (fewer large instances) rather than scaled out (many smaller instances) — `c5a.8xlarge` at ~$890/month provides roughly double the inspection throughput of a `c5a.4xlarge`.

> These estimates use on-demand pricing in us-west-1 and are provided for planning purposes. Costs vary by region. Use [AWS Pricing Calculator](https://calculator.aws/) with your target region and expected usage for a precise figure.

---

## 12. Technical Documentation

Full deployment and operations documentation is maintained in the GitHub repository alongside the CloudFormation templates.

| Document | What It Covers |
|---|---|
| [QUICKSTART.md](https://github.com/jharris-ns/AWS-AIGW-Reference-Architecture/blob/master/docs/QUICKSTART.md) | Prerequisites checklist, three-step deploy reference |
| [DEPLOYMENT.md](https://github.com/jharris-ns/AWS-AIGW-Reference-Architecture/blob/master/docs/DEPLOYMENT.md) | Full parameter reference, preflight checks, minimum IAM policy, deploy options |
| [ARCHITECTURE.md](https://github.com/jharris-ns/AWS-AIGW-Reference-Architecture/blob/master/docs/ARCHITECTURE.md) | Network design, traffic flows, IAM role details, HA scenarios, cost estimates |
| [SECURITY.md](https://github.com/jharris-ns/AWS-AIGW-Reference-Architecture/blob/master/docs/SECURITY.md) | IAM role policies, security group rules, encryption details, known limitations |
| [OPERATIONS.md](https://github.com/jharris-ns/AWS-AIGW-Reference-Architecture/blob/master/docs/OPERATIONS.md) | Scaling, monitoring, log groups, AMI upgrade procedure |
| [TROUBLESHOOTING.md](https://github.com/jharris-ns/AWS-AIGW-Reference-Architecture/blob/master/docs/TROUBLESHOOTING.md) | Diagnostic commands, issue/cause/solution tables, log pattern reference |
| [GitHub Repository](https://github.com/jharris-ns/AWS-AIGW-Reference-Architecture) | Combined template, scripts, source code |
| [Standalone POV Templates](https://github.com/jharris-ns/AWS-POV-Templates-CFT) | AIG-only and DLPoD-only templates for POV deployments |
