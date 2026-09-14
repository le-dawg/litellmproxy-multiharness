#!/bin/sh
set -eu

LITELLM_HOME="${LITELLM_HOME:-$HOME/.litellm}"
SERVICE_HOME="${SERVICE_HOME:-$LITELLM_HOME/service}"
ENV_FILE="${LITELLM_ENV_FILE:-$SERVICE_HOME/.env}"
CONFIG_FILE="${LITELLM_CONFIG_FILE:-$LITELLM_HOME/config.yaml}"
PYTHON_BIN="${LITELLM_PYTHON_BIN:-$SERVICE_HOME/.venv/bin/python}"

if [ ! -r "$ENV_FILE" ]; then
  echo "litellm-preflight: missing readable env file at $ENV_FILE" >&2
  exit 1
fi

unset AZURE_OPENAI_API_KEY

set -a
. "$ENV_FILE"
set +a

if [ -z "${AZURE_OPENAI_API_KEY:-}" ]; then
  echo "litellm-preflight: AZURE_OPENAI_API_KEY missing or blank in $ENV_FILE" >&2
  exit 1
fi

if [ ! -r "$CONFIG_FILE" ]; then
  echo "litellm-preflight: missing readable config at $CONFIG_FILE" >&2
  exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "litellm-preflight: missing python runtime at $PYTHON_BIN" >&2
  exit 1
fi

EXPECTED_BASE="https://regent-ai-dev.cognitiveservices.azure.com"
EXPECTED_VERSION="2025-04-01-preview"
EXPECTED_MODEL="azure/gpt-5.4"
EXPECTED_KEY_REF="os.environ/AZURE_OPENAI_API_KEY"

"$PYTHON_BIN" - <<'PY' "$CONFIG_FILE" "$EXPECTED_BASE" "$EXPECTED_VERSION" "$EXPECTED_MODEL" "$EXPECTED_KEY_REF"
import sys
import yaml

config_path, expected_base, expected_version, expected_model, expected_key_ref = sys.argv[1:6]

try:
    with open(config_path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
except yaml.YAMLError as exc:
    print(f"litellm-preflight: config is not valid YAML: {exc}", file=sys.stderr)
    raise SystemExit(1)

errors = []

litellm_settings = config.get("litellm_settings") or {}
if litellm_settings.get("drop_params") is not True:
    errors.append("litellm_settings.drop_params must be true")

required_aliases = {"claude-gpt-5", "claude-sonnet-4-5"}
models = {}
for item in config.get("model_list") or []:
    name = item.get("model_name")
    if name:
        models[name] = item.get("litellm_params") or {}

missing = sorted(required_aliases - models.keys())
if missing:
    errors.append("missing required model aliases: " + ", ".join(missing))

for alias in sorted(required_aliases & models.keys()):
    params = models[alias]
    if params.get("model") != expected_model:
        errors.append(f"{alias}: model must be {expected_model}")
    if params.get("api_base") != expected_base:
        errors.append(f"{alias}: api_base must be {expected_base}")
    if str(params.get("api_version")) != expected_version:
        errors.append(f"{alias}: api_version must be {expected_version}")
    if params.get("api_key") != expected_key_ref:
        errors.append(f"{alias}: api_key must be {expected_key_ref}")

if errors:
    for error in errors:
        print(f"litellm-preflight: {error}", file=sys.stderr)
    raise SystemExit(1)

print("litellm-preflight: credential source and translation contract validated", file=sys.stderr)
PY
