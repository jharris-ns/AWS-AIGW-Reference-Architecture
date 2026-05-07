# Certificate Management for the AI Gateway ALB

The Application Load Balancer requires an SSL/TLS certificate for its HTTPS listener. This guide covers how to create and import certificates into AWS Certificate Manager (ACM) for use with the AI Gateway stack.

---

## Option 1: Request a Public ACM Certificate (Production)

This is the recommended approach for production deployments. ACM issues a free, auto-renewing certificate validated against a domain you own.

### Prerequisites

- A registered domain name
- Access to DNS records (Route 53 or another DNS provider) OR access to the domain's admin email

### Steps

```bash
REGION=us-west-1
DOMAIN=aigw.example.com

aws acm request-certificate \
  --domain-name "${DOMAIN}" \
  --validation-method DNS \
  --region "${REGION}" \
  --query "CertificateArn" \
  --output text
```

This returns a certificate ARN and creates a pending validation request.

#### Complete DNS Validation

```bash
# Get the validation CNAME record details
CERT_ARN=<arn from previous step>

aws acm describe-certificate \
  --certificate-arn "${CERT_ARN}" \
  --region "${REGION}" \
  --query "Certificate.DomainValidationOptions[0].ResourceRecord"
```

Add the returned CNAME record to your DNS zone. If using Route 53:

```bash
# The CNAME name and value from the previous command
aws route53 change-resource-record-sets \
  --hosted-zone-id <zone-id> \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "<CNAME name from above>",
        "Type": "CNAME",
        "TTL": 300,
        "ResourceRecords": [{"Value": "<CNAME value from above>"}]
      }
    }]
  }'
```

Validation typically completes within a few minutes. Check status:

```bash
aws acm describe-certificate \
  --certificate-arn "${CERT_ARN}" \
  --region "${REGION}" \
  --query "Certificate.Status"
```

Once the status is `ISSUED`, the certificate is ready for use.

---

## Option 2: Import a Self-Signed Certificate (Testing/Lab)

For lab, demo, or testing environments where you don't have a domain or don't need browser trust, you can generate a self-signed certificate and import it into ACM.

> **Note:** Browsers and API clients will show certificate warnings with self-signed certificates. This is acceptable for testing but not for production.

### macOS / Linux

#### Generate the certificate

```bash
# Generate a private key and self-signed certificate (valid for 365 days)
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout aigw.key \
  -out aigw.crt \
  -subj "/CN=aigw.lab.internal"
```

#### Import into ACM

```bash
REGION=us-west-1

CERT_ARN=$(aws acm import-certificate \
  --certificate fileb://aigw.crt \
  --private-key fileb://aigw.key \
  --region "${REGION}" \
  --query "CertificateArn" \
  --output text)

echo "Certificate ARN: ${CERT_ARN}"
```

#### Clean up local key material

```bash
# Remove the private key from your local machine after import
rm aigw.key
# Keep aigw.crt if you need to trust it on clients
```

### Windows (PowerShell)

#### Option A: Using OpenSSL (if installed)

If you have OpenSSL installed (via Git for Windows, Chocolatey, or standalone):

```powershell
# Generate private key and self-signed certificate
openssl req -x509 -nodes -days 365 -newkey rsa:2048 `
  -keyout aigw.key `
  -out aigw.crt `
  -subj "/CN=aigw.lab.internal"

# Import into ACM
$CertArn = aws acm import-certificate `
  --certificate fileb://aigw.crt `
  --private-key fileb://aigw.key `
  --region us-west-1 `
  --query "CertificateArn" `
  --output text

Write-Host "Certificate ARN: $CertArn"

# Clean up
Remove-Item aigw.key
```

#### Option B: Using PowerShell (no OpenSSL required)

```powershell
# Generate a self-signed certificate using PowerShell
$cert = New-SelfSignedCertificate `
  -DnsName "aigw.lab.internal" `
  -CertStoreLocation "Cert:\CurrentUser\My" `
  -NotAfter (Get-Date).AddDays(365) `
  -KeyAlgorithm RSA `
  -KeyLength 2048

# Export the certificate (PEM format)
$certBytes = $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert)
$certPem = "-----BEGIN CERTIFICATE-----`n"
$certPem += [Convert]::ToBase64String($certBytes, [Base64FormattingOptions]::InsertLineBreaks)
$certPem += "`n-----END CERTIFICATE-----"
$certPem | Out-File -Encoding ASCII -FilePath aigw.crt

# Export the private key (PEM format)
$keyBytes = $cert.PrivateKey.ExportRSAPrivateKey()
$keyPem = "-----BEGIN RSA PRIVATE KEY-----`n"
$keyPem += [Convert]::ToBase64String($keyBytes, [Base64FormattingOptions]::InsertLineBreaks)
$keyPem += "`n-----END RSA PRIVATE KEY-----"
$keyPem | Out-File -Encoding ASCII -FilePath aigw.key

# Import into ACM
$CertArn = aws acm import-certificate `
  --certificate fileb://aigw.crt `
  --private-key fileb://aigw.key `
  --region us-west-1 `
  --query "CertificateArn" `
  --output text

Write-Host "Certificate ARN: $CertArn"

# Clean up
Remove-Item aigw.key
Remove-Item "Cert:\CurrentUser\My\$($cert.Thumbprint)"
```

---

## Option 3: Import an Existing Certificate

If you already have a certificate and private key from a commercial CA or internal PKI:

```bash
REGION=us-west-1

# If you also have a certificate chain (intermediate CAs):
aws acm import-certificate \
  --certificate fileb://certificate.pem \
  --private-key fileb://private-key.pem \
  --certificate-chain fileb://chain.pem \
  --region "${REGION}" \
  --query "CertificateArn" \
  --output text

# Without a chain:
aws acm import-certificate \
  --certificate fileb://certificate.pem \
  --private-key fileb://private-key.pem \
  --region "${REGION}" \
  --query "CertificateArn" \
  --output text
```

---

## Using the Certificate ARN

Pass the certificate ARN as the `AcmCertificateArn` parameter when deploying the stack:

```bash
aws cloudformation create-stack \
  --stack-name my-aigw \
  --template-url "https://${BUCKET}.s3.${REGION}.amazonaws.com/templates/gateway-asg.yaml" \
  --parameters \
    ParameterKey=AcmCertificateArn,ParameterValue=${CERT_ARN} \
    ...
```

---

## Certificate Renewal

| Certificate type | Renewal |
|-----------------|---------|
| ACM-issued (public) | Automatic — ACM renews before expiry as long as DNS validation records remain in place |
| Imported (self-signed or CA-signed) | Manual — you must generate a new certificate and re-import before expiry |

### Check certificate expiry

```bash
aws acm describe-certificate \
  --certificate-arn <arn> \
  --region us-west-1 \
  --query "Certificate.{Status:Status,NotAfter:NotAfter,InUse:InUseBy}"
```

### Re-import an updated certificate

To replace an expiring imported certificate without changing the ARN (no stack update needed):

```bash
aws acm import-certificate \
  --certificate-arn <existing-arn> \
  --certificate fileb://new-cert.pem \
  --private-key fileb://new-key.pem \
  --region us-west-1
```

The ALB picks up the new certificate automatically — no listener changes or stack updates required.

---

## Listing Certificates

```bash
aws acm list-certificates \
  --region us-west-1 \
  --query "CertificateSummaryList[*].{Domain:DomainName,ARN:CertificateArn,Status:Status}" \
  --output table
```
