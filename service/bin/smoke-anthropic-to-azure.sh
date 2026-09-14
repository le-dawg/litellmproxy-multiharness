#!/bin/sh
set -eu

HOST="${LITELLM_HOST:-127.0.0.1}"
PORT="${LITELLM_PORT:-5596}"
MODEL="claude-gpt-5"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --model)
      if [ "$#" -lt 2 ]; then
        echo "smoke-anthropic-to-azure: --model requires a value" >&2
        exit 1
      fi
      MODEL="$2"
      shift 2
      ;;
    *)
      echo "smoke-anthropic-to-azure: unknown argument $1" >&2
      exit 1
      ;;
  esac
done

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT INT TERM

body_file="$tmpdir/body.json"
status="$(
  curl -sS \
    -o "$body_file" \
    -w '%{http_code}' \
    "http://$HOST:$PORT/v1/messages" \
    -H 'content-type: application/json' \
    -d "{
      \"model\": \"$MODEL\",
      \"max_tokens\": 32,
      \"messages\": [
        {\"role\": \"user\", \"content\": \"Reply with exactly: proxy ok\"}
      ]
    }"
)"

if [ "$status" != "200" ]; then
  echo "smoke-anthropic-to-azure: expected HTTP 200 for $MODEL but got $status" >&2
  cat "$body_file" >&2
  exit 1
fi

python3 - <<'PY' "$body_file" "$MODEL"
import json
import sys

body_path, model = sys.argv[1:3]

with open(body_path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)

content = payload.get("content") or []
texts = []
for item in content:
    if isinstance(item, dict) and item.get("type") == "text":
        texts.append(item.get("text", ""))

joined = " ".join(texts).strip()
if joined != "proxy ok":
    print(f"smoke-anthropic-to-azure: unexpected response for {model}: {joined!r}", file=sys.stderr)
    raise SystemExit(1)

print(f"smoke-anthropic-to-azure: {model} translation path ok")
PY
