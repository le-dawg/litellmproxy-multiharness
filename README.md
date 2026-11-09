# litellmproxy-multiharness

Production-hardened local proxy and routing harness for [LiteLLM](https://github.com/BerriAI/litellm) running as a persistent, rootless macOS user `launchd` daemon on `127.0.0.1:5596`.

Specifically engineered for seamless multi-agent orchestration across **Claude Code**, **Codex Desktop**, **Antigravity Gateway (`agy`)**, and upstream providers (Unstoppable AI, Regolo AI, Azure OpenAI) via unified OpenAI and Anthropic endpoint emulation.

---

## Architecture & Recommended Configuration

### Multi-Agent Model Precedence Architecture

When integrating local or third-party proxies with Claude Code, relying on the `ANTHROPIC_MODEL` environment variable creates a **hard session lock**, preventing the interactive `/model` picker or subagent configurations from switching tiers dynamically.

`litellmproxy-multiharness` decouples model selection into a clean, 4-tier precedence model:

```text
=========================================================================================================================================
                                     CLAUDE CODE & LITELLM MULTIHARNESS PRECEDENCE ARCHITECTURE
=========================================================================================================================================

 [ LAYER 1: CLAUDE CODE INVOCATION ]
   │
   ├── CLI Flag: --model <id> ────────────────────────────────────────────────────────┐ (Highest Precedence)
   │                                                                                  │
   ├── /model Interactive Menu Pick                                                   │
   │    ├── Option 1: "Default"   ──────> settings.json "model": "claude-sonnet-5"────┼─────────┐
   │    ├── Option 2: "Sonnet"    ──────> ANTHROPIC_DEFAULT_SONNET_MODEL ─────────────┼───┐     │
   │    ├── Option 3: "Opus"      ──────> ANTHROPIC_DEFAULT_OPUS_MODEL ───────────────┼─┐ │     │
   │    └── Option 4: "Haiku"     ──────> ANTHROPIC_DEFAULT_HAIKU_MODEL ──────────────┼───┼─┐   │
   │                                                                                  │ │ │ │   │
   └── [RECOMMENDED]: Omit ANTHROPIC_MODEL from settings.json (no hard-pin lock!)     │ │ │ │   │
                                                                                      │ │ │ │   │
======================================================================================│=│=│=│===│========================================
 [ LAYER 2: RESOLVED REQUEST STRINGS ]                                                │ │ │ │   │
                                                                                      │ │ │ │   │
     claude-sonnet-5            <─────────────────────────────────────────────────────┘ │ │ │   │
     claude-sonnet-5-5          <───────────────────────────────────────────────────────┘ │ │   │
     claude-opus-5-5            <─────────────────────────────────────────────────────────┘ │   │
     claude-haiku-4-5-20251001  <───────────────────────────────────────────────────────────┘   │
                                                                                                │
================================================================================================│========================================
 [ LAYER 3: LITELLM PROXY ROUTING (:5596) ]                                                     │
                                                                                                │
     ┌────────────────────────────┐                                                             │
     │ Request: claude-sonnet-5   │ ────> claude-sonnet-5-5-high ───────────────────────────────┼────────┐
     └────────────────────────────┘                                                             │        │
     ┌────────────────────────────┐                                                             │        │
     │ Request: claude-sonnet-5-5 │ ────> glm-5.3-flash (Unstoppable AI Credits, max reasoning)  │        │
     └────────────────────────────┘                                                             │        │
     ┌────────────────────────────┐                                                             │        │
     │ Request: *opus* / opus-5-5 │ ────> gemini-4-argon-medium (Antigravity Pro) ──────────────┼──┐     │
     └────────────────────────────┘                                                             │  │     │
     ┌────────────────────────────┐                                                             │  │     │
     │ Request: claude-haiku-4-5  │ ────> antigravity-flash-low (gemini-3.8-flash-low) ─────────┼─────┐  │
     └────────────────────────────┘                                                             │  │  │  │
                                                                                                │  │  │  │
================================================================================================│==│==│==│===============================
 [ LAYER 4: TARGET BACKEND EXECUTION ENGINES ]                                                  │  │  │  │
                                                                                                │  │  │  │
   [ ANTIGRAVITY GATEWAY (:8788) ]                                                              │  │  │  │
     ├── claude-sonnet-5-5-high (Antigravity Sonnet 5.5) <──────────────────────────────────────┘  │  │  │
     │     └── [Fallback if unavailable] ──> gemini-3.8-flash-high                                  │  │  │
     │                                                                                             │  │  │
     ├── gemini-4-argon-medium (All Opus & Pro requests) <─────────────────────────────────────────┘  │  │
     │                                                                                                │  │
     └── gemini-3.8-flash-low (Haiku requests) <──────────────────────────────────────────────────────┘  │
                                                                                                         │
   [ UNSTOPPABLE AI PROXY (Credits) ]                                                                    │
     └── GLM 5.3 Flash (budget_tokens: 32000, stream: True) <───────────────────────────────────────────┘
           └── [Fallback if wallet depleted] ──> gemini-4-argon-medium (:8788)
=========================================================================================================================================
```

### Recommended Claude Code Configuration (`~/.claude/settings.json`)

To prevent hard-locking and ensure the `/model` menu correctly presents the 4 discrete tiers, configure `~/.claude/settings.json` as follows:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:5596",
    "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    "MCP_TIMEOUT": "30000",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5-20251001"
  },
  "model": "claude-sonnet-5"
}
```

> [!IMPORTANT]
> **Do not set `ANTHROPIC_MODEL` in `settings.json`.** Setting `ANTHROPIC_MODEL` acts as an absolute environment override, ignoring user selections in `/model` and forcing every new turn into the same model string.

---

## Key Capabilities & Hardening

1. **Null-Byte Sanitization (`_strip_null_bytes`):**
   - Strips `\x00` null bytes across messages, system prompts, and tool results recursively. Prevents downstream process spawners (`Bun.spawn`, `execve`) from crashing with `TypeError: The argument must be a string without null bytes` when inspecting binary plist or raw file reads.

2. **Responses API Parameter Sanitization (`tool_choice_guard.py`):**
   - **`stop_sequences` Stripping:** Azure Responses API rejects `stop_sequences` with HTTP 400. The guard silently strips this parameter for `/v1/responses` calls.
   - **Orphan `tool_choice` Stripping:** Strictly forbids orphan `tool_choice` unless `tools` are present. The guard automatically removes `tool_choice` and `parallel_tool_calls` when tools are empty or null.
   - **Orphan Tool Call Output Cleanup:** When auto-compact truncation drops earlier `function_call` turns while retaining downstream `function_call_output` items, Azure rejects the request with HTTP 400 (`Invalid Value: 'input.call_id'. Function call output requires call_id.`). The pre-call hook sanitizes `input` arrays by dropping orphaned outputs before dispatching upstream.

3. **Dynamic Unstoppable AI Disambiguation & Fallbacks:**
   - Detects `claude-sonnet-5-5` requests and routes them to `glm-5.3-flash` with dynamic keychain authentication and thinking budgets.
   - Disambiguates local deployment suffixes (`-high`, `-medium`, `-low`) so that requests targeted for the local Antigravity daemon (`claude-sonnet-5-5-high`) route directly to `:8788` without being intercepted.
   - Provides multi-tier failover: if Unstoppable prepaid credits exhaust, requests gracefully fall back to local `gemini-4-argon-medium` and remote ZeroTier nodes.

4. **Cosmetic Error Leak Suppression:**
   - LiteLLM's `router.py` unconditionally injects `. Received Model Group=... Available Model Group Fallbacks=None` onto all unhandled errors when `expose_router_debug_in_errors` is true.
   - Setting `expose_router_debug_in_errors: false` in `config.yaml` suppresses this red-herring message and reveals the true underlying error (DNS drop, rate limit, or context overflow).

5. **DarkWake Network Reconnection Resilience:**
   - macOS DarkWake from Deep Idle causes scheduled tasks to fire before the Wi-Fi physical link is re-associated (`[Errno 8] nodename nor servname provided, or not known`).
   - Configured `router_settings` (`num_retries: 3`, `retry_after: 3`, `allowed_fails_policy: {RateLimitError: 5}`) bridges the 2–5 second DHCP reconnection window cleanly.

6. **Transport Reliability:**
   - Enforces `DISABLE_AIOHTTP_TRANSPORT=true` (using `httpx`) to prevent truncated chunked responses on streaming Responses API calls.

---

## Directory Layout

```text
litellm-simplified/
├── config/
│   └── config.yaml                      # Hardened proxy configuration template
├── docs/
│   ├── design/
│   │   ├── azure-gpt54-proxy-hardening-plan.md  # 2026-09-14 hardening implementation plan
│   │   ├── community-install-lifecycle.md
│   │   ├── launchagent-design.md
│   │   └── launchagent-implementation-plan.md
│   ├── reference/
│   │   ├── optimized-goal-prompt.md
│   │   ├── oss-install-script-best-practices.md
│   │   └── proxy-install-notes.md
│   └── reports/
│       ├── copilot-adversarial-review.md         # Adversarial review by Copilot CLI + Claude Sonnet 5
│       ├── launchagent-implementation-report.md
│       └── rca-azure-gpt54-router-fallbacks.md   # Comprehensive Root Cause Analysis
├── service/
│   └── bin/
│       ├── preflight-litellm.sh         # Validates credentials & config translation contracts
│       ├── smoke-anthropic-to-azure.sh  # Live translation smoke test
│       ├── start-litellm.sh             # Launchd execution wrapper
│       └── test-launchagent-hardening.sh# Daemon revival & sleep assertion test
├── tests/
│   ├── test_config_validation.py        # Configuration hardening assertions
│   ├── test_live_proxy.py               # Live proxy health & error suppression tests
│   └── test_tool_choice_guard.py        # Unit tests for parameter sanitization guard
├── litellm-proxy-install.sh             # Self-contained installer
├── pyproject.toml                       # Python project configuration (uv managed)
├── tool_choice_guard.py                 # Active pre-call callback guard
└── README.md
```

---

## Quick Start

### Installation

```bash
chmod +x litellm-proxy-install.sh
./litellm-proxy-install.sh
```

### Running Tests

All tests are managed via `uv`:

```bash
uv run pytest tests/ -v
```

### Managing the Service

- **Check Health:**
  ```bash
  curl http://127.0.0.1:5596/health
  ```
- **Restart / Reload Daemon:**
  ```bash
  launchctl kickstart -k gui/$(id -u)/com.thedawgctor.litellm-proxy
  ```
- **Inspect Logs:**
  ```bash
  tail -f ~/.litellm/service/logs/litellm.stderr.log
  ```

---

## Forensic Analysis & Learnings

- **Full RCA:** Read [`docs/reports/rca-azure-gpt54-router-fallbacks.md`](docs/reports/rca-azure-gpt54-router-fallbacks.md) for forensic evidence detailing the string mutation bug in `router.py`, the macOS `DarkWake from Deep Idle` timing analysis, and the Azure Responses API schema breakdown.
- **Adversarial Verification:** Read [`docs/reports/copilot-adversarial-review.md`](docs/reports/copilot-adversarial-review.md) for the critique generated by Claude Sonnet 5 via GitHub Copilot CLI.
- **Hardening Plan:** Read [`docs/design/azure-gpt54-proxy-hardening-plan.md`](docs/design/azure-gpt54-proxy-hardening-plan.md) for the phased task list.
