# Dispatch for Explorer Survey 2

You are Explorer 2 in the Survey Phase.
Your working directory: /Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_explorer_survey_2
Project root: /Users/thedawgctor/Desktop/litellmproxy-multiharness
Original Request: /Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/ORIGINAL_REQUEST.md

Please read /Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/ORIGINAL_REQUEST.md completely.

Your Focus:
Survey Error Handling & Fallback Circuit Breaker (R2) and Prompt Flattening Sanitization & Trailing Hook Normalization (R3).
- Investigate upstream error handling in antigravity_bridge.py and litellmproxy: how `GetModelResponse` errors, timeouts, or empty responses `{}` are handled. Why is HTTP 200 with empty string `""` returned, and where should HTTP 502/504 be raised?
- Investigate LiteLLM configuration and router behavior in this codebase: how fallback routing is configured (`antigravity-flash -> antigravity-remote-flash-medium`), how `StripOrphanToolChoiceGuard` circuit breaker operates, and how it handles 502/504 vs empty 200 OK.
- Investigate prompt flattening in `messages_to_prompt`: how multi-turn messages are concatenated, where terminal `SYSTEM:` hook blocks (e.g. Caveman mode) cause confusion or 0-token loops, and how to normalize multi-turn prompts so they always end with an explicit user invocation.
- Investigate redundant system hook reminders (`CAVEMAN MODE ACTIVE`, statusline buddy tags) across conversational history and how to filter and deduplicate them.
- Document all file paths, functions, configurations, and edge cases.
- Write your findings to /Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_explorer_survey_2/analysis.md and handoff.md.
- Send a completion message back when done.

## 2026-10-26T00:17:13Z
[Message] timestamp=2026-10-26T00:17:13Z sender=15e96a95-ed90-42d5-9ead-d1b4d60233e3 priority=MESSAGE_PRIORITY_HIGH
You are Explorer 2 conducting the Survey Phase for Project Orchestrator.
Your working directory is: /Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_explorer_survey_2
Project root: /Users/thedawgctor/Desktop/litellmproxy-multiharness

MANDATORY INSTRUCTIONS:
1. You MUST read the original request at /Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/ORIGINAL_REQUEST.md before starting work.
2. You are read-only: do NOT modify source code files. You may write only to your working directory (/Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_explorer_survey_2).
3. If running any python commands or scripts, ONLY EVER USE `uv` as the entry point. Always rely on `uv` best practices.
4. Maintain your progress.md with "Last visited: [timestamp]" updates.

YOUR MISSION:
Survey Error Handling & Fallback Circuit Breaker (R2) and Prompt Flattening Sanitization & Trailing Hook Normalization (R3).
- Investigate upstream error handling in antigravity_bridge.py and litellmproxy: how GetModelResponse errors, timeouts, or empty responses {} are handled. Why is HTTP 200 with empty string "" returned, and where should HTTP 502/504 be raised?
- Investigate LiteLLM configuration and router behavior in this codebase: how fallback routing is configured (antigravity-flash -> antigravity-remote-flash-medium), how StripOrphanToolChoiceGuard circuit breaker operates, and how it handles 502/504 vs empty 200 OK.
- Investigate prompt flattening in messages_to_prompt: how multi-turn messages are concatenated, where terminal SYSTEM: hook blocks (e.g. Caveman mode) cause confusion or 0-token loops, and how to normalize multi-turn prompts so they always end with an explicit user invocation.
- Investigate redundant system hook reminders (CAVEMAN MODE ACTIVE, statusline buddy tags) across conversational history and how to filter and deduplicate them.
- Document all file paths, functions, configurations, and edge cases.
- Write your comprehensive report to /Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_explorer_survey_2/analysis.md and a self-contained handoff.md.
- Send a completion message back to the orchestrator via send_message when done.
