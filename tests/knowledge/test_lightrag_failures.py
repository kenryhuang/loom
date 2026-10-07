import asyncio
import contextlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from loom.core import err, make_loom_error, ok
from loom.knowledge import lightrag_worker
from loom.service.contracts import ServiceError
from loom.tasks.config import ModelConfig, TaskRunnerConfig


@pytest.fixture
def worker(monkeypatch):
    from loom.knowledge import store

    events, settings = [], {}

    class FakeRag:
        def __init__(self, **kwargs):
            settings.update(kwargs)
            self.llm_model_func = kwargs['llm_model_func']
            self.embedding_func = kwargs['embedding_func']
            self.doc_status = self

        async def initialize_storages(self):
            pass

        async def ainsert(self, *args, **kwargs):
            pass

        async def get_by_id(self, identifier):
            return {'status': 'processed', 'chunks_count': 1}

        async def get_knowledge_graph(self, *args, **kwargs):
            return SimpleNamespace(nodes=[], edges=[], is_truncated=False)

        async def finalize_storages(self):
            settings['finalized'] = True

    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace(asarray=lambda value: value))
    monkeypatch.setitem(sys.modules, 'lightrag', SimpleNamespace(LightRAG=FakeRag, QueryParam=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'lightrag.utils', SimpleNamespace(EmbeddingFunc=SimpleNamespace))
    monkeypatch.setattr(store, 'embed', lambda profile, texts, **kwargs: [[1, 0] for _ in texts])
    monkeypatch.setattr(lightrag_worker, 'emit', events.append)
    config = TaskRunnerConfig(models={'main': ModelConfig(model='test')})
    monkeypatch.setattr(lightrag_worker, 'load_task_config', lambda _: ok(config))
    monkeypatch.setattr(lightrag_worker, 'create_provider_from_task_config', lambda *args, **kwargs: ok(settings['provider']))
    settings['provider'] = None
    value = {
        'config_path': 'unused', 'model': 'main', 'workspace': 'unused', 'profile': {}, 'dimension': 2,
        'action': 'index', 'documents': [{'id': 'doc', 'name': 'analysis.md', 'content': 'test'}], 'all_ids': ['doc'],
    }
    return FakeRag, value, events, settings


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['error', 'empty', 'length', 'exception'])
async def test_model_failure_is_reported_before_library_waits_and_stops_new_work(worker, monkeypatch, mode):
    rag, value, events, settings = worker

    class Provider:
        async def chat(self, *args, **kwargs):
            if mode == 'exception':
                raise RuntimeError('SECRET provider body')
            if mode == 'error':
                return err(make_loom_error('LLM_FAILED', 'SECRET provider body', retryable=False))
            return ok(SimpleNamespace(content='' if mode == 'empty' else 'truncated', finish_reason='length' if mode == 'length' else None))

    settings['provider'] = Provider()

    async def insert(self, *args, **kwargs):
        try:
            await self.llm_model_func('document text')
        except lightrag_worker.WorkerError:
            # LightRAG can swallow a model error and continue into other stages or wait forever.
            with pytest.raises(lightrag_worker.WorkerError):
                await self.embedding_func.func(['should not be embedded'])
            await asyncio.Event().wait()

    monkeypatch.setattr(rag, 'ainsert', insert)
    with pytest.raises(lightrag_worker.WorkerError, match='Indexing model request failed'):
        await asyncio.wait_for(lightrag_worker.execute(value), 1)
    failures = [event for event in events if 'error' in event]
    assert len(failures) == 1
    assert failures[0]['progress']['stage'] == 'extracting_failed'
    assert 'analysis.md' in failures[0]['error']
    assert 'SECRET' not in json.dumps(events)
    assert events[-1] == failures[0]
    assert not settings.get('finalized')  # No flushing a failed copy / starting more embeddings.


@pytest.mark.asyncio
async def test_embedding_failure_is_terminal_even_when_library_swallows_it(worker, monkeypatch):
    from loom.knowledge import store

    rag, value, events, settings = worker

    def broken(*args, **kwargs):
        raise ServiceError('Embedding service failed or returned invalid vectors', 502) from ValueError('SECRET invalid payload')

    async def insert(self, *args, **kwargs):
        with contextlib.suppress(lightrag_worker.WorkerError):
            await self.embedding_func.func(['first batch'])

    monkeypatch.setattr(store, 'embed', broken)
    monkeypatch.setattr(rag, 'ainsert', insert)
    with pytest.raises(lightrag_worker.WorkerError, match='Embedding failed'):
        await asyncio.wait_for(lightrag_worker.execute(value), 1)
    assert events[-1]['progress']['stage'] == 'embedding_failed'
    assert 'error' in events[-1] and 'SECRET' not in json.dumps(events)
    assert not settings.get('finalized')


@pytest.mark.asyncio
async def test_unexpected_library_failure_is_safe_and_skips_flush(worker, monkeypatch):
    rag, value, events, settings = worker

    async def insert(self, *args, **kwargs):
        raise ValueError('SECRET document text and provider body')

    monkeypatch.setattr(rag, 'ainsert', insert)
    with pytest.raises(lightrag_worker.WorkerError, match='ValueError'):
        await lightrag_worker.execute(value)
    assert events[-1]['progress']['stage'] == 'indexing_failed'
    assert 'SECRET' not in json.dumps(events)
    assert not settings.get('finalized')


@pytest.mark.asyncio
async def test_internal_failed_document_is_detected_while_pipeline_still_waits(worker, monkeypatch):
    rag, value, events, _ = worker

    async def insert(self, *args, **kwargs):
        self.failed = True
        await asyncio.Event().wait()

    async def status(self, identifier):
        return {'status': 'failed' if getattr(self, 'failed', False) else 'processing', 'error_msg': 'SECRET raw library diagnostic'}

    monkeypatch.setattr(rag, 'ainsert', insert)
    monkeypatch.setattr(rag, 'get_by_id', status)
    monkeypatch.setattr(lightrag_worker, 'DOCUMENT_STATUS_INTERVAL', 0.02)
    with pytest.raises(lightrag_worker.WorkerError, match='marked the document failed'):
        await asyncio.wait_for(lightrag_worker.execute(value), 1)
    assert events[-1]['progress']['stage'] == 'indexing_failed'
    assert 'error' in events[-1] and 'SECRET' not in json.dumps(events)


@pytest.mark.asyncio
async def test_cleanup_timeout_prevents_success_and_reports_terminal_stage(worker, monkeypatch):
    rag, value, events, _ = worker

    async def never_finishes(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(rag, 'finalize_storages', never_finishes)
    monkeypatch.setattr(lightrag_worker, 'CLEANUP_TIMEOUT', 0.02)
    with pytest.raises(lightrag_worker.WorkerError, match='cleanup time limit'):
        await asyncio.wait_for(lightrag_worker.execute(value), 1)
    assert events[-1]['progress']['stage'] == 'cleanup_failed'
    assert 'error' in events[-1]


def test_real_lightrag_queue_closes_during_an_active_request():
    pytest.importorskip('lightrag.utils')
    # Run in a child: the unfixed queue shutdown hangs asyncio.run, beyond wait_for's protection.
    script = '''
import asyncio
from types import SimpleNamespace
from lightrag.utils import priority_limit_async_func_call
from loom.knowledge.lightrag_worker import close_rag
async def run():
    started = asyncio.Event()
    @priority_limit_async_func_call(1, queue_name='regression')
    async def slow():
        started.set()
        await asyncio.sleep(60)
    request = asyncio.create_task(slow())
    await started.wait()
    rag = SimpleNamespace(embedding_func=SimpleNamespace(func=slow), role_llm_funcs={'extract': slow})
    await close_rag(rag, successful=False)
    await asyncio.gather(request, return_exceptions=True)
asyncio.run(run())
print('queue closed')
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'queue closed' in result.stdout
