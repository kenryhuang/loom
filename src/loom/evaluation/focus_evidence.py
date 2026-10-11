"""Choose substantive source fields before spending the bounded evidence budget."""

from dataclasses import asdict, replace


def focus_evidence_refs(store, segment, anchors=()):
    def fields(raw):
        ref = store.pointer(raw)
        event = store.event(ref)
        value = store.resolve(ref)
        paths = []
        if ref.start is None and ref.end is None:
            if ref.field_path and isinstance(value, dict):
                paths = [f"{ref.field_path}.{key}" for key in ("stdout", "stderr", "content", "text", "exit_code", "error")
                         if value.get(key) not in (None, "")]
            elif ref.field_path is None:
                paths = {"llm.completed": ["response.content", "response.tool_calls"],
                         "tool.started": ["input"], "tool.completed": ["output.value"],
                         "tool.failed": ["error"]}.get(event.event_type, [])
                if event.event_type == "llm.requested":
                    return []  # Repeated context envelopes can be fetched on demand.
                if event.event_type.startswith(("workflow.", "plan.")):
                    paths = ["plan", "workflow"]
                if event.event_type in {'verification.completed', 'verification.recorded'}:
                    paths = ['result', 'status', 'reason', 'assurance']
                if event.event_type.startswith('acceptance.gate.'):
                    paths = ['acceptance.state', 'acceptance.reason', 'assurance', 'results']
        found = []
        for path in paths:
            narrowed = replace(ref, field_path=path)
            try:
                if store.resolve(narrowed) in (None, "", [], {}):
                    continue
            except ValueError:
                continue
            found.extend(fields(asdict(narrowed)) if path == "output.value" else [asdict(narrowed)])
        return found or [asdict(ref)]

    # Interleave primary actions/results with linked plans and neighbor evidence.
    primary = [r for raw in segment["evidence_refs"] for r in fields(raw)]
    context = [r for raw in anchors for r in fields(raw)]
    combined = []
    for i in range(max(len(primary), len(context))):
        if i < len(primary):
            combined.append(primary[i])
        if i < len(context):
            combined.append(context[i])
    unique = {(r["line_number"], r["field_path"], r["start"], r["end"]): r for r in combined}
    return list(unique.values())
