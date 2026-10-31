"""Deterministic, source-addressable features; extraction is not semantic review."""

from collections import Counter
from copy import deepcopy
from dataclasses import asdict

from loom.evaluation.evidence_store import text_value
from loom.evaluation.framework import boundary_event
from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.goal_loops.packing import serialized

VERSION = "goal-cards.2"
CARD_CHARS = 2000
ARGUMENTS = {"path", "paths", "file_path", "url", "query", "pattern", "offset", "limit", "start_line", "end_line", "cwd"}
RESULTS = {"exit_code", "returncode", "status", "success", "error", "truncated", "shown_lines", "next_offset", "total_lines", "total", "count", "uri", "path"}
BOUNDARY = {"objective", "description", "reason", "state", "status", "passed", "required", "criterion_id", "verifier", "outcome", "summary"}


def compile_cards(source, facts, view):
    registry, cards = {}, []
    tools = {r["id"]: r for r in facts["tool_uses"]}
    models = {r["id"]: r for r in facts["model_calls"]}
    tool_identity_counts = Counter((t.get("trace_id"), t.get("tool_call_id")) for t in tools.values())

    def register(ref):
        key = "e" + identity(ref)[:12]
        registry[key] = dict(id=key, ref=ref, line=ref["line_number"])
        return key

    def excerpt(ref, limit=600):
        raw = text_value(source.resolve(ref))
        start = ref.get("start") or 0
        precise = {**ref, "start": start, "end": start + min(len(raw), limit)}
        return dict(ref=register(precise), text=raw[:limit], omitted_chars=max(0, len(raw) - limit))

    def fields(value, ref, allowed, depth=0, remaining=None):
        remaining = [16] if remaining is None else remaining
        result = {}
        if depth > 3 or remaining[0] <= 0:
            return result
        if isinstance(value, list):
            for index, child in enumerate(value[:8]):
                pointer = {**ref, "field_path": f"{ref.get('field_path')}.{index}"}
                result.update(fields(child, pointer, allowed, depth + 1, remaining))
            return result
        if not isinstance(value, dict):
            return result
        for key, child in value.items():
            if remaining[0] <= 0:
                break
            path = (ref.get("field_path") + "." if ref.get("field_path") else "") + key
            pointer = {**ref, "field_path": path, "start": None, "end": None}
            if key in allowed and isinstance(child, (str, int, float, bool, list)):
                entry = excerpt(pointer, 180)
                result[key + "#" + str(remaining[0])] = entry
                remaining[0] -= 1
            elif isinstance(child, (dict, list)) and key not in {"stdout", "stderr", "content", "messages", "reasoning", "reasoning_content"}:
                result.update(fields(child, pointer, allowed, depth + 1, remaining))
        return result

    # Link only explicitly identified tool messages from the same execution scope.
    presentations = {}
    for model in models.values():
        if model.get("run_id") != view["run_id"] or not model.get("request_ref"):
            continue
        event = source.event(model["request_ref"])
        messages = event.payload.get("messages", [])
        for n, message in enumerate(messages if isinstance(messages, (list, tuple)) else []):
            if not hasattr(message, "get") or message.get("role") != "tool" or not message.get("tool_call_id"):
                continue
            pointer = asdict(source.ref(event, f"messages.{n}.content")) if "content" in message else None
            if pointer:
                presentations.setdefault((model.get("trace_id"), message["tool_call_id"]), []).append(pointer)

    for atom in view["atoms"]:
        refs = atom.get("evidence_refs", [])
        if not refs:
            continue
        card = dict(
            id="c" + identity(atom["id"])[:12],
            call_ids=[atom["id"]],
            kind=atom["kind"],
            name=atom.get("tool") or atom.get("model") or atom["kind"],
            status=atom.get("status", "unknown"),
            start_line=min(r["line_number"] for r in refs),
            end_line=max(r["line_number"] for r in refs),
            features={},
            excerpts=[],
            refs=[],
            raw_refs=[],
            captured=[],
            presented=[],
            adopted="not_assessed",
        )
        if atom["kind"] == "model":
            model = models.get(atom["id"], {})
            ref = model.get("response_ref")
            if ref:
                value = source.resolve(ref)
                if isinstance(value, dict) and isinstance(value.get("content"), str):
                    pointer = {**ref, "field_path": (ref.get("field_path") + "." if ref.get("field_path") else "") + "content"}
                    card["raw_refs"].append(register(pointer))
                    card["excerpts"].append(excerpt(pointer))
                if isinstance(value, dict):
                    # Tool selection is public model output, even when content is empty.
                    for n, call in enumerate((value.get("tool_calls") or [])[:4]):
                        if isinstance(call, dict) and isinstance(call.get("name"), str):
                            prefix = (ref.get("field_path") + "." if ref.get("field_path") else "") + f"tool_calls.{n}."
                            card["excerpts"].append(excerpt({**ref, "field_path": prefix + "name"}, 120))
                            if isinstance(call.get("arguments"), str):
                                pointer = {**ref, "field_path": prefix + "arguments"}
                                card["raw_refs"].append(register(pointer))
            card["actor"] = dict(trace=model.get("trace_id"), loop=model.get("loop_id"), role=model.get("usage_role", "unknown"))
        else:
            tool = tools.get(atom["id"], {})
            card["actor"] = dict(trace=tool.get("trace_id"), loop=tool.get("loop_id"), role=tool.get("usage_role", "unknown"))
            card["caller_id"] = tool.get("round_id")
            for field, allowed in (("input_ref", ARGUMENTS), ("raw_output_ref", RESULTS)):
                ref = tool.get(field)
                if not ref:
                    continue
                card["raw_refs"].append(register(ref))
                value = source.resolve(ref)
                card["features"]["arguments" if field == "input_ref" else "result"] = fields(value, ref, allowed)
                if field == "input_ref" and isinstance(value, dict) and isinstance(value.get("command"), str):
                    # A bounded command is an explicit action description, never its result body.
                    pointer = {**ref, "field_path": (ref.get("field_path") + "." if ref.get("field_path") else "") + "command"}
                    card["features"]["command"] = excerpt(pointer, 240)
                if field == "raw_output_ref":
                    card["captured"].append(dict(ref=register(ref), chars=tool.get("output_char_length"), truncated=tool.get("source_truncated")))
            pointers = presentations.get((tool.get("trace_id"), tool.get("tool_call_id")), [])
            # Ambiguous ID reuse is not evidence of delivery to this invocation.
            unique = tool_identity_counts[(tool.get("trace_id"), tool.get("tool_call_id"))] == 1
            if unique:
                captured = text_value(source.resolve(tool["raw_output_ref"])) if tool.get("raw_output_ref") else None
                for ref in pointers:
                    if ref["line_number"] <= card["end_line"]:
                        continue
                    value = text_value(source.resolve(ref))
                    card["presented"].append(
                        dict(
                            ref=register(ref),
                            line=ref["line_number"],
                            chars=len(value),
                            mode="full" if captured is not None and value == captured else "preview_or_transformed",
                        )
                    )
            card["presentation_status"] = "recorded" if card["presented"] else "unknown"

        def collect(value, card=card):
            if isinstance(value, dict):
                if "ref" in value:
                    card["refs"].append(value["ref"])
                for v in value.values():
                    collect(v)
            elif isinstance(value, list):
                for v in value:
                    collect(v)

        collect(card["features"])
        collect(card["excerpts"])
        # Empty results still have an exact recorded status and call identity.
        for ref in refs:
            event = source.event(ref)
            for field in ("status", "state", "error"):
                if field in event.payload:
                    card["excerpts"].append(excerpt(asdict(source.ref(event, field)), 120))
        card["refs"] = list(dict.fromkeys(card["refs"] + [e["ref"] for e in card["excerpts"]]))
        card["complete"] = all(not e.get("omitted_chars") for e in card["excerpts"]) and not card["captured"]
        cards.append(card)

    for record in view["records"]:
        if record["call_ids"] or not boundary_event(record['type']):
            continue
        excerpts = []
        metadata = {}
        remaining = [12]
        for ref in record["refs"]:
            value = source.resolve(ref)
            if isinstance(value, (dict, list)):
                metadata.update(fields(value, ref, BOUNDARY, remaining=remaining))
            elif (ref.get("field_path") or "").split(".")[-1] in BOUNDARY and remaining[0] > 0:
                excerpts.append(excerpt(ref, 240))
                remaining[0] -= 1
        cards.append(
            dict(
                id="c" + identity(source.source_sha256, record["line"])[:12],
                call_ids=[],
                kind="boundary",
                name=record["type"],
                status="recorded",
                start_line=record["line"],
                end_line=record["line"],
                features=metadata,
                excerpts=excerpts,
                refs=[e["ref"] for e in excerpts] + [e["ref"] for e in metadata.values()],
                raw_refs=[register(r) for r in record["refs"]],
                captured=[],
                presented=[],
                adopted="not_assessed",
                complete=all(not e["omitted_chars"] for e in excerpts),
            )
        )
    cards.sort(key=lambda c: (c["end_line"], c["start_line"], c["id"]))
    links = link_cards(cards)
    return dict(
        version=VERSION,
        cards=cards,
        registry=registry,
        metadata_links=links,
        record_index=[dict(line=r["line"], type=r["type"], call_ids=r["call_ids"], ref=r["source_ref"]) for r in view["records"]],
    )


def link_cards(cards):
    """Recorded order and exact model→tool invocation, never semantic dependency."""
    call_cards = {call: card["id"] for card in cards for call in card["call_ids"]}
    links = []
    for index, card in enumerate(cards):
        previous = cards[index - 1]["id"] if index else None
        caller = call_cards.get(card.get("caller_id"))
        card.update(sequence_index=index + 1, previous_card_id=previous, caller_card_id=caller)
        for kind, origin in (("sequence", previous), ("invokes", caller)):
            if origin:
                links.append(dict(source_id=origin, target_id=card["id"], kind=kind, layer="recorded"))
    return links


def card_payload(card, cutoff):
    """Later presentation is a fact, but unavailable to earlier semantic judgments."""
    presented = [p for p in card["presented"] if p["line"] <= cutoff]
    payload = {
        **{k: card[k] for k in ("id", "kind", "name", "status", "start_line", "end_line", "features", "excerpts", "refs", "raw_refs")},
        "presented": presented[-3:],
        "presentation_count": len(presented),
        "captured": card["captured"],
        "omitted_metadata_fields": 0,
        "call_ids": card["call_ids"],
        "actor": card.get("actor", {}),
        "caller_id": card.get("caller_id"),
        "caller_card_id": card.get("caller_card_id"),
        "sequence_index": card.get("sequence_index"),
        "previous_card_id": card.get("previous_card_id"),
    }
    payload["features"] = deepcopy(card["features"])
    payload["excerpts"] = list(card["excerpts"])
    payload["name"] = payload["name"][:160]

    def leaves(value):
        for key, child in value.items():
            if isinstance(child, dict):
                if "ref" in child:
                    yield value, key, child
                else:
                    yield from leaves(child)

    # Long verifier/plan structures remain local. Prefer purpose/path/status over
    # repetitive auxiliary metadata; exact source pointers remain available for reads.
    priority = {"description": 5, "objective": 5, "command": 5, "path": 4, "query": 4, "status": 4, "exit_code": 4, "reason": 3}
    while len(serialized(payload)) > CARD_CHARS:
        candidates = list(leaves(payload["features"]))
        if not candidates:
            if len(payload["excerpts"]) <= 1:
                break
            payload["excerpts"].pop()
        else:
            parent, key, _ = min(reversed(candidates), key=lambda item: priority.get(item[1].split("#")[0], 1))
            del parent[key]
        payload["omitted_metadata_fields"] += 1
        payload["refs"] = list(dict.fromkeys([e["ref"] for e in payload["excerpts"]] + [e["ref"] for _, _, e in leaves(payload["features"])]))
    return payload
