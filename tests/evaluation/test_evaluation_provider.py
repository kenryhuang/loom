import asyncio
import threading
import time

import pytest

from loom.evaluation.provider import bounded_http_client, evaluator_provider
from loom.llm.api import OpenAIProvider


def test_evaluator_limits_generation_without_mutating_solver():
    solver = OpenAIProvider(api_key='fixture', model='fixture', max_completion_tokens=65536,
                            request_options={'enable_thinking': True, 'reasoning_effort': 'high'})
    judge = evaluator_provider(solver, output_tokens=4096, timeout_seconds=120)
    assert judge.max_completion_tokens == 4096
    assert judge.request_options['enable_thinking'] is False
    assert judge.request_options['reasoning_effort'] == 'low'
    assert judge.http_client is not None
    assert solver.max_completion_tokens == 65536
    assert solver.request_options['enable_thinking'] is True
    assert solver.http_client is None
    assert evaluator_provider(judge, output_tokens=8192, timeout_seconds=120).max_completion_tokens == 4096


def test_http_timeout_does_not_wait_for_blocking_executor_shutdown(monkeypatch):
    release, finished = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        try:
            assert kwargs['timeout'] == .02
            release.wait(5)
            raise OSError('released fixture')
        finally:
            finished.set()

    monkeypatch.setattr('urllib.request.urlopen', blocked)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            asyncio.run(bounded_http_client(.02)('https://example.test', {'body': {}, 'method': 'POST', 'headers': {}}))
        assert time.monotonic() - started < 1
    finally:
        release.set()
        assert finished.wait(1)
