# DLP On Demand — Standalone Template

`dlpod/template/gateway-dlpod.yaml` deploys Netskope DLP On Demand appliances in an Auto Scaling Group behind a private internal ALB. Each instance is automatically tethered to the Netskope management plane via SSH/CLI automation orchestrated by a Step Functions state machine.

> **Deploying DLP On Demand together with AI Gateway?** Use the combined template at `templates/gateway-combined.yaml` — see the [root README](../README.md).

---

## Contents

| Path | Description |
|---|---|
| `template/gateway-dlpod.yaml` | CloudFormation template |
| `docs/DEPLOYMENT.md` | Prerequisites, parameters, deploy commands, verification |
| `docs/OPERATIONS.md` | Tethering flow, scaling, troubleshooting, IAM roles |
| `scripts/deploy-artifacts.sh` | Build and upload Lambda artifacts to S3 |
| `scripts/dlpod_handlers.py` | DLP On Demand tethering Lambda source |
| `scripts/build-tui-layer.sh` | Build paramiko/pyte Lambda Layer (run inside Docker/Podman) |

## Quick deploy

```bash
# 1. Build and upload Lambda artifacts (run from repository root)
dlpod/scripts/deploy-artifacts.sh <region>

# 2. Upload template
aws s3 cp dlpod/template/gateway-dlpod.yaml \
  s3://<bucket>/templates/gateway-dlpod.yaml --region <region>

# 3. Deploy
aws cloudformation create-stack \
  --stack-name <stack-name> \
  --template-url https://<bucket>.s3.<region>.amazonaws.com/templates/gateway-dlpod.yaml \
  --parameters \
    ParameterKey=DlpodLicenseKey,ParameterValue=<license-key> \
    ParameterKey=LambdaCodeBucket,ParameterValue=<bucket> \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full parameter reference, AMI selection, and verification steps.
