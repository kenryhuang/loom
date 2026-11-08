"""Read-only comparison of saved Deep Eval checkpoints under current gap policy."""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from loom.evaluation.work_items import status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--limit', type=int, default=50)
    args = parser.parse_args()
    database = Path(args.database).resolve()
    totals, prior, current, gap_codes = Counter(), Counter(), Counter(), Counter()
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute("SELECT body FROM semantic_jobs WHERE session_id=? AND json_extract(body,'$.analysis_version')='goal_graph_v1' "
                          "ORDER BY rowid DESC LIMIT ?", (args.session_id, args.limit))
        for (raw,) in rows:
            job = json.loads(raw)
            cp = job.get('checkpoint') or {}
            if not cp.get('entities'):
                continue
            value = status(cp)
            units = list(cp.get('units', {}).values()) + list(cp.get('memory', {}).get('units', {}).values())
            totals[job['state']] += 1
            prior['retryable_units'] += sum(u.get('disposition') in {'abstained', 'quarantined', 'exhausted'} for u in units)
            current['retryable_units'] += value['retryable_work']
            current[value['completion_kind']] += 1
            current['unknown_judgments'] += value['unknown_judgments']
            for gap in value['gaps']:
                gap_codes[gap['code']] += 1
    print(json.dumps(dict(session_id=args.session_id, jobs=sum(totals.values()), stored_states=totals,
                          previous_policy=prior, current_policy=current, gap_codes=gap_codes,
                          model_calls=0, source_mutated=False), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
