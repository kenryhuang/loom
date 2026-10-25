"""Versioned migration, publication and fine-grained graph dependencies."""

from copy import deepcopy

from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.issues import RuleViolation, issue, stopped
from loom.evaluation.memory.contracts import FIELDS
from loom.evaluation.memory.validation import validate_record

VERSION = 'trace-memory.2'


def dependencies(record, cp):
    if record['kind'] == 'contributions':
        sid = record['stage_id']
        stage = cp['stages'].get(sid, {})
        actions = {c['id'] for c in cp['extraction']['cards'] if c['id'] in record.get('slice_action_ids', [])}
        links = cp.get('action_attributions', [])
        return dict(stage=identity({k: stage.get(k) for k in ('id', 'goal_id', 'attempt_id', 'kind')}),
                    memberships=identity([(cid, sorted(r['stage_id'] for r in links if r['card_id'] == cid and r['stage_id']))
                                          for cid in sorted(actions)]))
    if record['kind'] == 'workflows':
        source = cp.get('memory', {}).get('records', {})
        return dict(records=identity([(key, source.get(key, {}).get('publication_state'), source.get(key, {}).get('dependencies'),
                                       {k: source.get(key, {}).get(k) for k in ('summary', 'rating', 'rationale', 'basis', 'validation', 'citations')})
                                      for key in record.get('source_record_ids', [])]))
    return dict(evidence=identity(record['source_digest'], record.get('evidence_refs', [])))


def migrate(cp, expected):
    memory = cp.get('memory')
    if memory is None:
        memory = cp['memory'] = dict(identity=expected, units={}, records={}, quarantine={}, read_chars=0, cursor=0,
                                     complete=False, execution_complete=False, epoch=1)
        return memory
    old = memory['identity']
    if any(old.get(k) != expected.get(k) for k in ('source', 'run_id', 'model')):
        raise stopped('checkpoint.memory_identity', 'Memory source, run or model changed; create a new evaluation', category='state', stage='memory')
    if old.get('version') not in {'trace-memory.1', VERSION}:
        raise stopped('checkpoint.memory_version', 'Unsupported memory checkpoint version', category='state', stage='memory')
    if old.get('version') != VERSION:
        cp.setdefault('analysis_epochs', []).append(dict(kind='memory', epoch=memory.get('epoch', 1),
            identity=deepcopy(old), snapshot=deepcopy(memory), reason='anchor_protocol_and_isolated_work_items'))
        memory['epoch'] = memory.get('epoch', 1) + 1
        memory.setdefault('quarantine', {})
        from loom.evaluation.memory.records import commit_records
        from loom.evaluation.work_items import settle

        for unit in memory['units'].values():
            if unit.get('complete'):
                continue
            response = next((r for r in reversed(cp['responses']) if r.get('activity') == 'memory' and r['batch_id'] == unit['id']), None)
            if response is not None:
                original = deepcopy(memory['records'])
                issues = commit_records(memory, unit, response['text'], schema=response.get('response_schema'),
                                        finish_reason=response.get('finish_reason'))
                memory['records'].update(original)
                response.setdefault('revalidations', []).append(dict(version='memory-validation.6', issues=deepcopy(issues), model_calls=0))
                settle(unit, issues, accepted=sum(r['unit_id'] == unit['id'] for r in memory['records'].values()))
                if issues:
                    # A new anchor contract is a genuine input change, not another
                    # retry of the old copy-quote task. Keep candidate identities.
                    unit.update(work_state='pending', protocol_version=1)
                    unit['repair_targets'] = [r['id'] for r in memory['quarantine'].values() if r['unit_id'] == unit['id']]
                    unit['rejected_records'] = [r for r in memory['quarantine'].values() if r['unit_id'] == unit['id']]
                    for rejected in unit['rejected_records']:
                        if isinstance(rejected['record'], dict):
                            rejected['record']['key'] = rejected['id']
        for record in memory['records'].values():
            unit = memory['units'].get(record['unit_id'])
            try:
                if unit is None:
                    raise RuleViolation('memory.legacy_scope', 'Legacy scope is missing')
                validate_record(record['kind'], {k: record[k] for k in FIELDS[record['kind']]['items']['properties']}, unit)
                record.update(publication_state='published', provenance_status='validated', support_assessment='legacy_model_judgment')
            except (RuleViolation, KeyError):
                record['publication_state'] = 'legacy'
                failure = issue('memory.legacy_unverified', 'Saved memory could not be validated against its original delivery',
                                stage='memory', unit_id=record['unit_id'], record_type=record['kind'])
                key = 'candidate:' + identity(record['id'])
                memory['quarantine'][key] = dict(id=key, unit_id=record['unit_id'], kind=record['kind'],
                                                record={}, issue=failure, error=failure['reason'])
                if unit:
                    settle(unit, [failure])
                memory['complete'] = False
            record['slice_action_ids'] = [a['id'] for a in (unit or {}).get('scope', {}).get('actions', [])]
            if record['kind'] == 'workflows' and unit:
                record['source_record_ids'] = unit.get('source_record_ids', [r['id'] for r in unit.get('memories', [])])
            record['dependencies'] = dependencies(record, cp)
            if record['kind'] == 'contributions' and unit:
                memberships = [(a['id'], sorted(m['stage_id'] for m in a.get('memberships', []) if m['stage_id']))
                               for a in sorted(unit['scope']['actions'], key=lambda a: a['id'])]
                record['dependencies']['memberships'] = identity(memberships)
        for record in memory['records'].values():
            if record['kind'] == 'workflows':
                record['dependencies'] = dependencies(record, cp)
        for unit in memory['units'].values():
            if not unit.get('complete'):
                unit.update(work_state='pending', protocol_version=1)
                unit['repair_targets'] = [r['id'] for r in memory['quarantine'].values() if r['unit_id'] == unit['id']]
                unit['rejected_records'] = [r for r in memory['quarantine'].values() if r['unit_id'] == unit['id']]
                for rejected in unit['rejected_records']:
                    if isinstance(rejected['record'], dict):
                        rejected['record']['key'] = rejected['id']
                for key in ('pending_response', 'pending_schema', 'pending_finish_reason'):
                    unit.pop(key, None)
        memory['execution_complete'] = memory.get('complete', False) and all(u.get('complete') for u in memory['units'].values())
    memory['identity'] = expected
    # Invalidate contributions first so downstream workflows see their new state.
    for record in sorted(memory['records'].values(), key=lambda r: r['kind'] == 'workflows'):
        if record.get('publication_state') == 'published' and record.get('dependencies') != dependencies(record, cp):
            record['publication_state'] = 'stale'
            unit = memory['units'][record['unit_id']]
            unit.update(complete=False, work_state='pending', dependency_changed=True)
            unit.pop('disposition', None)
            memory.update(complete=False, execution_complete=False)
    return memory
