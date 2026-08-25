# OSS Installation Script Best Practices

Date: 2026-08-01
Scope: OSS installer patterns for macOS and developer tooling, with cross-platform notes where they meaningfully affect service setup, PATH behavior, automation, and supportability.

## Executive Takeaways

The durable pattern across mature OSS tooling is consistent:

1. Prefer a package-manager or signed package path when available; keep the shell installer as a convenience path, not the only path. This is especially visible in Homebrew, uv, and Docker docs, and in Docker's explicit warning not to depend on the convenience script for production deployments. [HB-Install] [HB-Homepage] [UV-Install] [Docker-Install-Repo]
2. Make the installer explain its plan, work in both interactive and unattended modes, and avoid surprising shell-profile or PATH mutations unless the user explicitly opted in or the behavior is clearly documented and suppressible. [HB-Homepage] [UV-Installer] [NVM-Readme] [Rustup-Install]
3. Default to per-user installs and rootless operation where practical; require elevation only for system-wide directories, package-manager bootstrap, or machine-level service registration. [HB-Install] [Docker-Rootless] [Apple-Launchd]
4. Treat service installation as a separate, native integration step. On macOS that means `launchd`/LaunchAgents or LaunchDaemons; on Linux that means `systemd` unit files plus `daemon-reload`, enable/start, and user-vs-system service decisions. [Apple-Launchd] [Launchd-Plist] [Systemctl] [Systemd-Service] [Loginctl]

## Catalog Of Best Practices

### 1. Offer more than one install path

Mature projects usually expose multiple supported paths:

- Homebrew supports the shell installer and also points macOS users at a `.pkg` installer for interactive or unattended MDM use. [HB-Homepage] [HB-Install]
- uv explicitly documents both standalone installers and package-manager installation. [UV-Install]
- Docker publishes distro-native repository instructions and separately calls its script a convenience install path rather than the production recommendation. [Docker-Ubuntu] [Docker-Install-Repo]

Practical rule: design the LiteLLM proxy installer so that `curl | sh` is only one entry point. Also document package-manager, archive/manual, and service-only setup flows.

### 2. Make the script inspectable before it mutates anything

Homebrew's homepage says the script explains what it will do and pauses before doing it. That is a strong UX and trust pattern for `curl | sh` installers. [HB-Homepage]

Practical rule:

- Print an execution plan first: install location, detected OS/arch, dependency path chosen, service mode chosen, files to be created, and whether elevation will be requested.
- In interactive mode, ask for confirmation before any privileged or profile-mutating step.
- In noninteractive mode, require an explicit flag or env var so automation is intentional.

### 3. Support both interactive and unattended execution cleanly

Homebrew supports `NONINTERACTIVE=1` for automation, and its `.pkg` route is documented for unattended MDM installs. uv also exposes installer environment variables specifically for CI and unmanaged environments. [HB-Install] [HB-Homepage] [UV-Installer]

Practical rule:

- Provide first-class `--yes` or `--non-interactive`.
- Provide `--dry-run`.
- Make automation-safe behavior stable and documented.
- Do not prompt conditionally in a way that breaks CI or SSH provisioning.

### 4. Keep OS and architecture detection explicit and fail closed

Rustup, Homebrew, and other mature installers distinguish host platform and architecture because artifact choice and default install prefix depend on them. Homebrew documents different default prefixes on Apple Silicon and Intel macOS, and rustup documents host-target-sensitive installation methods. [HB-Install] [Rustup-Install] [Rustup-Other]

Practical rule:

- Detect OS and architecture once.
- Map that detection to an explicit support matrix.
- Reject unknown or unsupported combinations with a precise error, rather than guessing.
- Log the resolved target artifact name.

For macOS specifically, avoid assuming `/usr/local`; Apple Silicon defaults are materially different from Intel defaults in the Homebrew ecosystem. [HB-Install]

### 5. Detect package managers, but do not be overly magical

The strongest pattern from Homebrew, uv, and Docker docs is not "detect every package manager and improvise"; it is "offer clearly documented supported paths." [HB-Install] [UV-Install] [Docker-Ubuntu] [Docker-Install-Repo]

Practical rule:

- Detect whether tools like `brew`, `uv`, `python3`, `launchctl`, and `systemctl` exist.
- Use detection to choose between already-supported code paths.
- If the preferred manager is absent, say exactly what alternative will be used.
- Do not silently mix install methods on one machine.

### 6. Treat shell profile and environment changes as sensitive operations

This is one of the clearest recurring themes:

- nvm's installer attempts to add sourcing lines to the correct shell profile file. [NVM-Readme]
- uv documents `UV_NO_MODIFY_PATH` and `UV_UNMANAGED_INSTALL` specifically to avoid shell-profile and environment mutation in some environments. [UV-Installer]
- rustup documents `CARGO_HOME` and `RUSTUP_HOME` so installation location is configurable instead of hardcoded. [Rustup-Install]

Practical rule:

- Never assume one profile file.
- Detect shell family and profile candidates, then either ask or print exactly which file will be changed.
- Support `NO_MODIFY_PATH`-style behavior.
- Prefer writing an env file or service-specific environment block over globally editing login shell files when only the service needs the variables.

### 7. Do not assume PATH looks the same everywhere

Homebrew's FAQ explicitly notes that GUI apps on macOS do not inherit Homebrew's prefix in `PATH` by default. That is a critical reminder for macOS agents, menu bar apps, IDE-launched tools, and `launchd` services. [HB-FAQ]

Practical rule:

- Resolve and store absolute executable paths for services.
- Do not rely on `PATH` lookup inside LaunchAgents/LaunchDaemons/systemd units.
- Validate command paths at install time.
- Emit a post-install note if shell restarts or re-sourcing are required for interactive CLI use.

### 8. Make installs idempotent and resumable

Mature installers are expected to be rerunnable:

- nvm's installer centers its state in `~/.nvm` and profile snippets rather than performing opaque one-shot mutations. [NVM-Readme]
- rustup supports reinstall/update/uninstall flows and configurable homes. [Rustup-Install]
- Homebrew publishes both installer and uninstaller paths. [HB-Install-Repo]

Practical rule:

- Before creating files, check whether equivalent state already exists.
- Replace only files you own.
- If a version is already installed, either no-op or perform an explicit upgrade path.
- Maintain a manifest of created files so reruns and uninstall are reliable.

### 9. Build failure handling around phases, not around one giant shell blob

Docker's convenience-script caveat is a useful reminder: convenience scripts drift, and hidden assumptions become production liabilities. [Docker-Install-Repo]

Practical rule:

- Structure the installer into phases: preflight, artifact fetch, install, configure env, register service, verify, summarize.
- Stop on first failure.
- Print the failed phase and the exact remediation.
- Clean up temporary files deterministically.
- For owned files, support rollback of the current phase where feasible.

For example, if service registration fails after binaries are installed, leave the binary in place but revert the partially written unit/plist and say what remains installed.

### 10. Separate binary installation from service installation

Apple and systemd docs both assume services are first-class OS objects, not an afterthought. `launchd` expects a property list with explicit keys like `ProgramArguments`, while `systemd` expects unit files with `[Unit]`, `[Service]`, and `[Install]` semantics. [Apple-Launchd] [Launchd-Plist] [Systemd-Unit] [Systemd-Service]

Practical rule:

- Install the binary first.
- Offer service registration as a second, explicit step.
- Generate native unit/plist files from templates.
- Keep those templates in version control, not inline string concatenation buried in the script.

### 11. Choose the right service scope: user vs system

The OS-native guidance strongly implies that scope matters:

- Apple distinguishes LaunchAgents (per-user) from LaunchDaemons (system-level). [Apple-Launchd]
- `loginctl enable-linger` exists specifically so user-level `systemd --user` services can persist after logout. [Loginctl]
- Docker's rootless mode reinforces the general principle that user-scoped operation is preferable when it satisfies the use case. [Docker-Rootless]

Practical rule:

- Default a developer proxy to a user service, not a system daemon.
- Escalate to machine-level service installation only for explicit shared-host use cases.
- Document what changes when moving from user to system scope: ports, ownership, environment, logs, and auto-start semantics.

### 12. Use native restart and supervision semantics instead of ad hoc backgrounding

Apple recommends making daemons `launchd` compliant and describes launch-on-demand and `KeepAlive` behavior. systemd describes services as supervised processes defined by unit files. [Apple-Launchd] [Systemd-Service]

Practical rule:

- Do not use `nohup`, `disown`, shell ampersands, or stray PID files as the primary service story.
- Use `launchd`/`systemd` restart policies and logging.
- Prefer OS-native log surfaces and status commands.

### 13. Provide uninstall and diagnostics as part of the design

Homebrew and rustup both make uninstall a documented part of the lifecycle, not an afterthought. [HB-Install-Repo] [Rustup-Install]

Practical rule:

- Ship `uninstall` instructions on day one.
- Print where logs live, where config lives, and how to inspect service status.
- End the installer with a deterministic verification checklist:
  - installed binary path
  - version command
  - config path
  - service status command
  - log command

### 14. Keep the installer maintainable like any other production code

The maturity of Homebrew, nvm, and Docker installers is not just in feature breadth; it is also in the fact that these are maintained artifacts with stable docs and repo history. [HB-Install-Repo] [NVM-Readme] [Docker-Install-Repo]

Practical rule:

- Keep business logic out of one monolithic script where possible.
- Centralize OS/arch detection and template rendering.
- Lint shell with ShellCheck and format it consistently.
- Cover at least the decision logic with tests.
- Version installer behavior alongside release artifacts.

## Anti-Patterns

Avoid these:

1. `curl | sh` as the only supported install path.
2. Implicit privilege escalation or broad `sudo` usage before the script has explained why it needs elevation.
3. Blind shell-profile edits with no disclosure, no opt-out, and no shell detection.
4. Assuming `/usr/local`, `python`, or a specific `PATH` layout on macOS.
5. Using shell backgrounding instead of native service managers.
6. Writing service files with relative paths or environment assumptions.
7. Mixing user-owned files and root-owned files in the same install prefix without a clear ownership model.
8. Non-idempotent behavior on rerun.
9. Lack of uninstall or cleanup guidance.
10. Treating a convenience script as a production configuration-management system.

## Tailored Recommendation Checklist For A LiteLLM Proxy Installer

Recommended shape for this installer:

### Installer modes

- Support three entry points:
  - package-manager/manual install docs
  - convenience shell installer
  - service-registration-only command for an already installed binary
- Make the shell installer a thin orchestrator, not the only source of truth.

### Preflight

- Detect and print:
  - OS
  - architecture
  - current user
  - whether running under `sudo`
  - whether `brew`, `uv`, `python3`, `launchctl`, and `systemctl` are present
- Fail with a clear message on unsupported OS/arch pairs.
- Decide user-service vs system-service up front and print that decision.

### Installation layout

- Default to a per-user install for developer machines.
- Keep binary, config, logs, and runtime data in distinct locations.
- Store a small manifest of created files for later upgrade/uninstall.
- Use absolute paths everywhere that service managers will consume.

### Environment handling

- Do not require global shell-profile edits for the proxy to run as a service.
- If CLI convenience commands need PATH changes, make that an explicit optional step.
- Support env-file based configuration for secrets and runtime settings.
- Never print secrets back to the terminal summary.

### Service setup

- On macOS, default to a LaunchAgent for the current user; reserve LaunchDaemon for explicit machine-wide installs. [Apple-Launchd] [Launchd-Plist]
- On Linux, default to a `systemd --user` service where available; if persistence after logout is desired, document or offer `loginctl enable-linger`. [Loginctl]
- Generate native templates with:
  - absolute executable path
  - absolute config/env file paths
  - log destinations or journald/launchd defaults
  - restart policy appropriate for a developer proxy
- After writing units, run the native reload/bootstrap steps and verify status. [Systemctl]

### Safety and rollback

- Implement phases with clear ownership:
  - preflight
  - install files
  - write config
  - register service
  - verify
- If config or service registration fails, remove only the files created in that failed phase.
- Leave successful prior phases intact and report exactly what remains installed.
- Provide a dedicated uninstall path that removes service definitions before deleting binaries/configs you own.

### UX

- Show the plan before executing it.
- Offer `--yes`, `--dry-run`, `--verbose`, and `--uninstall`.
- End with copy-pasteable verification commands:
  - version check
  - service status
  - recent logs
  - config location
- If the installer changed a shell profile, say which file changed and what line was added.
- If the installer did not change PATH, say how to invoke the binary with its absolute path.

### Maintainability

- Keep service templates in tracked files.
- Keep platform detection and file-ownership logic in small functions.
- Lint shell in CI.
- Add test coverage for:
  - OS/arch detection
  - existing-install detection
  - noninteractive mode
  - service template rendering
  - rollback on partial failure

## Sources

- [HB-Homepage] Homebrew homepage. `https://brew.sh/`
- [HB-Install] Homebrew Installation docs. `https://docs.brew.sh/Installation`
- [HB-FAQ] Homebrew FAQ. `https://docs.brew.sh/FAQ`
- [HB-Install-Repo] Homebrew install repository. `https://github.com/Homebrew/install`
- [Rustup-Install] The rustup book: Installation. `https://rust-lang.github.io/rustup/installation/index.html`
- [Rustup-Other] The rustup book: Other installation methods. `https://rust-lang.github.io/rustup/installation/other.html`
- [NVM-Readme] nvm README and installer behavior notes. `https://github.com/nvm-sh/nvm/blob/master/README.md`
- [UV-Install] uv installation docs. `https://docs.astral.sh/uv/getting-started/installation/`
- [UV-Installer] uv installer reference. `https://docs.astral.sh/uv/reference/installer/`
- [Docker-Ubuntu] Docker Engine install docs for Ubuntu. `https://docs.docker.com/engine/install/ubuntu/`
- [Docker-Install-Repo] docker/docker-install README. `https://github.com/docker/docker-install`
- [Docker-Rootless] Docker rootless mode docs. `https://docs.docker.com/engine/security/rootless/`
- [Systemctl] systemctl man page. `https://www.freedesktop.org/software/systemd/man/latest/systemctl.html`
- [Systemd-Unit] systemd.unit man page. `https://freedesktop.org/software/systemd/man/systemd.unit.html`
- [Systemd-Service] systemd.service man page. `https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html`
- [Loginctl] loginctl man page. `https://www.freedesktop.org/software/systemd/man/latest/loginctl.html`
- [Apple-Launchd] Apple, Creating Launch Daemons and Agents. `https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html`
- [Launchd-Plist] `launchd.plist(5)` reference mirror for key semantics such as `ProgramArguments` and `EnvironmentVariables`. `https://www.manpagez.com/man/5/launchd.plist/osx-10.11.6.php`

## Notes On Evidence Strength

- The strongest primary-source guidance here comes from official docs and official repos: Homebrew, uv, rustup, Docker, freedesktop systemd docs, and Apple launchd docs.
- A few implementation details for macOS plist keys are referenced through a man-page mirror because Apple's web docs do not expose the same key-level detail as clearly in searchable page snippets.
- Some recommendations above are explicit in the sources; others are design inferences drawn from repeated patterns across multiple mature OSS installers. Those inferred recommendations are intentionally labeled as practical rules rather than direct quotations.
