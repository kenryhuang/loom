import asyncio
import threading
import time

import pytest

from loom.evaluation.provider import bounded_http_client, create_evaluation_provider, evaluator_provider
from loom.llm.api import OpenAIProvider
from loom.tasks.config import ModelConfig, TaskRunnerConfig, create_provider_from_task_config


@pytest.mark.parametrize("selected", [None, "other"])
def test_explicit_evaluation_options_override_judge_only(selected):
    models = {name: ModelConfig(model=name, api_key="fixture", base_url="https://example.test/v1",
                               request_options={"enable_thinking": False, "reasoning_effort": "max",
                                                "parallel_tool_calls": False}) for name in ("main", "other")}
    config = TaskRunnerConfig(default_model="main", evaluation_model="main", models=models,
                              evaluation_request_options={"enable_thinking": True, "reasoning_effort": "low"})
    judge = create_evaluation_provider(config, model_name=selected).unwrap()
    wrapped = evaluator_provider(judge, output_tokens=None, timeout_seconds=180)
    body = wrapped._request_body([], None, None, stream=False)
    assert body["model"] == (selected or "main")
    assert body["enable_thinking"] is True
    assert body["reasoning_effort"] == "low"
    assert body["parallel_tool_calls"] is False
    solver = create_provider_from_task_config(config, model_name=selected).unwrap()
    assert solver.request_options["reasoning_effort"] == "max"
    assert solver.request_options["enable_thinking"] is False
    assert config.models[selected or "main"].request_options == solver.request_options


def test_evaluation_without_options_inherits_model_and_reports_creation_errors():
    config = TaskRunnerConfig(default_model="main", models={"main": ModelConfig(
        model="main", api_key="fixture", base_url="https://example.test/v1", request_options={"reasoning_effort": "high"})})
    assert create_evaluation_provider(config).unwrap().request_options == {"reasoning_effort": "high"}
    assert not create_evaluation_provider(config, model_name="missing").ok


@pytest.mark.parametrize("options", [
    {"enable_thinking": True, "reasoning_effort": "max"},
    {"enable_thinking": False, "reasoning_effort": "high"},
    {},
])
def test_evaluator_limits_generation_without_overriding_model_options(options):
    solver = OpenAIProvider(api_key='fixture', model='fixture', max_completion_tokens=65536,
                            request_options=options)
    judge = evaluator_provider(solver, output_tokens=4096, timeout_seconds=120)
    assert judge.max_completion_tokens == 4096
    assert judge.request_options == options
    assert judge.http_client is not None
    assert solver.max_completion_tokens == 65536
    assert solver.request_options == options
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
