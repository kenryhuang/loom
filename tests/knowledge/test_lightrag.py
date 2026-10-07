import hashlib
import json

import pytest

from loom.knowledge import crawler, lightrag_local, web_sources
from loom.knowledge.store import KnowledgeStore
from loom.service.contracts import ServiceError


def test_crawler_scope_robots_and_depth():
    seen = []
    def fetch(url, **_):
        seen.append(url)
        content = {
            'https://example.com/robots.txt': 'User-agent: *\nDisallow: /docs/private',
            'https://example.com/docs': ('<title>Docs</title><main>Welcome <a href="/docs/a">A</a>'
                                         '<a href="/other">Other</a><a href="/docs/private">Private</a></main>'),
            'https://example.com/docs/a': '<main>Details<a href="/docs/deep">Deep</a></main>',
        }[url]
        return {'url': url, 'status': 200, 'type': 'text/html', 'text': content}
    result = crawler.crawl({'url': 'https://example.com/docs', 'path_prefix': '/docs', 'max_pages': 10, 'max_depth': 1, 'delay_seconds': 0}, fetcher=fetch)
    assert len(result['pages']) == 2
    assert not result['complete']
    assert result['skipped'] == ['https://example.com/docs/private']
    assert 'https://example.com/other' not in seen
    assert result['pages'][0]['title'] == 'Docs'


def test_crawler_blocks_private_resolution_and_redirects(monkeypatch):
    monkeypatch.setattr(crawler.socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('127.0.0.1', 80))])
    with pytest.raises(ServiceError, match='public addresses'):
        crawler.fetch('http://example.com/')
    with pytest.raises(ServiceError, match='scope'):
        crawler.fetch('https://other.example/', scope=lambda _: False)
    with pytest.raises(ServiceError, match='credentials'):
        crawler.normalize('https://user:password@example.com/')


@pytest.fixture
def graph_store(tmp_path, monkeypatch):
    monkeypatch.setattr(lightrag_local, 'require', lambda: None)
    monkeypatch.setattr(lightrag_local, 'binding', lambda *a: 'fingerprint')
    store = KnowledgeStore(tmp_path, embedder=lambda p, texts: [[1, 0] for _ in texts])
    profile = store.create_profile({'name': 'test', 'endpoint': 'http://localhost:8000/embeddings', 'model': 'test'})
    base = store.create({'name': 'Graph', 'engine': 'lightrag_local', 'embedding_profile_id': profile['id'], 'indexing_model': 'main'})
    calls = []
    def call(store, base, workspace, action, **values):
        calls.append(values)
        (workspace / 'index.json').write_text(json.dumps(values['all_ids']))
        return {'entities': 2, 'relations': 1, 'graph_truncated': False, 'chunks': len(values['all_ids'])}
    monkeypatch.setattr(lightrag_local, 'call', call)
    return store, base['id'], calls


def test_failed_or_cancelled_generation_keeps_published_documents(graph_store, monkeypatch):
    store, kb, calls = graph_store
    store.index(kb, {'name': 'doc', 'content': 'Original evidence'})
    original = store.get(kb)['lightrag_generation']
    def failure(*a, **kw):
        raise ServiceError('Model offline')
    monkeypatch.setattr(lightrag_local, 'call', failure)
    with pytest.raises(ServiceError):
        store.index(kb, {'name': 'doc', 'content': 'Replacement'})
    assert store.get(kb)['lightrag_generation'] == original
    with store.connect() as db:
        assert db.execute('SELECT content FROM documents').fetchone()[0] == 'Original evidence'
    assert len(list((store.directory / 'lightrag' / kb).glob('generation_*'))) == 1


def test_website_diff_skips_model_and_only_prunes_complete_crawls(graph_store):
    store, kb, calls = graph_store
    source = web_sources.save_source(store, kb, {'url': 'https://example.com/', 'delete_missing': True})
    pages = [{'url': 'https://example.com/' + name, 'title': name, 'content': name, 'digest': hashlib.sha256(name.encode()).hexdigest()} for name in ('a', 'b')]
    complete = True
    def crawl(*_):
        return {'pages': pages, 'complete': complete, 'errors': [], 'skipped': []}
    first = web_sources.sync(store, kb, source['id'], crawler=crawl)
    assert first['updated'] == 2 and len(calls) == 1
    assert web_sources.sync(store, kb, source['id'], crawler=crawl)['unchanged'] == 2
    assert len(calls) == 1
    pages.pop()
    complete = False
    assert web_sources.sync(store, kb, source['id'], crawler=crawl)['removed'] == 0
    assert len(store.get(kb)['documents']) == 2
    complete = True
    assert web_sources.sync(store, kb, source['id'], crawler=crawl)['removed'] == 1
    assert len(store.get(kb)['documents']) == 1
    doc = store.get(kb)['documents'][0]
    assert store.read(kb, doc['id'])['source_url'] == 'https://example.com/a'


def test_cancelled_index_job_retains_previous_generation(graph_store, monkeypatch):
    import threading
    import time

    from loom.knowledge.jobs import KnowledgeJobs

    store, kb, _ = graph_store
    store.index(kb, {'name': 'doc', 'content': 'Original'})
    generation = store.get(kb)['lightrag_generation']
    started = threading.Event()
    def blocked(*a, cancelled, **kw):
        started.set()
        for _ in range(500):
            if cancelled():
                raise ServiceError('Knowledge job cancelled', 409)
            time.sleep(0.01)
        raise AssertionError('Cancellation not received')
    monkeypatch.setattr(lightrag_local, 'call', blocked)
    jobs = KnowledgeJobs(store)
    try:
        job = jobs.start(kb, {'name': 'doc', 'content': 'Changed'})
        assert started.wait(3)
        assert jobs.cancel(kb, job['id'])['cancel_requested']
        for _ in range(300):
            state = jobs.get(kb, job['id'])
            if state['state'] == 'cancelled':
                break
            time.sleep(0.01)
        assert state['state'] == 'cancelled'
        assert store.get(kb)['lightrag_generation'] == generation
    finally:
        jobs.close()
