# LiteLLM Proxy Installer Notes

## Assumptions

- The canonical LiteLLM home is `~/.litellm`.
- The pinned runtime lives at `~/.litellm/service`.
- The active config is `~/.litellm/config.yaml`.
- The wrapper entrypoint is `~/.litellm/service/bin/start-litellm.sh`.
- The LaunchAgent label defaults to `com.thedawgctor.litellm-proxy`.
- The proxy binds `127.0.0.1:5596`.
- LiteLLM is pinned to `1.89.0`.
- The installer should prefer an absolute `uv` path because launchd's PATH is minimal.

## Operator Prompts

- Override or accept the default home, runtime, config, wrapper, label, host, port, Python minor version, and LaunchAgents directory.
- Choose how to handle `uv`:
  - reuse a detected binary
  - provide a custom absolute path
  - install it with Astral's official installer
- Decide whether to let `uv` install the requested Python runtime if it is not already available.
- Decide whether to create a stub config if the configured `config.yaml` is missing.
- Review environment variables referenced by the config and decide whether to continue if neither the current shell nor launchd exposes them.
- Decide whether to back up existing `pyproject.toml`, wrapper, or plist before overwriting.
- Choose between:
  - delayed auto-start via `StartInterval` (default design target, still not fully proven)
  - immediate `RunAtLoad` plus `KeepAlive`
- Decide whether to reload the LaunchAgent immediately after writing the files.

## Known Unresolved Issues

- The `StartInterval` delayed auto-start path is still not fully proven. Current evidence shows the absolute-`uv` wrapper fix solved the explicit launchd `uv: not found` failure, but a bootstrap-only reload still may leave the job in `runs = 0` with `pended nondemand spawn = interval`.
- The script checks environment variables referenced by `os.environ/...` in the config, but it does not create or manage a dedicated env file. If the required variables are absent from the user launchd environment, the proxy can still fail immediately.
- The script uses `uv lock` and `uv sync` during installation. That is the intended pinned-runtime path, but it assumes normal network/package resolution when the operator actually runs it.
