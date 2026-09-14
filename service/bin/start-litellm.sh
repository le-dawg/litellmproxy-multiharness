#!/bin/sh
set -eu

LITELLM_HOME="${LITELLM_HOME:-$HOME/.litellm}"
SERVICE_HOME="${SERVICE_HOME:-$LITELLM_HOME/service}"
LOG_HOME="$SERVICE_HOME/logs"
RUN_HOME="$SERVICE_HOME/run"
ENV_FILE="${LITELLM_ENV_FILE:-$SERVICE_HOME/.env}"
CONFIG_FILE="${LITELLM_CONFIG_FILE:-$LITELLM_HOME/config.yaml}"
PREFLIGHT_SCRIPT="$SERVICE_HOME/bin/preflight-litellm.sh"

mkdir -p "$LOG_HOME" "$RUN_HOME"

if [ ! -r "$ENV_FILE" ]; then
  echo "litellm-service: missing readable env file at $ENV_FILE" >&2
  exit 1
fi

unset AZURE_OPENAI_API_KEY

set -a
. "$ENV_FILE"
set +a

if [ -z "${AZURE_OPENAI_API_KEY:-}" ]; then
  echo "litellm-service: AZURE_OPENAI_API_KEY missing or blank in $ENV_FILE" >&2
  exit 1
fi

export AZURE_OPENAI_API_KEY

# The default aiohttp transport has been triggering truncated chunked
# responses on /v1/responses for this Azure route. Force the safer httpx
# transport unless the environment explicitly overrides it.
export DISABLE_AIOHTTP_TRANSPORT="${DISABLE_AIOHTTP_TRANSPORT:-true}"

if [ ! -x "$PREFLIGHT_SCRIPT" ]; then
  echo "litellm-service: missing executable preflight script at $PREFLIGHT_SCRIPT" >&2
  exit 1
fi

"$PREFLIGHT_SCRIPT"

echo "litellm-service: loaded Azure credential from dedicated env file $ENV_FILE" >&2

cd "$SERVICE_HOME"

exec /opt/homebrew/bin/uv run litellm \
  --config "$CONFIG_FILE" \
  --host 127.0.0.1 \
  --port 5596
