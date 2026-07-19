from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.campaigns.proposer import ProposalRequest
from loom.campaigns.workspace import CandidateWorkspace
from loom.core import ok
from loom.llm import LlmResponse, TokenUsage
from loom.optimize.proposer import LoomNativeProposerAdapter


class FakeChatProvider:
    model = "proposer-model"

    def __init__(self, content: str):
        self.content = content
        self.messages = ()
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        del tools, cancellation, tool_choice
        self.calls += 1
        self.messages = tuple(messages)
        return ok(LlmResponse(content=self.content, usage=TokenUsage(10, 32, 42)))


class SequenceChatProvider:
    model = "proposer-model"

    def __init__(self, contents: tuple[str, ...]):
        self.contents = contents
        self.messages: list[tuple] = []
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        del tools, cancellation, tool_choice
        content = self.contents[self.calls]
        self.calls += 1
        self.messages.append(tuple(messages))
        return ok(LlmResponse(content=content, usage=TokenUsage(10, 32, 42)))


class RecordingHistory:
    def __init__(self):
        self.queries: list[str] = []

    async def query(self, query, arguments, *, operation_id):
        del arguments, operation_id
        self.queries.append(query)
        return ok(
            {
                "schema_version": "loom.campaign.history-envelope.v1",
                "query": query,
                "trust": "untrusted_historical_evidence",
                "data": {
                    "records": [
                        {
                            "record_id": f"{query}-1",
                            "phase": "discovery",
                            "summary": "Use bounded loop limits.",
                        }
                    ]
                },
            }
        )


def _baseline() -> dict:
    return {
        "agent.system_prompt": {"system_prompt_addendum": ""},
        "agent.tool_policy": {"allowed_tools": ["read_file", "finish"]},
        "agent.loop_policy": {"max_tool_calls_per_step": 5, "max_history_steps": 5},
        "models.solver.request_options": {"enable_thinking": False},
    }


def _request(max_candidates: int = 1) -> ProposalRequest:
    return ProposalRequest(
        "cmp_optimize",
        iteration=1,
        max_candidates=max_candidates,
        remaining_budget={"proposer_tokens": 4096, "cost": "1"},
        editable_surfaces=(
            "agent.system_prompt",
            "agent.tool_policy",
            "agent.loop_policy",
            "models.solver.request_options",
        ),
        forbidden_surfaces=("evaluator", "governance", "provider"),
    )


def _draft(*, surface: str = "agent.loop_policy") -> dict:
    if surface == "agent.loop_policy":
        operation = {
            "op": "set_limit",
            "path": "agent.loop_policy.max_tool_calls_per_step",
            "value": 4,
        }
    else:
        operation = {"op": "set", "path": f"{surface}.value", "value": "candidate"}
    return {
        "kind": "declarative_patch",
        "parent_ids": [],
        "inspiration_ids": [],
        "hypothesis": {
            "problem": "Tool loops are unnecessarily broad.",
            "mechanism": "Use a smaller bounded tool-call limit.",
            "expected_improvements": [{"metric_id": "total_tokens", "expected_delta": -100}],
            "expected_regressions": [{"metric_id": "task_success_rate", "expected_delta": -0.01}],
            "preserved_behaviors": ["workspace confinement"],
        },
        "changed_surfaces": [surface],
        "operations": [operation],
    }


def _response(*drafts: dict, fenced: bool = False) -> str:
    content = json.dumps({"drafts": list(drafts)})
    return f"```json\n{content}\n```" if fenced else content


@pytest.mark.asyncio
async def test_native_proposer_writes_bounded_single_surface_drafts(tmp_path: Path):
    provider = FakeChatProvider(_response(_draft(), fenced=True))
    history = RecordingHistory()
    workspace = CandidateWorkspace.allocate(tmp_path, "iteration-1").unwrap()
    adapter = LoomNativeProposerAdapter(provider, _baseline(), evidence_refs=("trace:seed",))

    batch = (await adapter.propose(_request(), history, workspace)).unwrap()

    assert len(batch.drafts) == 1
    assert all(len(draft.changed_surfaces) == 1 for draft in batch.drafts)
    assert all((workspace.root / draft.artifact_path).is_file() for draft in batch.drafts)
    assert all(draft.patch_path is not None and (workspace.root / draft.patch_path).is_file() for draft in batch.drafts)
    assert batch.usage.proposer_tokens == 42
    assert batch.usage.cost == "0"
    patch = json.loads((workspace.root / batch.drafts[0].patch_path).read_text(encoding="utf-8"))
    assert patch["base"] == _baseline()
    assert patch["operations"] == _draft()["operations"]
    assert batch.drafts[0].evidence_refs == ("trace:seed",)
    assert history.queries == ["campaign.frontier", "finding.search", "candidate.list"]
    prompt = "\n".join(message.content for message in provider.messages)
    assert "untrusted_historical_evidence" in prompt
    assert '"const":"declarative_patch"' in prompt
    assert "validation" not in prompt.lower()
    assert "holdout" not in prompt.lower()


@pytest.mark.asyncio
async def test_native_proposer_retries_invalid_kind_with_validation_feedback(tmp_path: Path):
    invalid = _response({**_draft(), "kind": "declarative"})
    provider = SequenceChatProvider((invalid, _response(_draft())))
    history = RecordingHistory()
    workspace = CandidateWorkspace.allocate(tmp_path, "protocol-retry").unwrap()
    adapter = LoomNativeProposerAdapter(
        provider,
        _baseline(),
        evidence_refs=("trace:seed",),
        max_protocol_retries=1,
    )

    batch = (await adapter.propose(_request(), history, workspace)).unwrap()

    assert provider.calls == 2
    assert batch.usage.proposer_tokens == 84
    assert (workspace.root / "proposer-raw-response-attempt-1.txt").read_text(encoding="utf-8") == invalid
    retry_prompt = "\n".join(message.content or "" for message in provider.messages[1])
    assert "Native proposer draft kind is not allowed" in retry_prompt
    assert '"declarative_patch"' in retry_prompt
    assert history.queries == ["campaign.frontier", "finding.search", "candidate.list"]


@pytest.mark.asyncio
async def test_native_proposer_protocol_retry_exhaustion_preserves_every_raw_response(tmp_path: Path):
    responses = (
        _response({**_draft(), "kind": "bad-a"}),
        _response({**_draft(), "kind": "bad-b"}),
    )
    provider = SequenceChatProvider(responses)
    workspace = CandidateWorkspace.allocate(tmp_path, "protocol-exhausted").unwrap()
    adapter = LoomNativeProposerAdapter(
        provider,
        _baseline(),
        evidence_refs=("trace:seed",),
        max_protocol_retries=1,
    )

    result = await adapter.propose(_request(), RecordingHistory(), workspace)

    assert result.error.code == "PROPOSAL_FAILED"
    assert result.error.retryable is False
    assert result.error.metadata["attempts"] == 2
    assert result.error.metadata["last_protocol_error"] == "Native proposer draft kind is not allowed"
    raw_paths = tuple(Path(value) for value in result.error.metadata["raw_response_paths"])
    assert len(raw_paths) == 2
    assert tuple(path.read_text(encoding="utf-8") for path in raw_paths) == responses


@pytest.mark.asyncio
async def test_native_proposer_replays_durable_result_without_provider_or_history_calls(tmp_path: Path):
    workspace = CandidateWorkspace.allocate(tmp_path, "resume").unwrap()
    first_provider = FakeChatProvider(_response(_draft()))
    first_history = RecordingHistory()
    first = LoomNativeProposerAdapter(first_provider, _baseline(), evidence_refs=("trace:seed",))
    expected = (await first.propose(_request(), first_history, workspace)).unwrap()

    second_provider = FakeChatProvider("not valid JSON")
    second_history = RecordingHistory()
    second = LoomNativeProposerAdapter(second_provider, _baseline(), evidence_refs=("trace:seed",))
    replayed = (await second.propose(_request(), second_history, workspace)).unwrap()

    assert replayed == expected
    assert first_provider.calls == 1 and second_provider.calls == 0
    assert second_history.queries == []


@pytest.mark.asyncio
async def test_native_proposer_records_malformed_raw_response(tmp_path: Path):
    workspace = CandidateWorkspace.allocate(tmp_path, "malformed").unwrap()
    adapter = LoomNativeProposerAdapter(FakeChatProvider('{"drafts": [{"kind": "declarative_patch"'), _baseline(), evidence_refs=("trace:seed",))

    result = await adapter.propose(_request(), RecordingHistory(), workspace)

    assert not result.ok
    assert result.error.code == "PROPOSAL_FAILED"
    raw_path = Path(result.error.metadata["raw_response_path"])
    assert raw_path.is_file()
    assert raw_path.is_relative_to(workspace.root)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drafts",
    [
        (_draft(), _draft()),
        ({**_draft(), "changed_surfaces": ["agent.loop_policy", "agent.system_prompt"]},),
        (_draft(surface="evaluator"),),
        ({**_draft(), "hypothesis": {**_draft()["hypothesis"], "expected_regressions": []}},),
        ({**_draft(), "artifact_path": "../../escape.json"},),
    ],
    ids=("duplicate", "multiple-surfaces", "forbidden", "missing-regression", "path-escape"),
)
async def test_native_proposer_rejects_unsafe_or_non_falsifiable_drafts(tmp_path: Path, drafts: tuple[dict, ...]):
    workspace = CandidateWorkspace.allocate(tmp_path, "invalid").unwrap()
    adapter = LoomNativeProposerAdapter(FakeChatProvider(_response(*drafts)), _baseline(), evidence_refs=("trace:seed",))

    result = await adapter.propose(_request(max_candidates=2), RecordingHistory(), workspace)

    assert not result.ok
    assert result.error.code == "PROPOSAL_FAILED"
