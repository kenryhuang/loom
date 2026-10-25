"""Read-only saved-evaluation migration audit; never calls a model or writes the store.

python -m loom.evaluation.replay_audit --database runs/service/service.sqlite --evaluation-id evaluation_...
Reports mechanical coverage and cost only, not semantic correctness.
"""

import argparse
import json
import sqlite3
from copy import deepcopy
from pathlib import Path

from loom.evaluation.evidence_delivery import prepare
from loom.evaluation.goal_loops.attribution import action_attributions
from loom.evaluation.goal_loops.checkpoint_repair import reconcile_checkpoint
from loom.evaluation.memory.contracts import FIELDS
from loom.evaluation.memory.lifecycle import VERSION, migrate
from loom.evaluation.memory.request import messages_for
from loom.evaluation.work_items import status


def audit(job):
    cp = deepcopy(job['checkpoint'])
    original = deepcopy(cp)
    cards = cp['extraction']['cards']
    reconcile_checkpoint(cp, cards)
    cp['action_attributions'] = action_attributions(cp, cards)
    profiles = []
    if cp.get('memory'):
        expected = {**cp['memory']['identity'], 'version': VERSION, 'graph_revision': cp['graph_revision']}
        memory = migrate(cp, expected)
        for unit in memory['units'].values():
            if not unit.get('complete'):
                prepare(unit)
                profiles.append(dict(unit_id=unit['id'], spans=len(unit['manifest']['handles']),
                                     request_chars=messages_for(unit, 'text')[1]['total_chars']))
    before = original.get('memory', {}).get('records', {})
    after = cp.get('memory', {}).get('records', {})
    preserved = sum(key in after and all(after[key].get(f) == row.get(f) for f in FIELDS[row['kind']]['items']['properties'])
                    for key, row in before.items())
    return dict(evaluation_id=job['id'], session_id=job.get('session_id'), model=job.get('model'),
                stored_usage=job.get('usage'), source_digest=cp['identity']['source'],
                new_model_calls=0, usage_unchanged=cp['usage'] == original['usage'],
                graph_revision_before=original['graph_revision'], graph_revision_after=cp['graph_revision'],
                previous_stages_preserved=all(cp['stages'].get(k) == v for k, v in original['stages'].items()),
                previous_records=len(before), preserved_claims=preserved,
                published_records=sum(r.get('publication_state') == 'published' for r in after.values()),
                quarantined_records=len(cp.get('memory', {}).get('quarantine', {})),
                migration_status=status(cp), pending_request_profiles=profiles,
                semantic_quality='not_assessed_by_mechanical_replay')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--evaluation-id', action='append', required=True)
    args = parser.parse_args()
    with sqlite3.connect(Path(args.database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        reports = []
        for identifier in args.evaluation_id:
            row = db.execute('SELECT body FROM semantic_jobs WHERE id=?', (identifier,)).fetchone()
            if row is None:
                parser.error('Evaluation not found: ' + identifier)
            reports.append(audit(json.loads(row[0])))
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
