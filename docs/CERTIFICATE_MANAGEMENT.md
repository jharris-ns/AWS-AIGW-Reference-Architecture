# Certificate Management

## Overview

Certificates for both the AI Gateway ALB and the DLPoD ALB are automatically generated at stack creation time by a shared Lambda custom resource. No manual certificate creation, import, or renewal is required.

## How It Works

The `CertGeneratorFunction` Lambda runs as a CloudFormation custom resource during stack creation. It:

1. Generates a 2048-bit RSA private key using OpenSSL
2. Creates a self-signed X.509 certificate with:
   - CN matching the configured domain name
   - SAN (Subject Alternative Name) matching the CN
   - `basicConstraints = critical, CA:TRUE` (required by the AIG DLP service)
   - `keyUsage = digitalSignature, keyEncipherment, keyCertSign`
   - `extendedKeyUsage = serverAuth`
   - 365-day validity
3. Imports the certificate and private key into ACM
4. Stores the certificate PEM in SSM Parameter Store (for AIG DLP TUI configuration)

## Certificates Generated

### Gateway ALB Certificate

- **Domain**: Value of `GatewayAlbDomainName` parameter (default: `aigw.internal`)
- **Used by**: Gateway ALB HTTPS listener
- **ACM ARN**: Available in stack outputs as `GatewayAlbCertificateArn` (if outputted) or via `aws acm list-certificates`

### DLPoD ALB Certificate (conditional)

- **Domain**: Value of `DlpDomainName` parameter (default: `dlp.aigw.internal`)
- **Used by**: DLPoD private ALB HTTPS listener
- **SSM Parameter**: `/<stack-name>/dlpod-cert` -- contains the PEM, used by the AIG enrollment Lambda to configure DLP via TUI
- **Only created when**: `DlpodAmiId` parameter is provided

## Certificate Requirements

The AIG's DLP management service validates certificates strictly:

- **CA:TRUE** -- The certificate must have `basicConstraints = critical, CA:TRUE`. Certificates with `CA:FALSE` are rejected with error "certificate must be a CA certificate".
- **PEM format for TUI** -- When pasted into the AIG TUI, the base64 body must be on a **single line** (no line wrapping). The BEGIN/END markers are on separate lines. Standard PEM files with 64-char line wrapping must be collapsed before pasting.

## Viewing Certificates

```bash
# List ACM certificates
aws acm list-certificates --query "CertificateSummaryList[*].[DomainName,CertificateArn]" --output table

# View certificate details
aws acm describe-certificate --certificate-arn <arn> --query "Certificate.[DomainName,NotAfter,Status]"

# View DLPoD cert stored in SSM
aws ssm get-parameter --name "/<stack-name>/dlpod-cert" --query "Parameter.Value" --output text

# Decode and inspect a cert
aws ssm get-parameter --name "/<stack-name>/dlpod-cert" --query "Parameter.Value" --output text | openssl x509 -noout -subject -dates -ext basicConstraints
```

## Certificate Renewal

Certificates are valid for 365 days. To renew:

1. Update the stack with a changed `CertVersion` property on the custom resource (forces regeneration)
2. Or delete and recreate the stack

Auto-generated certificates are self-signed and not publicly trusted. For production deployments requiring publicly trusted certificates, replace the cert generator custom resource with ACM public certificate requests using DNS validation.

## Production Considerations

For production deployments with public-facing AI Gateway endpoints:

- Use ACM public certificates with DNS validation instead of self-signed certs
- Configure Route 53 public hosted zones for automatic DNS validation
- Self-signed certs are appropriate for internal/private endpoints (DLPoD ALB is always internal)
