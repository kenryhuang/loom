import asyncio
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from loom.core import Observation, ToolRef, now_iso, ok
from loom.llm.api import LlmResponse, LlmToolCall
from loom.service.controller import LoomService
from loom.tasks.assembly import default_plugin_registry
from loom.tools.collections import ToolCollection
from tests.service.test_controller import command, wait_state


class GeneralProvider:
    model = "general-test"

    def __init__(self, state):
        self.objective = state["task"]["objective"]

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        text = "\n".join(message.content for message in messages)
        if "Current node: prepare" in text:
            return ok(LlmResponse(json.dumps({"action": {"kind": "none", "description": "Prepare", "input": {"report": "Prepared"}}})))
        if "Current node: second" in text and "two-nodes" in self.objective and not any(message.name == "request_input" for message in messages):
            return ok(LlmResponse("", (LlmToolCall("question", "request_input", '{"question":"Continue second node?"}'),)))
        if self.objective.startswith("source ") and "Current node: summary" not in text:
            url = self.objective.split(" ", 1)[1]
            if "tool/fetch_url:" not in text and not any(message.name == "fetch_url" for message in messages):
                return ok(LlmResponse("", (LlmToolCall("source", "fetch_url", json.dumps({"url": url})),)))
        return ok(LlmResponse(json.dumps({"reasoning": "verified", "action": {"kind": "none", "description": "Done", "input": {"report": "done"}}})))


def general_provider_factory(state):
    return GeneralProvider(state)


class WorkspaceReportProvider:
    model = "workspace-report-test"

    def __init__(self, state):
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        self.calls += 1
        if self.calls == 1:
            name, value = "create_report", {"content": "# AI 学习路线\n\n完整分析文档。", "path": "reports/ai-study.md", "sources": ["source:book"]}
        else:
            name, value = "finish", {"report": "分析已保存到 reports/ai-study.md。"}
        return ok(LlmResponse("", (LlmToolCall(f"report-{self.calls}", name, json.dumps(value)),)))


def workspace_report_provider_factory(state):
    return WorkspaceReportProvider(state)


def test_durable_research_session_saves_workspace_report_and_artifacts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = LoomService(tmp_path / "data", provider_factory=workspace_report_provider_factory).start()
    try:
        spec = {
            "session_environment": {
                "plugin": "session", "resources": [{"id": "workspace", "kind": "directory", "uri": str(workspace), "access": "read-write"}],
            },
            "tools": {"collections": ["document_outputs", "task_control"]},
            "workflow": {"plugin": "dynamic"},
            "outputs": [{"kind": "report", "require_evidence_refs": True}],
        }
        sid = service.create("research", {"objective": "AIstudy", "workspace": str(workspace), "task_spec": spec})["session_id"]
        state = wait_state(service, sid, "idle")
        assert state["run"]["state"] == "completed"
        assert (workspace / "reports/ai-study.md").read_text() == "# AI 学习路线\n\n完整分析文档。"
        ref = state["output_artifacts"][0]
        assert ref["workspace_paths"] == ["reports/ai-study.md"]
        final = json.loads(service.store.read_artifact(sid, ref["sha256"]))
        saved = final["documents"][0]
        original = json.loads(service.store.read_artifact(sid, saved["artifact"]["sha256"]))
        assert original["report"] == (workspace / saved["path"]).read_text()
        assert final["report"] == state["messages"][-1]["content"]
        with service.store.transaction() as db:
            operations = [json.loads(row[0]) for row in db.execute("SELECT body FROM operations WHERE session_id=?", (sid,))]
        operation = next(op for op in operations if op["call"]["name"] == "create_report")
        assert operation["effect_kind"] == "side_effecting" and operation["status"] == "completed"
    finally:
        service.close()


class PagedSourceProvider:
    model = "paged-source-test"

    def __init__(self, state):
        self.url = state["task"]["objective"].split(" ", 1)[1]

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        pages = [message for message in messages if message.name in {"fetch_url", "read_artifact"}]
        if not pages:
            return ok(LlmResponse("", (LlmToolCall("fetch", "fetch_url", json.dumps({"url": self.url})),)))
        value = json.loads(pages[-1].content)
        if value["read_more"]:
            return ok(LlmResponse("", (LlmToolCall(f"page-{value['read_more']['offset']}", "read_artifact", json.dumps(value["read_more"])),)))
        assert "source-tail-marker" in value["content"]
        return ok(LlmResponse(json.dumps({"action": {"kind": "none", "input": {"report": "Read all source pages"}}})))


def paged_source_provider_factory(state):
    return PagedSourceProvider(state)


def custom_registry_factory():
    registry = default_plugin_registry()

    async def increment(value, _options):
        marker = Path(value["path"])
        marker.write_text(marker.read_text() + "once\n" if marker.exists() else "once\n")
        return ok(Observation("increment", "increment", {"written": True}, now_iso()))

    ref = ToolRef(
        "increment", "Perform one recorded operation", input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    )
    registry.register("tools", "counter", lambda config, request: ToolCollection("counter", (ref,), {"increment": increment}, {"increment": "side_effecting"}))

    async def slow_increment(value, _options):
        Path(value["path"]).write_text("once\n")
        await asyncio.sleep(5)
        return ok(Observation("slow", "slow_increment", {"written": True}, now_iso()))

    slow = ToolRef(
        "slow_increment",
        "Timed operation with uncertain cancellation",
        timeout_ms=10,
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    registry.register(
        "tools",
        "slow_counter",
        lambda config, request: ToolCollection("slow_counter", (slow,), {"slow_increment": slow_increment}, {"slow_increment": "side_effecting"}),
    )
    return registry


def two_node_spec(first=None):
    return {
        "tools": {"collections": ["counter"] if first else ["task_control"]},
        "workflow": {
            "plugin": "dynamic",
            "nodes": [
                first or {"id": "first", "objective": "Complete first node"},
                {"id": "second", "objective": "Complete second node", "dependencies": ["first"]},
            ],
        },
    }


def test_workspace_free_dynamic_session_restores_pending_input(tmp_path):
    service = LoomService(tmp_path, provider_factory=general_provider_factory).start()
    try:
        sid = service.create("general", {"objective": "two-nodes", "task_spec": two_node_spec()})["session_id"]
        state = wait_state(service, sid, "awaiting_input")
        assert state["task"]["workspace"] is None
        run_id = state["run"]["id"]
        request_id = state["input_request"]["id"]
        assert state["workflow"]["workflow"]["nodes"][0]["status"] == "succeeded"
    finally:
        service.close()
    service = LoomService(tmp_path, provider_factory=general_provider_factory).start()
    try:
        command(service, sid, "answer_input", request_id=request_id, answer="Continue")
        state = wait_state(service, sid, "idle")
        assert state["run"]["id"] == run_id
        assert [node["status"] for node in state["workflow"]["workflow"]["nodes"]] == ["succeeded", "succeeded"]
        assert state["messages"][-1]["content"] == "done"
    finally:
        service.close()


def test_registered_deterministic_tool_is_not_replayed_after_restart(tmp_path):
    marker = tmp_path / "marker"
    first = {
        "id": "first",
        "objective": "Record operation",
        "executor_kind": "tool",
        "executor_config": {"tool_id": "increment", "input": {"path": str(marker)}},
    }
    service = LoomService(tmp_path / "service", provider_factory=general_provider_factory, plugin_registry_factory=custom_registry_factory).start()
    try:
        sid = service.create("custom", {"objective": "two-nodes", "task_spec": two_node_spec(first)})["session_id"]
        state = wait_state(service, sid, "awaiting_input")
        request_id = state["input_request"]["id"]
        assert marker.read_text() == "once\n"
        with service.store.transaction() as db:
            operations = [json.loads(row[0]) for row in db.execute("SELECT body FROM operations WHERE session_id=?", (sid,))]
        assert any(op["effect_kind"] == "side_effecting" and op["status"] == "completed" for op in operations)
    finally:
        service.close()
    service = LoomService(tmp_path / "service", provider_factory=general_provider_factory, plugin_registry_factory=custom_registry_factory).start()
    try:
        command(service, sid, "answer_input", request_id=request_id, answer="Continue")
        wait_state(service, sid, "idle")
        assert marker.read_text() == "once\n"
    finally:
        service.close()


def test_large_research_result_compacts_and_retains_session_artifact(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(gzip.compress(b"source facts " * 2500))

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = LoomService(tmp_path, provider_factory=general_provider_factory).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/source"
        spec = {
            "tools": {"collections": ["web_research", "task_control"]},
            "workflow": {
                "plugin": "dynamic",
                "nodes": [
                    {"id": "prepare", "objective": "Prepare the retrieval"},
                    {
                        "id": "fetch",
                        "objective": "Read the source",
                        "dependencies": ["prepare"],
                        "executor_kind": "tool",
                        "executor_config": {"tool_id": "fetch_url", "input": {"url": url}},
                    },
                    {"id": "summary", "objective": "Summarize the source", "dependencies": ["fetch"]},
                ],
            },
            # Three pinned node descriptions plus tool instructions need room to fit;
            # the 32 KB source still exceeds this window and must be compacted.
            "context": {"plugin": "research_context", "max_window_chars": 14000},
            "outputs": [{"kind": "report", "require_evidence_refs": True, "require_verified_sources": True}],
        }
        sid = service.create("source", {"objective": f"source {url}", "task_spec": spec})["session_id"]
        state = wait_state(service, sid, "idle")
        assert requests == ["/source"]
        events = service.events(sid, 0, limit=200)
        compacted = next(event for event in events if event["type"] == "context.compacted")
        ref = compacted["payload"]["artifact"]
        artifact = json.loads(service.store.read_artifact(sid, ref["sha256"]))
        assert "source facts" in json.dumps(artifact)
        assert state["messages"][-1]["content"] == "done"
        source_node = next(node for node in state["workflow"]["workflow"]["nodes"] if node["id"] == "fetch")
        assert len(source_node["provenance"]) < 2100 and source_node["artifact_refs"][0]["kind"] == "source"
        report = json.loads(service.store.read_artifact(sid, state["output_artifacts"][0]["sha256"]))
        assert report == {"report": "done", "sources": [url]}
    finally:
        service.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_service_reads_source_pages_to_the_end_without_refetching(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("资料正文。" * 2800 + "source-tail-marker").encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = LoomService(tmp_path, provider_factory=paged_source_provider_factory).start()
    try:
        from loom.tasks.assembly import load_task_spec

        spec = load_task_spec(Path(__file__).resolve().parents[2] / "examples/task-specs/research.yaml")
        sid = service.create("paged", {"objective": f"读取 http://127.0.0.1:{server.server_port}/source", "task_spec": spec})["session_id"]
        state = wait_state(service, sid, "idle")
        assert requests == ["/source"]
        assert state["messages"][-1]["content"] == "Read all source pages"
        calls = [event["payload"] for event in service.events(sid, 0, limit=200) if event["type"] == "tool.started"]
        assert [call["input"]["offset"] for call in calls if call["tool_id"] == "read_artifact"] == [6000, 12000]
    finally:
        service.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_uncertain_runtime_timeout_requires_reconciliation_before_resume(tmp_path):
    marker = tmp_path / "marker"
    spec = {
        "tools": {"collections": ["slow_counter"]},
        "workflow": {
            "plugin": "dynamic",
            "nodes": [
                {
                    "id": "write",
                    "objective": "Write once",
                    "executor_kind": "tool",
                    "executor_config": {"tool_id": "slow_increment", "input": {"path": str(marker)}},
                },
                {"id": "answer", "objective": "Answer", "dependencies": ["write"]},
            ],
        },
    }
    service = LoomService(tmp_path / "service", provider_factory=general_provider_factory, plugin_registry_factory=custom_registry_factory).start()
    try:
        sid = service.create("uncertain", {"objective": "Task", "task_spec": spec})["session_id"]
        state = wait_state(service, sid, "recovering")
        assert state["input_request"]["kind"] == "recovery"
        assert marker.read_text() == "once\n"
        with service.store.transaction() as db:
            op = json.loads(db.execute("SELECT body FROM operations WHERE session_id=?", (sid,)).fetchone()[0])
        assert op["status"] == "started"
        command(
            service,
            sid,
            "answer_input",
            request_id=state["input_request"]["id"],
            answer=json.dumps({"resolution": "completed", "evidence": "Verified marker contains once"}),
        )
        wait_state(service, sid, "idle")
        assert marker.read_text() == "once\n"
    finally:
        service.close()
