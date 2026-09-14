#!/usr/bin/env bash
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.thedawgctor.litellm-proxy.plist"
LABEL="com.thedawgctor.litellm-proxy"

echo "=== [1/4] Verifying Plist XML Syntax ==="
plutil -lint "$PLIST"

echo "=== [2/4] Checking Power Assertions (Sleep Mode Safety) ==="
LITELLM_PID=$(lsof -ti:5596 | head -n1 || true)
if [[ -n "$LITELLM_PID" ]]; then
  ASSERTIONS=$(pmset -g assertions | grep -E "PreventSystemSleep|PreventUserIdleSystemSleep" | grep "$LITELLM_PID" || true)
  if [[ -n "$ASSERTIONS" ]]; then
    echo "ERROR: LiteLLM holds sleep-blocking assertions: $ASSERTIONS"
    exit 1
  fi
  echo "OK: LiteLLM holds 0 sleep-preventing assertions."
fi

echo "=== [3/4] Testing Daemon Revival after SIGTERM (Exit 143) ==="
if [[ -z "$LITELLM_PID" ]]; then
  echo "LiteLLM not currently running on 5596. Bootstrapping..."
  launchctl bootstrap gui/$(id -u) "$PLIST" || true
  sleep 3
  LITELLM_PID=$(lsof -ti:5596 | head -n1)
fi

echo "Sending SIGTERM to PID $LITELLM_PID..."
kill -15 "$LITELLM_PID"

echo "Waiting up to 15 seconds for launchd KeepAlive revival..."
REVIVED=0
for i in {1..15}; do
  sleep 1
  NEW_PID=$(lsof -ti:5596 | head -n1 || true)
  if [[ -n "$NEW_PID" && "$NEW_PID" != "$LITELLM_PID" ]]; then
    echo "OK: Daemon resurrected with new PID $NEW_PID"
    REVIVED=1
    break
  fi
done

if [[ "$REVIVED" -ne 1 ]]; then
  echo "ERROR: launchd failed to revive LiteLLM after SIGTERM exit."
  exit 1
fi

echo "=== [4/4] Verifying HTTP Endpoints on Revived Daemon ==="
/Users/thedawgctor/.litellm/service/bin/smoke-anthropic-to-azure.sh

echo "All tests passed successfully!"
