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
    assert first['updated'] == 2 and len(calls) == 2
    assert web_sources.sync(store, kb, source['id'], crawler=crawl)['unchanged'] == 2
    assert len(calls) == 2
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


@pytest.mark.asyncio
async def test_worker_embedding_timeout_allows_retries_and_preserves_batches(monkeypatch):
    import sys
    from types import SimpleNamespace

    from loom.core import ok
    from loom.knowledge import lightrag_worker
    from loom.knowledge import store as store_module
    from loom.tasks.config import ModelConfig, TaskRunnerConfig

    requests, delays, settings = [], [], {}
    clock = [0]

    def embedder(profile, texts, *, timeout):
        requests.append((list(texts), timeout))
        if len(requests) < 3:
            clock[0] += timeout
            raise ServiceError('Embedding service failed', 502) from TimeoutError()
        return [[1, 0] for _ in texts]

    async def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    original_embed_batch = lightrag_worker.embed_batch

    async def embed_batch(*args):
        return await original_embed_batch(*args, sleep=sleep)

    class FakeRag:
        def __init__(self, **kwargs):
            settings.update(kwargs)
            self.doc_status = self

        async def initialize_storages(self):
            pass

        async def ainsert(self, content, **kwargs):
            vectors = await settings['embedding_func'].func([str(i) for i in range(21)])
            assert vectors == [[1, 0]] * 21
            # LightRAG 1.5.7 derives this deadline from default_embedding_timeout.
            assert clock[0] < 2 * settings['default_embedding_timeout']

        async def get_by_id(self, identifier):
            return {'status': 'processed', 'chunks_count': 21}

        async def get_knowledge_graph(self, *args, **kwargs):
            return SimpleNamespace(nodes=[], edges=[], is_truncated=False)

        async def finalize_storages(self):
            settings['finalized'] = True

    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace(asarray=lambda value: value))
    monkeypatch.setitem(sys.modules, 'lightrag', SimpleNamespace(LightRAG=FakeRag, QueryParam=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'lightrag.utils', SimpleNamespace(EmbeddingFunc=SimpleNamespace))
    monkeypatch.setattr(store_module, 'embed', embedder)
    monkeypatch.setattr(lightrag_worker, 'embed_batch', embed_batch)
    monkeypatch.setattr(lightrag_worker, 'emit', lambda value: None)
    config = TaskRunnerConfig(models={'main': ModelConfig(model='test')})
    monkeypatch.setattr(lightrag_worker, 'load_task_config', lambda path: ok(config))
    monkeypatch.setattr(lightrag_worker, 'create_provider_from_task_config', lambda *args, **kwargs: ok(None))

    result = await lightrag_worker.execute({
        'config_path': 'unused', 'model': 'main', 'workspace': 'unused', 'profile': {}, 'dimension': 2,
        'action': 'index', 'documents': [{'id': 'doc', 'name': 'test', 'content': 'test'}], 'all_ids': ['doc'],
    })
    assert [texts for texts, _ in requests] == [list(map(str, range(10)))] * 3 + [list(map(str, range(10, 20))), ['20']]
    assert [timeout for _, timeout in requests] == [60] * 5
    assert delays == [2, 4]
    assert result['chunks'] == 21
    assert settings['finalized']


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


def test_website_resume_reuses_crawl_and_only_finalized_documents(graph_store, monkeypatch):
    store, kb, _ = graph_store
    store.index(kb, {'name': 'original', 'content': 'Published evidence'})
    generation = store.get(kb)['lightrag_generation']
    source = web_sources.save_source(store, kb, {'url': 'https://example.com/'})
    pages = [{'url': f'https://example.com/{name}', 'title': name, 'content': name} for name in ('a', 'b', 'c')]
    crawls, attempted, updates = [], [], []
    fail = True
    original_call = lightrag_local.call
    def crawl(*_):
        crawls.append(1)
        return {'pages': pages, 'complete': True, 'errors': [], 'skipped': []}
    def call(*args, **kw):
        name = kw['documents'][0]['name']
        attempted.append(name)
        kw['progress'](stage='extracting', llm_calls=2, reported_tokens=100, embedding_inputs=3)
        if fail and '_b.md' in name:
            (args[2] / 'corrupt').write_text('Interrupted worker output')
            raise ServiceError('Time limit', 504)
        assert not (args[2] / 'corrupt').exists()
        return {**original_call(*args, **kw), 'usage': {'llm_calls': 2, 'reported_tokens': 100, 'embedding_inputs': 3}}
    monkeypatch.setattr(lightrag_local, 'call', call)
    with pytest.raises(ServiceError, match='1 document checkpoints retained'):
        web_sources.sync(store, kb, source['id'], crawler=crawl, progress=lambda **v: updates.append(v))
    assert store.get(kb)['lightrag_generation'] == generation
    assert len(store.get(kb)['documents']) == 1
    # A new store instance simulates service restart, without process-local recovery state.
    restarted = KnowledgeStore(store.directory, embedder=store.embedder)
    fail = False
    result = web_sources.sync(restarted, kb, source['id'], crawler=crawl, progress=lambda **v: updates.append(v))
    assert len(crawls) == 1
    assert [name.rsplit('_', 1)[-1] for name in attempted] == ['a.md', 'b.md', 'b.md', 'c.md']
    assert len(restarted.get(kb)['documents']) == 4
    assert result['usage']['llm_calls'] == 8  # Includes the failed attempt.
    assert any(v.get('resumed_documents') == 1 for v in updates)
    assert not (store.directory / 'website_snapshots' / kb / f"{source['id']}.json").exists()
    assert not (store.directory / 'lightrag' / kb / f"resume_{source['id']}").exists()


def test_crawl_resume_retries_interrupted_page_without_refetching_completed_pages():
    requested, saved = [], []
    stop = False
    def fetch(url, **_):
        nonlocal stop
        requested.append(url)
        if url.endswith('robots.txt'):
            return {'url': url, 'status': 404, 'text': ''}
        if url.endswith('/b') and len(requested) == 3:
            stop = True
            raise ServiceError('Cancelled')
        return {'url': url, 'status': 200, 'type': 'text/html', 'text': '<main>Body<a href="/b">Next</a></main>'}
    spec = {'url': 'https://example.com/', 'path_prefix': '/', 'max_pages': 10, 'max_depth': 2, 'delay_seconds': 0}
    def checkpoint(value):
        saved.append(json.loads(json.dumps(value)))
    with pytest.raises(ServiceError):
        crawler.crawl(spec, cancelled=lambda: stop, fetcher=fetch, checkpoint=checkpoint)
    stop = False
    result = crawler.crawl(spec, fetcher=fetch, resume=saved[-1])
    assert requested.count('https://example.com/') == 1
    assert requested.count('https://example.com/b') == 2
    assert len(result['pages']) == 2 and result['complete']


def test_website_settings_change_invalidates_saved_snapshot(graph_store, monkeypatch):
    store, kb, _ = graph_store
    source = web_sources.save_source(store, kb, {'url': 'https://example.com/'})
    crawls = []
    def crawl(spec, *_):
        crawls.append(spec['max_pages'])
        return {'pages': [{'url': spec['url'], 'title': 'A', 'content': 'A'}], 'complete': True, 'errors': [], 'skipped': []}
    original = lightrag_local.call
    def fail(*a, **kw):
        raise ServiceError('Offline')
    monkeypatch.setattr(lightrag_local, 'call', fail)
    with pytest.raises(ServiceError):
        web_sources.sync(store, kb, source['id'], crawler=crawl)
    web_sources.save_source(store, kb, {'max_pages': 300}, source['id'])
    monkeypatch.setattr(lightrag_local, 'call', original)
    web_sources.sync(store, kb, source['id'], crawler=crawl)
    assert crawls == [200, 300]


def test_website_budget_stops_between_documents_and_resumes(graph_store, monkeypatch):
    from types import SimpleNamespace

    from loom.knowledge import resume

    store, kb, calls = graph_store
    source = web_sources.save_source(store, kb, {'url': 'https://example.com/'})
    clock = [0]
    monkeypatch.setattr(resume, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    original = lightrag_local.call
    def call(*args, **kw):
        clock[0] += 1801
        return original(*args, **kw)
    monkeypatch.setattr(lightrag_local, 'call', call)
    def crawl(*_):
        return {'pages': [{'url': f'https://example.com/{n}', 'title': n, 'content': n} for n in ('a', 'b')],
                'complete': True, 'errors': [], 'skipped': []}
    with pytest.raises(ServiceError, match='sync budget reached'):
        web_sources.sync(store, kb, source['id'], crawler=crawl)
    assert len(calls) == 1
    assert store.get(kb)['documents'] == []
    result = web_sources.sync(store, kb, source['id'], crawler=crawl)
    assert result['updated'] == 2 and len(calls) == 2
    assert len(store.get(kb)['documents']) == 2
