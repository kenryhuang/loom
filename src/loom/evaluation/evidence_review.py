"""Track which original character ranges were actually supplied to an evaluator."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from loom.evaluation.evidence_store import EvidencePointer, EvidenceStore, text_value


def pointers_in(value: Any):
    if isinstance(value, Mapping):
        if "source_sha256" in value and "line_number" in value:
            yield value
        else:
            for item in value.values():
                yield from pointers_in(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from pointers_in(item)


class EvidenceReview:
    def __init__(self, store: EvidenceStore):
        self.store = store
        self.spans: dict[tuple[int, str | None], list[tuple[int, int]]] = {}

    def present(self, ref: EvidencePointer | Mapping[str, Any], *, max_chars: int) -> dict[str, Any]:
        ref = self.store.pointer(ref)
        result = self.store.read(ref, max_chars=max_chars)
        start = ref.start or 0
        self.spans.setdefault((ref.line_number, ref.field_path), []).append((start, start + result["returned_chars"]))
        return result

    def covers(self, ref: EvidencePointer | Mapping[str, Any]) -> bool:
        ref = self.store.pointer(ref)
        for (line, field), spans in self.spans.items():
            if line != ref.line_number:
                continue
            if field == ref.field_path:
                whole = self.store.resolve(replace(ref, start=None, end=None))
                start = ref.start or 0
                end = len(text_value(whole)) if ref.end is None else ref.end
            elif field is None or (ref.field_path is not None and ref.field_path.startswith(field + ".")):
                whole = self.store.resolve(replace(ref, field_path=field, start=None, end=None))
                start, end = 0, len(text_value(whole))
            else:
                continue
            cursor = start
            for low, high in sorted(spans):
                if low > cursor:
                    break
                cursor = max(cursor, high)
                if cursor >= end:
                    return True
        return False
