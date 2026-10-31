import asyncio
from types import SimpleNamespace

import pytest

from loom.llm import LlmResponse
from loom.llm.context_budget import estimate_message_tokens, model_context_window
from loom.tasks.acceptance import AcceptanceBlocked
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest


def controller(provider, config=None):
    assembly = TaskAssembly(TaskRequest("Answer", task_spec={"acceptance": config or {}}), plan_mode="off")
    assembly.configure_acceptance(SimpleNamespace(model="qwen3.8-max"), verification_provider=provider)
    calls = []

    async def request(runtime, stage, prompt, content, timeout):
        calls.append(content)
        return LlmResponse(content='{"results": []}')

    assembly.acceptance_model_request = request
    return assembly.acceptance, calls


def test_large_evidence_uses_model_context_and_reaches_provider_intact_after_restore():
    gate, calls = controller(SimpleNamespace(model="qwen3.8-max"))
    # Older checkpoints contain neither input-budget metadata nor capacity settings.
    gate.restore(gate.snapshot())
    content = "Evidence text. " * 5000 + "行星百科证据" * 1000
    asyncio.run(gate.model(None, "verify", "Verify", {"evidence": content}))
    assert len(calls[0]) > 48000
    assert content in calls[0]
    budget = gate.public()["input_budget"]
    assert budget["max_input_tokens"] == 600000
    assert budget["context_source"] == "model_metadata"
    assert budget["estimated_input_tokens"] < budget["max_input_tokens"]


def test_budget_uses_separate_verifier_and_reserves_output_space():
    gate, calls = controller(SimpleNamespace(model="custom-judge", context_window_tokens=10000, max_completion_tokens=9000),
                             {"max_output_tokens": 8000})
    budget = gate.input_budget()
    assert budget["max_input_tokens"] == 6000
    assert budget["max_output_tokens"] == 4000
    assert budget["context_source"] == "configured"
    with pytest.raises(AcceptanceBlocked, match="estimated.*60% of 10000"):
        asyncio.run(gate.model(None, "verify", "Review", {"evidence": "证据" * 2000}))
    assert not calls
    assert gate.data["model_calls"] == 0
    assert gate.public()["input_budget"]["estimated_input_tokens"] > 6000


@pytest.mark.parametrize("cap", [None, 48000])
def test_explicit_legacy_character_cap_is_optional(cap):
    gate, calls = controller(SimpleNamespace(model="qwen3.8-max"), {"max_prompt_chars": cap})
    request = gate.model(None, "verify", "Review", {"evidence": "x" * 50000})
    if cap is None:
        asyncio.run(request)
        assert calls
    else:
        with pytest.raises(AcceptanceBlocked, match="explicit task max_prompt_chars is 48000"):
            asyncio.run(request)
        assert not calls


def test_unknown_capacity_is_labeled_and_configured_value_overrides_known_model():
    assert model_context_window(SimpleNamespace(model="unknown")) == (32768, "fallback")
    assert model_context_window(SimpleNamespace(model="qwen3.8-max", context_window_tokens=64000)) == (64000, "configured")
    assert estimate_message_tokens("中文") > estimate_message_tokens("ab")


@pytest.mark.parametrize("percent", [0, 96, True, 0.6])
def test_invalid_context_percent_is_rejected(percent):
    with pytest.raises(ValueError, match="context_window_percent"):
        controller(SimpleNamespace(model="unknown"), {"context_window_percent": percent})


def test_reasoning_defaults_leave_room_for_answer_without_overriding_explicit_caps():
    provider = SimpleNamespace(model='qwen3.8-max', request_options={'enable_thinking': True}, max_completion_tokens=8192)
    gate, _ = controller(provider)
    assert gate.input_budget()['max_output_tokens'] == 8192
    assert gate.config['model_timeout_seconds'] == 180
    assert gate.config['max_seconds'] == 360
    # Explicit task caps stay authoritative even when they may be insufficient.
    limited, _ = controller(provider, {'max_output_tokens': 1000, 'max_seconds': 90, 'model_timeout_seconds': 30})
    assert limited.input_budget()['max_output_tokens'] == 1000
    assert limited.config['max_seconds'] == 90
    assert limited.config['model_timeout_seconds'] == 30
    provider.max_completion_tokens = 2000
    assert gate.input_budget()['max_output_tokens'] == 2000
    provider.request_options = {'enable_thinking': False}
    assert gate.config['max_seconds'] == 180
    assert gate.config['max_output_tokens'] == 4096
