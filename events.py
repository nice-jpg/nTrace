"""Trace event construction and LangChain value normalization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

SCHEMA_VERSION = 1


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def json_value(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Best-effort JSON conversion without allowing trace failures into the agent."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime,)):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (Path, Enum)):
        return str(value.value if isinstance(value, Enum) else value)

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return "<recursive>"
    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            return {str(key): json_value(item, _seen=seen) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [json_value(item, _seen=seen) for item in value]
        if is_dataclass(value):
            return json_value(asdict(value), _seen=seen)
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return json_value(model_dump(mode="json"), _seen=seen)
        if hasattr(value, "__dict__"):
            public = {
                key: item for key, item in vars(value).items() if not str(key).startswith("_")
            }
            if public:
                return {
                    "type": type(value).__name__,
                    **json_value(public, _seen=seen),
                }
        return str(value)
    except Exception:  # noqa: BLE001 - telemetry serialization is fail-open.
        return f"<{type(value).__name__}>"
    finally:
        seen.discard(identity)


def message_role(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("role") or message.get("type") or "")
    return str(getattr(message, "type", None) or getattr(message, "role", None) or "")


def message_content(message: Any) -> Any:
    return message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)


def _message_field(message: Any, field: str) -> Any:
    return message.get(field) if isinstance(message, Mapping) else getattr(message, field, None)


def _function_call(call: Any) -> dict[str, Any]:
    payload = call if isinstance(call, Mapping) else {}
    return {
        "type": "function_call",
        "name": json_value(payload.get("name")),
        "arguments": json_value(payload.get("arguments", payload.get("args"))),
    }


def user_inputs(messages: Sequence[Any]) -> list[Any]:
    """Serialize model inputs as typed Responses-style items."""

    inputs: list[Any] = []
    for message in messages:
        content = message_content(message)
        role = message_role(message)
        if content not in (None, "", []):
            inputs.append({"type": "message", "role": role, "content": json_value(content)})

        additional_kwargs = _message_field(message, "additional_kwargs")
        if isinstance(additional_kwargs, Mapping):
            for reasoning in additional_kwargs.get("reasoning_items") or []:
                inputs.append(json_value(reasoning))

        for call in _message_field(message, "tool_calls") or []:
            inputs.append(_function_call(call))
    return inputs


def tool_calls(messages: Sequence[Any]) -> list[Any]:
    calls: list[Any] = []
    for message in messages:
        raw = message.get("tool_calls") if isinstance(message, Mapping) else getattr(message, "tool_calls", None)
        if raw:
            calls.extend(json_value(raw))
    return calls


def tool_results(messages: Sequence[Any]) -> list[Any]:
    return [json_value(message) for message in messages if message_role(message).lower() == "tool"]


def current_tool_exchange(messages: Sequence[Any]) -> tuple[list[Any], list[Any]]:
    """Return only the most recent tool-call turn and its matching results."""

    call_index: int | None = None
    calls: list[Any] = []
    for index in range(len(messages) - 1, -1, -1):
        raw_calls = _message_field(messages[index], "tool_calls") or []
        if raw_calls:
            call_index = index
            calls = list(json_value(raw_calls))
            break
    if call_index is None:
        return [], []

    call_ids = {
        str(call.get("id"))
        for call in calls
        if isinstance(call, Mapping) and call.get("id") is not None
    }
    results: list[Any] = []
    for message in messages[call_index + 1:]:
        if message_role(message).lower() != "tool":
            continue
        result = json_value(message)
        result_id = _message_field(message, "tool_call_id")
        if not call_ids or result_id is None or str(result_id) in call_ids:
            results.append(result)
    return calls, results


def latest_output(messages: Sequence[Any]) -> Any:
    for message in reversed(messages):
        if message_role(message).lower() in {"assistant", "ai"}:
            return json_value(message_content(message))
    return None


def normalize_token_usage(messages: Sequence[Any]) -> dict[str, Any]:
    for message in reversed(messages):
        usage = getattr(message, "usage_metadata", None)
        metadata = getattr(message, "response_metadata", None)
        if not usage and isinstance(message, Mapping):
            usage = message.get("usage_metadata")
            metadata = message.get("response_metadata")
        if not usage and isinstance(metadata, Mapping):
            usage = metadata.get("token_usage") or metadata.get("usage")
        if not isinstance(usage, Mapping):
            continue
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
        total_tokens = usage.get("total_tokens")
        if total_tokens is None:
            try:
                total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
            except (TypeError, ValueError):
                total_tokens = 0
        return {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "total_tokens": total_tokens or 0,
            "details": json_value(usage),
        }
    return {}


def tools_payload(tools: Sequence[BaseTool] | None) -> list[dict[str, Any]]:
    return [
        {
            "name": json_value(tool.name),
            "description": json_value(tool.description),
        }
        for tool in (tools or [])
    ]


__all__ = [
    "SCHEMA_VERSION",
    "current_tool_exchange",
    "json_value",
    "latest_output",
    "normalize_token_usage",
    "tool_calls",
    "tool_results",
    "tools_payload",
    "user_inputs",
    "utc_timestamp",
]
