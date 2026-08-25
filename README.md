# litellm-simplified

One-command installer for [LiteLLM Proxy](https://github.com/BerriAI/litellm) as a persistent, rootless macOS user service.

## What it does

- Bootstraps a pinned `uv` venv with `litellm[proxy]==1.89.0`
- Registers a `launchd` LaunchAgent for auto-start on login + crash recovery
- Binds to `127.0.0.1:5596` — localhost only, no root required
- Supports idempotent re-runs and clean uninstall

## Quick start

```bash
chmod +x litellm-proxy-install.sh
./litellm-proxy-install.sh
```

## Documentation

- **Design:** [`docs/design/`](docs/design/) — architecture decisions, specs, implementation plans
- **Reference:** [`docs/reference/`](docs/reference/) — best practices, operator notes, prompts
- **Reports:** [`docs/reports/`](docs/reports/) — post-mortems, audit results
