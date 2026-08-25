# LiteLLM LaunchAgent Implementation Report

## Purpose

This report captures what was implemented for the local LiteLLM proxy service, why each piece exists, what was verified, what remains unresolved, and how future developers should treat this functionality.

The implementation goal was to separate the core LiteLLM proxy service from the in-development widget app, replace the ad hoc `uvx` launch path with a pinned local runtime, and create a durable macOS user-local service model that can be reasoned about, tested, and maintained independently.

## High-Level Outcome

The rollout implemented a pinned LiteLLM runtime under `~/.litellm/service`, a stable wrapper script, and a per-user LaunchAgent at `~/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist`.

The service now has:

- a dedicated pinned runtime home
- a stable operator-facing startup entrypoint
- a user LaunchAgent registration
- file-based logs
- a concrete fix for launchd's minimal `PATH` behavior by using an absolute `uv` path in the wrapper

What is fully working:

- direct wrapper execution starts LiteLLM correctly
- manual launchd start via `launchctl kickstart -k gui/$(id -u)/com.thedawgctor.litellm-proxy` starts the proxy correctly
- the proxy listens on `127.0.0.1:5596`
- the `/models` endpoint responds with the configured model list

What remains unresolved:

- the `StartInterval`-only delayed auto-start path has not yet been demonstrated to fire on its own after a plain `bootout`/`bootstrap` cycle in the observed pre-login verification window

That unresolved point is narrow but important: the service wiring works, but the exact launchd-native delayed auto-start behavior is still not proven end-to-end.

## Implemented Files And Runtime Layout

The implementation established the following layout:

```text
~/.litellm/
  config.yaml
  service/
    pyproject.toml
    uv.lock
    .venv/
    bin/
      start-litellm.sh
    logs/
      litellm.stdout.log
      litellm.stderr.log
    run/
```

And the LaunchAgent:

```text
~/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist
```

## What We Implemented And Why

### 1. Canonical LiteLLM Home: `~/.litellm`

**What exists**

- Active config remains at `~/.litellm/config.yaml`
- Runtime machinery lives under `~/.litellm/service`

**Intent**

The intent is to give LiteLLM one canonical home on the machine so operators and future developers do not have to mentally merge multiple locations such as:

- config in one place
- runtime in transient cache state
- service files somewhere else

This reduces ambiguity, makes manual inspection easier, and gives installers and maintenance scripts one predictable root.

### 2. Pinned `uv` Runtime Under `~/.litellm/service`

**What exists**

- `pyproject.toml`
- `uv.lock`
- materialized `.venv`
- LiteLLM pinned to `1.89.0`

**Intent**

The intent is to eliminate `uvx`-style ephemeral runtime behavior and replace it with a durable, reproducible local runtime. This addresses the earlier operational complaint that `uvx` downloads too much and makes service startup too implicit and too variable.

This feature exists so that:

- startup behavior is deterministic
- upgrades are deliberate
- rollback is possible through lockfile and runtime management
- service debugging is not entangled with cache state

### 3. Stable Wrapper Script: `~/.litellm/service/bin/start-litellm.sh`

**What exists**

The wrapper:

- is shell-based
- is minimal
- creates required service-owned directories
- enters `~/.litellm/service`
- ends with `exec`
- runs LiteLLM on `127.0.0.1:5596`
- now calls `/opt/homebrew/bin/uv` explicitly

**Intent**

The wrapper exists to provide one stable, operator-facing service entrypoint and to keep the LaunchAgent plist simple.

It is intentionally not a supervisor, validator, or installer. It should remain a thin bridge between launchd and the pinned LiteLLM runtime.

That design serves several goals:

- the same command can be tested manually and run under launchd
- launchd supervises the real LiteLLM process directly because of `exec`
- runtime assumptions stay explicit and inspectable
- policy does not leak into shell logic

### 4. Absolute `uv` Path In The Wrapper

**What exists**

The wrapper now executes:

```sh
exec /opt/homebrew/bin/uv run litellm ...
```

**Intent**

This change exists because launchd runs with a minimal default `PATH`:

```text
/usr/bin:/bin:/usr/sbin:/sbin
```

That caused the concrete failure:

```text
exec: uv: not found
```

The intent of the absolute path is to remove dependence on interactive shell environment and make the service launch path self-sufficient under launchd.

This is one of the most important implementation details in the rollout because it converts a theoretical service definition into one that can actually start under launchd.

### 5. Per-User LaunchAgent: `com.thedawgctor.litellm-proxy`

**What exists**

The LaunchAgent:

- lives in `~/Library/LaunchAgents`
- runs in the user GUI domain
- invokes the stable wrapper script
- writes stdout/stderr to service log files
- sets `ThrottleInterval` to `10`
- uses `StartInterval` `10` as the current launchd-native approximation for delayed startup

**Intent**

The LaunchAgent exists to make the proxy a user-managed login service rather than a terminal-bound ad hoc process.

That means:

- login should become the normal service entrypoint
- the widget should not be required for basic service availability
- service lifecycle should be managed via launchd rather than open terminal tabs

The user-local LaunchAgent was chosen instead of a system daemon because this service is tied to one user's config, one user's credentials, one user's workflow, and localhost-only usage.

### 6. File-Based Logging

**What exists**

- `~/.litellm/service/logs/litellm.stdout.log`
- `~/.litellm/service/logs/litellm.stderr.log`

**Intent**

The logs exist to make failure diagnosis straightforward without adding extra macOS-specific observability layers.

The design intentionally prefers simple file logs because:

- this is a user-local service
- plain files are the fastest debugging surface
- they work regardless of Console.app familiarity
- installers and support scripts can point to them directly

### 7. Host And Port Locking

**What exists**

- host is fixed to `127.0.0.1`
- port is fixed to `5596`

**Intent**

The host restriction exists to keep the proxy local-only and reduce accidental exposure.

The fixed port exists to give dependent tools a stable endpoint and to avoid the split-brain behavior already observed when different parts of the system expected different ports.

This feature is fundamentally about predictability.

### 8. Launchd As The Only Restart Authority

**What exists**

- no custom retry counter
- no wrapper-level supervision loop
- no widget-level lifecycle dependency in the core design

**Intent**

The intent is to keep lifecycle control in one place. launchd should decide when the service is loaded, reloaded, stopped, or restarted. The wrapper should not try to become a second init system.

This keeps the implementation lean and easier to audit.

## Verification Summary

### Verified Working

The following behaviors were demonstrated:

1. The pinned LiteLLM runtime exists and reports version `1.89.0`.
2. The wrapper script is syntactically valid and executable.
3. Running the wrapper directly starts LiteLLM successfully.
4. The proxy listens on `127.0.0.1:5596`.
5. The `/models` endpoint returns the configured model list.
6. The LaunchAgent plist is syntactically valid.
7. The LaunchAgent can be bootstrapped and inspected with `launchctl`.
8. After switching the wrapper to an absolute `uv` path, `launchctl kickstart -k ...` successfully starts the service.

### Verified Failure And Fix

The rollout found and fixed one concrete launchd/runtime defect:

- **Failure:** `uv` was not found when the wrapper was launched by launchd
- **Cause:** launchd's stripped-down default `PATH`
- **Fix:** use `/opt/homebrew/bin/uv` explicitly in the wrapper

This is an important example of why launchd verification mattered before reboot.

## Remaining Unresolved Behavior

The unresolved issue is not whether the service can run under launchd at all. It can.

The unresolved issue is whether the current LaunchAgent design truly produces the intended delayed automatic start after login or after equivalent load events using:

```xml
<key>StartInterval</key>
<integer>10</integer>
```

Observed behavior after bootstrap-only reload:

- `state = not running`
- `runs = 0`
- `pended nondemand spawn = interval`
- no listener on `127.0.0.1:5596`

Observed behavior after manual demand:

- service starts
- listener appears
- logs show normal LiteLLM startup

So the implementation currently supports:

- manual wrapper start
- manual launchd kickstart

But it does not yet conclusively support:

- proven automatic interval-driven launch after a plain reload
- proven automatic 10-second delayed startup after real login

That means the service is operationally close, but not yet fully closed out as a finished login-startup mechanism.

## Design Intent Versus Implemented Reality

This distinction matters.

**Design intent**

- login-owned local service
- 10-second delayed startup
- no widget dependency
- reproducible local runtime
- stable wrapper
- simple logs

**Implemented reality today**

- all structural pieces exist
- direct execution works
- launchd execution works when explicitly started
- `uv` path defect is fixed
- delayed interval auto-start remains unproven

Future work should start from the implemented reality, not from the original spec language alone.

## Operational Guidance

Until the interval behavior is fully resolved, the most reliable operator model is:

1. Treat the runtime and wrapper as correct.
2. Treat the LaunchAgent registration as structurally correct.
3. Treat delayed auto-start as still under verification.
4. Use logs and `launchctl print gui/$(id -u)/com.thedawgctor.litellm-proxy` as the primary inspection surfaces.
5. Keep manual `kickstart` available as a recovery or diagnostic path.

## Recommended Next Validation

The next validation should be a real login-cycle test:

1. Log out and back in, or reboot.
2. Wait at least 12 seconds.
3. Check `lsof -nP -iTCP:5596 -sTCP:LISTEN`.
4. Check the local `/models` endpoint.
5. Inspect logs and `launchctl print` if the listener does not appear.

If auto-start still does not happen after a real login-cycle test, the team should stop treating `StartInterval` as a proven delayed-login mechanism and revise the LaunchAgent design accordingly.

## Clear Statement For Future Developers

Future developers must treat this functionality as a small, user-local service subsystem with strict separation of concerns: the pinned runtime lives under `~/.litellm/service`, the wrapper remains a minimal `exec`-based entrypoint, launchd is the only lifecycle authority, and widget development must not be allowed to reintroduce hidden coupling or operational dependence. Do not add retry logic, policy branching, or environment magic into the wrapper; do not assume interactive shell state under launchd; prefer explicit paths and inspectable behavior; and do not claim the delayed-login startup is solved until it is demonstrated through a real login-cycle verification rather than inferred from plist structure alone.
