"""Rebuildable projections of authoritative evaluation memory and task metadata."""

from copy import deepcopy

from loom.evaluation.evidence_delivery import cited_evidence
from loom.memory.store import bound_workspace
from loom.skills.contracts import VERSION, digest


def project(session, evaluation=None, *, episode=None):
    task = session['task']
    evaluation = evaluation or {}
    memory = evaluation.get('checkpoint', {}).get('memory', {})
    identity = memory.get('identity', {})
    run = evaluation.get('run_id') or (session.get('run') or {}).get('id')
    episode = episode or {}
    base = dict(session_id=session['session_id'], run_ids=[run] if run else [],
                task_episode_id=episode.get('id') or 'uncertain:' + task['id'],
                source_digest=identity.get('source'), scope=dict(workspace=(evaluation.get('memory_scope') or {}).get('workspace', bound_workspace(task))),
                task_contract=deepcopy(episode.get('contract') or {k: task.get(k) for k in ('objective', 'task_spec')}),
                goal_revision_refs=episode.get('goal_revision_refs', []),
                context=dict(runtime='unknown', task_spec=(episode.get('contract') or {}).get('task_spec'),
                             workspace=(evaluation.get('memory_scope') or {}).get('workspace', bound_workspace(task))),
                outcome=dict(local_result='unknown',
                             task_result=episode.get('task_result', 'fail' if not evaluation and task.get('state') == 'failed' else 'unknown'),
                             acceptance_status=episode.get('acceptance_status', 'unknown'), user_feedback='unknown'),
                quality=dict(boundary_uncertain=not bool(episode.get('id')) or episode.get('boundary_uncertain', False),
                             missing_fields=['initial_environment_snapshot']),
                exposure=deepcopy(episode.get('exposure', dict(skill_versions_seen=[], prior_memory_refs=[]))),
                created_at=evaluation.get('created_at', session.get('created_at')), extractor_version=VERSION)
    units = [u for u in memory.get('units', {}).values() if 'evidence' in u]
    rows = []
    for record in memory.get('records', {}).values():
        if (record.get('kind') not in {'facts', 'lessons', 'workflows'} or record.get('publication_state') != 'published'
                or record.get('run_id') != run or record.get('source_digest') != identity.get('source')):
            continue
        try:
            evidence = cited_evidence([record], units)
        except (ValueError, KeyError):
            continue
        if not evidence:
            continue
        # Evaluation/model IDs are views, not new execution identities.
        key = digest([base['session_id'], run, base['source_digest'], record['kind'],
                      sorted(record.get('action_ids', [])), sorted(record.get('goal_ids', []))])
        case = deepcopy(base)
        case['context']['observed_tool_ids'] = sorted({a['name'] for u in units for a in u.get('scope', {}).get('actions', [])
                                                       if a.get('kind') == 'tool' and a.get('name')})
        case.update(id='case:' + key[:32], problem=dict(trigger=record.get('trigger', record.get('preconditions', record['title'])),
                    symptoms=record.get('failed_approach'), initial_state='unknown', unresolved_questions=[]),
                    trajectory=dict(semantic_actions=record.get('steps', []), dependency_edges=[], branch_decisions=[]),
                    record=deepcopy(record), evidence=evidence,
                    provenance=dict(evaluation_id=evaluation['id'], analysis_id=evaluation['analysis_id'],
                                    record_id=record['id'], record_digest=digest(record), source_digest=identity['source']))
        # Even a published workflow is not an independently verified whole-task result.
        if record.get('validation') in {'observed', 'observed_recovery'}:
            case['outcome']['local_result'] = 'observed_recovery' if record['validation'] == 'observed_recovery' else 'observed'
        case['quality']['evidence_coverage'] = 'cited_spans_only'
        case['revision'] = digest({k: v for k, v in case.items() if k not in {'created_at', 'provenance'}})
        rows.append(case)
    if not rows:
        case = deepcopy(base)
        case.update(id='case:' + digest([session['session_id'], run, 'metadata'])[:32],
                    problem=dict(trigger=base['task_contract'].get('objective') or '', initial_state='unknown'),
                    trajectory=dict(semantic_actions=[], dependency_edges=[]), evidence=[], record=None,
                    provenance=dict(evaluation_id=evaluation.get('id'), analysis_id=evaluation.get('analysis_id')))
        case['quality']['missing_fields'].append('usable_memory')
        case['revision'] = digest(case)
        rows.append(case)
    return rows
