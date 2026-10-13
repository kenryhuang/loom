import json
import threading
import time

import pytest

from loom.knowledge.jobs import KnowledgeJobs
from loom.knowledge.store import KnowledgeStore
from loom.service.contracts import ServiceError


def wait(jobs, kb, jid, states):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = jobs.get(kb, jid)
        if job['state'] in states and jid not in jobs.cancels:
            return job
        time.sleep(.02)
    pytest.fail(str(job))


def test_folder_pause_resume_clear_and_delete_keep_original_files(tmp_path):
    source = tmp_path / 'original'
    source.mkdir()
    (source / 'one.md').write_text('First planet evidence')
    (source / 'two.md').write_text('Second planet evidence')
    store = KnowledgeStore(tmp_path / 'index')
    kb = store.create({'name': 'Planets'})['id']
    jobs = KnowledgeJobs(store)
    entered, release = threading.Event(), threading.Event()
    index = store.index
    calls = []
    def slow(kb_id, payload, **options):
        calls.append(payload['name'])
        if len(calls) == 2:
            entered.set()
            assert release.wait(5)
        return index(kb_id, payload, **options)
    store.index = slow
    try:
        job = jobs.import_local(kb, {'kind': 'folder', 'path': str(source)})
        assert entered.wait(5)
        jobs.control(kb, job['id'], 'pause')
        release.set()
        assert wait(jobs, kb, job['id'], {'paused'})['state'] == 'paused'
        assert len(store.get(kb)['documents']) == 1
        with pytest.raises(ServiceError):
            jobs.delete_base(kb)
        jobs.close()
        jobs = KnowledgeJobs(store)
        jobs.control(kb, job['id'], 'resume')
        assert wait(jobs, kb, job['id'], {'completed', 'failed'})['state'] == 'completed'
        assert len(calls) == 3  # Completed first document was not indexed again.
        documents = store.get(kb)['documents']
        assert len(documents) == 2 and all(d['source_path'].startswith(str(source)) for d in documents)
        with store.connect() as db:
            assert all(row[0] == '' for row in db.execute('SELECT content FROM documents'))
            manifest = json.loads(db.execute('SELECT body FROM job_inputs').fetchone()[0])
            assert set(manifest) == {'files', 'root', 'completed'}
        assert store.search([kb], 'planet')['matches']
        assert 'evidence' in store.read(kb, documents[0]['id'])['text']
        jobs.control(kb, job['id'], 'clear')
        with pytest.raises(ServiceError):
            jobs.get(kb, job['id'])
        jobs.delete_base(kb)
        assert (source / 'one.md').read_text() == 'First planet evidence'
        assert (source / 'two.md').read_text() == 'Second planet evidence'
        assert not store.list()
    finally:
        release.set()
        jobs.close()


def test_changed_source_is_not_reported_as_old_index_evidence(tmp_path):
    path = tmp_path / 'file.md'
    path.write_text('Original text')
    store = KnowledgeStore(tmp_path / 'index')
    kb = store.create({'name': 'Docs'})['id']
    jobs = KnowledgeJobs(store)
    try:
        job = jobs.import_local(kb, {'kind': 'file', 'path': str(path)})
        assert wait(jobs, kb, job['id'], {'completed', 'failed'})['state'] == 'completed'
        doc = store.get(kb)['documents'][0]
        path.write_text('Changed text')
        with pytest.raises(ServiceError, match='changed'):
            store.read(kb, doc['id'])
    finally:
        jobs.close()


def test_fts_website_import_replaces_titles_prunes_once_and_reimports_deleted_docs(tmp_path):
    from loom.knowledge.web_sources import save_source, sync

    store = KnowledgeStore(tmp_path / 'index')
    kb = store.create({'name': 'Web docs'})['id']
    source = save_source(store, kb, {'url': 'https://example.com/', 'delete_missing': True})
    pages = [{'url': 'https://example.com/' + name, 'title': name, 'content': 'Planet ' + name} for name in ('a', 'b')]
    def crawler(*_):
        return {'pages': pages, 'complete': True, 'errors': [], 'skipped': []}
    assert sync(store, kb, source['id'], crawler=crawler)['updated'] == 2
    pages[0]['title'] = 'A new title'
    assert sync(store, kb, source['id'], crawler=crawler)['updated'] == 1
    assert len(store.get(kb)['documents']) == 2
    pages.pop()
    assert sync(store, kb, source['id'], crawler=crawler)['removed'] == 1
    assert sync(store, kb, source['id'], crawler=crawler)['removed'] == 0
    doc = store.get(kb)['documents'][0]
    assert doc['source_url'] == 'https://example.com/a'
    store.remove_document(kb, doc['id'])
    assert sync(store, kb, source['id'], crawler=crawler)['updated'] == 1
    assert store.search([kb], 'Planet')['matches']
