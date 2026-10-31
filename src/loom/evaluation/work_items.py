"""Execution progress and semantic quality are separate, read-only projections."""

from loom.evaluation.framework import semantic_unknown_ids
from loom.evaluation.rules import policy

VERSION = 'evaluation-status.2'


def settle(unit, issues, *, accepted=0, unknown=False):
    unit['issues'] = [policy(e) for e in issues]
    disposition = 'quarantined' if any(e['category'] != 'coverage' for e in issues) else 'abstained' if issues or unknown else (
        'accepted' if accepted else 'no_findings')
    unit.update(work_state='settled', disposition=disposition, complete=not issues and not unknown)


def terminal(unit):
    return unit.get('complete', False) or unit.get('work_state') == 'settled'


def status(cp):
    graph = list(cp.get('units', {}).values())
    graph_scanned = cp.get('cursor', 0) >= cp.get('total_cards', len(cp.get('extraction', {}).get('cards', [])))
    memory = cp.get('memory', {})
    requested = cp.get('memory_requested', False)
    done = graph_scanned and all(terminal(u) for u in graph) and (not requested or memory.get('execution_complete', memory.get('complete', False)))
    units = graph + (list(memory.get('units', {}).values()) if requested else [])
    issues = list(cp.get('conflicts', [])) + [e for u in memory.get('units', {}).values() for e in u.get('issues', [])]
    unknown = len(semantic_unknown_ids(cp))
    unknown += sum(r.get('rating') == 'unknown' for r in memory.get('records', {}).values() if r.get('publication_state') != 'stale')
    quality = bool(issues or unknown or any(u.get('disposition') in {'abstained', 'quarantined', 'exhausted'} for u in units))
    return dict(status_version=VERSION, execution_state='completed' if done else 'paused',
                result_state='ready' if done and not quality else 'partial',
                support_state='mixed' if quality else 'not_assessed',
                pending_work=sum(not terminal(u) for u in units) + int(not graph_scanned)
                + int(requested and not memory.get('execution_complete', memory.get('complete', False))),
                retryable_work=sum(u.get('disposition') in {'abstained', 'quarantined', 'exhausted'} for u in units),
                unknown_judgments=unknown)


def retry_failed(cp):
    """An explicit user operation; ordinary Resume never resets settled failures."""
    for group in (cp.get('units', {}), cp.get('memory', {}).get('units', {})):
        for unit in group.values():
            if unit.get('disposition') in {'abstained', 'quarantined', 'exhausted'}:
                unit.setdefault('retry_history', []).append(dict(disposition=unit['disposition'], issues=unit.get('issues', [])))
                unit.update(work_state='pending', complete=False, retry_requested=True)
                if 'cards' in unit:
                    unit['rejudge_cards'] = sorted(set(unit['cards']) & semantic_unknown_ids(cp))
                unit['retry_generation'] = unit.get('retry_generation', 0) + 1
                unit.pop('disposition', None)
    if cp.get('memory'):
        for record in cp['memory']['records'].values():
            unit = cp['memory']['units'].get(record['unit_id'], {})
            if unit.get('retry_requested') and record.get('rating') == 'unknown' and record.get('publication_state') == 'published':
                stages = unit.setdefault('rejudge_stages', [])
                if record['stage_id'] not in stages:
                    stages.append(record['stage_id'])
        cp['memory'].update(execution_complete=False, complete=False)
    cp['complete'] = False
