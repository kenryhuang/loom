# Trace Driven Evolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first trace-driven evolution analyzer: offline JSONL trace input, step episode construction, LLM step scoring, deterministic signal/proposal generation, artifact persistence, and a CLI.

**Architecture:** Add focused modules under `loom.evolution` instead of expanding `mutations.py`. Runtime remains unchanged. The analyzer consumes persisted trace records, asks a provider to score complete step episodes, aggregates repeated attribution patterns, writes evolution artifacts, and never applies mutations.

**Tech Stack:** Python 3.11, frozen dataclasses, async provider calls, JSONL files, existing `Result`/`LlmMessage`/`LlmResponse` contracts, pytest, uv, ruff.

---

## File Structure

- Create `src/loom/evolution/episodes.py`
  - Parse JSONL trace records.
  - Normalize persisted `event` and `trace` records.
  - Group records into `StepEpisode` objects by `run_id` and `trace_id`.

- Create `src/loom/evolution/scoring.py`
  - Define `StepScore` and scoring prompt helpers.
  - Call an injected LLM provider.
  - Parse and validate score JSON.

- Create `src/loom/evolution/proposals.py`
  - Define `EvolutionSignal`, `EvolutionProposal`, and proposal gate config.
  - Aggregate repeated score attributions.
  - Generate small, gated proposal records.

- Create `src/loom/evolution/artifacts.py`
  - Write JSONL artifacts for scores, signals, and proposals.
  - Render a compact markdown/plain-text report.

- Create `src/loom/evolution/analyze.py`
  - Provide `AnalyzeConfig`, `AnalyzeResult`, `analyze_trace`, argparse parsing, and `python -m loom.evolution.analyze`.

- Modify `src/loom/evolution/__init__.py`
  - Export the new public contracts and `analyze_trace`.

- Create tests:
  - `tests/evolution/test_evolution_episodes.py`
  - `tests/evolution/test_evolution_scoring.py`
  - `tests/evolution/test_evolution_proposals.py`
  - `tests/evolution/test_evolution_artifacts.py`
  - `tests/evolution/test_evolution_analyze.py`
  - `tests/integration/test_trace_driven_evolution.py`

---

### Task 1: Trace Episode Builder

**Files:**
- Create: `src/loom/evolution/episodes.py`
- Test: `tests/evolution/test_evolution_episodes.py`

- [ ] **Step 1: Write the failing episode builder tests**

Create `tests/evolution/test_evolution_episodes.py`:

```python
import json

from loom.evolution.episodes import StepEpisode, build_step_episodes, load_trace_records


def _event(event_type, trace_id="trace-1", run_id="run-1", payload=None):
    payload = {
        "type": event_type,
        "run_id": run_id,
        "loop_id": "loop-1",
        "trace_id": trace_id,
        "step_number": 0,
        **(payload or {}),
    }
    return {"type": "event", "eventType": event_type, "traceId": trace_id, "payload": payload, "hash": f"hash-{event_type}"}


def _trace(trace_id="trace-1", run_id="run-1"):
    return {
        "type": "trace",
        "id": trace_id,
        "runId": run_id,
        "payload": {
            "id": trace_id,
            "run_id": run_id,
            "loop_id": "loop-1",
            "loop_version": "v1",
            "step_number": 0,
            "root_trace_id": trace_id,
            "started_at": "2026-06-28T00:00:00Z",
            "ended_at": "2026-06-28T00:00:01Z",
            "duration_ms": 1,
            "input_context_id": "ctx-in",
            "output_context_id": "ctx-out",
            "outcome": "pass",
            "metadata": {"tokenUsage": {"totalTokens": 42}},
        },
        "hash": "hash-trace",
    }


def test_load_trace_records_reads_jsonl_in_order(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in (_event("run.started", trace_id=None), _event("step.started"), _trace()))
        + "\n",
        encoding="utf-8",
    )

    records = load_trace_records(path)

    assert [record.record_type for record in records] == ["event", "event", "trace"]
    assert records[1].event_type == "step.started"
    assert records[2].trace_id == "trace-1"


def test_build_step_episodes_groups_llm_tool_and_completed_trace():
    records = (
        _event("run.started", trace_id=None),
        _event("step.started"),
        _event("llm.requested", payload={"messages": [{"role": "user", "content": "inspect"}]}),
        _event("llm.completed", payload={"response": {"content": "use tool"}}),
        _event("tool.started", payload={"tool_id": "inspect-project", "input": {}}),
        _event("tool.completed", payload={"tool_id": "inspect-project", "output": {"ok": True}}),
        _event("step.completed"),
        _trace(),
        _event("run.completed", trace_id=None),
    )

    episodes = build_step_episodes(records)

    assert len(episodes) == 1
    episode = episodes[0]
    assert isinstance(episode, StepEpisode)
    assert episode.run_id == "run-1"
    assert episode.trace_id == "trace-1"
    assert episode.loop_id == "loop-1"
    assert episode.step_number == 0
    assert episode.complete is True
    assert [event["type"] for event in episode.llm_requests] == ["llm.requested"]
    assert [event["type"] for event in episode.llm_completions] == ["llm.completed"]
    assert [event["type"] for event in episode.tool_events] == ["tool.started", "tool.completed"]
    assert episode.completed_trace["id"] == "trace-1"


def test_build_step_episodes_marks_missing_completion_incomplete():
    episodes = build_step_episodes((_event("step.started"), _event("llm.requested")))

    assert len(episodes) == 1
    assert episodes[0].complete is False
    assert episodes[0].completed_event is None
    assert episodes[0].completed_trace is None
```

- [ ] **Step 2: Run the episode tests to verify they fail**

Run:

```bash
uv run pytest tests/evolution/test_evolution_episodes.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'loom.evolution.episodes'`.

- [ ] **Step 3: Implement `episodes.py`**

Create `src/loom/evolution/episodes.py`:

```python
"""Trace episode construction for offline evolution analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class TraceRecord:
    record_type: str
    payload: dict[str, Any]
    event_type: str | None = None
    trace_id: str | None = None
    run_id: str | None = None
    hash: str | None = None


@dataclass(frozen=True, slots=True)
class StepEpisode:
    run_id: str
    trace_id: str
    loop_id: str
    step_number: int
    started_event: dict[str, Any] | None
    llm_requests: tuple[dict[str, Any], ...] = ()
    llm_completions: tuple[dict[str, Any], ...] = ()
    tool_events: tuple[dict[str, Any], ...] = ()
    action_events: tuple[dict[str, Any], ...] = ()
    observation_events: tuple[dict[str, Any], ...] = ()
    completed_trace: dict[str, Any] | None = None
    completed_event: dict[str, Any] | None = None
    event_hashes: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.completed_trace is not None and self.completed_event is not None


def load_trace_records(path: str | Path) -> tuple[TraceRecord, ...]:
    records: list[TraceRecord] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        records.append(_record_from_raw(raw))
    return tuple(records)


def build_step_episodes(records: tuple[TraceRecord | dict[str, Any], ...]) -> tuple[StepEpisode, ...]:
    normalized = tuple(record if isinstance(record, TraceRecord) else _record_from_raw(record) for record in records)
    buckets: dict[str, dict[str, Any]] = {}

    for record in normalized:
        if record.trace_id is None:
            continue
        bucket = buckets.setdefault(
            record.trace_id,
            {
                "run_id": record.run_id or "",
                "trace_id": record.trace_id,
                "loop_id": "",
                "step_number": 0,
                "started_event": None,
                "llm_requests": [],
                "llm_completions": [],
                "tool_events": [],
                "action_events": [],
                "observation_events": [],
                "completed_trace": None,
                "completed_event": None,
                "event_hashes": [],
            },
        )
        if record.run_id:
            bucket["run_id"] = record.run_id
        if record.hash:
            bucket["event_hashes"].append(record.hash)
        payload = record.payload
        if payload.get("loop_id"):
            bucket["loop_id"] = payload["loop_id"]
        if payload.get("step_number") is not None:
            bucket["step_number"] = int(payload["step_number"])

        if record.record_type == "trace":
            bucket["completed_trace"] = payload
            bucket["loop_id"] = payload.get("loop_id", bucket["loop_id"])
            bucket["step_number"] = int(payload.get("step_number", bucket["step_number"]))
            continue

        event_type = record.event_type or payload.get("type")
        if event_type == "step.started":
            bucket["started_event"] = payload
        elif event_type == "step.completed":
            bucket["completed_event"] = payload
        elif event_type == "llm.requested":
            bucket["llm_requests"].append(payload)
        elif event_type == "llm.completed":
            bucket["llm_completions"].append(payload)
        elif event_type and event_type.startswith("tool."):
            bucket["tool_events"].append(payload)
        elif event_type and event_type.startswith("action."):
            bucket["action_events"].append(payload)
        elif event_type and event_type.startswith("observation."):
            bucket["observation_events"].append(payload)

    return tuple(_episode_from_bucket(bucket) for bucket in buckets.values())


def _record_from_raw(raw: dict[str, Any]) -> TraceRecord:
    record_type = str(raw.get("type", ""))
    payload = dict(raw.get("payload") or {})
    event_type = raw.get("eventType")
    trace_id = raw.get("traceId") or payload.get("trace_id") or payload.get("id")
    run_id = raw.get("runId") or payload.get("run_id")
    return TraceRecord(record_type, payload, event_type, trace_id, run_id, raw.get("hash"))


def _episode_from_bucket(bucket: dict[str, Any]) -> StepEpisode:
    return StepEpisode(
        run_id=bucket["run_id"],
        trace_id=bucket["trace_id"],
        loop_id=bucket["loop_id"],
        step_number=bucket["step_number"],
        started_event=bucket["started_event"],
        llm_requests=tuple(bucket["llm_requests"]),
        llm_completions=tuple(bucket["llm_completions"]),
        tool_events=tuple(bucket["tool_events"]),
        action_events=tuple(bucket["action_events"]),
        observation_events=tuple(bucket["observation_events"]),
        completed_trace=bucket["completed_trace"],
        completed_event=bucket["completed_event"],
        event_hashes=tuple(bucket["event_hashes"]),
    )
```

- [ ] **Step 4: Run the episode tests to verify they pass**

Run:

```bash
uv run pytest tests/evolution/test_evolution_episodes.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/loom/evolution/episodes.py tests/evolution/test_evolution_episodes.py
git commit -m "feat: build evolution trace episodes"
```

---

### Task 2: LLM Step Scoring

**Files:**
- Create: `src/loom/evolution/scoring.py`
- Test: `tests/evolution/test_evolution_scoring.py`

- [ ] **Step 1: Write the failing scoring tests**

Create `tests/evolution/test_evolution_scoring.py`:

```python
import asyncio
import json

from loom.core import ok
from loom.evolution.episodes import StepEpisode
from loom.evolution.scoring import LlmStepScorer, StepScore, build_step_scoring_messages, parse_step_score
from loom.llm import LlmResponse, TokenUsage


class FakeScoreProvider:
    model = "fake-score-model"

    def __init__(self, content):
        self.content = content
        self.messages = None

    async def chat(self, messages, tools=None, cancellation=None):
        self.messages = messages
        return ok(LlmResponse(content=self.content, usage=TokenUsage(11, 7, 18)))


def _episode():
    return StepEpisode(
        run_id="run-1",
        trace_id="trace-1",
        loop_id="loop-1",
        step_number=0,
        started_event={"type": "step.started"},
        llm_requests=({"type": "llm.requested", "messages": [{"role": "user", "content": "audit"}]},),
        llm_completions=({"type": "llm.completed", "response": {"content": "bad json"}},),
        tool_events=({"type": "tool.completed", "tool_id": "inspect-project", "input": {}, "output": {"ok": True}},),
        completed_trace={"id": "trace-1", "outcome": "pass", "metadata": {"tokenUsage": {"totalTokens": 42}}},
        completed_event={"type": "step.completed"},
        event_hashes=("hash-1",),
    )


def _score_json():
    return json.dumps(
        {
            "overall": 0.62,
            "dimensions": {
                "task_progress": 0.7,
                "evidence_grounding": 0.5,
                "tool_choice_quality": 0.8,
                "tool_argument_quality": 0.9,
                "context_relevance": 0.6,
                "prompt_following": 0.4,
                "cost_efficiency": 0.7,
                "failure_recovery": 0.3,
            },
            "attribution": {"system_prompt": ["Output format was too easy to violate."], "tool_schema": []},
            "proposed_fixes": ["Clarify required JSON output shape."],
            "confidence": 0.81,
        }
    )


def test_build_step_scoring_messages_contains_episode_evidence():
    messages = build_step_scoring_messages(_episode())

    assert messages[0].role == "system"
    assert "step evolution judge" in messages[0].content.lower()
    assert "trace-1" in messages[1].content
    assert "inspect-project" in messages[1].content
    assert "tokenUsage" in messages[1].content


def test_parse_step_score_returns_structured_score():
    score = parse_step_score(_score_json(), _episode(), evaluator_model="fake-score-model", token_usage=TokenUsage(1, 2, 3)).unwrap()

    assert isinstance(score, StepScore)
    assert score.trace_id == "trace-1"
    assert score.overall == 0.62
    assert score.dimensions["prompt_following"] == 0.4
    assert score.attribution["system_prompt"] == ("Output format was too easy to violate.",)
    assert score.proposed_fixes == ("Clarify required JSON output shape.",)
    assert score.confidence == 0.81
    assert score.token_usage.total_tokens == 3


def test_parse_step_score_rejects_invalid_json():
    result = parse_step_score("not-json", _episode(), evaluator_model="fake-score-model", token_usage=TokenUsage())

    assert not result.ok
    assert result.error.code == "LLM_PARSE_ERROR"


def test_llm_step_scorer_calls_provider_and_parses_score():
    async def scenario():
        provider = FakeScoreProvider(_score_json())
        scorer = LlmStepScorer(provider)

        result = await scorer.score(_episode())

        assert result.ok
        assert result.value.trace_id == "trace-1"
        assert provider.messages is not None

    asyncio.run(scenario())
```

- [ ] **Step 2: Run the scoring tests to verify they fail**

Run:

```bash
uv run pytest tests/evolution/test_evolution_scoring.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'loom.evolution.scoring'`.

- [ ] **Step 3: Implement `scoring.py`**

Create `src/loom/evolution/scoring.py`:

```python
"""LLM step scoring for trace-driven evolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from loom.core import Result, err, make_loom_error, ok
from loom.evolution.episodes import StepEpisode
from loom.llm import LlmMessage, TokenUsage

SCORE_DIMENSIONS = (
    "task_progress",
    "evidence_grounding",
    "tool_choice_quality",
    "tool_argument_quality",
    "context_relevance",
    "prompt_following",
    "cost_efficiency",
    "failure_recovery",
)


@dataclass(frozen=True, slots=True)
class StepScore:
    run_id: str
    trace_id: str
    step_number: int
    overall: float
    dimensions: dict[str, float]
    attribution: dict[str, tuple[str, ...]]
    proposed_fixes: tuple[str, ...]
    evidence_event_hashes: tuple[str, ...]
    confidence: float
    evaluator_model: str
    token_usage: TokenUsage = TokenUsage()


class LlmStepScorer:
    def __init__(self, provider: Any):
        self.provider = provider

    async def score(self, episode: StepEpisode) -> Result:
        messages = build_step_scoring_messages(episode)
        response = await self.provider.chat(messages, tools=None)
        if not response.ok:
            return response
        return parse_step_score(
            response.value.content or "",
            episode,
            evaluator_model=getattr(self.provider, "model", "unknown"),
            token_usage=response.value.usage,
        )


def build_step_scoring_messages(episode: StepEpisode) -> tuple[LlmMessage, ...]:
    system = "\n".join(
        [
            "You are a step evolution judge for Loom.",
            "Score one loop step using only the evidence provided.",
            "Return only valid JSON with keys: overall, dimensions, attribution, proposed_fixes, confidence.",
            "Use scores from 0.0 to 1.0.",
        ]
    )
    user_payload = {
        "run_id": episode.run_id,
        "trace_id": episode.trace_id,
        "loop_id": episode.loop_id,
        "step_number": episode.step_number,
        "complete": episode.complete,
        "llm_requests": episode.llm_requests,
        "llm_completions": episode.llm_completions,
        "tool_events": episode.tool_events,
        "action_events": episode.action_events,
        "observation_events": episode.observation_events,
        "completed_trace": episode.completed_trace,
        "completed_event": episode.completed_event,
        "event_hashes": episode.event_hashes,
        "dimensions": SCORE_DIMENSIONS,
        "attribution_categories": (
            "system_prompt",
            "user_prompt",
            "tool_schema",
            "tool_description",
            "tool_collection",
            "tool_selection",
            "tool_call_policy",
            "skill_context",
            "history_context",
            "knowledge_context",
            "runtime_policy",
        ),
    }
    return (LlmMessage("system", system), LlmMessage("user", json.dumps(user_payload, sort_keys=True, default=str)))


def parse_step_score(content: str, episode: StepEpisode, *, evaluator_model: str, token_usage: TokenUsage) -> Result:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        return err(make_loom_error("LLM_PARSE_ERROR", f"Failed to parse step score JSON: {exc}", retryable=False))
    if not isinstance(raw, dict):
        return err(make_loom_error("VALIDATION_FAILED", "Step score must be a JSON object", retryable=False))

    dimensions = _score_mapping(raw.get("dimensions") or {})
    attribution = _attribution_mapping(raw.get("attribution") or {})
    proposed_fixes = tuple(str(item) for item in raw.get("proposed_fixes") or ())

    return ok(
        StepScore(
            run_id=episode.run_id,
            trace_id=episode.trace_id,
            step_number=episode.step_number,
            overall=float(raw.get("overall", 0.0)),
            dimensions=dimensions,
            attribution=attribution,
            proposed_fixes=proposed_fixes,
            evidence_event_hashes=episode.event_hashes,
            confidence=float(raw.get("confidence", 0.0)),
            evaluator_model=evaluator_model,
            token_usage=token_usage,
        )
    )


def _score_mapping(value: dict[str, Any]) -> dict[str, float]:
    return {str(key): float(score) for key, score in value.items()}


def _attribution_mapping(value: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    return {str(key): tuple(str(item) for item in items or ()) for key, items in value.items()}
```

- [ ] **Step 4: Run the scoring tests to verify they pass**

Run:

```bash
uv run pytest tests/evolution/test_evolution_scoring.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/loom/evolution/scoring.py tests/evolution/test_evolution_scoring.py
git commit -m "feat: score evolution trace episodes"
```

---

### Task 3: Signals, Proposals, And Gates

**Files:**
- Create: `src/loom/evolution/proposals.py`
- Test: `tests/evolution/test_evolution_proposals.py`

- [ ] **Step 1: Write the failing proposal tests**

Create `tests/evolution/test_evolution_proposals.py`:

```python
from loom.evolution.proposals import ProposalGateConfig, aggregate_step_scores, gate_proposal, generate_evolution_proposals
from loom.evolution.scoring import StepScore
from loom.llm import TokenUsage


def _score(trace_id, surface="system_prompt", confidence=0.8, overall=0.4):
    return StepScore(
        run_id="run-1",
        trace_id=trace_id,
        step_number=0,
        overall=overall,
        dimensions={"prompt_following": 0.3},
        attribution={surface: ("Output contract is unclear.",)},
        proposed_fixes=("Clarify JSON output contract.",),
        evidence_event_hashes=(f"hash-{trace_id}",),
        confidence=confidence,
        evaluator_model="fake-score-model",
        token_usage=TokenUsage(),
    )


def test_aggregate_step_scores_requires_repeated_surface():
    signals = aggregate_step_scores((_score("trace-1"), _score("trace-2"), _score("trace-3", surface="tool_schema")), min_frequency=2)

    assert len(signals) == 1
    assert signals[0].surface == "system_prompt"
    assert signals[0].frequency == 2
    assert signals[0].trace_ids == ("trace-1", "trace-2")
    assert signals[0].confidence == 0.8


def test_generate_evolution_proposals_creates_bounded_prompt_proposal():
    signals = aggregate_step_scores((_score("trace-1"), _score("trace-2")), min_frequency=2)

    proposals = generate_evolution_proposals(signals, max_proposals=1)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.surface == "system_prompt"
    assert proposal.kind == "prompt_rule"
    assert proposal.created_from_trace_ids == ("trace-1", "trace-2")
    assert proposal.reversible is True
    assert proposal.patch["operation"] == "add_rule"


def test_gate_proposal_rejects_low_confidence_signal():
    signal = aggregate_step_scores((_score("trace-1", confidence=0.4), _score("trace-2", confidence=0.4)), min_frequency=2)[0]
    proposal = generate_evolution_proposals((signal,), max_proposals=1)[0]

    result = gate_proposal(proposal, ProposalGateConfig(min_confidence=0.7))

    assert not result.ok
    assert result.error.code == "MUTATION_REJECTED"
```

- [ ] **Step 2: Run the proposal tests to verify they fail**

Run:

```bash
uv run pytest tests/evolution/test_evolution_proposals.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'loom.evolution.proposals'`.

- [ ] **Step 3: Implement `proposals.py`**

Create `src/loom/evolution/proposals.py`:

```python
"""Signal aggregation and proposal generation for trace-driven evolution."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from loom.core import Result, err, make_loom_error, ok
from loom.evolution.scoring import StepScore


@dataclass(frozen=True, slots=True)
class EvolutionSignal:
    kind: str
    surface: str
    severity: float
    frequency: int
    trace_ids: tuple[str, ...]
    explanation: str
    confidence: float
    evidence_event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    id: str
    surface: str
    kind: str
    title: str
    rationale: str
    created_from_trace_ids: tuple[str, ...]
    expected_impact: dict[str, Any]
    risk: str
    reversible: bool
    ttl_runs: int | None
    patch: dict[str, Any]
    confidence: float
    evidence_event_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProposalGateConfig:
    min_confidence: float = 0.7
    max_risk: str = "medium"
    require_reversible: bool = True


def aggregate_step_scores(scores: tuple[StepScore, ...], *, min_frequency: int = 2) -> tuple[EvolutionSignal, ...]:
    grouped: dict[str, list[StepScore]] = defaultdict(list)
    for score in scores:
        for surface, explanations in score.attribution.items():
            if explanations:
                grouped[surface].append(score)

    signals: list[EvolutionSignal] = []
    for surface, surface_scores in grouped.items():
        if len(surface_scores) < min_frequency:
            continue
        confidence = sum(score.confidence for score in surface_scores) / len(surface_scores)
        severity = sum(1.0 - score.overall for score in surface_scores) / len(surface_scores)
        trace_ids = tuple(score.trace_id for score in surface_scores)
        evidence_hashes = tuple(hash_value for score in surface_scores for hash_value in score.evidence_event_hashes)
        explanation = surface_scores[0].attribution[surface][0]
        signals.append(
            EvolutionSignal(
                kind="repeated_attribution",
                surface=surface,
                severity=severity,
                frequency=len(surface_scores),
                trace_ids=trace_ids,
                explanation=explanation,
                confidence=confidence,
                evidence_event_hashes=evidence_hashes,
            )
        )
    return tuple(sorted(signals, key=lambda signal: (-signal.severity, -signal.frequency, signal.surface)))


def generate_evolution_proposals(signals: tuple[EvolutionSignal, ...], *, max_proposals: int = 3) -> tuple[EvolutionProposal, ...]:
    proposals = [_proposal_from_signal(signal) for signal in signals]
    return tuple(proposals[:max_proposals])


def gate_proposal(proposal: EvolutionProposal, config: ProposalGateConfig | None = None) -> Result:
    config = config or ProposalGateConfig()
    if proposal.confidence < config.min_confidence:
        return err(make_loom_error("MUTATION_REJECTED", "Proposal confidence is below threshold", retryable=False))
    if config.require_reversible and not proposal.reversible:
        return err(make_loom_error("MUTATION_REJECTED", "Proposal is not reversible", retryable=False))
    return ok(proposal)


def _proposal_from_signal(signal: EvolutionSignal) -> EvolutionProposal:
    kind, patch = _proposal_kind_and_patch(signal)
    return EvolutionProposal(
        id=f"proposal-{signal.surface}-{abs(hash(signal.trace_ids))}",
        surface=signal.surface,
        kind=kind,
        title=f"Improve {signal.surface}",
        rationale=signal.explanation,
        created_from_trace_ids=signal.trace_ids,
        expected_impact={"severity": signal.severity, "frequency": signal.frequency},
        risk="low",
        reversible=True,
        ttl_runs=10,
        patch=patch,
        confidence=signal.confidence,
        evidence_event_hashes=signal.evidence_event_hashes,
    )


def _proposal_kind_and_patch(signal: EvolutionSignal) -> tuple[str, dict[str, Any]]:
    if signal.surface in {"system_prompt", "user_prompt"}:
        return "prompt_rule", {"operation": "add_rule", "surface": signal.surface, "text": signal.explanation}
    if signal.surface in {"tool_schema", "tool_description"}:
        return "tool_schema_clarification", {"operation": "clarify_schema", "surface": signal.surface, "text": signal.explanation}
    if signal.surface == "tool_collection":
        return "tool_collection_policy", {"operation": "adjust_resolver_priority", "text": signal.explanation}
    if signal.surface == "skill_context":
        return "skill_proposal", {"operation": "propose_skill", "text": signal.explanation}
    return "context_policy", {"operation": "adjust_context_policy", "surface": signal.surface, "text": signal.explanation}
```

- [ ] **Step 4: Run the proposal tests to verify they pass**

Run:

```bash
uv run pytest tests/evolution/test_evolution_proposals.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/loom/evolution/proposals.py tests/evolution/test_evolution_proposals.py
git commit -m "feat: propose trace driven evolution changes"
```

---

### Task 4: Artifact Persistence And Report Rendering

**Files:**
- Create: `src/loom/evolution/artifacts.py`
- Test: `tests/evolution/test_evolution_artifacts.py`

- [ ] **Step 1: Write the failing artifact tests**

Create `tests/evolution/test_evolution_artifacts.py`:

```python
import json

from loom.evolution.artifacts import EvolutionArtifacts, render_evolution_report, write_evolution_artifacts
from loom.evolution.proposals import EvolutionProposal, EvolutionSignal
from loom.evolution.scoring import StepScore
from loom.llm import TokenUsage


def _score():
    return StepScore(
        run_id="run-1",
        trace_id="trace-1",
        step_number=0,
        overall=0.5,
        dimensions={"prompt_following": 0.4},
        attribution={"system_prompt": ("Output contract unclear.",)},
        proposed_fixes=("Clarify output contract.",),
        evidence_event_hashes=("hash-1",),
        confidence=0.8,
        evaluator_model="fake-score-model",
        token_usage=TokenUsage(1, 2, 3),
    )


def _signal():
    return EvolutionSignal("repeated_attribution", "system_prompt", 0.5, 2, ("trace-1", "trace-2"), "Output contract unclear.", 0.8, ("hash-1",))


def _proposal():
    return EvolutionProposal(
        id="proposal-1",
        surface="system_prompt",
        kind="prompt_rule",
        title="Improve system prompt",
        rationale="Output contract unclear.",
        created_from_trace_ids=("trace-1",),
        expected_impact={"severity": 0.5},
        risk="low",
        reversible=True,
        ttl_runs=10,
        patch={"operation": "add_rule", "text": "Return valid JSON."},
        confidence=0.8,
        evidence_event_hashes=("hash-1",),
    )


def test_write_evolution_artifacts_writes_jsonl_streams(tmp_path):
    artifacts = write_evolution_artifacts(tmp_path, scores=(_score(),), signals=(_signal(),), proposals=(_proposal(),))

    assert isinstance(artifacts, EvolutionArtifacts)
    assert artifacts.scores_path.exists()
    assert artifacts.signals_path.exists()
    assert artifacts.proposals_path.exists()
    score_record = json.loads(artifacts.scores_path.read_text(encoding="utf-8").splitlines()[0])
    assert score_record["trace_id"] == "trace-1"
    assert score_record["token_usage"]["total_tokens"] == 3


def test_render_evolution_report_summarizes_counts_and_proposals():
    report = render_evolution_report(scores=(_score(),), signals=(_signal(),), proposals=(_proposal(),))

    assert "Trace Driven Evolution Report" in report
    assert "scores: 1" in report
    assert "signals: 1" in report
    assert "proposals: 1" in report
    assert "Improve system prompt" in report
```

- [ ] **Step 2: Run the artifact tests to verify they fail**

Run:

```bash
uv run pytest tests/evolution/test_evolution_artifacts.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'loom.evolution.artifacts'`.

- [ ] **Step 3: Implement `artifacts.py`**

Create `src/loom/evolution/artifacts.py`:

```python
"""Artifact persistence for trace-driven evolution analysis."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from loom.llm import TokenUsage


@dataclass(frozen=True, slots=True)
class EvolutionArtifacts:
    out_dir: Path
    scores_path: Path
    signals_path: Path
    proposals_path: Path
    report_path: Path


def write_evolution_artifacts(out_dir: str | Path, *, scores, signals, proposals) -> EvolutionArtifacts:
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    scores_path = base / "step-scores.jsonl"
    signals_path = base / "signals.jsonl"
    proposals_path = base / "proposals.jsonl"
    report_path = base / "report.md"

    _write_jsonl(scores_path, scores)
    _write_jsonl(signals_path, signals)
    _write_jsonl(proposals_path, proposals)
    report_path.write_text(render_evolution_report(scores=scores, signals=signals, proposals=proposals), encoding="utf-8")

    return EvolutionArtifacts(base, scores_path, signals_path, proposals_path, report_path)


def render_evolution_report(*, scores, signals, proposals) -> str:
    lines = [
        "# Trace Driven Evolution Report",
        "",
        f"- scores: {len(scores)}",
        f"- signals: {len(signals)}",
        f"- proposals: {len(proposals)}",
        "",
        "## Proposals",
        "",
    ]
    if not proposals:
        lines.append("No proposals passed the configured gates.")
    for proposal in proposals:
        lines.extend(
            [
                f"### {proposal.title}",
                "",
                f"- id: `{proposal.id}`",
                f"- surface: `{proposal.surface}`",
                f"- kind: `{proposal.kind}`",
                f"- confidence: {proposal.confidence:.2f}",
                f"- risk: `{proposal.risk}`",
                f"- rationale: {proposal.rationale}",
                "",
            ]
        )
    return "\n".join(lines)


def _write_jsonl(path: Path, records) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(_to_plain(record), sort_keys=True) + "\n")


def _to_plain(value: Any) -> Any:
    if isinstance(value, TokenUsage):
        return {
            "prompt_tokens": value.prompt_tokens,
            "completion_tokens": value.completion_tokens,
            "total_tokens": value.total_tokens,
        }
    if is_dataclass(value):
        plain = {}
        for key, item in asdict(value).items():
            plain[_to_snake(key)] = _to_plain(item)
        return plain
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_to_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _to_snake(value: str) -> str:
    return value
```

- [ ] **Step 4: Run the artifact tests to verify they pass**

Run:

```bash
uv run pytest tests/evolution/test_evolution_artifacts.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/loom/evolution/artifacts.py tests/evolution/test_evolution_artifacts.py
git commit -m "feat: persist evolution analysis artifacts"
```

---

### Task 5: Analyzer Orchestrator And CLI

**Files:**
- Create: `src/loom/evolution/analyze.py`
- Modify: `src/loom/evolution/__init__.py`
- Test: `tests/evolution/test_evolution_analyze.py`

- [ ] **Step 1: Write the failing analyzer tests**

Create `tests/evolution/test_evolution_analyze.py`:

```python
import asyncio
import json

from loom.core import ok
from loom.evolution.analyze import AnalyzeConfig, analyze_trace, parse_args
from loom.llm import LlmResponse, TokenUsage


class FakeScoreProvider:
    model = "fake-score-model"

    async def chat(self, messages, tools=None, cancellation=None):
        return ok(
            LlmResponse(
                content=json.dumps(
                    {
                        "overall": 0.4,
                        "dimensions": {"prompt_following": 0.3},
                        "attribution": {"system_prompt": ["Output contract unclear."]},
                        "proposed_fixes": ["Clarify output contract."],
                        "confidence": 0.9,
                    }
                ),
                usage=TokenUsage(3, 4, 7),
            )
        )


def _write_trace(path):
    records = [
        {"type": "event", "eventType": "step.started", "traceId": "trace-1", "payload": {"type": "step.started", "run_id": "run-1", "loop_id": "loop-1", "trace_id": "trace-1", "step_number": 0}, "hash": "hash-start"},
        {"type": "event", "eventType": "llm.requested", "traceId": "trace-1", "payload": {"type": "llm.requested", "run_id": "run-1", "loop_id": "loop-1", "trace_id": "trace-1", "step_number": 0, "messages": []}, "hash": "hash-llm-request"},
        {"type": "event", "eventType": "llm.completed", "traceId": "trace-1", "payload": {"type": "llm.completed", "run_id": "run-1", "loop_id": "loop-1", "trace_id": "trace-1", "step_number": 0, "response": {"content": "{}"}}, "hash": "hash-llm-complete"},
        {"type": "event", "eventType": "step.completed", "traceId": "trace-1", "payload": {"type": "step.completed", "run_id": "run-1", "loop_id": "loop-1", "trace_id": "trace-1", "step_number": 0}, "hash": "hash-step-complete"},
        {"type": "trace", "id": "trace-1", "runId": "run-1", "payload": {"id": "trace-1", "run_id": "run-1", "loop_id": "loop-1", "loop_version": "v1", "step_number": 0, "root_trace_id": "trace-1", "started_at": "2026-06-28T00:00:00Z", "ended_at": "2026-06-28T00:00:01Z", "duration_ms": 1, "input_context_id": "ctx-in", "output_context_id": "ctx-out", "outcome": "pass"}, "hash": "hash-trace"},
    ]
    path.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")


def test_parse_args_accepts_trace_and_output_paths(tmp_path):
    options = parse_args(("--trace-path", str(tmp_path / "trace.jsonl"), "--out-dir", str(tmp_path / "evolution"), "--min-confidence", "0.8"))

    assert options.trace_path == tmp_path / "trace.jsonl"
    assert options.out_dir == tmp_path / "evolution"
    assert options.min_confidence == 0.8


def test_analyze_trace_scores_and_writes_artifacts(tmp_path):
    async def scenario():
        trace_path = tmp_path / "trace.jsonl"
        _write_trace(trace_path)

        result = await analyze_trace(
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "evolution", min_confidence=0.7, min_signal_frequency=1),
            provider=FakeScoreProvider(),
        )

        assert result.ok
        assert len(result.value.episodes) == 1
        assert len(result.value.scores) == 1
        assert len(result.value.signals) == 1
        assert len(result.value.proposals) == 1
        assert result.value.artifacts.report_path.exists()

    asyncio.run(scenario())
```

- [ ] **Step 2: Run the analyzer tests to verify they fail**

Run:

```bash
uv run pytest tests/evolution/test_evolution_analyze.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'loom.evolution.analyze'`.

- [ ] **Step 3: Implement `analyze.py`**

Create `src/loom/evolution/analyze.py`:

```python
"""Offline trace-driven evolution analyzer CLI."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, ok
from loom.evolution.artifacts import EvolutionArtifacts, render_evolution_report, write_evolution_artifacts
from loom.evolution.episodes import StepEpisode, build_step_episodes, load_trace_records
from loom.evolution.proposals import EvolutionProposal, EvolutionSignal, ProposalGateConfig, aggregate_step_scores, gate_proposal, generate_evolution_proposals
from loom.evolution.scoring import LlmStepScorer, StepScore
from loom.llm import create_env_openai_provider


@dataclass(frozen=True, slots=True)
class AnalyzeConfig:
    trace_path: Path
    out_dir: Path = Path(".loom/evolution")
    min_confidence: float = 0.7
    min_signal_frequency: int = 2
    max_proposals: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_path", Path(self.trace_path))
        object.__setattr__(self, "out_dir", Path(self.out_dir))


@dataclass(frozen=True, slots=True)
class AnalyzeResult:
    episodes: tuple[StepEpisode, ...]
    scores: tuple[StepScore, ...]
    signals: tuple[EvolutionSignal, ...]
    proposals: tuple[EvolutionProposal, ...]
    artifacts: EvolutionArtifacts
    report: str


async def analyze_trace(config: AnalyzeConfig, *, provider: Any | None = None) -> Result:
    if provider is None:
        provider_result = create_env_openai_provider()
        if not provider_result.ok:
            return provider_result
        provider = provider_result.value

    if not config.trace_path.exists():
        return err(make_loom_error("VALIDATION_FAILED", "Trace path does not exist", retryable=False, metadata={"trace_path": str(config.trace_path)}))

    records = load_trace_records(config.trace_path)
    episodes = build_step_episodes(records)
    scorer = LlmStepScorer(provider)
    scores = []
    for episode in episodes:
        scored = await scorer.score(episode)
        if scored.ok:
            scores.append(scored.value)

    signals = aggregate_step_scores(tuple(scores), min_frequency=config.min_signal_frequency)
    raw_proposals = generate_evolution_proposals(signals, max_proposals=config.max_proposals)
    gate_config = ProposalGateConfig(min_confidence=config.min_confidence)
    proposals = tuple(proposal for proposal in raw_proposals if gate_proposal(proposal, gate_config).ok)
    artifacts = write_evolution_artifacts(config.out_dir, scores=tuple(scores), signals=signals, proposals=proposals)
    report = render_evolution_report(scores=tuple(scores), signals=signals, proposals=proposals)
    return ok(AnalyzeResult(episodes, tuple(scores), signals, proposals, artifacts, report))


def parse_args(argv: tuple[str, ...] | list[str] | None = None) -> AnalyzeConfig:
    parser = argparse.ArgumentParser(description="Analyze persisted Loom traces and propose bounded evolution changes.")
    parser.add_argument("--trace-path", required=True, type=Path, help="Path to persisted trace JSONL")
    parser.add_argument("--out-dir", type=Path, default=Path(".loom/evolution"), help="Directory for evolution artifacts")
    parser.add_argument("--min-confidence", type=float, default=0.7, help="Minimum proposal confidence")
    parser.add_argument("--min-signal-frequency", type=int, default=2, help="Minimum repeated attribution count")
    parser.add_argument("--max-proposals", type=int, default=3, help="Maximum proposals to emit")
    args = parser.parse_args(None if argv is None else list(argv))
    return AnalyzeConfig(
        trace_path=args.trace_path,
        out_dir=args.out_dir,
        min_confidence=args.min_confidence,
        min_signal_frequency=args.min_signal_frequency,
        max_proposals=args.max_proposals,
    )


def main(argv: tuple[str, ...] | list[str] | None = None) -> None:
    config = parse_args(argv)
    result = asyncio.run(analyze_trace(config))
    if not result.ok:
        raise SystemExit(result.error.message if result.error else "Evolution analysis failed")
    print(result.value.report)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Export public contracts**

Modify `src/loom/evolution/__init__.py` by adding imports:

```python
from loom.evolution.analyze import AnalyzeConfig, AnalyzeResult, analyze_trace
from loom.evolution.episodes import StepEpisode, TraceRecord, build_step_episodes, load_trace_records
from loom.evolution.proposals import EvolutionProposal, EvolutionSignal, ProposalGateConfig
from loom.evolution.scoring import LlmStepScorer, StepScore
```

Add these names to `__all__`:

```python
"AnalyzeConfig",
"AnalyzeResult",
"EvolutionProposal",
"EvolutionSignal",
"LlmStepScorer",
"ProposalGateConfig",
"StepEpisode",
"StepScore",
"TraceRecord",
"analyze_trace",
"build_step_episodes",
"load_trace_records",
```

- [ ] **Step 5: Run the analyzer tests to verify they pass**

Run:

```bash
uv run pytest tests/evolution/test_evolution_analyze.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit Task 5**

```bash
git add src/loom/evolution/analyze.py src/loom/evolution/__init__.py tests/evolution/test_evolution_analyze.py
git commit -m "feat: add evolution trace analyzer cli"
```

---

### Task 6: Real Trace Integration

**Files:**
- Create: `tests/integration/test_trace_driven_evolution.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_trace_driven_evolution.py`:

```python
import asyncio
import json
import sys
from pathlib import Path

from loom.core import ok
from loom.evolution.analyze import AnalyzeConfig, analyze_trace
from loom.examples.real_project_smoke import RealProjectSmokeConfig, run_real_project_smoke
from loom.llm import LlmResponse, LlmToolCall, TokenUsage


class FakeSmokeProvider:
    model = "fake-smoke-model"

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None):
        self.calls += 1
        if self.calls == 1:
            return ok(
                LlmResponse(
                    content=None,
                    tool_calls=(
                        LlmToolCall("inspect-call", "inspect-project", "{}"),
                        LlmToolCall("smoke-call", "run-smoke-test", "{}"),
                    ),
                    finish_reason="tool_calls",
                )
            )
        return ok(
            LlmResponse(
                content=json.dumps(
                    {
                        "reasoning": "I used the tool evidence.",
                        "action": {
                            "kind": "custom",
                            "description": "Write report",
                            "input": {"report": "# Smoke Report\n\nThe smoke run passed."},
                        },
                        "alternatives": [],
                        "confidence": 0.8,
                    }
                ),
                usage=TokenUsage(10, 10, 20),
            )
        )


class FakeEvolutionScoreProvider:
    model = "fake-evolution-score-model"

    async def chat(self, messages, tools=None, cancellation=None):
        return ok(
            LlmResponse(
                content=json.dumps(
                    {
                        "overall": 0.45,
                        "dimensions": {
                            "task_progress": 0.7,
                            "evidence_grounding": 0.8,
                            "tool_choice_quality": 0.7,
                            "tool_argument_quality": 0.8,
                            "context_relevance": 0.5,
                            "prompt_following": 0.4,
                            "cost_efficiency": 0.6,
                            "failure_recovery": 0.3,
                        },
                        "attribution": {"system_prompt": ["The output contract should be more explicit."]},
                        "proposed_fixes": ["Clarify the JSON report contract."],
                        "confidence": 0.9,
                    }
                ),
                usage=TokenUsage(11, 12, 23),
            )
        )


def test_real_project_smoke_trace_can_be_analyzed_for_evolution(tmp_path: Path):
    async def scenario():
        project = tmp_path / "sample"
        project.mkdir()
        (project / "README.md").write_text("# Sample\n\nA tiny sample project.\n", encoding="utf-8")
        (project / "pyproject.toml").write_text('[project]\nname = "sample"\n', encoding="utf-8")
        trace_path = tmp_path / "traces" / "smoke.jsonl"

        smoke = await run_real_project_smoke(
            RealProjectSmokeConfig(
                target_path=project,
                smoke_command=(sys.executable, "-c", "print('smoke ok')"),
                cli_smoke_enabled=False,
                trace_path=trace_path,
            ),
            provider=FakeSmokeProvider(),
            llm=True,
        )

        assert smoke.ok

        analyzed = await analyze_trace(
            AnalyzeConfig(trace_path=trace_path, out_dir=tmp_path / "evolution", min_signal_frequency=1),
            provider=FakeEvolutionScoreProvider(),
        )

        assert analyzed.ok
        assert analyzed.value.episodes
        assert analyzed.value.scores
        assert analyzed.value.proposals
        assert analyzed.value.artifacts.scores_path.exists()
        assert analyzed.value.artifacts.proposals_path.exists()
        assert "Trace Driven Evolution Report" in analyzed.value.artifacts.report_path.read_text(encoding="utf-8")

    asyncio.run(scenario())
```

- [ ] **Step 2: Run the integration test to verify it fails or exposes missing exports**

Run:

```bash
uv run pytest tests/integration/test_trace_driven_evolution.py -q
```

Expected before Task 5 is complete: FAIL with missing analyzer imports. Expected after Task 5 is complete: PASS.

- [ ] **Step 3: If Task 5 is already complete, run the integration test to verify it passes**

Run:

```bash
uv run pytest tests/integration/test_trace_driven_evolution.py -q
```

Expected: `1 passed`.

- [ ] **Step 4: Commit Task 6**

```bash
git add tests/integration/test_trace_driven_evolution.py
git commit -m "test: analyze persisted real smoke traces"
```

---

### Task 7: Final Verification

**Files:**
- No new files unless verification reveals a concrete import or formatting issue.

- [ ] **Step 1: Run focused evolution tests**

Run:

```bash
uv run pytest tests/evolution tests/integration/test_trace_driven_evolution.py -q
```

Expected: all evolution tests pass.

- [ ] **Step 2: Run real project smoke tests**

Run:

```bash
uv run pytest tests/integration/test_real_project_smoke.py tests/integration/test_trace_driven_evolution.py -q
```

Expected: all selected integration tests pass.

- [ ] **Step 3: Run lint**

Run:

```bash
uv run ruff check src tests
```

Expected: `All checks passed!`

- [ ] **Step 4: Run format check**

Run:

```bash
uv run ruff format --check src tests
```

Expected: `61 files already formatted` or the updated formatted-file count.

- [ ] **Step 5: Run full test suite**

Run:

```bash
uv run pytest -q
```

Expected: full suite passes with the existing skipped-test count.

- [ ] **Step 6: Commit final verification fixes if any files changed**

If verification required an import or formatting fix:

```bash
git add src tests
git commit -m "fix: polish trace evolution analyzer"
```

If no files changed, do not create an empty commit.

---

## Self-Review

### Spec Coverage

- Offline JSONL trace input: Task 1 and Task 5.
- Step episode construction: Task 1.
- LLM step scoring: Task 2.
- Deterministic aggregation: Task 3.
- Proposal generation and gates: Task 3.
- Artifact persistence: Task 4.
- CLI shape: Task 5.
- Real trace flow from `real_project_smoke`: Task 6.
- No automatic mutation application: Task 3 only emits proposals; Task 5 writes artifacts and report.

### Deliberate Deferrals

- Shadow evaluation is not included in this first implementation plan.
- Controlled application and rollback commands are not included in this first implementation plan.
- Generated executable tools are not included.
- TUI integration for evolve events is not included.

These deferrals match the approved design's first implementation target.

### Verification Expectations

The final deliverable is complete when:

- `python -m loom.evolution.analyze --trace-path <path> --out-dir <dir>` exists.
- It writes `step-scores.jsonl`, `signals.jsonl`, `proposals.jsonl`, and `report.md`.
- It can analyze a persisted `real_project_smoke` trace using a fake scoring provider in tests.
- It does not apply any proposal automatically.
