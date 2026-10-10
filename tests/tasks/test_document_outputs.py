import json
from pathlib import Path

import pytest

from loom.core import ok, thaw_json
from loom.llm.api import LlmResponse, LlmToolCall
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import make_task_context, run_generic_task
from tests.acceptance_fakes import FixtureVerifier


def report_request(workspace=None, *, access="read-write"):
    resources = [] if workspace is None else [{"id": "reports", "kind": "directory", "uri": str(workspace), "access": access}]
    return TaskRequest(
        "Write the analysis",
        task_spec={
            "session_environment": {"plugin": "session", "resources": resources},
            "tools": {"collections": ["document_outputs", "task_control"]},
            "workflow": {"plugin": "dynamic"},
            "outputs": [{"kind": "report", "require_evidence_refs": True}],
        },
    )


@pytest.mark.asyncio
async def test_report_writes_utf8_document_and_keeps_full_artifact(tmp_path):
    assembly = TaskAssembly(report_request(tmp_path))
    try:
        content = "# AI 学习路线\n\n先学习硬件，再分析推理性能。\n"
        output = thaw_json((await assembly.handlers()["create_report"]({
            "content": content, "path": "reports/ai-study.md", "sources": ["source:book"],
        })).unwrap().value)
        assert (tmp_path / output["path"]).read_text() == content
        assert output["bytes_written"] == len(content.encode("utf-8"))
        assert output["artifact"]["kind"] == "report"
        assert assembly.read_artifact(output["artifact"]["sha256"]) == {
            "report": content, "sources": ["source:book"], "path": "reports/ai-study.md", "bytes_written": len(content.encode("utf-8")),
        }
        binding = assembly.bindings["create_report"]
        assert binding.effect_kind == "side_effecting" and binding.resource_refs == ("reports",)
        assert "path" in binding.ref.input_schema["required"]
        assert not assembly.output_error()
        updated = "# Updated analysis\n"
        assert (await assembly.handlers()["create_report"]({"content": updated, "path": output["path"]})).ok
        assert (tmp_path / output["path"]).read_text() == updated
        assert assembly.read_artifact(output["artifact"]["sha256"])["report"] == content
    finally:
        await assembly.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("readonly", [False, True])
async def test_reports_without_writable_workspace_keep_artifact_only(tmp_path, monkeypatch, readonly):
    monkeypatch.chdir(tmp_path)
    assembly = TaskAssembly(report_request(tmp_path if readonly else None, access="read"))
    try:
        output = thaw_json((await assembly.handlers()["create_report"]({"content": "# Analysis", "sources": ["source:book"]})).unwrap().value)
        assert assembly.read_artifact(output["artifact"]["sha256"]) == {"report": "# Analysis", "sources": ["source:book"]}
        assert "path" not in output
        assert assembly.bindings["create_report"].effect_kind == "read_only"
        assert not assembly.bindings["create_report"].resource_refs
        assert not assembly.workspace_report_required and not assembly.output_error()
        failed = await assembly.handlers()["create_report"]({"content": "# Analysis", "path": "analysis.md"})
        assert not failed.ok and "writable workspace" in failed.error.message
        assert not (tmp_path / "analysis.md").exists()
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_reports_reject_missing_path_escape_and_symlinks(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("keep this")
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    assembly = TaskAssembly(report_request(root))
    try:
        for path in (None, "", "../outside.md", str(outside), "link/outside.md"):
            value = {"content": "replace"}
            if path is not None:
                value["path"] = path
            failed = await assembly.handlers()["create_report"](value)
            assert not failed.ok and failed.error.code == "VALIDATION_FAILED"
            assert outside.read_text() == "keep this"
        assert not assembly.snapshot().get("workspace_reports")
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_write_failure_does_not_publish_report_or_satisfy_completion(tmp_path):
    (tmp_path / "reports").write_text("existing file")
    assembly = TaskAssembly(report_request(tmp_path))
    try:
        failed = await assembly.handlers()["create_report"]({"content": "analysis", "path": "reports/analysis.md"})
        assert not failed.ok and failed.error.code == "TOOL_FAILED"
        assert assembly._local_artifacts is None
        assert assembly.output_error()
        empty = await assembly.handlers()["create_report"]({"content": " ", "path": "empty.md"})
        assert not empty.ok and empty.error.code == "VALIDATION_FAILED"
        assert not (tmp_path / "empty.md").exists()
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_saved_report_contract_survives_restore_and_checks_document(tmp_path):
    request = report_request(tmp_path)
    assembly = TaskAssembly(request)
    restored = TaskAssembly(request)
    try:
        # A cited reply alone is insufficient when the task promises a workspace report.
        assembly._evidence = ["source:book"]
        rejected = thaw_json((await assembly.handlers()["finish"]({"report": "answer"})).unwrap().value)
        assert rejected["accepted"] is False and "create_report" in rejected["reason"]
        assert (await assembly.handlers()["create_report"]({"content": "analysis", "path": "analysis.md"})).ok
        restored.restore(assembly.snapshot())
        assert not restored.output_error()
        document = tmp_path / "analysis.md"
        document.write_text("changed")
        assert "changed" in restored.output_error()
        document.unlink()
        assert "unavailable" in restored.output_error()
        assert (await restored.handlers()["create_report"]({"content": "updated", "path": "analysis.md"})).ok
        assert not restored.output_error()
    finally:
        await assembly.close()
        await restored.close()


@pytest.mark.asyncio
async def test_workspace_report_journal_replay_does_not_rewrite_document(tmp_path):
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

    request = report_request(tmp_path)
    bridge = Bridge()
    first = TaskAssembly(request, execution=bridge, run_id="run")
    restored = TaskAssembly(request, execution=bridge, run_id="run")
    try:
        before = first.snapshot()
        value = {"content": "facts", "path": "analysis.md", "sources": ["source:book"]}
        options = {"metadata": {"tool_call_id": "report"}}
        original = (await first.handlers()["create_report"](value, options)).unwrap()
        restored.restore(before)
        (tmp_path / "analysis.md").write_text("external edit")
        replayed = (await restored.handlers()["create_report"](value, options)).unwrap()
        assert replayed == original and bridge.artifacts == 1
        assert (tmp_path / "analysis.md").read_text() == "external edit"
        assert restored.snapshot()["workspace_reports"]["analysis.md"]["artifact"]["sha256"] == "digest"
        assert "changed" in restored.output_error()
    finally:
        await first.close()
        await restored.close()


@pytest.mark.asyncio
async def test_research_finishes_with_saved_document_and_both_artifact_references(tmp_path):
    content = "# Complete analysis\n\nEvidence-backed details."
    answer = "Analysis saved in reports/analysis.md."

    class Provider:
        verification_provider = FixtureVerifier()
        model = "test"
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            self.calls += 1
            if self.calls == 1:
                name, value = "create_report", {"content": content, "path": "reports/analysis.md", "sources": ["source:book"]}
            else:
                name, value = "finish", {"report": answer}
            return ok(LlmResponse("", (LlmToolCall(f"call-{self.calls}", name, json.dumps(value)),)))

    request = report_request(tmp_path)
    context = make_task_context(request).unwrap()
    assert any(c.id == "workspace-report" for c in context.identity.constraints)
    result = (await run_generic_task(request, provider=Provider(), options=TaskRunOptions(max_steps=4))).unwrap()
    assert result.output == answer
    assert (tmp_path / "reports/analysis.md").read_text() == content
    scratch = thaw_json(result.run_result.context.state.scratch)
    ref = scratch["output_artifacts"][0]
    assert ref["workspace_paths"] == ["reports/analysis.md"]
    final = json.loads(Path(ref["local_path"]).read_text())
    assert final["report"] == answer
    assert final["sources"] == ["source:book"]
    document = final["documents"][0]
    assert document["path"] == "reports/analysis.md"
    assert json.loads(Path(document["artifact"]["local_path"]).read_text())["report"] == content


@pytest.mark.asyncio
async def test_direct_answer_cannot_bypass_workspace_document_contract(tmp_path):
    class Provider:
        verification_provider = FixtureVerifier()
        model = "test"

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            return ok(LlmResponse(json.dumps({"action": {"kind": "none", "input": {"report": "No saved document"}}})))

    request = report_request(tmp_path)
    request = TaskRequest(request.objective, task_spec={**request.task_spec, "outputs": [{"kind": "report"}]})
    result = await run_generic_task(request, provider=Provider())
    assert result.ok and result.value.run_result.metrics.outcome == "paused"
    assert "create_report" in result.value.run_result.context.state.scratch["acceptance"]["reason"]
