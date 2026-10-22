import asyncio
import json
from copy import deepcopy

import pytest

from loom.core import ok, thaw_json
from loom.llm.api import LlmResponse, LlmToolCall, TokenUsage
from loom.runtime.dag_scheduler import DagController, branch_admission
from loom.tasks.assembly import TaskAssembly, normalize_task_spec
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import run_generic_task
from tests.tasks.test_plugin_runtime import AnswerProvider


def branch(nid, **changes):
    return dict(id=nid, objective=f"Independently analyze dataset {nid}", execution_hint="parallel_candidate", context_independent=True,
                independence_reason="Independent immutable datasets; no shared implementation decisions or workspace writes",
                stable_inputs={"dataset": nid + " self-contained immutable facts"}, outputs=[dict(name="findings", kind="text")],
                executor_config={"allowed_tools": []}, **changes)


def spec(nodes):
    return dict(workflow=dict(plugin="adaptive_dag", nodes=nodes))


def test_default_unchanged_and_independence_is_not_implied_by_empty_dependencies():
    assert normalize_task_spec()["workflow"]["plugin"] == "legacy_planning"
    assembly = TaskAssembly(TaskRequest("Task", task_spec=spec([branch("one")])))
    node = assembly.workflow.controller.nodes[0]
    assert branch_admission(node, assembly.bindings) is None
    for updates in ({"context_independent": False}, {"stable_inputs": {}}, {"independence_reason": ""},
                    {"executor_config": {"allowed_tools": ["finish"]}}, {"executor_config": {}}):
        assert branch_admission({**node, **updates}, assembly.bindings)


def test_data_dependencies_require_success_and_actual_named_results():
    controller = DagController()
    controller.nodes = controller.validate([branch("a"), dict(id="b", objective="Consume a", dependencies=["a"],
        inputs=[dict(from_node="a", output_name="findings")])])
    for status in ("skipped", "superseded", "failed", "succeeded"):
        controller.nodes[0]["status"] = status
        assert not controller.ready()
    controller.results["a"] = dict(outputs={"findings": "result"})
    assert [n["id"] for n in controller.ready()] == ["b"]
    with pytest.raises(ValueError, match="cycle"):
        controller.validate([dict(id="a", objective="a", dependencies=["b"]), dict(id="b", objective="b", dependencies=["a"])])


def test_dag_revision_upserts_future_contracts_but_preserves_active_history():
    controller = DagController()
    controller.nodes = controller.validate([dict(id="active", objective="Keep context", status="running")])
    original = deepcopy(controller.nodes[0])
    controller.propose(dict(base_revision=0, reason="Plan independent work", nodes=[branch("new")]))
    assert controller.nodes[0] == original
    with pytest.raises(ValueError, match="cannot be rewritten"):
        controller.propose(dict(base_revision=1, reason="Bad rewrite", nodes=[dict(id="active", objective="Replace history")]))
    assert controller.revision == 1


@pytest.mark.asyncio
async def test_shared_budget_declines_new_agents_and_returns_work_to_main():
    from types import SimpleNamespace

    from loom.runtime.engine import CancellationToken
    from loom.tasks.dag_lanes import run_lanes
    from loom.tasks.runner import make_task_context

    request = TaskRequest("Integrate", task_spec=spec([branch("a"), branch("b")]))
    assembly = TaskAssembly(request)
    class NoCalls:
        model = "fixture"
        async def chat(self, *args, **kwargs):
            raise AssertionError("No branch request can spend the integration reserve")
    assembly.configure_acceptance(NoCalls(), limits={"max_llm_calls": 2})
    async def emit(event):
        return ok(None)
    context = make_task_context(request, assembly=assembly).unwrap()
    async def prepared(context, runtime, checkpoint):
        return context
    assembly.prepare_acceptance = prepared
    runtime = SimpleNamespace(run_id=context.run_id, trace_id="trace", loop_id="main", cancellation=CancellationToken(),
                              trace_sink=SimpleNamespace(emit=emit))
    result = (await run_lanes(assembly.workflow, context, runtime, assembly.workflow.controller.nodes)).unwrap()
    assert result.control.kind == "continue"
    assert "budget" in result.control.reason
    assert all(n["status"] == "pending" and n["execution_hint"] == "reuse_preferred" for n in assembly.workflow.controller.nodes)


@pytest.mark.asyncio
async def test_parallel_branches_overlap_and_parent_integrates_once_in_original_context():
    class Provider(AnswerProvider):
        entered = 0
        active = 0
        maximum = 0

        def __init__(self):
            self.both = asyncio.Event()

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            names = {t["function"]["name"] for t in tools or ()}
            if "complete_branch" in names:
                assert names == {"complete_branch"}
                self.entered += 1
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.entered == 2:
                    self.both.set()
                await asyncio.wait_for(self.both.wait(), 2)
                self.active -= 1
                data = json.loads(next(m.content for m in messages if m.name == "branch_contract"))
                content = data["stable_inputs"]["dataset"]
                return ok(LlmResponse("", (LlmToolCall("result", "complete_branch", json.dumps({"outputs": {"findings": content}})),),
                                      usage=TokenUsage(100, 20, 120)))
            assert self.entered == 2 and self.active == 0
            handoffs = [m for m in messages if m.name == "dag_result"]
            assert len(handoffs) == 2
            assert "a self-contained" in str(handoffs) and "b self-contained" in str(handoffs)
            return await super().chat(messages, tools, cancellation, tool_choice)

    provider = Provider()
    result = (await run_generic_task(TaskRequest("Analyze a and b then integrate", task_spec=spec([
        branch("a"), branch("b"), dict(id="integrate", objective="Integrate findings", dependencies=["a", "b"])])),
        provider=provider, options=TaskRunOptions(max_steps=5))).unwrap()
    assert provider.maximum == 2
    assert result.run_result.traces[0].metadata["tokenUsage"]["total_tokens"] == 240
    workflow = thaw_json(result.run_result.context.state.scratch)["execution_plugins"]["workflow"]["state"]
    assert [n["status"] for n in workflow["workflow"]["nodes"]] == ["succeeded"] * 3
    assert len({lane["context_id"] for lane in workflow["lanes"].values()}) == 2
    assert workflow["assignments"]["integrate"]["context_session_id"] == workflow["main_context_id"]


@pytest.mark.asyncio
async def test_sequential_nodes_reuse_working_messages_without_new_agent():
    class Provider(AnswerProvider):
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            self.calls += 1
            assert "complete_branch" not in str(tools)
            if self.calls == 1:
                return ok(LlmResponse("unique observation retained across nodes", (LlmToolCall("done", "complete_node",
                    json.dumps({"evidence": "first node complete"})),)))
            assert "unique observation retained across nodes" in str(messages)
            return await super().chat(messages, tools, cancellation, tool_choice)

    result = (await run_generic_task(TaskRequest("Serial work", task_spec=spec([
        dict(id="a", objective="Investigate"), dict(id="b", objective="Use investigation", dependencies=["a"])])),
        provider=Provider(), options=TaskRunOptions(max_steps=4))).unwrap()
    workflow = thaw_json(result.run_result.context.state.scratch)["workflow"]
    assert not workflow["lanes"]
    assert len({a["context_session_id"] for a in workflow["assignments"].values()}) == 1


def test_checkpoint_isolated_and_default_workflow_cannot_restore_dag_state():
    assembly = TaskAssembly(TaskRequest("Task", task_spec=spec([branch("a"), branch("b")])))
    snapshot = deepcopy(assembly.snapshot())
    other = TaskAssembly(TaskRequest("Task", task_spec=spec([branch("a"), branch("b")])))
    other.restore(snapshot)
    assert other.workflow.main_context_id == assembly.workflow.main_context_id
    other.workflow.lanes["new"] = {}
    assert not assembly.workflow.lanes
    default = TaskAssembly(TaskRequest("Task", task_spec=dict(workflow=dict(plugin="dynamic"))))
    with pytest.raises(ValueError):
        default.restore(snapshot)


@pytest.mark.asyncio
async def test_persistent_branch_group_reuses_its_conversation_for_followup_nodes():
    class Provider(AnswerProvider):
        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            if "complete_branch" in str(tools):
                contracts = [json.loads(m.content) for m in messages if m.name == "branch_contract"]
                if contracts[-1]["objective"].endswith("a2"):
                    assert len(contracts) == 2
                    assert any(m.role == "tool" and m.name == "complete_branch" for m in messages)
                return ok(LlmResponse("", (LlmToolCall("done", "complete_branch", '{"outputs":{"findings":"checked"}}'),),
                                      usage=TokenUsage(30, 10, 40)))
            return await super().chat(messages, tools, cancellation, tool_choice)

    nodes = [branch("a", work_group_hint="research"), branch("b"),
             branch("a2", work_group_hint="research", dependencies=["a", "b"]),
             dict(id="integrate", objective="Integrate", dependencies=["a2"])]
    result = (await run_generic_task(TaskRequest("Analyze", task_spec=spec(nodes)), provider=Provider(),
                                     options=TaskRunOptions(max_steps=5))).unwrap()
    workflow = thaw_json(result.run_result.context.state.scratch)["execution_plugins"]["workflow"]["state"]
    assert workflow["lanes"]["research"]["completed_nodes"] == ["a", "a2"]
    assert workflow["assignments"]["a"]["context_session_id"] == workflow["assignments"]["a2"]["context_session_id"]


@pytest.mark.asyncio
async def test_durable_read_cohort_pauses_and_restores_without_replaying_completed_read(tmp_path):
    from dataclasses import replace

    from loom.llm.managed_step import ManagedStep
    from loom.runtime import create, create_runtime_registry, step
    from loom.runtime.checkpoints import decode, encode
    from loom.service.artifacts import ServiceArtifacts
    from loom.tasks.runner import make_task_context, make_task_loop

    (tmp_path / "source.txt").write_text("immutable input")
    nodes = [branch("a"), branch("b"), dict(id="integrate", objective="Integrate", dependencies=["a", "b"])]
    for node in nodes[:2]:
        node["executor_config"]["allowed_tools"] = ["read_file"]
    task_spec = spec(nodes)
    task_spec.update(session_environment=dict(plugin="session", resources=[dict(id="workspace", kind="directory", uri=str(tmp_path), access="read")]),
                     tools=dict(collections=["filesystem", "task_control"]))
    request = TaskRequest("Read independent stable inputs", task_spec=task_spec)

    class Bridge:
        def __init__(self):
            self.checkpoint = None
            self.counters = {}
            self.input_cursor = 0
            self.ops, self.finished = {}, []
            self.pause = True
            self.artifacts = ServiceArtifacts(tmp_path / "data")

        def boundary(self, cp):
            self.checkpoint = decode(encode(cp))
            self.counters = {k: cp[k] for k in ("llm_calls", "usage")}
            return dict(control={"kind": "paused", "reason": "test pause"} if self.pause and self.finished else None)

        def operation_start(self, call):
            return self.ops.get(call.id)

        def operation_finish(self, call, result):
            self.ops[call.id] = result
            self.finished.append(call.id)

        def rpc(self, kind, value):
            if kind == "publish_artifact":
                return self.artifacts.publish(value["value"], value["kind"])
            if kind == "read_artifact":
                return json.loads(self.artifacts.read(value["digest"]))
            return None

    class Provider(AnswerProvider):
        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            if "complete_branch" in str(tools):
                if not any(m.name == "read_file" for m in messages):
                    await asyncio.sleep(.01)
                    return ok(LlmResponse("", (LlmToolCall("read", "read_file", '{"path":"source.txt"}'),), usage=TokenUsage(20, 5, 25)))
                return ok(LlmResponse("", (LlmToolCall("end", "complete_branch", '{"outputs":{"findings":"immutable input"}}'),),
                                      usage=TokenUsage(20, 5, 25)))
            return await super().chat(messages, tools, cancellation, tool_choice)

    bridge, provider = Bridge(), Provider()

    async def execute():
        assembly = TaskAssembly(request, execution=bridge)
        assembly.configure_acceptance(provider)
        context = make_task_context(request, assembly=assembly).unwrap()
        if bridge.checkpoint:
            context = bridge.checkpoint["context"]
            assembly.restore(bridge.checkpoint["plugin_states"])
        managed = ManagedStep(provider, bridge, planning=assembly.workflow, assembly=assembly)
        definition = assembly.wrap_loop(replace(make_task_loop(request, provider, planning=assembly.workflow), step=managed))
        handle = create(definition, registry=create_runtime_registry(tools=assembly.handlers())).unwrap()
        result = (await step(handle, context)).unwrap()
        return result, assembly

    first, before = await execute()
    assert first.control.kind == "paused"
    saved_contexts = {k: v["context_id"] for k, v in before.workflow.lanes.items()}
    assert bridge.finished
    bridge.pause = False
    resumed, after = await execute()
    assert resumed.control.kind == "continue", resumed.control.reason
    assert {k: v["context_id"] for k, v in after.workflow.lanes.items()} == saved_contexts
    assert all(v["status"] == "succeeded" for v in after.workflow.lanes.values())
    assert len(bridge.finished) == len(set(bridge.finished)) == 2
    assert bridge.counters["llm_calls"] >= 4  # Includes branch calls and acceptance, not independent budgets.
