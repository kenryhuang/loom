"""Validate and commit independent memory records, retaining accepted siblings.

Array admission uses the exact request contract; item validation is independent
so one bad citation cannot discard other supported records. Only declared rule
violations become model feedback. Programming errors propagate to the service.
"""

from copy import deepcopy

from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.goal_loops.packing import serialized
from loom.evaluation.issues import RuleViolation, issue, protocol_issue, validate_schema
from loom.evaluation.memory.contracts import FIELDS, PROTOCOL, protocol_for
from loom.evaluation.memory.evidence import sanitize
from loom.evaluation.memory.validation import validate_record
from loom.evaluation.rules import policy
from loom.llm.api import LlmResponse
from loom.llm.structured import ProtocolError, parse_response


def commit_records(memory, unit, response, *, schema=None, finish_reason=None):
    errors = []
    unit.update(issues=errors, rejected_records=[], omitted_rejected_records=0)
    quarantine = memory.setdefault('quarantine', {})
    anchored = unit.get('protocol_version') == 2 and (schema is None or 'evidence' in schema['properties']['facts']['items']['properties'])

    def candidate(kind, index, row):
        key = row.get('key') if isinstance(row, dict) else None
        if isinstance(key, str) and key in quarantine and quarantine[key]['unit_id'] == unit['id'] and quarantine[key]['kind'] == kind:
            return key
        return 'candidate:' + identity(unit['id'], kind, key if isinstance(key, str) else index)

    def reject(error, kind=None, index=None, row=None):
        error = policy({**error, "stage": "memory", "unit_id": unit["id"]})
        if kind is not None:
            error["record_type"] = kind
        if index is not None:
            error["record_index"] = index + 1
            error = policy(error)
            cid = candidate(kind, index, row)
            error['candidate_id'] = cid
            rejected = dict(id=cid, unit_id=unit['id'], kind=kind, index=index + 1,
                            record={} if error['code'] == 'memory.credentials' else sanitize(row), error=error["reason"], issue=error)
            quarantine[cid] = rejected
            if len(serialized(unit["rejected_records"] + [rejected])) <= 16000:
                unit["rejected_records"].append(rejected)
            else:
                unit["omitted_rejected_records"] += 1
        errors.append(error)

    try:
        value, _ = parse_response(LlmResponse(content=response, finish_reason=finish_reason), PROTOCOL.max_response_chars)
    except ProtocolError as exc:
        reject(protocol_issue(exc))
        return errors
    if not isinstance(value, dict):
        reject(issue("response.object", "Return a memory object with contributions, facts, lessons and workflows arrays", category="format", phase="decode"))
        return errors
    schema = schema if schema is not None else protocol_for(unit).schema
    if set(value) - (set(FIELDS) | ({'evidence_requests'} if anchored else set())):
        reject(issue("response.fields", "Unknown memory fields; allowed: contributions, facts, lessons, workflows", category="format", phase="decode"))
    accepted_stages = {r["stage_id"] for r in memory["records"].values()
                       if r["unit_id"] == unit["id"] and r["kind"] == "contributions" and r.get('publication_state') != 'stale'}
    if anchored:
        try:
            validate_schema(value.get('evidence_requests', []), schema['properties']['evidence_requests'])
            unit['requested_pages'] = value.get('evidence_requests', [])
        except RuleViolation as exc:
            reject(exc.issue, 'evidence_requests')
    for kind in FIELDS:
        rows = value.get(kind)
        try:
            # Enforce mode and pending-stage capacity from the request, without
            # letting a bad item roll back valid siblings in the same array.
            spec = {k: v for k, v in schema["properties"][kind].items() if k != "items"}
            validate_schema(rows, spec)
        except RuleViolation as exc:
            reject(exc.issue, kind)
            continue
        for index, row in enumerate(rows):
            cid = candidate(kind, index, row)
            if anchored and any(r.get('candidate_id') == cid and r.get('publication_state') == 'published' for r in memory['records'].values()):
                continue
            try:
                materialized, provenance = row, {}
                if anchored:
                    from loom.evaluation.memory.wire import materialize

                    if unit.get('repair_targets') and kind != 'contributions' and cid not in unit['repair_targets']:
                        raise RuleViolation('memory.repair_scope', 'Repair may only replace requested candidates', path=('key',))
                    materialized, provenance = materialize(kind, row, unit)
                record = validate_record(kind, materialized, unit)
            except RuleViolation as exc:
                reject(exc.issue, kind, index, row)
                continue
            if kind == "contributions":
                key = identity(kind, unit["id"], record["stage_id"])
            elif kind == "workflows":
                key = identity(kind, unit["id"], sorted(record["goal_ids"]))
            elif anchored:
                key = identity(cid)
            else:
                key = identity(kind, {k: record[k] for k in FIELDS[kind]["items"]["properties"]})
            record.update(id="memory:" + key, kind=kind, unit_id=unit["id"], **{
                k: memory["identity"][source] for k, source in (("source_digest", "source"), ("run_id", "run_id"), ("graph_revision", "graph_revision"))
            })
            record.update(publication_state='published', provenance_status='validated',
                          support_assessment=record.get('basis', record.get('validation', 'model_judgment')),
                          candidate_id=cid, provenance=provenance,
                          slice_action_ids=[a['id'] for a in unit['scope']['actions']],
                          source_record_ids=unit.get('source_record_ids', []))
            if kind == 'workflows' and anchored:
                selected = [e for e in unit['evidence'] if e['id'] in provenance['selected_spans']]
                record['source_record_ids'] = [r['id'] for r in unit.get('memories', []) if any(
                    e['text'] in c['quote'] and any(
                        all(pointer.get(k) == e['ref'].get(k) for k in ('source_sha256', 'line_number', 'field_path'))
                        for pointer in memory['records'].get(r['id'], {}).get('evidence_refs', []))
                    for c in r.get('citations', []) for e in selected)]
            old = memory['records'].get(record['id'])
            if old and old != record:
                record['history'] = [*old.get('history', []), {k: v for k, v in old.items() if k != 'history'}]
            memory["records"][record["id"]] = record
            if cid in quarantine:
                unit.setdefault('resolved_candidates', []).append(quarantine.pop(cid))
            if kind == "contributions":
                accepted_stages.add(record["stage_id"])
    if unit["mode"] == "extract":
        missing = sorted({s["id"] for s in unit["scope"]["stages"]} - accepted_stages)
        if missing:
            reject(issue("memory.stages_pending", "Rate every delivered stage, using unknown when needed: " + ", ".join(missing),
                         category="coverage", phase="coverage", stage_ids=missing), "contributions")
    if unit.get('requested_pages'):
        reject(issue('memory.evidence_pending', 'Candidate requests another delivered-document page', category='coverage'), 'evidence_requests')
    unit["accepted_records"] = [
        {k: r[k] for k in ("kind", "title", "summary", "stage_id", "rating") if k in r}
        for r in memory["records"].values() if r["unit_id"] == unit["id"] and r.get('publication_state') != 'stale'
    ][:48]
    # Omission in a repair is an explicit unresolved candidate, not silent success.
    if anchored:
        for rejected in quarantine.values():
            if rejected['unit_id'] == unit['id'] and rejected['issue'] not in errors:
                errors.append(deepcopy(rejected['issue']))
    unit["issues"] = deepcopy(errors)
    return errors


def commit(memory, unit, response):
    """Legacy string-returning API. Runtime and policy use commit_records."""
    return [f"{e.get('record_type', 'response')}: "
            + (f"record {e['record_index']}: " if 'record_index' in e else "") + e["reason"]
            for e in commit_records(memory, unit, response, schema=PROTOCOL.schema)]
