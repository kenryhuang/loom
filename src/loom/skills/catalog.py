"""Paged reconciliation of every session, including failures and sessions without memory."""

import json
import threading
import time
from collections import defaultdict

from loom.evaluation.evidence_store import text_value
from loom.evaluation.memory.evidence import sanitize
from loom.memory.store import bound_workspace
from loom.skills.cases import project
from loom.skills.contracts import digest
from loom.skills.lineage import assign


class CaseCatalog:
    def __init__(self, store, trajectory):
        self.store, self.trajectory = store, trajectory
        self.lock = threading.RLock()
        self.last_full = {}

    def reconcile(self, workspace):
        with self.lock:
            return self._reconcile(workspace)

    def _reconcile(self, workspace):
        full = time.monotonic() - self.last_full.get(workspace, 0) > 60
        cursor, count = '', 0
        while True:
            with self.store.transaction() as db:
                sessions = db.execute('''SELECT id,json_object('session_id',id,'task',json_extract(body,'$.task'),
                    'run',json_extract(body,'$.run'),'created_at',json_extract(body,'$.created_at'),
                    'deleted_at',json_extract(body,'$.deleted_at')) AS body
                    FROM sessions WHERE id>? ORDER BY id LIMIT 100''', (cursor,)).fetchall()
            if not sessions:
                break
            for row in sessions:
                cursor = row['id']
                state = json.loads(row['body'])
                # Historical evaluation scope is checked too: configuration may since have moved workspace.
                with self.store.transaction() as db:
                    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    previous = db.execute('SELECT digest FROM skill_source_cursors WHERE session_id=?', (cursor,)).fetchone()
                    if previous and not full and not db.execute('SELECT 1 FROM skill_dirty_sessions WHERE session_id=?', (cursor,)).fetchone():
                        continue
                    evaluations = [json.loads(r[0]) for r in db.execute('''SELECT json_object(
                        'id',id,'session_id',session_id,'run_id',json_extract(body,'$.run_id'),
                        'analysis_id',json_extract(body,'$.analysis_id'),'model',json_extract(body,'$.model'),
                        'created_at',json_extract(body,'$.created_at'),'state',json_extract(body,'$.state'),
                        'memory_scope',json_extract(body,'$.memory_scope'),'checkpoint',json_object(
                            'memory',json_extract(body,'$.checkpoint.memory'),
                            'graph_revision',json_extract(body,'$.checkpoint.graph_revision')))
                        FROM semantic_jobs WHERE session_id=? ORDER BY id''', (cursor,))] if 'semantic_jobs' in tables else []
                    for evaluation in evaluations:
                        if evaluation['checkpoint']['memory'] is None:
                            evaluation['checkpoint']['memory'] = {}
                scopes = {bound_workspace(state['task']), *[(e.get('memory_scope') or {}).get('workspace') for e in evaluations]}
                if workspace not in scopes and not state.get('deleted_at'):
                    continue
                fingerprint = digest([state['task'], state.get('deleted_at'), state.get('run'),
                                      [(e['id'], e.get('checkpoint', {}).get('memory'), e.get('state')) for e in evaluations]])
                if previous and previous[0] == fingerprint:
                    with self.store.transaction() as db:
                        db.execute('DELETE FROM skill_dirty_sessions WHERE session_id=?', (cursor,))
                    continue
                cases = []
                if not state.get('deleted_at'):
                    for evaluation in evaluations:
                        episode = self.episode(state, evaluation)
                        cases.extend(project(state, evaluation, episode=episode))
                    if not evaluations:
                        cases = project(state)
                # Identical source/action slices across evaluators are multiple interpretations of one case.
                grouped = defaultdict(list)
                for case in cases:
                    grouped[case['id']].append(case)
                merged = []
                for values in grouped.values():
                    case = values[-1]
                    case['evaluation_views'] = [c['provenance'] for c in values]
                    meanings = {digest({k: v for k, v in (c['record'] or {}).items() if k in {
                        'title', 'summary', 'steps', 'validation', 'better_approach', 'failure_reason', 'basis'}}) for c in values}
                    case['quality']['interpretation_conflict'] = len(meanings) > 1
                    case['revision'] = digest([case['revision'], sorted(meanings)])
                    case['source_revision'] = case['revision']
                    merged.append(case)
                self.store.replace_session(cursor, merged, fingerprint)
                count += len(merged)
        cases = self.store.all_cases(workspace)
        groups = assign(cases)
        with self.store.transaction() as db:
            for case in cases:
                revision = digest([case.get('source_revision', case['revision']), groups[case['id']]['task_family_id'],
                                   groups[case['id']]['split_group_id']])
                if case.get('task_family_id') and revision != case['revision']:
                    db.execute("UPDATE skill_versions SET state='suspended', revision=revision+1 "
                               "WHERE state IN ('active','trial','candidate') AND id IN "
                               '(SELECT version_id FROM skill_dependencies WHERE case_id=?)', (case['id'],))
                case.update(groups[case['id']])
                case['revision'] = revision
                db.execute('UPDATE skill_cases SET revision=?,body=? WHERE id=?', (revision, json.dumps(case, ensure_ascii=False), case['id']))
                db.execute('INSERT OR IGNORE INTO skill_case_revisions VALUES(?,?,?)', (case['id'], revision, json.dumps(case, ensure_ascii=False)))
        if full:
            self.last_full[workspace] = time.monotonic()
        return dict(updated_cases=count, total_cases=len(cases), families=len({c['task_family_id'] for c in cases}),
                    uncertain_cases=sum(c['quality']['boundary_uncertain'] for c in cases))

    def episode(self, session, evaluation):
        run = evaluation.get('run_id')
        with self.store.transaction() as db:
            events = [json.loads(row[0]) for row in db.execute('''SELECT body FROM events WHERE session_id=?
                AND json_extract(body,'$.run_id')=? AND json_extract(body,'$.type') IN
                ('run.started','task.goal.revised','skill.retrieved','memory.retrieved',
                 'acceptance.gate.passed','acceptance.gate.blocked','acceptance.plan.accepted',
                 'acceptance.plan.revised','run.failed') ORDER BY seq''', (session['session_id'], run))]
        selected = [e for e in events if e.get('run_id') == run]
        started = next((e['payload'] for e in selected if e['type'] == 'run.started'), {})
        acceptance = [e['type'] for e in selected if e['type'] in {'acceptance.gate.passed', 'acceptance.gate.blocked'}]
        outcomes = dict(task_result='fail' if any(e['type'] == 'run.failed' for e in selected) else 'unknown',
                        acceptance_status=acceptance[-1] if acceptance else 'unknown')
        if not started.get('task_episode_id') or not started.get('task_contract'):
            return outcomes  # Never attach today's objective to a supposedly certain historic episode.
        contract = {**started['task_contract'], 'acceptance_revisions': [dict(event_id=e['event_id'], seq=e['seq'],
                    plan=e['payload'].get('acceptance', {}).get('plan')) for e in selected
                    if e['type'] in {'acceptance.plan.accepted', 'acceptance.plan.revised'}]}
        revisions = []
        exposures, memories = [], []
        for event in selected:
            if event['type'] == 'task.goal.revised':
                revisions.append(dict(seq=event['seq'], goal_revision=event.get('goal_revision'), objective=event['payload'].get('objective')))
            if event['type'] == 'skill.retrieved':
                exposures.extend(event['payload'].get('version_ids', []))
            if event['type'] == 'memory.retrieved':
                memories.extend(event['payload'].get('sources', []))
        return dict(id=started['task_episode_id'], contract=contract, goal_revision_refs=revisions,
                    boundary_uncertain=any(r['objective'] != contract.get('objective') for r in revisions),
                    **outcomes,
                    exposure=dict(skill_versions_seen=sorted(set(exposures)), prior_memory_refs=memories))

    def verify_evidence(self, case):
        with self.store.transaction() as db:
            if not self.store.source_valid(case, db=db):
                raise ValueError('Case source is stale or unavailable')
        p = case['provenance']
        source, _ = self.trajectory._data(self.trajectory._load(case['session_id'], p['analysis_id']))
        for entry in case['evidence']:
            value = sanitize(text_value(source.resolve(entry['ref'])))
            if entry['text'] not in value:
                raise ValueError('Case evidence no longer resolves to its original trace')
        return True
