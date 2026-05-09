# Plan: Replace SSM with SSH + TUI Enrollment via Step Functions

## Context

The current architecture uses SSM to run enrollment commands on gateway instances. The goal is to eliminate SSM entirely and have Lambda connect directly to the gateway. The TUI enrollment code from `aig-integration-test` drives the `aig-cli` menu via SSH, which works with the existing `nsadmin` user — **no AMI changes required**. The SSH tunnel approach (in `docs/SSH_TUNNEL_PROPOSAL.md`) requires a new `lambda-automation` user on the AMI because `nsadmin` drops into a restricted TUI menu that blocks port forwarding. The TUI approach sidesteps this by driving that menu directly.

---

## Why Step Functions Is Required

| Constraint | Value |
|---|---|
| Lambda max timeout | 900s (15 min) |
| Pre-enrollment install | up to 900s (15 min) |
| Enrollment completion | up to 600s (10 min) |
| **Total worst case** | **25+ min** |

A single Lambda cannot span the full enrollment. Step Functions breaks the work into short Lambda invocations (seconds each) with `Wait` states for polling intervals — no Lambda running during waits.

The SSH/TUI session cannot persist across Lambda invocations, but it doesn't need to: the aig-cli TUI shows **current state** when you navigate to the enrollment screen. Pre-enrollment continues on-instance regardless of the TUI session. Each Lambda opens a fresh SSH connection, navigates to the enrollment menu, observes the state, acts, and disconnects.

---

## Feasibility Testing: pexpect vs paramiko

### pexpect — RULED OUT

The initial approach was to use pexpect (the same library used by the `aig-integration-test` TUI code) via a Lambda Layer bundling `pexpect`, `pyte`, and the `ssh` binary.

**Findings from the `tui-ssh-test` stack deployment:**

1. **`/dev/ptmx` does not exist in the Lambda execution environment.** pexpect requires a pseudo-terminal (PTY) via `pty.fork()` → `pty.openpty()` → `/dev/ptmx`. Lambda's container does not expose PTY devices. This causes `OSError: out of pty devices` at `pexpect.spawn()`.

2. **Architecture mismatch on Apple Silicon.** Building the layer on an M-series Mac (even via Docker/Podman) produces an aarch64 `ssh` binary. Lambda runs on x86_64, resulting in `Exec format error`. Requires `--platform linux/amd64` in the container build. This is a build-time issue, not a blocker, but added friction.

3. **No workaround.** pexpect fundamentally requires local PTY devices. There is no configuration or fallback that avoids this requirement. pexpect cannot work in Lambda.

### paramiko + pyte — CONFIRMED WORKING

Pivoted to paramiko `invoke_shell()` with pyte for terminal emulation. This approach allocates the PTY on the **remote** side (the gateway instance), not locally in Lambda.

**Test results (2026-05-08, `tui-ssh-test` stack in us-west-1):**

- **paramiko 4.0.0** + **pyte** in a Lambda Layer (9.8MB)
- SSH connected to Ubuntu test instance via key-based auth
- `invoke_shell(term='xterm', width=120, height=40)` opened an interactive terminal
- pyte captured full screen state including cursor position tracking
- Commands executed and output captured correctly: `whoami` → `ubuntu`, `uname -a` → kernel info
- Total Lambda execution: ~19 seconds
- **Status: PASSED** — no errors

### What this means for the TUI code

The existing `libs/tui/` code from `aig-integration-test` uses pexpect throughout (`tui_session.py`). Since pexpect doesn't work in Lambda, the TUI session layer needs to be reimplemented using paramiko. The higher-level code (`tui_actions.py`, `tui_screen.py`, `enrollment.py`) can be reused with a new session class that presents the same interface.

---

## Lambda Layer: paramiko + pyte

### Layer contents

```
layer/
└── python/                    # Python packages (added to PYTHONPATH)
    ├── paramiko/              # SSH client library
    ├── pyte/                  # Terminal emulation
    ├── cryptography/          # paramiko dependency
    ├── bcrypt/                # paramiko dependency
    ├── pynacl/                # paramiko dependency
    └── cffi/                  # cryptography dependency
```

### Build script (`scripts/build-tui-layer.sh`)

```bash
podman run --rm --platform linux/amd64 --entrypoint bash \
  -v "$PWD/scripts:/build" -w /build \
  public.ecr.aws/lambda/python:3.12 \
  ./build-tui-layer.sh
```

No `ssh` binary needed — paramiko handles SSH natively in Python.

---

## Step Functions State Machine

```
ASG Lifecycle Hook → SNS → DispatcherLambda → Step Functions

States:
  1. RegisterAppliance          [Lambda, ~5s]
     Netskope API: POST /api/v2/aig/appliances → get appliance_id + enrollment_token
     Store appliance_id in SSM param (for termination cleanup only)

  2. WaitForSSH                 [Lambda + Wait/Retry loop, up to 5 min]
     paramiko.SSHClient().connect() to instance private IP
     Retry every 15s until SSH accepts connections

  3. StartEnrollment            [Lambda, ~30s]
     paramiko invoke_shell() → pyte screen → navigate to "Enroll this AI Gateway" → Enter
     Read initial screen state:
       - "Enrollment completed" → jump to CompleteLifecycle
       - "Enter enrollment token:" → jump to SubmitToken
       - Otherwise → pre-enrollment in progress → continue to PollPreEnrollment

  4. PollPreEnrollment          [Wait 30s + Lambda, loop up to ~18 min]
     paramiko invoke_shell() → navigate to enrollment screen
     Check if "Enter enrollment token:" appears within ~10s
       - Yes → go to SubmitToken
       - No → Wait 30s and retry
       - "Enrollment completed" → jump to CompleteLifecycle

  5. SubmitToken                [Lambda, ~30s]
     paramiko invoke_shell() → navigate to enrollment
     Wait for "Enter enrollment token:" → send token → Enter
     Wait for "Enrollment completed" (up to 60s)
     If timeout → go to PollEnrollmentResult

  6. PollEnrollmentResult       [Wait 10s + Lambda, loop up to ~10 min]
     paramiko invoke_shell() → navigate to enrollment → check for "Enrollment completed"

  7. ConfigureDLP               [Choice: skip if no DLP_HOST_URL]
     See DLP section below

  8. RestartServices            [Lambda, ~5s]
     paramiko invoke_shell() → TUI restart via menu

  9. CompleteLifecycle           [Lambda, ~2s]
     Call autoscaling:CompleteLifecycleAction(CONTINUE)

  Error Handler:
     CompleteLifecycleAction(ABANDON) + cleanup appliance registration
```

---

## DLP Configuration — Open Question

The current SSM bash script configures DLP via direct HTTP API calls (`PUT /aiapi/dlp/cert`, `PUT /aiapi/dlp/hostconfig`). The TUI has a "Configure Content Inspection Services" menu item, but the copied TUI code has no automation for it — only `enrollment.py` and `csr.py`.

**Options:**
- **A)** Write new TUI automation for the DLP menu (mirrors how enrollment.py drives the enrollment menu)
- **B)** Use paramiko `exec_command()` to run curl commands on the instance if the nsadmin user has any shell escape or if there's a way to invoke commands from the TUI
- **C)** Configure DLP separately (manual or different mechanism)

This needs investigation into what the "Configure Content Inspection Services" TUI menu exposes.

---

## Files to Create/Modify

### New files

| File | Purpose |
|---|---|
| `libs/tui/paramiko_session.py` | New TUI session class using paramiko `invoke_shell()` + pyte. Presents the same interface as `TUISession` (connect, send_key, get_screen_text, etc.) so `TUIActions` and `enrollment.py` work unchanged. |
| `scripts/step_function_handlers.py` | Lambda handlers for each Step Function state (register, poll, submit, verify, complete) |

### Modified files

| File | Changes |
|---|---|
| `templates/gateway-asg.yaml` | Add `AWS::StepFunctions::StateMachine` with ASL definition; add IAM role for Step Functions; replace inline Lambda (ZipFile) with S3-packaged Lambda (Code: S3Bucket/S3Key); add `AWS::Lambda::LayerVersion` for paramiko/pyte; add SSH key generation Custom Resource (from `templates/test-ssh-tunnel.yaml`); add Lambda VPC configuration (private subnets + security group); add VPC endpoint for Secrets Manager; add security group rule: Lambda SG → Gateway SG on port 22; update lifecycle hook HeartbeatTimeout from 900 to 3600; **remove**: `GatewaySetupDocument`, SSM VPC endpoints, SSM IAM permissions on instance role; **keep**: `handle_cfn_event` handler (adapted for Step Functions trigger) |
| `libs/tui/utils/enrollment.py` | Refactor into step-wise functions that each Step Function state can call independently |
| `scripts/build-tui-layer.sh` | Updated to install `paramiko pyte` (no longer pexpect or ssh binary) |

### Reused as-is (from copied TUI libs)

| File | Status |
|---|---|
| `libs/tui/tui_actions.py` | Menu navigation logic — works with any session implementing the interface |
| `libs/tui/tui_screen.py` | Screen parsing and pyte integration — works unchanged |
| `libs/tui/menu_config.py` | Menu patterns — works unchanged |

### Not needed for Lambda (can remove or keep for testing)

| File | Reason |
|---|---|
| `libs/tui/tui_session.py` | Replaced by `paramiko_session.py` for Lambda use; keep for local testing with pexpect |
| `libs/tui/tui_session_publickey.py` | Not applicable |
| `libs/tui/tui_helpers.py` | Test utility, may be partially reused |
| `libs/tui/base_test.py` | Test framework, not needed for Lambda |
| `libs/tui/config.py` | pexpect-specific config, not needed for paramiko session |
| `libs/tui/config_publickey.py` | Not applicable |

---

## Verification

1. **~~pexpect feasibility~~** — ~~Build layer, test in Lambda~~ **DONE — pexpect does not work in Lambda (no PTY devices)**
2. **~~paramiko + pyte feasibility~~** — **DONE — PASSED** (test stack `tui-ssh-test`, us-west-1, 2026-05-08)
3. **~~TUI connectivity test~~** — **DONE — PASSED** against real AI Gateway (AMI `ami-077c245171c8b4942`, AIG Gateway v1.3.340). TUI detected, menu navigation working, `*` selection indicator tracking correct, `TUIActions.navigate_to_menu_item()` working.
4. **~~Full TUI enrollment test~~** — **DONE — PASSED** (2026-05-08). See details below.
5. **~~Step Functions handler test~~** — **DONE — PASSED** (2026-05-08). See Step Function test results below.
6. **~~Deregistration test~~** — **DONE — PASSED** (API call works, but requires non-VPC Lambda). See architecture finding below.
7. **End-to-end ASG test** — Scale the ASG up, verify the full lifecycle hook → Step Functions → TUI enrollment → InService flow
8. **Termination test** — Scale down, verify appliance deregistration and cleanup

### Single-Lambda Enrollment Test Results (2026-05-08)

**Test environment:**
- Gateway: `i-0767122e50afc9f0a` at `172.31.7.56` (AIG Gateway v1.3.340, `ami-077c245171c8b4942`)
- Tenant: `bespin.goskope.com`
- Lambda: `tui-ssh-test-tui-test` with paramiko/pyte layer, 900s timeout, VPC-attached
- SSH auth: key-based (EC2 key pair via Secrets Manager), nsadmin user

**Enrollment flow observed:**

| Step | Time | Detail |
|---|---|---|
| SSH connect | +0s | paramiko key auth to nsadmin@172.31.7.56 |
| TUI detected | +20s | "Netskope AI Gateway Configuration Wizard" rendered |
| Navigate to Enroll | +20s | `TUIActions.navigate_to_menu_item("Enroll this AI Gateway")` |
| Press Enter | +21s | Entered enrollment screen |
| Pre-enrollment check | +21s | CPU ✓ PASS, Memory ✓ PASS, Disk ✓ PASS |
| Pre-enrollment init | +21s→ | Progress bar: 70% after ~2 min (first attempt timed out at 120s) |
| Token prompt | ~+10min | "Enter enrollment token:" appeared after pre-enrollment completed |
| Token submitted | +0s | JWT token sent via `session.child.send()` + Enter |
| Enrollment completed | ~+30s | "Enrollment completed successfully: Gateway ID: 019e07cc-c39b-78dc-8c1b-e010ff836410" |

### Step Function Handler Test Results (2026-05-08)

**Test environment:**
- Stack: `sfn-enroll-test` in us-west-1
- Gateway: `i-016d77958f8edae38` at `172.31.7.133` (fresh AIG Gateway v1.3.340)
- Lambda: `sfn-enroll-test-enrollment` with action-routed handler
- Appliance registered manually (Lambda can't reach Netskope API from VPC — see architecture finding)

**Step-wise enrollment — simulating Step Function flow:**

| Action | Result | Detail |
|---|---|---|
| `register` (manual) | Appliance `019e0805-36da-7ad2-9375-163846647f92` created | Enrollment token generated |
| `check_ssh` | `ssh_ready: true` | Key auth to nsadmin |
| `start_enrollment` | `pre_enrollment_running` | TUI detected, navigated to enrollment, pre-enrollment started |
| `check_enrollment` (poll 1, +60s) | `pre_enrollment_running` | System check screen visible |
| `check_enrollment` (poll 2, +90s) | `pre_enrollment_running` | Still installing |
| `check_enrollment` (poll 3, +120s) | `pre_enrollment_running` | Still installing |
| `check_enrollment` (poll 4, +150s) | `token_prompt_ready` | Pre-enrollment complete, prompt detected |
| `submit_token` | `completed` | "Enrollment completed successfully" |
| `deregister` (manual) | HTTP 200 | Appliance deleted from tenant |

**Each action is a separate Lambda invocation with a separate SSH session** — confirms that reconnecting between invocations works correctly for the Step Function polling pattern.

### Key findings (both tests combined)

- Pre-enrollment takes **~10-15 minutes** on a fresh gateway — confirms Step Functions is necessary for production (Lambda 15-min limit leaves no margin)
- The TUI session can be **reconnected** between Lambda invocations — pre-enrollment continues on-instance regardless of SSH session
- The `ChannelWrapper.expect()` pattern matching works correctly against real TUI output including box-drawing characters and progress bars
- nsadmin user accepts **EC2 key pair auth (publickey only, no password)** on AIG Gateway v1.3.340
- `ParamikoTUISession` auto-detects TUI on connect (no need to run `aig-cli` — nsadmin drops directly into it)

### Architecture Finding: Two Lambdas Required

The `register` and `deregister` actions call the Netskope tenant API over HTTPS (internet). The Lambda must be in a VPC for SSH access to the gateway. **A VPC Lambda cannot reach the internet without a NAT gateway.**

**Options:**
- **A) Two Lambdas** — a non-VPC Lambda for Netskope API calls (register/deregister/lifecycle completion) and a VPC Lambda for SSH/TUI actions. The Step Function calls each as appropriate.
- **B) NAT gateway** — the production `gateway-asg.yaml` already creates a NAT gateway when deploying a new VPC. A single Lambda in a private subnet behind the NAT can reach both the gateway (SSH) and the internet (Netskope API).

**Recommendation:** Option B for production (NAT gateway already exists). Option A for the test stack to avoid NAT gateway cost.

### HA: Multi-AZ Lambda

The Lambda must be configured with **both private subnets** (across AZs) in `VpcConfig.SubnetIds`. Lambda creates ENIs in each subnet and automatically runs in a healthy AZ if one goes down. The Secrets Manager VPC endpoint must also span both private subnets. Step Functions is a regional service and is inherently HA.

---

## Implementation Order

1. ~~Build pexpect Lambda Layer~~ — **Done, ruled out (no PTY in Lambda)**
2. ~~Build paramiko/pyte Lambda Layer~~ — **Done** (9.8MB, `scripts/build-tui-layer.sh`)
3. ~~Test paramiko + pyte from Lambda~~ — **Done, PASSED**
4. ~~Create `libs/tui/paramiko_session.py`~~ — **Done** (paramiko session compatible with TUIActions/TUIScreen)
5. ~~Test against real gateway instance~~ — **Done, PASSED** (TUI navigation + full enrollment)
6. ~~Build Step Function Lambda handlers~~ — **Done** (`scripts/step_function_handlers.py`, action-routed)
7. ~~Define Step Functions ASL state machine~~ — **Done** (`templates/test-step-function.yaml`)
8. ~~Test step-wise enrollment flow~~ — **Done, PASSED** (all actions validated individually)
9. Integrate into `gateway-asg.yaml` (SSH key gen, Layer, Step Functions, Lambda packaging, SSM removal)
10. End-to-end ASG lifecycle test
