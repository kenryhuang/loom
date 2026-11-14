import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from loom.contexts.bounded import BoundedContextManager
from loom.contexts.working_set import encoded
from loom.core import ok, thaw_json
from loom.llm.api import LlmMessage, LlmResponse, LlmToolCall
from loom.tasks.assembly import TaskAssembly
from loom.tasks.context_review import replay
from loom.tasks.evidence import artifact_page
from loom.tasks.file_read import file_read_payload
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import make_task_context, run_generic_task
from tests.tasks.test_plugin_runtime import AnswerProvider


class Artifacts:
    def __init__(self):
        self.values = {}

    def publish(self, value, kind):
        digest = hashlib.sha256(encoded(value).encode()).hexdigest()
        self.values[digest] = deepcopy(value)
        return {"sha256": digest, "kind": kind}

    def read(self, digest):
        return deepcopy(self.values[digest])


def source(state, key="read1", text="plan is consumed", path="page.ts", start=0, version=None):
    output = file_read_payload(text.encode(), path, "/workspace/" + path)
    output["resource"]["byte_range"] = [start, start + len(text.encode())]
    if version:
        output["resource"]["after_sha256"] = version
    receipt = {"id": "tool:run:" + key, "tool": "read_file", "ok": True, "input": json.dumps({"path": path}),
               "artifact": state.publish_artifact({"input": {"path": path}, "output": output}, "acceptance_tool_evidence"),
               "artifact_format": "call", "goal_digest": "goal"}
    state.observe_tool(receipt, output, workspace="/workspace")
    return receipt, output


def notes(state, evidence_id="tool:run:read1", *, change_id="notes1", claim="Page consumes the practice plan"):
    item = state.data["sources"][evidence_id]
    return {"change_id": change_id, "base_revision": state.data["revision"],
            "findings": [{"id": "f1", "question_id": "q1", "claim": claim, "kind": "observed", "criterion_ids": ["wiring"],
                          "limitations": "Static evidence only", "evidence": [{"evidence_id": evidence_id, "range": item["range"], "unit": item["unit"]}]}],
            "questions": [{"id": "q1", "question": "Does the page consume the plan?", "status": "supported", "finding_ids": ["f1"],
                           "next_action": "Check runtime separately if required"}]}


def manager(artifacts, **config):
    return BoundedContextManager({"working_set": config}, publish_artifact=artifacts.publish, read_artifact=artifacts.read)


def test_repeated_compaction_restore_preserves_findings_and_pending_ranges():
    artifacts = Artifacts()
    context = manager(artifacts)
    state = context.working_set
    state.set_goal("goal")
    source(state)
    state.update_notes(notes(state))
    source(state, "pending", "tail remains unknown", path="api.ts", start=200)
    messages = [LlmMessage("system", "Review the wiring"), LlmMessage("user", "Task")]
    for i in range(7):
        messages.extend([LlmMessage("assistant", "", tool_calls=(LlmToolCall(str(i), "read_file", '{}'),)),
                         LlmMessage("tool", "content" * 2000, name="read_file", tool_call_id=str(i))])
        messages, event = context.compact(messages, 10000)
        assert event and context.size(messages) <= 9000
        projected = json.loads(next(m.content for m in messages if m.name == "context_working_set"))
        assert projected["status"]["findings"] == 1 and projected["status"]["pending_sources"] == 1
        restored = manager(artifacts)
        restored.restore(context.snapshot())
        assert restored.working_set.data == context.working_set.data
        context = restored
    assert context.working_set.data["findings"]["f1"]["claim"] == "Page consumes the practice plan"
    assert context.working_set.data["sources"]["tool:run:pending"]["range"] == [200, 220]


def test_notes_are_atomic_idempotent_and_not_acceptance():
    artifacts = Artifacts()
    state = manager(artifacts).working_set
    source(state)
    change = notes(state)
    result = state.update_notes(change)
    before = deepcopy(state.data)
    assert state.update_notes(change) == result and state.data == before
    for mutation in (lambda c: c.update(change_id="other"),
                     lambda c: c["findings"][0]["evidence"][0].update(range=[0, 9999]),
                     lambda c: c["findings"][0].update(claim="different")):
        bad = deepcopy(change)
        mutation(bad)
        with pytest.raises(ValueError):
            state.update_notes(bad)
        assert state.data == before
    assert "passed" not in encoded(state.data) and "acceptance is independent" in result["note"]


@pytest.mark.parametrize("mutation", [
    lambda c: c["findings"][0]["evidence"][0].update(evidence_id="invented"),
    lambda c: c["findings"][0]["evidence"][0].update(unit="entry"),
    lambda c: c["findings"][0]["evidence"][0].update(range=[0, 9000]),
    lambda c: c["findings"][0].update(evidence=[]),
    lambda c: c["questions"][0].update(finding_ids=["invented"]),
])
def test_invalid_citations_cannot_be_saved(mutation):
    state = manager(Artifacts()).working_set
    source(state)
    change = notes(state)
    mutation(change)
    before = deepcopy(state.data)
    with pytest.raises(ValueError):
        state.update_notes(change)
    assert before == state.data


def test_new_versions_and_goal_revisions_preserve_history_without_reusing_mapping():
    state = manager(Artifacts()).working_set
    state.set_goal("goal")
    source(state)
    state.update_notes(notes(state))
    source(state, "read2", "no plan consumption")
    assert state.data["sources"]["tool:run:read1"]["stale"]
    with pytest.raises(ValueError, match="stale"):
        state.update_notes(notes(state, change_id="new"))
    state.set_goal("new goal")
    assert not state.selected_evidence([{"id": "wiring"}])
    assert state.data["findings"]["f1"]["goal_digest"] == "goal"
    assert state.data["history"]


def test_projection_is_bounded_and_does_not_nest_previews():
    context = manager(Artifacts(), max_projection_chars=1500, preview_chars=100)
    state = context.working_set
    receipt, output = source(state, text="x" * 2000)
    for i in range(100):
        source(state, str(i), path=f"file-{i}.ts")
    messages = [LlmMessage("system", "Review"), LlmMessage("user", "Task"),
                LlmMessage("assistant", "", tool_calls=(LlmToolCall("read1", "read_file", '{}'),)),
                LlmMessage("tool", encoded(output), name="read_file", tool_call_id="read1")]
    first = context.prepare_window(messages, 10000)
    second = context.prepare_window(first, 10000)
    assert first == second
    projected = next(m for m in first if m.name == "context_working_set")
    assert len(projected.content) <= 1500
    assert json.loads(projected.content)["omitted"]["sources"] > 0
    assert first[2].tool_calls == messages[2].tool_calls and first[3].tool_call_id == "read1"
    raw = state.read_artifact(receipt["artifact"]["sha256"])
    assert raw["output"]["content"] == "x" * 2000


def test_utf8_range_reads_cover_missing_tail_without_repeating_prefix():
    raw = "ab中文tail".encode()
    first = file_read_payload(raw, "file", "/file", max_bytes=6)
    assert first["content"] == "ab中" and first["resource"]["byte_range"] == [0, 5]
    second = file_read_payload(raw, "file", "/file", offset_bytes=first["next_offset_bytes"], max_bytes=20)
    assert first["content"] + second["content"] == raw.decode()
    assert second["resource"]["byte_range"] == [5, len(raw)] and second["next_offset_bytes"] is None
    assert first["resource"]["after_sha256"] == second["resource"]["after_sha256"]
    with pytest.raises(ValueError, match="inside"):
        file_read_payload(raw, "file", "/file", offset_bytes=3)
    page = artifact_page({"input": {}, "output": second}, "digest", view="text", offset=1)
    assert page["content"] == "tail"  # artifact offset counts characters, not source bytes


def review_request(tmp_path, **config):
    return TaskRequest("Review practice plan wiring", workspace=tmp_path, task_spec={
        "session_environment": {"plugin": "session", "resources": [{"id": "workspace", "kind": "directory",
            "uri": str(tmp_path), "access": "read-only"}]},
        "tools": {"collections": ["filesystem", "task_control"]},
        "context": {"plugin": "bounded_context", "working_set": config},
        "workflow": {"plugin": "legacy_planning", "mode": "off"}})


@pytest.mark.asyncio
async def test_crash_between_receipt_and_checkpoint_replays_sources_and_notes(tmp_path):
    (tmp_path / "page.ts").write_text("plan is consumed")
    request = review_request(tmp_path)
    original = TaskAssembly(request)
    original.acceptance.goal(make_task_context(request, assembly=original).unwrap())
    before = original.snapshot()
    result = (await original.handlers()["read_file"]({"path": "page.ts"}, {"metadata": {"tool_call_id": "read1"}})).unwrap()
    state = original.context_manager.working_set
    identifier = next(iter(state.data["sources"]))
    note_result = (await original.handlers()["record_context_notes"](notes(state, identifier))).unwrap()
    after = deepcopy(state.data)
    restored = TaskAssembly(request)
    restored._local_artifacts = original._local_artifacts
    restored.restore(before)
    (tmp_path / "page.ts").unlink()  # replay must not re-read or regenerate source evidence
    replay(restored, result)
    replay(restored, note_result)
    replay(restored, result)
    replay(restored, note_result)
    assert restored.context_manager.working_set.data == after
    assert len(restored._acceptance_calls) == 1
    await original.close()
    await restored.close()


@pytest.mark.asyncio
async def test_current_file_change_rejects_new_claim_and_keeps_old_finding(tmp_path):
    (tmp_path / "page.ts").write_text("plan is consumed")
    assembly = TaskAssembly(review_request(tmp_path))
    await assembly.handlers()["read_file"]({"path": "page.ts"})
    state = assembly.context_manager.working_set
    identifier = next(iter(state.data["sources"]))
    (tmp_path / "page.ts").write_text("plan no longer consumed")
    result = (await assembly.handlers()["record_context_notes"](notes(state, identifier))).unwrap()
    assert not result.value["accepted"] and state.data["sources"][identifier]["stale"]
    assert not state.data["findings"]
    await assembly.close()


@pytest.mark.asyncio
async def test_normal_runner_consumes_notes_in_next_request_and_acceptance_uses_raw_evidence(tmp_path):
    (tmp_path / "page.ts").write_text("plan is consumed\n" + "detail\n" * 1000)

    class Provider(AnswerProvider):
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            self.calls += 1
            assert "record_context_notes" in str(tools)
            projected = json.loads(next(m.content for m in messages if m.name == "context_working_set"))
            if self.calls == 1:
                return ok(LlmResponse("", (LlmToolCall("read", "read_file", '{"path":"page.ts"}'),)))
            if self.calls == 2:
                src = projected["sources"][0]
                change = {"change_id": "n", "base_revision": projected["revision"], "findings": [
                    {"id": "f", "question_id": "q", "claim": "plan is consumed", "kind": "observed", "criterion_ids": ["goal"],
                     "limitations": "static only", "evidence": [{"evidence_id": src["id"], "range": [0, 16], "unit": "byte"}]}],
                    "questions": [{"id": "q", "question": "plan wiring", "finding_ids": ["f"], "status": "supported", "next_action": "report"}]}
                return ok(LlmResponse("", (LlmToolCall("note", "record_context_notes", json.dumps(change)),)))
            assert projected["findings"][0]["claim"] == "plan is consumed"
            assert not projected["status"]["pending_sources"]
            assert "detail\ndetail" not in str(messages)
            return await super().chat(messages, tools, cancellation, tool_choice)

    result = (await run_generic_task(review_request(tmp_path), provider=Provider(), options=TaskRunOptions(max_steps=10))).unwrap()
    scratch = thaw_json(result.run_result.context.state.scratch)
    assert scratch["review_working_set"]["findings"] == 1
    saved = scratch["execution_plugins"]["context"]["state"]["working_set"]
    assert saved["artifact"]["kind"] == "context_working_set"


def test_disabled_context_keeps_legacy_checkpoint_and_rejects_implicit_enablement():
    artifacts = Artifacts()
    context = BoundedContextManager({}, publish_artifact=artifacts.publish)
    snapshot = context.snapshot()
    assert "working_set" not in snapshot["state"]
    context.restore(snapshot)
    with pytest.raises(ValueError, match="Incompatible"):
        manager(artifacts).restore(snapshot)


def test_lane_merge_is_explicit_idempotent_and_namespaced():
    artifacts = Artifacts()
    parent = manager(artifacts).working_set
    child = manager(artifacts).working_set
    source(child)
    child.update_notes(notes(child))
    assert not parent.data["sources"]
    snapshot = child.snapshot()
    parent.merge(snapshot, "lane-a")
    after = deepcopy(parent.data)
    parent.merge(snapshot, "lane-a")
    assert after == parent.data
    assert parent.data["findings"]["lane-a:f1"]["question_id"] == "lane-a:q1"
    assert parent.data["questions"]["lane-a:q1"]["finding_ids"] == ["lane-a:f1"]


def test_lane_version_conflict_requires_revalidation_in_parent():
    artifacts = Artifacts()
    parent, child = manager(artifacts).working_set, manager(artifacts).working_set
    source(parent, "old", "old version")
    source(child, "new", "new version")
    parent.merge(child.snapshot(), "lane-a")
    assert all(s["stale"] for s in parent.data["sources"].values())
    source(parent, "confirmed", "new version")
    assert not parent.data["sources"]["tool:run:confirmed"]["stale"]


def test_stalled_read_policy_is_explicit_and_notes_reset_it():
    state = manager(Artifacts(), max_stalled_reads=2).working_set
    source(state)
    source(state, "read2")
    source(state, "read3")
    assert state.pause_reason()
    state.update_notes(notes(state))
    assert not state.pause_reason()
    default = manager(Artifacts()).working_set
    for i in range(20):
        source(default, str(i))
    assert not default.pause_reason()


@pytest.mark.asyncio
async def test_managed_session_restores_notes_before_next_model_request(tmp_path):
    from loom.llm.managed_step import ManagedStep
    from loom.runtime import create, create_runtime_registry, step
    from loom.runtime.checkpoints import decode, encode
    from loom.tasks.runner import make_task_loop
    from tests.llm.test_managed_step import Execution
    from tests.tasks.test_acceptance import Sink

    class Journal(Execution):
        def __init__(self):
            super().__init__()
            self.store = Artifacts()
            self.receipts = {}
            self.pause = True

        def rpc(self, kind, payload):
            if kind == "publish_artifact":
                return self.store.publish(payload["value"], payload["kind"])
            assert kind == "read_artifact"
            return self.store.read(payload["digest"])

        def operation_start(self, call):
            return self.receipts.get(call.id)

        def operation_finish(self, call, result):
            self.receipts[call.id] = result

        def boundary(self, checkpoint):
            self.checkpoint = decode(encode(checkpoint))
            return {"control": {"kind": "paused", "reason": "test restart"} if self.pause and "note" in self.receipts else None}

    class Provider(AnswerProvider):
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            self.calls += 1
            view = json.loads(next(m.content for m in messages if m.name == "context_working_set"))
            if self.calls == 1:
                return ok(LlmResponse("", (LlmToolCall("read", "read_file", '{"path":"page.ts"}'),)))
            if self.calls == 2:
                state = journal.current.context_manager.working_set
                change = notes(state, view["sources"][0]["id"])
                return ok(LlmResponse("", (LlmToolCall("note", "record_context_notes", json.dumps(change)),)))
            assert view["findings"][0]["claim"] == "Page consumes the practice plan"
            assert view["sources"][0]["state"] == "analyzed"
            # Pause before acceptance so this test specifically isolates worker restoration.
            return ok(LlmResponse("", (LlmToolCall("ask", "request_input", '{"question":"Continue review?"}'),)))

    (tmp_path / "page.ts").write_text("plan is consumed")
    request, journal, provider = review_request(tmp_path), Journal(), Provider()

    async def execute():
        assembly = TaskAssembly(request, execution=journal)
        journal.current = assembly
        assembly.configure_acceptance(provider)
        context = make_task_context(request, assembly=assembly).unwrap()
        if journal.checkpoint:
            assembly.restore(journal.checkpoint["plugin_states"])
            context = journal.checkpoint["context"]
        managed = ManagedStep(provider, journal, assembly=assembly, planning=assembly.workflow)
        definition = assembly.wrap_loop(replace(make_task_loop(request, provider), step=managed))
        handle = create(definition, registry=create_runtime_registry(tools=assembly.handlers())).unwrap()
        try:
            return (await step(handle, context, trace_sink=Sink())).unwrap()
        finally:
            await assembly.close()

    assert (await execute()).control.kind == "paused"
    journal.pause = False
    (tmp_path / "page.ts").unlink()
    assert (await execute()).control.kind in {"paused", "waiting_input"}
    assert provider.calls == 3 and set(journal.receipts) == {"read", "note"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["barrier", "pipeline"])
async def test_parallel_dag_lanes_keep_notes_isolated_and_merge_explicit_results(tmp_path, mode):
    from tests.tasks.test_adaptive_dag import branch

    for name in ("a", "b"):
        (tmp_path / f"{name}.ts").write_text(name + " plan consumed")
    request = review_request(tmp_path)
    specification = deepcopy(request.task_spec)
    nodes = [{**branch(name), "executor_config": {"allowed_tools": ["read_file"]}} for name in ("a", "b")]
    if mode == "pipeline":
        nodes[0]["execution_hint"] = "main_candidate"
    nodes.append({"id": "integrate", "objective": "Integrate", "dependencies": ["a", "b"]})
    specification["workflow"] = {"plugin": "adaptive_dag", "nodes": nodes, "parallel": {"mode": mode}}

    class Provider(AnswerProvider):
        def __init__(self):
            self.calls = {}

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            view = json.loads(next(m.content for m in messages if m.name == "context_working_set"))
            if "complete_branch" not in str(tools):
                assert view["status"]["sources"] == 2 and view["status"]["findings"] == 2
                assert {f["claim"] for f in view["findings"]} == {"a consumes plan", "b consumes plan"}
                return await super().chat(messages, tools, cancellation, tool_choice)
            contract = json.loads(next(m.content for m in messages if m.name == "branch_contract"))
            name = contract["stable_inputs"]["dataset"][0]
            self.calls[name] = self.calls.get(name, 0) + 1
            count = self.calls[name]
            if count == 1:
                assert not view["sources"]
                return ok(LlmResponse("", (LlmToolCall(name + "read", "read_file", json.dumps({"path": name + ".ts"})),)))
            own_sources = [s for s in view["sources"] if s["path"] == name + ".ts"]
            assert len(own_sources) == 1
            if mode != "pipeline" or name != "a":
                assert len(view["sources"]) == 1
            if count == 2:
                src = own_sources[0]
                change = {"change_id": "same-local-id", "base_revision": view["revision"], "findings": [
                    {"id": "f", "question_id": "q", "claim": name + " consumes plan", "kind": "observed", "criterion_ids": ["goal"],
                     "limitations": "static", "evidence": [{"evidence_id": src["id"], "range": src["range"], "unit": "byte"}]}],
                    "questions": [{"id": "q", "question": "Wiring", "finding_ids": ["f"], "status": "supported", "next_action": "handoff"}]}
                return ok(LlmResponse("", (LlmToolCall(name + "note", "record_context_notes", json.dumps(change)),)))
            assert any(f["claim"] == name + " consumes plan" for f in view["findings"])
            return ok(LlmResponse("", (LlmToolCall(name + "done", "complete_branch", json.dumps({"outputs": {"findings": name + " consumes plan"}})),)))

    result = (await run_generic_task(replace(request, task_spec=specification), provider=Provider(),
                                     options=TaskRunOptions(max_steps=5))).unwrap()
    assert result.run_result.context.state.scratch["review_working_set"]["findings"] == 2


@pytest.mark.asyncio
async def test_acceptance_prioritizes_citations_without_accepting_claims(tmp_path):
    from loom.tasks.acceptance_evidence import evidence

    (tmp_path / "page.ts").write_text("actual implementation contradicts the note")
    assembly = TaskAssembly(review_request(tmp_path))
    await assembly.handlers()["read_file"]({"path": "page.ts"})
    state = assembly.context_manager.working_set
    identifier = next(iter(state.data["sources"]))
    state.update_notes(notes(state, identifier, claim="unverified optimistic claim"))
    for i in range(60):
        (tmp_path / f"noise{i}.txt").write_text("unrelated")
        await assembly.handlers()["read_file"]({"path": f"noise{i}.txt"})
    rows = evidence(assembly, criteria=[{"id": "wiring", "verifier": "semantic", "scope": [], "check": {}}])
    assert rows[1]["id"] == identifier
    assert "actual implementation contradicts" in rows[1]["output"]
    assert "optimistic claim" not in rows[1]["output"]
    assert assembly.acceptance.data["state"] != "passed"
    await assembly.close()


@pytest.mark.asyncio
async def test_json_action_results_use_the_same_review_projection(tmp_path):
    (tmp_path / "page.ts").write_text("plan consumed " * 1000)

    class Provider(AnswerProvider):
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            self.calls += 1
            if self.calls == 1:
                return ok(LlmResponse(json.dumps({"reasoning": "Read page", "action": {
                    "kind": "tool", "target": "read_file", "description": "Inspect", "input": {"path": "page.ts"}}})))
            source_message = next(m for m in messages if m.role == "user" and m.name == "read_file")
            assert json.loads(source_message.content)["context_excerpt"]
            assert len(source_message.content) < 2000
            view = json.loads(next(m.content for m in messages if m.name == "context_working_set"))
            assert view["status"]["pending_sources"] == 1  # An excerpt is not a completed analysis.
            return await super().chat(messages, tools, cancellation, tool_choice)

    assert (await run_generic_task(review_request(tmp_path, preview_chars=300), provider=Provider())).ok


@pytest.mark.asyncio
async def test_opt_in_stall_returns_cooperative_pause_with_retained_state(tmp_path):
    (tmp_path / "page.ts").write_text("plan consumed")

    class Provider(AnswerProvider):
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            self.calls += 1
            assert self.calls <= 3
            return ok(LlmResponse("", (LlmToolCall("read" + str(self.calls), "read_file", '{"path":"page.ts"}'),)))

    result = (await run_generic_task(review_request(tmp_path, max_stalled_reads=2), provider=Provider())).unwrap()
    assert result.run_result.metrics.outcome == "paused"
    assert result.run_result.context.state.scratch["review_working_set"]["sources"] == 3


def test_enabled_working_set_respects_allowlist_and_rejects_isolated_write_lanes(tmp_path):
    from loom.tasks.request import TaskHarness

    request = review_request(tmp_path)
    with pytest.raises(ValueError, match="allowlist"):
        TaskAssembly(request, harness=TaskHarness(allowed_tools=("read_file", "read_artifact")))
    specification = deepcopy(request.task_spec)
    specification["workflow"] = {"plugin": "adaptive_dag", "parallel": {"isolated_writes": True}}
    with pytest.raises(ValueError, match="isolated_writes"):
        TaskAssembly(replace(request, task_spec=specification))
