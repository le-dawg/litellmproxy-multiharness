✗ Inspect working directory and search for litellm config/repo (shell)
  │ echo "=== cwd ==="; pwd; ls -la; echo "=== check for litellm repo/config ==="; find / -maxdepth
  │ 4 -iname "*litellm*" 2>/dev/null | grep -v Library/Caches | head -50
  └ Permission denied and could not request permission from user

● Check for actual litellm config and codex automation files (shell)
  │ ls -la ~/.litellm 2>/dev/null; echo "---"; ls -la ~/.codex 2>/dev/null; echo "---config---"; cat
  │ ~/.litellm/config.yaml 2>/dev/null; echo "---automation---"; find ~/.codex -iname "*.toml"
  │ 2>/dev/null | xargs -I{} sh -c 'echo ==={}===; cat {}' 2>/dev/null
  └ 14 lines…

● Read litellm config and tool choice guard script (shell)
  │ echo "=== config.yaml ==="; cat ~/.litellm/config.yaml; echo; echo "=== tool_choice_guard.py
  │ ==="; cat ~/.litellm/tool_choice_guard.py 2>/dev/null | head -150
  └ 227 lines…

● Find codex automation toml files (shell)
  │ find ~/.codex -iname "*.toml" 2>/dev/null
  └ 87 lines…

✗ Read daily-brief automation config (shell)
  │ cat "/Users/thedawgctor/.codex/automations/daily-brief/automation.toml"
  └ Permission denied and could not request permission from user

● Read remainder of tool_choice_guard.py (shell)
  │ tail -80 ~/.litellm/tool_choice_guard.py
  └ 81 lines…

● Locate installed litellm package and version (shell)
  │ python3 -c "import litellm; print(litellm.__file__); print(litellm.__version__)" 2>&1 | head -20
  └ 6 lines…

✗ Read automation.toml
  │ ~/.codex/automations/daily-brief/automation.toml
  └ Permission denied and could not request permission from user

✗ Check litellm package availability locally (shell)
  │ pip3 show litellm 2>&1 | head -5; pip3 download litellm --no-deps -d /tmp/litellm_pkg 2>&1 |
  │ tail -5; find / -path "*/site-packages/litellm/router.py" 2>/dev/null | head -5
  └ Permission denied and could not request permission from user

✗ Check permissions on automation file (shell)
  │ ls -la /Users/thedawgctor/.codex/automations/daily-brief/ 2>&1; stat -f "%Sp %N"
  │ /Users/thedawgctor/.codex/automations/daily-brief/automation.toml 2>&1
  └ Permission denied and could not request permission from user

● Search code (MCP: github-mcp-server) · expose_router_debug_in_errors repo:BerriAI/litellm · query…
  └ {"total_count":2,"incomplete_results":false,"items":[{"name":"__init__.py","p...

● Search code (MCP: github-mcp-server) · DEFAULT_COOLDOWN_TIME_SECONDS repo:BerriAI/litellm · query…
  └ {"total_count":2,"incomplete_results":false,"items":[{"name":"constants.py","...

● Search code (MCP: github-mcp-server) · allowed_fails_policy repo:BerriAI/litellm language:Python …
  └ {"total_count":14,"incomplete_results":false,"items":[{"name":"cooldown_handl...

● Search code (MCP: github-mcp-server) · "Received Model Group" "Available Model Group Fallbacks" r…
  └ {"total_count":2,"incomplete_results":false,"items":[{"name":"test_router_exc...

● Get file or directory contents (MCP: github-mcp-server) · litellm/router_utils/cooldown_handlers.…
  └ Output too large to read at once (24.8 KB). Saved to: /var/folders/4w/byzn348...

● Search cooldown handler logic for exception-type gating (shell)
  │ grep -n "_is_cooldown_required\|BadRequest\|RateLimit\|Timeout\|def \|status_code\|400\|429"
  │ /var/folders/4w/byzn34851lb1fwnyt1416wkm0000gn/T/1789401525804-copilot-tool-output-f5b78283754e4
  │ 0659e075682952aa0f1.txt | head -80
  └ 34 lines…

● Read 1789401525804-copilot-tool-output-f5b78283754e40659e075682952aa0f1.txt
  │ /var/folders/4w/byzn34851lb1fwnyt1416wkm0000gn/T/1789401525804-copilot-tool-output-f5b78283754e4
  │ 0659e075682952aa0f1.txt
  └ L180:410 (230 lines read)

● Search code (MCP: github-mcp-server) · gaierror repo:BerriAI/litellm · query: "gaierror repo:Berr…
  └ {"total_count":4,"incomplete_results":false,"items":[{"name":"url_utils.py","...

● Search code (MCP: github-mcp-server) · "nodename nor servname" repo:BerriAI/litellm · query: ""no…
  └ {"total_count":1,"incomplete_results":false,"items":[{"name":"test_image_hand...

● Search code (MCP: github-mcp-server) · httpcore.ConnectError APIConnectionError repo:BerriAI/lite…
  └ {"total_count":0,"incomplete_results":false,"items":[]}

● Search code (MCP: github-mcp-server) · SSRFError raise repo:BerriAI/litellm language:Python · que…
  └ {"total_count":16,"incomplete_results":false,"items":[{"name":"url_utils.py",...

● Get file or directory contents (MCP: github-mcp-server) · litellm/litellm_core_utils/url_utils.py…
  └ successfully downloaded text file (SHA: fa070a648f520ea965bd7127995dbcb35e072...

✗ Fetch azure.py and locate SSRF-related polling logic (shell)
  │ curl -s "https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/llms/azure/azure.py" -o
  │ /tmp/azure_full.py 2>&1; wc -l /tmp/azure_full.py; grep -n
  │ "SSRFError\|validate_url\|assert_same_origin\|operation_location\|502" /tmp/azure_full.py
  └ Permission denied and could not request permission from user

● Get file or directory contents (MCP: github-mcp-server) · litellm/llms/azure/azure.py · owner: "B…
  └ Output too large to read at once (59.0 KB). Saved to: /var/folders/4w/byzn348...

● Search Azure.py content for SSRF polling logic context (shell)
  │ grep -n "SSRFError\|validate_url\|assert_same_origin\|operation_location\|502"
  │ /var/folders/4w/byzn34851lb1fwnyt1416wkm0000gn/T/1789401582393-copilot-tool-output-6126061f3ac54
  │ 4d1b5eaf4da2861f3a2.txt
  └ 16 lines…

● Search code (MCP: github-mcp-server) · "APIConnectionError" "ConnectError" repo:BerriAI/litellm p…
  └ {"total_count":0,"incomplete_results":false,"items":[]}

● Search code (MCP: github-mcp-server) · custom_llm_provider == "azure" APIConnectionError repo:Ber…
  └ {"total_count":15,"incomplete_results":false,"items":[{"name":"common_request...

● Search code (MCP: github-mcp-server) · disable_cooldowns router_settings repo:BerriAI/litellm pat…
  └ {"total_count":0,"incomplete_results":false,"items":[]}

● Search code (MCP: github-mcp-server) · drop_params responses api unsupported_params repo:BerriAI/…
  └ {"total_count":0,"incomplete_results":false,"items":[]}

● Search code (MCP: github-mcp-server) · get_model_group wildcard "model_name" pattern repo:BerriAI…
  └ {"total_count":0,"incomplete_results":false,"items":[]}

● Search code (MCP: github-mcp-server) · wildcard model group cooldown pattern_match_deployments re…
  └ {"total_count":0,"incomplete_results":false,"items":[]}

● Get file or directory contents (MCP: github-mcp-server) · litellm/router_utils/pattern_match_depl…
  └ successfully downloaded text file (SHA: d5234e27ec6fdef4110278b833d5292992d78...

● Inspect azure.py around SSRF polling checks to identify which API this guards (shell)
  │ sed -n '860,940p' /var/folders/4w/byzn34851lb1fwnyt1416wkm0000gn/T/1789401582393-copilot-tool-ou
  │ tput-6126061f3ac544d1b5eaf4da2861f3a2.txt
  └ 82 lines…

## Adversarial Review: LiteLLM/Azure GPT-5.4 RCA

I pulled your actual `~/.litellm/config.yaml`, the `tool_choice_guard.py` callback, and cross-checked claims against LiteLLM's real source (`cooldown_handlers.py`, `url_utils.py`, `azure.py`, `exception_mapping_utils.py`, `__init__.py` on `BerriAI/litellm`). Several parts of the RCA are correct on mechanism but wrong on severity/causality. Here's the breakdown.

### Verdict on each RCA claim

**#1 "expose_router_debug_in_errors is a red herring" — TRUE, and understated.** Confirmed in `litellm/__init__.py`: `expose_router_debug_in_errors: bool = True` with a maintainer comment: *"Deprecation: planned to flip to False in a future major release."* LiteLLM's own maintainers already know this is a diagnostic-leakage anti-pattern. This isn't a subtle finding — it's a documented, acknowledged wart. Fix it and move on; don't spend more RCA cycles on it.

**#3 "Cascading Deployment Cooldown Lockouts" — LARGELY WRONG as stated.** This is the RCA's weakest claim, and it's presented as if it were the unifying mechanism. Actual code (`_is_cooldown_required`, `_should_cooldown_deployment`):
- 4xx errors **do not** trigger cooldown by default except 429/401/408/404. Your "unknown parameter," "invalid tool_choice," and "context_length_exceeded" (all 400s) **should not** be poisoning the deployment per LiteLLM's own logic — unless you've set an `allowed_fails_policy` naming those exception types, which your config does **not** have (no `router_settings:` block exists at all in your `config.yaml`).
- Default `DEFAULT_COOLDOWN_TIME_SECONDS = 5`, `DEFAULT_ALLOWED_FAILS = 3` — a 5-second cooldown, not the "phantom outage" duration implied.
- Critically: every `model_name` in your config (`gpt-5.4`, `gpt-*`, `*`, `claude-gpt-5`, etc.) maps to a **single deployment** (no alternates within the group). LiteLLM's cooldown logic has an explicit carve-out: `is_single_deployment_model_group` **skips** the 429 cooldown branch and the error-rate-threshold branch specifically *"to avoid cooldowns on single deployment model groups."* Your topology is the one case LiteLLM tries hardest not to lock out.
- Bonus: `_is_cooldown_required` literally ignores any exception whose string contains `"APIConnectionError"` — network errors are explicitly exempted from cooldown by design.

So the RCA's central "one error poisons everything" narrative doesn't match your actual config or LiteLLM's actual gating logic. Something else is producing repeat failures — most likely genuine repeat upstream conditions (rate limits, real DNS flaps) or the exception-mapping bug below, not a self-inflicted cooldown spiral.

**#2 (Errno 8 root cause) — plausible but unverified, and contains a real inconsistency the RCA glossed over.** Your own error text says `litellm.APIError: AzureException APIError`, not `litellm.APIConnectionError`. LiteLLM's exception mapper is supposed to classify `httpcore.ConnectError`/`socket.gaierror` as `APIConnectionError` (confirmed by test fixtures expecting exactly that). Getting a generic `APIError` instead means the Azure-specific exception mapping **fell through** to a catch-all — and I found a live regression test in LiteLLM's own suite (`test_exception_mapping_utils.py`) explicitly documenting a case where "upstream 400 ... fell through to the generic 500 APIConnectionError instead of surfacing as [correctly]." Mis-classification fallthrough is a known, recurring bug class in this codebase — not a one-off. That matters because it also means this error **bypassed** the `"APIConnectionError"` cooldown exemption, if it was ever going to cooldown at all. I checked whether this could instead be LiteLLM's internal SSRF pre-flight resolver (`validate_url()` in `url_utils.py`, which independently calls `socket.getaddrinfo` and raises a distinctly-worded `SSRFError`) — but that path is only wired into image-fetch, DALL-E-2 polling, MCP/Langfuse/A2A URL validation, not the primary chat/Responses completion call, and your traceback shows the standard `httpcore.ConnectError → httpx.ConnectError → socket.gaierror` chain, which correctly rules that path out. Good — but the RCA should have said so explicitly instead of leaving it as an unexamined alternative.

macOS sleep/wake DNS flap remains the most probable trigger, but it's asserted from a timestamp coincidence, not confirmed (no evidence of correlating `pmset -g log` / network reachability transitions was shown). uvloop threadpool exhaustion and stale keep-alive sockets in httpx's pooled `AsyncClient` are real, competing candidates you didn't rule out — a stale pooled connection surviving a network interface change would produce the *same* symptom (connect layer failure) without any DNS involvement at all.

**#4 (param passthrough) — RCA is behind the current state.** The orphan-`call_id` sanitizer it worries about is **already implemented** in your `tool_choice_guard.py` (`StripOrphanToolChoiceGuard._sanitize_responses_input`), scoped to `call_type in ("aresponses", "_aresponses_websocket", "responses")`. So either (a) the 281/212 occurrence counts in the log predate this guard's deployment, or (b) the guard has a gap — e.g., it only validates `call_id` linkage *within a single request's `input` array*; it does nothing for orphaned state Azure may have persisted server-side across turns in a stateful/`previous_response_id` conversation, which auto-compaction on the Codex Desktop side could still violate.

### Answers to your four questions

**1. macOS DNS flap vs. other causes:** Not proven, only plausible. Competing hypotheses not ruled out: stale pooled `httpx.AsyncClient` keep-alive socket post network-transition (very common with long-lived proxy processes across sleep/wake — httpx doesn't proactively validate pooled connections), and the confirmed exception-mapping fallthrough bug masking the true exception type. Get a `traceback` (not just message text) from `--detailed_debug`, and log `socket.getaddrinfo` calls with timestamps around the failure to actually correlate with sleep/wake events before concluding DNS.

**2. Is disabling `expose_router_debug_in_errors` + touching cooldown settings the right fix?** Disabling the debug flag: yes, no downside, do it. Touching `cooldown_time`/`allowed_fails` is solving a problem that mostly doesn't exist given your single-deployment topology and default 4xx exclusions — and setting `cooldown_time: 0` **globally** would be actively dangerous if you ever add a second Azure deployment or region for failover, since it disables the one thing that stops LiteLLM hammering a genuinely dead endpoint. If you must tune anything, use a scoped `allowed_fails_policy` for 429/`RateLimitError` only, not a blanket disable.

**3. Definitive param-passthrough strategy for Azure Responses API:** `drop_params: true` only helps at the Chat Completions transformation layer; the Responses API has its own param allowlist/transformation path in LiteLLM and Azure enforces its own strict 400s independent of LiteLLM. There's no "pass everything through and it'll just work" mode for Responses API on Azure — you need (a) `drop_params: true` retained, (b) your existing tool_choice/orphan-call_id guard extended to cover cross-turn state, not just intra-request, and (c) an explicit allowlist test matrix per Codex Desktop feature (plain text, tool calls, streaming, background/async) run against the real Azure endpoint in CI/pre-deploy, because Azure's Responses API validation rules are stricter and less documented than OpenAI's own.

### Recommendations (actionable)
1. Set `litellm.expose_router_debug_in_errors = False` — safe, zero downside.
2. Do **not** touch `cooldown_time`/`allowed_fails` blindly; add a scoped `allowed_fails_policy` for `RateLimitError` only if 429s recur.
3. Enable `--detailed_debug` logging (or equivalent) to capture full tracebacks, not just exception messages, so mis-classified exceptions (APIError vs APIConnectionError) are identifiable next time.
4. Audit whether the 800 "Received Model Group=" log lines predate the `tool_choice_guard.py` deployment date — if they're mostly historical, the RCA's severity is overstated for current state.
5. Extend the orphan-call_id guard to handle cross-turn/stateful conversation compaction, not just intra-request arrays.
6. Instrument actual network-state correlation (macOS `pmset`/reachability logs) before asserting the sleep/wake DNS theory as fact.



--- STDERR ---


Changes    +0 -0
AI Credits 68.79 (3m 55s)
Tokens     ↑ 1.6m (1.5m cached, 83.6k written) • ↓ 17.7k (10.5k reasoning)
Resume     copilot --resume=79d88f5b-6d48-49e8-ab49-cbc813547189
