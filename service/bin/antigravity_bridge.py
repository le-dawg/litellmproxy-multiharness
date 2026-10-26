#!/usr/bin/env python3
import json
import os
import re
import socket
import subprocess
import time
import uuid
from http import client as http_client
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

BUDDY_REGEX = re.compile(r"<!--\s*buddy:.*?-->", re.DOTALL | re.IGNORECASE)
CAVEMAN_BLOCK_REGEX = re.compile(
    r"(?:<system-reminder>\s*)?(?:UserPromptSubmit hook additional context:\s*)?CAVEMAN MODE ACTIVE.*?(?:</system-reminder>|(?=\n\n)|$)",
    re.DOTALL | re.IGNORECASE,
)
TOOL_CALL_REGEX = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)


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
        return str(value.get("text") or value.get("content") or "")
    return str(value)


def format_tool_instructions(tools, tool_choice="auto"):
    if not tools:
        return ""
    tool_lines = []
    for tool in tools:
        func = tool.get("function") or tool
        name = func.get("name")
        desc = func.get("description", "")
        params = func.get("parameters", {})
        tool_lines.append(
            f"- Tool: `{name}`\n"
            f"  Description: {desc}\n"
            f"  Parameters Schema: {json.dumps(params, ensure_ascii=False)}"
        )
    tools_str = "\n".join(tool_lines)

    instruction = (
        "# Available Tools\n"
        "You have access to the following tools:\n"
        f"{tools_str}\n\n"
        "# Tool Calling Instructions\n"
        "To invoke a tool, output a tool call block using the exact demarcation tags:\n"
        "<tool_call>\n"
        "{\"name\": \"TOOL_NAME\", \"arguments\": { ... }}\n"
        "</tool_call>\n\n"
        "Rules:\n"
        "1. The arguments must strictly conform to the parameter JSON schema.\n"
        "2. Do NOT wrap <tool_call> in markdown code blocks like ```json or ```xml.\n"
        "3. You may provide explanatory text before or after the tool call.\n"
        "4. You may make multiple tool calls by outputting multiple consecutive <tool_call>...</tool_call> blocks.\n"
        "5. If no tool call is needed, respond with normal conversational text."
    )

    if tool_choice == "required":
        instruction += "\nCRITICAL: You MUST call at least one tool in this turn."
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        req_name = tool_choice.get("function", {}).get("name")
        if req_name:
            instruction += f"\nCRITICAL: You MUST invoke the `{req_name}` tool in this turn."
    elif tool_choice == "none":
        instruction += "\nCRITICAL: Do NOT call any tools in this turn. Respond with text only."

    return instruction


def extract_balanced_json(text: str, start_idx: int):
    """Extract a balanced JSON { ... } or [ ... ] chunk starting at start_idx.
    Properly handles quotes and escape characters.
    Returns (matched_str, end_idx) or None if unbalanced.
    """
    if start_idx >= len(text):
        return None
    open_char = text[start_idx]
    if open_char == "{":
        close_char = "}"
    elif open_char == "[":
        close_char = "]"
    else:
        return None

    depth = 0
    in_quote = None
    escape = False

    for i in range(start_idx, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if in_quote:
            if ch == in_quote:
                in_quote = None
            continue
        else:
            if ch in ('"', "'"):
                in_quote = ch
            elif ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    return text[start_idx : i + 1], i + 1
    return None


def _extract_single_call(item):
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if not name and isinstance(item.get("function"), dict):
        name = item["function"].get("name")
        args = item["function"].get("arguments", {})
    else:
        args = item.get("arguments", item.get("parameters", {}))

    if not name or not isinstance(name, str):
        return None

    if isinstance(args, (dict, list)):
        args_str = json.dumps(args, ensure_ascii=False)
    elif isinstance(args, str):
        args_str = args
    else:
        args_str = "{}"

    call_id = f"call_{uuid.uuid4().hex[:24]}"
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": args_str,
        },
    }


def _fallback_parse_block(raw_block: str):
    name_m = re.search(r'["\']name["\']\s*:\s*["\']([^"\']+)["\']', raw_block)
    if not name_m:
        fn_m = re.search(r'["\']function["\']\s*:\s*["\']?([^"\'\s,}]+)', raw_block)
        if not fn_m:
            return None
        name = fn_m.group(1)
    else:
        name = name_m.group(1)

    parsed = {"name": name, "arguments": {}}
    args_m = re.search(r'["\'](?:arguments|parameters)["\']\s*:\s*', raw_block)
    if args_m:
        rest = raw_block[args_m.end():].lstrip()
        if rest:
            if rest[0] in ("{", "["):
                res = extract_balanced_json(rest, 0)
                if res:
                    chunk, _ = res
                    try:
                        parsed["arguments"] = json.loads(chunk)
                    except Exception:
                        parsed["arguments"] = chunk
            elif rest[0] in ('"', "'"):
                inner = rest[1:].lstrip()
                if inner and inner[0] in ("{", "["):
                    res = extract_balanced_json(inner, 0)
                    if res:
                        chunk, _ = res
                        try:
                            parsed["arguments"] = json.loads(chunk)
                        except Exception:
                            parsed["arguments"] = chunk
                else:
                    str_m = re.search(r'^([^"\'\\]*(?:\\.[^"\'\\]*)*)["\']', rest[1:], re.DOTALL)
                    if str_m:
                        parsed["arguments"] = str_m.group(1)
                    else:
                        parsed["arguments"] = rest[1:]
    return parsed


def _extract_calls_from_inner(raw_inner: str):
    if raw_inner.startswith("```"):
        raw_inner = re.sub(r"^```(?:json|xml)?\s*", "", raw_inner)
        raw_inner = re.sub(r"\s*```$", "", raw_inner)
    raw_inner = raw_inner.strip()
    if not raw_inner:
        return []

    calls = []

    # 1. Direct json.loads
    try:
        parsed = json.loads(raw_inner)
        if isinstance(parsed, dict):
            c = _extract_single_call(parsed)
            if c:
                return [c]
        elif isinstance(parsed, list):
            for item in parsed:
                c = _extract_single_call(item)
                if c:
                    calls.append(c)
            if calls:
                return calls
    except Exception:
        pass

    # 2. raw_decode loop
    decoder = json.JSONDecoder()
    idx = 0
    length = len(raw_inner)
    items = []
    has_parse_error = False

    while idx < length:
        while idx < length and raw_inner[idx] in " \t\r\n,":
            idx += 1
        if idx >= length:
            break
        try:
            obj, end = decoder.raw_decode(raw_inner, idx)
            items.append(obj)
            idx = end
        except json.JSONDecodeError:
            has_parse_error = True
            break

    while idx < length and raw_inner[idx] in " \t\r\n,":
        idx += 1
    if not has_parse_error and idx >= length and items:
        for item in items:
            if isinstance(item, list):
                for sub in item:
                    c = _extract_single_call(sub)
                    if c:
                        calls.append(c)
            elif isinstance(item, dict):
                c = _extract_single_call(item)
                if c:
                    calls.append(c)
        if calls:
            return calls

    # 3. Scanner using balanced braces
    calls.clear()
    idx = 0
    while idx < length:
        while idx < length and raw_inner[idx] in " \t\r\n,":
            idx += 1
        if idx >= length:
            break

        if raw_inner[idx] in ("{", "["):
            res = extract_balanced_json(raw_inner, idx)
            if res:
                block, end_idx = res
                obj = None
                try:
                    obj = json.loads(block)
                except Exception:
                    obj = _fallback_parse_block(block)

                if isinstance(obj, list):
                    for sub in obj:
                        c = _extract_single_call(sub)
                        if c:
                            calls.append(c)
                elif isinstance(obj, dict):
                    c = _extract_single_call(obj)
                    if c:
                        calls.append(c)
                idx = end_idx
                continue

        idx += 1

    if calls:
        return calls

    # 4. Ultimate fallback
    fallback_obj = _fallback_parse_block(raw_inner)
    if fallback_obj:
        c = _extract_single_call(fallback_obj)
        if c:
            return [c]

    return []


def parse_model_response(raw_text: str):
    raw_text = str(raw_text or "")
    if not raw_text:
        return "", []
    tool_calls = []

    def replacer(match):
        raw_inner = match.group(1).strip()
        extracted = _extract_calls_from_inner(raw_inner)
        if extracted:
            tool_calls.extend(extracted)
            return ""
        return match.group(0)

    remaining_text = TOOL_CALL_REGEX.sub(replacer, raw_text).strip()
    remaining_text = re.sub(r"```(?:xml|json)?\s*```", "", remaining_text).strip()
    return remaining_text, tool_calls


def messages_to_prompt(messages, tools=None, tool_choice="auto"):
    if not isinstance(messages, list):
        if isinstance(messages, dict):
            messages = [messages]
        elif isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        else:
            messages = []

    # Map call_id -> tool_name across conversation
    call_id_to_name = {}
    for msg in messages:
        if isinstance(msg, dict) and msg.get("tool_calls"):
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tc_id = tc.get("id")
                    fn = tc.get("function") or {}
                    fn_name = fn.get("name")
                    if tc_id and fn_name:
                        call_id_to_name[tc_id] = fn_name

    # 1. Identify index of last CAVEMAN reminder across all messages
    last_caveman_idx = -1
    for idx, msg in enumerate(messages):
        if isinstance(msg, dict):
            raw = flatten_content(msg.get("content"))
            if "CAVEMAN MODE ACTIVE" in raw:
                last_caveman_idx = idx

    # 2. Sanitize and prepare cleaned message entries
    cleaned_messages = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").lower()
        content = str(flatten_content(msg.get("content")) or "")

        # Strip buddy tags
        content = BUDDY_REGEX.sub("", content).strip()

        # Deduplicate caveman reminder: omit or clean from earlier turns
        if "CAVEMAN MODE ACTIVE" in content and idx != last_caveman_idx:
            content = CAVEMAN_BLOCK_REGEX.sub("", content).strip()

        # Format assistant tool calls if present
        tool_calls = msg.get("tool_calls") or []
        tc_blocks = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            fn_name = fn.get("name", "")
            fn_args = fn.get("arguments", "{}")
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except Exception:
                    pass
            tc_blocks.append(
                f"<tool_call>\n{json.dumps({'name': fn_name, 'arguments': fn_args}, ensure_ascii=False)}\n</tool_call>"
            )

        if tc_blocks:
            if content:
                content = content + "\n" + "\n".join(tc_blocks)
            else:
                content = "\n".join(tc_blocks)

        # Format tool result role
        if role in ("tool", "function"):
            call_id = msg.get("tool_call_id") or msg.get("id") or ""
            tool_name = msg.get("name") or call_id_to_name.get(call_id, "")
            tag = f"TOOL RESULT ({tool_name})" if tool_name else (f"TOOL RESULT ({call_id})" if call_id else "TOOL RESULT")
            role = tag

        # Retain if content is non-empty
        if content:
            cleaned_messages.append({"role": role, "content": content})

    # 3. Tool instruction formatting
    tool_inst = format_tool_instructions(tools, tool_choice) if tools else ""

    # 4. Reorder trailing system hooks so they precede the active user turn
    last_user_idx = -1
    for i in range(len(cleaned_messages) - 1, -1, -1):
        if cleaned_messages[i]["role"] == "user":
            last_user_idx = i
            break

    if last_user_idx == -1:
        # No user message exists in the conversation at all
        cleaned_messages.append({"role": "user", "content": "Please proceed with the above instructions."})
        last_user_idx = len(cleaned_messages) - 1
    else:
        # Move any trailing system/developer messages that follow last_user_idx to before last_user_idx
        trailing_system_msgs = []
        other_msgs_after_user = []
        for i in range(last_user_idx + 1, len(cleaned_messages)):
            m = cleaned_messages[i]
            if m["role"] in ("system", "developer"):
                trailing_system_msgs.append(m)
            else:
                other_msgs_after_user.append(m)

        if trailing_system_msgs:
            user_msg = cleaned_messages[last_user_idx]
            cleaned_messages = (
                cleaned_messages[:last_user_idx]
                + trailing_system_msgs
                + [user_msg]
                + other_msgs_after_user
            )

    # 5. Inject tool instructions into system prompt
    if tool_inst:
        if cleaned_messages and cleaned_messages[0]["role"] in ("system", "developer"):
            cleaned_messages[0]["content"] = f"{tool_inst}\n\n{cleaned_messages[0]['content']}"
        else:
            cleaned_messages.insert(0, {"role": "system", "content": tool_inst})

    # 6. Assemble prompt parts
    parts = []
    for msg in cleaned_messages:
        role = msg["role"]
        content = msg["content"]
        if role.startswith("TOOL RESULT"):
            parts.append(f"{role}: {content}")
        else:
            parts.append(f"{role.upper()}: {content}")

    prompt = "\n\n".join(parts).rstrip()
    if not prompt.endswith("ASSISTANT:"):
        prompt += "\n\nASSISTANT:"

    return prompt


_cached_ls_info = None
_cached_ls_expiry = 0.0

_cached_model_map = None
_cached_model_map_expiry = 0.0


def discover_ls(force_refresh=False):
    global _cached_ls_info, _cached_ls_expiry
    now = time.time()
    if not force_refresh and _cached_ls_info and now < _cached_ls_expiry:
        try:
            port = int(_cached_ls_info["address"].split(":")[1])
            if port_ready(port):
                return _cached_ls_info
        except Exception:
            pass

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
                info = {
                    "pid": pid,
                    "address": f"127.0.0.1:{port}",
                    "csrf_token": csrf_token,
                }
                _cached_ls_info = info
                _cached_ls_expiry = now + 10.0
                return info
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
        info = discover_ls(force_refresh=True)
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
    with request.urlopen(rpc_request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def load_model_map(force_refresh=False):
    global _cached_model_map, _cached_model_map_expiry
    now = time.time()
    if not force_refresh and _cached_model_map and now < _cached_model_map_expiry:
        return _cached_model_map

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
        ranked.sort(reverse=True, key=lambda x: x[:4])
        chosen = ranked[0][-1]
        return (((chosen.get("modelOrAlias") or {}).get("model")) or default_model)

    flash_model = choose(lambda label, model_id, _cfg: "flash" in label or "flash" in model_id) or default_model
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

    _cached_model_map = model_map
    _cached_model_map_expiry = now + 30.0
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
        self.handle_chat_completions()

    def handle_chat_completions(self, payload=None):
        try:
            if payload is None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            request_id = f"ag-{uuid.uuid4().hex}"
            tools = payload.get("tools")
            tool_choice = payload.get("tool_choice", "auto")
            prompt = messages_to_prompt(payload.get("messages") or [], tools=tools, tool_choice=tool_choice)
            requested_model, internal_model = resolve_model_name(payload.get("model"))
            log_traffic(
                "incoming_request",
                {
                    "request_id": request_id,
                    "path": self.path,
                    "requested_model": requested_model,
                    "prompt": prompt,
                    "tools": tools,
                    "tool_choice": tool_choice,
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
            log_traffic(
                "upstream_response",
                {
                    "request_id": request_id,
                    "status": 200,
                    "body": upstream_response,
                },
            )

            raw_response = upstream_response.get("response") if isinstance(upstream_response, dict) else None
            remaining_text, tool_calls = parse_model_response(raw_response or "")

            # Fail-fast check on empty upstream completion (F5)
            if (not raw_response or (isinstance(raw_response, str) and not raw_response.strip())) and not tool_calls:
                log_service(f"Upstream model returned empty completion for request {request_id}")
                self.respond_json(502, {
                    "error": {
                        "message": "Upstream model returned empty completion",
                        "type": "bad_gateway",
                        "code": 502,
                    }
                })
                return

            is_stream = bool(payload.get("stream"))
            cmpl_id = f"chatcmpl-{uuid.uuid4().hex}"
            created_ts = int(time.time())

            if is_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

                chunks = []
                # 1. Text chunk if remaining_text is non-empty
                if remaining_text:
                    chunks.append({
                        "id": cmpl_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": requested_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": remaining_text},
                                "finish_reason": None,
                            }
                        ],
                    })

                # 2. Segregated tool_calls chunks (F3)
                if tool_calls:
                    for idx, tc in enumerate(tool_calls):
                        delta = {
                            "tool_calls": [
                                {
                                    "index": idx,
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tc["function"]["name"],
                                        "arguments": tc["function"]["arguments"],
                                    },
                                }
                            ]
                        }
                        if not remaining_text and idx == 0:
                            delta["role"] = "assistant"
                        chunks.append({
                            "id": cmpl_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": requested_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": delta,
                                    "finish_reason": None,
                                }
                            ],
                        })
                    finish_reason = "tool_calls"
                else:
                    finish_reason = "stop"

                # 3. Final chunk with finish_reason
                chunks.append({
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": requested_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": finish_reason,
                        }
                    ],
                })

                try:
                    for chunk in chunks:
                        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    log_service(f"client disconnected during streaming for request {request_id}")
                return

            # Non-streaming response (F3)
            if tool_calls:
                finish_reason = "tool_calls"
                message_obj = {
                    "role": "assistant",
                    "content": remaining_text if remaining_text else None,
                    "tool_calls": tool_calls,
                }
            else:
                finish_reason = "stop"
                message_obj = {
                    "role": "assistant",
                    "content": remaining_text,
                }

            response_body = {
                "id": cmpl_id,
                "object": "chat.completion",
                "created": created_ts,
                "model": requested_model,
                "choices": [
                    {
                        "index": 0,
                        "message": message_obj,
                        "finish_reason": finish_reason,
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
        except (TimeoutError, socket.timeout) as exc:
            log_service(f"upstream timeout: {exc}")
            self.respond_json(504, {
                "error": {
                    "message": "Upstream language server timed out",
                    "type": "gateway_timeout",
                    "code": 504,
                }
            })
        except error.URLError as exc:
            log_service(f"upstream urlerror: {exc}")
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                self.respond_json(504, {
                    "error": {
                        "message": "Upstream language server timed out",
                        "type": "gateway_timeout",
                        "code": 504,
                    }
                })
            else:
                self.respond_json(502, {
                    "error": {
                        "message": f"Upstream language server connection failed: {exc.reason}",
                        "type": "bad_gateway",
                        "code": 502,
                    }
                })
        except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError, http_client.RemoteDisconnected, RuntimeError) as exc:
            log_service(f"upstream connection/runtime error: {exc}")
            self.respond_json(502, {
                "error": {
                    "message": f"Upstream language server connection failed: {exc}",
                    "type": "bad_gateway",
                    "code": 502,
                }
            })
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            self.respond_json(exc.code, {"error": {"message": body, "code": exc.code}})
        except Exception as exc:
            log_service(f"bridge error: {exc}")
            self.respond_json(500, {"error": {"message": str(exc), "code": 500}})

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
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    log_service(f"starting antigravity bridge on {HOST}:{PORT}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
