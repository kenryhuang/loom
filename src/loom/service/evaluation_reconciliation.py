"""Explicit, zero-model-call repair of stopped evaluations' framework obligations.

python -m loom.service.evaluation_reconciliation --data-dir runs/service --evaluation-id evaluation_... [--apply]
"""

import argparse
import json
from copy import deepcopy

from loom.core import now_iso
from loom.evaluation.goal_loops.attribution import action_attributions
from loom.evaluation.goal_loops.graph_projection import project
from loom.evaluation.issues import describe
from loom.evaluation.memory.obligations import VERSION, reconcile_framework_obligations
from loom.evaluation.memory.request import scope
from loom.evaluation.work_items import status
from loom.service.contracts import ServiceError, canonical
from loom.service.semantic import SessionSemantic, _behavior_facts
from loom.service.trajectory import evidence_store


def reconcile(store, evaluation_id, *, apply=False):
    # Serialize with normal service writes. Never touch queued/running jobs or
    # instantiate a worker manager, which would interrupt other evaluations.
    with store.transaction() as db:
        saved = db.execute('SELECT body FROM semantic_jobs WHERE id=?', (evaluation_id,)).fetchone()
        if not saved:
            raise ServiceError('Evaluation not found', 404)
        original = json.loads(saved[0])
        if original.get('analysis_version') != 'goal_graph_v1' or original['state'] not in {'incomplete', 'completed'}:
            raise ServiceError('Framework reconciliation requires a settled graph evaluation', 409)
        job = deepcopy(original)
        cp = job['checkpoint']
        cards = cp['extraction']['cards']
        memberships = action_attributions(cp, cards)
        for unit in cp.get('memory', {}).get('units', {}).values():
            if unit.get('mode') == 'extract' and 'pending_response' not in unit:
                ids = {a['id'] for a in unit['scope']['actions']}
                unit['scope'] = scope(cp, [c for c in cards if c['id'] in ids], memberships)
        retired = reconcile_framework_obligations(cp)
        result = dict(evaluation_id=evaluation_id, retired_candidates=retired, applied=False, model_calls=0, status=status(cp))
        if not apply or not retired:
            return result
        parent = db.execute('SELECT body FROM session_analyses WHERE id=? AND session_id=?',
                            (job['analysis_id'], job['session_id'])).fetchone()
        if not parent:
            raise ServiceError('Original source analysis not found', 404)
        parent = json.loads(parent[0])
        source = evidence_store(json.loads(store.artifacts.read(parent['source']['sha256'])))
        if source.source_sha256 != cp['identity']['source']:
            raise ServiceError('Original source digest changed', 409)
        factual = json.loads(store.artifacts.read(parent['result']['sha256']))
        bundle = project(source, _behavior_facts(source, factual, job), job['run_id'], cp)
        payload = SessionSemantic._result(job, source, factual, bundle)
        assert cp['usage'] == original['checkpoint']['usage'] and job['usage'] == original['usage']

        def artifact(value, kind):
            ref = store.artifact_in_transaction(db, job['session_id'], value, kind)
            db.execute('INSERT OR IGNORE INTO evaluation_artifacts VALUES(?,?,?)',
                       (job['id'], job['session_id'], ref['sha256']))
            return ref

        backup = artifact(original, 'evaluation_reconciliation_backup')
        job['result'] = artifact(payload, 'trajectory_evaluation')
        job.setdefault('reconciliations', []).append(dict(version=VERSION, at=now_iso(), backup=backup,
                                                         retired_candidates=retired, model_calls=0))
        assessment = status(cp)
        job.update(assessment)
        details = bundle['coverage'].get('review_gaps', []) + bundle['memory']['coverage'].get('issues', [])
        job.update(state='completed' if assessment['result_state'] == 'ready' else 'incomplete', error_details=details, error=None)
        if job['state'] == 'incomplete':
            job['error'] = 'Processing finished with quality gaps. Accepted records remain available. Use Retry failed items.'
            if details:
                job['error'] += ' ' + '; '.join(describe(e) for e in details[:2])
            elif assessment['unknown_judgments']:
                job['error'] += f" {assessment['unknown_judgments']} judgment(s) remain unknown because evidence is insufficient."
        db.execute('UPDATE semantic_jobs SET body=? WHERE id=?', (canonical(job), job['id']))
        return {**result, 'applied': True, 'state': job['state'], 'backup': backup['sha256'], 'status': assessment}


def main():
    from loom.service.store import SessionStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--evaluation-id', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(reconcile(SessionStore(args.data_dir), args.evaluation_id, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
