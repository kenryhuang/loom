"""Bounded, sanitized trace slices. Nothing is read from the live workspace."""

import re

from loom.diagnostics.evidence import SECRET, redact
from loom.evaluation.evidence_store import text_value
from loom.evaluation.goal_loops.facts import identity

SKIP = {"messages", "reasoning", "reasoning_content", "image", "image_url", "data", "base64"}


def sanitize(value):
    """Reuse credential filtering and additionally exclude private-key blocks."""
    value = redact(value)
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)", "[REDACTED]", value, flags=re.S)
    return value


def card_evidence(source, card, registry):
    result, seen, remaining = [], set(), 4000

    def leaf(value, ref, depth=0):
        nonlocal remaining
        if remaining <= 0 or depth > 5 or len(result) >= 12:
            return
        if isinstance(value, dict):
            # Read useful result fields before transport metadata or large blobs.
            priority = {"error": 0, "stderr": 1, "stdout": 2, "content": 3, "exit_code": 4}
            for key in sorted(value, key=lambda k: priority.get(k, 5)):
                if key in SKIP or SECRET.search(key):
                    continue
                path = (ref.get("field_path") + "." if ref.get("field_path") else "") + key
                leaf(value[key], {**ref, "field_path": path, "start": None, "end": None}, depth + 1)
            return
        if isinstance(value, list):
            for i, child in enumerate(value[:8]):
                path = (ref.get("field_path") + "." if ref.get("field_path") else "") + str(i)
                leaf(child, {**ref, "field_path": path, "start": None, "end": None}, depth + 1)
            return
        if value is None:
            return
        text = text_value(value)
        # Sanitize before slicing: a credential crossing a slice boundary must not leak.
        clean = sanitize(text)
        if clean != text:
            # Replacing secret spans changes offsets; preserve the whole source pointer,
            # and never permit a quote containing a redaction marker.
            slices = [(0, min(len(clean), 1200))]
        else:
            slices = [(0, min(len(text), 1200))]
            if len(text) > 1200:
                slices.append((max(1200, len(text) - 1200), len(text)))
        for start, end in slices:
            end = min(end, start + remaining)
            if end <= start:
                continue
            pointer = ref if clean != text else {**ref, "start": (ref.get("start") or 0) + start, "end": (ref.get("start") or 0) + end}
            key = "m" + identity(pointer)[:16]
            if key in seen:
                continue
            seen.add(key)
            snippet = clean[start:end]
            event_type = source.event(ref).event_type
            basis = (
                "model_claim"
                if card["kind"] == "model"
                else "observed_output"
                if event_type in {"tool.completed", "tool.failed"}
                else "recorded_input_or_boundary"
            )
            result.append(dict(id=key, ref=pointer, text=snippet, action_id=card["id"], basis=basis, truncated=end - start < len(text), redacted=clean != text))
            remaining -= len(snippet)

    for key in dict.fromkeys([*reversed(card.get("raw_refs", [])), *card.get("refs", [])]):
        if key not in registry:
            continue
        ref = registry[key]["ref"]
        if any(part in SKIP or SECRET.search(part) for part in (ref.get("field_path") or "").split(".")):
            continue
        leaf(source.resolve(ref), ref)
    return result
