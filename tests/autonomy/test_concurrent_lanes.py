import asyncio
import json

import pytest

from loom.core import ok, thaw_json
from loom.llm.api import LlmResponse, LlmToolCall, TokenUsage
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import run_generic_task
from tests.tasks.test_adaptive_dag import branch
from tests.tasks.test_plugin_runtime import AnswerProvider


@pytest.mark.asyncio
async def test_pipeline_starts_dependent_main_before_slow_peer_finishes():
    class Provider(AnswerProvider):
        def __init__(self):
            self.child = asyncio.Event()
            self.order = []

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            if "complete_branch" not in {t["function"]["name"] for t in tools or []}:
                return await super().chat(messages, tools, cancellation, tool_choice)
            seed = json.loads(next(m.content for m in reversed(messages) if m.name == "branch_contract"))
            name = seed["stable_inputs"]["dataset"].split()[0]
            self.order.append("start:" + name)
            if name == "b":
                await asyncio.wait_for(self.child.wait(), 3)
            if name == "c":
                assert "done:b" not in self.order
                self.child.set()
            self.order.append("done:" + name)
            return ok(LlmResponse("", (LlmToolCall("done", "complete_branch", json.dumps({"outputs": {"findings": name}})),), usage=TokenUsage(10, 10, 20)))

    provider = Provider()
    child = branch("c", dependencies=["a"], inputs=[dict(from_node="a", output_name="findings")])
    child["execution_hint"] = "main_candidate"
    spec = dict(
        workflow=dict(
            plugin="adaptive_dag",
            parallel=dict(mode="pipeline"),
            nodes=[branch("a"), branch("b"), child, dict(id="integrate", objective="Verify final output", dependencies=["b", "c"])],
        )
    )
    result = (
        await run_generic_task(TaskRequest("Analyze independently then integrate", task_spec=spec), provider=provider, options=TaskRunOptions(max_steps=5))
    ).unwrap()
    assert provider.order.index("start:c") < provider.order.index("done:b")
    workflow = thaw_json(result.run_result.context.state.scratch)["execution_plugins"]["workflow"]["state"]
    assert all(n["status"] == "succeeded" for n in workflow["workflow"]["nodes"])
    assert result.run_result.traces[0].metadata["tokenUsage"]["total_tokens"] == 60


@pytest.mark.asyncio
async def test_isolated_file_branch_integrates_only_after_peer_returns(tmp_path):
    (tmp_path / "a.txt").write_text("base")

    class Provider(AnswerProvider):
        def __init__(self):
            self.written = asyncio.Event()

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            names = {t["function"]["name"] for t in tools or []}
            if "complete_branch" not in names:
                assert (tmp_path / "a.txt").read_text() == "changed"
                return await super().chat(messages, tools, cancellation, tool_choice)
            seed = json.loads(next(m.content for m in reversed(messages) if m.name == "branch_contract"))
            name = seed["stable_inputs"]["dataset"].split()[0]
            if name == "writer" and not any(m.role == "tool" for m in messages):
                return ok(LlmResponse("", (LlmToolCall("write", "write_file", '{"path":"a.txt","content":"changed"}'),), usage=TokenUsage(10, 10, 20)))
            if name == "writer":
                self.written.set()
            else:
                await asyncio.wait_for(self.written.wait(), 3)
                assert (tmp_path / "a.txt").read_text() == "base"
            return ok(LlmResponse("", (LlmToolCall("done", "complete_branch", json.dumps({"outputs": {"findings": name}})),), usage=TokenUsage(10, 10, 20)))

    writer = branch("writer", isolated_write=True)
    writer["executor_config"]["allowed_tools"] = ["read_file", "write_file"]
    spec = dict(
        workflow=dict(
            plugin="adaptive_dag",
            parallel=dict(mode="pipeline", isolated_writes=True),
            nodes=[writer, branch("reader"), dict(id="integrate", objective="Verify output", dependencies=["writer", "reader"])],
        )
    )
    spec["session_environment"] = dict(plugin="session", resources=[dict(id="workspace", kind="directory", uri=str(tmp_path), access="read-write")])
    result = (
        await run_generic_task(TaskRequest("Write then verify", workspace=tmp_path, task_spec=spec), provider=Provider(), options=TaskRunOptions(max_steps=5))
    ).unwrap()
    assert (tmp_path / "a.txt").read_text() == "changed"
    workflow = thaw_json(result.run_result.context.state.scratch)["execution_plugins"]["workflow"]["state"]
    assert all(n["status"] == "succeeded" for n in workflow["workflow"]["nodes"])


@pytest.mark.asyncio
async def test_pipeline_checkpoint_pause_and_restore_preserves_tool_receipts(tmp_path, monkeypatch):
    from tests.tasks import test_adaptive_dag as existing

    original = existing.spec

    def pipeline(nodes):
        value = original(nodes)
        value["workflow"]["parallel"] = dict(mode="pipeline")
        return value

    monkeypatch.setattr(existing, "spec", pipeline)
    await existing.test_durable_read_cohort_pauses_and_restores_without_replaying_completed_read(tmp_path)


@pytest.mark.asyncio
async def test_pipeline_budget_preserves_main_acceptance_reserve(monkeypatch):
    from tests.tasks import test_adaptive_dag as existing

    original = existing.spec

    def pipeline(nodes):
        value = original(nodes)
        value["workflow"]["parallel"] = dict(mode="pipeline")
        return value

    monkeypatch.setattr(existing, "spec", pipeline)
    await existing.test_shared_budget_declines_new_agents_and_returns_work_to_main()


@pytest.mark.asyncio
async def test_barrier_and_pipeline_preserve_acceptance_handoffs_and_total_cost():
    results = {}
    for mode in ("barrier", "pipeline"):

        class Provider(AnswerProvider):
            def __init__(self):
                self.order = []

            async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
                if "complete_branch" not in {t["function"]["name"] for t in tools or []}:
                    return await super().chat(messages, tools, cancellation, tool_choice)
                seed = json.loads(next(m.content for m in reversed(messages) if m.name == "branch_contract"))
                name = seed["stable_inputs"]["dataset"].split()[0]
                self.order.append("start:" + name)
                if name == "b":
                    await asyncio.sleep(0.05)
                self.order.append("done:" + name)
                return ok(LlmResponse("", (LlmToolCall("done", "complete_branch", json.dumps({"outputs": {"findings": name}})),), usage=TokenUsage(10, 10, 20)))

        provider = Provider()
        nodes = [branch("a"), branch("b"), branch("c", dependencies=["a"]), dict(id="integrate", objective="Verify findings", dependencies=["b", "c"])]
        nodes[0]["work_group_hint"] = nodes[2]["work_group_hint"] = "research"
        spec = dict(workflow=dict(plugin="adaptive_dag", parallel=dict(mode=mode), nodes=nodes))
        result = (await run_generic_task(TaskRequest("Compare a b c", task_spec=spec), provider=provider, options=TaskRunOptions(max_steps=6))).unwrap()
        state = thaw_json(result.run_result.context.state.scratch)["execution_plugins"]["workflow"]["state"]
        # Barrier does not fork a brand new single context; let c use its retained
        # a-group in this representative comparison so both execute three lanes.
        receipts = [r["host_receipt"] for r in state["results"].values() if r.get("host_receipt")]
        results[mode] = dict(
            states=[n["status"] for n in state["workflow"]["nodes"]],
            order=provider.order,
            handoffs=len(receipts),
            calls=sum(r["model_calls"] for r in receipts),
        )
    assert results["pipeline"]["states"] == results["barrier"]["states"] == ["succeeded"] * 4
    assert results["pipeline"]["order"].index("start:c") < results["pipeline"]["order"].index("done:b")
    assert results["barrier"]["order"].index("start:c") > results["barrier"]["order"].index("done:b")
    assert results["barrier"]["handoffs"] == results["pipeline"]["handoffs"] == 3
    assert results["barrier"]["calls"] == results["pipeline"]["calls"] == 3  # 60 reported fixture tokens in either mode.
