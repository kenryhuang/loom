import asyncio
import json
from dataclasses import asdict

from loom.core import ok
from loom.llm import LlmResponse, TokenUsage


def setup_evidence(tmp_path):
    from loom.evaluation.diagnostics import FactAnalysis
    from loom.evaluation.evidence_store import EvidenceStore

    p = tmp_path / "trace.jsonl"
    p.write_text(json.dumps({"type": "llm.completed", "run_id": "r", "loop_id": "l", "trace_id": "t", "step_number": 0,
                             "llm_call_id": "c", "response": {"content": "x" * 8000 + "important tail"}}) + '\n')
    store = EvidenceStore.open(p)
    ref = asdict(store.ref(store.events[0], "response.content", start=8000))
    facts = FactAnalysis(task_contracts=({"id": "task:r", "run_id": "r", "objective": "inspect", "criteria": []},),
                         trajectory=({"id": "round:r:c", "run_id": "r", "loop_id": "l", "llm_call_id": "c", "response_ref": ref},))
    return store, facts, ref


def diagnosis(ref):
    return {"dimension": "context_effectiveness", "scope": "round:r:c", "observation": "The response has an important tail",
            "interpretation": "The tail must remain accessible", "epistemic_status": "observed", "supporting_refs": [ref]}


def final_payload(ref):
    from loom.evaluation.diagnostics import DIMENSIONS

    return {"diagnoses": [diagnosis(ref)], "verification": [], "preserved_behaviors": [], "verification_framework": [],
            "round_analyses": [{"round_id": "round:r:c", "pre_state": "Unknown initial state", "intent": "Inspect the response",
                                "action": "Produce response", "observed_change": "The response includes important tail",
                                "post_state": "Response recorded; external effect unknown", "progress_kind": "unknown",
                                "evidence_refs": [ref], "dimensions": {
                                    dimension: {"status": "unknown", "rationale": "Fixture lacks sufficient task evidence", "evidence_refs": [ref]}
                                    for dimension in DIMENSIONS}}]}


class ReadThenAnalyze:
    model = "fixture-judge"

    def __init__(self, ref):
        self.ref = ref

    async def chat(self, messages, tools=None, cancellation=None):
        expanded = any('"evidence":' in (m.content or "") and '"content": "important tail"' in (m.content or "") for m in messages)
        payload = final_payload(self.ref) if expanded else {"read_evidence": [self.ref]}
        return ok(LlmResponse(content=json.dumps(payload), usage=TokenUsage(10, 5, 15)))


def test_judge_expands_registered_evidence_and_records_separate_usage(tmp_path):
    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, ref = setup_evidence(tmp_path)
    result = asyncio.run(judge_effectiveness(store, facts, ReadThenAnalyze(ref))).unwrap()
    assert result.diagnoses[0].supporting_refs[0].start == 8000
    assert result.usage["total_tokens"] >= 30
    assert result.coverage["status"] == "complete"
    assert result.coverage["evidence_reads"] >= 1


def test_judge_read_budget_exhaustion_is_explicit_incomplete_analysis(tmp_path):
    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, ref = setup_evidence(tmp_path)
    result = asyncio.run(judge_effectiveness(store, facts, ReadThenAnalyze(ref), max_read_rounds=0)).unwrap()
    assert result.coverage["status"] == "incomplete"
    assert not result.diagnoses


def test_judge_rejects_fabricated_evidence(tmp_path):
    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, ref = setup_evidence(tmp_path)
    result = asyncio.run(judge_effectiveness(store, facts, ReadThenAnalyze({**ref, "source_sha256": "fake"})))
    assert not result.ok
    assert "source" in result.error.message.lower()


def test_judge_rejects_malformed_final_output(tmp_path):
    from loom.evaluation.effectiveness_judge import judge_effectiveness

    class Malformed:
        model = "fixture-judge"

        async def chat(self, messages, tools=None, cancellation=None):
            return ok(LlmResponse(content='{"diagnoses": "all good"}', usage=TokenUsage()))

    store, facts, _ = setup_evidence(tmp_path)
    result = asyncio.run(judge_effectiveness(store, facts, Malformed()))
    assert not result.ok


def test_failed_execution_cannot_become_supported_criterion(tmp_path):
    from loom.evaluation.diagnostics import FactAnalysis
    from loom.evaluation.effectiveness_judge import validate_verification

    store, _, ref = setup_evidence(tmp_path)
    facts = FactAnalysis(task_contracts=({"id": "task:r", "criteria": [{"id": "c1", "description": "test passes"}]},),
                         verification_evidence=({"execution_status": "failed", "evidence_refs": [ref]},))
    rows = validate_verification([{"criterion_id": "c1", "status": "supported", "method": "execution",
                                   "rationale": "tests pass", "evidence_refs": [ref]}], store, facts)
    assert rows[0]["status"] == "unverified"
    assert "execution" in rows[0]["limitation"]


def test_successful_exit_without_oracle_and_complete_requirement_coverage_is_unverified(tmp_path):
    from loom.evaluation.diagnostics import FactAnalysis
    from loom.evaluation.effectiveness_judge import validate_verification

    store, _, ref = setup_evidence(tmp_path)
    facts = FactAnalysis(task_contracts=({"id": "task:r", "run_id": "r", "criteria": [{"id": "c1", "description": "Construct and run test"}]},),
                         verification_evidence=({"execution_status": "passed", "run_id": "r", "evidence_refs": [ref]},))
    rows = validate_verification([{"criterion_id": "c1", "status": "supported", "method": "execution",
                                   "rationale": "Existing test exited zero", "evidence_refs": [ref],
                                   "requirement_coverage": "partial"}], store, facts)
    assert rows[0]["status"] == "unverified"


def test_model_cannot_confirm_freshness_when_recorded_check_predates_edits(tmp_path):
    from loom.evaluation.diagnostics import FactAnalysis
    from loom.evaluation.effectiveness_judge import validate_verification

    store, _, ref = setup_evidence(tmp_path)
    facts = FactAnalysis(task_contracts=({"id": "task:r", "run_id": "r", "criteria": [{"id": "c1"}]},),
                         verification_evidence=({"execution_status": "passed", "run_id": "r", "evidence_refs": [ref],
                                                 "freshness": {"artifact_binding": "unknown", "later_recorded_writes": [{"id": "edit"}]}},))
    rows = validate_verification([{"criterion_id": "c1", "status": "supported", "method": "execution", "rationale": "Passed",
                                   "evidence_refs": [ref], "oracle": "assert expected result", "checked_scope": "Final artifact",
                                   "requirement_coverage": "complete", "freshness": "confirmed"}], store, facts)
    assert rows[0]["status"] == "unverified"


def test_judge_cannot_attribute_evidence_to_an_invented_round(tmp_path):
    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, ref = setup_evidence(tmp_path)

    class WrongScope:
        model = "fixture-judge"

        async def chat(self, messages, tools=None, cancellation=None):
            payload = final_payload(ref)
            payload["diagnoses"][0]["scope"] = "round:another-source:invented"
            return ok(LlmResponse(content=json.dumps(payload), usage=TokenUsage()))

    result = asyncio.run(judge_effectiveness(store, facts, WrongScope()))
    assert not result.ok
    assert "scope" in result.error.message


class ScriptedJudge:
    model = "fixture-judge"

    def __init__(self, responses):
        self.responses = iter(responses)

    async def chat(self, messages, tools=None, cancellation=None):
        return ok(LlmResponse(content=json.dumps(next(self.responses)), usage=TokenUsage()))


def test_truncated_evidence_cannot_support_complete_absence_diagnosis(tmp_path):
    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, ref = setup_evidence(tmp_path)
    ref = {**ref, "start": None}
    value = final_payload(ref)
    value["diagnoses"][0].update(observation="The document contains no important tail", evidence_coverage="complete")
    result = asyncio.run(judge_effectiveness(store, facts, ScriptedJudge([{"read_evidence": [ref]}, value]), max_evidence_chars=100)).unwrap()
    assert result.coverage["status"] == "incomplete"
    assert result.diagnoses[0].epistemic_status == "unknown"
    assert result.diagnoses[0].evidence_coverage != "complete"


def test_initial_prompt_budget_includes_system_rubric(tmp_path):
    from loom.evaluation.effectiveness_judge import judge_effectiveness

    class NeverCalled:
        model = "fixture-judge"

        async def chat(self, messages, tools=None, cancellation=None):
            raise AssertionError("Over-budget prompt must not reach provider")

    store, facts, _ = setup_evidence(tmp_path)
    result = asyncio.run(judge_effectiveness(store, facts, NeverCalled(), max_prompt_chars=2000)).unwrap()
    assert result.coverage["status"] == "incomplete"
    assert result.coverage["evidence_chars"] == 0


def test_runtime_pass_is_not_source_review_verification(tmp_path):
    from loom.evaluation.diagnostics import FactAnalysis
    from loom.evaluation.effectiveness_judge import validate_verification
    from loom.evaluation.evidence_store import EvidenceStore

    p = tmp_path / "trace.jsonl"
    p.write_text('{"type":"run.completed","run_id":"r","outcome":"pass"}\n')
    store = EvidenceStore.open(p)
    ref = asdict(store.ref(store.events[0]))
    facts = FactAnalysis(task_contracts=({"id": "task:r", "run_id": "r", "criteria": [{"id": "c"}]},))
    rows = validate_verification([{"criterion_id": "c", "status": "supported", "method": "source_review",
                                   "rationale": "Run passed", "evidence_refs": [ref]}], store, facts)
    assert rows[0]["status"] == "unverified"


def test_run_without_rounds_is_included_in_semantic_coverage(tmp_path):
    from dataclasses import replace

    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, _ = setup_evidence(tmp_path)
    facts = replace(facts, task_contracts=(*facts.task_contracts, {"id": "task:empty", "run_id": "empty", "objective": "Review empty run", "criteria": []}))
    seen = set()

    class TrackTasks:
        model = "fixture-judge"

        async def chat(self, messages, tools=None, cancellation=None):
            payload = json.loads(messages[1].content)
            seen.update(t["id"] for t in payload["task_contracts"])
            return ok(LlmResponse(content=json.dumps({"diagnoses": [], "verification": [], "preserved_behaviors": [], "verification_framework": []}),
                                  usage=TokenUsage()))

    result = asyncio.run(judge_effectiveness(store, facts, TrackTasks())).unwrap()
    assert seen == {"task:r", "task:empty"}
    assert result.coverage["round_reviews"]["missing_round_ids"] == ["round:r:c"]
    assert result.coverage["status"] == "incomplete"


def test_verification_and_diagnosis_cannot_borrow_another_runs_evidence(tmp_path):
    from loom.evaluation.diagnostics import FactAnalysis
    from loom.evaluation.effectiveness_judge import judge_effectiveness, validate_verification
    from loom.evaluation.evidence_store import EvidenceStore

    p = tmp_path / "trace.jsonl"
    p.write_text('{"type":"run.started","run_id":"a"}\n'
                 '{"type":"tool.completed","run_id":"b","output":{"exit_code":0,"stdout":"passed"}}\n')
    store = EvidenceStore.open(p)
    ref = asdict(store.ref(store.events[1], "output"))
    facts = FactAnalysis(task_contracts=({"id": "task:a", "run_id": "a", "criteria": [{"id": "c"}]},),
                         verification_evidence=({"run_id": "b", "execution_status": "passed", "evidence_refs": [ref]},))
    rows = validate_verification([{"criterion_id": "c", "status": "supported", "method": "execution", "rationale": "Passed",
                                   "evidence_refs": [ref]}], store, facts)
    assert rows[0]["status"] == "unverified"
    value = final_payload(ref)
    value["diagnoses"][0]["scope"] = "task:a"
    result = asyncio.run(judge_effectiveness(store, facts, ScriptedJudge([value])))
    assert not result.ok
    assert "scope" in result.error.message


def test_valid_but_unread_evidence_is_not_an_observed_fact(tmp_path):
    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, ref = setup_evidence(tmp_path)
    # The initial facts expose only the tail. A valid pointer to the whole response is not evidence of reading it.
    value = final_payload({**ref, "start": None})
    result = asyncio.run(judge_effectiveness(store, facts, ScriptedJudge([value]))).unwrap()
    assert result.diagnoses[0].epistemic_status == "unknown"
    assert result.coverage["status"] == "incomplete"


def test_initial_previews_share_total_evidence_budget(tmp_path):
    from dataclasses import replace

    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, ref = setup_evidence(tmp_path)
    ref = {**ref, "start": 0, "end": 500}
    facts = replace(facts, trajectory=({**facts.trajectory[0], "response_ref": ref},))

    class CheckPreview(ScriptedJudge):
        async def chat(self, messages, tools=None, cancellation=None):
            previews = json.loads(messages[1].content)["initial_evidence"]
            assert sum(p["returned_chars"] for p in previews) <= 100
            return await super().chat(messages, tools, cancellation)

    result = asyncio.run(judge_effectiveness(store, facts, CheckPreview([final_payload(ref)]), max_evidence_chars=100)).unwrap()
    assert result.coverage["evidence_chars"] == 100
    assert result.coverage["status"] == "incomplete"
    assert result.diagnoses[0].epistemic_status == "unknown"


def test_round_can_cite_its_run_level_task_contract(tmp_path):
    from loom.evaluation.diagnostics import FactAnalysis
    from loom.evaluation.effectiveness_judge import judge_effectiveness
    from loom.evaluation.evidence_store import EvidenceStore

    path = tmp_path / "trace.jsonl"
    path.write_text('{"type":"run.started","run_id":"r","goal":{"objective":"Never modify production"}}\n'
                    '{"type":"llm.requested","run_id":"r","loop_id":"l","messages":[]}\n')
    store = EvidenceStore.open(path)
    ref = asdict(store.ref(store.events[0], "goal.objective"))
    facts = FactAnalysis(task_contracts=({"id": "task:r", "run_id": "r", "source_ref": ref},),
                         trajectory=({"id": "round:r:c", "run_id": "r", "loop_id": "l"},))
    result = asyncio.run(judge_effectiveness(store, facts, ScriptedJudge([final_payload(ref)]))).unwrap()
    assert result.diagnoses[0].epistemic_status == "observed"


def test_oversized_batches_split_and_reach_provider_within_budget(tmp_path):
    from dataclasses import replace

    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, _ = setup_evidence(tmp_path)
    facts = replace(facts, trajectory=tuple({**facts.trajectory[0], "id": f"round:{i}", "detail": ["detail " * 60] * 30} for i in range(4)))
    seen = []

    class Probe:
        model = "fixture-judge"

        async def chat(self, messages, tools=None, cancellation=None):
            assert sum(len(m.content or "") for m in messages) <= 40000
            payload = json.loads(messages[1].content)
            seen.extend(row["id"] for row in payload.get("trajectory", []))
            return ok(LlmResponse(content=json.dumps({"diagnoses": [], "verification": [], "preserved_behaviors": [], "verification_framework": []}),
                                  usage=TokenUsage()))

    result = asyncio.run(judge_effectiveness(store, facts, Probe(), max_prompt_chars=40000)).unwrap()
    assert seen == ["round:0", "round:1", "round:2", "round:3"]
    assert result.coverage["batches"] > 1
    assert result.usage["calls"] > 1
    assert not any("prompt budget" in item for item in result.coverage["limitations"])


def test_task_synthesis_cannot_rewrite_validated_batch_round_states(tmp_path):
    from dataclasses import replace

    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, ref = setup_evidence(tmp_path)
    facts = replace(facts, trajectory=tuple({**facts.trajectory[0], "id": f"round:{i}"} for i in range(2)))

    class SynthesisRewrite:
        model = "fixture-judge"

        async def chat(self, messages, tools=None, cancellation=None):
            payload = json.loads(messages[1].content)
            value = final_payload(ref)
            value["diagnoses"] = []
            ids = [r["id"] for r in payload.get("trajectory", [])]
            label = "original batch state"
            if payload["stage"] == "task_synthesis":
                ids, label = ["round:0", "round:1"], "unjustified synthesis rewrite"
            value["round_analyses"] = [{**value["round_analyses"][0], "round_id": key, "observed_change": label} for key in ids]
            return ok(LlmResponse(content=json.dumps(value), usage=TokenUsage()))

    result = asyncio.run(judge_effectiveness(store, facts, SynthesisRewrite(), batch_rounds=1)).unwrap()
    assert len(result.round_analyses) == 2
    assert all(row["observed_change"] == "original batch state" for row in result.round_analyses)


def test_opt_in_bounded_correction_never_accepts_invalid_evidence(tmp_path):
    from loom.evaluation.effectiveness_judge import judge_effectiveness

    store, facts, ref = setup_evidence(tmp_path)
    invalid = {"read_evidence": [{**ref, "field_path": "invented.field"}]}
    result = asyncio.run(judge_effectiveness(store, facts, ScriptedJudge([invalid, final_payload(ref)]),
                        repair_invalid=True, max_read_rounds=1)).unwrap()
    assert result.coverage["corrected_responses"] == 1
    assert result.diagnoses[0].supporting_refs[0].field_path == "response.content"
    exhausted = asyncio.run(judge_effectiveness(store, facts, ScriptedJudge([invalid, invalid]),
                           repair_invalid=True, max_read_rounds=1)).unwrap()
    assert exhausted.coverage["status"] == "incomplete"
    assert not exhausted.diagnoses
    assert not exhausted.round_analyses
