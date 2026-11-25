# GJC integration (2026-10-19) — surgical, proxy untouched

GJC (gajae-code) consumes this proxy as a custom OpenAI-compatible provider.
Changes were made ONLY on the GJC side:

- `~/.gjc/agent/models.yml` → provider `litellm`, baseUrl `http://127.0.0.1:5596/v1`,
  `api: openai-completions`, `compat.supportsStore: false`, apiKey `sk-litellm-local`
  (proxy currently runs without master-key auth — `LITELLM_MASTER_KEY` is not in the
  service `.env`, so the Bearer value is unchecked), ONE model: `antigravity-flash`.
- `~/.gjc/agent/config.yml` → `modelRoles.default: litellm/antigravity-flash`.
- Medium effort requires NO proxy change: the antigravity gateway's `resolveAgyModel`
  defaults flash-family requests to `gemini-3.8-flash-medium`.
- Proxy `drop_params: true` absorbs GJC's `store:false`.
- Azure-routed entries (claude-gpt-5 group, wildcards) are known dead tech debt
  (backlogged): untouched, unused, never relied on.
- The litellm widget facility was NOT used (incomplete/buggy at integration time).
- Verified: proxy chain curl test, `gjc -p` default test + gateway `[CHAT]` log
  evidence (`model=gemini-3.8-flash-medium`), Paseo end-to-end run, `gjc --smoke-test`.
- Future (NOT done here): regolo `glm5.2`/`qwen3.5-122b` registration + GJC role
  slots; effort-suffixed proxy entries if per-request effort control is ever needed;
  setting a real `LITELLM_MASTER_KEY` in `~/.litellm/service/.env` (then update the
  GJC apiKey) if the proxy ever gets exposed beyond loopback.
