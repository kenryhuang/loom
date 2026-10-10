"""Immutable-source evidence access. Never executes or follows trace content."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from loom.core import freeze_json, thaw_json
from loom.trace_analysis.records import normalize_record
from loom.trace_analysis.schemas import NormalizedEvent


def text_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(thaw_json(value), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class EvidencePointer:
    source_sha256: str
    line_number: int
    event_hash: str | None = None
    field_path: str | None = None
    start: int | None = None
    end: int | None = None


class EvidenceStore:
    """Snapshot a registered trace once; references resolve against that snapshot."""

    def __init__(self, path: Path, data: bytes):
        self.path = path
        self.source_bytes = data
        self.source_sha256 = hashlib.sha256(data).hexdigest()
        self._by_line: dict[int, NormalizedEvent] = {}
        self.coverage: dict[str, Any] = {"hash_mismatches": [], "missing_hash_lines": [], "repeated_record_lines": []}
        seen: set[str] = set()
        for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("record must be an object")
            except ValueError as exc:
                raise ValueError(f"Invalid trace at line {number}: {exc}") from exc
            event = normalize_record(freeze_json(raw), line_number=number)
            self._by_line[number] = event
            fingerprint = json.dumps(raw, sort_keys=True, separators=(",", ":"))
            if fingerprint in seen:
                self.coverage["repeated_record_lines"].append(number)
            seen.add(fingerprint)
            if event.hash is None:
                self.coverage["missing_hash_lines"].append(number)
            else:
                payload = raw.get("payload", raw)
                calculated = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if calculated != event.hash:
                    self.coverage["hash_mismatches"].append(number)
        self.events = tuple(self._by_line.values())
        self.coverage["event_count"] = len(self.events)
        self.coverage["source_status"] = "empty" if not self.events else "recorded"

    @classmethod
    def open(cls, path: str | Path) -> EvidenceStore:
        path = Path(path)
        return cls(path, path.read_bytes())

    def ref(self, event: NormalizedEvent, field_path: str | None = None, *, start: int | None = None,
            end: int | None = None) -> EvidencePointer:
        if event.line_number is None or self._by_line.get(event.line_number) != event:
            raise ValueError("Evidence event is not registered in this source")
        pointer = EvidencePointer(self.source_sha256, event.line_number, event.hash, field_path, start, end)
        self.resolve(pointer)
        return pointer

    def pointer(self, value: Mapping[str, Any] | EvidencePointer) -> EvidencePointer:
        if isinstance(value, EvidencePointer):
            pointer = value
        else:
            try:
                pointer = EvidencePointer(**dict(value))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid evidence pointer: {exc}") from exc
        self.resolve(pointer)
        return pointer

    def resolve(self, ref: EvidencePointer | Mapping[str, Any]) -> Any:
        if not isinstance(ref, EvidencePointer):
            return self.resolve(self.pointer(ref))
        if ref.source_sha256 != self.source_sha256:
            raise ValueError("Evidence source digest does not match registered source")
        if isinstance(ref.line_number, bool) or not isinstance(ref.line_number, int):
            raise ValueError("Evidence line must be an integer")
        event = self._by_line.get(ref.line_number)
        if event is None:
            raise ValueError("Evidence line is not a registered record")
        if ref.event_hash != event.hash:
            raise ValueError("Evidence event hash does not match record")
        current: Any = event.payload
        if ref.field_path is not None:
            if not isinstance(ref.field_path, str) or not ref.field_path:
                raise ValueError("Invalid evidence field path")
            for part in ref.field_path.split("."):
                if isinstance(current, Mapping) and part in current:
                    current = current[part]
                elif isinstance(current, list | tuple) and part.isdigit() and int(part) < len(current):
                    current = current[int(part)]
                else:
                    raise ValueError(f"Evidence field does not exist: {ref.field_path}")
        if ref.start is not None or ref.end is not None:
            text = text_value(current)
            start, end = (0 if ref.start is None else ref.start), (len(text) if ref.end is None else ref.end)
            if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end)) or not 0 <= start <= end <= len(text):
                raise ValueError("Evidence character range is invalid")
            return text[start:end]
        return thaw_json(current)

    def read(self, ref: EvidencePointer | Mapping[str, Any], *, max_chars: int = 4000) -> dict[str, Any]:
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
            raise ValueError("Evidence read budget must be a positive integer")
        pointer = self.pointer(ref)
        text = text_value(self.resolve(pointer))
        returned_ref = {**asdict(pointer), "start": pointer.start or 0, "end": (pointer.start or 0) + min(max_chars, len(text))}
        return {"ref": asdict(pointer), "returned_ref": returned_ref, "content": text[:max_chars], "total_chars": len(text),
                "returned_chars": min(max_chars, len(text)), "truncated": len(text) > max_chars}

    def event(self, ref: EvidencePointer | Mapping[str, Any]) -> NormalizedEvent:
        return self._by_line[self.pointer(ref).line_number]

    def index(self, *, excerpt_chars: int = 240) -> tuple[dict[str, Any], ...]:
        return tuple({"event_type": e.event_type, "run_id": e.run_id, "loop_id": e.loop_id,
                      "trace_id": e.trace_id, "step_number": e.step_number, "llm_call_id": e.llm_call_id,
                      "tool_call_id": e.tool_call_id, **self.read(self.ref(e), max_chars=excerpt_chars)} for e in self.events)
