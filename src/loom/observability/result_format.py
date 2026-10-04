"""Extract final report bodies and format structured output without UI dependencies."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_REPORT_FIELDS = ("report", "markdown", "final_answer", "answer", "content")
_WRAPPER_FIELDS = ("result", "output", "value", "data")
_WRAPPER_METADATA = frozenset((*_WRAPPER_FIELDS, "ok", "success", "status", "format", "type"))
_JSON_FENCE = re.compile(r"^(`{3,}|~{3,})(?:json)?\s*\n(.*?)\n\1\s*$", re.DOTALL | re.IGNORECASE)
_MARKDOWN_FENCE = re.compile(r"^(`{3,}|~{3,})(?:markdown|md)\s*\n(.*?)\n\1\s*$", re.DOTALL | re.IGNORECASE)


def _decode(value: Any) -> Any:
    # Handle quoted JSON inside JSON as well as a complete fenced JSON response.
    # Plain Markdown with embedded JSON examples remains intact.
    for _ in range(12):
        if not isinstance(value, str):
            break
        text = value.strip()
        fence = _JSON_FENCE.fullmatch(text)
        candidate = fence[2] if fence else text
        if not candidate.startswith(("{", "[", '"')):
            break
        try:
            decoded = json.loads(candidate)
        except (ValueError, TypeError):
            break
        if not isinstance(decoded, str | dict | list) or decoded == value:
            break
        value = decoded
    return value


def extract_report_content(value: Any, *, _depth: int = 0) -> Any | None:
    """Find report payloads in known task envelopes without dropping ordinary data."""
    if _depth >= 12:
        return None
    value = _decode(value)
    if not isinstance(value, Mapping):
        return None
    for name in _REPORT_FIELDS:
        content = value.get(name)
        if isinstance(content, str) and content.strip() or isinstance(content, Mapping | list | tuple):
            return normalize_result(content, _depth=_depth + 1)
    action = value.get("action")
    if isinstance(action, Mapping) and action.get("kind") != "tool":
        report = extract_report_content(action.get("input"), _depth=_depth + 1)
        if report is not None:
            return report
    wrappers = [name for name in _WRAPPER_FIELDS if name in value]
    if len(wrappers) == 1 and set(value).issubset(_WRAPPER_METADATA):
        return normalize_result(value[wrappers[0]], _depth=_depth + 1)
    return None


def normalize_result(value: Any, *, _depth: int = 0) -> Any:
    value = _decode(value)
    report = extract_report_content(value, _depth=_depth) if _depth < 12 else None
    return value if report is None else report


def format_result_text(value: Any) -> str:
    """Return decoded Markdown/text or valid, indented JSON for CLI and storage."""
    value = normalize_result(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping | list | tuple):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return "" if value is None else str(value)


def render_result_markdown(value: Any) -> str:
    """Use Markdown for prose and a highlighted JSON block for structured values."""
    value = normalize_result(value)
    if isinstance(value, str) and (fence := _MARKDOWN_FENCE.fullmatch(value.strip())):
        return fence[2]
    if not isinstance(value, Mapping | list | tuple):
        return format_result_text(value)
    content = json.dumps(value, ensure_ascii=False, indent=2)
    fence = "`" * max(3, 1 + max((len(match[0]) for match in re.finditer(r"`+", content)), default=0))
    return f"{fence}json\n{content}\n{fence}"
