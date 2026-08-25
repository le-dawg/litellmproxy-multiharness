# LiteLLM LaunchAgent Design

## Goal

Provide a lean, reliable macOS per-user LaunchAgent that starts the LiteLLM proxy automatically at login without depending on the in-development widget app.

The design must preserve the current basic proxy behavior while replacing the current ad hoc `uvx`-based launch path with a pinned `uv`-managed runtime.

## Scope

This design covers:

- one-time bootstrap of a pinned local LiteLLM runtime before reboot
- steady-state login startup via LaunchAgent
- stable wrapper-script entrypoint
- runtime filesystem layout
- restart and logging behavior

This design does not cover:

- widget integration or widget-driven service management
- LiteLLM Admin UI, database, budgets, virtual keys, or spend tracking setup
- migration to a full LiteLLM suite deployment
- remote exposure beyond localhost

## Requirements

### Functional Requirements

1. The LiteLLM proxy must start automatically after user login.
2. Startup must be delayed by 10 seconds.
3. The service must bind only to `127.0.0.1`.
4. The service must listen on port `5596`.
5. The service must use LiteLLM `1.89.0`.
6. The service must read its active config from `~/.litellm/config.yaml`.
7. The service must not depend on `uvx`.
8. The service must not depend on the widget app.
9. The service entrypoint must be a stable shell script.
10. The wrapper script must use `exec` so launchd supervises the LiteLLM process directly.

### Operational Requirements

1. LiteLLM runtime machinery must live under `~/.litellm/service`.
2. The pinned `uv` project must live under `~/.litellm/service`.
3. Service logs must be written to files under `~/.litellm/service/logs`.
4. LaunchAgent must be the only restart authority.
5. Restart behavior must use launchd throttling rather than a custom retry counter.
6. The wrapper script must remain minimal and must not implement an extra validation layer.

## Filesystem Layout

The canonical LiteLLM home on this machine is:

```text
~/.litellm/
```

The runtime layout is:

```text
~/.litellm/
  config.yaml
  service/
    pyproject.toml
    uv.lock
    bin/
      start-litellm.sh
    logs/
      litellm.stdout.log
      litellm.stderr.log
    run/
```

Notes:

- `~/.litellm/config.yaml` remains the active LiteLLM config file.
- `~/.litellm/service/` is the dedicated pinned `uv` project home.
- `~/.litellm/service/bin/start-litellm.sh` is the only stable operator-facing runtime entrypoint.
- `~/.litellm/service/run/` is reserved for service-owned runtime artifacts if needed.

## Architecture

The system has four parts:

1. **LiteLLM home**
   - `~/.litellm` is the canonical home for all LiteLLM operations on this machine.
   - The active config file is `~/.litellm/config.yaml`.

2. **Pinned runtime**
   - A dedicated `uv` project is created under `~/.litellm/service`.
   - LiteLLM is pinned to `1.89.0`.
   - This runtime is prepared ahead of time and is not bootstrapped during login.

3. **Stable wrapper script**
   - The LaunchAgent does not run LiteLLM directly.
   - It calls `~/.litellm/service/bin/start-litellm.sh`.
   - The script is a thin entrypoint only.

4. **LaunchAgent**
   - A per-user LaunchAgent under `~/Library/LaunchAgents/` owns login startup and restart behavior.
   - It waits 10 seconds after load before starting the service.
   - It supervises the actual LiteLLM process through the wrapper script's final `exec`.

## Bootstrap Flow

Bootstrap is a one-time preparation step that must be completed before reboot.

Bootstrap performs the following:

1. Create `~/.litellm/service/` and required subdirectories.
2. Create a pinned `uv` project under `~/.litellm/service/`.
3. Install LiteLLM `1.89.0` into that project.
4. Create the stable wrapper script at `~/.litellm/service/bin/start-litellm.sh`.
5. Create the LaunchAgent plist in `~/Library/LaunchAgents/`.
6. Load the LaunchAgent so the setup can be tested before reboot.

Bootstrap is intentionally separate from steady-state runtime.

The login service must never perform package resolution or dependency installation.

## Runtime Flow

Steady-state runtime after bootstrap is:

```text
login
  -> launchd loads LaunchAgent
  -> launchd-native 10 second delay
  -> LaunchAgent runs ~/.litellm/service/bin/start-litellm.sh
  -> wrapper enters pinned runtime
  -> wrapper execs LiteLLM 1.89.0
  -> LiteLLM binds 127.0.0.1:5596
  -> LiteLLM reads ~/.litellm/config.yaml
```

The wrapper script is intentionally minimal:

1. Resolve service-owned paths.
2. Ensure service-owned directories needed for normal execution exist.
3. Enter the pinned runtime under `~/.litellm/service`.
4. `exec` the LiteLLM command.

The wrapper script must not:

- sleep for launch delay
- implement retry counting
- manage restart policy
- perform extended validation or preflight logic
- contain widget-specific behavior

## Wrapper Script Design

The wrapper script is a stable shell entrypoint. Its purpose is to keep the LaunchAgent plist simple and to provide one consistent command for both manual testing and launchd startup.

Conceptual shape:

```sh
#!/bin/sh
set -eu

LITELLM_HOME="$HOME/.litellm"
SERVICE_HOME="$LITELLM_HOME/service"
PROJECT_HOME="$SERVICE_HOME/project"
LOG_HOME="$SERVICE_HOME/logs"

mkdir -p "$LOG_HOME" "$SERVICE_HOME/run"

cd "$SERVICE_HOME"

exec uv run litellm \
  --config "$LITELLM_HOME/config.yaml" \
  --host 127.0.0.1 \
  --port 5596
```

This is conceptual, not final implementation text, but the requirements are fixed:

- it must be shell-based
- it must be stable
- it must end in `exec`
- it must use the pinned project runtime
- it must hard-bind to `127.0.0.1:5596`

## LaunchAgent Design

The LaunchAgent is a per-user launchd job.

Responsibilities:

- start automatically on login
- apply a 10-second startup delay using launchd behavior
- invoke the stable wrapper script
- write stdout/stderr to service log files
- restart the service after unexpected exit
- rely on launchd throttling instead of a custom retry cap

LaunchAgent behavior requirements:

1. **User scope**
   - Use a LaunchAgent, not a LaunchDaemon.
   - The service is user-local and should run in the user's login session.

2. **Delayed startup**
   - The 10-second delay belongs to launchd behavior, not to shell `sleep`.

3. **Restart authority**
   - launchd is the only restart authority.
   - The wrapper script does not attempt to supervise or relaunch LiteLLM.

4. **Throttled retries**
   - launchd retry throttling is sufficient.
   - The design deliberately does not include a hard retry cap.

5. **Logging**
   - Stdout and stderr are redirected to files under `~/.litellm/service/logs`.

## Failure Model

The design uses launchd-native restart semantics.

Desired outcomes:

- If LiteLLM crashes unexpectedly, launchd retries it with throttling.
- If the operator unloads the LaunchAgent, the service stops and remains stopped.
- If LiteLLM fails because of bad config or missing environment, launchd may retry according to its throttle policy, and the operator diagnoses the issue from logs.

This design intentionally does not add a custom retry counter or stateful wrapper logic.

Rationale:

- a retry cap would make the wrapper script act like a second supervisor
- that duplicates launchd responsibility
- it complicates the service model for little benefit in a lean local setup

## Logging

The service uses plain file logs only.

Required log location:

```text
~/.litellm/service/logs/
```

Required log outputs:

- `litellm.stdout.log`
- `litellm.stderr.log`

The design does not require macOS unified logging or Console.app integration.

Rationale:

- this is a user-local service
- simple file logs are the most direct debugging surface
- unified logging would add complexity without clear value for this use case

## Environment

The design does not require a dedicated env file by default.

Current expectation:

- LiteLLM config references `os.environ/AZURE_OPENAI_API_KEY`
- the user already has a LaunchAgent that injects `AZURE_OPENAI_API_KEY` into the launchd environment

Therefore:

- the LaunchAgent may rely on inherited user launchd environment
- no separate env file is required unless later reliability testing proves it necessary

This is an implementation assumption to verify during rollout, not a reason to complicate the design upfront.

## Security And Exposure

The service is local-only.

Hard requirements:

- bind only to `127.0.0.1`
- do not expose LiteLLM on LAN or public interfaces
- do not design for remote access in this spec

## Explicit Non-Goals

This design intentionally does not include:

- widget startup authority
- widget-mediated reload logic
- ServiceManagement registration via app bundle
- LiteLLM Admin UI setup
- master key setup
- database-backed spend tracking
- budgets, virtual keys, or RBAC
- external host binding
- `uvx`

## Open Questions Resolved

The following decisions are fixed by this design:

- Use `~/.litellm` as canonical LiteLLM home.
- Keep active config at `~/.litellm/config.yaml`.
- Put runtime machinery under `~/.litellm/service`.
- Use a dedicated pinned `uv` project under `~/.litellm/service`.
- Pin LiteLLM to `1.89.0`.
- Use a stable shell script as the LaunchAgent entrypoint.
- Use `exec` in the wrapper script.
- Use launchd-native 10-second delayed startup.
- Drop the hard retry cap.
- Use launchd throttling only.
- Use file logs only.
- Hard-bind to `127.0.0.1`.
- Use port `5596`.

## Recommended Next Step

After spec approval, the next step is to create an implementation plan that covers:

1. bootstrap of the pinned `uv` project
2. wrapper script creation
3. LaunchAgent plist design and delayed-start mechanism
4. local verification before reboot
5. reboot verification checklist
