"""Bound request metadata independently of trace length; account for schema too."""

import json

from loom.llm.structured import native_schema

STATE_CHARS = 6000
REQUEST_CHARS = 24000


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bounded_state(state):
    """The complete graph stays local. Truncation of a selected view is explicit."""
    trimmed = 0

    def short(text, limit):
        nonlocal trimmed
        text = str(text)
        if len(text) > limit:
            trimmed += 1
        return text[:limit]

    def conditions(values):
        nonlocal trimmed
        trimmed += max(0, len(values) - 6)
        return [short(serialized({k: v[k] for k in ("id", "description", "required") if k in v}) if isinstance(v, dict) else v, 200) for v in values[:6]]

    counts = {k: len(state[k]) for k in ("roots", "goals", "stages", "closures", "older_goal_index")}
    out = {
        "roots": [{**r, "objective": short(r["objective"], 800), "conditions": conditions(r["conditions"])} for r in state["roots"][-4:]],
        "goals": [{**r, "objective": short(r["objective"], 240), "conditions": conditions(r["conditions"])} for r in state["goals"]],
        "stages": [{**r, "title": short(r["title"], 160)} for r in state["stages"]],
        "closures": [{**r, "open_questions": [short(q, 160) for q in r["open_questions"][:3]]} for r in state["closures"]],
        "older_goal_index": state["older_goal_index"],
        "omitted_goals": state["omitted_goals"],
        "state_limits": {"max_chars": STATE_CHARS, "omitted_rows": {}, "truncated_fields": trimmed},
    }
    # Drop low-priority retrieval/context rows before shortening an active root.
    for key in ("older_goal_index", "closures", "stages", "goals", "roots"):
        minimum = 1 if key == "roots" else 0
        while len(serialized(out)) > STATE_CHARS - 300 and len(out[key]) > minimum:
            out[key].pop(0 if key == "roots" else -1)
    out["state_limits"]["omitted_rows"] = {k: n - len(out[k]) for k, n in counts.items() if n > len(out[k])}
    return out


def request_profile(messages, payload, mode, protocol):
    schema = None
    if mode == "json_schema":
        schema = {
            "type": "json_schema",
            "json_schema": {"name": f"{protocol.name}_v{protocol.version}", "strict": True, "schema": native_schema(protocol.schema)},
        }
    elif mode == "json_object":
        schema = {"type": "json_object"}
    schema_text = serialized(schema) if schema else ""
    texts = [m.content for m in messages]
    parts = {
        "system": len(texts[0]),
        "contract": len(serialized(payload["contract"])) if "contract" in payload else 0,
        "cards": len(serialized(payload["cards"])),
        "state": len(serialized({k: payload[k] for k in ("roots", "goals", "stages", "closures", "older_goal_index", "state_limits")})),
        "frontier": len(serialized({k: payload[k] for k in ("frontier", "boundary_evidence", "prior_actions") if k in payload})),
        "extra_evidence": len(serialized(payload["extra_evidence"])),
        "response_schema": len(schema_text),
    }
    total = sum(map(len, texts)) + len(schema_text)
    parts["envelope_and_control"] = total - sum(parts.values())
    return dict(
        card_count=len(payload["cards"]),
        message_chars=sum(map(len, texts)),
        schema_chars=len(schema_text),
        total_chars=total,
        input_estimate=sum(len(t.encode("utf-8")) for t in [*texts, schema_text]) + 512,
        estimation="utf8_bytes_including_response_schema_plus_512_not_tokenizer",
        components_chars=parts,
    )
