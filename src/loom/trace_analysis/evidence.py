"""Evidence reference helpers for trace analysis."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from loom.trace_analysis.schemas import EvidenceRef, NormalizedEvent


def value_at_path(value: Any, field_path: str | None) -> Any:
    if not field_path:
        return value
    current = value
    for part in field_path.split("."):
        current = _value_at_part(current, part)
        if current is None:
            return None
    return current


def evidence_ref_for_event(
    event: NormalizedEvent,
    *,
    field_path: str | None = None,
    max_excerpt_chars: int = 160,
) -> EvidenceRef:
    value = value_at_path(event.payload, field_path)
    return EvidenceRef(
        event_hash=event.hash,
        event_type=event.event_type,
        subject_id=evidence_subject_id(event),
        field_path=field_path,
        excerpt=_excerpt(value, max_chars=max_excerpt_chars),
    )


def evidence_subject_id(event: NormalizedEvent) -> str | None:
    if event.run_id is None:
        return None
    if event.event_type.startswith("tool."):
        call_identity = event.tool_call_id or event.tool_id or event.record_id
        if event.trace_id is None or event.step_number is None:
            return f"tool:{event.run_id}:{call_identity}"
        return f"tool:{event.run_id}:{event.trace_id}:{event.step_number}:{call_identity}"
    if event.llm_call_id:
        if event.trace_id is None or event.step_number is None:
            return f"llm:{event.run_id}:{event.llm_call_id}"
        return f"llm:{event.run_id}:{event.trace_id}:{event.step_number}:{event.llm_call_id}"
    if event.trace_id is not None and event.step_number is not None:
        return f"step:{event.run_id}:{event.trace_id}:{event.step_number}"
    return f"run:{event.run_id}"


def _value_at_part(value: Any, part: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(part)
    if isinstance(value, Sequence) and not isinstance(value, str):
        try:
            return value[int(part)]
        except (ValueError, IndexError):
            return None
    return None


def _excerpt(value: Any, *, max_chars: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return ""
    first_line = text.splitlines()[0] if "\n" in text else text
    if first_line != text:
        return _truncate(f"{first_line}...", max_chars=max_chars)
    return _truncate(text, max_chars=max_chars)


def _truncate(text: str, *, max_chars: int) -> str:
    if max_chars <= 3:
        return text[:max_chars]
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


__all__ = ["evidence_ref_for_event", "evidence_subject_id", "value_at_path"]
