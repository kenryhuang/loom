"""Historical raw-evidence read API; not invoked by the metadata-only pipeline."""

from jsonschema import Draft202012Validator

from loom.evaluation.evidence_store import text_value
from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.goal_loops.graph_contract import REQUEST


def read_requests(source, registry, requests, *, cutoff, inspected, remaining_chars):
    """Validate the complete plan before reading; aliases cannot bypass range dedup."""
    pending, used = [], 0
    for request in requests:
        Draft202012Validator(REQUEST).validate(request)
        if not request["question"].strip() or not request["purpose"].strip():
            raise ValueError("Evidence read requires a question and purpose")
        if request["ref"] not in registry or registry[request["ref"]]["line"] > cutoff:
            raise ValueError("Cannot read unregistered or future evidence")
        base = registry[request["ref"]]["ref"]
        text = text_value(source.resolve(base))
        if request["start"] >= len(text):
            raise ValueError("Evidence range is outside the registered field")
        start = (base.get("start") or 0) + request["start"]
        end = start + min(request["length"], len(text) - request["start"])
        pointer = {**base, "start": start, "end": end}
        if any(
            r["line_number"] == pointer["line_number"]
            and r.get("field_path") == pointer.get("field_path")
            and (r.get("start") or 0) <= start
            and (r.get("end") or 0) >= end
            for r in inspected + [p["ref"] for p in pending]
        ):
            raise ValueError("Evidence range was already delivered; request an unseen range or conclude unknown")
        used += end - start
        if used > remaining_chars:
            raise ValueError("Evidence read budget reached")
        pending.append(
            dict(
                id="e" + identity(pointer)[:12],
                ref=pointer,
                line=pointer["line_number"],
                text=text[request["start"] : request["start"] + end - start],
                question=request["question"],
            )
        )
    return pending


