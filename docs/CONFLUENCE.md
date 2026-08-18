# Netskope AI Gateway — AWS CloudFormation Reference Architecture

**Document audience:** Product Management, Sales Engineering, Solutions Architecture, InfoSec  
**Template:** `templates/gateway-combined.yaml`  
**Services covered:** Netskope AI Gateway (AIG) + DLP On Demand (DLPoD)  
**Reading time:** ~30 minutes

---

## Table of Contents

1. [Introduction](#1-introduction)
   - [The Problem This Solves](#11-the-problem-this-solves)
   - [What the Template Does](#12-what-the-template-does)
   - [Controls Applied to Every Request and Response](#13-controls-applied-to-every-request-and-response)
   - [Deployment Options — Three Templates](#14-deployment-options--three-templates)
   - [AWS Services Used](#15-aws-services-used)
   - [Cost Overview](#16-cost-overview)
   - [Prerequisites](#17-prerequisites)

2. [Security Considerations](#2-security-considerations)
   - [How AWS Identity and Access Management Works](#21-how-aws-identity-and-access-management-works)
   - [IAM Roles in This Deployment](#22-iam-roles-in-this-deployment)
   - [Credential and Secret Management](#23-credential-and-secret-management)
   - [Network Security](#24-network-security)
   - [Encryption](#25-encryption)
   - [What Is Explicitly Prohibited by This Design](#26-what-is-explicitly-prohibited-by-this-design)
   - [Known Limitations and Accepted Risks](#27-known-limitations-and-accepted-risks)
   - [AWS Well-Architected Alignment Summary](#28-aws-well-architected-alignment-summary)

3. [Network Architecture](#3-network-architecture)
   - [Architecture Overview](#31-architecture-overview)
   - [How AWS Networking Works — Background](#32-how-aws-networking-works--background)
   - [VPC and Subnet Design](#33-vpc-and-subnet-design)
   - [How Traffic Flows from the Internet to the Gateway](#34-how-traffic-flows-from-the-internet-to-the-gateway)
   - [AI Gateway Tier](#35-ai-gateway-tier)
   - [DLP On Demand Tier](#36-dlp-on-demand-tier)
   - [Certificate Management](#37-certificate-management)
   - [High Availability and Failure Scenarios](#38-high-availability-and-failure-scenarios)
   - [Instance Sizing Reference](#39-instance-sizing-reference)

4. [Automation Flow — How It Works](#4-automation-flow--how-it-works)
   - [What "Automation" Means in This Context](#41-what-automation-means-in-this-context)
   - [AWS Services Powering the Automation](#42-aws-services-powering-the-automation)
   - [Stack Creation Sequence](#43-stack-creation-sequence)
   - [AI Gateway Enrollment — Step by Step](#44-ai-gateway-enrollment--step-by-step)
   - [DLP On Demand Tethering — Step by Step](#45-dlp-on-demand-tethering--step-by-step)
   - [Runtime Traffic Flow — Per Request](#46-runtime-traffic-flow--per-request)
   - [Auto-Scaling Flow](#47-auto-scaling-flow)
   - [Instance Termination and Deregistration](#48-instance-termination-and-deregistration)

5. [Troubleshooting](#5-troubleshooting)
   - [How to Orient Yourself Before Diagnosing](#51-how-to-orient-yourself-before-diagnosing)
   - [Diagnostic Commands — Run These First](#52-diagnostic-commands--run-these-first)
   - [AI Gateway Issues](#53-ai-gateway-issues)
   - [DLP On Demand Issues](#54-dlp-on-demand-issues)
   - [Certificate Issues](#55-certificate-issues)
   - [Stack-Level Issues](#56-stack-level-issues)
   - [Log Reference — What Good Looks Like](#57-log-reference--what-good-looks-like)

---

## 1. Introduction

### 1.1 The Problem This Solves

Enterprise use of large language models (LLMs) — tools like OpenAI's GPT-4, Anthropic's Claude, or Amazon Bedrock's hosted models — creates a class of data security risks that traditional network controls were never designed to handle.

When employees or applications send prompts to an LLM, they may inadvertently (or deliberately) include sensitive corporate data: customer PII, source code, financial records, confidential plans. The LLM API is an outbound HTTPS connection — it looks like any other web request from the outside. Standard web proxies and DLP tools see only the encrypted connection; they cannot inspect the content of individual prompts and responses.

Additionally, because LLMs produce natural-language output that can be shaped by the prompt, they are vulnerable to **prompt injection attacks** — attempts to override the system's instructions through carefully crafted user input. For example, a user might submit a prompt that causes the model to ignore its system instructions, impersonate another entity, or extract information it shouldn't share.

**What this template deploys** is a solution to both problems: a Netskope AI Gateway that sits *inline* between your applications and LLM providers, inspecting every request and response in real time, combined with a Netskope DLP On Demand service that performs content inspection entirely inside your AWS account — so sensitive data never leaves your network boundary for DLP analysis.

The entire solution — servers, networking, security configuration, service enrollment, and automated monitoring — is deployed through a single AWS CloudFormation template. Deployment takes approximately 15–20 minutes and requires no manual configuration after launch.

### 1.2 What the Template Does

**AWS CloudFormation** is Amazon's infrastructure-as-code service. Instead of manually clicking through the AWS console to create servers, networks, and policies, you describe everything you want in a template file (in YAML format), and CloudFormation creates it all automatically in the correct order. This makes deployments repeatable, version-controlled, and auditable — every resource is defined in code, and CloudFormation tracks what it created so it can update or delete everything as a unit.

A CloudFormation **stack** is the live deployment of a template. When you deploy this template, CloudFormation creates a stack containing approximately 80 AWS resources — servers, load balancers, networks, security policies, automation functions, and more — all wired together and ready to serve traffic.

This template (`templates/gateway-combined.yaml`) deploys the following, fully automatically:

- A private network (AWS VPC) with public and private subnets across two availability zones (data centers)
- An **internet-facing load balancer** that accepts HTTPS requests from your applications
- An **AI Gateway** fleet running on EC2 instances, automatically enrolled with your Netskope tenant
- An **internal DLP On Demand** fleet that inspects content entirely inside your VPC
- All security policies, encryption, DNS, TLS certificates, and IAM permissions required to operate
- Automation that handles instance lifecycle — when a new instance launches, it is automatically enrolled and verified before it starts serving traffic; when an instance terminates, it is automatically deregistered from Netskope

After `CREATE_COMPLETE`, both services are enrolled, wired together, and ready to receive traffic. There are no manual steps.

### 1.3 Controls Applied to Every Request and Response

Every HTTP request and response that passes through the AI Gateway is subject to all of the following controls simultaneously, in real time:

| Control | What It Enforces | Where It Runs |
|---|---|---|
| **Data Loss Prevention (DLP)** | Detects and blocks sensitive data in prompts and responses — PII, credentials, source code, regulated content such as PCI or HIPAA data — using Netskope DLP policies. Content is scanned against a full DLP engine with fingerprinting, exact data matching, and ML-based classifiers. | DLP On Demand (inside your VPC — no data leaves your AWS account) |
| **Prompt Injection Detection** | Identifies attempts to override system instructions, exfiltrate data through the model, or manipulate model behavior through crafted user inputs. Runs inline on the AI Gateway using built-in detection rules. | AI Gateway |
| **Access Control** | Enforces which applications and users are permitted to reach which models. Requests from unauthorized sources or to blocked model endpoints are rejected at the gateway. Policy is managed centrally in the Netskope tenant. | AI Gateway |
| **Rate Limiting** | Caps request volume per application or user to control LLM API costs and prevent abuse. Rate limit policies are set in the Netskope management console. | AI Gateway |
| **Audit Logging** | Records all requests and responses — including blocked, redacted, and allowed events — to the Netskope management plane. This provides a complete audit trail for compliance review, incident investigation, and usage reporting. | AI Gateway → Netskope management plane |

Applications connect to the AI Gateway's HTTPS endpoint and require **no code changes** — the gateway presents an OpenAI-compatible API regardless of which upstream LLM provider is being used. From the application's perspective, it is talking to an OpenAI endpoint; in practice, every request is being inspected, logged, and governed before it reaches the model.

### 1.4 Deployment Options — Three Templates

Three CloudFormation templates are available. They share the same lifecycle automation design but serve different deployment scenarios:

| Template | File | Use when |
|---|---|---|
| **Combined (recommended)** | `templates/gateway-combined.yaml` | Deploying AI Gateway + DLP On Demand together — single stack, automatic wiring between services. Most customers should use this. |
| **AI Gateway only** | `aig/template/gateway-aig.yaml` | Deploying AIG without DLP On Demand, or adding AIG to an existing VPC that already has networking. Does not require packaging and uploading Lambda code to S3 — the Lambda function is embedded directly in the template. |
| **DLP On Demand only** | `dlpod/template/gateway-dlpod.yaml` | Deploying DLPoD as a standalone service — for example, to test it independently or to add it to an environment where AIG is already running. |

Each template is **self-contained**: it creates its own VPC, subnets, security groups, and all supporting resources. No pre-existing infrastructure is required. The combined template automatically generates the DLP certificate, shares it with the AIG configuration, and wires the two services together — this handoff happens before any instances launch, so there is no window during which AIG is running without DLP configured.

The remainder of this document focuses on the combined template, which deploys both services.

### 1.5 AWS Services Used

Understanding what each AWS service does helps make sense of the architecture and troubleshooting steps. Below is every service this template uses, with a brief explanation of the service and how it is used here.

---

**Amazon EC2 (Elastic Compute Cloud)**

EC2 is AWS's virtual server service. An EC2 instance is a virtual machine running in an AWS data center. You choose the operating system, CPU, and memory size; AWS runs the hardware.

This template uses EC2 instances to run the Netskope AI Gateway appliance software and the Netskope DLP On Demand appliance software. Both appliances are pre-packaged as AWS Marketplace AMIs (Amazon Machine Images) — essentially a snapshot of a configured server that can be launched repeatedly. The AI Gateway uses `m5.4xlarge` instances (16 virtual CPUs, 64 GB RAM) by default. DLP On Demand uses `c5a.4xlarge` instances (16 virtual CPUs, 32 GB RAM).

---

**Amazon EC2 Auto Scaling**

Auto Scaling manages a group of EC2 instances (called an Auto Scaling Group, or ASG) as a fleet. It automatically replaces instances that become unhealthy, and can add or remove instances based on CPU usage or other metrics.

This template creates two Auto Scaling Groups: one for the AI Gateway fleet and one for the DLP On Demand fleet. The ASG is responsible for ensuring the right number of instances are running and healthy at all times. When an instance fails its health check, the ASG terminates it and launches a replacement — automatically.

A critical feature used here is **lifecycle hooks**: ASG can pause a newly launched instance in a holding state called `Pending:Wait` before it goes live. This pause gives the automation system time to register the instance with Netskope and write its configuration before it starts serving traffic. Without lifecycle hooks, instances might start serving requests before they're properly enrolled.

---

**Elastic Load Balancing — Application Load Balancer (ALB)**

An Application Load Balancer (ALB) distributes incoming HTTPS traffic across multiple EC2 instances. It terminates TLS (decrypts HTTPS), performs health checks on each instance, and routes requests only to healthy instances. If an instance fails its health check, the ALB stops sending it traffic automatically.

This template creates two ALBs:
- An **internet-facing ALB** for the AI Gateway — this is the public entry point that your applications connect to. It lives in public subnets and is reachable from the internet.
- An **internal ALB** for DLP On Demand — this is only reachable from within the VPC. AI Gateway instances send content to it for DLP inspection. It is never exposed to the internet.

---

**Amazon VPC (Virtual Private Cloud)**

A VPC is your own private, isolated section of the AWS network. Think of it as your own private data center network inside AWS. You control the IP address ranges, routing rules, and what can communicate with what.

This template creates a new VPC with four subnets organized into two tiers:
- **Public subnets** — connected to the internet, used for the internet-facing load balancer and the NAT Gateway
- **Private subnets** — no direct internet access, used for all EC2 instances (AI Gateway and DLP On Demand) and the internal load balancer

Placing compute instances in private subnets is an AWS security best practice: it means there is no way to connect directly to an instance from the internet. All inbound traffic must go through the load balancer.

---

**NAT Gateway (Network Address Translation)**

A NAT Gateway allows EC2 instances in private subnets to make outbound connections to the internet, without exposing those instances to inbound connections from the internet.

In this deployment, AI Gateway and DLP On Demand instances need to call out to the internet for several reasons:
- AIG instances connect to the Netskope management plane for enrollment and audit logging
- AIG instances forward allowed LLM API requests to providers like OpenAI or Amazon Bedrock
- DLP On Demand instances call home to Netskope to complete tethering (licensing and policy sync)

The NAT Gateway handles all of this outbound traffic. Traffic exits the NAT Gateway with the NAT Gateway's IP address — the instances' private IP addresses are never exposed.

---

**AWS Lambda**

Lambda is a serverless compute service. You provide code (a function), and AWS runs it in response to events — without you needing to provision or manage servers. Lambda functions run for seconds or minutes and are billed only for the time they actually run.

This template uses three Lambda functions:
1. **AIG Activation Lambda** — runs every time an AIG instance launches or terminates. At launch, it calls the Netskope API to register the appliance, gets an enrollment token, and writes it to AWS Secrets Manager. At termination, it deregisters the appliance.
2. **DLPoD Lambda** — runs as part of the DLP On Demand tethering workflow. It SSH-connects to a new DLPoD instance and automates the CLI steps to apply the license key, configure DNS, and wait for tethering to complete. This Lambda runs inside the VPC so it can reach the DLPoD instance's private IP address.
3. **Certificate Generator Lambda** — runs once at stack creation. It generates self-signed TLS certificates for both load balancers, imports them into AWS Certificate Manager, and writes them to the right places so the AIG instances know which certificate to trust when connecting to the DLPoD load balancer.

---

**AWS Step Functions**

Step Functions is a workflow orchestration service. It lets you define a sequence of steps — calling Lambda functions, waiting for conditions to be met, retrying on failure, branching based on outcomes — and manages the execution state for you.

DLP On Demand tethering is a multi-step process that takes 15–25 minutes and involves waiting for the instance to boot, connecting via SSH, applying a license, and waiting for the instance to phone home to Netskope. Step Functions orchestrates this workflow: it calls the DLPoD Lambda repeatedly with different instructions, tracks which step is complete, handles retries if SSH isn't ready yet, and knows when the whole process is done.

The advantage of Step Functions over a single long-running Lambda function is reliability: Step Functions persists the workflow state, so if a step fails or times out, it can retry from where it left off.

---

**Amazon SNS (Simple Notification Service)**

SNS is a pub/sub messaging service. Publishers send messages to a "topic," and all subscribers to that topic receive the message.

In this deployment, Auto Scaling uses SNS to deliver lifecycle events (instance launching, instance terminating) to Lambda functions. When an AIG instance launches, Auto Scaling publishes a message to the AIG lifecycle SNS topic. The AIG Activation Lambda is subscribed to that topic, so it receives the event immediately and begins enrollment.

This decoupling — Auto Scaling doesn't call Lambda directly, it publishes to SNS — is an AWS best practice for resilience. If the Lambda function is temporarily unavailable, SNS retries delivery.

---

**AWS Secrets Manager**

Secrets Manager is an encrypted vault for sensitive credentials: API keys, passwords, license keys, TLS certificates. Credentials stored in Secrets Manager are encrypted at rest with AES-256, and access is controlled by IAM policies — only the specific roles you authorize can read each secret.

The critical advantage of Secrets Manager over putting credentials in configuration files or environment variables is the **access control model**: you can grant read access to exactly the right component and nothing else. An EC2 instance that gets compromised cannot read credentials it wasn't authorized to read.

This template stores five secrets/parameters:
- Netskope API token + tenant URL (Secrets Manager)
- AIG bootstrap data — enrollment token, DLP certificate, DLP host (Secrets Manager)
- DLPoD license key (Secrets Manager)
- DLPoD ALB certificate PEM (SSM Parameter Store, SecureString)
- AIG appliance IDs for lifecycle tracking (SSM Parameter Store)

---

**AWS Systems Manager (SSM) Parameter Store**

Parameter Store is a key-value store for configuration data. SecureString parameters are encrypted with KMS. It is similar to Secrets Manager but simpler — suited for individual values that are more configuration than credential.

In this deployment, Parameter Store holds the DLPoD ALB TLS certificate PEM (so the AIG Activation Lambda can write it into the AIG bootstrap secret) and the AIG appliance IDs (so the Activation Lambda can look up the appliance ID at termination time to deregister the correct appliance).

---

**AWS Certificate Manager (ACM)**

ACM is a managed service for TLS/SSL certificates. It stores certificates and makes them available to load balancers. ACM handles certificate renewal automatically for certificates it issues, and it is the only way to attach a TLS certificate to an AWS Application Load Balancer.

This template imports self-signed certificates into ACM (for both ALBs) using the Certificate Generator Lambda. For the AIG ALB, you can optionally provide your own ACM-issued or ACM-imported certificate (via the `AcmCertificateArn` parameter) if you need a trusted TLS certificate for external clients.

---

**Amazon Route 53**

Route 53 is AWS's DNS service. In addition to public DNS, it supports **private hosted zones** — DNS zones that resolve only within a specific VPC. This means you can create a DNS name like `dlp.aigw.internal` that resolves to the internal DLPoD load balancer's IP address, but only for resources inside the VPC.

This template creates a private hosted zone for `aigw.internal` and a DNS record `dlp.aigw.internal` that points to the DLPoD internal load balancer. AI Gateway instances use this name to find DLPoD — they never use a hardcoded IP address. This is important because the load balancer's IP addresses can change; the DNS name stays constant.

---

**Amazon CloudWatch**

CloudWatch is AWS's monitoring and logging service. Lambda functions write logs to CloudWatch Log Groups. You can search logs, set alarms on log patterns, and track metrics (invocation count, errors, duration) for all Lambda functions.

This template creates four log groups (one per Lambda function) with a 7-day retention period. These logs are your primary diagnostic tool when troubleshooting enrollment or tethering failures.

---

**AWS IAM (Identity and Access Management)**

IAM is AWS's permission system. It controls who (or what) can do what in your AWS account. Every EC2 instance, Lambda function, and AWS service that needs to call other AWS services must have an IAM role that explicitly grants those permissions.

This template creates nine IAM roles — one for each distinct component of the system. Each role has the minimum permissions that component needs and nothing more. This is covered in depth in the Security section.

### 1.6 Cost Overview

The following estimates assume a default deployment in us-west-1 (1 AIG instance + 1 DLPoD instance), on-demand pricing. Actual costs vary by region, traffic volume, and instance count.

| Resource | Quantity | Estimated monthly cost |
|---|---|---|
| EC2 — AI Gateway (`m5.4xlarge`) | 1 instance, on-demand | ~$550 |
| EC2 — DLP On Demand (`c5a.4xlarge`) | 1 instance, on-demand | ~$445 |
| NAT Gateway (data processing + hourly) | 1 NAT Gateway | ~$35–65 |
| ALB — AI Gateway (internet-facing) | 1 ALB | ~$20–40 |
| ALB — DLP On Demand (internal) | 1 ALB | ~$18–30 |
| Secrets Manager | 3 secrets | ~$1.20 |
| Route 53 private hosted zone | 1 zone | ~$0.50 |
| Lambda + Step Functions | Invocations at launch/scale | < $1 |
| CloudWatch Logs | Lambda + instance logs | ~$1–5 |
| ACM certificates | 2 imported certs | Free |
| **Total (1 AIG + 1 DLPoD, on-demand)** | | **~$1,070–$1,140/month** |

**Scaling impact:**
- Each additional AI Gateway instance (`m5.4xlarge`): approximately +$550/month
- Each additional DLPoD instance (`c5a.4xlarge`): approximately +$445/month
- Example: 4 AIG + 2 DLPoD = approximately $3,200–$3,500/month

**GPU-accelerated AI Gateway (optional advanced guardrails):**
- `g4dn.xlarge` (NVIDIA T4): approximately +$380/month per instance
- `g5.xlarge` (NVIDIA A10G): approximately +$760/month per instance

**Reserved Instances or Savings Plans** reduce EC2 costs by 30–60% for steady-state workloads — significant at this instance size. Use [AWS Pricing Calculator](https://calculator.aws/) for precise regional estimates.

### 1.7 Prerequisites

The following must be in place before running `create-stack`:

**AWS:**
- AWS CLI installed and configured (`aws sts get-caller-identity` must return your account ID)
- IAM permissions covering: CloudFormation, EC2, Auto Scaling, ELB, Lambda, Step Functions, Secrets Manager, SNS, Route 53, ACM, SSM Parameter Store, CloudWatch, IAM (to create roles)
- **AI Gateway AMI** subscribed in AWS Marketplace — search "Netskope AI Gateway" and accept the subscription for your region
- **DLP On Demand AMI** subscribed in AWS Marketplace — search "Netskope DLP On Demand"
- Lambda deployment artifacts (`lambda-dlpod.zip` + `pexpect-layer.zip`) uploaded to an S3 bucket in the same region as the stack — use `scripts/deploy-artifacts.sh` to do this automatically

**Netskope:**
- Netskope tenant URL in the format `https://<tenant>.goskope.com`
- Netskope RBAC v3 API token with the **AIG Administrator** role — generated from **Settings → Administration → Administrators & Roles** in the Netskope console
- DLP On Demand license key — provided by Netskope

> **Region note:** Default AMI IDs are for **us-west-1 only**. For other regions, obtain the correct AMI IDs from Netskope support after subscribing in Marketplace, and pass them as `GatewayAmiId` and `DlpodAmiId` parameters when deploying.

---

## 2. Security Considerations

This deployment is designed against the [AWS Well-Architected Framework Security Pillar](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/welcome.html), which defines best practices for securing workloads on AWS. Every design decision in this template maps to a specific security principle. This section explains those decisions in plain terms.

### 2.1 How AWS Identity and Access Management Works

Understanding IAM is essential for evaluating the security of this deployment. Here is a brief primer for readers who are not AWS practitioners.

**The problem IAM solves:** In AWS, every resource (a Lambda function, an EC2 instance, a Step Functions workflow) needs permission to call other AWS services. Without a permission model, any Lambda function could read any S3 bucket, any EC2 instance could read any secret, and so on. IAM provides the mechanism to explicitly grant and restrict those permissions.

**IAM roles:** A role is a set of permissions. EC2 instances, Lambda functions, and AWS services assume roles at runtime. For example, an EC2 instance can be assigned an IAM role that grants it `secretsmanager:GetSecretValue` on a specific secret ARN — and nothing else. If that instance tries to read any other secret, or call any other AWS service, the call fails with an `AccessDenied` error.

**Least privilege:** The AWS security best practice is to grant the minimum permissions needed for the specific task. A role that only needs to read one secret should only have permission to read that one secret — not all secrets, not all services.

**Why this matters for security:** If an EC2 instance is compromised (malware, misconfiguration, etc.), the attacker has access to whatever that instance's IAM role allows. If the role only permits reading one specific secret, the blast radius is limited. If the role had `"Action": "*", "Resource": "*"` (full admin access), a compromised instance could affect your entire AWS account.

This deployment creates nine separate IAM roles — one for each distinct component — and each role has the minimum permissions needed for that component. The following section details those roles.

### 2.2 IAM Roles in This Deployment

*Aligns with [AWS Well-Architected SEC03-BP01](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_permissions_define.html): Define access requirements and enforce least privilege.*

Nine IAM roles are created. Each role follows the naming convention `<stack-name>-<role-purpose>`, where `<stack-name>` is the name you give the CloudFormation stack at deployment time.

| Role | Assumed By | What It Can Do | What It Cannot Do |
|---|---|---|---|
| `<stack>-aig-role` | AI Gateway EC2 instances | Read the AIG bootstrap secret from Secrets Manager. Write to its CloudWatch log group. | Read API credentials. Read DLPoD secrets. Call Netskope API. Access any other AWS resource. |
| `<stack>-aig-activation-role` | AIG Activation Lambda | Read API credentials secret. Write enrollment token to AIG bootstrap secret. Read DLPoD cert from SSM. Track appliance IDs in SSM. Complete ASG lifecycle hooks. Describe EC2 instances. | Access DLPoD secrets. Access DLPoD lifecycle. Start Step Functions. |
| `<stack>-aig-lifecycle-sns-role` | Auto Scaling service | Publish AIG lifecycle events (instance launching, terminating) to the AIG SNS topic. | Access any other SNS topic, secret, or AWS resource. |
| `<stack>-dlpod-role` | DLP On Demand EC2 instances | Write to its CloudWatch log group. | Access any secret. Call Netskope API. Perform any action outside its log group. |
| `<stack>-dlpod-activation-role` | DLPoD Activation Lambda | Describe an EC2 instance to get its private IP address. Start a Step Functions execution. Complete ASG termination lifecycle hook. | Access DLPoD secrets directly. SSH to instances. |
| `<stack>-dlpod-sfn-role` | Step Functions state machine | Invoke the DLPoD tethering Lambda. | Access any other Lambda, secret, or AWS resource. |
| `<stack>-dlpod-lambda-role` | DLPoD tethering Lambda (VPC-attached) | Read DLPoD license key from Secrets Manager. Complete the DLPoD ASG launch lifecycle hook. Create/delete its VPC network interface (required for VPC-attached Lambda). Write to its CloudWatch log group. | Access AIG secrets. Access AIG lifecycle hooks. |
| `<stack>-dlpod-lifecycle-sns-role` | Auto Scaling service | Publish DLPoD lifecycle events to the DLPoD SNS topic. | Access any other SNS topic or AWS resource. |
| `<stack>-cert-generator-role` | Certificate Generator Lambda | Import and delete TLS certificates in ACM. Write and delete SSM parameters for cert storage. Write the DLP block into the AIG bootstrap secret. Write to its CloudWatch log group. | Read API credentials. Access DLPoD secrets. Perform any other action. |

**The most important security property:** AIG instances have the `<stack>-aig-role`, which grants read access to the AIG bootstrap secret only. The Netskope API token (`<stack>-api-credentials`) is a completely separate secret, and the AIG instance role has **no access to it**. If an AIG instance is compromised, the attacker cannot read the Netskope API token. They can read the bootstrap secret — which contains an enrollment token, not the API token. The enrollment token is scoped to enrolling appliances; it cannot be used to enumerate tenants, modify policies, or access other systems.

> **AWS Best Practice:** For production deployments, scope the IAM policy used to *deploy* this stack to your stack name prefix — for example, `arn:aws:iam::*:role/aigw-*` for IAM role creation. This prevents the deployer account from creating IAM roles outside the expected namespace, which would close a privilege escalation path. See the deployer IAM policy in [docs/DEPLOYMENT.md](DEPLOYMENT.md) for the full policy and this recommendation.

### 2.3 Credential and Secret Management

*Aligns with [AWS Well-Architected SEC08-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_protect_data_rest.html): Enforce encryption at rest.*

**Why not environment variables?** Lambda functions and EC2 instances both support environment variables for passing configuration. However, environment variables in Lambda are stored in plaintext in the function configuration and visible to anyone with IAM access to `lambda:GetFunctionConfiguration`. EC2 user data (the configuration script passed to instances at launch) is similarly visible. Neither mechanism is appropriate for secrets like API tokens.

**AWS Secrets Manager** solves this by storing secrets as encrypted values that are only accessible to IAM roles that have been explicitly granted permission. Secrets are never visible in the AWS console to users who do not have `secretsmanager:GetSecretValue` on that specific secret ARN.

**What is stored and where:**

| Secret | AWS Service | Contents | Who Creates It | Who Reads It |
|---|---|---|---|---|
| `<stack>-api-credentials` | Secrets Manager | Netskope API token + tenant URL | CloudFormation (from the `NetskopeApiToken` and `NetskopeTenantUrl` parameters, which are marked `NoEcho: true`) | AIG Activation Lambda only |
| `<stack>-aig-bootstrap` | Secrets Manager | AIG enrollment token (written at launch), DLP certificate PEM, DLP endpoint hostname | Cert Generator Lambda (writes DLP block at stack creation); AIG Activation Lambda (writes enrollment token at each instance launch) | AIG EC2 instances at boot |
| `<stack>-dlpod-credentials` | Secrets Manager | DLP On Demand license key | CloudFormation (from the `DlpodLicenseKey` parameter, `NoEcho: true`) | DLPoD tethering Lambda only |
| `/<stack>/dlpod-cert` | SSM Parameter Store (SecureString) | PEM-encoded DLPoD ALB TLS certificate | Cert Generator Lambda | AIG Activation Lambda (to copy into the AIG bootstrap secret) |
| `/<stack>/appliances/<instance-id>` | SSM Parameter Store | Netskope appliance ID for this specific instance | AIG Activation Lambda at instance launch | AIG Activation Lambda at instance termination |

**`NoEcho: true` — what it means:** CloudFormation parameters marked `NoEcho: true` are treated like passwords — once submitted, their values are never shown again in the CloudFormation console, stack events, or AWS CLI output. This means your Netskope API token and DLPoD license key are submitted once (when you run `create-stack`) and then never appear in any log, event, or output.

**The credential flow — how the API token becomes an enrollment token:**

```
Step 1: You provide your Netskope API token as a CloudFormation parameter (NoEcho: true)
        └─ CloudFormation creates <stack>-api-credentials in Secrets Manager (encrypted at rest)

Step 2: An AIG instance launches → lifecycle hook holds it in Pending:Wait
        └─ Auto Scaling publishes an event to SNS
        └─ AIG Activation Lambda is invoked

Step 3: AIG Activation Lambda runs:
        └─ Reads API token from <stack>-api-credentials (over TLS using AWS SDK)
        └─ Calls Netskope REST API → receives an enrollment token (in Lambda memory only)
        └─ Reads DLPoD cert PEM from SSM /<stack>/dlpod-cert
        └─ Writes enrollment token + DLP config to <stack>-aig-bootstrap (Secrets Manager)
        └─ The API token is never written to disk, never logged, exists only in Lambda memory

Step 4: AIG instance completes lifecycle hook and moves to InService
        └─ At boot, reads <stack>-aig-bootstrap → has enrollment token + DLP config
        └─ Self-enrolls with Netskope tenant
        └─ Never sees the API token
```

The API token and enrollment token are in separate secrets with separate access controls. Compromise of the bootstrap secret (which the AIG instance can read) does not expose the API token. Compromise of the API credentials secret (which only the Activation Lambda can read) does not expose instance-level enrollment tokens.

**What is explicitly not done:**
- Secrets are never placed in EC2 user data (which is visible in plaintext via the AWS API)
- Secrets are never in Lambda environment variables (visible in the function configuration)
- Secrets are never in CloudFormation output values (which are logged and visible to anyone with CloudFormation read access)
- API tokens are never passed through Step Functions execution input (execution history is visible in the console)

### 2.4 Network Security

*Aligns with [AWS Well-Architected SEC05-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_network_protection_create_layers.html): Create network layers.*

**Security groups** are AWS's virtual firewall mechanism for EC2 instances and load balancers. Each security group has explicit inbound and outbound rules. By default, security groups deny all inbound traffic and allow all outbound traffic. Rules can reference other security groups by ID — meaning you can say "allow inbound from the ALB security group" rather than specifying IP addresses, which would change as the infrastructure scales.

This deployment uses five security groups:

**AIG ALB Security Group** (protects the internet-facing load balancer)
| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | `0.0.0.0/0` (all internet) | Accept HTTPS from application clients |
| Outbound | TCP | 443 | AIG Instance Security Group | Forward to AI Gateway instances only |

**AIG Instance Security Group** (protects AIG EC2 instances)
| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | AIG ALB Security Group only | HTTPS from the load balancer — no other inbound permitted |
| Outbound | All | All | `0.0.0.0/0` | NAT Gateway → Netskope API, LLM providers, DLPoD ALB |

**DLPoD ALB Security Group** (protects the internal DLP load balancer)
| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | AIG Instance Security Group only | DLP inspection requests from AIG instances |
| Outbound | TCP | 443 | DLPoD Instance Security Group | Forward to DLP On Demand instances |

**DLPoD Instance Security Group** (protects DLPoD EC2 instances)
| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 443 | DLPoD ALB Security Group | DLP inspection requests |
| Inbound | TCP | 22 | DLPoD Lambda Security Group only | SSH for tethering automation — no other inbound SSH permitted |
| Outbound | All | All | `0.0.0.0/0` | NAT Gateway → Netskope management plane (tethering callhome) |

**DLPoD Lambda Security Group** (protects the VPC-attached tethering Lambda)
| Direction | Protocol | Port | Source / Destination | Purpose |
|---|---|---|---|---|
| Outbound | TCP | 22 | DLPoD Instance Security Group | SSH to DLPoD instances for tethering |
| Outbound | TCP | 443 | DLPoD Instance Security Group | HTTPS for tethering status polling |

**Key isolation properties:**
- No EC2 instance has a public IP address. There is no path from the internet directly to any instance.
- The only inbound internet path leads to the AIG ALB, which forwards only to AIG instances.
- DLPoD is completely isolated from the internet on the inbound side. It is reachable only from the DLPoD ALB (for DLP inspection) and the DLPoD Lambda (for tethering automation).
- SSH to DLPoD instances is only possible from the DLPoD Lambda security group. There is no SSH access from the internet, from AIG instances, or from any other source.
- AIG instances cannot be SSH-connected from anywhere. They are enrolled entirely through the bootstrap secret mechanism; no shell access is needed or permitted.

### 2.5 Encryption

**In transit** — all network communication is encrypted:

| Path | Protocol | Notes |
|---|---|---|
| Internet → AIG ALB | TLS 1.2+ | Certificate from ACM — auto-generated self-signed (default) or user-provided |
| AIG ALB → AIG instances | HTTPS on port 443 | ALB health checks and traffic forwarding both use TLS |
| AIG instances → DLPoD ALB | HTTPS on port 443 | Self-signed cert; AIG trusts the specific cert that was written to its bootstrap secret |
| DLPoD ALB → DLPoD instances | HTTPS on port 443 | Internal TLS |
| DLPoD instances → Netskope management plane | HTTPS on port 443 | Tethering callhome |
| Lambda functions → Secrets Manager / SSM | HTTPS on port 443 | AWS SDK uses TLS for all service calls |
| Lambda functions → Netskope REST API | HTTPS on port 443 | AIG enrollment and deregistration |
| DLPoD Lambda → DLPoD instances | SSH on port 22 | Tethering automation — password-based SSH |

**At rest** — all stored data is encrypted:

| Resource | Encryption | Key |
|---|---|---|
| Secrets Manager secrets | AES-256 | AWS-managed KMS key (default) |
| SSM Parameter Store (SecureString) | AES-256 | AWS-managed KMS key |
| CloudWatch Logs | Encrypted at rest by default | AWS-managed |
| EBS root volumes | Encrypted if account-level default EBS encryption is enabled | Not explicitly enforced by this template — see known limitations |

> **Action required for production:** Run `aws ec2 enable-ebs-encryption-by-default --region <region>` before deploying if your organization requires EBS encryption. The template does not enforce this explicitly.

### 2.6 What Is Explicitly Prohibited by This Design

The following patterns — common sources of credential exposure — are all explicitly avoided:

| Anti-Pattern | Why It's Risky | What This Template Does Instead |
|---|---|---|
| Secrets in EC2 user data | User data is visible in plaintext via the AWS API (`ec2:DescribeInstanceAttribute`). Anyone with that permission can read it. | EC2 user data is empty. Instances read their configuration from Secrets Manager at boot using their IAM role. |
| Secrets in Lambda environment variables | Environment variables are visible in the Lambda function configuration in the console and via `lambda:GetFunctionConfiguration`. | Lambda functions retrieve credentials at runtime from Secrets Manager using the execution role. Credentials are not present in the function configuration. |
| Secrets in CloudFormation outputs | Stack outputs are logged to CloudFormation events and visible to anyone with `cloudformation:DescribeStacks`. | `NetskopeApiToken` and `DlpodLicenseKey` are `NoEcho: true`. They are not in any output. The enrollment token is written to Secrets Manager, not output. |
| Wildcard IAM permissions (`"Resource": "*"`) | If a role has wildcard permissions and the resource it is attached to is compromised, the attacker has broad access to the account. | All IAM policies use specific resource ARNs constructed from the stack name. No `Action: "*"` or `Resource: "*"` on sensitive operations. |
| Shared IAM roles across components | One role serving multiple components increases the blast radius of a compromise. | Nine separate roles — one per functional component. |

### 2.7 Known Limitations and Accepted Risks

| Item | Detail | Mitigation |
|---|---|---|
| AIG ALB uses a self-signed TLS certificate by default | The auto-generated certificate has `aig.aigw.internal` as its Common Name/Subject Alternative Name. Browsers and API clients that perform strict TLS verification will reject this certificate by default. | For production deployments where external API clients require trusted TLS, provide a valid ACM-issued or ACM-imported certificate via the `AcmCertificateArn` parameter. The DLPoD ALB certificate is always self-signed — this is internal-only traffic, and the AIG is configured to trust the specific cert by fingerprint via the bootstrap secret. |
| DLPoD tethering uses password-based SSH | During tethering, the DLPoD Lambda connects to the DLPoD instance via SSH. It generates a unique 24-character random password, sets it on the instance, uses it for the tethering session, and then discards it. The password exists in Step Functions execution state during the tethering window only. | The password is unique per instance, random, and never reused. The SSH path is locked to DLPoD Lambda SG → DLPoD instance SG (port 22) — no other source can connect via SSH. After tethering completes, the password is not retained anywhere accessible. |
| EBS encryption not explicitly enforced | The EC2 launch templates in this stack do not set `Encrypted: true` on EBS volumes. Root volumes are encrypted only if your AWS account has account-level default EBS encryption enabled. | Enable account-level EBS encryption (`aws ec2 enable-ebs-encryption-by-default`) before deploying. |
| Secrets Manager secrets deleted on stack teardown | By default, deleting the CloudFormation stack permanently deletes all Secrets Manager secrets, including the Netskope API token. | If the API token or license key need to be preserved (for example, to redeploy the stack later), back up the values before running `delete-stack`. Alternatively, store the values in a separate Secrets Manager secret outside the stack before deploying. |

### 2.8 AWS Well-Architected Alignment Summary

The [AWS Well-Architected Framework](https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html) is Amazon's set of architectural best practices organized into six pillars. This deployment was designed against the Security and Reliability pillars in particular.

| Pillar | Design Decision | Implementation Detail |
|---|---|---|
| **Security** — Least privilege IAM | Nine dedicated roles, no shared credentials, no wildcard permissions | Each role scoped to the specific resources and actions its component needs |
| **Security** — Secrets management | API token never reaches EC2 instances | Lambda exchanges API token for enrollment token in memory; AIG instances read only the bootstrap secret |
| **Security** — No public IP on compute | All EC2 in private subnets | Internet path exists only through the AIG ALB; no direct instance access from internet |
| **Security** — Encryption in transit | All paths use TLS | AIG ALB terminates TLS; all internal paths also use HTTPS; Lambda → Secrets Manager uses TLS |
| **Security** — Sensitive parameters masked | `NoEcho: true` prevents log exposure | `NetskopeApiToken` and `DlpodLicenseKey` are marked `NoEcho: true` in the CloudFormation template |
| **Security** — Separate secrets for separate purposes | Three secrets, three access control policies | API credentials, bootstrap data, and license key are in separate Secrets Manager secrets with separate IAM grants |
| **Reliability** — Multi-AZ | Both ASGs and both ALBs span two availability zones | AZ failure reduces capacity but does not interrupt service |
| **Reliability** — Auto-replacement on failure | ASG lifecycle hooks for both launch and termination | Failed instances are replaced automatically; enrollment and tethering are fully automated |
| **Reliability** — Startup ordering enforced | Certificate generator runs before instances launch | DLP cert and endpoint pre-populated in bootstrap secret before any AIG instance boots |
| **Reliability** — Zero manual steps | Full lifecycle automation | SNS → Lambda → Step Functions handles every instance lifecycle event at launch and termination |
| **Operational Excellence** — Infrastructure as code | Single CloudFormation template | All 80+ resources version-controlled; deployments are repeatable and auditable |
| **Cost Optimization** — Demand-driven scaling | Step scaling policy on CPU threshold | AIG instances scale out at 70% average CPU; scale in when load drops |

---

## 3. Network Architecture

### 3.1 Architecture Overview

The following diagram shows the complete network topology and traffic paths:

```
                              Internet
                                  │
                    ┌─────────────▼─────────────┐
                    │   AIG Application Load     │
                    │   Balancer (HTTPS:443)      │   ← Public Subnets AZ1 + AZ2
                    │   Internet-facing           │     Terminates TLS, checks instance health
                    └───────────┬────────────────┘
                                │ HTTPS:443
              ┌─────────────────┼─────────────────┐
              │                 │                 │
     ┌────────▼──────┐                  ┌─────────▼──────┐
     │ AI Gateway    │                  │ AI Gateway     │   ← Private Subnets
     │ Instance      │                  │ Instance       │     No public IP
     │ AZ1           │                  │ AZ2            │
     └────────┬──────┘                  └─────────┬──────┘
              │                                   │
              └─────────────────┬─────────────────┘
                                │ HTTPS:443
                                │ dlp.aigw.internal (Route 53 private DNS)
                    ┌───────────▼─────────────────┐
                    │   DLPoD Internal ALB         │   ← Private Subnets AZ1 + AZ2
                    │   (HTTPS:443, internal only) │     Not reachable from internet
                    └───────────┬─────────────────┘
              ┌─────────────────┼─────────────────┐
     ┌────────▼──────┐                  ┌─────────▼──────┐
     │ DLP On Demand │                  │ DLP On Demand  │
     │ Instance AZ1  │                  │ Instance AZ2   │
     └───────────────┘                  └────────────────┘

─────────────────────────────────────────────────────────────────
NAT Gateway (Public Subnet AZ1)
     │
     ├─→ Netskope management plane (enrollment, audit logging, tethering callhome)
     ├─→ LLM providers (OpenAI, Anthropic, AWS Bedrock, etc.)
     └─→ Other outbound internet traffic

─────────────────────────────────────────────────────────────────
VPC Automation (no internet path)
     DLPoD Lambda (VPC-attached) ─SSH:22─→ DLPoD Instance (tethering automation)
```

### 3.2 How AWS Networking Works — Background

For readers who are not familiar with AWS networking, here is a brief explanation of the key concepts that appear in this architecture.

**Virtual Private Cloud (VPC):** A VPC is a logically isolated section of the AWS cloud where you launch AWS resources. Think of it as your own private data center network inside AWS. You define the IP address range (CIDR block), create subdivisions (subnets), and control routing rules. Nothing from the internet can reach resources in your VPC unless you explicitly configure it.

**Subnets:** A subnet is a segment of the VPC's IP address range assigned to a specific availability zone. Subnets are classified as public or private based on their routing:
- **Public subnets** have a route to an Internet Gateway (IGW) — resources in public subnets can send and receive traffic from the internet if they have a public IP address.
- **Private subnets** have no route to an Internet Gateway — resources in private subnets cannot be directly reached from the internet, and they cannot make outbound internet connections without a NAT Gateway.

**Availability Zones:** AWS data centers are organized into Availability Zones (AZs) — physically separate data centers within a region, connected by high-speed low-latency links. Deploying resources across two AZs means your service survives the failure of one entire data center.

**Internet Gateway:** An IGW is a VPC component that enables communication between the VPC and the internet. This template creates one IGW, attached to the VPC. Only the public subnets route through it; private subnet resources never access the IGW directly.

**NAT Gateway:** A NAT (Network Address Translation) Gateway sits in a public subnet and allows private subnet resources to initiate outbound internet connections. The NAT Gateway translates the private IP addresses to its own public IP for the outbound request, and translates the response back. This way, instances in private subnets can call the Netskope API or LLM providers without being reachable from the internet.

**Security Groups:** Virtual firewalls that control traffic to and from individual resources. Security groups are stateful — if you allow an outbound connection, the response traffic is automatically allowed in, without needing an explicit inbound rule for it.

### 3.3 VPC and Subnet Design

This template creates a dedicated VPC — no pre-existing networking is required. The VPC CIDR and subnet CIDRs are configurable parameters.

| Subnet | Availability Zone | CIDR (default) | Resources Hosted |
|---|---|---|---|
| Public Subnet 1 | AZ1 | `10.0.1.0/24` | AIG ALB node, NAT Gateway |
| Public Subnet 2 | AZ2 | `10.0.2.0/24` | AIG ALB node |
| Private Subnet 1 | AZ1 | `10.0.10.0/24` | AIG instances, DLPoD instances, DLPoD ALB node |
| Private Subnet 2 | AZ2 | `10.0.11.0/24` | AIG instances, DLPoD instances, DLPoD ALB node |

**Why only one NAT Gateway?** A single NAT Gateway in AZ1 handles all outbound internet traffic from both private subnets. A second NAT Gateway in AZ2 would eliminate the dependency on AZ1 for outbound traffic, at the cost of roughly doubling NAT Gateway charges. For a single-region deployment where the AZ1 outage scenario is acceptable, one NAT Gateway is standard practice. For the highest resilience, deploy one NAT Gateway per AZ and update the private subnet routing tables accordingly — this is a post-deployment customization.

**Routing:**
- Both public subnets route `0.0.0.0/0` to the Internet Gateway
- Both private subnets route `0.0.0.0/0` to the NAT Gateway
- All local VPC traffic (`10.0.0.0/16`) is handled by the VPC's local router

**Private DNS:**

The template creates an **Amazon Route 53 private hosted zone** for `aigw.internal`. Within this zone, a single DNS record is created:

```
dlp.aigw.internal → DLPoD internal ALB DNS name (AWS-generated)
```

This record is only resolvable from within the VPC. AI Gateway instances query this DNS name when they need to send content to DLP On Demand. Using a DNS name rather than a hardcoded IP address is important because:
1. The ALB's IP addresses can change as AWS manages its infrastructure
2. The ALB may have multiple IP addresses (one per AZ)
3. DNS allows the AIG configuration to remain static even if the DLPoD ALB is replaced

### 3.4 How Traffic Flows from the Internet to the Gateway

When an application sends an LLM API request through the AI Gateway, here is the complete network path:

1. **Application → AIG ALB DNS name:** The application resolves the AIG ALB's DNS name (available as a CloudFormation output after deployment) to an IP address in one of the public subnets.

2. **AIG ALB:** Receives the HTTPS request, terminates TLS (decrypts the connection using the TLS certificate from ACM), performs a health check to confirm the target instance is ready, and forwards the request over HTTPS to an AIG instance in a private subnet.

3. **AIG instance — inline inspection:** The AI Gateway software performs all inline inspections: access control, rate limiting, prompt injection detection. The instance then connects to DLPoD for content inspection.

4. **AIG → DLPoD ALB → DLPoD instance:** The AIG instance resolves `dlp.aigw.internal` via Route 53 (private DNS within the VPC), connects to the DLPoD internal ALB over HTTPS, and the ALB routes the content to a DLPoD instance. DLPoD applies DLP policies and returns a verdict (allow, block, or redact).

5. **AIG → LLM provider (if allowed):** If the content passes DLP inspection and all other policy checks, the AIG instance forwards the request to the upstream LLM provider API (OpenAI, Bedrock, etc.) via the NAT Gateway.

6. **LLM response → DLP inspection → client:** The LLM response comes back to the AIG instance, passes through DLP inspection again, is logged to the Netskope management plane, and is returned to the application.

Total additional latency added by the gateway (inspection overhead) is typically under 100ms for standard DLP policies. Complex DLP policies with many rules may add more.

### 3.5 AI Gateway Tier

| Attribute | Value |
|---|---|
| Instance type (default) | `m5.4xlarge` — 16 virtual CPUs, 64 GB RAM |
| Alternative instance types | `m6i.4xlarge` (newer generation), `c5.4xlarge` (compute-optimized), `g4dn.xlarge` / `g5.xlarge` (GPU-accelerated ML guardrails) |
| AMI | Netskope AI Gateway appliance (AWS Marketplace) |
| Placement | Auto Scaling Group across private subnets AZ1 + AZ2 |
| Load balancer | Internet-facing Application Load Balancer, public subnets, HTTPS port 443 |
| Desired capacity | Configurable (1–4); defaults to 1 |
| Minimum/maximum | Fixed: minimum 1, maximum 4 |
| Auto-scale trigger | Average CPU ≥ 70% for two consecutive 5-minute CloudWatch evaluation periods |
| Enrollment method | Reads bootstrap secret from Secrets Manager at boot; self-enrolls autonomously using `aig-cli` |

The AI Gateway presents an OpenAI-compatible HTTPS API to applications. Applications connect to the AIG ALB's DNS name on port 443, the same way they would connect to the OpenAI API. The gateway proxies requests to the configured upstream models while applying all governance controls.

The `m5.4xlarge` instance type is sized for the combination of DLP inspection overhead (which is CPU-intensive), prompt injection detection (ML inference), and proxy throughput. Contact Netskope support for throughput benchmarks per instance type. For workloads requiring advanced ML-based guardrails (custom models, embeddings-based detection), GPU instance types (`g4dn.xlarge` or `g5.xlarge`) can be specified via the `GatewayInstanceType` parameter.

### 3.6 DLP On Demand Tier

| Attribute | Value |
|---|---|
| Instance type (default) | `c5a.4xlarge` — 16 virtual CPUs, 32 GB RAM (AMD EPYC) |
| Alternative instance types | `c5a.8xlarge` (2×throughput), `c5a.16xlarge` (4×throughput) |
| AMI | Netskope DLP On Demand appliance (AWS Marketplace) |
| Placement | Auto Scaling Group across private subnets AZ1 + AZ2 |
| Load balancer | Internal Application Load Balancer, private subnets, HTTPS port 443 |
| DNS name | `dlp.aigw.internal` (Route 53 private hosted zone) |
| Desired capacity | Configurable (1–4); defaults to 1 |
| Tethering | SSH-based CLI automation via Step Functions (15–25 minutes per instance) |

DLP On Demand receives content from the AI Gateway over HTTPS, applies Netskope DLP policies using the full on-premises DLP engine (including fingerprinting, exact data matching, and ML classifiers), and returns a verdict. Because inspection happens inside the VPC on the DLPoD EC2 instance, **the content being inspected never leaves your AWS account**. This is a key differentiator from cloud-based DLP services, which require sending content to an external endpoint for inspection.

The `c5a` family is compute-optimized (higher CPU-to-memory ratio) — well-suited for the CPU-intensive nature of DLP regex matching, fingerprint comparison, and classification. For high-throughput deployments, scale up to `c5a.8xlarge` or `c5a.16xlarge` before scaling out to multiple instances, since larger instances are more cost-efficient per GB inspected.

### 3.7 Certificate Management

Both Application Load Balancers require TLS certificates. AWS ALBs can only use certificates stored in AWS Certificate Manager (ACM). This template manages certificates automatically.

**How the Certificate Generator works:**

A custom CloudFormation resource triggers the Certificate Generator Lambda function during stack creation, before any EC2 instances launch. The Lambda function:

1. Generates a self-signed TLS certificate for the DLPoD internal ALB (10-year validity, CA:TRUE extension so AIG trusts it as a CA cert)
2. Imports the DLPoD certificate into ACM and attaches it to the DLPoD ALB HTTPS listener
3. Stores the DLPoD certificate PEM in SSM Parameter Store at `/<stack>/dlpod-cert`
4. If no `AcmCertificateArn` was provided, generates a second self-signed certificate for the AIG ALB (`CN=aig.aigw.internal`), imports it to ACM, and attaches it to the AIG ALB HTTPS listener
5. Pre-populates the AIG bootstrap secret with the DLPoD certificate and endpoint — so the first AIG instance already has the DLP configuration it needs before it boots

**Certificate summary:**

| Certificate | ALB | How It's Created | Storage | Trust |
|---|---|---|---|---|
| AIG ALB cert | Internet-facing | Auto-generated self-signed (default), or user-provided ACM certificate ARN | ACM | Clients must trust it or skip TLS verification (unless a proper ACM cert is provided) |
| DLPoD ALB cert | Internal | Always auto-generated self-signed (10-year validity) | ACM + SSM `/<stack>/dlpod-cert` | AIG instances trust it via the specific cert written to their bootstrap secret |

**For production deployments:** If your API clients perform strict TLS certificate verification, provide a trusted certificate via the `AcmCertificateArn` parameter. This is an ACM ARN for a certificate you have already obtained — either ACM-issued (for a domain you own) or ACM-imported (for a certificate from your organization's PKI). When `AcmCertificateArn` is provided, the certificate generator skips generating a self-signed AIG ALB cert and uses the provided one instead.

### 3.8 High Availability and Failure Scenarios

The deployment is multi-AZ from the ground up. Both Auto Scaling Groups and both Application Load Balancers span AZ1 and AZ2. An AZ failure reduces capacity but does not interrupt service — the healthy AZ continues handling all traffic.

```
                AZ1                    AZ2
                ┌─────────────────┐   ┌─────────────────┐
Public          │ AIG ALB node    │   │ AIG ALB node    │
Subnets         │ NAT Gateway     │   │                 │
                │                 │   │                 │
Private         │ AIG Instance(s) │   │ AIG Instance(s) │
Subnets         │ DLPoD Instance  │   │ DLPoD Instance  │
                │ DLPoD ALB node  │   │ DLPoD ALB node  │
                └─────────────────┘   └─────────────────┘
                      ↑                     ↑
                  Both AZs active — ALBs distribute traffic across both
```

**Failure scenarios and outcomes:**

| Scenario | Immediate Impact | Automated Recovery |
|---|---|---|
| Single AIG instance fails health check | ALB stops routing to that instance; remaining instance(s) absorb the traffic | ASG terminates the failed instance and launches a replacement. Activation Lambda enrolls it automatically. Ready to serve in 5–15 minutes. |
| Single DLPoD instance fails health check | DLPoD ALB stops routing to that instance; remaining DLPoD instance(s) handle all DLP inspection | ASG terminates and replaces. Tethering automation runs automatically. Ready in 15–25 minutes. |
| AZ1 fully fails (data center outage) | AIG and DLPoD instances in AZ1 become unreachable. AZ2 instances continue serving all traffic. NAT Gateway in AZ1 also fails — AZ2 instances lose outbound internet unless routing is updated. | ALBs automatically route to healthy AZ2 targets. For outbound internet resilience, a second NAT Gateway in AZ2 is recommended for production (post-deployment customization). |
| AIG enrollment failure | Activation Lambda reports failure; ASG treats the instance as ABANDONED (fails the lifecycle hook) and automatically launches a replacement | Investigate the Activation Lambda logs to identify the root cause before the replacement arrives |
| DLPoD tethering failure | Step Functions execution reaches a failure state after all retries; ASG ABANDONS the instance after the 30-minute lifecycle hook window | Step Functions retries individual steps before marking the execution failed; check Step Functions execution history to identify which step failed |
| NAT Gateway unavailable | Outbound internet traffic from private subnets fails; DLP inspection (internal VPC traffic) is unaffected | NAT Gateway has a 99.99% AWS SLA and auto-recovers from transient failures. |

**Recovery Time Objectives:**

| Component | RTO |
|---|---|
| Single AIG instance (auto-replaced) | 5–15 minutes |
| Single DLPoD instance (auto-replaced + re-tethered) | 15–25 minutes |
| AZ failure (healthy AZ continues) | 0 seconds |
| Full stack deletion and recreate | 30–45 minutes |

**Recovery Point Objective (RPO):** Zero — both services are stateless. All configuration is stored in CloudFormation (code), the Netskope management plane (policy), and Secrets Manager (bootstrap data). No locally stored data is lost when an instance is replaced.

### 3.9 Instance Sizing Reference

| Service | Instance Type | vCPU | Memory | Use Case |
|---|---|---|---|---|
| AI Gateway | `m5.4xlarge` (default) | 16 | 64 GB | Standard DLP + guardrails workloads |
| AI Gateway | `m6i.4xlarge` | 16 | 64 GB | Newer generation; slightly better price/performance |
| AI Gateway | `c5.4xlarge` | 16 | 32 GB | Higher compute, lower memory; cost-optimized if memory is not a constraint |
| AI Gateway | `g4dn.xlarge` | 4 | 16 GB | GPU guardrails with NVIDIA T4; required for advanced ML-based detection models |
| AI Gateway | `g5.xlarge` | 4 | 16 GB | GPU guardrails with NVIDIA A10G; higher ML inference throughput |
| DLP On Demand | `c5a.4xlarge` (default) | 16 | 32 GB | Standard DLP inspection throughput |
| DLP On Demand | `c5a.8xlarge` | 32 | 64 GB | Higher throughput; prefer scale-up before scale-out |
| DLP On Demand | `c5a.16xlarge` | 64 | 128 GB | Maximum single-instance throughput |

---

## 4. Automation Flow — How It Works

### 4.1 What "Automation" Means in This Context

When you deploy this template and the CloudFormation stack reaches `CREATE_COMPLETE`, both services are already enrolled and serving. No one SSH-ed into any server. No one ran commands manually. Every enrollment and configuration step happened through automation triggered by the AWS lifecycle system.

This is not just convenient — it is a security and reliability requirement. Manual enrollment creates consistency problems (what if a step is skipped?), security risks (credentials passed over SSH sessions), and scaling problems (manual steps cannot keep up with Auto Scaling events). Every action the automation takes is logged to CloudWatch, auditable, and repeatable.

The automation system handles three distinct events:
1. **Instance launch at stack creation** — the first instance of each type is enrolled as part of stack creation
2. **Scale-out events** — when Auto Scaling adds a new instance due to CPU load, the same automation runs
3. **Instance termination** — when an instance terminates (for any reason — failure, scale-in, manual action), the automation deregisters it from Netskope

### 4.2 AWS Services Powering the Automation

Three AWS services work together to implement the automation:

**Amazon SNS (Simple Notification Service)** acts as the event bus. When an Auto Scaling Group makes a lifecycle decision (instance launching, instance terminating), it publishes a message to an SNS topic. Any subscriber to that topic receives the message. This decoupling means Auto Scaling doesn't need to know which Lambda function handles the event — it just publishes, and SNS delivers.

**AWS Lambda** provides the compute layer. Each Lambda function is a Python script that runs in response to the SNS message. For AIG, the Lambda function is embedded directly in the CloudFormation template (as an inline ZIP file) — no S3 bucket is required for the AIG standalone or combined template's AIG side. For DLPoD, the Lambda function is too large to embed inline (it includes `paramiko` for SSH automation and `pyte` for terminal emulation), so it is packaged as a ZIP file and referenced from S3.

**AWS Step Functions** orchestrates the multi-step DLPoD tethering workflow. Step Functions is used here instead of a single Lambda function because the tethering process takes 15–25 minutes and involves retries, waiting, and conditional logic — all of which Step Functions handles natively. A Lambda function has a maximum execution time of 15 minutes and cannot persist state between invocations; Step Functions can run indefinitely and maintains execution state throughout.

### 4.3 Stack Creation Sequence

When you run `aws cloudformation create-stack`, CloudFormation creates resources in dependency order. The following describes the sequence at a high level:

```
Phase 1 — Networking (parallel):
  ├─ VPC + Internet Gateway
  ├─ Public subnets (AZ1 + AZ2) + route tables → Internet Gateway
  ├─ Private subnets (AZ1 + AZ2) + route tables → NAT Gateway
  └─ NAT Gateway (in public subnet AZ1)

Phase 2 — Security + Messaging (parallel):
  ├─ 5 security groups (AIG ALB, AIG instance, DLPoD ALB, DLPoD instance, DLPoD Lambda)
  ├─ 9 IAM roles
  ├─ AIG lifecycle SNS topic + DLPoD lifecycle SNS topic
  └─ Route 53 private hosted zone + dlp.aigw.internal DNS record

Phase 3 — Lambda functions + ALBs + Secrets (parallel):
  ├─ AIG Activation Lambda (inline code)
  ├─ DLPoD Activation Lambda (inline code)
  ├─ DLPoD tethering Lambda (packaged from S3)
  ├─ Certificate Generator Lambda (inline code)
  ├─ AIG ALB + target group + listener
  ├─ DLPoD ALB + target group + listener
  ├─ <stack>-api-credentials (Secrets Manager)
  ├─ <stack>-aig-bootstrap (Secrets Manager, initially empty)
  └─ <stack>-dlpod-credentials (Secrets Manager)

Phase 4 — Certificate generation (sequential, before instances):
  └─ CertGeneratorFunction custom resource runs:
       → Generates DLPoD self-signed TLS certificate
       → Imports DLPoD cert to ACM
       → Writes DLPoD cert PEM to SSM /<stack>/dlpod-cert
       → Pre-populates <stack>-aig-bootstrap with DLP endpoint + cert
       → (If no AcmCertificateArn provided) generates + imports AIG ALB cert

Phase 5 — Auto Scaling Groups (parallel, after certs):
  ├─ GatewayAutoScalingGroup (AIG ASG) — launches AIG instances
  │    └─ Lifecycle hook holds first instance in Pending:Wait
  │    └─ SNS publishes launch event → AIG Activation Lambda → enrollment
  └─ DlpodAutoScalingGroup — launches DLPoD instances
       └─ Lifecycle hook holds first instance in Pending:Wait
       └─ SNS publishes launch event → DLPoD Activation Lambda → Step Functions → tethering

Phase 6 — CREATE_COMPLETE (when both ASGs have healthy registered targets)
```

The sequential dependency in Phase 4 is critical: the certificate generator must complete before any instances launch, so that the AIG bootstrap secret already contains the DLP certificate when the first AIG instance reads it at boot.

### 4.4 AI Gateway Enrollment — Step by Step

This flow runs automatically for every AIG instance launch — at stack creation (Phase 5 above) and at every scale-out event.

---

**Step 1: Instance launch and lifecycle hold**

The Auto Scaling Group launches a new AIG EC2 instance. As soon as the instance enters the `Pending` state, the **lifecycle hook** (`<stack>-aig-launch-hook`) intercepts it and transitions the instance to `Pending:Wait`. The instance is held in this state for up to 120 seconds. During this window, the instance is running but cannot serve traffic — it is invisible to the ALB target group. This pause is what allows the enrollment automation to run.

The lifecycle hook is configured **inline on the ASG resource** in the CloudFormation template (not as a separate `AWS::AutoScaling::LifecycleHook` resource). This is intentional: a separate lifecycle hook resource can be created after the ASG, which would allow the ASG to launch instances before the hook exists — creating a race condition. Inline hooks are guaranteed to exist at the moment the ASG is created.

---

**Step 2: SNS delivery**

The Auto Scaling Group publishes a lifecycle event message to the AIG SNS topic (`<stack>-aig-lifecycle-topic`). The message includes the instance ID, the lifecycle transition (`autoscaling:EC2_INSTANCE_LAUNCHING`), the lifecycle hook name, and a token that must be used to complete or abandon the hook.

The AIG Activation Lambda is subscribed to this SNS topic. SNS delivers the message to Lambda immediately. If the Lambda invocation fails, SNS retries with exponential backoff.

---

**Step 3: AIG Activation Lambda runs**

The Lambda function executes the following steps in order. All steps must succeed within the 120-second lifecycle hook window:

**3a. Read Netskope API credentials:**
```
secretsmanager:GetSecretValue on <stack>-api-credentials
→ {"api_token": "eyJ...", "tenant_url": "https://tenant.goskope.com"}
```
These values exist in Lambda memory only and are never logged.

**3b. Call Netskope REST API to register the appliance:**
```
POST https://tenant.goskope.com/api/v2/infrastructure/aig/appliances
Authorization: Bearer <api_token>
→ Response: {"appliance_id": "abc123", "enrollment_token": "eyJ..."}
```
The enrollment token is a time-limited credential that allows this specific appliance to enroll with the Netskope tenant. It does not grant any other access.

**3c. Read the DLPoD certificate:**
```
ssm:GetParameter on /<stack>/dlpod-cert
→ PEM-encoded TLS certificate (written here by the cert generator during stack creation)
```

**3d. Write the bootstrap secret:**
```
secretsmanager:PutSecretValue on <stack>-aig-bootstrap
→ {
    "bootstrap": true,
    "enrollment_token": "<redacted>",
    "dlp": {
      "certificate": "<PEM cert>",
      "host": "dlp.aigw.internal"
    }
  }
```
This is the only moment the enrollment token exists outside Lambda memory. It is written here so the AIG instance can read it at boot.

**3e. Record the appliance ID:**
```
ssm:PutParameter on /<stack>/appliances/<instance-id>
→ Value: "abc123"  (the Netskope appliance_id)
```
This is stored so the termination handler can look up which appliance ID to deregister when the instance terminates.

**3f. Complete the lifecycle hook:**
```
autoscaling:CompleteLifecycleAction
→ LifecycleActionResult: CONTINUE
```
This signals to Auto Scaling that enrollment succeeded. The instance transitions from `Pending:Wait` to `InService` and is registered with the ALB target group.

---

**Step 4: Instance self-enrollment**

The AIG instance boots normally after the lifecycle hook completes. Its user data contains no credentials — it is effectively empty except for a systemd service definition. At boot, the AIG appliance software:

1. Reads the bootstrap secret from Secrets Manager using the instance's IAM role (`<stack>-aig-role`)
2. Extracts the enrollment token
3. Runs `aig-cli enroll --token <enrollment_token>` to connect to the Netskope tenant
4. Reads the DLP configuration block from the bootstrap secret and configures DLP forwarding to `dlp.aigw.internal`

No manual commands. No SSH session. The instance handles its own enrollment from the credentials it reads from Secrets Manager.

---

**Step 5: ALB health check passes**

The AIG ALB performs health checks on the instance (HTTPS GET to the health check path every 30 seconds). The AIG appliance begins passing health checks once enrollment is complete and the service is running — typically 5–15 minutes from instance launch.

Once the health check passes, the ALB registers the instance as a healthy target and begins routing traffic to it.

**On instance termination** (scale-in, failure, or manual termination):

The same Activation Lambda is triggered for the `autoscaling:EC2_INSTANCE_TERMINATING` lifecycle transition. It:
1. Looks up the Netskope appliance ID from SSM (`/<stack>/appliances/<instance-id>`)
2. Calls the Netskope API to deregister the appliance
3. Deletes the SSM parameter
4. Calls `CompleteLifecycleAction: CONTINUE` so the instance finishes terminating

This ensures Netskope's inventory stays clean — terminated instances do not show up as "unreachable" in the console.

### 4.5 DLP On Demand Tethering — Step by Step

DLP On Demand tethering is more complex than AIG enrollment because it requires:
- Waiting for the DLPoD instance to finish its full boot sequence (which takes longer than AIG instances)
- Connecting via SSH to perform CLI configuration steps
- Waiting for the DLPoD appliance to "call home" to Netskope and complete its registration

This process is orchestrated by AWS Step Functions, which manages the state machine and retries.

---

**Step 1: Instance launch and lifecycle hold**

The DLPoD Auto Scaling Group launches a DLPoD instance. The lifecycle hook (`<stack>-dlpod-launch-hook`) holds the instance in `Pending:Wait` for up to **1800 seconds (30 minutes)**. This much longer window (compared to AIG's 120 seconds) accommodates the full tethering workflow, which can take 15–25 minutes.

---

**Step 2: DLPoD Activation Lambda starts a Step Functions execution**

SNS delivers the lifecycle event to the DLPoD Activation Lambda. This Lambda is lightweight — it does one thing: start a Step Functions state machine execution named `tether-<instance-id>` and pass the instance ID and lifecycle hook details as input. The Lambda then returns immediately; Step Functions takes over.

---

**Step 3: Step Functions state machine orchestrates tethering**

The state machine (`DlpodTetheringStateMachine`) calls the DLPoD Lambda function repeatedly, with a `mode` parameter telling it which step to execute. The DLPoD Lambda is VPC-attached — it runs inside the private VPC subnet so it can reach the DLPoD instance's private IP address directly.

Here is each state in the state machine:

**State: WaitForDlpodSSH**

The DLPoD Lambda attempts to open an SSH connection to the instance's private IP on port 22. DLPoD instances take 5–8 minutes to complete their boot sequence before they accept SSH connections. The Lambda retries every 25 seconds until SSH is available or the maximum retry count is reached.

This is the most common source of tethering failures: the SSH check is sensitive to network conditions and the DLPoD instance's boot time. If the DLPoD Lambda security group does not have outbound port 22 access to the DLPoD instance security group, it fails here.

*Typical duration: 5–8 minutes*

**State: DlpodChangePassword**

Once SSH is available, the Lambda logs in with the default DLPoD credentials and immediately changes the password to a randomly generated 24-character string. This password is passed through the Step Functions execution state and used for all subsequent SSH operations in this tethering session. After tethering completes, the password is not stored anywhere persistent.

*Duration: < 30 seconds*

**State: DlpodSetDNS**

The Lambda configures the DLPoD instance's DNS resolver to use the VPC's DNS server (the VPC CIDR base address + 2, e.g., `10.0.0.2` for CIDR `10.0.0.0/16`). This is required so the DLPoD instance can resolve the Netskope management plane hostname when calling home.

The `DnsServer` CloudFormation parameter must be set to the correct value for your VPC CIDR. The template calculates this automatically from the `VpcCidr` parameter using the CIDR+2 convention.

*Duration: < 30 seconds*

**State: DlpodSetLicense**

The Lambda reads the DLPoD license key from Secrets Manager (`<stack>-dlpod-credentials`) and enters it into the DLPoD CLI. The CLI automation uses `paramiko` (a Python SSH library) combined with `pyte` (a terminal emulator) to navigate the DLPoD's interactive menu-driven CLI interface. The license key is submitted and the DLPoD appliance begins its callhome sequence to Netskope.

*Duration: < 60 seconds*

**State: WaitForDlpodTetheringInit**

A fixed 120-second wait is inserted here. This gives the DLPoD appliance time to initiate its outbound HTTPS connection to the Netskope management plane before the next state starts polling. If polling starts too soon, it will see the appliance in an intermediate state and may misinterpret it as a failure.

*Duration: 2 minutes (fixed)*

**State: CheckDlpodTethering**

The Lambda polls the DLPoD CLI every 60 seconds to check the tethering status. The appliance reports its state as `Connecting`, `Syncing`, or `Connected`. The Lambda continues polling until the status is `Connected` or a maximum retry count is exceeded. This is the longest-running state — how long it takes depends on Netskope management plane responsiveness and the DLP policy size being synced.

*Duration: 5–15 minutes*

**State: DlpodCompleteLifecycle**

Once tethering is confirmed, the Lambda calls `autoscaling:CompleteLifecycleAction: CONTINUE`. The DLPoD instance transitions from `Pending:Wait` to `InService`. The DLPoD ALB health check begins evaluating the instance — once it passes, DLP inspection is active.

*Duration: < 5 seconds*

**Total tethering time: approximately 15–25 minutes from instance launch**

**Summary of tethering states:**

| State | What Happens | Typical Duration |
|---|---|---|
| `WaitForDlpodSSH` | Poll SSH port 22 every 25 seconds until the instance is ready | 5–8 min |
| `DlpodChangePassword` | Set unique random password via SSH CLI | < 30 sec |
| `DlpodSetDNS` | Configure DNS resolver to VPC DNS server | < 30 sec |
| `DlpodSetLicense` | Read license key from Secrets Manager; apply via CLI | < 60 sec |
| `WaitForDlpodTetheringInit` | Fixed wait for DLPoD callhome to initiate | 2 min |
| `CheckDlpodTethering` | Poll tethering status every 60 seconds until Connected | 5–15 min |
| `DlpodCompleteLifecycle` | CompleteLifecycleAction: CONTINUE | < 5 sec |

### 4.6 Runtime Traffic Flow — Per Request

Once both services are enrolled and healthy, here is what happens for every LLM API request:

```
1. Application sends:
   POST https://<aig-alb-dns>/v1/chat/completions
   {"model": "gpt-4o", "messages": [...]}

2. AIG ALB receives the request:
   - Terminates TLS
   - Selects a healthy AIG instance (round-robin)
   - Forwards over HTTPS to the selected instance

3. AI Gateway instance processes the request:
   a. Checks access control: is this application authorized to use model gpt-4o?
      → If not: returns 403, logs the blocked event to Netskope
   b. Checks rate limit: has this application exceeded its quota?
      → If yes: returns 429, logs the rate limit event
   c. Runs prompt injection detection on the message content
      → If detected: returns 400 with "Prompt injection detected", logs the event

4. AIG → DLP On Demand (if all inline checks pass):
   POST https://dlp.aigw.internal/api/scan
   (Content of the prompt is sent for DLP inspection)

   Route 53 resolves dlp.aigw.internal → DLPoD internal ALB IP
   DLPoD ALB routes to a healthy DLPoD instance
   DLPoD scans content → returns verdict: ALLOW / BLOCK / REDACT

   → If BLOCK: AIG returns an error to the client, logs the DLP event to Netskope
   → If REDACT: AIG redacts the flagged content and continues with the redacted prompt
   → If ALLOW: AIG continues to the upstream LLM

5. AIG → Upstream LLM provider (via NAT Gateway):
   POST https://api.openai.com/v1/chat/completions
   (The original or redacted prompt)

6. LLM response returns to AIG:
   → DLP inspection on the response content (same flow as step 4)
   → If response contains sensitive data: redact or block
   → Log the complete event (request + response + verdict) to Netskope management plane

7. AIG returns the (possibly redacted) response to the application.
```

The gateway adds one full round-trip to the DLPoD service for both the request and the response. This adds latency but keeps all content inspection inside the VPC.

### 4.7 Auto-Scaling Flow

The AI Gateway fleet scales automatically based on CPU utilization. DLP On Demand does not auto-scale — because each new DLPoD instance requires 15–25 minutes to tether, auto-scaling DLPoD would not respond quickly enough to short-lived traffic bursts. DLPoD should be right-sized at deployment time based on expected DLP throughput.

**AIG Auto-Scaling:**

```
1. Amazon CloudWatch evaluates the AIG ASG average CPU metric every 5 minutes.

2. If average CPU ≥ 70% for two consecutive 5-minute evaluation periods (10 minutes total),
   a CloudWatch Alarm transitions to the ALARM state.

3. The AIG ASG's step scaling policy adds one instance.

4. The new instance enters Pending:Wait → Activation Lambda → enrollment flow
   (identical to section 4.4, Steps 1–5)

5. New instance reaches InService and is registered with the ALB.
   (typically 5–15 minutes from scale-out trigger to traffic-ready)

6. If average CPU drops below the scale-in threshold, the ASG removes the
   excess instance. The Activation Lambda deregisters it from Netskope (section 4.8).
```

**Scaling limits:**
- Minimum instances: 1 (the ASG always maintains at least one running AIG instance)
- Maximum instances: 4 (configurable; the `DesiredCapacity` parameter sets the initial count)
- `DesiredCapacity` range: 1–4; setting it to 4 at deployment time disables scale-out (already at max)

### 4.8 Instance Termination and Deregistration

When any instance terminates — whether due to scale-in, health check failure, manual termination, or a Spot interruption — the Activation Lambda fires for the termination lifecycle event and performs cleanup.

**AIG termination flow:**
1. ASG initiates termination → lifecycle hook `<stack>-aig-term-hook` holds the instance in `Terminating:Wait`
2. SNS delivers the termination event to the AIG Activation Lambda
3. Lambda reads appliance ID from SSM (`/<stack>/appliances/<instance-id>`)
4. Lambda calls Netskope API to deregister the appliance
5. Lambda deletes the SSM parameter
6. Lambda calls `CompleteLifecycleAction: CONTINUE` → instance terminates

**DLPoD termination flow:**
1. ASG initiates termination → lifecycle hook holds the instance in `Terminating:Wait`
2. SNS delivers the event to the DLPoD Activation Lambda (lightweight Lambda only)
3. Activation Lambda calls `CompleteLifecycleAction: CONTINUE` → instance terminates

Note: DLPoD does not require an API deregistration call at termination — the Netskope management plane handles this through the tethering connection going offline.

---

## 5. Troubleshooting

### 5.1 How to Orient Yourself Before Diagnosing

Before diving into a specific issue, it helps to understand the normal progression of a deployment and where things can go wrong.

**Expected timeline for a successful deployment:**

| Time from `create-stack` | What Should Be Happening |
|---|---|
| 0–3 minutes | CloudFormation creating VPC, subnets, security groups, IAM roles, Lambda functions |
| 3–5 minutes | Certificate generator Lambda running; bootstrap secret pre-populated |
| 5–8 minutes | DLPoD instance booting (in `Pending:Wait`); AIG Activation Lambda running |
| 5–10 minutes | AIG instance launches, Activation Lambda enrolls it with Netskope |
| 8–15 minutes | AIG instance self-enrolls, ALB health check begins |
| 10–15 minutes | AIG instance passes ALB health check → `InService` |
| 10–30 minutes | DLPoD tethering progressing through Step Functions states |
| 15–30 minutes | DLPoD reaches `InService`; DLP inspection active |
| 15–35 minutes | CloudFormation `CREATE_COMPLETE` |

**The three most common failure points:**

1. **AIG Activation Lambda fails** (enrollment step, 5–10 min mark): Usually an API token problem (wrong token, expired, missing AIG Administrator role) or a network connectivity problem (NAT Gateway not reachable). The AIG instance gets ABANDONED and a replacement launches automatically, but the replacement will fail the same way.

2. **DLPoD tethering Lambda fails at `WaitForDlpodSSH`** (8–20 min mark): Usually a security group misconfiguration — the DLPoD Lambda security group doesn't have outbound port 22 access to the DLPoD instance security group. Or a DNS issue — `DnsServer` parameter is set incorrectly.

3. **DLPoD tethering Lambda fails at `DlpodSetLicense`** (20–25 min mark): Usually an invalid or expired license key.

### 5.2 Diagnostic Commands — Run These First

Replace `<stack-name>` and `<region>` with your actual values before running.

```bash
STACK=<stack-name>
REGION=<region>

# 1. Overall stack status
aws cloudformation describe-stacks \
  --stack-name $STACK \
  --query 'Stacks[0].StackStatus' \
  --output text \
  --region $REGION

# 2. All stack resources and their status (useful for identifying what is stuck)
aws cloudformation describe-stack-resources \
  --stack-name $STACK \
  --query "StackResources[*].[LogicalResourceId,ResourceType,ResourceStatus]" \
  --output table \
  --region $REGION

# 3. Recent stack events (shows CloudFormation progression and failures)
aws cloudformation describe-stack-events \
  --stack-name $STACK \
  --query "StackEvents[?ResourceStatus=='CREATE_FAILED' || ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
  --output table \
  --region $REGION

# 4. AIG instance states (healthy instances show InService + Healthy)
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names $STACK-aig-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table \
  --region $REGION

# 5. DLPoD instance states
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names $STACK-dlpod-asg \
  --query "AutoScalingGroups[0].Instances[*].[InstanceId,LifecycleState,HealthStatus]" \
  --output table \
  --region $REGION

# 6. DLPoD Step Functions executions (check for FAILED or RUNNING)
DLPOD_SFN=$(aws cloudformation describe-stack-resources \
  --stack-name $STACK \
  --query "StackResources[?LogicalResourceId=='DlpodTetheringStateMachine'].PhysicalResourceId" \
  --output text --region $REGION)
aws stepfunctions list-executions \
  --state-machine-arn $DLPOD_SFN \
  --query "executions[*].[name,status,startDate]" \
  --output table \
  --region $REGION

# 7. AIG Activation Lambda logs (last 20 minutes)
aws logs tail /aws/lambda/$STACK-aig-activation \
  --since 20m \
  --region $REGION

# 8. DLPoD tethering Lambda logs (last 60 minutes — tethering takes up to 25 min)
aws logs tail /aws/lambda/$STACK-dlpod \
  --since 60m \
  --region $REGION

# 9. DLPoD Activation Lambda logs
aws logs tail /aws/lambda/$STACK-dlpod-activation \
  --since 60m \
  --region $REGION

# 10. Certificate Generator Lambda logs (check at start if DLP not configured)
aws logs tail /aws/lambda/$STACK-cert-generator \
  --since 120m \
  --region $REGION

# 11. AIG bootstrap secret contents (check that dlp block is present)
aws secretsmanager get-secret-value \
  --secret-id $STACK-aig-bootstrap \
  --query SecretString \
  --output text \
  --region $REGION | python3 -m json.tool

# 12. All stack outputs (shows ALB DNS names, state machine ARN, etc.)
aws cloudformation describe-stacks \
  --stack-name $STACK \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table \
  --region $REGION
```

### 5.3 AI Gateway Issues

#### Issue: AIG instance is stuck in `Pending:Wait`

**What `Pending:Wait` means:** The instance has launched and the lifecycle hook is holding it before it enters service. The Activation Lambda should run within 1–2 minutes of the instance entering `Pending:Wait`. If the instance stays in this state for more than 3 minutes, the Lambda either hasn't been invoked or has failed.

**The lifecycle hook timeout is 120 seconds.** If the Lambda doesn't complete in time, the instance is ABANDONED and a replacement launches. The replacement will enter `Pending:Wait` and the same Lambda will be invoked again.

**Diagnosis steps:**

```bash
# Check AIG activation Lambda logs for this specific instance
aws logs tail /aws/lambda/$STACK-aig-activation --since 10m --region $REGION
```

**What the log will show for the most common causes:**

| Log Pattern | Cause | Solution |
|---|---|---|
| `401 Unauthorized` or `403 Forbidden` from Netskope API | The API token is wrong, expired, or the associated role doesn't have AIG Administrator permissions | Log in to the Netskope console → **Settings → Administration → Administrators & Roles** → verify the API token role includes AIG Administrator |
| `ConnectTimeout` or `ConnectionError` when calling Netskope API | The Lambda cannot reach the Netskope API endpoint — network connectivity failure | Check that the NAT Gateway is in `available` state (`aws ec2 describe-nat-gateways --region $REGION --output table`). Check the private subnet route table has a `0.0.0.0/0` route to the NAT Gateway. |
| `SSM Parameter /<stack>/dlpod-cert does not exist` | The certificate generator Lambda failed during stack creation | See the [Certificate Issues](#55-certificate-issues) section. The AIG Activation Lambda cannot write the DLP block to the bootstrap secret without this cert. |
| `ResourceNotFoundException` for the bootstrap secret | The `<stack>-aig-bootstrap` Secrets Manager secret was not created | Check CloudFormation stack resources for `AigBootstrapSecret` status. If the secret was never created, the stack has a dependency ordering problem. |
| `AccessDenied` on `secretsmanager:GetSecretValue` | The Lambda's IAM role doesn't have access to the secret | Check `<stack>-aig-activation-role` policies in the IAM console. This indicates the IAM role was not created with the correct permissions. |
| Lambda not invoked at all (no logs within 3 min of launch) | SNS subscription between the lifecycle topic and the Lambda is missing or broken | Check that the AIG Lambda SNS subscription exists: `aws sns list-subscriptions-by-topic --topic-arn <aig-lifecycle-topic-arn> --region $REGION` |

**If the Lambda is failing repeatedly:** Fix the root cause before the replacement instance launches. If you run out of time (the replacement enters `Pending:Wait` while you're still diagnosing), it will ABANDON too and another replacement will launch. Fix the root cause, then let the next replacement proceed.

---

#### Issue: AIG ALB target is stuck `unhealthy`

**Context:** Even after the lifecycle hook completes and the instance moves to `InService`, the AIG ALB target group may show the instance as `unhealthy`. This means the ALB's health check cannot successfully reach the instance.

The health check performs an HTTPS GET request to the instance's health check path every 30 seconds. The instance must return HTTP 200 within the timeout period. If two consecutive checks fail, the target is marked unhealthy.

```bash
# Find the AIG target group ARN
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'$STACK-aig')].TargetGroupArn" \
  --output text --region $REGION)

# Check health of all registered targets
aws elbv2 describe-target-health \
  --target-group-arn $TG_ARN \
  --output table \
  --region $REGION
```

| Health Check State | Likely Cause | Solution |
|---|---|---|
| `initial` | AIG enrollment has just completed; the AIG service needs a few minutes to fully start | Wait 5–10 more minutes; this is normal immediately after enrollment |
| `unhealthy` — `Target.ResponseCodeMismatch` | AIG service running but returning non-200 on the health check path | Check AIG instance system logs; enrollment may have partially failed |
| `unhealthy` — `Target.Timeout` | Health check not getting a response from the instance | Check security group: AIG ALB SG must allow outbound TCP 443 to AIG instance SG |
| `unhealthy` — `Target.ConnectionError` | TCP connection refused on port 443 | AIG service not running — likely enrollment failed silently; check AIG activation Lambda logs |
| `unused` | Instance just registered; health check not started | Wait 30–60 seconds for first health check evaluation |

---

#### Issue: DLP inspection not working after AIG enrolls

If the AIG instance is `InService` and `healthy` but DLP is not being applied, the most likely cause is that the AIG bootstrap secret does not contain the DLP configuration block.

**Step 1: Check the bootstrap secret for the DLP block**

```bash
aws secretsmanager get-secret-value \
  --secret-id $STACK-aig-bootstrap \
  --query SecretString \
  --output text \
  --region $REGION | python3 -m json.tool
```

Expected output with DLP configured:
```json
{
  "bootstrap": true,
  "enrollment_token": "[REDACTED]",
  "dlp": {
    "certificate": "-----BEGIN CERTIFICATE-----\n...",
    "host": "dlp.aigw.internal"
  }
}
```

If the `dlp` key is missing, the certificate generator failed to pre-populate the bootstrap secret. See the [Certificate Issues](#55-certificate-issues) section. AIG instances that booted without this block enrolled without DLP forwarding configured — they will need to be replaced (terminate the instance; the ASG will launch a replacement that will read the updated bootstrap secret).

**Step 2: Check that DLPoD has healthy targets**

```bash
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'$STACK-dlpod')].TargetGroupArn" \
  --output text --region $REGION)

aws elbv2 describe-target-health \
  --target-group-arn $TG_ARN \
  --output table \
  --region $REGION
```

If DLPoD has no healthy targets, DLP inspection will fail (or time out). Check DLPoD tethering status.

**Step 3: Verify Route 53 private zone resolution**

The DNS name `dlp.aigw.internal` must resolve to the DLPoD ALB from within the VPC. If the private hosted zone was not created correctly, AIG instances cannot reach DLPoD.

```bash
# Check the private hosted zone exists
aws route53 list-hosted-zones-by-name \
  --dns-name aigw.internal \
  --query "HostedZones[?Config.PrivateZone==\`true\`].[Name,Id]" \
  --output table
```

### 5.4 DLP On Demand Issues

#### Issue: DLPoD instance stuck in `Pending:Wait`

**What to check first:** Is a Step Functions execution running for this instance?

```bash
aws stepfunctions list-executions \
  --state-machine-arn $DLPOD_SFN \
  --query "executions[*].[name,status,startDate]" \
  --output table \
  --region $REGION
```

- **If there is a `RUNNING` execution:** Tethering is in progress. This is normal — check the DLPoD tethering Lambda logs to see which step it's on.
- **If there are no executions or no `RUNNING` execution for this instance:** The DLPoD Activation Lambda failed to start Step Functions. Check the Activation Lambda logs:

```bash
aws logs tail /aws/lambda/$STACK-dlpod-activation --since 30m --region $REGION
```

| Activation Lambda Log Pattern | Cause | Solution |
|---|---|---|
| `AccessDenied` starting Step Functions execution | `<stack>-dlpod-activation-role` lacks `states:StartExecution` permission | Check the IAM role in the console; this indicates a template problem |
| `StateMachineDoesNotExist` | The state machine ARN passed to the Lambda is wrong | Check CloudFormation output `DlpodTetheringStateMachineArn` matches the actual state machine |
| Lambda not invoked | SNS subscription not created | Check SNS topic subscriptions for the DLPoD lifecycle topic |

---

#### Issue: Step Functions execution FAILED

If a tethering execution has `FAILED` status, you can see exactly which step failed and why:

```bash
# Get the ARN of the most recent failed execution
EXEC_ARN=$(aws stepfunctions list-executions \
  --state-machine-arn $DLPOD_SFN \
  --status-filter FAILED \
  --query "executions[0].executionArn" \
  --output text \
  --region $REGION)

# Get the failure details from execution history
aws stepfunctions get-execution-history \
  --execution-arn $EXEC_ARN \
  --query "events[?type=='TaskFailed'].[taskFailedEventDetails.error,taskFailedEventDetails.cause]" \
  --output table \
  --region $REGION

# Get DLPoD Lambda logs around the time of the failure
aws logs tail /aws/lambda/$STACK-dlpod --since 90m --region $REGION
```

**Failure by state:**

| Failed State | Most Likely Cause | Solution |
|---|---|---|
| `WaitForDlpodSSH` | DLPoD Lambda security group cannot reach DLPoD instance on port 22, OR DLPoD instance never came up | **Security group:** Verify DLPoD Lambda SG has outbound TCP 22 to DLPoD instance SG. In the Lambda log, look for `Connection refused` (instance not ready) vs `Connection timed out` (security group blocking). If `timed out`, the security group is the issue. |
| `DlpodChangePassword` | SSH connected but the CLI automation failed — unexpected output from the DLPoD CLI, wrong default password, or SSH session dropped | Check Lambda logs for `pexpect timeout` — this means the automation script expected a specific output from the CLI (a prompt, a menu item) but didn't see it within the timeout. This can indicate a DLPoD appliance version mismatch. |
| `DlpodSetDNS` | DNS configuration step failed — usually the `DnsServer` parameter is wrong | The `DnsServer` must be your VPC CIDR's base address + 2. For VPC CIDR `10.0.0.0/16`, the DNS server is `10.0.0.2`. Check the `DnsServer` parameter value you provided. |
| `DlpodSetLicense` | License key invalid, expired, or the Lambda cannot reach Secrets Manager | Verify the license key in the Netskope console under **Settings → Security Cloud Platform → On-Premises Infrastructure**. Check that the DLPoD Lambda can reach the Secrets Manager VPC endpoint (or NAT Gateway) — it needs HTTPS access to Secrets Manager from within the VPC. |
| `CheckDlpodTethering` after multiple retries | DLPoD cannot reach the Netskope management plane to complete tethering callhome | Check NAT Gateway is operational. Check DLPoD instance SG allows all outbound traffic (required for tethering callhome). Check that the Netskope tenant URL is correct. |
| `DlpodCompleteLifecycle` | `autoscaling:CompleteLifecycleAction` failed | Verify `<stack>-dlpod-lambda-role` has `autoscaling:CompleteLifecycleAction` permission scoped to the DLPoD ASG. |

**If tethering fails permanently:** The DLPoD instance will be ABANDONED after the 30-minute lifecycle hook window. The ASG launches a replacement. If the root cause is not fixed, the replacement will fail the same way. Fix the root cause first. Common resolutions:
- Wrong `DnsServer` parameter: update the stack with `aws cloudformation update-stack` with the corrected parameter
- Bad license key: update the `<stack>-dlpod-credentials` secret in Secrets Manager, then terminate the current instance to trigger a fresh tethering attempt

### 5.5 Certificate Issues

#### Issue: DLPoD cert missing from SSM or DLP block missing from bootstrap secret

The certificate generator Lambda must succeed before any instances launch. If it fails, both services are affected: DLPoD has no certificate in ACM (DLPoD ALB HTTPS listener fails), and AIG instances boot without DLP configuration.

```bash
# Check CloudFormation events for the cert generator custom resource
aws cloudformation describe-stack-events \
  --stack-name $STACK \
  --region $REGION \
  --query "StackEvents[?LogicalResourceId=='DlpodAlbCertificate' || LogicalResourceId=='AigAlbCertificate'].[ResourceStatus,ResourceStatusReason,Timestamp]" \
  --output table

# Check cert generator Lambda logs
aws logs tail /aws/lambda/$STACK-cert-generator --since 120m --region $REGION
```

| Log Pattern | Cause | Solution |
|---|---|---|
| `AccessDenied` on `acm:ImportCertificate` | The cert generator Lambda role is missing ACM permissions | Inspect `<stack>-cert-generator-role` in IAM console — it should have `acm:ImportCertificate` on `arn:aws:acm:<region>:<account>:certificate/*` |
| `AccessDenied` on `ssm:PutParameter` | The cert generator Lambda role is missing SSM Parameter Store write permission | Check that `<stack>-cert-generator-role` includes `ssm:PutParameter` on `arn:aws:ssm:<region>:<account>:parameter/<stack>/*` |
| `AccessDenied` on `secretsmanager:PutSecretValue` | The cert generator cannot write the DLP block to the AIG bootstrap secret | Check that the role has `secretsmanager:PutSecretValue` on the `<stack>-aig-bootstrap` secret ARN |
| Lambda never invoked | The custom resource CloudFormation event did not trigger the Lambda | CloudFormation custom resources call the Lambda synchronously and wait for a response. If no Lambda invocation log exists at all, check that the Lambda function was created successfully. |
| Lambda timed out | Certificate generation took longer than the Lambda timeout | The cert generator has a 5-minute timeout — this should not be reached for self-signed certs. If it is, check for AWS API throttling. |

**If the cert generator failed after stack creation is complete:** The bootstrap secret will be missing its `dlp` block, and the DLPoD ALB listener may not have a certificate. You can re-run the cert generator by triggering a stack update — change any parameter and update the stack, which will re-run the custom resource. Alternatively, terminate all AIG instances so they re-read the bootstrap secret when they are replaced.

### 5.6 Stack-Level Issues

#### Issue: Stack creation stuck at `CREATE_IN_PROGRESS` for more than 30 minutes

```bash
# Find which resource is stuck
aws cloudformation describe-stack-events \
  --stack-name $STACK \
  --region $REGION \
  --query "StackEvents[?ResourceStatus=='CREATE_IN_PROGRESS'].[LogicalResourceId,ResourceType,Timestamp]" \
  --output table
```

| Stuck Resource | Why It Gets Stuck | Solution |
|---|---|---|
| `DlpodAlbCertificate` or `AigAlbCertificate` | CloudFormation custom resources must receive a response from the Lambda (via a pre-signed S3 URL callback). If the Lambda crashes without sending a response, CloudFormation waits up to **1 hour** before rolling back. | Check cert generator Lambda logs. If the Lambda crashed, the only options are to wait for the 1-hour timeout (stack rolls back) or delete the stack. Fix the Lambda issue before redeploying. |
| `GatewayAutoScalingGroup` | CloudFormation waits until the ASG has at least `DesiredCapacity` healthy instances in the target group. If AIG enrollment is failing repeatedly, the ASG never signals health. | Check AIG activation Lambda logs. Fix the enrollment issue (usually API token). |
| `DlpodAutoScalingGroup` | Same as AIG, for DLPoD — CloudFormation waits for DLPoD instances to be healthy. DLPoD tethering takes 15–25 minutes, so this is expected to be in progress for that long. | If stuck beyond 30 minutes, check Step Functions execution status. |

---

#### Issue: Stack deletion is hanging

```bash
# Check for instances in Terminating:Wait
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names $STACK-aig-asg $STACK-dlpod-asg \
  --query "AutoScalingGroups[*].Instances[?LifecycleState=='Terminating:Wait'].[InstanceId,LifecycleState]" \
  --output table \
  --region $REGION
```

If instances are stuck in `Terminating:Wait`, the termination lifecycle hook is waiting for the Activation Lambda to complete and call `CompleteLifecycleAction`. If the Lambda is failing (for example, because the Netskope API call failed), the instance stays in `Terminating:Wait` until the hook timeout expires (120 seconds for AIG, 30 minutes for DLPoD).

**To force the termination to proceed (use only if you are certain enrollment cleanup is not needed):**

```bash
# Force-complete the AIG termination hook for a specific instance
aws autoscaling complete-lifecycle-action \
  --lifecycle-hook-name $STACK-aig-launch-hook \
  --auto-scaling-group-name $STACK-aig-asg \
  --lifecycle-action-result CONTINUE \
  --instance-id <instance-id> \
  --region $REGION
```

**If the stack is stuck and cannot be deleted normally:**

Check for resources that might block deletion:
- SSM parameters in the `/<stack>/` namespace (must be deleted before the Parameter Store resource is deleted)
- Secrets Manager secrets in recovery mode (`aws secretsmanager list-secrets --region $REGION`)
- ENIs (Elastic Network Interfaces) attached to the DLPoD Lambda that are stuck in `detaching`

AWS CloudFormation will eventually roll forward on its own as resources time out, but this can take up to an hour per stuck resource.

### 5.7 Log Reference — What Good Looks Like

Use these patterns to confirm that everything is working correctly, and to identify the specific failure point when something goes wrong.

---

#### Successful AIG enrollment — `/aws/lambda/<stack>-aig-activation`

```
[INFO] Received SNS message for instance i-0abc123def456789
[INFO] Lifecycle event: autoscaling:EC2_INSTANCE_LAUNCHING
[INFO] Reading API credentials from Secrets Manager: <stack>-api-credentials
[INFO] Calling Netskope API: POST https://tenant.goskope.com/api/v2/infrastructure/aig/appliances
[INFO] Appliance registered successfully: appliance_id=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
[INFO] Reading DLPoD cert from SSM: /<stack>/dlpod-cert
[INFO] Writing enrollment token to bootstrap secret: <stack>-aig-bootstrap
[INFO] Writing appliance ID to SSM: /<stack>/appliances/i-0abc123def456789
[INFO] Completing lifecycle action: CONTINUE for i-0abc123def456789
[INFO] Done. Enrollment complete for i-0abc123def456789
```

Note: Actual API tokens, enrollment tokens, and certificate content are redacted (masked) in logs. The log shows that the actions occurred, but not the values.

---

#### Successful DLPoD tethering — `/aws/lambda/<stack>-dlpod`

```
[INFO] Starting tethering for DLPoD instance i-0xyz789abc123456 at 10.0.10.52
[INFO] Mode: WaitForDlpodSSH — attempting SSH connection (attempt 3/20)
[INFO] SSH connection established to 10.0.10.52:22
[INFO] Mode: DlpodChangePassword — setting new password via CLI
[INFO] Password changed successfully
[INFO] Mode: DlpodSetDNS — configuring DNS resolver to 10.0.0.2
[INFO] DNS configured successfully
[INFO] Mode: DlpodSetLicense — reading license key from Secrets Manager
[INFO] License key applied successfully
[INFO] Mode: CheckDlpodTethering — checking tethering status (attempt 1/15)
[INFO] Tethering status: Connecting
[INFO] Mode: CheckDlpodTethering — checking tethering status (attempt 4/15)
[INFO] Tethering status: Connected
[INFO] Mode: DlpodCompleteLifecycle — completing lifecycle action
[INFO] Lifecycle action complete: CONTINUE for i-0xyz789abc123456
```

---

#### Common failure patterns to search for

```
# AIG Activation Lambda failures
[ERROR] Netskope API returned 401 Unauthorized
[ERROR] Netskope API returned 403 Forbidden — check API token role permissions
[ERROR] ConnectionError: Failed to reach https://tenant.goskope.com — check NAT Gateway
[ERROR] Timeout connecting to Netskope API
[ERROR] SSM parameter /<stack>/dlpod-cert not found — cert generator may have failed
[ERROR] AccessDenied calling secretsmanager:GetSecretValue on <stack>-api-credentials

# DLPoD Tethering Lambda failures
[ERROR] SSH connection refused — DLPoD instance may not be ready (retry expected)
[ERROR] SSH connection timed out — check security group: DLPoD Lambda SG → DLPoD instance port 22
[ERROR] pexpect TIMEOUT waiting for CLI prompt — CLI automation script may not match DLPoD version
[ERROR] License key rejected: Invalid license format
[ERROR] License key rejected: License has expired
[ERROR] Tethering check failed after 15 attempts — DLPoD cannot reach Netskope management plane

# Certificate Generator Lambda failures
[ERROR] AccessDenied: acm:ImportCertificate
[ERROR] AccessDenied: ssm:PutParameter on /<stack>/dlpod-cert
[ERROR] Failed to update bootstrap secret — check secretsmanager permissions on cert-generator-role
```

---

*For full deployment instructions including S3 artifact upload, parameter reference, and CLI deploy commands, see [DEPLOYMENT.md](DEPLOYMENT.md). For day-to-day operations, scaling procedures, and monitoring, see [OPERATIONS.md](OPERATIONS.md).*
