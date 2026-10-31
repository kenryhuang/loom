"""Small judge inputs. Full traces, outputs and graph history stay on the host."""

import json
from dataclasses import asdict

from loom.evaluation.evidence_store import text_value
from loom.evaluation.framework import card_policy
from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.goal_loops.graph_normalization import terminal_metadata_records as terminal_metadata_records
from loom.evaluation.goal_loops.packing import serialized

VERSION = "action-metadata.2"


def card_metadata(source, card, registry):
    """Only deliver selected intent/arguments/status fields, with precise pointers.

    Structured model content is parsed locally: code, patches and complete report
    bodies are not an intent summary. The original content is still inspectable.
    """
    fields = []

    def add(name, text, ref, limit=180):
        text = str(text)
        if not text.strip() or len(fields) >= 8:
            return
        pointer = {**ref, "start": ref.get("start") or 0, "end": (ref.get("start") or 0) + min(len(text), limit)}
        eid = "e" + identity(pointer)[:12]
        registry[eid] = dict(id=eid, ref=pointer, line=pointer["line_number"])
        fields.append(dict(field=name, text=text[:limit], ref=eid, **({"truncated": True} if len(text) > limit else {})))

    def selected(value, path, base, depth=0):
        if depth > 3 or len(fields) >= 8:
            return
        allowed = {"intent", "summary", "reason", "description", "objective", "action", "type", "kind", "target", "tool", "name", "status", "passed",
                   "command", "path", "query", "pattern", "criterion_id"}
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                if key in allowed and isinstance(child, (str, bool, int)):
                    # JSON string content uses offsets into the containing string,
                    # not invented nested EvidenceStore paths.
                    needle = json.dumps(child, ensure_ascii=False)
                    encoded = text_value(source.resolve(base))
                    start = encoded.find(needle)
                    if start < 0:
                        needle = json.dumps(child, ensure_ascii=True)
                        start = encoded.find(needle)
                    if start >= 0:
                        add(child_path, needle, {**base, "start": start, "end": start + len(needle)})
                elif key in {"criteria", "results", "steps", "plan", "actions", "nodes", "decision", "action", "input", "arguments", "tool_calls"}:
                    selected(child, child_path, base, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value[:4]):
                selected(child, f"{path}.{index}", base, depth + 1)

    for excerpt in card.get("excerpts", []):
        entry = registry.get(excerpt["ref"])
        if not entry:
            continue
        ref = entry["ref"]
        if card["kind"] == "model" and (ref.get("field_path") or "").endswith("content"):
            base = {**ref, "start": None, "end": None}
            text = text_value(source.resolve(base))
            encoded = text.strip()
            if encoded.startswith("```") and encoded.endswith("```"):
                encoded = "\n".join(encoded.splitlines()[1:-1])
            try:
                value = json.loads(encoded)
            except (ValueError, TypeError):
                value = None
            if isinstance(value, (dict, list)):
                selected(value, "intent", base)
            elif not text.lstrip().startswith(("{", "[", "```")):
                add("intent", text, base, 320)
        else:
            add("selection_or_status", excerpt["text"], ref, 120)

    def walk(value, path=""):
        if not isinstance(value, dict):
            return
        if "ref" in value and "text" in value:
            if value["ref"] in registry:
                add(path, value["text"], registry[value["ref"]]["ref"], 240 if path == "command" else 160)
        else:
            for key in sorted(value):
                child = value[key]
                walk(child, f"{path}.{key.split('#')[0]}".strip("."))

    walk(card.get("features", {}))
    # Tool invocation/completion and model usage role are citable recorded facts,
    # even when all argument/output bodies were intentionally excluded.
    for line in dict.fromkeys((card["start_line"], card["end_line"])):
        event = source.event(dict(source_sha256=source.source_sha256, line_number=line))
        for key in ("tool_id", "usage_role", "type"):
            value = event.payload.get(key)
            if isinstance(value, str):
                add("record." + key, value, asdict(source.ref(event, key)), 100)
    return dict(
        **{key: card[key] for key in ("id", "kind", "name", "status", "start_line", "end_line")},
        usage_role=(card.get("actor") or {}).get("role", "unknown"),
        framework=card_policy(card),
        fields=fields,
        refs=list(dict.fromkeys(f["ref"] for f in fields)),
        **({"caller_card_id": card["caller_card_id"]} if card.get("caller_card_id") else {}),
    )


def feedback_metadata(errors):
    """Never replay giant validation messages or entire rejected records."""
    rows, seen = [], set()
    for error in errors:
        fields = ("code", "category", "phase", "unit_id", "record_index", "record_type", "record_id", "card_ids", "path", "details")
        row = {k: error[k] for k in fields if k in error}
        row["reason"] = str(error.get("reason", "Invalid record"))[:180]
        key = serialized(row)
        if key not in seen:
            rows.append(row)
            seen.add(key)
        if len(rows) == 6:
            break
    return rows
