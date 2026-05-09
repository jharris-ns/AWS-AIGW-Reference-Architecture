# Netskope AI Gateway — Auto Scaling Deployment

## Overview

This CloudFormation template (`templates/gateway-asg.yaml`) deploys the Netskope AI Gateway as an auto-scaling cluster behind an Application Load Balancer. It automates appliance enrollment, DLP configuration, and lifecycle management so that instances are fully configured before receiving traffic and cleanly deregistered on termination.

![Architecture Diagram](docs/architecture.png)

---

## Prerequisites

Before deploying, ensure the following are in place.

### 1. Netskope AI Gateway AMI

The AI Gateway is deployed from a shared AMI provided by Netskope. This AMI must be available in your target AWS account and region.

- If the AMI is not in your account, contact your Netskope representative to have it shared, or copy it from the source account using `aws ec2 copy-image`.
- The AMI includes the AI Gateway appliance services (VAM, DMS, traffic intercept, ext-authz, RLS), SSH support for Lambda automation, and all required dependencies.
- Pass the AMI ID as the `GatewayAmiId` parameter when deploying.

### 2. Netskope Tenant Credentials

You need:
- **Tenant URL** — Your Netskope tenant URL (e.g., `https://tenant.goskope.com`). Do not include `/api/v2` — the template appends the API path automatically.
- **API Token** — An RBAC v3 service account token with permissions to manage AI Gateway appliances.

These credentials are stored in AWS Secrets Manager by the template and are never exposed to the gateway instances.

#### Generating an RBAC v3 Service Account Token

Netskope RBAC v3 is the current mandatory access control model. It uses service accounts with role-based permissions for API access, replacing the legacy REST API v2 token model.

1. **Create a Role** — In your Netskope tenant, navigate to **Settings > Administration > Administrators & Roles > Roles** and click **New**. Assign the permissions required for AI Gateway management (On-Premises Infrastructure). Save the role.

2. **Create a Service Account** — Navigate to **Settings > Administration > Administrators & Roles > Administrators**. Click **Add** and select **Service Account**. Assign the role created in step 1 and click **Create**.

3. **Copy the Token** — The token is displayed only once after creation. Copy it immediately and store it securely. This is the value you pass as the `NetskopeApiToken` parameter.

> **Note:** Existing REST API v2 tokens (`Settings > Tools > REST API v2`) continue to work until expiry but cannot be renewed. New deployments should use RBAC v3 service accounts. If a token is lost, it can be regenerated from the service account settings.

For more information, see:
- [Netskope RBAC V3 Overview](https://docs.netskope.com/en/netskope-rbac-v3-overview/)
- [Administrators RBAC V3](https://docs.netskope.com/en/administrators-rbac-v3/)
- [Service Account Migration](https://docs.netskope.com/en/service-account-migration-and-netskope-client-auditing/)

### 3. ACM Certificate

An AWS Certificate Manager (ACM) certificate is required for the ALB HTTPS listener. You can:
- Request a public certificate through ACM (requires DNS or email validation).
- Import an existing certificate (including self-signed for testing).

Note the certificate ARN — it is a required stack parameter. See [CERTIFICATE_MANAGEMENT.md](docs/CERTIFICATE_MANAGEMENT.md) for step-by-step instructions on creating and importing certificates (macOS, Linux, and Windows).

### 4. Lambda Deployment Artifacts

The enrollment Lambda and its Layer must be built and uploaded to an S3 bucket in the deployment region. A script is provided that handles bucket creation, builds, and uploads:

```bash
scripts/deploy-artifacts.sh us-west-1
```

See [DEPLOYMENT.md](docs/DEPLOYMENT.md) for manual steps and update procedures.

### 5. DLPoD Appliance (Optional)

If you want DLP content inspection, deploy a Netskope DLP On Demand appliance separately. It must be reachable from the AI Gateway instances over HTTPS (port 443). If the DLPoD is in a different VPC, you need VPC peering or Transit Gateway connectivity. Pass the DLPoD's URL as the `DlpHostUrl` parameter.

### 6. AWS Services and IAM Permissions

The template creates resources across the following AWS services:

| Service | Resources Created |
|---------|------------------|
| **EC2** | Launch Template, Security Groups, VPC (optional), Subnets (optional), VPC Endpoints (optional) |
| **Auto Scaling** | Auto Scaling Group, Lifecycle Hooks |
| **Elastic Load Balancing** | Application Load Balancer, Target Group, HTTPS Listener |
| **Lambda** | 2 Functions (activation + enrollment), Layer, Permission |
| **Step Functions** | State Machine (enrollment orchestration) |
| **IAM** | 5 Roles (gateway, activation Lambda, enrollment Lambda, Step Functions, lifecycle SNS), Instance Profile |
| **Secrets Manager** | 2 Secrets (Netskope credentials, SSH private key) |
| **SNS** | Topic, Subscription |
| **CloudWatch Logs** | Log Group (Lambda) |

The IAM principal deploying the stack needs the permissions listed below. The stack uses `CAPABILITY_NAMED_IAM` because it creates IAM roles with explicit names.

You can create a dedicated IAM role with this policy and assume it before deploying. See the AWS documentation for:
- [Creating IAM roles](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create.html)
- [Assuming a role with the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html)

<details>
<summary>IAM policy JSON (click to expand)</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFormation",
      "Effect": "Allow",
      "Action": "cloudformation:*",
      "Resource": "*"
    },
    {
      "Sid": "EC2",
      "Effect": "Allow",
      "Action": "ec2:*",
      "Resource": "*"
    },
    {
      "Sid": "ELB",
      "Effect": "Allow",
      "Action": "elasticloadbalancing:*",
      "Resource": "*"
    },
    {
      "Sid": "AutoScaling",
      "Effect": "Allow",
      "Action": "autoscaling:*",
      "Resource": "*"
    },
    {
      "Sid": "Lambda",
      "Effect": "Allow",
      "Action": "lambda:*",
      "Resource": "*"
    },
    {
      "Sid": "IAM",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PassRole",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:CreateInstanceProfile",
        "iam:DeleteInstanceProfile",
        "iam:GetInstanceProfile",
        "iam:AddRoleToInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SecretsManager",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:CreateSecret",
        "secretsmanager:DeleteSecret",
        "secretsmanager:DescribeSecret",
        "secretsmanager:TagResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "StepFunctions",
      "Effect": "Allow",
      "Action": [
        "states:CreateStateMachine",
        "states:DeleteStateMachine",
        "states:UpdateStateMachine",
        "states:DescribeStateMachine",
        "states:TagResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SNS",
      "Effect": "Allow",
      "Action": [
        "sns:CreateTopic",
        "sns:DeleteTopic",
        "sns:Subscribe",
        "sns:Unsubscribe",
        "sns:GetTopicAttributes",
        "sns:SetTopicAttributes",
        "sns:TagResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:DescribeLogGroups",
        "logs:PutRetentionPolicy",
        "logs:TagResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3TemplateAccess",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::netskope-aigw-templates-*/*"
    },
    {
      "Sid": "ACM",
      "Effect": "Allow",
      "Action": [
        "acm:DescribeCertificate",
        "acm:ListCertificates"
      ],
      "Resource": "*"
    }
  ]
}
```

</details>

---

## Upload Templates to S3

The CloudFormation templates exceed the 51,200-byte inline limit for `--template-body`. Upload them to an S3 bucket before deployment. The bucket must be in the same region as the stack you are deploying.

### Create the bucket

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-1
BUCKET="netskope-aigw-templates-${ACCOUNT_ID}"

aws s3 mb "s3://${BUCKET}" --region "${REGION}"
```

### Upload templates

```bash
aws s3 cp templates/gateway-asg.yaml "s3://${BUCKET}/templates/gateway-asg.yaml"
aws s3 cp scripts/lambda-step-function.zip "s3://${BUCKET}/lambda-step-function.zip"
aws s3 cp scripts/pexpect-layer.zip "s3://${BUCKET}/layers/pexpect-layer.zip"
```

### Bucket layout

```
s3://<bucket>/
  templates/
    gateway-asg.yaml          # Auto Scaling deployment (~59 KB)
  lambda-step-function.zip    # Enrollment Lambda package
  layers/
    pexpect-layer.zip         # paramiko/pyte Lambda Layer
```

### Template URL format

```
https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-asg.yaml
```

The bucket does not need to be public. CloudFormation fetches the template using the caller's IAM credentials. For same-account deployments, the `s3:GetObject` permission in the deploying role is sufficient. For cross-account deployments, additionally add a bucket policy granting `s3:GetObject` to the deploying account.

---

## Deployment

### Deploy with an existing VPC

```bash
aws cloudformation create-stack \
  --stack-name my-aigw \
  --template-url "https://${BUCKET}.s3.${REGION}.amazonaws.com/templates/gateway-asg.yaml" \
  --parameters \
    ParameterKey=ExistingVpcId,ParameterValue=vpc-xxxxxxxxx \
    ParameterKey=ExistingPublicSubnetId,ParameterValue=subnet-pub1 \
    ParameterKey=ExistingPublicSubnet2Id,ParameterValue=subnet-pub2 \
    ParameterKey=ExistingPrivateSubnetId,ParameterValue=subnet-priv1 \
    ParameterKey=ExistingPrivateSubnet2Id,ParameterValue=subnet-priv2 \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=AcmCertificateArn,ParameterValue=arn:aws:acm:<region>:<account>:certificate/<id> \
    ParameterKey=DlpHostUrl,ParameterValue=https://dlpod.internal \
    ParameterKey=LambdaCodeBucket,ParameterValue=${BUCKET} \
  --capabilities CAPABILITY_NAMED_IAM
```

When using an existing VPC, review the [VPC Requirements](#vpc-requirements-existing-vpc) section to ensure your network is correctly configured.

### Deploy with a new VPC

Omit all `Existing*` parameters (or set them to empty strings). The template creates a VPC with public subnets, private subnets, an internet gateway, a NAT gateway, route tables, and all required VPC endpoints.

### Outputs

After deployment, the stack provides these outputs:

| Output | Description |
|--------|-------------|
| `ALBDnsName` | ALB DNS name — point your DNS (Route 53 CNAME or alias) to this |
| `ALBHostedZoneId` | ALB canonical hosted zone ID (for Route 53 alias records) |
| `AutoScalingGroupName` | ASG name for scaling operations |
| `VpcId` | VPC ID (created or existing) |
| `PublicSubnetId` | Subnet 1 ID (created or existing) |
| `GatewaySecurityGroupId` | Gateway security group ID |

---

## Stack Parameters

### Netskope Tenant Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `NetskopeTenantUrl` | Yes | Netskope tenant URL (e.g., `https://tenant.goskope.com`). Do not include the API path. |
| `NetskopeApiToken` | Yes | API token for appliance registration. Stored in Secrets Manager (NoEcho). |
| `DlpHostUrl` | No | DLPoD HTTPS endpoint. Leave empty to skip DLP configuration. |

### Network Configuration

The template uses a split-subnet architecture: **public subnets** for the ALB and **private subnets** for the gateway instances. The private subnets route outbound traffic through a NAT gateway.

**Option A: Create a new VPC** — Leave all `Existing*` parameters empty. The template creates a VPC with public subnets, private subnets, an internet gateway, a NAT gateway, route tables, and all required VPC endpoints.

**Option B: Use an existing VPC** — Provide the VPC ID, two public subnet IDs (for the ALB), and two private subnet IDs (for the instances). The template creates only the security groups and deploys into your existing network. See [VPC Requirements](#vpc-requirements-existing-vpc).

| Parameter | Required | Description |
|-----------|----------|-------------|
| `ExistingVpcId` | No | Existing VPC ID. Empty creates a new VPC. |
| `ExistingPublicSubnetId` | No | Existing public subnet for ALB (AZ 1). Empty creates new. |
| `ExistingPublicSubnet2Id` | No | Existing public subnet for ALB (AZ 2). Empty creates new. |
| `ExistingPrivateSubnetId` | No | Existing private subnet for instances (AZ 1). Empty creates new. |
| `ExistingPrivateSubnet2Id` | No | Existing private subnet for instances (AZ 2). Empty creates new. |
| `VpcCidr` | No | CIDR for new VPC (ignored when using existing). |
| `PublicSubnetCidr` | No | CIDR for new public subnet 1. |
| `PublicSubnet2Cidr` | No | CIDR for new public subnet 2. |
| `PrivateSubnetCidr` | No | CIDR for new private subnet 1. |
| `PrivateSubnet2Cidr` | No | CIDR for new private subnet 2. |

### Gateway Instance Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `GatewayAmiId` | Yes | Netskope AI Gateway AMI ID. Obtain from your Netskope representative. |
| `InstanceType` | No | Instance type. Minimum 16 vCPU, 64 GiB RAM. Allowed: m5.4xlarge, m6i.4xlarge, c5.4xlarge. |
| `LambdaCodeBucket` | Yes | S3 bucket containing Lambda package and Layer. |
| `LambdaCodeKey` | No | S3 key for enrollment Lambda (default: `lambda-step-function.zip`). |
| `LambdaLayerKey` | No | S3 key for paramiko/pyte Layer (default: `layers/pexpect-layer.zip`). |

> **Note:** Advanced AI guardrails (beyond basic policy enforcement) require a CUDA-capable NVIDIA GPU. If advanced guardrails are needed, use GPU instance types (e.g., g4dn.4xlarge, g5.4xlarge) and update the `AllowedValues` constraint in the template.

### Load Balancer Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `AcmCertificateArn` | Yes | ACM certificate ARN for the ALB HTTPS listener. |

### Auto Scaling Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `MinCapacity` | No | Minimum number of gateway instances. |
| `MaxCapacity` | No | Maximum number of gateway instances. |
| `DesiredCapacity` | No | Initial number of gateway instances. |

### Tagging

| Parameter | Required | Description |
|-----------|----------|-------------|
| `Project` | No | Project name applied as a tag to all resources. |
| `Environment` | No | Environment tag (dev, staging, prod). |

---

## VPC Requirements (Existing VPC)

When deploying into an existing VPC, you are responsible for ensuring the VPC meets the following requirements. The template does not validate these at deployment time — failures will manifest as VPC endpoint connectivity issues or Lambda timeouts.

### Subnets

The template requires **four subnets** — two public and two private, each pair in different availability zones.

**Public subnets** (for the ALB):
- Must have a route to an internet gateway (the ALB is internet-facing).
- Two subnets in different AZs are required (ALB multi-AZ requirement).
- These subnets only host the ALB ENIs, not the gateway instances.

**Private subnets** (for the gateway instances):
- Must have a route to a NAT gateway for outbound internet access (to reach the Netskope tenant API and LLM providers).
- The ALB routes to instances using their private IPs — instances do not need public IPs.
- Two subnets in different AZs ensure multi-AZ instance placement.
- VPC endpoints (see below) can reduce dependency on the NAT gateway for AWS service calls.

### VPC Endpoints and NAT Gateway

The Enrollment Lambda runs in the VPC private subnets and needs access to:

| Service | Access method | Purpose |
|---------|--------------|---------|
| Secrets Manager | VPC endpoint (created by template) | Read SSH private key |
| ASG / Step Functions APIs | NAT gateway | Complete lifecycle action, AWS API calls |
| Gateway instances (SSH) | Direct VPC connectivity | TUI enrollment over port 22 |

The private subnets **must have a NAT gateway** for outbound internet access. The gateway instances also require NAT for Netskope policy updates and LLM provider connectivity.

The template creates a Secrets Manager VPC endpoint in all deployments. An S3 gateway endpoint is created when deploying a new VPC.

### DNS Resolution

- The VPC must have **DNS support** and **DNS hostnames** enabled (`EnableDnsSupport: true`, `EnableDnsHostnames: true`).
- The Secrets Manager VPC endpoint must have **Private DNS enabled** so the Lambda can resolve `secretsmanager.<region>.amazonaws.com` to the endpoint's private IP.

### DLPoD Network Connectivity

If using DLP (`DlpHostUrl` is set), the AI Gateway instances must be able to reach the DLPoD appliance on HTTPS (port 443). If the DLPoD is:

- **In the same VPC and subnet** — No additional configuration needed.
- **In a different subnet in the same VPC** — Ensure route table entries and security groups allow traffic.
- **In a different VPC** — VPC peering or Transit Gateway is required, along with route table entries in both VPCs and security group rules on the DLPoD allowing inbound 443 from the gateway VPC CIDR.
- **On a public IP** — The gateway instances need outbound internet access. Note that the DLPoD's TLS certificate CN/SAN must match the hostname or IP used in `DlpHostUrl`, or the AI Gateway's TLS verification will reject the connection.

---

## About the Netskope AI Gateway

### What It Does

The Netskope AI Gateway is a software appliance that intercepts and secures traffic between AI agents, applications, and LLMs. It sits inline on the request/response path and provides:

- **Data Loss Prevention (DLP)** — Monitors prompts and responses for sensitive data (PII, credentials, proprietary content) and blocks or redacts as configured by policy.
- **Content Moderation** — AI guardrail protection that detects and blocks prompt injection attacks, inappropriate content, and policy-violating interactions.
- **Authentication** — Manages secure access to LLMs through token-based authentication, ensuring only authorized applications and users can reach models.
- **Rate Limiting** — Controls the volume of requests per consumer to prevent abuse, manage costs, and ensure fair resource allocation.
- **Monitoring and Compliance** — Logs all AI interactions for audit trails, tracks usage patterns, and ensures interactions meet corporate governance standards.
- **Unified API** — Provides a single, consistent API interface (OpenAI-compatible) for interacting with multiple AI model providers, simplifying application integration.

Applications send requests to the AI Gateway instead of directly to the LLM provider. The gateway inspects the request, applies security policies, forwards permitted requests to the model, inspects the response, and returns it to the application. Blocked or non-compliant requests receive a policy-defined response without reaching the model.

### How DLP On Demand (DLPoD) Integrates

The AI Gateway can optionally integrate with a **Netskope DLP On Demand (DLPoD)** appliance for deep content inspection. DLPoD is a separate appliance that provides local, collocated document and text scanning via a REST API. It supports both structured and unstructured content analysis, including text extracted from LLM prompts and responses.

When configured, the AI Gateway forwards prompt and response content to the DLPoD appliance for DLP policy evaluation before allowing the traffic to proceed. This keeps sensitive content inspection local to your environment — data does not leave your VPC for DLP scanning.

The DLPoD appliance must be deployed separately (it is not included in this template) and must be network-reachable from the AI Gateway instances. The `DlpHostUrl` parameter points the gateway to the DLPoD's HTTPS endpoint.

---

## Related Documentation

- [Netskope AI Gateway Documentation](https://docs.netskope.com/en/ai-gateway/)
- [Deploy AI Gateway on Netskope Portal](https://docs.netskope.com/en/deploy-ai-gateway-on-netskope-portal/)
- [AI Gateway Sizing Guidelines](https://docs.netskope.com/en/ai-gateway-sizing-guidelines/)
- [DLP On Demand Documentation](https://docs.netskope.com/en/data-loss-prevention-on-demand/)
- [DEPLOYMENT.md](docs/DEPLOYMENT.md) — Building artifacts, uploading to S3, and deploying the stack
- [CERTIFICATE_MANAGEMENT.md](docs/CERTIFICATE_MANAGEMENT.md) — Creating and importing ACM certificates
- [DEVOPS.md](docs/DEVOPS.md) — Operational runbook covering instance lifecycle, scaling, secrets management, and troubleshooting
