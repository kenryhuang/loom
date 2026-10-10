import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from loom.contexts.bounded import BoundedContextManager
from loom.core import Observation, ToolRef, now_iso, ok, thaw_json
from loom.execution.native import NativeToolExecutionRuntime
from loom.llm.api import LlmMessage, LlmResponse, LlmToolCall
from loom.runtime.execution_contracts import Invocation
from loom.runtime.plugin_contracts import PluginManifest
from loom.runtime.workflow import WorkflowController
from loom.service.contracts import ServiceError, validate_create
from loom.service.scheduler import ready_sessions
from loom.tasks.assembly import TaskAssembly, default_plugin_registry, load_task_spec
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import make_task_context, run_generic_task
from loom.tools.collections import ToolCollection
from tests.acceptance_fakes import FixtureVerifier


class AnswerProvider:
    verification_provider = FixtureVerifier()
    model = "test"

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        return ok(LlmResponse(json.dumps({"reasoning": "evidence", "action": {"kind": "none", "description": "Done", "input": {"report": "done"}}})))


@pytest.mark.asyncio
async def test_general_task_without_workspace_or_programming_tools():
    request = TaskRequest("Answer a question", task_spec={"workflow": {"plugin": "dynamic"}})
    context = make_task_context(request).unwrap()
    assert not context.affordances.resources
    assert "workspace_inspection" not in {capability.id for capability in context.identity.capabilities}
    assert not {"read_file", "write_file", "shell_execute"} & {ref.id for ref in context.affordances.tools}
    result = (await run_generic_task(request, provider=AnswerProvider())).unwrap()
    assert result.output == "done"
    assert thaw_json(result.run_result.context.state.scratch)["workflow"]["workflow"]["nodes"][0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_dynamic_deterministic_node_then_model_node():
    spec = {
        "tools": {"collections": ["document_outputs", "task_control"]},
        "workflow": {
            "plugin": "dynamic",
            "nodes": [
                {
                    "id": "report",
                    "objective": "Write report",
                    "executor_kind": "tool",
                    "executor_config": {"tool_id": "create_report", "input": {"content": "facts", "sources": ["source:one"]}},
                },
                {"id": "answer", "objective": "Answer from report", "dependencies": ["report"]},
            ],
        },
        "outputs": [{"kind": "report", "require_evidence_refs": True}],
    }
    result = (await run_generic_task(TaskRequest("Research", task_spec=spec), provider=AnswerProvider(), options=TaskRunOptions(max_steps=4))).unwrap()
    scratch = thaw_json(result.run_result.context.state.scratch)
    assert [node["status"] for node in scratch["workflow"]["workflow"]["nodes"]] == ["succeeded", "succeeded"]
    assert scratch["execution_plugins"]["evidence"] == ["source:one"]
    artifact = scratch["output_artifacts"][0]
    assert json.loads(Path(artifact["local_path"]).read_text()) == {"report": "done", "sources": ["source:one"]}


@pytest.mark.asyncio
async def test_deterministic_journal_replay_restores_evidence_without_republishing():
    class Bridge:
        saved = None
        artifacts = 0

        def operation_start(self, call):
            return self.saved

        def operation_finish(self, call, result):
            self.saved = result

        def rpc(self, kind, value):
            assert kind == "publish_artifact"
            self.artifacts += 1
            return {"sha256": "digest", "kind": value["kind"]}

    request = TaskRequest("Report", task_spec={"tools": {"collections": ["document_outputs"]}, "workflow": {"plugin": "dynamic"}})
    bridge = Bridge()
    first = TaskAssembly(request, execution=bridge, run_id="run")
    before = first.snapshot()
    options = {"metadata": {"tool_call_id": "report"}}
    value = {"content": "facts", "sources": ["source:one"]}
    original = (await first.handlers()["create_report"](value, options)).unwrap()
    restored = TaskAssembly(request, execution=bridge, run_id="run")
    restored.restore(before)
    replayed = (await restored.handlers()["create_report"](value, options)).unwrap()
    assert replayed == original and bridge.artifacts == 1
    assert restored.snapshot()["evidence"] == ["source:one"]
    assert not restored.runtime.operations
    await first.close()
    await restored.close()


@pytest.mark.asyncio
async def test_workflow_revision_does_not_complete_the_running_node():
    root = WorkflowController().validate([{"id": "first", "objective": "Investigate"}])[0]
    root["status"] = "running"

    class Provider(AnswerProvider):
        verification_provider = FixtureVerifier()
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            self.calls += 1
            if self.calls == 1:
                value = {
                    "base_revision": 0,
                    "reason": "Findings require follow-up",
                    "nodes": [root, {"id": "follow-up", "objective": "Summarize", "dependencies": ["first"]}],
                }
                return ok(LlmResponse("", (LlmToolCall("revise", "revise_workflow", json.dumps(value)),)))
            if self.calls == 2:
                # A revision boundary must keep the current node running.
                assert "Current node: first" in str(messages)
                return ok(LlmResponse("", (LlmToolCall("complete", "complete_node", json.dumps({"evidence": "Investigation complete"})),)))
            assert "Current node: follow-up" in str(messages)
            return await super().chat(messages, tools, cancellation, tool_choice)

    provider = Provider()
    spec = {"workflow": {"plugin": "dynamic", "nodes": [{"id": "first", "objective": "Investigate"}]}}
    result = (await run_generic_task(TaskRequest("Investigate and summarize", task_spec=spec), provider=provider, options=TaskRunOptions(max_steps=4))).unwrap()
    workflow = thaw_json(result.run_result.context.state.scratch)["workflow"]["workflow"]
    assert workflow["revision"] == 1 and provider.calls == 3
    assert [node["status"] for node in workflow["nodes"]] == ["succeeded", "succeeded"]
    assert workflow["nodes"][0]["provenance"] == "Investigation complete"


@pytest.mark.asyncio
async def test_direct_answer_cannot_bypass_output_contract():
    spec = {"workflow": {"plugin": "dynamic"}, "outputs": [{"kind": "report", "require_evidence_refs": True}]}
    result = await run_generic_task(TaskRequest("Research", task_spec=spec), provider=AnswerProvider())
    assert result.ok and result.value.run_result.metrics.outcome == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin", ["dynamic", "legacy_planning"])
async def test_rejected_finish_keeps_workflow_unfinished(plugin):
    config = {"plugin": plugin} if plugin == "dynamic" else {"plugin": plugin, "mode": "force"}
    request = TaskRequest("Research", task_spec={"workflow": config, "outputs": [{"kind": "report", "require_evidence_refs": True}]})
    assembly = TaskAssembly(request)
    workflow = assembly.workflow
    if plugin == "dynamic":
        workflow.controller.nodes = workflow.controller.validate([{"id": "task", "objective": "Research", "status": "running"}])
        workflow._active = "task"
    else:
        workflow.controller.submit("Research", ("Read source",)).unwrap()
        item = workflow.controller.state.items[0]
        workflow.controller.update("Read", ({"id": item.id, "content": item.content, "status": "completed"},)).unwrap()
    result = (await assembly.handlers()["finish"]({"report": "No sources yet"})).unwrap()
    assert thaw_json(result.value)["accepted"] is False
    assert workflow.unfinished()
    await assembly.close()


def test_unavailable_execution_backend_never_falls_back(tmp_path):
    marker = tmp_path / "marker"
    with pytest.raises(ServiceError, match="no execution fallback"):
        validate_create({"objective": "Task", "task_spec": {"execution_runtime": {"plugin": "docker"}}})
    assert not marker.exists()


def test_plugin_configuration_and_version_fence_restore():
    original = TaskAssembly(TaskRequest("Task", task_spec={"workflow": {"plugin": "dynamic"}}))
    state = original.snapshot()
    restored = TaskAssembly(original.request)
    restored.restore(state)
    modified = TaskAssembly(
        TaskRequest("Task", task_spec={"context": {"plugin": "bounded_context", "max_history_steps": 2}, "workflow": {"plugin": "dynamic"}})
    )
    with pytest.raises(ValueError, match="configuration changed"):
        modified.restore(state)
    state["context"]["version"] = "old"
    with pytest.raises(ValueError, match="Incompatible"):
        restored.restore(state)


def test_persisted_frozen_context_plugin_state_restores():
    assembly = TaskAssembly(TaskRequest("Task", task_spec={"workflow": {"plugin": "dynamic"}}))
    context = make_task_context(assembly.request, assembly=assembly).unwrap()
    context = replace(context, state=replace(context.state, scratch={"execution_plugins": assembly.snapshot()}))
    restored = TaskAssembly(assembly.request)
    restored.restore(context.state.scratch["execution_plugins"])
    restored._evidence.append("new-source")
    assert restored.snapshot()["evidence"] == ["new-source"]
    assert not thaw_json(context.state.scratch)["execution_plugins"]["evidence"]


@pytest.mark.asyncio
async def test_native_execution_deduplicates_and_restores_results():
    calls = []

    async def handler(value, _options):
        calls.append(value)
        return ok(Observation("obs", "test", {"value": value}, now_iso()))

    runtime = NativeToolExecutionRuntime({}, entrypoints={"test/run": handler})
    invocation = Invocation("operation", "call", "test/run", "test/run", {"number": 1})
    first = await runtime.execute(invocation)
    second = await runtime.execute(invocation)
    assert first.execution_id == second.execution_id and len(calls) == 1
    restored = NativeToolExecutionRuntime({}, entrypoints={"test/run": handler})
    restored.restore(runtime.snapshot())
    result = await restored.execute(invocation)
    assert result.result.ok and len(calls) == 1
    with pytest.raises(ValueError, match="different invocation"):
        await restored.execute(replace(invocation, input={"number": 2}))
    assert restored.inspect("missing") == "unknown"


@pytest.mark.asyncio
async def test_changed_binding_version_cannot_reuse_a_previous_execution():
    assembly = TaskAssembly(
        TaskRequest("Report", task_spec={"tools": {"collections": ["document_outputs"]}, "workflow": {"plugin": "legacy_planning", "mode": "off"}})
    )
    value, options = {"content": "facts"}, {"metadata": {"tool_call_id": "report"}}
    assert (await assembly.handlers()["create_report"](value, options)).ok
    assembly.bindings["create_report"] = replace(assembly.bindings["create_report"], version="2")
    with pytest.raises(ValueError, match="different invocation"):
        await assembly.handlers()["create_report"](value, options)
    await assembly.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellable", [True, False])
async def test_native_cancel_does_not_claim_unconfirmed_termination(cancellable):
    started = asyncio.Event()

    async def handler(_value, _options):
        started.set()
        await asyncio.sleep(10)

    runtime = NativeToolExecutionRuntime({}, entrypoints={"slow": handler}, cancellable=["slow"] if cancellable else [])
    task = asyncio.create_task(runtime.execute(Invocation("operation", "call", "slow", "slow", {})))
    await started.wait()
    confirmed = await runtime.cancel("operation")
    await asyncio.gather(task, return_exceptions=True)
    assert confirmed == cancellable
    assert runtime.inspect("operation") == ("cancelled" if cancellable else "unknown")


@pytest.mark.asyncio
async def test_tool_collection_can_be_registered_without_changing_runner():
    calls = []

    async def lookup(value, _options):
        calls.append(value)
        return ok(Observation("obs", "lookup", {"answer": "42"}, now_iso()))

    collection = ToolCollection("catalog", (ToolRef("lookup", "Lookup a fact"),), {"lookup": lookup}, {"lookup": "read_only"})
    registry = default_plugin_registry()
    registry.register("tools", "catalog", lambda config, request: collection)

    class Provider(AnswerProvider):
        verification_provider = FixtureVerifier()
        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            if not any(message.role == "tool" for message in messages):
                return ok(LlmResponse("", (LlmToolCall("lookup", "lookup", "{}"),)))
            return await super().chat(messages, tools, cancellation, tool_choice)

    result = await run_generic_task(
        TaskRequest("Lookup", task_spec={"tools": {"collections": ["catalog"]}, "workflow": {"plugin": "legacy_planning", "mode": "off"}}),
        provider=Provider(),
        plugin_registry=registry,
    )
    assert result.ok and calls == [{}]


def test_missing_plugin_dependency_fails_closed():
    registry = default_plugin_registry()

    class NeedsDatabase:
        manifest = PluginManifest("database", "session_environment", dependencies=("connection/database",))

        def open(self):
            return self

        def resources(self):
            return ()

    registry.register("session_environment", "database", lambda config: NeedsDatabase())
    with pytest.raises(ValueError, match="Missing dependencies"):
        TaskAssembly(TaskRequest("Task", task_spec={"session_environment": {"plugin": "database"}}), registry=registry)


def test_read_only_resources_hide_mutating_tools(tmp_path):
    spec = {
        "session_environment": {"plugin": "session", "resources": [{"id": "project", "kind": "directory", "uri": str(tmp_path), "access": "read"}]},
        "workflow": {"plugin": "legacy_planning", "mode": "off"},
    }
    assembly = TaskAssembly(TaskRequest("Read", task_spec=spec))
    assert "read_file" in assembly.bindings
    assert not {"shell_execute", "write_file", "edit_file"} & assembly.bindings.keys()


def test_resource_scheduler_shares_reads_and_blocks_writes():
    def state(sid, mode):
        return {"session_id": sid, "task": {"state": "queued", "workspace": None, "resource_claims": [{"resource_id": "dataset:one", "access_mode": mode}]}}

    active = {"old": SimpleNamespace(workspace=None, resource_claims=({"resource_id": "dataset:one", "access_mode": "shared_read"},))}
    assert list(ready_sessions([state("writer", "exclusive_write"), state("reader", "shared_read")], active, 3)) == ["reader"]
    assert list(ready_sessions([state("b", "shared_read"), state("a", "shared_read")], {}, 2)) == ["a", "b"]


def test_workspace_free_sessions_do_not_lock_each_other():
    states = [{"session_id": sid, "task": {"state": "queued", "workspace": None, "resource_claims": []}} for sid in ("a", "b")]
    assert list(ready_sessions(states, {}, 2)) == ["b", "a"]


def test_context_compaction_preserves_pairs_and_complete_artifacts():
    artifacts = []

    def publish(value, kind):
        artifacts.append((value, kind))
        return {"sha256": "digest", "kind": kind}

    manager = BoundedContextManager({"recent_exchanges": 1, "summary_chars": 256}, publish_artifact=publish)
    old = LlmToolCall("old", "read", "{}")
    recent = LlmToolCall("recent", "read", "{}")
    messages = [
        LlmMessage("system", "fixed instructions"),
        LlmMessage("user", "goal"),
        LlmMessage("assistant", "", tool_calls=(old,)),
        LlmMessage("tool", "A" * 5000, tool_call_id="old"),
        LlmMessage("assistant", "", tool_calls=(recent,)),
        LlmMessage("tool", "recent facts", tool_call_id="recent"),
    ]
    window, event = manager.compact(messages, 1800)
    assert event and manager.size(window) < 1800
    assert [m.tool_call_id for m in window if m.role == "tool"] == ["recent"]
    assert [call.id for m in window for call in m.tool_calls] == ["recent"]
    assert artifacts[0][0]["messages"][1]["content"] == "A" * 5000
    restored = BoundedContextManager(manager.config, publish_artifact=publish)
    restored.restore(manager.snapshot())
    assert restored.generation == manager.generation and restored.summary == manager.summary


def test_incomplete_tool_exchange_cannot_be_compacted():
    manager = BoundedContextManager({}, publish_artifact=lambda *_: {"sha256": "digest"})
    with pytest.raises(ValueError, match="incomplete tool exchange"):
        manager.compact(
            [
                LlmMessage("system", "instructions"),
                LlmMessage("user", "goal"),
                LlmMessage("assistant", "A" * 3000, tool_calls=(LlmToolCall("call", "read", "{}"),)),
            ],
            1200,
        )


def test_workflow_revisions_validate_before_mutating_state():
    controller = WorkflowController(tools=["read"])
    controller.nodes = controller.validate([{"id": "first", "objective": "First"}])
    before = controller.snapshot()
    bad = {
        "base_revision": 0,
        "reason": "change",
        "nodes": [
            {"id": "first", "objective": "First", "dependencies": ["second"]},
            {"id": "second", "objective": "Second", "dependencies": ["first"]},
        ],
    }
    with pytest.raises(ValueError, match="cycle"):
        controller.propose(bad)
    assert controller.snapshot() == before
    bad["base_revision"] = -1
    with pytest.raises(ValueError, match="Stale"):
        controller.propose(bad)


def test_completed_workflow_nodes_are_immutable_and_capabilities_are_bounded():
    controller = WorkflowController()
    controller.nodes = controller.validate([{"id": "done", "objective": "Completed", "status": "succeeded"}])
    with pytest.raises(ValueError, match="cannot be rewritten"):
        controller.propose({"base_revision": 0, "reason": "change", "nodes": [{"id": "done", "objective": "Changed"}]})
    with pytest.raises(ValueError, match="capabilities"):
        controller.validate([{"id": "new", "objective": "New", "required_capabilities": ["root"]}])


def test_task_spec_yaml_resolves_directory_relative_to_spec(tmp_path):
    path = tmp_path / "task.yaml"
    path.write_text("session_environment:\n  plugin: session\n  resources:\n    - id: project\n      kind: directory\n      uri: .\n      access: read\n")
    spec = load_task_spec(path)
    assert spec["session_environment"]["resources"][0]["uri"] == str(tmp_path)


@pytest.mark.parametrize(
    "value", [{"session_environment": None}, {"session_environment": {"resources": [False]}}, {"session_environment": {"resources": [{"kind": "directory"}]}}]
)
def test_malformed_resource_files_fail_with_configuration_error(tmp_path, value):
    path = tmp_path / "task.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_task_spec(path)


def test_invalid_runtime_with_directory_fails_with_configuration_error(tmp_path):
    spec = {
        "session_environment": {"plugin": "session", "resources": [{"id": "project", "kind": "directory", "uri": str(tmp_path)}]},
        "execution_runtime": None,
    }
    with pytest.raises(ServiceError, match="execution_runtime"):
        validate_create({"objective": "Task", "task_spec": spec})


@pytest.mark.asyncio
async def test_unconfirmed_deadline_preserves_unknown_status():
    async def slow(_value, _options):
        await asyncio.sleep(5)

    runtime = NativeToolExecutionRuntime({}, entrypoints={"slow": slow})
    result = await runtime.execute(Invocation("operation", "call", "slow", "slow", {}, deadline=time.time() + 0.01))
    assert result.status == "unknown" and result.result.error.code == "EXECUTION_UNKNOWN"
    assert runtime.inspect("operation") == "unknown"


@pytest.mark.parametrize(
    "spec",
    [
        False,
        {"session_environment": []},
        {"tools": {"collections": "shell"}},
        {"outputs": [{}]},
        {"context": {"plugin": "bounded_context", "max_window_chars": -1}},
        {"workflow": {"plugin": "dynamic", "nodes": [False]}},
        {"workflow": {"plugin": "dynamic", "nodes": False}},
    ],
)
def test_invalid_plugin_configuration_is_rejected_at_creation(spec):
    with pytest.raises(ServiceError):
        validate_create({"objective": "Task", "task_spec": spec})


def test_proposal_cannot_skip_execution_by_claiming_success():
    controller = WorkflowController()
    controller.nodes = controller.validate([{"id": "first", "objective": "First"}])
    with pytest.raises(ValueError, match="unexecuted nodes"):
        controller.propose({"base_revision": 0, "reason": "claim done", "nodes": [{"id": "first", "objective": "First", "status": "succeeded"}]})


def test_writable_directory_cannot_request_shared_lock(tmp_path):
    spec = {
        "session_environment": {
            "plugin": "session",
            "resources": [{"id": "project", "kind": "directory", "uri": str(tmp_path), "access": "read-write", "access_mode": "shared_read"}],
        }
    }
    with pytest.raises(ValueError, match="exclusive"):
        TaskAssembly(TaskRequest("Write", task_spec=spec))
