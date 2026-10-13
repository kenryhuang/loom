import json

from loom.knowledge.activity import activity
from loom.knowledge.jobs import KnowledgeJobs
from loom.knowledge.store import KnowledgeStore
from loom.knowledge.web_sources import save_source


def test_activity_groups_all_attempts_by_source_and_exposes_shared_storage(tmp_path):
    store = KnowledgeStore(tmp_path)
    base = store.create({'name': 'Docs'})
    jobs = KnowledgeJobs(store)
    source = save_source(store, base['id'], {'url': 'https://example.com/docs/'})
    base.update(engine='lightrag_local', lightrag_generation='generation_current')
    index = tmp_path / 'lightrag' / base['id']
    checkpoint = index / ('resume_' + source['id'])
    checkpoint.mkdir(parents=True)
    marker = checkpoint / 'index.json'
    marker.write_text('{"done": 38}')
    try:
        with store.connect() as db:
            for number in range(30):
                job = {'id': f'job_{number}', 'kind': 'website', 'source_id': source['id'], 'document': source['url'],
                       'knowledge_base_id': base['id'], 'state': 'failed', 'created_at': f'2026-10-01T12:{number:02}:00Z',
                       'checkpointed': number, 'progress': [{'stage': 'large history omitted'}]}
                db.execute('INSERT INTO jobs VALUES(?,?,?)', (job['id'], base['id'], json.dumps(job)))
            for name in ('/first/docs', '/second/docs'):
                job = {'id': name, 'kind': 'local', 'document': name, 'knowledge_base_id': base['id'], 'state': 'completed'}
                db.execute('INSERT INTO jobs VALUES(?,?,?)', (job['id'], base['id'], json.dumps(job)))
        groups = activity(store, jobs, base)
        assert len(groups) == 3
        web = next(g for g in groups if g['kind'] == 'website')
        assert web['latest']['id'] == 'job_29'
        assert len(web['history']) == 29  # Not truncated by the old 20-job limit.
        assert 'progress' not in web['history'][0]
        assert web['index_directory'] == str(index)
        assert web['published_directory'] == str(index / 'generation_current')
        assert web['checkpoint_directory'] == str(checkpoint)
        jobs.control(base['id'], 'job_10', 'clear')
        assert marker.read_text() == '{"done": 38}'
        assert len(next(g for g in activity(store, jobs, base) if g['kind'] == 'website')['history']) == 28
        # A resumed older job becomes latest even though its database row is old.
        with store.connect() as db:
            row = json.loads(db.execute("SELECT body FROM jobs WHERE id='job_2'").fetchone()[0])
            row.update(state='running', started_at='2026-10-02T00:00:00Z')
            db.execute("UPDATE jobs SET body=? WHERE id='job_2'", (json.dumps(row),))
        assert next(g for g in activity(store, jobs, base) if g['kind'] == 'website')['latest']['id'] == 'job_2'
    finally:
        jobs.close()
