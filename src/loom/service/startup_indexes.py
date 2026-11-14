"""Small recovery indexes; existing history is indexed explicitly, never at startup.

python -m loom.service.startup_indexes --database runs/service/service.sqlite
SQLite maintains these partial indexes on every write, including writes by an older service.
"""

import argparse
from pathlib import Path

from loom.core.sqlite import write_transaction
from loom.service.contracts import ServiceError

INDEXES = {
    'semantic_jobs': ('semantic_jobs_active', "json_extract(body,'$.state') IN ('queued','running')"),
    'agent_runs': ('agent_runs_planning', "json_extract(body,'$.state')='planning'"),
}


def create(db, table):
    name, predicate = INDEXES[table]
    db.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table}(id) WHERE {predicate}')


def require(db, table):
    name, _ = INDEXES[table]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone():
        return
    # Empty/new storage needs no backfill. Existing history must never be scanned
    # by startup, even just to build an index for an otherwise cheap query.
    if not db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
        create(db, table)
        return
    path = next(r[2] for r in db.execute('PRAGMA database_list') if r[1] == 'main')
    raise ServiceError(f'Recovery index missing; run python -m loom.service.startup_indexes --database {path}', 503)


def prepare(database):
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise ValueError('Service database does not exist')
    indexed = []
    # One explicit transaction; a failed build leaves the previous database
    # usable. Concurrent publishers are serialized by SQLite and maintain the
    # indexes automatically after commit. No jobs or historical bodies change.
    with write_transaction(path) as db:
        for table in INDEXES:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                create(db, table)
                indexed.append(INDEXES[table][0])
    return indexed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    args = parser.parse_args()
    print('Ready: ' + ', '.join(prepare(args.database)))


if __name__ == '__main__':
    main()
