"""Bounded request construction; no validation or checkpoint mutation."""

from loom.evaluation.goal_loops.packing import serialized
from loom.evaluation.memory.contracts import FIELDS, SYSTEM, pending_stage_ids, protocol_for
from loom.evaluation.memory.evidence import sanitize
from loom.llm.api import LlmMessage
from loom.llm.structured import native_schema


def memory_input(record):
    return {key: record[key] for key in ('id', 'kind', *FIELDS[record['kind']]['items']['properties']) if key in record}


def focus_repair(unit):
    """Narrow only when the failed candidates identify their source actions."""
    from loom.evaluation.evidence_delivery import manifest

    pending = pending_stage_ids(unit)
    owners = {a['id'] for a in unit['scope']['actions'] if any(m['stage_id'] in pending for m in a['memberships'])}
    by_id = {e['id']: e['action_id'] for e in unit['evidence']}
    rejected = unit.get('rejected_records', [])
    for item in rejected:
        row = item.get('record', {})
        handles = row.get('evidence', []) if isinstance(row, dict) else []
        selected = {by_id[unit['manifest']['handles'][h]] for h in handles if h in unit['manifest']['handles']}
        selected.update(a for a in row.get('action_ids', []) if a in {a['id'] for a in unit['scope']['actions']})
        if not selected and item.get('kind') != 'contributions':
            return  # No justified scope reduction: do not guess which output it meant.
        owners.update(selected)
    if owners:
        unit['delivery_span_ids'] = [e['id'] for e in unit['evidence'] if e['action_id'] in owners]
        unit['manifest'] = manifest(unit)


def scope(cp, cards, memberships):
    ids = {c["id"] for c in cards}
    links = [r for r in memberships if r["card_id"] in ids]
    stage_ids = {r["stage_id"] for r in links if r["stage_id"]}
    goals = {g["id"]: g for g in cp["entities"]["goals"]}
    goal_ids = {r["goal_id"] for r in links}
    for gid in list(goal_ids):
        while goals.get(gid, {}).get("parent_goal_id"):
            gid = goals[gid]["parent_goal_id"]
            goal_ids.add(gid)
    return dict(
        goals=[
            {
                **{k: g[k] for k in ("id", "parent_goal_id", "objective")},
                "conditions": [serialized(c)[:400] for c in g.get("conditions", [])[:12]],
                "omitted_conditions": max(0, len(g.get("conditions", [])) - 12),
            }
            for g in goals.values()
            if g["id"] in goal_ids
        ],
        stages=[{k: s[k] for k in ("id", "goal_id", "title", "kind")} for s in cp["stages"].values() if s["id"] in stage_ids],
        actions=[
            dict(
                id=c["id"],
                kind=c["kind"],
                name=c["name"],
                status=c["status"],
                start_line=c["start_line"],
                end_line=c["end_line"],
                memberships=[{k: r[k] for k in ("goal_id", "stage_id", "basis")} for r in links if r["card_id"] == c["id"]],
            )
            for c in cards
        ],
    )


def messages_for(unit, mode):
    payload = dict(
        mode=unit["mode"],
        **unit["scope"],
        evidence=[{k: e[k] for k in ("id", "text", "action_id", "basis", "truncated")} for e in unit["evidence"]],
        previous_memories=unit.get("memories", []),
        accepted_records=unit.get("accepted_records", []),
        feedback=unit.get("issues", unit.get("errors", [])),
        rejected_records=unit.get("rejected_records", []),
        omitted_rejected_records=unit.get("omitted_rejected_records", 0),
    )
    response_protocol = protocol_for(unit)
    system = SYSTEM
    if unit.get('protocol_version') == 2:
        from loom.evaluation.evidence_delivery import deliver
        from loom.evaluation.memory.wire import SYSTEM as ANCHOR_SYSTEM

        system = ANCHOR_SYSTEM
        payload['evidence'] = deliver(unit)
        payload['delivery_id'] = unit['manifest']['id']
        payload['evidence_pages'] = [{"handle": key, "action_id": page['action_id'],
                                      "field": page['ref'].get('field_path'), "range": [page['ref']['start'], page['ref']['end']]}
                                     for key, page in unit.get('pages', {}).items()] if not unit.get('page_reads') else []
        pending = pending_stage_ids(unit)
        payload['stages'] = [s for s in payload['stages'] if s['id'] in pending]
        payload['rejudge_stages'] = unit.get('rejudge_stages', [])
        payload['repair_targets'] = unit.get('repair_targets', [])
        payload['actions'] = [{**a,
            'source_fields': {e['id'].rsplit('.', 1)[0]: e['field'] for e in payload['evidence'] if e['action_id'] == a['id']},
            'evidence': [{k: e[k] for k in ('id', 'text', 'basis', 'truncated')}
                         for e in payload['evidence'] if e['action_id'] == a['id']]}
            for a in payload['actions']]
        payload.pop('evidence')
    payload["empty_record_types"] = [k for k, spec in response_protocol.schema["properties"].items() if spec["maxItems"] == 0]
    if mode != "json_schema":
        payload["response_contract"] = response_protocol.schema
    messages = [LlmMessage(role="system", content=system), LlmMessage(role="user", content=serialized(sanitize(payload)))]
    schema = serialized(native_schema(response_protocol.schema)) if mode == "json_schema" else ""
    chars = sum(len(m.content) for m in messages) + len(schema) + 256
    estimate = sum(len(m.content.encode()) for m in messages) + len(schema.encode()) + 512
    return messages, dict(total_chars=chars, input_estimate=estimate, estimation="utf8_bytes_plus_schema_and_512_not_tokenizer")
