# AI Gateway — Standalone Template

`aig/template/gateway-aig.yaml` deploys Netskope AI Gateway in an Auto Scaling Group behind an internet-facing ALB. Enrollment is fully automated via native Secrets Manager bootstrap — no SSH, no Step Functions, no Lambda layer.

> **Deploying AI Gateway together with DLP On Demand?** Use the combined template at `templates/gateway-combined.yaml` — see the [root README](../README.md).

---

## Contents

| Path | Description |
|---|---|
| `template/gateway-aig.yaml` | CloudFormation template |
| `docs/DEPLOYMENT.md` | Prerequisites, parameters, deploy commands, verification |
| `docs/OPERATIONS.md` | Enrollment flow, scaling, troubleshooting, IAM roles |

## Quick deploy

The template is ~34 KB — deploy directly from a local file (no S3 upload required):

```bash
aws cloudformation create-stack \
  --stack-name <stack-name> \
  --template-body file://aig/template/gateway-aig.yaml \
  --parameters \
    ParameterKey=NetskopeTenantUrl,ParameterValue=https://tenant.goskope.com \
    ParameterKey=NetskopeApiToken,ParameterValue=<token> \
    ParameterKey=AcmCertificateArn,ParameterValue=<cert-arn> \
    ParameterKey=Project,ParameterValue=aigw \
    ParameterKey=Environment,ParameterValue=prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <region>
```

No Lambda artifacts to build — all Lambda code is inline in the template.

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full parameter reference, AMI selection, and verification steps.
