#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request


APP_PATH = "/Applications/Antigravity.app"
LS_PATH = f"{APP_PATH}/Contents/Resources/bin/language_server"
HOST = os.environ.get("ANTIGRAVITY_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ANTIGRAVITY_BRIDGE_PORT", "8788"))
HOME = Path.home()
STATE_DIR = HOME / ".litellm"
STATE_DIR.mkdir(parents=True, exist_ok=True)
TRAFFIC_LOG = STATE_DIR / "antigravity-traffic.jsonl"
SERVICE_LOG = STATE_DIR / "antigravity-bridge.log"


def log_service(message):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    with SERVICE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def log_traffic(kind, payload):
    event = {
        "ts": time.time(),
        "kind": kind,
        **payload,
    }
    with TRAFFIC_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def flatten_content(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("input_text") or item.get("output_text") or ""
                if text:
                    parts.append(str(text))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return value.get("text") or value.get("content") or ""
    return str(value)


def messages_to_prompt(messages):
    if not isinstance(messages, list):
        return flatten_content(messages)
    if len(messages) == 1 and isinstance(messages[0], dict):
        return flatten_content(messages[0].get("content"))
    parts = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = (message.get("role") or "user").upper()
        content = flatten_content(message.get("content"))
        if content:
            parts.append(f"{role}: {content}")
    return "\n\n".join(parts)


def discover_ls():
    result = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True, check=False)
    for line in result.stdout.splitlines():
        if LS_PATH not in line:
            continue
        try:
            pid_text, command = line.strip().split(" ", 1)
            pid = int(pid_text)
        except ValueError:
            continue
        match = re.search(r"--csrf_token\s+([^\s]+)", command)
        if not match:
            continue
        csrf_token = match.group(1)
        ports = discover_listen_ports(pid)
        for port in ports:
            if port_ready(port):
                return {
                    "pid": pid,
                    "address": f"127.0.0.1:{port}",
                    "csrf_token": csrf_token,
                }
    return None


def discover_listen_ports(pid):
    result = subprocess.run(
        ["lsof", "-Pan", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    ports = []
    for line in result.stdout.splitlines()[1:]:
        match = re.search(r"127\.0\.0\.1:(\d+)\s*\(LISTEN\)", line)
        if match:
            ports.append(int(match.group(1)))
    return ports


def port_ready(port):
    try:
        with request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def ensure_ls(timeout_seconds=30):
    info = discover_ls()
    if info:
        return info
    subprocess.run(["open", "-a", APP_PATH], check=False)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        info = discover_ls()
        if info:
            return info
        time.sleep(1)
    raise RuntimeError("Antigravity language server did not become ready")


def rpc(method, payload):
    info = ensure_ls()
    body = json.dumps(payload).encode("utf-8")
    rpc_request = request.Request(
        f"http://{info['address']}/exa.language_server_pb.LanguageServerService/{method}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": info["csrf_token"],
        },
    )
    with request.urlopen(rpc_request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def load_model_map():
    catalog = rpc("GetCascadeModelConfigData", {})
    configs = catalog.get("clientModelConfigs") or []
    default_model = (((catalog.get("defaultOverrideModelConfig") or {}).get("modelOrAlias") or {}).get("model"))

    def choose(predicate):
        ranked = []
        for config in configs:
            label = (config.get("label") or "").lower()
            model_id = (config.get("modelId") or "").lower()
            if not predicate(label, model_id, config):
                continue
            ranked.append((
                1 if config.get("isRecommended") else 0,
                1 if "high" in label or "high" in model_id else 0,
                1 if "medium" in label or "medium" in model_id else 0,
                label,
                config,
            ))
        if not ranked:
            return None
        ranked.sort(reverse=True)
        chosen = ranked[0][-1]
        return (((chosen.get("modelOrAlias") or {}).get("model")) or default_model)

    flash_model = default_model or choose(lambda label, model_id, _cfg: "flash" in label or "flash" in model_id)
    pro_model = choose(lambda label, model_id, _cfg: any(token in label or token in model_id for token in ["opus", "pro", "sonnet"])) or flash_model
    opus_model = choose(lambda label, model_id, _cfg: "opus" in label or "opus" in model_id) or pro_model
    sonnet_model = choose(lambda label, model_id, _cfg: "sonnet" in label or "sonnet" in model_id) or pro_model
    gemini_model = choose(lambda label, model_id, _cfg: "gemini" in label or "gemini" in model_id) or flash_model

    model_map = {
        "antigravity-flash": flash_model,
        "antigravity-pro": pro_model,
        "antigravity-gemini": gemini_model,
        "antigravity-opus": opus_model,
        "antigravity-sonnet": sonnet_model,
        "claude-opus-5.5": opus_model,
        "claude-sonnet-5.5": sonnet_model,
        "gemini": gemini_model,
    }

    # Dynamically register all model configurations from the active Language Server catalog
    for config in configs:
        model_id = config.get("modelId")
        target = ((config.get("modelOrAlias") or {}).get("model"))
        if model_id and target:
            model_map[model_id] = target
            model_map[model_id.lower()] = target

    return model_map


def resolve_model_name(name):
    requested = (name or "antigravity-flash").replace("openai/", "")
    if requested == "antigravity":
        requested = "antigravity-flash"
    model_map = load_model_map()
    model = model_map.get(requested) or model_map.get(requested.lower())
    if not model:
        raise KeyError(f"Unsupported antigravity model: {requested}")
    return requested, model


class Handler(BaseHTTPRequestHandler):
    server_version = "antigravity-bridge/0.1"

    def do_GET(self):
        if self.path in {"/health", "/healthz", "/v1/health"}:
            info = ensure_ls()
            self.respond_json(200, {"status": "ok", "ls_address": info["address"]})
            return
        if self.path == "/v1/models":
            model_map = load_model_map()
            now = int(time.time())
            model_items = [
                {"id": m_id, "object": "model", "created": now, "owned_by": "antigravity"}
                for m_id in sorted(model_map.keys())
            ]
            self.respond_json(200, {"data": model_items})
            return
        self.respond_json(404, {"error": {"message": f"Unsupported path: {self.path}"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.respond_json(404, {"error": {"message": f"Unsupported path: {self.path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            request_id = f"ag-{uuid.uuid4().hex}"
            prompt = messages_to_prompt(payload.get("messages") or [])
            requested_model, internal_model = resolve_model_name(payload.get("model"))
            log_traffic(
                "incoming_request",
                {
                    "request_id": request_id,
                    "path": self.path,
                    "requested_model": requested_model,
                    "prompt": prompt,
                },
            )
            upstream_payload = {"prompt": prompt, "model": internal_model}
            log_traffic(
                "upstream_request",
                {
                    "request_id": request_id,
                    "method": "GetModelResponse",
                    "payload": upstream_payload,
                },
            )
            upstream_response = rpc("GetModelResponse", upstream_payload)
            content = upstream_response.get("response", "")
            log_traffic(
                "upstream_response",
                {
                    "request_id": request_id,
                    "status": 200,
                    "body": upstream_response,
                },
            )
            is_stream = bool(payload.get("stream"))
            cmpl_id = f"chatcmpl-{uuid.uuid4().hex}"
            created_ts = int(time.time())

            if is_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                chunk1 = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": requested_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": content},
                            "finish_reason": None,
                        }
                    ],
                }
                chunk2 = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": requested_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                }
                self.wfile.write(f"data: {json.dumps(chunk1)}\n\n".encode("utf-8"))
                self.wfile.write(f"data: {json.dumps(chunk2)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            response_body = {
                "id": cmpl_id,
                "object": "chat.completion",
                "created": created_ts,
                "model": requested_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            log_traffic(
                "outgoing_response",
                {
                    "request_id": request_id,
                    "status": 200,
                    "body": response_body,
                },
            )
            self.respond_json(200, response_body)
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            self.respond_json(exc.code, {"error": {"message": body}})
        except Exception as exc:
            log_service(f"bridge error: {exc}")
            self.respond_json(500, {"error": {"message": str(exc)}})

    def log_message(self, *_args):
        return

    def respond_json(self, status_code, body):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    log_service(f"starting antigravity bridge on {HOST}:{PORT}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
