# LiteLLM LaunchAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bootstrap a pinned local LiteLLM `1.89.0` runtime under `~/.litellm/service`, add a stable wrapper entrypoint, and install a per-user LaunchAgent that starts the proxy on `127.0.0.1:5596` after a 10-second login delay.

**Architecture:** The implementation keeps `~/.litellm` as the canonical LiteLLM home and places all runtime machinery under `~/.litellm/service`. A LaunchAgent in `~/Library/LaunchAgents/` is the only startup and restart authority; it invokes a stable shell wrapper, which uses `exec` to run LiteLLM from a pinned `uv` project. Logging is file-based under `~/.litellm/service/logs`, and no widget or `uvx` behavior is involved.

**Tech Stack:** macOS launchd LaunchAgents, shell (`/bin/sh`), `uv`, LiteLLM `1.89.0`

## Global Constraints

- Canonical LiteLLM home: `~/.litellm`
- Active config path: `~/.litellm/config.yaml`
- Runtime machinery path: `~/.litellm/service`
- Pinned runtime project path: `~/.litellm/service`
- Wrapper entrypoint path: `~/.litellm/service/bin/start-litellm.sh`
- Logs path: `~/.litellm/service/logs`
- Use LiteLLM version `1.89.0`
- Do not use `uvx`
- Do not depend on the widget app
- Bind only to `127.0.0.1`
- Listen only on port `5596`
- LaunchAgent is the only restart authority
- Use launchd-native 10-second delayed startup
- Use launchd throttling only; no custom retry counter
- Wrapper script must stay minimal and must use `exec`
- No dedicated env file unless implementation proves inherited `AZURE_OPENAI_API_KEY` is unreliable

---

## File Structure

**Plan document**

- Create: `docs/superpowers/plans/2026-08-01-litellm-launchagent.md`

**Service-owned runtime files**

- Create: `~/.litellm/service/pyproject.toml`
- Create: `~/.litellm/service/uv.lock`
- Create: `~/.litellm/service/bin/start-litellm.sh`
- Create: `~/.litellm/service/logs/`
- Create: `~/.litellm/service/run/`
- Create: `~/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist`

**Files to inspect but not redesign**

- Read: `~/.litellm/config.yaml`
- Read: current `launchctl` user environment to confirm `AZURE_OPENAI_API_KEY` inheritance

**Verification surfaces**

- Log files: `~/.litellm/service/logs/litellm.stdout.log`, `~/.litellm/service/logs/litellm.stderr.log`
- LaunchAgent label: `com.thedawgctor.litellm-proxy`
- Endpoint: `http://127.0.0.1:5596`

**Note on commits**

- The target machine state lives in the home directory and `~/Library/LaunchAgents/`, not inside a git repository.
- For this plan, every task ends with a checkpoint instead of a git commit.

### Task 1: Bootstrap The Pinned `uv` Runtime

**Files:**
- Create: `~/.litellm/service/pyproject.toml`
- Create: `~/.litellm/service/uv.lock`
- Create: `~/.litellm/service/logs/`
- Create: `~/.litellm/service/run/`
- Read: `~/.litellm/config.yaml`

**Interfaces:**
- Consumes: existing `~/.litellm/config.yaml`
- Produces:
  - directory contract:
    - `~/.litellm/service`
    - `~/.litellm/service/logs`
    - `~/.litellm/service/run`
  - pinned runtime callable by running `uv run litellm --version` from `~/.litellm/service`

- [ ] **Step 1: Create the service directory skeleton**

```bash
mkdir -p "$HOME/.litellm/service/bin" \
         "$HOME/.litellm/service/logs" \
         "$HOME/.litellm/service/run"
```

- [ ] **Step 2: Verify the config file already exists**

Run:

```bash
test -f "$HOME/.litellm/config.yaml"
```

Expected:

```text
exit status 0
```

- [ ] **Step 3: Create a minimal pinned `uv` project file**

Write this file to `~/.litellm/service/pyproject.toml`:

```toml
[project]
name = "litellm-proxy-service"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = [
  "litellm[proxy]==1.89.0",
]
```

- [ ] **Step 4: Lock and sync the environment**

Run:

```bash
cd "$HOME/.litellm/service"
uv lock
uv sync
```

Expected:

```text
`uv.lock` created and the project environment materialized without using `uvx`
```

- [ ] **Step 5: Verify the pinned LiteLLM version**

Run:

```bash
cd "$HOME/.litellm/service"
uv run litellm --version
```

Expected:

```text
output references LiteLLM 1.89.0
```

- [ ] **Step 6: Checkpoint**

Record that the pinned runtime exists and the version check passed:

```text
Task 1 complete when `uv run litellm --version` works from `~/.litellm/service` and the service directory skeleton exists.
```

### Task 2: Add The Stable Wrapper Entrypoint

**Files:**
- Create: `~/.litellm/service/bin/start-litellm.sh`
- Uses: `~/.litellm/service/pyproject.toml`
- Uses: `~/.litellm/config.yaml`

**Interfaces:**
- Consumes:
  - pinned runtime at `~/.litellm/service`
  - config file at `~/.litellm/config.yaml`
- Produces:
  - executable wrapper at `~/.litellm/service/bin/start-litellm.sh`
  - manual operator command:
    - `~/.litellm/service/bin/start-litellm.sh`

- [ ] **Step 1: Write the wrapper script**

Write this file to `~/.litellm/service/bin/start-litellm.sh`:

```sh
#!/bin/sh
set -eu

LITELLM_HOME="$HOME/.litellm"
SERVICE_HOME="$LITELLM_HOME/service"
LOG_HOME="$SERVICE_HOME/logs"
RUN_HOME="$SERVICE_HOME/run"

mkdir -p "$LOG_HOME" "$RUN_HOME"

cd "$SERVICE_HOME"

exec uv run litellm \
  --config "$LITELLM_HOME/config.yaml" \
  --host 127.0.0.1 \
  --port 5596
```

- [ ] **Step 2: Make the wrapper executable**

Run:

```bash
chmod +x "$HOME/.litellm/service/bin/start-litellm.sh"
```

- [ ] **Step 3: Verify the wrapper syntax**

Run:

```bash
sh -n "$HOME/.litellm/service/bin/start-litellm.sh"
```

Expected:

```text
no output and exit status 0
```

- [ ] **Step 4: Verify the wrapper resolves the pinned runtime command path**

Run:

```bash
HOME="$HOME" /bin/sh -c '. "$HOME/.litellm/service/bin/start-litellm.sh"' 2>/dev/null
```

Expected:

```text
Do not leave this running. This step is only to confirm the script reaches the LiteLLM exec path without shell syntax errors. Stop immediately if it binds successfully.
```

- [ ] **Step 5: Checkpoint**

Record that the wrapper is executable, syntactically valid, and ready to be called by launchd.

### Task 3: Create And Load The LaunchAgent

**Files:**
- Create: `~/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist`
- Uses: `~/.litellm/service/bin/start-litellm.sh`
- Uses: `~/.litellm/service/logs/litellm.stdout.log`
- Uses: `~/.litellm/service/logs/litellm.stderr.log`

**Interfaces:**
- Consumes:
  - wrapper script at `~/.litellm/service/bin/start-litellm.sh`
  - service log directory
- Produces:
  - LaunchAgent label: `com.thedawgctor.litellm-proxy`
  - user login startup and restart supervision for the LiteLLM proxy

- [ ] **Step 1: Write the LaunchAgent plist**

Write this file to `~/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.thedawgctor.litellm-proxy</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>/Users/thedawgctor/.litellm/service/bin/start-litellm.sh</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>/Users/thedawgctor/.litellm/service/logs/litellm.stdout.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/thedawgctor/.litellm/service/logs/litellm.stderr.log</string>

  <key>WorkingDirectory</key>
  <string>/Users/thedawgctor/.litellm/service</string>
</dict>
</plist>
```

- [ ] **Step 2: Validate plist syntax**

Run:

```bash
plutil -lint "$HOME/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist"
```

Expected:

```text
OK
```

- [ ] **Step 3: Decide and implement the launchd-native 10-second login delay**

Run:

```bash
man launchd.plist
```

Expected:

```text
Confirm the exact launchd-native mechanism chosen for a 10-second post-login start. Update the plist accordingly before load if the initial plist needs a delay key or schedule-based adjustment.
```

Implementation note:

```text
This step is intentionally explicit because the spec fixed “launchd-native delay” but did not yet freeze the exact plist mechanism. The implementer must choose the simplest documented launchd-native expression that gives a reliable 10-second post-login delay without adding shell sleep.
```

- [ ] **Step 4: Load the LaunchAgent**

Run:

```bash
launchctl bootout "gui/$(id -u)/com.thedawgctor.litellm-proxy" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist"
```

Expected:

```text
LaunchAgent loads without plist or permission errors
```

- [ ] **Step 5: Confirm launchd sees the job**

Run:

```bash
launchctl print "gui/$(id -u)/com.thedawgctor.litellm-proxy" | sed -n '1,120p'
```

Expected:

```text
Output includes the label, plist path, wrapper script program arguments, and log paths.
```

- [ ] **Step 6: Checkpoint**

Record that the job is loaded and visible in launchd under `com.thedawgctor.litellm-proxy`.

### Task 4: Verify Runtime Behavior Before Reboot

**Files:**
- Uses: `~/.litellm/service/bin/start-litellm.sh`
- Uses: `~/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist`
- Uses: `~/.litellm/service/logs/litellm.stdout.log`
- Uses: `~/.litellm/service/logs/litellm.stderr.log`

**Interfaces:**
- Consumes:
  - loaded LaunchAgent
  - local endpoint `127.0.0.1:5596`
- Produces:
  - verified manual-start path
  - verified launchd-start path
  - operator checklist for post-reboot validation

- [ ] **Step 1: Confirm the local port becomes reachable**

Run:

```bash
lsof -nP -iTCP:5596 -sTCP:LISTEN
```

Expected:

```text
one LiteLLM listener bound to 127.0.0.1:5596 or *:5596, with the wrapper/launchd path traceable to the pinned runtime
```

- [ ] **Step 2: Confirm the LiteLLM endpoint responds**

Run:

```bash
curl -sS http://127.0.0.1:5596/models
```

Expected:

```text
JSON response containing the configured model list
```

- [ ] **Step 3: Inspect stderr log for startup issues**

Run:

```bash
tail -n 50 "$HOME/.litellm/service/logs/litellm.stderr.log"
```

Expected:

```text
No shell errors, no missing-runtime errors, and no bind failures unless another process already owns port 5596.
```

- [ ] **Step 4: Verify inherited environment behavior**

Run:

```bash
launchctl getenv AZURE_OPENAI_API_KEY
```

Expected:

```text
Non-empty value, confirming no dedicated env file is needed for this rollout.
```

- [ ] **Step 5: Exercise an intentional stop path**

Run:

```bash
launchctl bootout "gui/$(id -u)/com.thedawgctor.litellm-proxy"
```

Expected:

```text
The service stops and remains stopped until explicitly bootstrapped again.
```

- [ ] **Step 6: Reload and re-verify**

Run:

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist"
sleep 12
lsof -nP -iTCP:5596 -sTCP:LISTEN
```

Expected:

```text
Service returns after the delayed launch path and listens again on port 5596.
```

- [ ] **Step 7: Write the reboot verification checklist**

Store this checklist in the implementation notes used during rollout:

```text
1. Reboot or log out and back in.
2. Wait at least 12 seconds after login.
3. Run `lsof -nP -iTCP:5596 -sTCP:LISTEN`.
4. Run `curl -sS http://127.0.0.1:5596/models`.
5. If either check fails, inspect:
   - ~/.litellm/service/logs/litellm.stdout.log
   - ~/.litellm/service/logs/litellm.stderr.log
   - launchctl print gui/$(id -u)/com.thedawgctor.litellm-proxy
```

- [ ] **Step 8: Checkpoint**

Task complete when manual verification confirms:

```text
- launchd can load the job
- the job starts the pinned runtime
- LiteLLM responds on 127.0.0.1:5596
- the operator can stop and restart it intentionally
- post-reboot verification steps are written down
```

## Self-Review

**Spec coverage**

- Runtime under `~/.litellm/service`: covered by Tasks 1 and 2
- Pinned `uv` project with LiteLLM `1.89.0`: covered by Task 1
- Stable shell wrapper with `exec`: covered by Task 2
- LaunchAgent-only restart authority: covered by Task 3
- Launchd-native delayed login startup: covered by Task 3
- File logs under `~/.litellm/service/logs`: covered by Tasks 2, 3, and 4
- Hard bind `127.0.0.1:5596`: covered by Tasks 2 and 4
- No widget dependency and no `uvx`: enforced in Global Constraints and Tasks 1-3

**Placeholder scan**

- No `TODO`, `TBD`, or “implement later” placeholders remain.
- The only intentionally open implementation choice is the exact documented launchd-native delay expression; it is explicitly isolated in Task 3 Step 3 because the spec approved launchd-native delay but did not freeze the specific plist key strategy.

**Type and interface consistency**

- Wrapper path is consistently `~/.litellm/service/bin/start-litellm.sh`
- Runtime path is consistently `~/.litellm/service`
- Config path is consistently `~/.litellm/config.yaml`
- LaunchAgent label is consistently `com.thedawgctor.litellm-proxy`
- Endpoint is consistently `127.0.0.1:5596`
