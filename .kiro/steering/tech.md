# Tech Steering: LiteLLM MultiHarness

## Runtime Environment & Constraints
- **Operating System**: macOS (Darwin arm64)
- **Package Manager & Execution**: Python 3.14 via `uv` (`uv run`). No raw system python invocations.
- **Proxy Engine**: LiteLLM Proxy running as a local service on port 5596 (`~/.litellm/service`).
- **Bridge Engine**: `service/bin/antigravity_bridge.py` on port 8788 bridging OpenAI Chat Completions to local Antigravity Language Server protobuf RPC.
- **Failover Node**: Remote Mac Mini via ZeroTier network (`10.147.17.130:8788`).

## Architectural Invariants
1. **Separation of Router vs Guard**:
   - Routing and model-group load balancing belong in `config/config.yaml`.
   - Inspection, response sanitization, and circuit breaking belong in `tool_choice_guard.py` (`CustomLogger`).
   - Hooks in `tool_choice_guard.py` must NEVER hardcode single static keys or clobber model definitions.
2. **Deterministic Error Handling**:
   - Upstream HTTP status codes (402, 429, 502, 504) must be matched across raw exceptions, response bodies, and LiteLLM-masked `APIConnectionError` objects.
   - Status codes alone are insufficient; regex inspection of response text and error messages is mandatory.
3. **State Persistence**:
   - Circuit breaker and keystore state must be persisted atomically to disk (`~/.litellm/`) to survive daemon restarts and background CLI tooling operations.
   - Keystores must check file modification times (`mtime`) on each turn to synchronize external probe updates without requiring proxy restarts.
4. **Testing Rigor**:
   - All changes must pass the automated replay test harness (`tests/`) using dynamic ephemeral ports (`127.0.0.1:0`).
   - Real socket assertions and session replays (e.g. `7f8bfd5b`) must be preserved.
