#!/bin/sh
set -eu

# Review artifact only.
# This installer is designed for an operator to inspect and run manually later.
# It was not executed automatically during artifact creation.

TIMESTAMP="$("/bin/date" +"%Y%m%d-%H%M%S")"

DEFAULT_LITELLM_HOME="$HOME/.litellm"
DEFAULT_SERVICE_HOME="$DEFAULT_LITELLM_HOME/service"
DEFAULT_CONFIG_PATH="$DEFAULT_LITELLM_HOME/config.yaml"
DEFAULT_WRAPPER_PATH="$DEFAULT_SERVICE_HOME/bin/start-litellm.sh"
DEFAULT_LABEL="com.thedawgctor.litellm-proxy"
DEFAULT_HOST="127.0.0.1"
DEFAULT_PORT="5596"
DEFAULT_LITELLM_VERSION="1.89.0"
DEFAULT_PYTHON_VERSION="3.14"
DEFAULT_LAUNCHAGENTS_DIR="$HOME/Library/LaunchAgents"

say() {
  printf '%s\n' "$*"
}

warn() {
  printf 'WARN: %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

resolve_input_path() {
  case "$1" in
    "~")
      printf '%s\n' "$HOME"
      ;;
    "~/"*)
      printf '%s\n' "$HOME/${1#~/}"
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac
}

prompt_default() {
  prompt_text="$1"
  default_value="$2"
  printf '%s [%s]: ' "$prompt_text" "$default_value" >&2
  IFS= read -r response || exit 1
  if [ -z "$response" ]; then
    printf '%s\n' "$default_value"
  else
    printf '%s\n' "$response"
  fi
}

prompt_yes_no() {
  prompt_text="$1"
  default_value="$2"
  while :; do
    printf '%s [%s]: ' "$prompt_text" "$default_value" >&2
    IFS= read -r response || exit 1
    if [ -z "$response" ]; then
      response="$default_value"
    fi
    case "$response" in
      y|Y|yes|YES)
        return 0
        ;;
      n|N|no|NO)
        return 1
        ;;
      *)
        warn "Please answer y or n."
        ;;
    esac
  done
}

prompt_menu() {
  title="$1"
  default_choice="$2"
  shift 2
  option_count="$#"
  say
  say "$title"
  while [ "$#" -gt 0 ]; do
    say "  $1"
    shift
  done
  while :; do
    printf 'Choice [%s]: ' "$default_choice" >&2
    IFS= read -r response || exit 1
    if [ -z "$response" ]; then
      response="$default_choice"
    fi
    case "$response" in
      ''|*[!0-9]*)
        warn "Choose one of the listed menu numbers."
        ;;
      *)
        if [ "$response" -ge 1 ] && [ "$response" -le "$option_count" ]; then
          printf '%s\n' "$response"
          return 0
        fi
        warn "Choose one of the listed menu numbers."
        ;;
    esac
  done
}

backup_file() {
  target="$1"
  if [ -e "$target" ]; then
    backup_path="$target.$TIMESTAMP.bak"
    /bin/cp -p "$target" "$backup_path"
    say "Backed up $target -> $backup_path"
  fi
}

detect_arch() {
  /usr/bin/uname -m
}

detect_macos_version() {
  if [ -x /usr/bin/sw_vers ]; then
    /usr/bin/sw_vers -productVersion
  else
    printf 'unknown\n'
  fi
}

guess_brew_prefix() {
  arch="$(detect_arch)"
  if [ "$arch" = "arm64" ]; then
    printf '/opt/homebrew\n'
  else
    printf '/usr/local\n'
  fi
}

find_uv_candidate() {
  if command_exists uv; then
    command -v uv
    return 0
  fi

  for candidate in \
    "$(guess_brew_prefix)/bin/uv" \
    /opt/homebrew/bin/uv \
    /usr/local/bin/uv \
    "$HOME/.local/bin/uv" \
    "$HOME/.cargo/bin/uv"
  do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

detect_config_env_vars() {
  config_path="$1"
  if [ ! -f "$config_path" ]; then
    return 0
  fi

  /usr/bin/sed -n 's/.*os\.environ\/\([A-Z0-9_][A-Z0-9_]*\).*/\1/p' "$config_path" | /usr/bin/sort -u
}

check_env_var_visibility() {
  env_name="$1"

  shell_present="no"
  launchd_present="no"

  eval "shell_value=\${$env_name-}"
  if [ -n "${shell_value:-}" ]; then
    shell_present="yes"
  fi

  if /bin/launchctl getenv "$env_name" >/dev/null 2>&1; then
    launchd_value="$(/bin/launchctl getenv "$env_name" 2>/dev/null || true)"
    if [ -n "$launchd_value" ]; then
      launchd_present="yes"
    fi
  fi

  printf '%s|%s|%s\n' "$env_name" "$shell_present" "$launchd_present"
}

ensure_parent_dir() {
  parent_dir="$(dirname "$1")"
  if [ ! -d "$parent_dir" ]; then
    /bin/mkdir -p "$parent_dir"
  fi
}

python_upper_bound() {
  python_version="$1"
  /usr/bin/awk -F. '
    NF == 2 { printf "%s.%d\n", $1, $2 + 1; next }
    NF == 1 { printf "%d\n", $1 + 1; next }
    { exit 1 }
  ' <<EOF
$python_version
EOF
}

write_pyproject() {
  pyproject_path="$1"
  service_name="$2"
  litellm_version="$3"
  python_version="$4"
  python_upper="$(python_upper_bound "$python_version")" || die "Python version must be a major or major.minor value: $python_version"
  ensure_parent_dir "$pyproject_path"
  cat >"$pyproject_path" <<EOF
[project]
name = "$service_name"
version = "0.1.0"
requires-python = ">=$python_version,<$python_upper"
dependencies = [
  "litellm[proxy]==$litellm_version",
]
EOF
}

write_wrapper() {
  wrapper_path="$1"
  litellm_home="$2"
  service_home="$3"
  config_path="$4"
  host="$5"
  port="$6"
  uv_bin="$7"

  ensure_parent_dir "$wrapper_path"
  cat >"$wrapper_path" <<EOF
#!/bin/sh
set -eu

LITELLM_HOME="$litellm_home"
SERVICE_HOME="$service_home"
LOG_HOME="\$SERVICE_HOME/logs"
RUN_HOME="\$SERVICE_HOME/run"
UV_BIN="$uv_bin"

if [ ! -x "\$UV_BIN" ]; then
  printf 'ERROR: uv binary not executable at %s\n' "\$UV_BIN" >&2
  exit 127
fi

if [ ! -f "$config_path" ]; then
  printf 'ERROR: config file not found at %s\n' "$config_path" >&2
  exit 1
fi

/bin/mkdir -p "\$LOG_HOME" "\$RUN_HOME"
cd "\$SERVICE_HOME"

exec "\$UV_BIN" run litellm \\
  --config "$config_path" \\
  --host "$host" \\
  --port "$port"
EOF
  /bin/chmod 0755 "$wrapper_path"
}

write_stub_config() {
  config_path="$1"
  ensure_parent_dir "$config_path"
  cat >"$config_path" <<'EOF'
model_list:
  - model_name: example-model
    litellm_params:
      model: azure/example-model
      api_key: os.environ/AZURE_OPENAI_API_KEY
EOF
}

write_plist() {
  plist_path="$1"
  label="$2"
  wrapper_path="$3"
  service_home="$4"
  stdout_path="$5"
  stderr_path="$6"
  launch_mode="$7"
  delay_seconds="$8"

  ensure_parent_dir "$plist_path"

  if [ "$launch_mode" = "delayed" ]; then
    cat >"$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>$wrapper_path</string>
  </array>

  <key>RunAtLoad</key>
  <false/>

  <key>ThrottleInterval</key>
  <integer>$delay_seconds</integer>

  <key>StartInterval</key>
  <integer>$delay_seconds</integer>

  <key>StandardOutPath</key>
  <string>$stdout_path</string>

  <key>StandardErrorPath</key>
  <string>$stderr_path</string>

  <key>WorkingDirectory</key>
  <string>$service_home</string>
</dict>
</plist>
EOF
  else
    cat >"$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>$wrapper_path</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>$delay_seconds</integer>

  <key>StandardOutPath</key>
  <string>$stdout_path</string>

  <key>StandardErrorPath</key>
  <string>$stderr_path</string>

  <key>WorkingDirectory</key>
  <string>$service_home</string>
</dict>
</plist>
EOF
  fi
}

lint_plist() {
  plist_path="$1"
  if [ -x /usr/bin/plutil ]; then
    /usr/bin/plutil -lint "$plist_path"
  else
    warn "plutil not found; skipped plist validation."
  fi
}

load_launchagent() {
  uid_value="$1"
  plist_path="$2"
  label="$3"

  if /bin/launchctl help 2>&1 | /usr/bin/grep -q "bootstrap"; then
    /bin/launchctl bootout "gui/$uid_value/$label" >/dev/null 2>&1 || true
    /bin/launchctl bootstrap "gui/$uid_value" "$plist_path"
  else
    /bin/launchctl unload "$plist_path" >/dev/null 2>&1 || true
    /bin/launchctl load "$plist_path"
  fi
}

say "LiteLLM proxy installer review artifact"
say "macOS version: $(detect_macos_version)"
say "Architecture: $(detect_arch)"
say
say "Current design defaults:"
say "  home:    $DEFAULT_LITELLM_HOME"
say "  runtime: $DEFAULT_SERVICE_HOME"
say "  config:  $DEFAULT_CONFIG_PATH"
say "  wrapper: $DEFAULT_WRAPPER_PATH"
say "  label:   $DEFAULT_LABEL"
say "  host:    $DEFAULT_HOST"
say "  port:    $DEFAULT_PORT"
say "  version: LiteLLM $DEFAULT_LITELLM_VERSION"
say
warn "The 10-second StartInterval delayed auto-start path is still not fully proven."
warn "This script can write that mode as the default, but it will call out the risk explicitly."

LITELLM_HOME="$(resolve_input_path "$(prompt_default "LiteLLM home" "$DEFAULT_LITELLM_HOME")")"
SERVICE_HOME="$(resolve_input_path "$(prompt_default "Pinned runtime directory" "$DEFAULT_SERVICE_HOME")")"
CONFIG_PATH="$(resolve_input_path "$(prompt_default "Active config path" "$DEFAULT_CONFIG_PATH")")"
WRAPPER_PATH="$(resolve_input_path "$(prompt_default "Wrapper script path" "$DEFAULT_WRAPPER_PATH")")"
LABEL="$(prompt_default "LaunchAgent label" "$DEFAULT_LABEL")"
HOST="$(prompt_default "Bind host" "$DEFAULT_HOST")"
PORT="$(prompt_default "Bind port" "$DEFAULT_PORT")"
LITELLM_VERSION="$(prompt_default "LiteLLM version" "$DEFAULT_LITELLM_VERSION")"
PYTHON_VERSION="$(prompt_default "Preferred Python minor version for uv runtime" "$DEFAULT_PYTHON_VERSION")"
LAUNCHAGENTS_DIR="$(resolve_input_path "$(prompt_default "LaunchAgents directory" "$DEFAULT_LAUNCHAGENTS_DIR")")"
PLIST_PATH="$LAUNCHAGENTS_DIR/$LABEL.plist"
STDOUT_PATH="$SERVICE_HOME/logs/litellm.stdout.log"
STDERR_PATH="$SERVICE_HOME/logs/litellm.stderr.log"
RUN_HOME="$SERVICE_HOME/run"
PYPROJECT_PATH="$SERVICE_HOME/pyproject.toml"
SERVICE_PROJECT_NAME="litellm-proxy-service"
DELAY_SECONDS="10"

if [ "$(id -u)" -eq 0 ]; then
  die "Do not run this installer as root; it is designed for a per-user LaunchAgent."
fi

if [ -e "$SERVICE_HOME" ] && [ "$SERVICE_HOME" != "$LITELLM_HOME/service" ]; then
  warn "Runtime directory differs from the canonical default. Review the wrapper and plist paths carefully."
fi

if [ ! -d "$LAUNCHAGENTS_DIR" ]; then
  warn "LaunchAgents directory does not exist yet: $LAUNCHAGENTS_DIR"
  if prompt_yes_no "Create it now?" "y"; then
    /bin/mkdir -p "$LAUNCHAGENTS_DIR"
  else
    die "LaunchAgents directory is required."
  fi
fi

if [ ! -w "$LAUNCHAGENTS_DIR" ]; then
  die "LaunchAgents directory is not writable: $LAUNCHAGENTS_DIR"
fi

UV_BIN=""
if uv_candidate="$(find_uv_candidate)"; then
  say
  say "Detected uv at: $uv_candidate"
  uv_choice="$(prompt_menu \
    "How should uv be handled?" \
    "1" \
    "1. Use detected uv path" \
    "2. Enter a custom uv path" \
    "3. Reinstall uv with the official installer" \
    "4. Abort")"
  case "$uv_choice" in
    1)
      UV_BIN="$uv_candidate"
      ;;
    2)
      UV_BIN="$(resolve_input_path "$(prompt_default "Custom uv path" "$uv_candidate")")"
      ;;
    3)
      install_dir="$(resolve_input_path "$(prompt_default "uv install directory" "$HOME/.local/bin")")"
      if [ ! -x /usr/bin/curl ]; then
        die "curl is required for the official uv installer path."
      fi
      say "Installing uv into $install_dir via the official installer."
      env UV_INSTALL_DIR="$install_dir" /usr/bin/curl -LsSf https://astral.sh/uv/install.sh | /bin/sh
      UV_BIN="$install_dir/uv"
      ;;
    4)
      die "Aborted before changing the runtime."
      ;;
  esac
else
  warn "uv was not detected in PATH or common install locations."
  uv_choice="$(prompt_menu \
    "How should uv be handled?" \
    "1" \
    "1. Install uv with the official installer" \
    "2. Enter a custom uv path" \
    "3. Abort")"
  case "$uv_choice" in
    1)
      install_dir="$(resolve_input_path "$(prompt_default "uv install directory" "$HOME/.local/bin")")"
      if [ ! -x /usr/bin/curl ]; then
        die "curl is required for the official uv installer path."
      fi
      say "Installing uv into $install_dir via the official installer."
      env UV_INSTALL_DIR="$install_dir" /usr/bin/curl -LsSf https://astral.sh/uv/install.sh | /bin/sh
      UV_BIN="$install_dir/uv"
      ;;
    2)
      UV_BIN="$(resolve_input_path "$(prompt_default "Custom uv path" "$HOME/.local/bin/uv")")"
      ;;
    3)
      die "Aborted because no uv strategy was selected."
      ;;
  esac
fi

[ -n "$UV_BIN" ] || die "uv path was not resolved."
[ -x "$UV_BIN" ] || die "Resolved uv path is not executable: $UV_BIN"

if ! "$UV_BIN" python find "$PYTHON_VERSION" >/dev/null 2>&1; then
  warn "uv does not currently report a ready Python $PYTHON_VERSION runtime."
  if prompt_yes_no "Install Python $PYTHON_VERSION with uv now?" "y"; then
    "$UV_BIN" python install "$PYTHON_VERSION"
  else
    warn "Continuing without installing Python $PYTHON_VERSION. uv sync may fail later."
  fi
fi

/bin/mkdir -p "$SERVICE_HOME/bin" "$SERVICE_HOME/logs" "$RUN_HOME"

if [ -f "$CONFIG_PATH" ]; then
  say "Using existing config: $CONFIG_PATH"
else
  warn "Config file is missing: $CONFIG_PATH"
  if prompt_yes_no "Create a stub config file with an AZURE_OPENAI_API_KEY placeholder?" "n"; then
    write_stub_config "$CONFIG_PATH"
    say "Created stub config at $CONFIG_PATH"
  else
    die "Config file is required."
  fi
fi

ENV_NAMES="$(detect_config_env_vars "$CONFIG_PATH" || true)"
if [ -n "$ENV_NAMES" ]; then
  say
  say "Config references these environment variables:"
  for env_name in $ENV_NAMES; do
    status_line="$(check_env_var_visibility "$env_name")"
    old_ifs="$IFS"
    IFS='|'
    set -- $status_line
    IFS="$old_ifs"
    current_name="$1"
    shell_status="$2"
    launchd_status="$3"
    say "  $current_name -> shell:$shell_status launchd:$launchd_status"
  done

  missing_any="no"
  for env_name in $ENV_NAMES; do
    status_line="$(check_env_var_visibility "$env_name")"
    old_ifs="$IFS"
    IFS='|'
    set -- $status_line
    IFS="$old_ifs"
    shell_status="$2"
    launchd_status="$3"
    if [ "$shell_status" = "no" ] && [ "$launchd_status" = "no" ]; then
      warn "Neither the current shell nor launchd exposes $env_name."
      missing_any="yes"
    fi
  done

  if [ "${missing_any:-no}" = "yes" ]; then
    warn "The proxy may fail immediately under launchd until the missing variables are provided."
    say "Typical fix: export the variables into the user launchd environment before relying on the LaunchAgent."
    if ! prompt_yes_no "Continue anyway?" "n"; then
      die "Aborted due to missing required environment variables."
    fi
  fi
fi

if /bin/launchctl print "gui/$(/usr/bin/id -u)/$LABEL" >/dev/null 2>&1; then
  warn "A launchd job with label $LABEL is already loaded."
  if ! prompt_yes_no "Proceed and plan to reload that label?" "y"; then
    die "Aborted because the LaunchAgent label is already in use."
  fi
fi

if [ -e "$PLIST_PATH" ]; then
  warn "A plist already exists at $PLIST_PATH"
  if prompt_yes_no "Back it up before overwriting?" "y"; then
    backup_file "$PLIST_PATH"
  fi
fi

if [ -e "$WRAPPER_PATH" ]; then
  warn "A wrapper script already exists at $WRAPPER_PATH"
  if prompt_yes_no "Back it up before overwriting?" "y"; then
    backup_file "$WRAPPER_PATH"
  fi
fi

if [ -e "$PYPROJECT_PATH" ]; then
  warn "A pyproject already exists at $PYPROJECT_PATH"
  if prompt_yes_no "Back it up before overwriting?" "y"; then
    backup_file "$PYPROJECT_PATH"
  fi
fi

launch_mode_choice="$(prompt_menu \
  "Choose launch behavior." \
  "1" \
  "1. Delayed auto-start via StartInterval (default, not fully proven yet)" \
  "2. Immediate start with RunAtLoad + KeepAlive" \
  "3. Abort")"
case "$launch_mode_choice" in
  1)
    LAUNCH_MODE="delayed"
    ;;
  2)
    LAUNCH_MODE="immediate"
    ;;
  3)
    die "Aborted before writing the LaunchAgent."
    ;;
esac

write_pyproject "$PYPROJECT_PATH" "$SERVICE_PROJECT_NAME" "$LITELLM_VERSION" "$PYTHON_VERSION"
write_wrapper "$WRAPPER_PATH" "$LITELLM_HOME" "$SERVICE_HOME" "$CONFIG_PATH" "$HOST" "$PORT" "$UV_BIN"

(
  cd "$SERVICE_HOME"
  "$UV_BIN" lock
  "$UV_BIN" sync
)

write_plist "$PLIST_PATH" "$LABEL" "$WRAPPER_PATH" "$SERVICE_HOME" "$STDOUT_PATH" "$STDERR_PATH" "$LAUNCH_MODE" "$DELAY_SECONDS"
lint_plist "$PLIST_PATH"

if prompt_yes_no "Reload the LaunchAgent now?" "n"; then
  load_launchagent "$(/usr/bin/id -u)" "$PLIST_PATH" "$LABEL"
  say "LaunchAgent reloaded."
  if [ "$LAUNCH_MODE" = "delayed" ]; then
    warn "The delayed StartInterval path is still not fully proven. Bootstrap alone may leave runs=0."
  fi
else
  say "Skipped LaunchAgent reload."
fi

say
say "Installation artifacts prepared:"
say "  uv:        $UV_BIN"
say "  pyproject: $PYPROJECT_PATH"
say "  wrapper:   $WRAPPER_PATH"
say "  plist:     $PLIST_PATH"
say "  stdout:    $STDOUT_PATH"
say "  stderr:    $STDERR_PATH"
say
say "Recommended verification commands:"
say "  /bin/sh -n \"$WRAPPER_PATH\""
say "  /usr/bin/plutil -lint \"$PLIST_PATH\""
say "  /bin/launchctl print \"gui/\$(/usr/bin/id -u)/$LABEL\" | /usr/bin/sed -n '1,160p'"
say "  /usr/sbin/lsof -nP -iTCP:$PORT -sTCP:LISTEN"
say "  /usr/bin/curl -sS http://$HOST:$PORT/models"
say
if [ "$LAUNCH_MODE" = "delayed" ]; then
  warn "Chosen mode: delayed StartInterval startup. This remains the default design target, but its post-login behavior is not yet fully proven."
else
  say "Chosen mode: immediate RunAtLoad + KeepAlive."
fi
