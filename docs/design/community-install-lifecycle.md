# LiteLLM Community Install and Lifecycle Catalog

Date: 2026-08-01

## Scope

This note catalogs how LiteLLM is commonly installed, run, upgraded, supervised, and recovered in local or self-hosted setups, with emphasis on end-user lifecycle ergonomics rather than cluster-scale deployment. It distinguishes:

- Official LiteLLM guidance and supported paths
- Community conventions seen in wrappers, boilerplates, and forks
- Adjacent patterns that improve start/stop/reload, upgrades, rollback, logging, and low-friction operations

## Executive Readout

The center of gravity is clear:

- Official LiteLLM guidance centers on two runtime paths: `litellm --config config.yaml` for a direct Python/CLI install, and Docker Compose for a fuller gateway deployment with Postgres and the Admin UI. Kubernetes and Helm are the official production path, not the common local-user path. [O1][O2][O5]
- Community local setups overwhelmingly optimize for one of two operator models:
  - `uv` or venv plus an OS-native service manager for a lightweight local daemon
  - Docker Compose plus mounted config, `.env`, persistent volumes, and `docker compose` as the lifecycle interface [C1][C2][C3][C4]
- Homebrew exists around LiteLLM, but the official Homebrew packaging in-repo is for the thin `lite` client rather than the main proxy lifecycle. That makes Homebrew more relevant for client tooling than for the durable local proxy service itself. [O9][O10]
- The best lifecycle experience comes from adding a stable wrapper layer around LiteLLM itself: one command family for `start`, `stop`, `restart`, `status`, `logs`, `health`, `upgrade`, and `rollback`, backed by launchd on macOS or systemd on Linux. Official LiteLLM provides the health, migration, and configuration primitives; community projects add the operator ergonomics. [O3][O4][O6][O7][C1][C2]

## Official LiteLLM Guidance

### 1. Installation surfaces

Official docs present these as the real supported entry points:

- Python/CLI quick start: run the proxy directly with `config.yaml`. [O1]
- Docker quick start: start a complete stack quickly, then manage most configuration through the Admin UI. [O2]
- Production deployment: use Helm/Kubernetes and externalize migration control more carefully. [O5][O6]

Implication: official LiteLLM clearly supports local direct-run and Docker-first flows, but does not itself prescribe a local macOS/Linux service manager story such as launchd or systemd. That layer is mostly left to users and community wrappers. [O1][O2][O5]

### 2. Config placement and editing model

Official docs treat `config.yaml` as the primary configuration surface. LiteLLM also supports file composition via `include`, and on startup or refresh it loads `config.yaml` first, then overlays some settings from the database. [O3][O8]

Operationally, this means there are two common official configuration modes:

- File-first local mode: keep models and settings in `config.yaml`, edit the file, restart or refresh, and keep the runtime mostly deterministic. [O1][O3][O8]
- UI/DB-assisted mode: use the Admin UI and DB-backed configuration for models, credentials, and policy, while `config.yaml` remains the bootstrap layer. [O2][O3]

For low-risk local lifecycle, file-first is easier to diff, back up, and roll back than heavy DB-edited state.

### 3. Secrets handling

Official Docker quick start explicitly recommends passing provider keys as environment variables and referencing `os.environ/...` instead of storing raw provider keys directly in the UI. The docs also state that `LITELLM_SALT_KEY` encrypts provider API keys added in the UI and warn that it should be set to a strong value before storing anything durable, then not changed casually afterward. Master-key rotation is separately documented because the master key authenticates admin access and may participate in credential protection, depending on deployment. [O2][O11]

This yields a strong official pattern:

- Prefer environment variables or secret files outside the YAML
- Use `config.yaml` to reference secrets, not to embed them
- Treat `LITELLM_MASTER_KEY` and `LITELLM_SALT_KEY` as lifecycle-critical secrets, not disposable startup flags

### 4. Health checks, logs, and supervision hooks

Official health docs make LiteLLM operable by external supervisors: `/health/readiness` is the key readiness probe, `/health/liveliness` is the basic liveness signal, and the health documentation explicitly frames these endpoints as inputs to orchestrators, load balancers, and uptime monitors. [O4]

That is important because LiteLLM itself does not need to be the supervisor. It only needs to expose reliable health endpoints and logs so that launchd, systemd, Docker, or Kubernetes can do the real lifecycle work.

### 5. Upgrade and rollback model

Official docs describe:

- Auto-running schema migrations on startup by default in production-oriented setups [O6]
- `DISABLE_SCHEMA_UPDATE=true` when operators want manual control of schema change timing [O5][O6]
- A documented rollback procedure that may require deleting newer migration records and restarting the older LiteLLM version so it can re-apply the matching migration set [O7]
- A dedicated `uv/venv` upgrade guide and Docker image signature verification guidance [O6][O12]

Operationally, LiteLLM is easiest to run when application versioning and schema changes are treated as a coupled lifecycle, not as an afterthought.

### 6. Release cadence and upgrade caution

Release notes show a frequent release cadence, and the project documents versioning changes such as the PEP 440 naming transition starting with `v1.84.0`. The docs also include a March 2026 security update around a PyPI supply-chain incident affecting compromised LiteLLM package versions, with verified-safe version guidance. [O13][O14]

That raises the bar for local lifecycle design:

- pin versions
- stage upgrades
- verify artifacts
- avoid blind `latest` pulls for anything that persists state or secrets

## Community Conventions and Wrappers

### 1. Docker Compose as the dominant “easy button”

Community LiteLLM projects repeatedly package the proxy as Docker Compose with a mounted config, `.env`, and often Postgres plus optional UI layers. Examples include `korjavin/litellm-compose`, `chrisurf/litellm`, and `teremterem/litellm-server-boilerplate`, which explicitly advertises `uv` plus Docker and includes optional LibreChat integration. [C3][C4][C5]

Why this pattern keeps showing up:

- one command to bring the stack up
- easy log access via `docker compose logs`
- image pinning provides a natural rollback handle
- secrets fit naturally into `.env` or compose environment blocks
- config edits become bind-mounted file edits instead of in-container changes

Community preference here is less about “Docker is best” and more about “Docker Compose gives operators a familiar lifecycle surface.”

### 2. launchd wrappers on macOS

Community macOS wrappers tend to convert LiteLLM into a real background service with an installer that creates a stable directory, copies config, generates a launchd plist, and starts the service. Search-visible examples include `Samuel86-star/litellm-proxy`, whose install script is described as creating `~/litellm/`, copying config, generating the correct launchd plist, and starting the service, plus other macOS-local wrappers like `kyr0/litellm-macos`, `cdrxyz/litellm-local`, and `guomk/litellm-proxy`. [C1][C6][C7][C8]

This is a strong local-user pattern because launchd gives:

- autostart on login or boot
- crash restart
- standard OS ownership of the daemon
- predictable log and status inspection through launchctl and system logs

The best macOS community wrappers also hide LiteLLM internals behind a small control surface so the user does not need to remember raw command lines.

### 3. systemd wrappers on Linux

Linux community projects predictably move to systemd. `preston-bernstein/llm-gateway` is representative: it is explicitly described as a self-hosted LiteLLM proxy running as a hardened systemd service. [C2]

That pattern usually implies:

- a dedicated working directory
- an environment file for secrets
- a systemd unit with restart policy
- lifecycle via `systemctl`
- logs via `journalctl`

Compared with ad hoc shell scripts, this sharply lowers friction for `status`, `restart`, `enable at boot`, and log inspection.

### 4. `uv` as the preferred Python-side installer in newer community material

Newer community wrappers and guides disproportionately use `uv` rather than raw `pip` for local LiteLLM installs. The boilerplate repo above is explicitly positioned around `uv`, and LiteLLM’s own thin `lite` CLI install path also leans on `uv` bootstrapping. [C5][O10]

This matters because `uv` reduces three sources of lifecycle pain:

- interpreter provisioning
- dependency isolation
- deterministic reinstall or upgrade flows

For local-user lifecycle, `uv` is the cleanest Python-native path when not using Docker.

### 5. Homebrew is adjacent, not central

The official in-repo Homebrew packaging explains why it remains a tap formula and not a `homebrew-core` formula: the formula builds the published `litellm` sdist with the CLI extra and depends on network resolution at install time, which `homebrew-core` does not allow. The same packaging README describes the formula as the Homebrew path for the thin `lite` CLI. [O9][O10]

The practical reading is:

- Homebrew is not the main official story for the full LiteLLM proxy service lifecycle
- It is more credible as a client-tool install convenience than as the core operator path for a durable local gateway

Community projects that rely on Homebrew usually do so for adjacent dependencies like Postgres, not because Homebrew is the best place to own the running LiteLLM service itself. [C9]

### 6. Adjacent community practice: wrapper CLIs over raw supervisors

An adjacent but useful pattern is to hide launchd/systemd behind a simpler control CLI. A good example is `macpmd`, a process manager that uses launchd on macOS and systemd on Linux for persistence and crash recovery while exposing a PM2-like operator interface. [C10]

This pattern is not LiteLLM-specific, but it matches what the best LiteLLM local wrappers are trying to achieve:

- keep the OS-native supervisor under the hood
- give the user a friendly `start|stop|restart|logs|status` surface

## What Actually Minimizes User Friction

Across official docs and community practice, the easiest local lifecycle has these properties:

### Good

- One durable install root
- One canonical config path
- Secrets outside the config file
- One service identity
- One command family for lifecycle actions
- Health checks that supervisors and humans can both use
- Version pinning and explicit upgrades
- Logs available from one obvious place
- Rollback that means “switch version and restart,” not “reverse-engineer state”

### Bad

- Running LiteLLM from an interactive shell without supervision
- Editing secrets directly into `config.yaml`
- Depending on floating `latest` images or package versions
- Letting schema changes happen during every experimental upgrade without a rollback plan
- Splitting runtime state across too many places: shell profile, random env exports, UI-edited credentials, and mutable container internals

## Recommendations for This User’s Local LiteLLM Lifecycle

These recommendations are aimed at a low-risk local deployment with easy day-2 operations.

### Recommended runtime choice

Use one of these two models and avoid mixing them:

1. Preferred for a lightweight local daemon on this machine: `uv`-managed LiteLLM plus launchd on macOS.
2. Preferred if UI, Postgres-backed state, or easier rollback-by-image is more important: Docker Compose with pinned image tags and mounted config.

Because this workspace already prefers `uv` for Python tooling, the first option is the cleaner fit unless there is a hard requirement for the full Docker quickstart stack.

### Recommended layout

- `~/.config/litellm/` or another single stable directory for config and helper scripts
- `config.yaml` in that directory
- `.env` or launchd environment file equivalent for provider keys and LiteLLM secrets
- logs routed to either launchd-managed files or a single known application log path
- a tiny control wrapper such as `litellmctl`

### Recommended lifecycle surface

Expose exactly these commands to the end user:

- `litellmctl start`
- `litellmctl stop`
- `litellmctl restart`
- `litellmctl status`
- `litellmctl logs`
- `litellmctl health`
- `litellmctl edit-config`
- `litellmctl upgrade <pinned-version>`
- `litellmctl rollback <previous-pinned-version>`

Under the hood:

- `start/stop/restart/status` should use launchd
- `health` should hit `/health/readiness` and a basic model-serving check
- `upgrade` should install a pinned version, run a preflight, restart, and verify health
- `rollback` should restore the previous pinned version and re-check health

### Secrets and config

- Keep provider keys out of `config.yaml`
- Reference secrets via environment variables such as `os.environ/...`
- Treat `LITELLM_MASTER_KEY` and `LITELLM_SALT_KEY` as persistent secrets with backup and rotation procedures
- Prefer file-first config over UI-only edits for anything that should be easy to diff and recover

### Upgrade strategy

- Pin LiteLLM versions explicitly
- Keep the current and previous version immediately available for rollback
- Read release notes before every upgrade
- Avoid automatic upgrades from PyPI or unpinned container tags
- Verify Docker images where relevant and be conservative with package upgrades given the documented March 2026 PyPI incident [O12][O14]

### Service supervision

- On macOS, use launchd rather than a raw long-running shell or ad hoc terminal tab
- On Linux, use systemd rather than `nohup`, `screen`, or a hand-rolled shell loop
- If a friendlier CLI is wanted, wrap launchd/systemd instead of replacing them with a weaker custom supervisor

### Lowest-risk default recommendation

For this user, the lowest-friction and lowest-risk local lifecycle is:

- install LiteLLM into an isolated `uv`-managed environment
- run it under launchd
- keep `config.yaml` and `.env` in one stable directory
- expose a small `litellmctl` wrapper for lifecycle actions
- pin every upgrade
- keep rollback to the immediately previous version trivial
- use `/health/readiness` as the restart/upgrade success gate

That design follows the official LiteLLM primitives, borrows the best community ergonomics, and avoids the most common local-ops failure modes.

## Sources

### Official LiteLLM

- [O1] LiteLLM Docs, “CLI - Quick Start”. https://docs.litellm.ai/docs/proxy/quick_start
- [O2] LiteLLM Docs, “Quickstart” (Docker Quick Start). https://docs.litellm.ai/docs/proxy/docker_quick_start
- [O3] LiteLLM Docs, “Overview” (proxy configs). https://docs.litellm.ai/docs/proxy/configs
- [O4] LiteLLM Docs, “Health Checks”. https://docs.litellm.ai/docs/proxy/health
- [O5] LiteLLM Docs, “Production Deployment”. https://docs.litellm.ai/docs/proxy/deploy
- [O6] LiteLLM Docs, “Production Best Practices” and “Upgrading LiteLLM Proxy (uv/venv)”. https://docs.litellm.ai/docs/proxy/prod and https://docs.litellm.ai/docs/troubleshoot/pip_venv_upgrade
- [O7] LiteLLM Docs, “Safe Rollback Guide”. https://docs.litellm.ai/docs/troubleshoot/rollback
- [O8] LiteLLM Docs, “File Management”. https://docs.litellm.ai/docs/proxy/config_management
- [O9] BerriAI LiteLLM repo, `packaging/homebrew` README. https://github.com/BerriAI/litellm/blob/litellm_internal_staging/packaging/homebrew/README.md
- [O10] LiteLLM Docs, “LiteLLM Proxy CLI”. https://docs.litellm.ai/docs/proxy/management_cli
- [O11] LiteLLM Docs, “Rotating the Master Key”. https://docs.litellm.ai/docs/proxy/master_key_rotations
- [O12] LiteLLM Docs, “Docker Image Security Guide”. https://docs.litellm.ai/docs/proxy/docker_image_security
- [O13] LiteLLM Docs, release notes and `v1.84.0` note on version naming. https://docs.litellm.ai/release_notes and https://docs.litellm.ai/release_notes/v1.84.0/v1-84-0
- [O14] LiteLLM Docs, “Security Update: Suspected Supply Chain Incident” and linked GitHub issue on compromised PyPI versions. https://docs.litellm.ai/blog/security-update-march-2026 and https://github.com/BerriAI/litellm/issues/24518

### Community and Adjacent Practice

- [C1] `Samuel86-star/litellm-proxy`, macOS-oriented launchd setup and installer. https://github.com/Samuel86-star/litellm-proxy
- [C2] `preston-bernstein/llm-gateway`, LiteLLM as a hardened systemd service. https://github.com/preston-bernstein/llm-gateway
- [C3] `korjavin/litellm-compose`, Docker Compose-based LiteLLM deployment. https://github.com/korjavin/litellm-compose
- [C4] `chrisurf/litellm`, Docker setup with Postgres and web UI. https://github.com/chrisurf/litellm
- [C5] `teremterem/litellm-server-boilerplate`, `uv` plus Docker boilerplate with optional LibreChat. https://github.com/teremterem/litellm-server-boilerplate
- [C6] `kyr0/litellm-macos`, local LiteLLM proxy deployment for macOS. https://github.com/kyr0/litellm-macos
- [C7] `cdrxyz/litellm-local`, thin self-contained local proxy for macOS and LM Studio workflows. https://github.com/cdrxyz/litellm-local
- [C8] `guomk/litellm-proxy`, includes launchd material for macOS. https://github.com/guomk/litellm-proxy/tree/main/launchd
- [C9] `spdesai25/litellm-proxy`, macOS setup using Homebrew for Postgres and Python venv for LiteLLM. https://github.com/spdesai25/litellm-proxy
- [C10] `WaterJuice/macpmd`, adjacent process-manager pattern built on launchd/systemd. https://github.com/WaterJuice/macpmd
