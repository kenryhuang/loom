"""Stable, read-only projection of unresolved evaluation work and uncertainty."""

from collections import defaultdict

from loom.evaluation.framework import semantic_unknown_ids
from loom.evaluation.goal_loops.facts import identity


def retryable_unit(unit):
    """An evidence-backed unknown alone cannot be repaired with the same input."""
    return (unit.get('disposition') in {'abstained', 'quarantined', 'exhausted'}
            and any(e.get('repair_strategy') != 'none' and e.get('category') in {'format', 'semantic', 'coverage'}
                    for e in unit.get('issues', [])))


def gaps(cp):
    """Group derivative coverage/dependency symptoms under their unit's root issues."""
    result = []
    memory = cp.get('memory') or {}
    conflicts = defaultdict(list)
    for item in cp.get('conflicts', []):
        conflicts[item.get('unit_id') or item.get('batch_id')].append(item)
    for domain, units in (('graph', cp.get('units', {})), ('memory', memory.get('units', {}))):
        for uid, unit in units.items():
            issues = unit.get('issues', []) or (conflicts.get(uid, []) if domain == 'graph' else [])
            seen, unique = set(), []
            for issue in issues:
                key = (issue.get('code'), issue.get('record_type'), issue.get('record_id'),
                       issue.get('candidate_id'), tuple(issue.get('path') or ()))
                if key not in seen:
                    seen.add(key)
                    unique.append(issue)
            roots = [e for e in unique if e.get('code') not in {'graph.cards_pending', 'graph.assessment_dependency'}]
            if not roots:
                roots = unique
            for error in roots:
                category = error.get('category', 'semantic')
                code = error.get('code', 'unknown')
                action = ('format' if category == 'format' else 'evidence' if code in {
                    'graph.evidence_scope', 'graph.relationship_evidence_scope', 'graph.evidence_owner', 'memory.anchor_undelivered',
                    'memory.workflow_step_support'} else 'candidate')
                if error.get('repair_strategy') == 'none':
                    action = 'none'
                symptoms = [e['code'] for e in unique if e.get('code') in {'graph.cards_pending', 'graph.assessment_dependency'}]
                result.append(dict(id='gap:' + identity(domain, uid, code, error.get('candidate_id'), error.get('record_id'),
                                                       error.get('record_index'), error.get('path')),
                                   domain=domain, unit_id=uid, code=code, category=category,
                                   candidate_id=error.get('candidate_id'), record_id=error.get('record_id'),
                                   reason=error.get('reason', ''), action=action, symptoms=list(dict.fromkeys(symptoms))))
    for uid, errors in conflicts.items():
        if uid in cp.get('units', {}):
            continue
        for error in errors:
            code = error.get('code', 'unknown')
            result.append(dict(id='gap:' + identity('graph-orphan', uid, code, error.get('record_id'), error.get('path')),
                               domain='graph', unit_id=uid, code=code, category=error.get('category', 'semantic'),
                               record_id=error.get('record_id'), reason=error.get('reason', ''), action='inspect'))
    for cid in sorted(semantic_unknown_ids(cp)):
        row = cp.get('assessments', {}).get(cid, {})
        result.append(dict(id='gap:' + identity('graph-unknown', cid), domain='graph', code='evidence.unknown',
                           record_id=cid, reason=row.get('delta', ''), action='inspect_evidence'))
    for rid, row in memory.get('records', {}).items():
        if row.get('rating') == 'unknown' and row.get('publication_state', 'published') == 'published':
            result.append(dict(id='gap:' + identity('memory-unknown', rid), domain='memory', code='evidence.unknown',
                               unit_id=row.get('unit_id'), record_id=rid, reason=row.get('rationale', ''), action='inspect_evidence'))
    return result
