from __future__ import annotations

from typing import Any, List, Optional, Set

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger


class StripOrphanToolChoiceGuard(CustomLogger):
    """Drop tool-only params when a request doesn't actually include tools.

    Also sanitize Responses API ``input`` arrays to remove
    ``function_call_output`` items that have a missing/null ``call_id``
    or whose ``call_id`` doesn't reference a preceding ``function_call``
    in the same input array.  This prevents Azure OpenAI from returning
    HTTP 400 ("Invalid Value: 'input.call_id'. Function call output
    requires call_id.") when Codex Desktop's auto-compact truncation
    removes earlier ``function_call`` turns while leaving their outputs.
    """

    # ------------------------------------------------------------------ #
    #  tool_choice guard (existing)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _has_tools(tools: Any) -> bool:
        if tools is None:
            return False
        if isinstance(tools, list):
            return len(tools) > 0
        return bool(tools)

    # ------------------------------------------------------------------ #
    #  Responses API input sanitizer (NEW)
    # ------------------------------------------------------------------ #

    CALL_ITEM_TYPES: Set[str] = {
        "function_call",
        "custom_tool_call",
        "google_search_call",
        "mcp_server_tool_call",
        "server_tool_use",
    }

    RESULT_ITEM_TYPES: Set[str] = {
        "function_call_output",
        "custom_tool_call_output",
        "google_search_result",
        "mcp_server_tool_result",
        "file_search_result",
    }

    @classmethod
    def _is_call_item(cls, item_type: Any) -> bool:
        if not isinstance(item_type, str):
            return False
        return item_type in cls.CALL_ITEM_TYPES or item_type.endswith("_call")

    @classmethod
    def _is_result_item(cls, item_type: Any) -> bool:
        if not isinstance(item_type, str):
            return False
        return (
            item_type in cls.RESULT_ITEM_TYPES
            or item_type.endswith("_result")
            or item_type.endswith("_call_output")
        )

    @staticmethod
    def _sanitize_responses_input(input_items: List[Any]) -> List[Any]:
        """Return a cleaned copy of the Responses API ``input`` list.

        Rules:
        1. Every result-like tool output item MUST have a non-empty
           reference id (``call_id`` or ``tool_use_id``).
        2. Every result-like tool output reference id MUST point to a
           tool-call item that appears *earlier* in the same list.

        Items that violate either rule are silently dropped and logged.
        """
        # First pass: collect all valid call ids from tool call items
        valid_call_ids: Set[str] = set()
        for item in input_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if StripOrphanToolChoiceGuard._is_call_item(item_type):
                call_id = item.get("call_id") or item.get("id") or item.get("tool_use_id")
                if call_id:
                    valid_call_ids.add(str(call_id))

        # Second pass: filter out broken result items referencing missing calls
        sanitized: List[Any] = []
        dropped_count = 0
        for item in input_items:
            if not isinstance(item, dict):
                sanitized.append(item)
                continue

            item_type = item.get("type")

            if StripOrphanToolChoiceGuard._is_result_item(item_type):
                call_id = item.get("call_id") or item.get("tool_use_id")
                # Rule 1: reference id must be present and non-empty
                if not call_id:
                    dropped_count += 1
                    verbose_proxy_logger.warning(
                        "StripOrphanToolChoiceGuard: dropped %s with missing/null call reference",
                        item_type,
                    )
                    continue
                # Rule 2: reference id must point to a known preceding tool call
                call_id_str = str(call_id)
                if call_id_str not in valid_call_ids:
                    dropped_count += 1
                    verbose_proxy_logger.warning(
                        "StripOrphanToolChoiceGuard: dropped orphaned "
                        "%s with call_id=%s (no matching tool call in input)",
                        item_type,
                        call_id_str,
                    )
                    continue

            sanitized.append(item)

        if dropped_count > 0:
            verbose_proxy_logger.info(
                "StripOrphanToolChoiceGuard: sanitized Responses API input, "
                "dropped %d orphaned/invalid function_call_output item(s)",
                dropped_count,
            )

        return sanitized

    # ------------------------------------------------------------------ #
    #  main hook
    # ------------------------------------------------------------------ #

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: Optional[dict],
        call_type: str,
    ) -> Optional[dict]:
        if not isinstance(data, dict):
            return data

        # --- Universal parameter sanitizers for Azure OpenAI ---
        # Azure OpenAI rejects 'call_type' with HTTP 400
        if "call_type" in data:
            data.pop("call_type", None)
            verbose_proxy_logger.debug(
                "StripOrphanToolChoiceGuard removed unsupported 'call_type' (call_type=%s, model=%s)",
                call_type,
                data.get("model"),
            )

        # --- Responses API input sanitizer ---
        # For /v1/responses calls, data contains an "input" field (list of items)
        if call_type in ("aresponses", "_aresponses_websocket", "responses"):
            raw_input = data.get("input")
            if isinstance(raw_input, list) and raw_input:
                # Only run sanitizer if there are result-like items carrying
                # tool outputs or server-tool results.
                has_result_items = any(
                    isinstance(item, dict)
                    and self._is_result_item(item.get("type"))
                    for item in raw_input
                )
                if has_result_items:
                    data["input"] = self._sanitize_responses_input(raw_input)

        # --- Responses API unsupported parameter sanitizer ---
        # Azure Responses API rejects 'stop_sequences' with HTTP 400
        if "stop_sequences" in data:
            data.pop("stop_sequences", None)
            verbose_proxy_logger.debug(
                "StripOrphanToolChoiceGuard removed unsupported 'stop_sequences' (call_type=%s, model=%s)",
                call_type,
                data.get("model"),
            )

        # --- tool_choice guard across all endpoints ---
        if "tool_choice" in data or "parallel_tool_calls" in data:
            tools = data.get("tools")
            if not self._has_tools(tools):
                stripped = False
                for key in ("tool_choice", "parallel_tool_calls"):
                    if key in data:
                        data.pop(key, None)
                        stripped = True
                if stripped:
                    verbose_proxy_logger.debug(
                        "StripOrphanToolChoiceGuard removed tool params without tools "
                        "(call_type=%s, model=%s)",
                        call_type,
                        data.get("model"),
                    )

        return data


proxy_guard = StripOrphanToolChoiceGuard()
