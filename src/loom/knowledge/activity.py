"""Source-level import activity, with compact attempt history and storage locations."""

import json

HISTORY_FIELDS = ('id', 'kind', 'state', 'stage', 'created_at', 'started_at', 'finished_at', 'updated_at',
                  'elapsed_seconds', 'error', 'checkpointed', 'indexed', 'total')


def activity(store, jobs, base):
    kb_id = base['id']
    engine = base['engine']
    directory = store.directory / {'lightrag_local': 'lightrag', 'yakdb_local': 'yakdb'}.get(engine, '')
    generation = base.get('lightrag_generation') or base.get('yakdb_generation')
    if engine in {'lightrag_local', 'yakdb_local'}:
        directory /= kb_id
    storage = {'index_directory': str(directory), 'published_directory': str(directory / generation) if generation else None,
               'index_file': str(store.path) if engine in {'sqlite_fts', 'sqlite_hybrid'} else None}
    with jobs.lock, store.connect() as db:
        records = [json.loads(r[0]) for r in db.execute('SELECT body FROM jobs WHERE kb_id=? ORDER BY rowid DESC', (kb_id,))]
        sources = {r[0]: json.loads(r[1]) for r in db.execute('SELECT id,body FROM web_sources WHERE kb_id=?', (kb_id,))}
        # Resuming an old attempt makes it current without changing its row ID.
        records.sort(key=lambda j: (j['state'] in {'queued', 'running', 'paused'}, j.get('started_at') or j.get('created_at') or ''), reverse=True)
        groups = {}
        for job in records:
            kind = job.get('kind', 'document')
            key = kind + ':' + (job.get('source_id') or job.get('document') or job['id'])
            if key in groups:
                groups[key]['history'].append({k: job[k] for k in HISTORY_FIELDS if k in job})
                continue
            spec = sources.get(job.get('source_id'), {})
            checkpoint = directory / f"resume_{job['source_id']}" if engine == 'lightrag_local' and job.get('source_id') else None
            snapshot = store.directory / 'website_snapshots' / kb_id / f"{job['source_id']}.json" if job.get('source_id') else None
            groups[key] = {
                'id': key, 'kind': kind, 'source': spec.get('url') or job.get('document', ''),
                'source_id': job.get('source_id'), 'path_scope': spec.get('path_prefix'), **storage,
                'checkpoint_directory': str(checkpoint) if checkpoint and checkpoint.is_dir() else None,
                'snapshot_file': str(snapshot) if snapshot and snapshot.is_file() else None,
                'latest': jobs.get(kb_id, job['id']), 'history': [],
            }
        return list(groups.values())
