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

This repository contains a single template: `templates/gateway-combined.yaml`. It deploys AI Gateway + DLP On Demand (+ optional AI Guardrails) together in one stack — single stack, automatic wiring between services.

The template is **self-contained**: it creates its own VPC, subnets, security groups, and all supporting resources. No pre-existing infrastructure is required. The template automatically generates the DLP certificate, shares it with the AIG configuration, and wires the two services together — this handoff happens before any instances launch, so there is no window during which AIG is running without DLP configured.

> **Standalone templates** for deploying AIG or DLPoD individually (e.g. for POV testing or adding to an existing VPC) are in a separate repository: [AWS-POV-Templates-CFT](https://github.com/jharris-ns/AWS-POV-Templates-CFT).

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

