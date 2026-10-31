"""Retire obsolete framework-only obligations without accepting their claims."""

from copy import deepcopy

from loom.evaluation.framework import VERSION as FRAMEWORK_VERSION
from loom.evaluation.framework import card_policy
from loom.evaluation.issues import describe
from loom.evaluation.memory.contracts import pending_stage_ids
from loom.evaluation.work_items import settle, terminal

VERSION = 'memory-obligations.1'


def reconcile_framework_obligations(cp):
    """Only retire a known stage whose recorded members are all framework-only.

    Missing/foreign stages and stages with real semantic actions are not covered
    by this migration. Accepted records, source evidence and responses are never
    rewritten, and no failed claim is promoted to a valid memory.
    """
    memory = cp.get('memory', {})
    cards = {c['id']: c for c in cp.get('extraction', {}).get('cards', [])}
    changed = []
    for unit in memory.get('units', {}).values():
        if unit.get('mode') != 'extract' or 'pending_response' in unit or unit.get('dependency_changed'):
            continue
        current_stages = {s['id'] for s in unit['scope']['stages']}
        actions = unit['scope']['actions']
        retired = []
        for cid, candidate in list(memory.get('quarantine', {}).items()):
            row = candidate.get('record')
            if candidate['unit_id'] != unit['id'] or candidate['kind'] != 'contributions' or not isinstance(row, dict):
                continue
            sid = row.get('stage_id')
            if not isinstance(sid, str) or sid not in cp.get('stages', {}) or sid in current_stages:
                continue
            members = {a['id'] for a in actions if (
                any(m.get('stage_id') == sid for m in a.get('memberships', []))
                or sid in cp.get('assessments', {}).get(a['id'], {}).get('stage_ids', []))}
            if not members or any(a not in cards or card_policy(cards[a])['stage_key_action'] for a in members):
                continue
            audit = dict(**deepcopy(candidate), resolution='not_applicable',
                         resolution_reason='This window has no semantic stage actions; its former members are framework context or root-owned actions.',
                         migration_version=VERSION, framework_version=FRAMEWORK_VERSION,
                         stage_id=sid, framework_action_ids=sorted(members), model_calls=0)
            unit.setdefault('retired_candidates', []).append(audit)
            del memory['quarantine'][cid]
            retired.append(cid)
        if not retired:
            continue
        retired_ids = set(retired)
        retired_stages = {r['stage_id'] for r in unit['retired_candidates'] if r['id'] in retired_ids}

        def obsolete(error, retired_ids=retired_ids, retired_stages=retired_stages, current_stages=current_stages, unit=unit):
            if error.get('candidate_id') in retired_ids:
                return True
            if error.get('code') == 'memory.stages_pending':
                return bool(error.get('stage_ids')) and set(error['stage_ids']) <= retired_stages
            # Old repair attempts could neither submit nor withdraw the removed
            # obligation. Keep unrelated format errors and other candidates.
            return (error.get('code') == 'schema.maxItems' and error.get('record_type') in {'contributions', 'withdrawals'}
                    and error.get('details', {}).get('limit') == 0
                    and not current_stages and not any(r['unit_id'] == unit['id'] for r in memory['quarantine'].values()))

        issues = [e for e in unit.get('issues', []) if not obsolete(e)]
        for candidate in memory['quarantine'].values():
            if candidate['unit_id'] == unit['id'] and candidate['issue'] not in issues:
                issues.append(deepcopy(candidate['issue']))
        unit['repair_targets'] = [cid for cid in unit.get('repair_targets', []) if cid not in retired_ids]
        unit['rejected_records'] = [r for r in unit.get('rejected_records', []) if r['id'] not in retired_ids]
        unit['issues'] = issues
        unit['errors'] = [describe(e) for e in issues]
        published = [r for r in memory.get('records', {}).values()
                     if r['unit_id'] == unit['id'] and r.get('publication_state') == 'published']
        if not pending_stage_ids(unit) and not unit.get('requested_pages'):
            settle(unit, issues, accepted=len(published), unknown=any(r.get('rating') == 'unknown' for r in published))
            if not issues:
                for key in ('retry_requested', 'repair_reserved'):
                    unit.pop(key, None)
        changed.extend(retired)
    if changed:
        # Do not turn unfinished extraction or synthesis into completed work.
        if memory.get('execution_complete') and not all(terminal(u) for u in memory['units'].values()):
            memory['execution_complete'] = False
        memory['complete'] = (bool(memory.get('execution_complete'))
                              and all(u.get('complete') for u in memory['units'].values()) and not memory['quarantine'])
    return changed
