import json
from copy import deepcopy

import pytest

from loom.core import ok
from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.goal_loops.graph_projection import project
from loom.evaluation.memory.evidence import card_evidence
from loom.evaluation.memory.projection import project_memory
from loom.evaluation.memory.review import commit, messages_for, protocol_for, validate_record
from loom.llm import LlmResponse, TokenUsage
from tests.evaluation.test_behavior_sequence import action, source
from tests.evaluation.test_incremental_graph import GraphJudge, run


def trace():
    store = source(
        [
            *action(1, "shell_execute", {"command": "uname -s"}, {"stdout": "Darwin arm64; Docker CLI unavailable", "exit_code": 0}),
            *action(2, "shell_execute", {"command": "grep -P pattern source.py"}, {"stderr": "grep: invalid option -- P", "exit_code": 2}),
            *action(3, "shell_execute", {"command": "rg pattern source.py"}, {"stdout": "source.py:12:pattern", "exit_code": 0}),
        ]
    )
    return store, build_behavior_facts(store)


class MemoryJudge(GraphJudge):
    def __init__(self):
        super().__init__()
        self.memory_requests = []

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        payload = json.loads(messages[1].content)
        if payload.get("mode") not in {"extract", "synthesis"}:
            return await super().chat(messages, **kwargs)
        self.memory_requests.append(payload)
        result = {k: [] for k in ("contributions", "facts", "lessons", "workflows")}
        evidence = payload.get('evidence', [{**e, 'action_id': a['id']} for a in payload['actions'] for e in a.get('evidence', [])])
        stage = next(iter(payload['stages']), next(m for a in payload['actions'] for m in a['memberships']))

        def common(entry, title):
            return dict(
                title=title,
                summary=title,
                scope="Recorded workspace on this macOS host; this run only",
                confidence="medium",
                goal_ids=[stage["goal_id"]],
                stage_ids=[stage.get('id', stage.get('stage_id'))],
                action_ids=[entry["action_id"]],
                citations=[dict(ref=entry["id"], quote=entry["text"])],
            )

        if payload["mode"] == "synthesis":
            entry = next(e for e in evidence if "invalid option" in e["text"])
            result["workflows"] = [
                dict(
                    **common(entry, "Check shell capabilities before searching"),
                    preconditions="macOS with an unknown grep implementation",
                    steps=["Check available search tools", "Use rg if available", "Verify the search results"],
                    avoid="Repeated grep -P invocations after unsupported-option output",
                    expected_benefit="Avoid repeated unsupported commands",
                    preserve="Check result relevance and run required tests",
                    validation="proposed",
                )
            ]
        else:
            for s in payload["stages"]:
                linked = {a["id"] for a in payload["actions"] if any(m["stage_id"] == s["id"] for m in a["memberships"])}
                entry = next(e for e in evidence if e["action_id"] in linked and e["basis"] == "observed_output")
                result["contributions"].append(
                    dict(
                        stage_id=s["id"],
                        goal_id=s["goal_id"],
                        rating="supporting",
                        rationale="Established host and search-tool constraints",
                        key_action_ids=[],
                        citations=[dict(ref=entry["id"], quote=entry["text"])],
                    )
                )
            entry = next(e for e in evidence if "Darwin arm64" in e["text"])
            result["facts"] = [
                dict(**common(entry, "Recorded host runs Darwin arm64"), category="environment", basis="observed", revalidate_when="Host or runtime changes")
            ]
            entry = next(e for e in evidence if "invalid option" in e["text"])
            result["lessons"] = [
                dict(
                    **common(entry, "grep -P was unsupported"),
                    trigger="Using grep -P on this host",
                    failed_approach="grep -P pattern source.py",
                    failure_reason="Unsupported P option",
                    better_approach="Check rg availability and use it for search",
                    validation="proposed",
                )
            ]
        if payload.get('delivery_id'):
            for kind, rows in result.items():
                for index, row in enumerate(rows):
                    row['key'] = f'{kind}:{index}'
                    row['evidence'] = [c['ref'] for c in row.pop('citations')]
                    if kind == 'contributions':
                        row.pop('goal_id')
                    else:
                        for key in ('title', 'action_ids', 'stage_ids', 'goal_ids'):
                            row.pop(key)
                    if kind == 'workflows':
                        row['steps'] = [dict(text=s, evidence=row['evidence'], basis='proposed') for s in row['steps']]
            result['evidence_requests'] = []
        return ok(LlmResponse(content=json.dumps(result), usage=TokenUsage(80, 40, 120)))


@pytest.mark.asyncio
async def test_memory_extracts_all_four_dimensions_with_scope_evidence_and_shared_usage():
    store, facts = trace()
    cp, judge = {}, MemoryJudge()
    result = await run(store, facts, "run", cp, judge, extract_memory=True)
    memory = result["memory"]
    assert memory["status"] == "complete"
    assert result["coverage"]["pipeline_complete"]
    assert len(memory["facts"]) == len(memory["lessons"]) == len(memory["workflows"]) == 1
    assert memory["contributions"][0]["rating"] == "supporting"
    assert memory["workflows"][0]["validation"] == "proposed"
    assert cp["usage"]["calls"] == 3  # One graph, one extraction, one cross-stage synthesis.
    assert len(judge.memory_requests) == 2
    assert memory["coverage"]["evidence_chars"] > 0
    assert "## Memory" in result["report"]["text"]
    for record in cp["memory"]["records"].values():
        assert record["source_digest"] == store.source_sha256
        assert record["run_id"] == "run"
        for ref in record["evidence_refs"]:
            store.resolve(ref)
    before = deepcopy(cp["memory"])
    await run(store, facts, "run", cp, judge, extract_memory=True)
    assert cp["memory"] == before
    assert len(judge.memory_requests) == 2
    assert project(store, facts, "run", cp)["memory"] == memory


@pytest.mark.asyncio
async def test_memory_budget_resume_preserves_graph_records_slices_and_accounting():
    store, facts = trace()
    cp, judge = {}, MemoryJudge()
    with pytest.raises(ValueError, match=r"\[budget.calls\].*2/2"):
        await run(store, facts, "run", cp, judge, extract_memory=True, max_calls=2)
    partial = project(store, facts, "run", cp)
    assert partial["memory"]["status"] == "partial"
    assert partial["memory"]["facts"]
    assert not partial["coverage"]["pipeline_complete"]
    assert cp["complete"]  # The completed graph is independent of memory synthesis.
    read_chars = cp["memory"]["read_chars"]
    result = await run(store, facts, "run", cp, judge, extract_memory=True, max_calls=4)
    assert result["memory"]["status"] == "complete"
    assert cp["memory"]["read_chars"] == read_chars
    assert len(judge.requests) == 1
    assert len(judge.memory_requests) == 2
    assert all(r["state"] == "settled" for r in cp["reservations"])


@pytest.mark.asyncio
async def test_evidence_budget_does_not_mark_unread_actions_as_reviewed():
    store, facts = trace()
    cp, judge = {}, MemoryJudge()
    with pytest.raises(ValueError, match="Memory evidence budget reached"):
        await run(store, facts, "run", cp, judge, extract_memory=True, max_evidence_chars=1)
    assert cp["memory"]["cursor"] == 0
    assert not judge.memory_requests
    result = await run(store, facts, "run", cp, judge, extract_memory=True, max_evidence_chars=100000)
    assert result["memory"]["status"] == "complete"


@pytest.mark.asyncio
async def test_memory_quotes_reject_undelivered_refs_and_cross_scope_claims_without_losing_good_records():
    store, facts = trace()
    cp = {}
    await run(store, facts, "run", cp, MemoryJudge(), extract_memory=True)
    memory = cp["memory"]
    unit = next(u for u in memory["units"].values() if u["mode"] == "extract")
    row = next(r for r in memory["records"].values() if r["kind"] == "facts")
    fields = ("title", "summary", "scope", "confidence", "goal_ids", "stage_ids", "action_ids", "citations", "category", "basis", "revalidate_when")
    fact = {k: deepcopy(row[k]) for k in fields}
    bad = deepcopy(fact)
    bad["citations"][0]["ref"] = "not-delivered"
    with pytest.raises(ValueError, match="delivered"):
        validate_record("facts", bad, unit)
    bad = deepcopy(fact)
    bad["goal_ids"] = ["another-run"]
    with pytest.raises(ValueError, match="undelivered"):
        validate_record("facts", bad, unit)
    bad = deepcopy(fact)
    bad["citations"][0]["quote"] = "Docker daemon is healthy"  # Not actually observed.
    with pytest.raises(ValueError, match="quote evidence"):
        validate_record("facts", bad, unit)
    result = {k: [] for k in ("contributions", "facts", "lessons", "workflows")}
    result["facts"] = [fact, bad]
    before = set(memory["records"])
    errors = commit(memory, unit, json.dumps(result))
    assert errors
    assert before <= set(memory["records"])


@pytest.mark.asyncio
async def test_model_intent_cannot_be_promoted_to_observed_fact():
    store, facts = trace()
    cp = {}
    await run(store, facts, "run", cp, MemoryJudge(), extract_memory=True)
    unit = next(u for u in cp["memory"]["units"].values() if u["mode"] == "extract")
    entry = next(e for e in unit["evidence"] if e["basis"] == "model_claim")
    row = dict(
        title="Claim",
        summary="Claim",
        scope="This run",
        confidence="low",
        goal_ids=[unit["scope"]["goals"][0]["id"]],
        stage_ids=[],
        action_ids=[entry["action_id"]],
        citations=[dict(ref=entry["id"], quote=entry["text"])],
        category="capability",
        basis="observed",
        revalidate_when="Before reuse",
    )
    with pytest.raises(ValueError, match="recorded tool output"):
        validate_record("facts", row, unit)
    row["basis"] = "inferred"
    assert validate_record("facts", row, unit)["basis"] == "inferred"


@pytest.mark.asyncio
async def test_evidence_is_bounded_redacted_and_keeps_tail_failure_with_exact_pointers():
    events = action(1, output={"stdout": "x" * 4000 + "\nimportant failure at tail", "api_key": "never-send-this-secret"})
    store = source(events)
    cp = {}
    await run(store, build_behavior_facts(store), "run", cp, GraphJudge())
    tool = next(c for c in cp["extraction"]["cards"] if c["kind"] == "tool")
    evidence = card_evidence(store, tool, cp["extraction"]["registry"])
    text = json.dumps(evidence)
    assert "never-send-this-secret" not in text
    assert "important failure at tail" in text
    assert sum(len(e["text"]) for e in evidence) <= 4000
    assert any(e["truncated"] for e in evidence)
    for entry in evidence:
        if not entry["redacted"]:
            assert store.resolve(entry["ref"]) == entry["text"]
    assert project_memory(cp)["status"] == "not_requested"


@pytest.mark.asyncio
async def test_memory_repairs_invalid_claim_without_repeating_good_memories():
    class RepairJudge(MemoryJudge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            response = await super().chat(messages, tools, cancellation, **kwargs)
            if json.loads(messages[1].content).get("mode") == "extract" and len(self.memory_requests) == 1:
                value = json.loads(response.value.content)
                value["facts"][0]["evidence"] = ["undelivered"]
                return ok(LlmResponse(content=json.dumps(value), usage=TokenUsage(80, 40, 120)))
            if json.loads(messages[1].content).get("mode") == "extract":
                value = json.loads(response.value.content)
                # Repairs follow the actual request contract: previously accepted
                # contributions and lessons are not submitted again.
                value.update(contributions=[], lessons=[])
                return ok(LlmResponse(content=json.dumps(value), usage=TokenUsage(80, 40, 120)))
            return response

    store, facts = trace()
    cp, judge = {}, RepairJudge()
    result = await run(store, facts, "run", cp, judge, extract_memory=True)
    assert result["memory"]["status"] == "complete"
    assert len(result["memory"]["facts"]) == len(result["memory"]["lessons"]) == 1
    assert len(judge.memory_requests) == 3
    assert judge.memory_requests[1]["feedback"]
    rejected = judge.memory_requests[1]["rejected_records"]
    assert len(rejected) == 1
    assert rejected[0]["kind"] == "facts"
    assert rejected[0]["record"]["evidence"] == ["undelivered"]
    assert rejected[0]['issue']['code'] == 'memory.anchor_undelivered'
    assert "contributions" in judge.memory_requests[1]["empty_record_types"]
    assert judge.memory_requests[1]["response_contract"]["properties"]["contributions"]["maxItems"] == 0
    assert not judge.memory_requests[2]["rejected_records"]


@pytest.mark.asyncio
async def test_cancelled_memory_request_retains_delivered_slices_and_unknown_usage():
    import asyncio

    entered = asyncio.Event()

    class SlowJudge(MemoryJudge):
        slow = True

        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            if self.slow and json.loads(messages[1].content).get("mode") == "extract":
                entered.set()
                await asyncio.Event().wait()
            return await super().chat(messages, tools, cancellation, **kwargs)

    store, facts = trace()
    cp, judge = {}, SlowJudge()
    task = asyncio.create_task(run(store, facts, "run", cp, judge, extract_memory=True))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cp["usage"]["unreported_calls"] == 1
    read_chars = cp["memory"]["read_chars"]
    judge.slow = False
    result = await run(store, facts, "run", cp, judge, extract_memory=True)
    assert result["memory"]["status"] == "complete"
    assert cp["usage"]["unreported_calls"] == 1
    assert cp["memory"]["read_chars"] == read_chars
    assert len(judge.requests) == 1


def test_memory_sanitization_removes_private_keys_and_credentials():
    from loom.evaluation.memory.evidence import sanitize

    value = {
        "stdout": "safe\n-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----\nafter",
        "content": "api_key=sk-example123456789; password=hidden",
        "api_key": "secret-key",
    }
    clean = json.dumps(sanitize(value))
    assert "private material" not in clean and "example123456789" not in clean and "secret-key" not in clean
    assert "safe" in clean and "after" in clean


@pytest.mark.asyncio
async def test_synthesis_repair_updates_consolidated_workflow_instead_of_duplicating_it():
    from loom.evaluation.memory.contracts import FIELDS

    store, facts = trace()
    cp = {}
    await run(store, facts, "run", cp, MemoryJudge(), extract_memory=True)
    memory = cp["memory"]
    unit = next(u for u in memory["units"].values() if u["mode"] == "synthesis")
    original = next(r for r in memory["records"].values() if r["kind"] == "workflows")
    row = {k: original[k] for k in FIELDS["workflows"]["items"]["properties"]}
    row = {**row, "title": "Consolidated search workflow", "summary": "Check capabilities once, then search and verify results"}
    result = {k: [] for k in FIELDS}
    result["workflows"] = [row]
    assert not commit(memory, unit, json.dumps(result))
    workflows = [r for r in memory["records"].values() if r["kind"] == "workflows"]
    assert len(workflows) == 1
    assert workflows[0]["id"] == original["id"]
    assert workflows[0]["title"] == row["title"]
    assert unit["accepted_records"][0]["title"] == row["title"]


def boundary_memory_case(kind="facts"):
    unit = dict(
        id="window",
        mode="extract",
        scope=dict(goals=[dict(id="goal")], stages=[], actions=[dict(id="tool"), dict(id="verification")]),
        evidence=[
            dict(id="output", action_id="tool", text="working tree clean", basis="observed_output", ref={"line_number": 2}),
            dict(id="check", action_id="verification", text="Check workspace status", basis="recorded_input_or_boundary", ref={"line_number": 1}),
        ],
    )
    row = dict(
        title="Clean workspace",
        summary="The recorded check found a clean workspace",
        scope="This workspace snapshot",
        confidence="medium",
        goal_ids=["goal"],
        stage_ids=[],
        action_ids=["tool"],
        citations=[dict(ref="output", quote="working tree clean"), dict(ref="check", quote="Check workspace status")],
    )
    if kind == "facts":
        row.update(category="project", basis="observed", revalidate_when="Workspace changes")
    elif kind == "lessons":
        row.update(
            trigger="Reverting changes",
            failed_approach="Leaving files modified",
            failure_reason="Unwanted changes",
            better_approach="Revert and verify status",
            validation="proposed",
        )
    else:
        row.update(
            preconditions="Revert requested",
            steps=["Revert", "Verify status"],
            avoid="Unverified completion",
            expected_benefit="Detect remaining changes",
            preserve="Workspace status check",
            validation="proposed",
        )
    return unit, row


@pytest.mark.parametrize("quote", [
    "margin 0.9 / spread 1.4 opens a visible gap",  # Joined lines / dropped comment prefix.
    "margin\n 0.9 / spread 1.4 opens a visible gap",  # Preserved newline but dropped '*'.
    "margin\n * 0.9 / spread 1.8 opens a visible gap",  # Changed the factual value.
])
def test_comment_citations_remain_literal_and_repair_identifies_the_failed_record(quote):
    unit, row = boundary_memory_case()
    literal = "margin\n * 0.9 / spread 1.4 opens a visible gap"
    unit["evidence"][0]["text"] = literal
    row["citations"][0]["quote"] = quote
    memory = dict(records={}, identity=dict(source="source", run_id="run", graph_revision=1))
    response = dict(contributions=[], facts=[row], lessons=[], workflows=[])
    errors = commit(memory, unit, json.dumps(response))
    assert len(errors) == 1
    assert "facts: record 1: Citation 1, ref output" in errors[0]
    assert unit["rejected_records"][0]["record"] == row
    assert not memory["records"]
    row["citations"][0]["quote"] = literal
    assert not commit(memory, unit, json.dumps(response))
    assert unit["rejected_records"] == []
    assert next(iter(memory["records"].values()))["citations"][0]["quote"] == literal


def test_windows_without_stages_and_synthesis_cannot_request_spurious_contributions():
    unit, row = boundary_memory_case()
    for entry in unit["evidence"]:
        entry["truncated"] = False
    messages, _ = messages_for(unit, "text")
    payload = json.loads(messages[1].content)
    assert payload["stages"] == []
    assert set(payload["empty_record_types"]) == {"contributions", "workflows"}
    assert protocol_for(unit).schema["properties"]["facts"]["maxItems"] > 0
    bogus = dict(stage_id="unknown", goal_id="goal", rating="supporting", rationale="Test passed", key_action_ids=[], citations=row["citations"])
    with pytest.raises(ValueError, match="No stages were delivered"):
        validate_record("contributions", bogus, unit)
    unit["mode"] = "synthesis"
    schema = protocol_for(unit).schema["properties"]
    assert all(schema[k]["maxItems"] == 0 for k in ("contributions", "facts", "lessons"))
    assert schema["workflows"]["maxItems"] > 0


@pytest.mark.parametrize("kind", ["facts", "lessons", "workflows"])
def test_cited_verification_owners_are_attached_without_changing_model_input(kind):
    unit, row = boundary_memory_case(kind)
    before = deepcopy(row)
    record = validate_record(kind, row, unit)
    assert row == before
    assert record["action_ids"] == ["tool", "verification"]
    assert record["normalizations"] == [dict(rule="include_cited_evidence_owners", added_action_ids=["verification"])]
    assert record["evidence_refs"] == [{"line_number": 2}, {"line_number": 1}]
    row["action_ids"] = []
    assert validate_record(kind, row, unit)["action_ids"] == ["tool", "verification"]


@pytest.mark.parametrize("invalid", ["unknown_action", "unknown_owner", "unknown_ref", "fabricated_quote"])
def test_action_scope_normalization_still_rejects_out_of_scope_or_unread_evidence(invalid):
    unit, row = boundary_memory_case()
    if invalid == "unknown_action":
        row["action_ids"].append("another-window")
    elif invalid == "unknown_owner":
        unit["evidence"][1]["action_id"] = "another-window"
    elif invalid == "unknown_ref":
        row["citations"][1]["ref"] = "unread"
    else:
        row["citations"][1]["quote"] = "No changes can ever occur"
    with pytest.raises(ValueError):
        validate_record("facts", row, unit)


@pytest.mark.asyncio
async def test_resume_revalidates_saved_scope_failure_without_another_extraction_call():
    from loom.evaluation.memory.contracts import FIELDS, PROTOCOL

    store, facts = trace()
    cp, judge = {}, MemoryJudge()
    await run(store, facts, "run", cp, judge, extract_memory=True)
    memory = cp["memory"]
    unit = next(u for u in memory["units"].values() if u["mode"] == "extract")
    response = next(r for r in reversed(cp["responses"]) if r["batch_id"] == unit["id"])
    value = {kind: [{k: r[k] for k in FIELDS[kind]['items']['properties']} for r in memory['records'].values()
                    if r['kind'] == kind and r['unit_id'] == unit['id']] for kind in FIELDS}
    fact = value["facts"][0]
    owner = fact["action_ids"][0]
    fact["action_ids"] = [next(a["id"] for a in unit["scope"]["actions"] if a["id"] != owner)]
    response["text"] = json.dumps(value)
    response['response_schema'] = deepcopy(PROTOCOL.schema)
    failure = "facts: Cited evidence must belong to the memory's action scope"
    response.update(validation_errors=[failure], validation_version="legacy")
    unit.update(complete=False, work_state='pending', protocol_version=1, errors=[failure], validation_version="legacy")
    unit.pop('manifest')
    memory['identity']['version'] = 'trace-memory.1'
    memory['execution_complete'] = False
    memory["complete"] = False
    memory["records"] = {k: r for k, r in memory["records"].items() if r["kind"] != "facts"}
    calls, usage = len(judge.memory_requests), deepcopy(cp["usage"])
    result = await run(store, facts, "run", cp, judge, extract_memory=True)
    assert result["memory"]["status"] == "complete"
    assert len(judge.memory_requests) == calls + 1
    assert judge.memory_requests[-1]['mode'] == 'synthesis'  # Recovered input has a new record identity.
    assert cp['usage']['calls'] == usage['calls'] + 1
    assert owner in result["memory"]["facts"][0]["action_ids"]
    assert response["validation_errors"] == [failure]
    assert response['revalidations'][-1]['issues'] == []
    assert cp['analysis_epochs'][-1]['snapshot']['units'][unit['id']]['errors'] == [failure]


@pytest.mark.asyncio
async def test_memory_provider_timeout_keeps_graph_and_reports_the_call_limit():
    from loom.core import err, make_loom_error
    from loom.evaluation.issues import EvaluationFailure

    class TimeoutMemory(GraphJudge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            if json.loads(messages[1].content).get("mode") in {"extract", "synthesis"}:
                return err(make_loom_error("TIMEOUT", "", retryable=True))
            return await super().chat(messages, tools, cancellation, **kwargs)

    store, facts = trace()
    checkpoint = {}
    with pytest.raises(EvaluationFailure) as failed:
        await run(store, facts, "run", checkpoint, TimeoutMemory(), extract_memory=True)
    assert checkpoint["graph_revision"] > 0
    detail = failed.value.issues[0]
    assert detail["stage"] == "memory"
    assert detail["code"] == "request.timeout"
    assert detail["details"]["timeout_seconds"] > 0
    assert "Resume" in detail["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['memory_read', 'memory_search', 'skill_read', 'skill_select', 'skill_record_step'])
async def test_retrieved_memory_cannot_become_fresh_observed_evidence(tool):
    store = source(action(1, tool, {'evaluation_id': 'old', 'record_id': 'm'}, {'text': 'Historical host was Darwin'}))
    cp = {}
    await run(store, build_behavior_facts(store), 'run', cp, GraphJudge())
    card = next(c for c in cp['extraction']['cards'] if c['name'] == tool)
    evidence = card_evidence(store, card, cp['extraction']['registry'])
    assert evidence and all(e['basis'] == 'historical_reference' for e in evidence)
    unit = dict(mode='extract', scope=dict(goals=[{'id': 'g'}], stages=[], actions=[{'id': card['id']}]), evidence=evidence)
    entry = evidence[0]
    row = dict(title='Host', summary='Host was Darwin', scope='Historical run', confidence='medium',
               goal_ids=['g'], stage_ids=[], action_ids=[card['id']], citations=[{'ref': entry['id'], 'quote': entry['text']}],
               category='environment', basis='observed', revalidate_when='Before use')
    with pytest.raises(ValueError, match='recorded tool output'):
        validate_record('facts', row, unit)
