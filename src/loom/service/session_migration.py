"""Copy-first, offline session storage migration.

python -m loom.service.session_migration --source runs/service --destination runs/service-v2
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


def legacy(path):
    if not path.exists():
        return False
    with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as db:
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='sessions' AND type='table'").fetchone())


def backup_database(source, destination):
    if destination.exists():
        raise FileExistsError(destination)
    temporary = destination.with_suffix(destination.suffix + '.partial')
    if temporary.exists():
        raise FileExistsError(temporary)
    try:
        with sqlite3.connect(f'{source.resolve().as_uri()}?mode=ro', uri=True) as src, sqlite3.connect(temporary) as dst:
            os.chmod(temporary, 0o600)
            src.backup(dst, pages=4096)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def upgrade_guard(path):
    if not legacy(path):
        yield
        return
    with (path.parent / 'supervisor.lock').open('a+') as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Stop the running supervisor before upgrading session storage; use copy migration for a live source') from exc
        backup = path.with_name('service.before-session-v2.sqlite')
        if not backup.exists():
            backup_database(path, backup)
        yield


def clone(source, destination):
    if sys.platform == 'darwin':
        subprocess.run(['cp', '-cR', str(source), str(destination)], check=True)
    elif source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def validate(backup, migrated):
    from loom.service.contracts import canonical
    from loom.service.session_storage import load

    report = {'sessions': 0, 'messages': 0, 'snapshot_sha256': hashlib.sha256(), 'tables': {}}
    with sqlite3.connect(f'{backup.resolve().as_uri()}?mode=ro', uri=True) as old, sqlite3.connect(migrated) as new:
        new.row_factory = sqlite3.Row
        integrity = new.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise ValueError(integrity)
        for sid, body in old.execute('SELECT id,body FROM sessions ORDER BY id'):
            state = json.loads(body)
            restored = load(new, sid, deleted=True)
            if state != restored:
                raise ValueError(f'Snapshot mismatch: {sid}')
            report['sessions'] += 1
            report['messages'] += len(state.get('messages', []))
            report['snapshot_sha256'].update(canonical(restored).encode())
            old_cursor = old.execute('SELECT MAX(seq) FROM events WHERE session_id=?', (sid,)).fetchone()[0] or 0
            if state.get('event_cursor', 0) != old_cursor:
                report.setdefault('preexisting_cursor_mismatches', []).append(sid)
        for table in ('events', 'commands', 'operations', 'worker_records', 'artifacts'):
            before = old.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            after = new.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            if before != after:
                raise ValueError(f'Row count mismatch: {table}')
            report['tables'][table] = after
        report['integrity_check'] = integrity
    report['snapshot_sha256'] = report['snapshot_sha256'].hexdigest()
    return report


def copy_and_migrate(source, destination):
    from loom.service.store import SessionStore

    source, destination = source.resolve(), destination.resolve()
    if destination.exists() or source == destination or source in destination.parents:
        raise ValueError('Destination must be a new directory outside the source')
    destination.mkdir(parents=True, mode=0o700)
    backup = destination / 'service.before-session-v2.sqlite'
    print('Creating consistent database backup', flush=True)
    backup_database(source / 'service.sqlite', backup)
    clone(backup, destination / 'service.sqlite')
    # Copy immutable artifacts after the DB snapshot; later source writes cannot
    # introduce references into this frozen snapshot. Missing referenced files fail validation.
    for name in ('artifacts', 'external_tools', 'knowledge'):
        folder = source / name
        if folder.exists():
            print(f'Copying {name}', flush=True)
            clone(folder, destination / name)
    # Independent auxiliary SQLite databases need backup as well, not a raw WAL copy.
    for dbpath in destination.rglob('*.sqlite'):
        relative = dbpath.relative_to(destination)
        if len(relative.parts) == 1:
            continue
        dbpath.unlink()
        Path(str(dbpath) + '-wal').unlink(missing_ok=True)
        Path(str(dbpath) + '-shm').unlink(missing_ok=True)
        backup_database(source / relative, dbpath)
    for name in ('diagnostics', 'recovery_workspaces', 'credential'):
        if (source / name).exists():
            clone(source / name, destination / name)
    print('Migrating copied database', flush=True)
    store = SessionStore(destination)
    report = validate(backup, store.path)
    print('Checking referenced artifacts', flush=True)
    with store.read() as db:
        digests = [r[0] for r in db.execute('SELECT DISTINCT digest FROM artifacts')]
    missing = []
    for digest in digests:
        try:
            store.artifacts.read(digest)
        except Exception as exc:
            missing.append({'digest': digest, 'error': str(exc)})
    report['artifacts_checked'] = len(digests)
    report['artifact_errors'] = missing
    report['source'] = str(source)
    report['destination'] = str(destination)
    (destination / 'session-migration-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    if missing:
        raise ValueError(f'{len(missing)} artifact references are missing or corrupt; see migration report')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(copy_and_migrate(args.source, args.destination), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
