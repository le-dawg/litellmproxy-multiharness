#!/usr/bin/env bash
# ==============================================================================
# unstoppable_wallet_probe.sh - Credit Replenishment Probe Daemon
# 
# Background daemon that periodically probes the Upstream Canopy Proxy
# to detect credit replenishment when LiteLLM MultiHarness is paused.
#
# Conforms to:
# - Requirements: R5 (Background Hourly Curl Probe Daemon & Notification)
# - Interface Contract 2: Pause Lockfile (~/.litellm/unstoppable_pause.lock)
# - Interface Contract 4: Background Curl Probe Request ("stream": true)
# ==============================================================================

set -uo pipefail

# ------------------------------------------------------------------------------
# Configuration & Defaults
# ------------------------------------------------------------------------------
LITELLM_HOME="${LITELLM_HOME:-$HOME/.litellm}"
PID_FILE="${PROBE_PID_PATH:-${PID_FILE:-$LITELLM_HOME/unstoppable_probe.pid}}"
PAUSE_LOCK_FILE="${UNSTOPPABLE_PAUSE_LOCK_PATH:-${PAUSE_LOCK_FILE:-$LITELLM_HOME/unstoppable_pause.lock}}"
KEYSTORE_FILE="${UNSTOPPABLE_KEYSTORE_PATH:-${KEYSTORE_FILE:-$LITELLM_HOME/unstoppable_wallets.json}}"
UPSTREAM_URL="${UPSTREAM_URL:-${CANOPY_PROXY_URL:-https://app.unstoppable.ai/api/v1/llm-proxy/anthropic/v1/messages}}"
INTERVAL="${PROBE_INTERVAL_SECONDS:-${SLEEP_INTERVAL:-3600}}"
CURL_TIMEOUT="${PROBE_CURL_TIMEOUT:-15}"

RUN_ONCE=0
VERBOSE=0
FORCE=0
CLI_KEY=""

# ------------------------------------------------------------------------------
# Argument Parsing
# ------------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  -i, --interval SECONDS   Polling interval in seconds (default: 3600)
  -1, --once               Execute a single probe iteration and exit
  -p, --pid-file PATH      Path to PID lock file (default: ~/.litellm/unstoppable_probe.pid)
  -l, --lock-file PATH     Path to pause lock file (default: ~/.litellm/unstoppable_pause.lock)
  -k, --keystore PATH      Path to keystore JSON (default: ~/.litellm/unstoppable_wallets.json)
  -u, --url URL            Upstream Canopy endpoint URL
  --key KEY                Explicit API key to use for probe request
  -f, --force              Probe even if pause lockfile is not present
  -v, --verbose            Enable verbose output
  -h, --help               Show this help message
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        -i|--interval)
            INTERVAL="$2"
            shift 2
            ;;
        -1|--once)
            RUN_ONCE=1
            shift
            ;;
        -p|--pid-file)
            PID_FILE="$2"
            shift 2
            ;;
        -l|--lock-file)
            PAUSE_LOCK_FILE="$2"
            shift 2
            ;;
        -k|--keystore)
            KEYSTORE_FILE="$2"
            shift 2
            ;;
        -u|--url)
            UPSTREAM_URL="$2"
            shift 2
            ;;
        --key)
            CLI_KEY="$2"
            shift 2
            ;;
        -f|--force)
            FORCE=1
            shift
            ;;
        -v|--verbose)
            VERBOSE=1
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            ;;
    esac
done

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
log() {
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] [unstoppable-probe] $*" >&2
}

log_verbose() {
    if [ "$VERBOSE" -eq 1 ]; then
        log "$@"
    fi
}

# ------------------------------------------------------------------------------
# PID Lock Management (Stale PID Detection & Clean Exit Trap)
# ------------------------------------------------------------------------------
acquire_pid_lock() {
    mkdir -p "$(dirname "$PID_FILE")"
    if [ -f "$PID_FILE" ]; then
        local existing_pid
        existing_pid=$(cat "$PID_FILE" 2>/dev/null || true)
        if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
            log "Probe daemon already running with PID $existing_pid. Exiting."
            exit 0
        fi
        log "Stale PID file found ($existing_pid is dead). Cleaning up stale lock."
        rm -f "$PID_FILE"
    fi

    echo "$$" > "$PID_FILE"
    if [ ! -f "$PID_FILE" ] || [ "$(cat "$PID_FILE" 2>/dev/null)" != "$$" ]; then
        log "Failed to acquire PID lock at $PID_FILE. Exiting."
        exit 1
    fi
    log_verbose "Acquired PID lock for process $$ at $PID_FILE"
}

cleanup() {
    if [ -f "$PID_FILE" ]; then
        local current_pid
        current_pid=$(cat "$PID_FILE" 2>/dev/null || true)
        if [ "$current_pid" = "$$" ]; then
            rm -f "$PID_FILE"
            log_verbose "Cleaned up PID file $PID_FILE"
        fi
    fi
}

trap cleanup EXIT
trap "exit 0" INT TERM HUP

# ------------------------------------------------------------------------------
# Credential Resolution
# ------------------------------------------------------------------------------
resolve_active_key() {
    # 1. Explicit CLI argument
    if [ -n "${CLI_KEY:-}" ]; then
        echo "$CLI_KEY"
        return 0
    fi

    # 2. Dedicated environment variables
    if [ -n "${CANOPY_API_KEY:-}" ]; then
        echo "$CANOPY_API_KEY"
        return 0
    fi
    if [ -n "${UNSTOPPABLE_PROBE_KEY:-}" ]; then
        echo "$UNSTOPPABLE_PROBE_KEY"
        return 0
    fi

    # 3. Read from keystore JSON
    if [ -r "$KEYSTORE_FILE" ]; then
        local parsed_key
        parsed_key=$(python3 - <<'PY' "$KEYSTORE_FILE" 2>/dev/null || true
import sys, json

try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
    wallets = {w.get("id"): w for w in data.get("wallets", []) if isinstance(w, dict)}
    active_id = data.get("active_wallet_id")
    if active_id and active_id in wallets and wallets[active_id].get("key"):
        print(wallets[active_id]["key"])
        sys.exit(0)
    for w in data.get("wallets", []):
        if isinstance(w, dict) and w.get("key"):
            print(w["key"])
            sys.exit(0)
except Exception:
    pass
sys.exit(1)
PY
        )
        if [ -n "$parsed_key" ]; then
            echo "$parsed_key"
            return 0
        fi
    fi

    # 4. Fallback to UNSTOPPABLE_WALLET_KEYS
    if [ -n "${UNSTOPPABLE_WALLET_KEYS:-}" ]; then
        local first_token
        first_token=$(echo "$UNSTOPPABLE_WALLET_KEYS" | tr ',;\n' ' ' | awk '{print $1}')
        if [ -n "$first_token" ] && [ ${#first_token} -ge 8 ]; then
            echo "$first_token"
            return 0
        fi
    fi

    # 5. Legacy environment variables
    if [ -n "${UNSTOPPABLE_CODE_KEY:-}" ]; then
        echo "$UNSTOPPABLE_CODE_KEY"
        return 0
    fi
    if [ -n "${UNSTOPPABLE_ANTHROPIC_KEY:-}" ]; then
        echo "$UNSTOPPABLE_ANTHROPIC_KEY"
        return 0
    fi

    # 6. Legacy key files
    for kf in "$HOME/.unstoppable-code-anthropic-key" "$LITELLM_HOME/.unstoppable_key"; do
        if [ -r "$kf" ]; then
            local file_val
            file_val=$(head -n 1 "$kf" | tr -d '[:space:]')
            if [ -n "$file_val" ] && [ ${#file_val} -ge 8 ]; then
                echo "$file_val"
                return 0
            fi
        fi
    done

    return 1
}

# ------------------------------------------------------------------------------
# Keystore Wallet Reactivation
# ------------------------------------------------------------------------------
reactivate_keystore_wallets() {
    if [ -f "$KEYSTORE_FILE" ]; then
        python3 - <<'PY' "$KEYSTORE_FILE" 2>/dev/null || true
import sys, json, time

keystore_path = sys.argv[1]
try:
    with open(keystore_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    wallets = data.get("wallets", [])
    active_id = None
    for w in wallets:
        if isinstance(w, dict):
            w["state"] = "active"
            w["last_exhausted"] = None
            if active_id is None and w.get("type") == "personal":
                active_id = w.get("id")

    if active_id is None and wallets:
        active_id = wallets[0].get("id")

    data["active_wallet_id"] = active_id
    data["updated_at"] = time.time()
    data["reason"] = "probe_replenishment_recovery"

    with open(keystore_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
except Exception as exc:
    sys.stderr.write(f"Warning: could not update keystore: {exc}\n")
PY
        log_verbose "Reactivated wallets in keystore: $KEYSTORE_FILE"
    fi
}

# ------------------------------------------------------------------------------
# Probe Request Execution (Interface Contract 4)
# ------------------------------------------------------------------------------
execute_probe() {
    local key="$1"
    local tmp_body
    tmp_body=$(mktemp -t unstoppable_probe_body.XXXXXX)

    log_verbose "Sending probe request to $UPSTREAM_URL with model deepseek-v4-pro (stream: true)"

    local http_code
    http_code=$(curl -s -m "$CURL_TIMEOUT" \
        -o "$tmp_body" \
        -w "%{http_code}" \
        -X POST "$UPSTREAM_URL" \
        -H "Authorization: Bearer $key" \
        -H "anthropic-version: 2023-06-01" \
        -H "Content-Type: application/json" \
        -d '{
            "model": "deepseek-v4-pro",
            "max_tokens": 1,
            "stream": true,
            "messages": [
                {"role": "user", "content": "probe"}
            ]
        }' 2>/dev/null || echo "000")

    local response_snippet=""
    if [ -f "$tmp_body" ]; then
        response_snippet=$(head -c 256 "$tmp_body" 2>/dev/null | tr '\n\r' ' ' || true)
        rm -f "$tmp_body"
    fi

    echo "$http_code|$response_snippet"
}

# ------------------------------------------------------------------------------
# Main Execution Loop
# ------------------------------------------------------------------------------
run_daemon() {
    acquire_pid_lock

    log "Starting credit replenishment probe daemon (PID $$)"
    log "Monitoring upstream: $UPSTREAM_URL (interval: ${INTERVAL}s, timeout: ${CURL_TIMEOUT}s)"

    while true; do
        # If lockfile is absent and not forced, proxy is unpaused; exit cleanly
        if [ "$FORCE" -eq 0 ] && [ ! -f "$PAUSE_LOCK_FILE" ]; then
            log "Pause lockfile $PAUSE_LOCK_FILE absent. MultiHarness is unpaused. Exiting daemon."
            exit 0
        fi

        local key
        key=$(resolve_active_key)
        if [ -z "$key" ]; then
            log "ERROR: No wallet key resolved from keystore or environment. Pausing probe."
            if [ "$RUN_ONCE" -eq 1 ]; then
                exit 1
            fi
            sleep "$INTERVAL"
            continue
        fi

        local probe_result
        probe_result=$(execute_probe "$key")
        local http_code="${probe_result%%|*}"
        local response_snippet="${probe_result#*|}"

        if [ "$http_code" = "200" ]; then
            log "SUCCESS: Credit replenishment confirmed (HTTP 200)!"
            reactivate_keystore_wallets

            if [ -f "$PAUSE_LOCK_FILE" ]; then
                rm -f "$PAUSE_LOCK_FILE"
                log "Removed pause lockfile: $PAUSE_LOCK_FILE"
            fi

            # Dispatch desktop alert with || true to never fail on headless systems
            osascript -e 'display notification "Credit replenishment confirmed. Resuming processing." with title "LiteLLM MultiHarness"' 2>/dev/null || true

            log "Resuming processing. Probe daemon task complete. Exiting."
            exit 0
        else
            log "Probe attempt returned HTTP $http_code (snippet: ${response_snippet:-<none>}). Funds not yet replenished. Retaining pause lock."
            if [ "$RUN_ONCE" -eq 1 ]; then
                exit 1
            fi
        fi

        log_verbose "Sleeping for $INTERVAL seconds before next probe attempt..."
        sleep "$INTERVAL"
    done
}

run_daemon
