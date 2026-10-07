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


def test_crawler_encodes_unicode_urls_without_double_encoding():
    url = crawler.normalize("https://例子.com/文档/hello%20world?q=中文#标题")
    assert url == "https://xn--fsqu00a.com/%E6%96%87%E6%A1%A3/hello%20world?q=%E4%B8%AD%E6%96%87"
    assert crawler.normalize(url) == url


@pytest.mark.asyncio
async def test_embedding_retries_transport_failures_and_preserves_batch():
    from loom.knowledge.lightrag_worker import embed_batch
    attempts, events, delays = [], [], []
    def embedder(profile, texts):
        attempts.append(texts)
        if len(attempts) < 3:
            raise ServiceError('Embedding service failed', 502) from TimeoutError()
        return [[1, 0]]
    async def sleep(delay):
        delays.append(delay)
    vectors = await embed_batch(embedder, {}, ['same input'], lambda **v: events.append(v), sleep=sleep)
    assert vectors == [[1, 0]]
    assert attempts == [['same input']] * 3
    assert delays == [2, 4]
    assert [e['retry'] for e in events] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize('cause,expected', [(ValueError(), 1), (TimeoutError(), 3)])
async def test_embedding_retry_is_bounded_and_rejects_invalid_vectors(cause, expected):
    from loom.knowledge.lightrag_worker import embed_batch
    attempts = []
    def embedder(*_):
        attempts.append(1)
        raise ServiceError('Embedding failed', 502) from cause
    async def sleep(_):
        pass
    with pytest.raises(ServiceError):
        await embed_batch(embedder, {}, ['input'], lambda **_: None, sleep=sleep)
    assert len(attempts) == expected


def test_raw_utf8_redirect_header_preserves_chinese_path(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import quote

    paths = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            paths.append(self.path)
            if self.path == quote('/中文'):
                self.send_response(301)
                self.send_header('Location', '/中文/'.encode().decode('latin-1'))
            else:
                self.send_response(200 if self.path == quote('/中文/') else 404)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'<main>Actual page</main>')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(crawler, 'public_addresses', lambda *_: ['127.0.0.1'])
    try:
        result = crawler.fetch(f'http://example.test:{server.server_port}/中文')
        assert result['status'] == 200
        assert paths == [quote('/中文'), quote('/中文/')]
        assert result['url'].endswith(quote('/中文/'))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_crawl_retry_is_bounded_and_rotates_validated_address(monkeypatch):
    attempts, updates = [], []
    def request(url, **options):
        attempts.append(options['address_index'])
        if len(attempts) == 1:
            raise ServiceError('Request timed out') from TimeoutError()
        return {'url': url, 'status': 200, 'text': 'ok'}
    monkeypatch.setattr(crawler, '_fetch', request)
    assert crawler.fetch('https://example.com/', progress=lambda **v: updates.append(v))['status'] == 200
    assert attempts == [0, 1]
    assert updates[0]['stage'] == 'crawl_retry'


def test_crawl_counts_missing_urls_and_deduplicates_redirect_targets():
    visited = []
    def fetch(url, **_):
        visited.append(url)
        if url.endswith('robots.txt'):
            return {'url': url, 'status': 404, 'text': ''}
        if url.endswith('/gone'):
            return {'url': url, 'status': 404, 'text': ''}
        return {'url': url + '/' if url.endswith('/doc') else url, 'status': 200, 'type': 'text/html',
                'text': '<main>Hello<a href="/doc">Document</a><a href="/doc/">Alias</a><a href="/gone">Missing</a></main>'}
    result = crawler.crawl({'url': 'https://example.com/', 'path_prefix': '/', 'max_pages': 3, 'max_depth': 2, 'delay_seconds': 0}, fetcher=fetch)
    assert result['visited'] == 3 and len(result['pages']) == 2
    assert result['missing'] == [{'url': 'https://example.com/gone', 'status': 404}]
    assert 'https://example.com/doc/' not in visited
    assert result['complete']


def test_partial_crawl_job_is_terminal_but_does_not_claim_full_sync(graph_store, monkeypatch):
    import time

    from loom.knowledge.jobs import KnowledgeJobs

    store, kb, _ = graph_store
    spec = web_sources.save_source(store, kb, {'url': 'https://example.com/'})
    monkeypatch.setattr(web_sources, 'sync', lambda *a, **kw: {'complete': False, 'pages': 1, 'chunks': 1, 'incomplete_reasons': ['page_limit']})
    jobs = KnowledgeJobs(store)
    try:
        job = jobs.sync(kb, spec['id'])
        for _ in range(200):
            value = jobs.get(kb, job['id'])
            if value['state'] == 'completed':
                break
            time.sleep(0.01)
        assert value['state'] == 'completed'
        assert value['stage'] == 'partial'
        assert not value['result']['complete']
    finally:
        jobs.close()
