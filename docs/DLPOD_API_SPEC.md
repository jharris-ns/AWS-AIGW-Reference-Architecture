# DLPoD Appliance API Specification

Proposed REST API endpoints for the DLPoD (DLP on Demand) appliance to replace SSH/CLI-based automation with a programmatic interface.

## Background

The current deployment automation (`dlpod_handlers.py`) configures DLPoD appliances by establishing SSH sessions and sending CLI commands to the `nsappliance` shell. This approach has several limitations:

- **Fragile**: Depends on screen scraping, prompt regex matching, and timing delays
- **Slow**: Each SSH session requires connection setup, authentication, and serial command execution
- **Error-prone**: Password prompts, interactive confirmation dialogs, and JSON parsing from CLI output are brittle
- **Non-idempotent**: No built-in way to check current state before applying configuration
- **Security surface**: Requires SSH access from Lambda (VPC-attached), long-lived password credentials passed through Step Functions state

An HTTP REST API on the appliance would allow the Lambda automation to make direct, stateless, idempotent calls without SSH tunneling or CLI parsing.

## Authentication

All endpoints require authentication. Recommended approach:

| Header | Value | Description |
|--------|-------|-------------|
| `Authorization` | `Bearer <token>` | API token generated during initial setup or derived from the license key |
| `X-API-Key` | `<key>` | Alternative: static API key set during password change phase |

The initial bootstrap endpoint (`POST /api/v1/auth/setup`) should accept the default credentials to establish the API token, replacing the current SSH password-change flow.

---

## Endpoints

### 1. Bootstrap / Initial Authentication

#### `POST /api/v1/auth/setup`

Replaces: `handle_dlpod_change_password` — SSH password change via `auth change-password nsadmin`

**Purpose**: Initialize the appliance API credentials on first boot. Changes the default password and returns an API token for all subsequent calls. This is the only endpoint that accepts default credentials.

**Request**:
```json
{
  "current_password": "nsappliance",
  "new_password": "xK9#mP2$vL7nQ4wR8jT1bF6y"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `current_password` | string | Yes | Current appliance password (default: `nsappliance`) |
| `new_password` | string | Yes | New password. Must meet complexity requirements: min 12 chars, mixed case, digit, special char |

**Response** (`201 Created`):
```json
{
  "api_token": "dlpod_tk_a1b2c3d4e5f6...",
  "token_type": "bearer",
  "expires_in": null,
  "password_changed": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `api_token` | string | Bearer token for all subsequent API calls |
| `token_type` | string | Always `bearer` |
| `expires_in` | int\|null | Token TTL in seconds, or `null` if non-expiring |
| `password_changed` | bool | Confirms password was updated |

**Errors**:
| Code | Condition |
|------|-----------|
| `400` | Password does not meet complexity requirements |
| `401` | `current_password` is incorrect |
| `409` | Setup already completed (default password already changed) |

---

### 2. DNS Configuration

#### `PUT /api/v1/network/dns`

Replaces: `handle_dlpod_set_dns` — SSH commands `configure` → `set dns primary <ip>` → `save` → `exit`

**Purpose**: Configure primary and optional secondary DNS servers. Must be set before the license key so the appliance can resolve the Netskope management plane hostname.

**Request**:
```json
{
  "primary": "172.31.0.2",
  "secondary": "8.8.8.8"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `primary` | string (IPv4) | Yes | Primary DNS server address |
| `secondary` | string (IPv4) | No | Secondary DNS server address |

**Response** (`200 OK`):
```json
{
  "primary": "172.31.0.2",
  "secondary": "8.8.8.8",
  "applied": true,
  "restart_required": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `primary` | string | Configured primary DNS |
| `secondary` | string\|null | Configured secondary DNS, or `null` |
| `applied` | bool | Whether the configuration was applied |
| `restart_required` | bool | Whether a service restart is needed to take effect |

**Errors**:
| Code | Condition |
|------|-----------|
| `400` | Invalid IP address format |
| `401` | Missing or invalid API token |

#### `GET /api/v1/network/dns`

**Purpose**: Retrieve current DNS configuration. Enables idempotent automation — check before setting.

**Response** (`200 OK`):
```json
{
  "primary": "172.31.0.2",
  "secondary": null,
  "configured": true
}
```

---

### 3. License Key

#### `PUT /api/v1/system/license`

Replaces: `handle_dlpod_set_license` — SSH commands `configure` → `set system licensekey <key>` → `save` → `exit`

**Purpose**: Set the tenant license key to initiate tethering with the Netskope management plane. The appliance begins outbound HTTPS connections to the tenant after this is applied.

**Request**:
```json
{
  "license_key": "ABCD-1234-EFGH-5678-..."
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `license_key` | string | Yes | Netskope tenant license key from Secrets Manager |

**Response** (`200 OK`):
```json
{
  "license_set": true,
  "tethering_initiated": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `license_set` | bool | Confirms key was accepted |
| `tethering_initiated` | bool | Appliance has started the tethering process |

**Errors**:
| Code | Condition |
|------|-----------|
| `400` | Invalid or malformed license key |
| `401` | Missing or invalid API token |
| `409` | License key already set (include current status in response body) |
| `424` | DNS not configured — prerequisite not met |

---

### 4. Tethering Status

#### `GET /api/v1/tethering/status`

Replaces: `handle_dlpod_check_tethering` — SSH command `status tethering` with JSON output parsing

**Purpose**: Poll tethering progress. The automation calls this repeatedly until `tethered` is `true`. Replaces the fragile JSON-block extraction from CLI output.

**Response** (`200 OK`):
```json
{
  "tethered": true,
  "tethering_status": {
    "cfg_serial_file_synced": true,
    "cloud_serial_file_synced": true,
    "serial_files_match": true,
    "cfgagent_connected": true,
    "callhome_reachable": true,
    "downloader_reachable": true
  },
  "precheck_status": {
    "required_images_present": true,
    "required_containers_running": true
  },
  "tenant_info": {
    "tenant_url": "bespin.goskope.com",
    "serial": "FF068A0FFE4D25E9C",
    "identifier": "08148a84-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "rest_token": "5be521d4..."
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `tethered` | bool | Composite: `callhome_reachable AND tenant_url AND serial` all truthy |
| `tethering_status` | object | Individual connectivity and sync checks |
| `tethering_status.callhome_reachable` | bool | Can reach Netskope management plane |
| `tethering_status.cfgagent_connected` | bool | Config agent connected to management plane |
| `tethering_status.cfg_serial_file_synced` | bool | Local serial file synchronized |
| `tethering_status.cloud_serial_file_synced` | bool | Cloud serial file synchronized |
| `tethering_status.serial_files_match` | bool | Local and cloud serial files match |
| `tethering_status.downloader_reachable` | bool | Downloader service reachable |
| `precheck_status` | object | Container and image readiness |
| `precheck_status.required_images_present` | bool | All required container images available |
| `precheck_status.required_containers_running` | bool | All required containers running |
| `tenant_info` | object\|null | Tenant details, populated after successful tethering |
| `tenant_info.tenant_url` | string | Netskope tenant hostname |
| `tenant_info.serial` | string | Appliance serial number |
| `tenant_info.identifier` | string | Appliance UUID |
| `tenant_info.rest_token` | string | Management plane auth token |

**Errors**:
| Code | Condition |
|------|-----------|
| `401` | Missing or invalid API token |
| `424` | License key not yet set |

---

### 5. TLS Certificate Management

#### `POST /api/v1/certificate/generate`

Replaces: `handle_dlpod_generate_cert` — SSH command `run request certificate generate self-signed ...` followed by interactive reboot confirmation

**Purpose**: Generate a self-signed TLS certificate for the appliance HTTPS service. Optionally triggers a service reload or reboot so the new certificate is served on port 443.

**Request**:
```json
{
  "common_name": "dlp.aigw.internal",
  "organization": "Netskope",
  "organization_unit": "TechAlliances",
  "city": "Santa Clara",
  "state": "CA",
  "country": "US",
  "email": "admin@example.com",
  "validity_days": 365,
  "auto_activate": true
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `common_name` | string | Yes | — | Certificate CN and SAN (should match DNS name) |
| `organization` | string | No | `Netskope` | Certificate organization field |
| `organization_unit` | string | No | `TechAlliances` | Certificate OU field |
| `city` | string | No | `Santa Clara` | Certificate locality |
| `state` | string | No | `CA` | Certificate state/province |
| `country` | string | No | `US` | Certificate country (2-letter code) |
| `email` | string | No | `admin@example.com` | Certificate email |
| `validity_days` | int | No | `365` | Certificate validity period |
| `auto_activate` | bool | No | `true` | Restart HTTPS service or reboot to activate the cert immediately |

**Response** (`201 Created`):
```json
{
  "cert_generated": true,
  "common_name": "dlp.aigw.internal",
  "validity_days": 365,
  "not_after": "2027-05-15T00:00:00Z",
  "fingerprint_sha256": "AB:CD:12:34:...",
  "activation": {
    "auto_activate": true,
    "method": "service_reload",
    "activated": true
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `cert_generated` | bool | Certificate was created |
| `common_name` | string | CN of the generated certificate |
| `validity_days` | int | Validity period |
| `not_after` | string (ISO 8601) | Certificate expiration |
| `fingerprint_sha256` | string | SHA-256 fingerprint for verification |
| `activation.auto_activate` | bool | Whether automatic activation was requested |
| `activation.method` | string | `service_reload` or `reboot` |
| `activation.activated` | bool | Whether the new cert is now being served |

**Errors**:
| Code | Condition |
|------|-----------|
| `400` | Invalid certificate parameters (e.g., country code not 2 chars) |
| `401` | Missing or invalid API token |

#### `GET /api/v1/certificate`

Replaces: `handle_dlpod_extract_cert` — TLS handshake to port 443 to scrape the DER certificate

**Purpose**: Retrieve the current appliance TLS certificate in PEM format. Eliminates the need for the Lambda to perform a raw TLS handshake and DER-to-PEM conversion.

**Response** (`200 OK`):
```json
{
  "certificate_pem": "-----BEGIN CERTIFICATE-----\nMIID...base64...\n-----END CERTIFICATE-----",
  "common_name": "dlp.aigw.internal",
  "issuer": "CN=dlp.aigw.internal,O=Netskope,OU=TechAlliances",
  "not_before": "2026-05-15T00:00:00Z",
  "not_after": "2027-05-15T00:00:00Z",
  "fingerprint_sha256": "AB:CD:12:34:...",
  "is_self_signed": true,
  "is_ca": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `certificate_pem` | string | Full PEM-encoded certificate (standard multi-line format) |
| `common_name` | string | Certificate CN |
| `issuer` | string | Certificate issuer DN |
| `not_before` | string (ISO 8601) | Validity start |
| `not_after` | string (ISO 8601) | Validity end |
| `fingerprint_sha256` | string | SHA-256 fingerprint |
| `is_self_signed` | bool | Whether the cert is self-signed |
| `is_ca` | bool | Whether `basicConstraints` includes `CA:TRUE` |

**Errors**:
| Code | Condition |
|------|-----------|
| `401` | Missing or invalid API token |
| `404` | No certificate has been generated yet |

---

### 6. Health and Readiness

#### `GET /api/v1/health`

Replaces: `handle_dlpod_check_ssh` — SSH connection test to verify the appliance is reachable

**Purpose**: Lightweight health check for ALB target group health checks and initial readiness polling. No authentication required (used by ALB before the appliance is configured).

**Response** (`200 OK`):
```json
{
  "status": "healthy",
  "uptime_seconds": 342,
  "version": "4.2.1"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `healthy`, `initializing`, or `degraded` |
| `uptime_seconds` | int | Seconds since appliance boot |
| `version` | string | DLPoD software version |

This endpoint serves double duty:
1. **Replaces SSH readiness polling**: Instead of retrying SSH connections every 15 seconds, the Lambda polls `GET /api/v1/health` until it returns `200`.
2. **ALB health check target**: Replace the current permissive HTTPS/443 root path check (`200-499` matcher) with a proper health endpoint.

#### `GET /api/v1/readiness`

**Purpose**: Comprehensive readiness check that reports the state of each configuration step. Enables the automation to determine exactly where in the setup flow the appliance is.

**Response** (`200 OK`):
```json
{
  "ready": true,
  "checks": {
    "password_changed": true,
    "dns_configured": true,
    "license_set": true,
    "tethered": true,
    "certificate_active": true,
    "services_running": true
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `ready` | bool | All checks passed |
| `checks.password_changed` | bool | Default password has been changed |
| `checks.dns_configured` | bool | DNS servers are set |
| `checks.license_set` | bool | License key has been applied |
| `checks.tethered` | bool | Tethered to Netskope management plane |
| `checks.certificate_active` | bool | TLS certificate is loaded and being served |
| `checks.services_running` | bool | All DLP services are running |

**Note**: This endpoint requires authentication (unlike `/health`), since it exposes configuration state.

---

### 7. Service Management

#### `POST /api/v1/services/restart`

Replaces: SSH command `restart dlpaas all` (used after DNS changes) and `request system reboot` with interactive `yes` confirmation

**Purpose**: Restart DLP services or reboot the appliance. The current automation requires a full reboot to activate new certificates and an interactive confirmation prompt — an API call eliminates both issues.

**Request**:
```json
{
  "scope": "dlp_services",
  "reason": "dns_change"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `scope` | string | Yes | `dlp_services` (restart DLP containers), `https` (restart HTTPS daemon), or `system` (full reboot) |
| `reason` | string | No | Audit log annotation |

**Response** (`202 Accepted`):
```json
{
  "action": "restart",
  "scope": "dlp_services",
  "initiated": true,
  "estimated_seconds": 30
}
```

| Field | Type | Description |
|-------|------|-------------|
| `action` | string | The action taken |
| `scope` | string | What was restarted |
| `initiated` | bool | Restart has been initiated |
| `estimated_seconds` | int | Estimated time until services are back |

**Errors**:
| Code | Condition |
|------|-----------|
| `400` | Invalid `scope` value |
| `401` | Missing or invalid API token |

---

## Endpoint Summary

| Method | Path | Auth | Replaces (Current SSH/CLI) | Purpose |
|--------|------|------|---------------------------|---------|
| `POST` | `/api/v1/auth/setup` | Default creds | `auth change-password nsadmin` | Bootstrap credentials, get API token |
| `PUT` | `/api/v1/network/dns` | Bearer | `set dns primary/secondary` | Configure DNS |
| `GET` | `/api/v1/network/dns` | Bearer | — (new) | Read DNS config |
| `PUT` | `/api/v1/system/license` | Bearer | `set system licensekey` | Set license key |
| `GET` | `/api/v1/tethering/status` | Bearer | `status tethering` + JSON parsing | Poll tethering state |
| `POST` | `/api/v1/certificate/generate` | Bearer | `run request certificate generate self-signed` + reboot | Generate TLS cert |
| `GET` | `/api/v1/certificate` | Bearer | TLS handshake + DER→PEM conversion | Retrieve cert in PEM |
| `GET` | `/api/v1/health` | None | SSH connection test | Health/readiness for ALB |
| `GET` | `/api/v1/readiness` | Bearer | — (new) | Full configuration state |
| `POST` | `/api/v1/services/restart` | Bearer | `restart dlpaas all` / `request system reboot` | Restart services or reboot |

## Automation Flow Comparison

### Current Flow (SSH/CLI)

```
Lambda → SSH connect (retry 15s) → change password (interactive prompts)
  → SSH connect → configure → set dns → save → exit
  → SSH connect → configure → set licensekey → save → exit
  → wait 120s
  → SSH connect → status tethering → parse JSON from CLI output (retry 60s)
  → SSH connect → configure → generate cert → save → exit → reboot (yes confirm)
  → TLS handshake → DER to PEM → store in SSM
  → complete lifecycle
```

Each step opens a new SSH session, navigates the CLI, and parses text output.

### Proposed Flow (REST API)

```
Lambda → GET /health (retry 15s)
  → POST /auth/setup (get token)
  → PUT /network/dns
  → PUT /system/license
  → wait 120s
  → GET /tethering/status (retry 60s)
  → POST /certificate/generate (auto_activate=true)
  → GET /certificate (PEM) → store in SSM
  → complete lifecycle
```

Each step is a single HTTP request with structured JSON input/output. No SSH, no screen scraping, no interactive prompts.

### Benefits

| Aspect | SSH/CLI (Current) | REST API (Proposed) |
|--------|-------------------|---------------------|
| Connection overhead | SSH handshake per step | HTTP keep-alive, single TCP |
| Output parsing | Regex on terminal output, JSON block extraction | Structured JSON responses |
| Error handling | Screen text matching ("BAD PASSWORD") | HTTP status codes + error bodies |
| Idempotency | No — re-running may cause unexpected state | Yes — GET before PUT, 409 on conflict |
| Interactive prompts | `yes/no` confirmation, password prompts | None — all parameters in request body |
| Dependencies | paramiko, pyte, VPC-attached Lambda | Standard HTTP client (urllib3/requests) |
| Lambda VPC | Required (SSH to private IP) | Required (HTTPS to private IP) |
| Security surface | SSH port 22 open, password in state machine | HTTPS port 443 only, bearer token |
| Observability | CloudWatch log scraping | HTTP status codes, structured responses |
