import asyncio
import json

import pytest

from loom.core import ok
from loom.evaluation.analyze import EvaluationConfig, analyze_trace, parse_args
from loom.evaluation.behavior_contracts import DIMENSIONS, outcome, validate_bundle
from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.behavior_judge import judge_behavior, partial_semantic
from loom.evaluation.evidence_store import EvidenceStore
from loom.evaluation.verification_receipts import artifact_boundary, capture_manifest, verification_receipt
from loom.llm import LlmResponse, TokenUsage


def source(tmp_path, events):
    path = tmp_path / "trace.jsonl"
    path.write_text("".join(json.dumps({"run_id": "r", **e}) + "\n" for e in events))
    return EvidenceStore.open(path)


def calls(count=2):
    events = []
    for i in range(count):
        scope = {"loop_id": "l", "trace_id": "t", "step_number": 0, "llm_call_id": str(i)}
        events += [
            {"type": "llm.requested", **scope, "messages": [{"role": "user", "content": "Find and fix the root cause"}]},
            {
                "type": "llm.completed",
                **scope,
                "response": {"content": "Inspect the failure", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            },
        ]
    return events


class BehaviorJudge:
    model = "fixture-judge"

    def __init__(self, *, fail_synthesis=False, unknown=False, invalid_ref=False):
        self.calls = 0
        self.fail_synthesis, self.unknown, self.invalid_ref = fail_synthesis, unknown, invalid_ref
        self.synthesis_packets = []

    async def chat(self, messages, tools=None, cancellation=None):
        self.calls += 1
        payload = json.loads(messages[1].content)
        stage = payload["stage"]
        if stage.startswith("scan:"):
            result = {"candidate_segment_ids": [s["id"] for s in payload["segment_index"]], "control_segment_ids": []}
        elif stage.startswith("focus:"):
            sid = payload["segments"][0]["id"]
            ref = payload["evidence_packets"][0]["returned_ref"]
            if self.invalid_ref:
                ref = {**ref, "source_sha256": "fabricated"}
            result = {
                "assessments": [
                    {
                        "segment_id": sid,
                        "dimension": d,
                        "status": "unknown" if self.unknown else "effective",
                        "rationale": "A discriminative check",
                        "evidence_refs": [ref],
                    }
                    for d in DIMENSIONS
                ],
                "diagnoses": [
                    {
                        "segment_id": sid,
                        "dimension": "investigation_efficiency",
                        "pattern_type": "discriminative_investigation",
                        "mechanism_id": "hypothesis_test",
                        "observation": "Inspect the failure",
                        "mechanism": "Test hypotheses",
                        "epistemic_status": "inferred",
                        "supporting_refs": [ref],
                        "counterevidence_refs": [],
                        "improvement_hypothesis": "Prioritize discriminative checks",
                        "intervention_surface": "planner",
                        "predicted_endpoint": "cost_to_supported_cause",
                        "validation": "Matched fault fixture; same oracle and environment",
                        "preserve": "Final regression test",
                        "alternatives": ["Missing context"],
                    }
                ],
                "preserved_behaviors": [],
            }
        else:
            if self.fail_synthesis:
                raise ValueError("Fixture synthesis unavailable")
            self.synthesis_packets = payload["evidence_packets"]
            result = {"summary": "Validated local findings retained", "finding_ids": [d["id"] for d in payload["diagnoses"]], "limitations": []}
        return ok(LlmResponse(content=json.dumps(result), usage=TokenUsage(10, 5, 15)))


def test_v3_base_metrics_are_separate_from_completion_claims(tmp_path):
    s = source(tmp_path, calls() + [{"type": "run.completed", "outcome": "pass"}])
    facts = build_behavior_facts(s)
    assert facts["base"]["metrics"]["model_calls"] == 2
    assert facts["base"]["metrics"]["tokens"]["total_tokens"] == 30
    assert facts["base"]["task_completion"] == "unverified"
    assert not facts["plan_revisions"]  # A short task needs no formal plan.


def test_followup_same_run_is_a_new_episode_but_resume_is_not(tmp_path):
    events = [
        {"type": "run.started"},
        *calls(1),
        {"type": "run.stopped"},
        {"type": "run.started"},
        {"type": "run.completed"},
        {"type": "run.started"},
        *calls(1),
    ]
    facts = build_behavior_facts(source(tmp_path, events))
    assert len(facts["episodes"]) == 2
    assert len(facts["episodes"][0]["attempts"]) == 2


def test_user_correction_accepted_applied_and_visible_are_distinct(tmp_path):
    events = [
        *calls(1),
        {"type": "message.created", "role": "user", "content": "只诊断，先不要修改", "command_id": "m2"},
        {"type": "command.applied", "command_id": "m2"},
        {
            "type": "llm.requested",
            "loop_id": "l",
            "trace_id": "t",
            "step_number": 1,
            "llm_call_id": "later",
            "messages": [{"role": "user", "content": "只诊断，先不要修改"}],
        },
    ]
    facts = build_behavior_facts(source(tmp_path, events))
    revision = next(g for g in facts["goal_revisions"] if g["origin"] == "user_message")
    assert revision["accepted_line"] < revision["applied_line"] < revision["visible_line"]
    assert len(revision["criteria"]) == 2
    assert revision["goal_coverage"] == "unknown"
    assert facts["base"]["task_completion"] == "unverified"


@pytest.mark.parametrize(
    ("criteria", "coverage", "expected"),
    [
        ([], "complete", "unverified"),
        ([{"status": "supported"}], "unknown", "partially_verified"),
        ([{"status": "supported"}], "complete", "achieved"),
        ([{"status": "supported"}, {"status": "unverified"}], "complete", "partially_verified"),
        ([{"status": "contradicted"}, {"status": "supported"}], "complete", "not_achieved"),
        ([{"status": "not_applicable"}], "complete", "unverified"),
    ],
)
def test_outcome_rules(criteria, coverage, expected):
    assert outcome(criteria, goal_coverage=coverage) == expected


@pytest.mark.parametrize("mutation,exclusive,expected", [(False, True, "achieved"), (True, True, "unverified"), (False, False, "unverified")])
def test_final_version_scope_binding(tmp_path, mutation, exclusive, expected):
    file = tmp_path / "report.md"
    file.write_text("analysis")
    before = capture_manifest(tmp_path, ["report.md"])
    receipt = verification_receipt(
        criterion_id="objective",
        goal_revision=2,
        scope=["report.md"],
        before=before,
        after=before,
        oracle="Exact content equals the approved analysis",
        environment="frozen-fixture",
        passed=True,
        exclusive=exclusive,
        coverage="complete",
    )
    if mutation:
        file.write_text("later change")
    final = artifact_boundary(scope=["report.md"], manifest=capture_manifest(tmp_path, ["report.md"]), environment="frozen-fixture", exclusive=exclusive)
    events = [{"type": "task.goal.revised", "objective": "Save the approved analysis", "goal_revision": 2, "input_cursor": 1}, receipt, final]
    facts = build_behavior_facts(source(tmp_path, events))
    assert facts["base"]["task_completion"] == expected
    # Offline evaluation uses only the snapshot even if the current workspace changes again.
    file.unlink()
    assert build_behavior_facts(source(tmp_path, events))["base"]["task_completion"] == expected


def test_deep_pipeline_passes_exact_quotes_to_synthesis_and_preserves_unknown_rates(tmp_path):
    s = source(tmp_path, calls(5))
    facts = build_behavior_facts(s)
    judge = BehaviorJudge(unknown=True)
    result = asyncio.run(judge_behavior(s, facts, judge)).unwrap()
    assert judge.calls == 4
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["supported_segments"] == 0
    assert result["coverage"]["dimensions"]["intent_alignment"]["unknown"] == 2
    assert len(result["diagnoses"]) == 2
    assert judge.synthesis_packets
    for p in judge.synthesis_packets:
        assert s.resolve(p["returned_ref"]) == p["content"]


def test_failed_synthesis_retains_local_findings_and_resume_counts_all_calls(tmp_path):
    s = source(tmp_path, calls(5))
    facts, cp = build_behavior_facts(s), {}
    with pytest.raises(ValueError, match="synthesis unavailable"):
        asyncio.run(judge_behavior(s, facts, BehaviorJudge(fail_synthesis=True), checkpoint=cp))
    partial = partial_semantic(facts, cp)
    assert len(partial["diagnoses"]) == 2
    assert not partial["coverage"]["synthesis_complete"]
    assert cp["usage"]["calls"] == 4
    judge = BehaviorJudge()
    result = asyncio.run(judge_behavior(s, facts, judge, checkpoint=cp)).unwrap()
    assert judge.calls == 1
    assert result["usage"]["calls"] == 5
    assert result["usage"]["unreported_calls"] == 1
    with pytest.raises(ValueError, match="Checkpoint"):
        asyncio.run(judge_behavior(s, facts, judge, checkpoint=cp, max_segments=1))


def test_fabricated_refs_are_rejected_and_never_become_findings(tmp_path):
    s = source(tmp_path, calls(5))
    facts, cp = build_behavior_facts(s), {}
    with pytest.raises(ValueError, match="source digest"):
        asyncio.run(judge_behavior(s, facts, BehaviorJudge(invalid_ref=True), checkpoint=cp))
    assert not partial_semantic(facts, cp)["diagnoses"]


def test_offline_cli_v3_bundle_and_legacy_reader_separation(tmp_path):
    s = source(tmp_path, calls())
    cfg = parse_args(["--analysis-version", "v3", "--trace-path", str(s.path), "--out-dir", str(tmp_path / "eval")])
    result = asyncio.run(analyze_trace(cfg)).unwrap()
    bundle = validate_bundle(json.loads(result.artifacts.evaluation_bundle_path.read_text()))
    assert bundle["semantic"]["coverage"]["status"] == "not_evaluated"
    assert bundle["base"]["task_completion"] == "unverified"
    with pytest.raises(ValueError, match="legacy"):
        validate_bundle({"schema_version": "loom.evaluation.bundle.v1"})


def test_partial_deep_coverage_does_not_change_base_outcome(tmp_path):
    s = source(tmp_path, calls())
    result = asyncio.run(
        analyze_trace(EvaluationConfig(s.path, out_dir=tmp_path / "out", analysis_version="v3", judge=True, judge_max_calls=1), judge_provider=BehaviorJudge())
    ).unwrap()
    assert result.semantic["coverage"]["status"] == "incomplete"
    assert result.facts["base"]["task_completion"] == "unverified"
    assert result.semantic["coverage"]["limitations"]


def test_accepted_plan_is_not_evidence_of_deliverable_completion(tmp_path):
    s = source(
        tmp_path,
        [
            {"type": "run.started", "metadata": {"objective": "Analyze and save a document"}},
            {"type": "plan.submitted", "revision": 1, "plan": {"items": [{"id": "read", "step": "Read sources", "status": "completed"}]}},
        ],
    )
    facts = build_behavior_facts(s)
    assert facts["plan_revisions"][0]["state"] == "accepted"
    assert facts["plan_revisions"][0]["nodes"][0]["evidenced_done"] == "unknown"
    assert facts["base"]["task_completion"] == "unverified"


def test_repeated_negative_search_is_only_a_candidate_and_changed_inputs_are_distinct(tmp_path):
    events = calls(2)
    for i in range(2):
        scope = {"loop_id": "l", "trace_id": "t", "step_number": 0, "llm_call_id": str(i), "tool_call_id": f"t{i}", "tool_id": "search"}
        events.extend(
            [
                {"type": "tool.started", **scope, "input": {"query": "hypothesis"}},
                {"type": "tool.completed", **scope, "output": {"status": "no_match", "content": "No evidence"}},
            ]
        )
    facts = build_behavior_facts(source(tmp_path, events))
    assert facts["candidate_windows"][0]["status"] == "candidate"
    assert facts["candidate_windows"][0]["pattern_type"] == "repeated_unchanged_result"
    assert all(s["state_change"] == "unknown" for s in facts["segments"])
    events[-2]["input"] = {"query": "new discriminative hypothesis"}
    assert not build_behavior_facts(source(tmp_path, events))["candidate_windows"]


def test_receipt_for_bounded_scope_survives_unrelated_file_change(tmp_path):
    (tmp_path / "answer").write_text("approved")
    manifest = capture_manifest(tmp_path, ["answer"])
    (tmp_path / "unrelated").write_text("different")
    events = [
        {"type": "task.goal.revised", "objective": "Save approved answer", "goal_revision": 4, "input_cursor": 1},
        verification_receipt(
            criterion_id="objective",
            goal_revision=4,
            scope=["answer"],
            before=manifest,
            after=manifest,
            oracle="Exact bytes are the approved answer",
            environment="env",
            passed=True,
            exclusive=True,
            coverage="complete",
        ),
        artifact_boundary(scope=["answer"], manifest=capture_manifest(tmp_path, ["answer"]), environment="env", exclusive=True),
    ]
    assert build_behavior_facts(source(tmp_path, events))["base"]["task_completion"] == "achieved"


def test_usage_duplicates_never_fabricate_complete_accounting(tmp_path):
    events = calls(1)
    events.append(dict(events[-1]))
    facts = build_behavior_facts(source(tmp_path, events))
    assert facts["base"]["metrics"]["tokens"]["known_total_tokens"] == 15
    assert facts["base"]["metrics"]["tokens"]["status"] == "incomplete"


def test_session_transport_messages_are_not_goals_and_bookkeeping_is_not_a_task(tmp_path):
    def request(call, objective, background=()):
        return {
            "type": "llm.requested",
            "run_id": call,
            "loop_id": "loop",
            "trace_id": call,
            "llm_call_id": call,
            "step_number": 0,
            "messages": [
                {"role": "user", "content": f"Current loop state:\nStep {call}"},
                {"role": "user", "name": "context_evidence", "content": "Current loop state:\nObservations"},
                {"role": "user", "name": "shell_execute", "content": '{"exit_code": 0}'},
                {"role": "user", "name": "workflow_tool_required", "content": "Call exactly one of enter_plan or continue_react"},
                *background,
                {"role": "user", "name": "current_request", "content": f"Current request for this round:\n{objective}"},
            ],
        }

    events = [
        {"type": "command.accepted", "run_id": None, "command_id": "input1"},
        {"type": "message.created", "run_id": None, "role": "user", "content": "Diagnose startup", "command_id": "input1"},
        {"type": "run.started", "run_id": "first"},
        {"type": "task.goal.revised", "run_id": "first", "objective": "Diagnose startup", "goal_revision": 2},
        {"type": "command.applied", "run_id": "first", "command_id": "input1"},
        request("first", "Diagnose startup"),
        {"type": "run.state.changed", "run_id": "first", "state": "completed"},
        {"type": "message.created", "run_id": "first", "role": "user", "content": "Investigate model speed", "command_id": "input2"},
        {"type": "run.started", "run_id": "second"},
        {"type": "task.goal.revised", "run_id": "second", "objective": "Investigate model speed", "goal_revision": 3},
        {"type": "command.applied", "run_id": "second", "command_id": "input2"},
        request(
            "second",
            "Investigate model speed",
            [{"role": "user", "name": "session_background", "content": "Earlier conversation"}, {"role": "user", "content": "Diagnose startup"}],
        ),
        {"type": "run.state.changed", "run_id": "second", "state": "suspended"},
    ]
    facts = build_behavior_facts(source(tmp_path, events))
    assert len(facts["episodes"]) == 2
    assert facts["episodes"][0]["start_line"] == 3
    outcomes = facts["base"]["outcomes"]
    assert [o["objective"] for o in outcomes] == ["Diagnose startup", "Investigate model speed"]
    assert [o["execution_status"] for o in outcomes] == ["completed", "suspended"]
    assert all(o["goal_coverage"] == "complete" and len(o["criteria"]) == 1 for o in outcomes)
    assert all(o["status"] == "unverified" and "No goal acceptance checks" in o["explanation"] for o in outcomes)
    assert len(facts["goal_revisions"]) == 4
    first_goals = [g for g in facts["goal_revisions"] if g["objective"] == "Diagnose startup"]
    assert {g["episode_id"] for g in first_goals} == {facts["episodes"][0]["id"]}
    assert {g["visible_line"] for g in first_goals} == {6}
    assert facts["base"]["execution_status"] == "suspended"


def test_waiting_session_has_no_execution_outcome(tmp_path):
    facts = build_behavior_facts(
        source(
            tmp_path,
            [
                {"type": "command.accepted", "run_id": None},
                {"type": "message.created", "run_id": None, "role": "user", "content": "Inspect", "command_id": "m"},
                {"type": "task.goal.revised", "run_id": None, "objective": "Inspect", "goal_revision": 1},
            ],
        )
    )
    assert facts["episodes"] == []
    assert facts["base"]["outcomes"] == []
    assert facts["base"]["execution_status"] == "not_started"


def test_loom_system_goal_handles_multiline_objective_without_context_goals(tmp_path):
    from loom.evaluation.behavior_goals import request_goals

    objective = "Analyze the cause\nWrite a report"
    messages = [
        {
            "role": "system",
            "content": (
                "You are the loop brain for this Loom context. Your role is: researcher.\n"
                f"Goal:\n- Objective: {objective}\nSuccess criteria:\n- report (required): Save the report"
            ),
        },
        {"role": "user", "content": "Current loop state:\nRepeated evolving state"},
        {"role": "user", "name": "context_evidence", "content": "Untrusted observations"},
    ]
    goals = request_goals(messages, "messages", allow_plain=True)
    assert len(goals) == 1 and goals[0][0] == objective
    assert messages[0]["content"][goals[0][2] : goals[0][3]] == objective


def test_same_objective_in_different_completed_runs_does_not_reassign_old_goals(tmp_path):
    events = []
    for run in ("one", "two"):
        events += [
            {"type": "run.started", "run_id": run},
            {"type": "task.goal.revised", "run_id": run, "objective": "Inspect", "input_cursor": 1},
            {
                "type": "llm.requested",
                "run_id": run,
                "loop_id": "l",
                "trace_id": run,
                "llm_call_id": run,
                "step_number": 0,
                "messages": [{"role": "user", "name": "current_request", "content": "Current request for this round:\nInspect"}],
            },
            {"type": "run.state.changed", "run_id": run, "state": "completed"},
        ]
    facts = build_behavior_facts(source(tmp_path, events))
    assert len(facts["goal_revisions"]) == 2
    assert len({g["episode_id"] for g in facts["goal_revisions"]}) == 2
    assert [g["visible_line"] for g in facts["goal_revisions"]] == [3, 7]


def test_pause_summary_does_not_split_a_resumed_execution(tmp_path):
    facts = build_behavior_facts(
        source(
            tmp_path,
            [
                {"type": "run.started"},
                *calls(1),
                {"type": "message.created", "role": "assistant", "content": "Paused for time budget"},
                {"type": "run.state.changed", "state": "suspended"},
                {"type": "run.started"},
                {"type": "run.state.changed", "state": "stopped"},
            ],
        )
    )
    assert len(facts["episodes"]) == 1
    assert len(facts["episodes"][0]["attempts"]) == 2
    assert facts["base"]["outcomes"][0]["execution_status"] == "stopped"


def test_time_reserve_synthesizes_saved_findings_and_resume_reuses_them(tmp_path):
    s = source(tmp_path, calls(5))
    facts, cp = build_behavior_facts(s), {}
    judge = BehaviorJudge()
    # Scan and first focus finish; the next focus would consume synthesis time.
    remaining = lambda: 90 if judge.calls < 2 else 15
    partial = asyncio.run(judge_behavior(s, facts, judge, checkpoint=cp,
        time_budget_seconds=100, remaining_seconds=remaining)).unwrap()
    assert partial['coverage']['status'] == 'incomplete'
    assert partial['coverage']['synthesis_complete']
    assert len(partial['diagnoses']) == len(partial['coverage']['pending_segments']) == 1
    assert len(partial['coverage']['unselected_segments']) == 0
    assert partial['coverage']['dimensions']['intent_alignment']['pending'] == 1
    assert partial['coverage']['dimensions']['intent_alignment']['unselected'] == 0
    resumed = BehaviorJudge()
    final = asyncio.run(judge_behavior(s, facts, resumed, checkpoint=cp)).unwrap()
    assert final['coverage']['status'] == 'complete'
    assert resumed.calls == 2  # Remaining focus and refreshed synthesis only.


def test_call_timeout_retains_unknown_usage_and_allows_synthesis(tmp_path):
    class SlowFocus(BehaviorJudge):
        async def chat(self, messages, tools=None, cancellation=None):
            if json.loads(messages[1].content)['stage'].startswith('focus:'):
                await asyncio.sleep(10)
            return await super().chat(messages, tools, cancellation)

    s = source(tmp_path, calls())
    cp = {}
    partial = asyncio.run(judge_behavior(s, build_behavior_facts(s), SlowFocus(), checkpoint=cp, max_call_seconds=.02)).unwrap()
    assert partial['coverage']['status'] == 'incomplete'
    assert partial['coverage']['synthesis_complete']
    assert partial['usage']['unreported_calls'] == 1
    assert partial['usage']['calls'] == 3
    assert 'call time budget reached' in partial['coverage']['limitations'][0]


def test_evidence_reads_and_output_repairs_have_distinct_progress(tmp_path):
    events = []

    class Sink:
        def emit(self, event):
            events.append(event)
            return ok(None)

    class ReadsAndRepairs(BehaviorJudge):
        turn = 0

        async def chat(self, messages, tools=None, cancellation=None):
            payload = json.loads(messages[1].content)
            if payload['stage'].startswith('focus:'):
                self.turn += 1
                if self.turn == 1:
                    return ok(LlmResponse(content=json.dumps({'read_evidence': [payload['evidence_packets'][0]['returned_ref']]})))
                if self.turn == 2:
                    return ok(LlmResponse(content='invalid json'))
            return await super().chat(messages, tools, cancellation)

    s = source(tmp_path, calls())
    result = asyncio.run(judge_behavior(s, build_behavior_facts(s), ReadsAndRepairs(), event_sink=Sink())).unwrap()
    assert result['coverage']['status'] == 'complete'
    activities = {e['evaluation_activity'] for e in events if e['type'] == 'llm.requested'}
    assert activities == {'review', 'read_evidence', 'repair'}
    assert {e['activity'] for e in events if e['type'] == 'evaluation.activity'} >= {'evidence.read', 'output.repair', 'stage.saved'}


def test_real_provider_options_are_serializable_and_caps_apply_to_requests(tmp_path):
    from loom.llm import LlmMessage
    from loom.llm.api import OpenAIProvider

    sent = []
    judge = BehaviorJudge()

    async def transport(url, request):
        body = request['body']
        sent.append(body)
        response = await judge.chat([LlmMessage(m['role'], m['content']) for m in body['messages']])
        return {'ok': True, 'status': 200, 'json': {
            'choices': [{'message': {'role': 'assistant', 'content': response.value.content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}}

    provider = OpenAIProvider(api_key='fixture', model='fixture', max_completion_tokens=65536,
        request_options={'enable_thinking': True, 'reasoning_effort': 'high', 'extra': {'nested': [1, 2]}}, http_client=transport)
    s = source(tmp_path, calls())
    result = asyncio.run(judge_behavior(s, build_behavior_facts(s), provider)).unwrap()
    assert result['coverage']['status'] == 'complete'
    assert [body['max_completion_tokens'] for body in sent] == [2048, 4096, 2048]
    assert all(body['enable_thinking'] is False and body['reasoning_effort'] == 'low' for body in sent)
    assert provider.request_options['enable_thinking'] is True
