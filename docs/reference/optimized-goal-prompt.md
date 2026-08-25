# Optimized Goal Prompt For LiteLLM Local Service Setup

Use this as the optimized prompt next time:

```text
Create a goal for this task and execute it end-to-end with the shortest-path approach.

Goal:
Set up a lean local LiteLLM proxy service on my Mac that runs from a pinned `uv` runtime under `~/.litellm/service`, uses `~/.litellm/config.yaml`, binds to `127.0.0.1:5596`, and is managed by a per-user LaunchAgent. Verify the real runtime path works under `launchctl`, not just in an interactive shell.

Constraints:
- Do not use `uvx`.
- Do not involve any widget app or helper app.
- Do not overproduce process artifacts unless I explicitly ask.
- Do not write specs, plans, reports, or research docs unless blocked or asked.
- Prefer direct execution over subagent orchestration unless parallel work is clearly necessary.
- Use no whitespace in any newly created folder names.
- Keep the wrapper minimal and end it with `exec`.
- Launchd is the only restart authority.
- Use file logs, not extra logging systems.
- Do not assume launchd inherits my shell `PATH`.
- Be suspicious of `StartInterval` as a “10-second after login” mechanism unless you can prove it works in practice.

Required implementation target:
- Canonical home: `~/.litellm`
- Runtime: `~/.litellm/service`
- Wrapper: `~/.litellm/service/bin/start-litellm.sh`
- Config: `~/.litellm/config.yaml`
- LaunchAgent: `~/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist`
- LiteLLM version: `1.89.0`
- Host: `127.0.0.1`
- Port: `5596`

Execution priorities:
1. Inspect existing `~/.litellm/config.yaml`.
2. Create the pinned `uv` project in `~/.litellm/service`.
3. Create the wrapper script.
4. Make the wrapper use an absolute `uv` path, not bare `uv`, if needed for launchd.
5. Create the LaunchAgent.
6. Test with `launchctl bootstrap`, `launchctl kickstart`, `launchctl print`, log inspection, listener check, and `/models`.
7. Fix only the concrete blocker you observe.
8. Stop once the service is working or a single clear blocker remains.

Verification standard:
- `uv run litellm --version` works from `~/.litellm/service`
- wrapper starts the proxy
- LaunchAgent can start the proxy under `launchctl`
- proxy listens on `127.0.0.1:5596`
- `/models` responds
- logs and launchctl state are checked
- if delayed auto-start after login is not provable, say that explicitly instead of pretending it is solved

Output style:
- Keep commentary short.
- Focus on implementation and verification.
- At the end, give me only:
  - what changed
  - what works
  - what remains unresolved
  - exact next validation step if needed

Anti-goals:
- no speculative architecture work
- no long research detours
- no review theater
- no unnecessary subagents
- no folder names with spaces
```

## One-Paragraph Version

```text
Create a goal and solve this with the shortest-path execution only: set up a lean local LiteLLM `1.89.0` proxy on my Mac using a pinned `uv` runtime under `~/.litellm/service`, config at `~/.litellm/config.yaml`, wrapper at `~/.litellm/service/bin/start-litellm.sh`, and a per-user LaunchAgent at `~/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist`, bound to `127.0.0.1:5596`; do not use `uvx`, do not involve any widget app, do not create plans/specs/research unless blocked, do not use whitespace in new folder names, keep the wrapper minimal with final `exec`, assume launchd does not inherit shell `PATH`, prefer an absolute `uv` path if needed, verify with real `launchctl` behavior (`bootstrap`, `kickstart`, `print`), logs, listener checks, and `/models`, fix only concrete blockers you observe, and finish with only: what changed, what works, what remains unresolved, and the exact next validation step if anything is still uncertain.
```
