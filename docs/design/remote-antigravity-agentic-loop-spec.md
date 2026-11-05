# Remote Antigravity Agentic Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local and remote Antigravity bridges complete, protocol-correct tool-calling backends for OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages clients routed through LiteLLM.

**Architecture:** Normalize every inbound wire format into one canonical conversation and tool-call model, render that model into a nonce-framed prompt for the existing `GetModelResponse` Language Server RPC, then parse and validate the framed result before serializing it back to the caller’s protocol. The bridge owns all tool-call identifiers and response continuity; LiteLLM remains a routing, failover, and edge-translation layer that must preserve the normalized tool lineage unchanged.

**Tech Stack:** Python 3.11+, standard-library HTTP server, LiteLLM proxy, JSON Schema validation, `httpx`, `pytest`, `pytest-asyncio`, macOS `launchd`, ZeroTier remote fallback.

## Global Constraints

- Only these upstream targets are supported: Gemini 3.8 Flash at low, medium, and high reasoning effort; and Gemini 4 Argon at low, medium, and high reasoning effort.
- A model outside that allowlist fails closed during bridge resolution and must not be selected as a fallback target.
- The remote fallback bridge remains reachable at `http://10.147.17.130:8788/v1` over ZeroTier and must expose the same agentic protocol version as the local bridge before it is eligible for tool-bearing failover.
- The bridge returns tool actions; it never executes terminal, filesystem, MCP, or other tools itself.
- A malformed schema, malformed tool frame, unknown tool name, invalid arguments, unknown call ID, or out-of-order tool result is an explicit API error. It is never flattened into text or silently discarded.
- Tool-call IDs are opaque strings. They are generated once by the bridge for a newly emitted action and are copied byte-for-byte through all later turns and protocol translations.
- Do not persist or log prompt text, tool schemas, tool arguments, or tool results in normal traffic logs. Log only request/response IDs, endpoint, model alias, call-ID digest, tool name, counts, latency, and error code.

---

## 1. Target Architecture and Protocol Contract

### 1.1 Ownership boundaries

```text
Harness (Chat / Responses / Messages)
             |
             v
LiteLLM proxy (:5596)
  - route selection and local->remote retry
  - native Anthropic edge translation when required
  - preserve tools, tool_choice, tool calls, and call IDs
             |
             v
Antigravity bridge (:8788)
  - request normalizer
  - response ledger and ID validator
  - prompt renderer / framed-response parser
  - schema validator
  - wire-format response and SSE adapters
             |
             v
Language Server RPC: GetModelResponse
  {"prompt": "<canonical agent prompt>", "model": "<allowlisted target>"}
```

The Language Server remains a text-generation transport. The bridge must not depend on undocumented RPC fields for tools or streaming. Tool semantics are encoded in a bridge-owned prompt protocol and decoded only by the bridge.

### 1.2 Canonical internal representation

Create these public types in `service/agentic/types.py`. Use `dict[str, Any]` for JSON values and `dataclasses` for immutable protocol values.

```python
class BridgeRequestError(ValueError):
    def __init__(self, code: str, param: str, value: str | None = None) -> None:
        self.code = code
        self.param = param
        self.value = value
        super().__init__(f"{code}: {param}{f'={value}' if value else ''}")


class UpstreamProtocolError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool


@dataclass(frozen=True)
class ToolChoice:
    mode: Literal["auto", "none", "required", "specific"]
    tool_name: str | None = None


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ToolCallPart:
    call_id: str
    name: str
    arguments: dict[str, Any]
    arguments_json: str


@dataclass(frozen=True)
class ToolResultPart:
    call_id: str
    content: str
    is_error: bool = False


ConversationPart = TextPart | ToolCallPart | ToolResultPart


@dataclass(frozen=True)
class CanonicalMessage:
    role: Literal["system", "user", "assistant", "tool"]
    parts: tuple[ConversationPart, ...]


@dataclass(frozen=True)
class AgentRequest:
    request_id: str
    public_model: str
    upstream_model: str
    messages: tuple[CanonicalMessage, ...]
    tools: tuple[ToolDefinition, ...]
    tool_choice: ToolChoice
    parallel_tool_calls: bool
    previous_response_id: str | None


@dataclass(frozen=True)
class AgentResult:
    response_id: str
    parts: tuple[TextPart | ToolCallPart, ...]
```

`arguments_json` is always `json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))`; serialization is deterministic and no adapter reparses or reassigns it.

### 1.3 Inbound translation matrix

| Source | Tool definition | Assistant call | Tool result | Canonical rule |
|---|---|---|---|---|
| OpenAI Chat Completions | `tools[].function` | `assistant.tool_calls[].id`, `function.name`, `function.arguments` | `role: "tool"`, `tool_call_id` | Preserve `id`; parse arguments into an object; result must follow its call. |
| OpenAI Responses | `tools[]` with top-level `name` and `parameters` | `input[]` item `type: "function_call"` with `call_id` | `input[]` item `type: "function_call_output"` with `call_id` | Expand `previous_response_id` first, then validate outputs against expanded and newly supplied calls. |
| Anthropic Messages | `tools[]` with `input_schema` | assistant content block `type: "tool_use"`, `id`, `name`, `input` | user content block `type: "tool_result"`, `tool_use_id` | Map `id`/`tool_use_id` directly to `call_id`; map `any` to `required`, a named choice to `specific`, and `disable_parallel_tool_use` to the inverse parallel flag. |

The normalizer rejects duplicate tool names, duplicate call IDs, a result before its call, an output referencing a call from a different conversation, a missing ID, a non-object argument payload, and an assistant call for an unregistered tool.

### 1.4 Framed Language Server prompt contract

`service/agentic/prompt.py` renders every request as one JSON document surrounded by fixed request markers. Message and tool content is JSON-encoded, never concatenated using role labels.

```text
[[AGENTIC_REQUEST:1]]
{
  "protocol_version": 1,
  "nonce": "<32 lowercase hex characters>",
  "tools": [{"name": "...", "description": "...", "input_schema": {...}, "strict": true}],
  "tool_choice": {"mode": "auto", "tool_name": null},
  "parallel_tool_calls": true,
  "messages": [...]
}
[[/AGENTIC_REQUEST:1]]

Return normal assistant text, or one or more complete tool frames. For each
tool action, use exactly:
[[AGENTIC_TOOL_CALL:<nonce>]]
{"name":"registered_tool_name","arguments":{...}}
[[/AGENTIC_TOOL_CALL:<nonce>]]
Do not include an ID. Do not place text inside a tool frame.
```

The nonce is generated per request and passed to `ToolFrameParser`. It prevents accidental treatment of ordinary model text as a tool action. The parser accepts only a complete begin marker, one JSON object, optional whitespace, and the matching end marker. It uses `json.JSONDecoder.raw_decode` and rejects trailing non-whitespace payload.

### 1.5 Output translation matrix

| Destination | Text output | Tool output | Terminal signal |
|---|---|---|---|
| OpenAI Chat Completions | `choices[0].message.content` | `choices[0].message.tool_calls[]` with `{id, type:"function", function:{name, arguments}}` | `finish_reason: "tool_calls"` when any call exists; otherwise `"stop"`. |
| OpenAI Responses | completed `message` output item with `output_text` content | completed `function_call` output item with `{call_id, name, arguments}` | response `status: "completed"`; never use a message item to stand in for a function call. |
| Anthropic Messages | content block `{type:"text", text}` | content block `{type:"tool_use", id, name, input}` | `stop_reason: "tool_use"` when any call exists; otherwise `"end_turn"`. |

The public result ID is generated once as `resp_<uuid-hex>` for Responses, `chatcmpl-<uuid-hex>` for Chat Completions, and `msg_<uuid-hex>` for Messages. A `ResponseLedger` associates a Responses ID with the canonical transcript and pending calls for its bounded lifetime.

### 1.6 Streaming rules

The current RPC produces one completed string, so the first release must buffer it, parse it completely, validate it, and then emit protocol-correct SSE. This is compatible streaming, not a claim of upstream token streaming. The parser is nevertheless incremental: a later confirmed RPC stream can call `feed(fragment)` without changing any wire adapter.

| API | Required sequence for a tool action |
|---|---|
| Chat Completions | role delta; tool-call delta containing `index`, stable `id`, `type`, and `function.name`; zero or more argument deltas; terminal `finish_reason: "tool_calls"`; `[DONE]`. |
| Responses | `response.created`; `response.in_progress`; `response.output_item.added`; zero or more `response.function_call_arguments.delta`; `response.function_call_arguments.done`; `response.output_item.done`; `response.completed`. |
| Messages | `message_start`; `content_block_start` for `tool_use`; `content_block_delta` with `input_json_delta.partial_json`; `content_block_stop`; `message_delta` with `stop_reason: "tool_use"`; `message_stop`. |

Chunk JSON argument strings only at Unicode code-point boundaries. Buffer an incomplete nonce marker or frame until it is complete; never leak a partial marker as assistant text. An unterminated or invalid frame produces an explicit upstream protocol error before an SSE success event is sent.

### 1.7 Response and call continuity

`ResponseLedger` is an in-memory, mutex-protected, TTL-bounded LRU store. Its default TTL is 30 minutes and its default capacity is 256 responses, both configurable through environment variables. It stores only enough canonical transcript state to expand a `previous_response_id` and validate later tool outputs. It has no disk persistence by default.

For a Responses request with `previous_response_id`, the bridge:

1. Resolves the record or returns a `400` `previous_response_not_found`.
2. Prepends its transcript and pending calls to the new input.
3. Validates each new `function_call_output.call_id` sequentially against pending calls.
4. Consumes each matched pending call exactly once.
5. Records the newly generated result and its pending calls under a fresh response ID.

For Chat and Messages, the caller supplies the full history; the same sequential validation runs against the canonicalized transcript. Reusing a completed call ID as a new result, changing the tool name attached to a call ID, or emitting a result before its call fails with `400` `orphan_tool_result`.

### 1.8 LiteLLM edge rules

`tool_choice_guard.py` is an edge guard, not a second protocol implementation:

- Keep its removal of `tool_choice` and `parallel_tool_calls` only when the request has no tools.
- Keep a tool result with a non-empty ID when `previous_response_id` is present; the bridge ledger is the authority that can resolve it.
- Without `previous_response_id`, validate result ordering sequentially rather than pre-collecting every call ID. A future call must not make an earlier output valid.
- Do not modify `tools`, `tool_choice`, `parallel_tool_calls`, call IDs, function names, argument strings, or result bodies on valid agentic requests.
- Preserve the existing routing and retry behavior, but require the remote capability endpoint to advertise protocol version 1 before a tool-bearing retry is accepted.

## 2. File Structure

| Path | Responsibility |
|---|---|
| `service/agentic/__init__.py` | Re-export stable bridge protocol entry points. |
| `service/agentic/types.py` | Canonical immutable protocol types and bridge exceptions. |
| `service/agentic/models.py` | Closed allowlist, alias-to-target resolution, and catalog validation. |
| `service/agentic/language_server.py` | Typed wrapper around Language Server discovery, health, and `GetModelResponse`. |
| `service/agentic/normalize.py` | Chat, Responses, and Messages request normalization plus sequential ID validation. |
| `service/agentic/prompt.py` | Nonce generation and canonical Language Server prompt rendering. |
| `service/agentic/parser.py` | Incremental framed-response parser and JSON Schema validation. |
| `service/agentic/ledger.py` | Bounded Responses continuity records and pending-call consumption. |
| `service/agentic/wire_chat.py` | Chat Completions JSON and SSE serializers. |
| `service/agentic/wire_responses.py` | Responses JSON and SSE serializers. |
| `service/agentic/wire_messages.py` | Anthropic Messages JSON and SSE serializers. |
| `service/agentic/service.py` | One orchestration method from normalized request to parsed result. |
| `service/bin/antigravity_bridge.py` | HTTP routes, existing Language Server discovery/RPC helpers, capability endpoint, and error envelopes. |
| `tool_choice_guard.py` | LiteLLM-safe validation and preservation of agentic inputs across routing. |
| `config/config.yaml` | Local and remote allowlisted model pools, paired fallback entries, and callback wiring. |
| `pyproject.toml` | Add `jsonschema` runtime dependency. |
| `tests/test_agentic_normalize.py` | All inbound translation and ID-invariant unit tests. |
| `tests/test_agentic_prompt_parser.py` | Prompt and delimiter/parser unit tests. |
| `tests/test_agentic_ledger.py` | `previous_response_id` and TTL/capacity tests. |
| `tests/test_agentic_wire.py` | Exact non-streaming payload and SSE event tests. |
| `tests/test_antigravity_bridge.py` | HTTP handler integration tests with a fake RPC client. |
| `tests/test_tool_choice_guard.py` | Edge-guard regression tests for valid and invalid lineage. |
| `tests/test_config_validation.py` | Allowlist and local/remote agentic fallback parity checks. |
| `tests/test_live_proxy.py` | Opt-in local proxy contract tests, including the full two-turn tool loop. |
| `docs/design/remote-antigravity-agentic-loop.md` | Architecture, API contract, rollout, and operator troubleshooting guide. |
| `README.md` | Supported endpoint and agentic-loop usage summary. |

## 3. MECE Backlog

The epics are mutually exclusive by responsibility: policy and deployability; inbound semantic conversion; Language Server conversion; non-streaming wire conversion; streaming wire conversion; LiteLLM routing; and verification/release. Together they cover every ingress, state transition, egress, and operational surface.

### Epic 1: Establish the closed model policy and bridge package boundary

**Files:**
- Create: `service/agentic/__init__.py`
- Create: `service/agentic/models.py`
- Create: `service/agentic/language_server.py`
- Modify: `service/bin/antigravity_bridge.py`
- Modify: `config/config.yaml`
- Modify: `pyproject.toml`
- Modify: `tests/test_config_validation.py`

**Acceptance criteria:**
- The bridge resolves only the six allowlisted targets.
- Dynamic catalog discovery cannot expose or select an unapproved target.
- Every local agentic pool has an identically capable remote counterpart.
- `GET /v1/agentic-capabilities` returns `{"protocol_version":1,"tools":true,"streaming":true,"chat_completions":true,"responses":true,"messages":true}`.

- [ ] **Step 1: Write configuration and resolver tests first**

```python
def test_agentic_model_targets_are_allowlisted():
    allowed = {
        "gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high",
        "gemini-4-argon-low", "gemini-4-argon-medium", "gemini-4-argon-high",
    }
    assert set(SUPPORTED_UPSTREAM_MODELS) == allowed


def test_remote_pool_has_a_counterpart_for_every_local_agentic_model():
    model_names = {entry["model_name"] for entry in config["model_list"]}
    local = {name.removeprefix("antigravity-") for name in model_names
             if name.startswith("antigravity-") and not name.startswith("antigravity-remote-")}
    remote = {name.removeprefix("antigravity-remote-") for name in model_names
              if name.startswith("antigravity-remote-")}
    assert local == remote
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `uv run pytest tests/test_config_validation.py -q`

Expected: failure because the closed resolver and paired Argon remote definitions do not exist.

- [ ] **Step 3: Implement the closed resolver and capability endpoint**

```python
SUPPORTED_UPSTREAM_MODELS = frozenset({
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "gemini-4-argon-low",
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
})


def canonical_public_model_name(name: str | None) -> str:
    requested = (name or "gemini-3.8-flash-medium").removeprefix("openai/").lower()
    aliases = {
        "antigravity-flash-low": "gemini-3.8-flash-low",
        "antigravity-flash-medium": "gemini-3.8-flash-medium",
        "antigravity-flash-high": "gemini-3.8-flash-high",
        "antigravity-argon-low": "gemini-4-argon-low",
        "antigravity-argon-medium": "gemini-4-argon-medium",
        "antigravity-argon-high": "gemini-4-argon-high",
    }
    return aliases.get(requested, requested)


def resolve_model_name(name: str, catalog: Mapping[str, str]) -> tuple[str, str]:
    requested = canonical_public_model_name(name)
    target = catalog.get(requested, requested)
    if target not in SUPPORTED_UPSTREAM_MODELS:
        raise BridgeRequestError("unsupported_model", "model", requested)
    return requested, target
```

Replace the dynamic catch-all model registration in `load_model_map()` with a filter through this resolver. Add the capability route before the default 404 route.

- [ ] **Step 4: Add the JSON Schema dependency**

```toml
"jsonschema>=4.23.0",
```

- [ ] **Step 5: Re-run focused tests**

Run: `uv run pytest tests/test_config_validation.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml config/config.yaml service/agentic service/bin/antigravity_bridge.py tests/test_config_validation.py
git commit -m "feat: define allowlisted agentic bridge model pools"
```

### Epic 2: Normalize all inbound protocols and enforce lineage

**Files:**
- Create: `service/agentic/types.py`
- Create: `service/agentic/normalize.py`
- Create: `service/agentic/ledger.py`
- Create: `tests/test_agentic_normalize.py`
- Create: `tests/test_agentic_ledger.py`

**Interfaces:**
- Consumes: model resolver from Epic 1.
- Produces: `normalize_chat_request()`, `normalize_responses_request()`, `normalize_messages_request()`, and `ResponseLedger`.
- Guarantees: all later layers receive a valid `AgentRequest`; no later layer performs protocol-specific ID interpretation.

**Acceptance criteria:**
- A tool call emitted in any supported wire format round-trips as the same canonical ID.
- Result-before-call, unknown-ID, duplicate-ID, and duplicate-result requests return a typed error.
- A Responses continuation expands `previous_response_id` and accepts only its pending IDs.

- [ ] **Step 1: Write parameterized inbound-format tests**

```python
@pytest.mark.parametrize("normalizer,payload", [
    (normalize_chat_request, OPENAI_CHAT_TOOL_HISTORY),
    (normalize_responses_request, RESPONSES_TOOL_HISTORY),
    (normalize_messages_request, ANTHROPIC_TOOL_HISTORY),
])
def test_normalizers_preserve_call_id(normalizer, payload):
    request = normalizer(payload, ledger=ResponseLedger())
    call = next(part for message in request.messages for part in message.parts
                if isinstance(part, ToolCallPart))
    assert call.call_id == "call_42"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_agentic_normalize.py tests/test_agentic_ledger.py -q`

Expected: failure because canonical types and normalizers do not exist.

- [ ] **Step 3: Implement typed normalization and sequential validation**

```python
def validate_tool_lineage(messages: Sequence[CanonicalMessage]) -> None:
    pending: dict[str, ToolCallPart] = {}
    completed: set[str] = set()
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                if part.call_id in pending or part.call_id in completed:
                    raise BridgeRequestError("duplicate_tool_call_id", "messages", part.call_id)
                pending[part.call_id] = part
            elif isinstance(part, ToolResultPart):
                if part.call_id not in pending or part.call_id in completed:
                    raise BridgeRequestError("orphan_tool_result", "messages", part.call_id)
                completed.add(part.call_id)
```

`ResponseLedger.resolve()` must return a typed `BridgeRequestError("previous_response_not_found", "previous_response_id", value)` after expiry or eviction.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_agentic_normalize.py tests/test_agentic_ledger.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/agentic/types.py service/agentic/normalize.py service/agentic/ledger.py tests/test_agentic_normalize.py tests/test_agentic_ledger.py
git commit -m "feat: normalize agentic requests with strict call lineage"
```

### Epic 3: Render and parse the Language Server agentic protocol

**Files:**
- Create: `service/agentic/prompt.py`
- Create: `service/agentic/parser.py`
- Create: `tests/test_agentic_prompt_parser.py`

**Interfaces:**
- Consumes: valid `AgentRequest` from Epic 2.
- Produces: `render_prompt(request, nonce) -> str` and `ToolFrameParser(nonce, tools, choice, parallel).feed()/finish()`.
- Guarantees: only validated `AgentResult` parts are passed to a wire serializer.

**Acceptance criteria:**
- Tool schemas, tool-choice policy, call IDs, and result IDs are present in rendered JSON rather than flattened role text.
- Every possible split position across a valid marker/frame yields the same parsed result.
- Unknown tools, invalid JSON, non-object arguments, schema violations, forbidden tool choices, and excess calls when parallel calling is disabled fail explicitly.

- [ ] **Step 1: Write parser boundary and schema tests**

```python
def test_frame_parser_accepts_every_chunk_boundary():
    read_file = ToolDefinition(
        name="read_file",
        description="Read a repository file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        strict=True,
    )
    raw = valid_tool_frame(nonce="a" * 32, name="read_file",
                           arguments={"path": "README.md"})
    for split in range(len(raw) + 1):
        parser = ToolFrameParser(nonce="a" * 32, tools=(read_file,), choice=ToolChoice("auto"),
                                 parallel_tool_calls=True)
        parser.feed(raw[:split])
        parser.feed(raw[split:])
        assert parser.finish().parts[0].name == "read_file"
```

- [ ] **Step 2: Run test and verify it fails**

Run: `uv run pytest tests/test_agentic_prompt_parser.py -q`

Expected: failure because neither renderer nor parser exists.

- [ ] **Step 3: Implement nonce-framed rendering and parser**

```python
begin = f"[[AGENTIC_TOOL_CALL:{self.nonce}]]"
end = f"[[/AGENTIC_TOOL_CALL:{self.nonce}]]"

decoded, consumed = json.JSONDecoder().raw_decode(frame_json)
if frame_json[consumed:].strip():
    raise UpstreamProtocolError("tool_frame_trailing_data")
if not isinstance(decoded.get("arguments"), dict):
    raise UpstreamProtocolError("tool_arguments_not_object")
```

Use `jsonschema.Draft202012Validator(tool.input_schema).validate(arguments)`. Generate `call_<uuid.uuid4().hex>` only after the parser validates a newly emitted call.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_agentic_prompt_parser.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/agentic/prompt.py service/agentic/parser.py tests/test_agentic_prompt_parser.py
git commit -m "feat: add framed language server tool protocol"
```

### Epic 4: Implement non-streaming response adapters and HTTP orchestration

**Files:**
- Create: `service/agentic/service.py`
- Create: `service/agentic/wire_chat.py`
- Create: `service/agentic/wire_responses.py`
- Create: `service/agentic/wire_messages.py`
- Modify: `service/bin/antigravity_bridge.py`
- Create: `tests/test_agentic_wire.py`
- Create: `tests/test_antigravity_bridge.py`

**Interfaces:**
- Consumes: validated `AgentRequest` and `AgentResult`.
- Produces: direct bridge support for `POST /v1/chat/completions`, `POST /v1/responses`, and `POST /v1/messages`.
- Guarantees: tool actions retain their canonical IDs and use protocol-native finish/stop reasons.

**Acceptance criteria:**
- An injected fake RPC output containing text plus two valid tool frames produces exactly two tool actions, not assistant text that resembles an action.
- Chat, Responses, and Messages non-streaming bodies match their API-native tool representations.
- Invalid inbound request errors use each endpoint’s documented-shaped error envelope and a 4xx status; malformed upstream frames return a 502-shaped error.
- Normal traffic logs contain correlation metadata only; fixture prompt text, argument values, and result values never occur in a log record.

- [ ] **Step 1: Write exact output assertions**

```python
def test_chat_response_uses_tool_calls_finish_reason():
    body = build_chat_response(result_with_call("call_42", "read_file", {"path": "README.md"}))
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["message"]["tool_calls"][0]["id"] == "call_42"


def test_responses_response_uses_function_call_item():
    body = build_responses_response(result_with_call("call_42", "read_file", {}))
    item = next(item for item in body["output"] if item["type"] == "function_call")
    assert item["call_id"] == "call_42"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_agentic_wire.py tests/test_antigravity_bridge.py -q`

Expected: failure because the bridge only handles chat completions as raw text.

- [ ] **Step 3: Implement the service orchestration**

```python
class RpcClient(Protocol):
    def get_model_response(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


def complete(request: AgentRequest, rpc_client: RpcClient, ledger: ResponseLedger) -> AgentResult:
    nonce = secrets.token_hex(16)
    raw = rpc_client.get_model_response(
        {"prompt": render_prompt(request, nonce), "model": request.upstream_model}
    )
    result = ToolFrameParser.from_request(request, nonce).parse_complete(raw["response"])
    ledger.record(request, result)
    return result
```

Move existing discovery and RPC helpers behind `RpcClient`. Keep the existing startup and health behavior. Route only known POST paths; keep a 404 for all other paths.
Replace `log_traffic()` uses that record `prompt` or raw `body` with an event containing only `request_id`, endpoint, requested model, response ID, tool names, a SHA-256 digest of each call ID, count, latency, status, and error code.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_agentic_wire.py tests/test_antigravity_bridge.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/agentic/service.py service/agentic/wire_chat.py service/agentic/wire_responses.py service/agentic/wire_messages.py service/agentic/language_server.py service/bin/antigravity_bridge.py tests/test_agentic_wire.py tests/test_antigravity_bridge.py
git commit -m "feat: serialize agentic bridge responses for all APIs"
```

### Epic 5: Implement protocol-correct streaming without partial actions

**Files:**
- Modify: `service/agentic/parser.py`
- Modify: `service/agentic/wire_chat.py`
- Modify: `service/agentic/wire_responses.py`
- Modify: `service/agentic/wire_messages.py`
- Modify: `service/bin/antigravity_bridge.py`
- Create: `tests/test_agentic_streaming.py`

**Interfaces:**
- Consumes: completely validated `AgentResult` in the first release and incremental parser events when a confirmed RPC stream becomes available.
- Produces: bytes yielded as valid SSE records for every public streaming endpoint.
- Guarantees: the same `call_id`, function name, and final JSON arguments appear in stream and non-stream response forms.

**Acceptance criteria:**
- Tool argument JSON is reconstructible from each endpoint’s deltas.
- A tool frame split at marker, JSON, and end-marker boundaries never appears as text.
- Stream termination is exactly one terminal API event plus `[DONE]` where the protocol requires it.

- [ ] **Step 1: Write SSE transcript tests**

```python
def test_chat_stream_reconstructs_tool_arguments():
    events = decode_sse(iter_chat_sse(result_with_call("call_42", "read_file", {"path": "x"})))
    assert reconstruct_chat_tool_calls(events)["call_42"]["arguments"] == '{"path":"x"}'
    assert events[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert events[-1] == "[DONE]"
```

- [ ] **Step 2: Run test and verify it fails**

Run: `uv run pytest tests/test_agentic_streaming.py -q`

Expected: failure because the current bridge sends one raw content chunk and `stop`.

- [ ] **Step 3: Implement protocol serializers**

Use one `iter_codepoint_chunks(value, size=512)` helper and one `write_sse(event_name, body)` helper. Emit only a complete, validated tool call. The non-stream parser result is authoritative until a supported RPC streaming method is explicitly integrated.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_agentic_streaming.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/agentic/parser.py service/agentic/wire_chat.py service/agentic/wire_responses.py service/agentic/wire_messages.py service/bin/antigravity_bridge.py tests/test_agentic_streaming.py
git commit -m "feat: stream validated tool calls across API formats"
```

### Epic 6: Preserve agentic payloads through LiteLLM routing and remote failover

**Files:**
- Modify: `tool_choice_guard.py`
- Modify: `config/config.yaml`
- Modify: `tests/test_tool_choice_guard.py`
- Modify: `tests/test_config_validation.py`
- Modify: `tests/test_live_proxy.py`

**Interfaces:**
- Consumes: bridge protocol version 1 and its capability endpoint from Epic 1.
- Produces: unchanged valid agentic payloads at either local or remote bridge.
- Guarantees: remote retry does not strip, rename, regenerate, or reorder tool lineage.

**Acceptance criteria:**
- A valid Responses `function_call_output` with `previous_response_id` remains present after the callback.
- An out-of-order output in a self-contained input is rejected rather than made valid by a later call item.
- Each allowlisted local pool maps to the same remote target tier.
- A tool-bearing retry is rejected with a clear capability error if the remote bridge does not advertise protocol version 1.

- [ ] **Step 1: Add edge-regression tests**

```python
@pytest.mark.asyncio
async def test_guard_keeps_previous_response_tool_output():
    payload = {
        "previous_response_id": "resp_1",
        "input": [{"type": "function_call_output", "call_id": "call_42", "output": "ok"}],
    }
    result = await StripOrphanToolChoiceGuard().async_pre_call_hook({}, None, payload, "aresponses")
    assert result["input"][0]["call_id"] == "call_42"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_tool_choice_guard.py tests/test_config_validation.py -q`

Expected: failure because the current global call-ID collection cannot distinguish a valid continuation from an orphan.

- [ ] **Step 3: Make the guard lineage-safe**

Replace the two-pass all-ID collection with a one-pass `seen_call_ids` validator for self-contained inputs. If `previous_response_id` is a non-empty string, retain non-empty result IDs for bridge validation. Continue to remove only tool-choice settings that have no tools.

- [ ] **Step 4: Add capability-gated retry behavior**

Cache the remote `GET /v1/agentic-capabilities` result for 30 seconds. Before routing a request containing `tools`, a tool result, or a `previous_response_id` to the remote endpoint, require `protocol_version == 1`, `tools == true`, `streaming == true`, and all three endpoint booleans (`chat_completions`, `responses`, and `messages`) to be true. Return a visible routing error on mismatch; do not fall back to raw-text behavior.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_tool_choice_guard.py tests/test_config_validation.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tool_choice_guard.py config/config.yaml tests/test_tool_choice_guard.py tests/test_config_validation.py tests/test_live_proxy.py
git commit -m "feat: preserve agentic call lineage during fallback"
```

### Epic 7: Document, deploy, and verify the end-to-end loop

**Files:**
- Create: `docs/design/remote-antigravity-agentic-loop.md`
- Modify: `README.md`
- Modify: `tests/test_live_proxy.py`

**Acceptance criteria:**
- The design document contains the framing grammar, error codes, model policy, endpoint contracts, and rollback sequence.
- Local and remote bridges run the same commit and return the same capability response before failover is enabled.
- A two-turn tool loop succeeds through LiteLLM for Chat, Responses, and Messages, in both normal and forced-remote routing tests.

- [ ] **Step 1: Add opt-in live contract tests**

```python
def test_responses_tool_loop_through_proxy():
    first = post_responses(required_echo_tool_request())
    call = only_function_call(first.json()["output"])
    second = post_responses({
        "model": first.json()["model"],
        "previous_response_id": first.json()["id"],
        "input": [{"type": "function_call_output", "call_id": call["call_id"], "output": "echoed"}],
    })
    assert second.status_code == 200
```

Mark these tests `@pytest.mark.live` and skip only when `LITELLM_LIVE_TESTS=1` is absent.

- [ ] **Step 2: Run unit and contract suites**

Run: `uv run pytest tests/test_agentic_normalize.py tests/test_agentic_prompt_parser.py tests/test_agentic_ledger.py tests/test_agentic_wire.py tests/test_agentic_streaming.py tests/test_antigravity_bridge.py tests/test_tool_choice_guard.py tests/test_config_validation.py -v`

Expected: PASS.

- [ ] **Step 3: Deploy remote first**

1. Merge the implementation PR.
2. On the remote Mac Mini, update to the merged commit and restart the bridge.
3. Verify `curl --fail http://10.147.17.130:8788/v1/agentic-capabilities`.
4. Run `LITELLM_LIVE_TESTS=1 uv run pytest tests/test_live_proxy.py -m live -v` against forced remote routing.
5. Update and restart the local bridge, then run the same live suite with normal routing.
6. Enable or retain the LiteLLM fallback only after both capability checks and both live suites pass.

- [ ] **Step 4: Verify every target-harness protocol family**

| Client family | Endpoint to verify | Required proof |
|---|---|---|
| Claude Code and Claude Desktop | `/v1/messages` | A `tool_use` block is returned, then its matching `tool_result` completes a second turn. |
| Codex | `/v1/responses` | `function_call.call_id` from the first response is accepted as `function_call_output.call_id` in the continuation. |
| ohmypi, Unstoppable, and Paseo | their configured OpenAI-compatible endpoint | A streamed tool-call delta reconstructs to the same non-stream call ID and arguments. |

- [ ] **Step 5: Commit**

```bash
git add docs/design/remote-antigravity-agentic-loop.md README.md tests/test_live_proxy.py
git commit -m "docs: document remote agentic bridge operation"
```

## 4. Verification Matrix

| Layer | Deterministic verification | Live verification |
|---|---|---|
| Model policy | unit test enumerates the six targets and rejects an unknown target | `/v1/models` and capability response agree on both machines |
| Ingress normalization | fixture for every wire format and every bad lineage condition | harness emits valid request shapes without 4xx |
| Prompt translation | snapshot includes tools, tool choice, calls, and results as JSON | traffic metadata shows an agentic request without content logging |
| Delimiter parser | byte-boundary, malformed-frame, multi-call, and schema-invalid tests | forced tool request yields a native tool action |
| Non-stream egress | exact JSON object snapshots | each client parses and executes its first action |
| Stream egress | reconstructed deltas equal non-stream result | streaming harness continues after executing a tool |
| Responses continuity | ledger continuation, expiry, duplicate, and unknown-ID tests | Codex two-turn loop succeeds |
| LiteLLM failover | callback preserves valid continuation payloads | remote forced route and local-to-remote retry both succeed |

## 5. Rollback and Failure Semantics

- Deploy the remote bridge before the local bridge so an eligible fallback is never older than the primary protocol.
- If the remote capability version is missing or incompatible, capability gating stops tool-bearing fallback with a visible route error; it must not downgrade to a text-only completion.
- If a bridge discovers an invalid model catalog, it remains unhealthy and returns a configuration error rather than silently selecting a target.
- If the Language Server returns malformed framed output, return a 502 `upstream_tool_protocol_error` and retain the request correlation ID in metadata logs.
- If the ledger cannot resolve a continuation, return a 400 `previous_response_not_found`; the client may resend a complete transcript, which is then validated normally.
- Roll back by pinning both bridge deployments to the prior known-good commit and disabling tool-bearing remote fallback in `config/config.yaml`; do not leave a mixed bridge version pair active.

## 6. Self-Review

- **Spec coverage:** The plan covers OpenAI tools/tool calls, Anthropic tool use, Responses function call and output linkage, direct Language Server RPC translation, non-streaming behavior, SSE behavior, schema validation, call-ID continuity, all named harness protocol families, local/remote parity, and deployment.
- **MECE check:** Each epic owns exactly one concern—policy, inbound conversion, RPC conversion, non-stream response conversion, stream conversion, proxy/failover, or operations—and no required surface is unowned.
- **Placeholder scan:** No implementation task relies on an unspecified parser, schema rule, ID rule, API endpoint, test command, or deployment order.
- **Type consistency:** The same `ToolCallPart.call_id`, `ToolResultPart.call_id`, `AgentRequest`, `AgentResult`, and `ResponseLedger` contracts are used from normalization through serialization and testing.
