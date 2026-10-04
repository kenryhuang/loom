import gzip
import io
import json
import zlib
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from loom.core import ok, thaw_json
from loom.llm.api import LlmResponse, LlmToolCall
from loom.tasks.assembly import TaskAssembly, load_task_spec
from loom.tasks.evidence import PAGE_CHARS
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import run_generic_task
from loom.tools.collections import research_collection


class AnswerProvider:
    model = "test"

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        return ok(LlmResponse(json.dumps({"reasoning": "memory", "action": {"kind": "none", "description": "Done", "input": {"report": "done"}}})))


SOURCE = "https://example.test/about/"
HTML = '<html><head><title>hidden</title><style>hidden</style></head><body><h1>Python 用途</h1><p>免费 &amp; 开源</p><script>hidden</script></body></html>'


def response(body, *, encoding="", content_type="text/html; charset=utf-8"):
    value = io.BytesIO(body)
    value.headers = Message()
    value.headers["Content-Type"] = content_type
    if encoding:
        value.headers["Content-Encoding"] = encoding
    value.url = SOURCE
    return value


def serve(monkeypatch, value):
    def urlopen(request, timeout):
        assert timeout == 20
        assert request.get_header("Accept-encoding") == "gzip, deflate"
        return value

    monkeypatch.setattr("loom.tools.collections.urlopen", urlopen)


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["identity", "gzip", "gzip_without_header", "deflate", "raw_deflate"])
async def test_fetch_decodes_compressed_html_and_extracts_text(monkeypatch, encoding):
    body = HTML.encode()
    if encoding.startswith("gzip"):
        body = gzip.compress(body)
    elif encoding == "deflate":
        body = zlib.compress(body)
    elif encoding == "raw_deflate":
        body = zlib.compress(body)[2:-4]
    header = "" if encoding == "gzip_without_header" else "deflate" if encoding == "raw_deflate" else encoding
    serve(monkeypatch, response(body, encoding=header))
    collection = research_collection(None)
    result = await collection.entrypoints["web_research/fetch_url"]({"url": SOURCE})
    assert result.ok
    value = thaw_json(result.value.value)
    assert value["content"] == HTML
    assert value["text"] == "Python 用途\n免费 & 开源"
    assert value["requested_url"] == value["url"] == SOURCE
    assert not value["truncated"]


@pytest.mark.asyncio
async def test_fetch_respects_response_charset(monkeypatch):
    serve(monkeypatch, response("语言与用途".encode("gb18030"), content_type="text/plain; charset=gb18030"))
    result = await research_collection(None).entrypoints["web_research/fetch_url"]({"url": SOURCE})
    assert thaw_json(result.unwrap().value)["content"] == "语言与用途"


@pytest.mark.asyncio
async def test_compressed_decoded_output_is_bounded(monkeypatch):
    serve(monkeypatch, response(gzip.compress(b"x" * 1000000), encoding="gzip", content_type="text/plain"))
    result = await research_collection(None).entrypoints["web_research/fetch_url"]({"url": SOURCE})
    value = thaw_json(result.unwrap().value)
    assert len(value["content"]) == 100000 and value["truncated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,encoding", [(b"bad gzip", "gzip"), (gzip.compress(b"content")[:-4], "gzip"), (b"unknown", "br")])
async def test_decode_failure_is_explicit(monkeypatch, body, encoding):
    serve(monkeypatch, response(body, encoding=encoding))
    result = await research_collection(None).entrypoints["web_research/fetch_url"]({"url": SOURCE})
    assert not result.ok and result.error.code == "HTTP_DECODE_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code,retryable", [
    (HTTPError(SOURCE, 401, "Unauthorized", {}, None), "HTTP_STATUS", False),
    (HTTPError(SOURCE, 503, "Unavailable", {}, None), "HTTP_STATUS", True),
    (TimeoutError("timed out"), "HTTP_TIMEOUT", True),
    (URLError(TimeoutError("timed out")), "HTTP_TIMEOUT", True),
    (URLError("DNS failed"), "HTTP_NETWORK_ERROR", True),
])
async def test_http_failures_are_known_tool_failures_not_execution_unknown(monkeypatch, error, code, retryable):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("loom.tools.collections.urlopen", fail)
    spec = load_task_spec("examples/task-specs/research.yaml")
    assembly = TaskAssembly(TaskRequest(f"读取 {SOURCE}", task_spec=spec))
    result = await assembly.handlers()["fetch_url"]({"url": SOURCE})
    assert not result.ok and result.error.code == code
    assert result.error.retryable is retryable
    assert result.error.metadata["url"] == SOURCE
    if isinstance(error, HTTPError):
        assert result.error.metadata["status_code"] == error.code
    assert next(iter(assembly.runtime.operations.values()))["status"] == "failed"
    assert assembly.completion_error() and not assembly.snapshot()["verified_sources"]
    await assembly.close()


def test_url_reading_cannot_use_general_tools_without_source_reader():
    with pytest.raises(ValueError, match="no source reader"):
        TaskAssembly(TaskRequest(f"读取 {SOURCE} 并总结", task_spec=load_task_spec("examples/task-specs/general.yaml")))


@pytest.mark.asyncio
async def test_manual_links_and_memory_cannot_complete_research(monkeypatch):
    spec = load_task_spec("examples/task-specs/research.yaml")
    assembly = TaskAssembly(TaskRequest(f"读取 {SOURCE} 并总结", task_spec=spec))
    await assembly.handlers()["create_report"]({"content": "模型记忆中的介绍", "sources": [SOURCE]})
    result = await assembly.handlers()["finish"]({"report": "模型记忆中的介绍"})
    assert thaw_json(result.unwrap().value)["accepted"] is False
    assert not assembly.snapshot()["verified_sources"]
    result = await run_generic_task(assembly.request, provider=AnswerProvider())
    assert not result.ok and result.error.code == "OUTPUT_CONTRACT_FAILED"
    await assembly.close()


@pytest.mark.asyncio
async def test_verified_retrieval_is_retained_and_finish_accepts_report(monkeypatch):
    serve(monkeypatch, response(gzip.compress(HTML.encode()), encoding="gzip"))
    spec = load_task_spec("examples/task-specs/research.yaml")
    request = TaskRequest(f"读取 {SOURCE} 并总结", task_spec=spec)
    assembly = TaskAssembly(request)
    fetched = thaw_json((await assembly.handlers()["fetch_url"]({"url": SOURCE})).unwrap().value)
    assert not assembly.completion_error()
    assert fetched["artifact"]["kind"] == "source"
    restored = TaskAssembly(request)
    restored.restore(assembly.snapshot())
    assert restored.snapshot()["verified_sources"][SOURCE] == fetched["artifact"]
    assert not restored.completion_error()
    assert thaw_json((await restored.handlers()["finish"]({"report": "Python 是免费开源的语言。"})).unwrap().value)["completed"]
    await assembly.close()
    await restored.close()


@pytest.mark.asyncio
async def test_source_journal_replay_restores_verified_proof_without_refetch(monkeypatch):
    serve(monkeypatch, response(HTML.encode()))

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
            return {"sha256": "source-digest", "kind": value["kind"]}

    request = TaskRequest(f"读取 {SOURCE}", task_spec=load_task_spec("examples/task-specs/research.yaml"))
    bridge = Bridge()
    first = TaskAssembly(request, execution=bridge, run_id="run")
    before = first.snapshot()
    options = {"metadata": {"tool_call_id": "source"}}
    original = (await first.handlers()["fetch_url"]({"url": SOURCE}, options)).unwrap()
    restored = TaskAssembly(request, execution=bridge, run_id="run")
    restored.restore(before)
    replayed = (await restored.handlers()["fetch_url"]({"url": SOURCE}, options)).unwrap()
    assert original == replayed and bridge.artifacts == 1
    assert not restored.completion_error()
    assert restored.snapshot()["verified_sources"][SOURCE]["sha256"] == "source-digest"
    await first.close()
    await restored.close()


@pytest.mark.asyncio
async def test_401_followed_by_model_memory_stays_incomplete(monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError(SOURCE, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("loom.tools.collections.urlopen", fail)

    class Provider(AnswerProvider):
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            self.calls += 1
            if self.calls == 1:
                return ok(LlmResponse("", (LlmToolCall("fetch", "fetch_url", json.dumps({"url": SOURCE})),)))
            return await super().chat(messages, tools, cancellation, tool_choice)

    request = TaskRequest(f"读取 {SOURCE}", task_spec=load_task_spec("examples/task-specs/research.yaml"))
    result = await run_generic_task(request, provider=Provider(), options=TaskRunOptions(max_steps=3))
    assert not result.ok and result.error.code == "OUTPUT_CONTRACT_FAILED"
    assert SOURCE in result.error.message


@pytest.mark.asyncio
async def test_final_complete_node_cannot_substitute_for_a_final_report():
    assembly = TaskAssembly(TaskRequest("解释概念", task_spec={"workflow": {"plugin": "dynamic"}}))
    workflow = assembly.workflow
    workflow.controller.nodes = workflow.controller.validate([{"id": "task", "objective": "解释概念", "status": "running"}])
    workflow._active = "task"
    result = await assembly.handlers()["complete_node"]({"evidence": "部分完成，无法读取"})
    assert not thaw_json(result.unwrap().value)["accepted"]
    assert workflow.controller.nodes[0]["status"] == "running" and workflow.unfinished()
    await assembly.close()


@pytest.mark.asyncio
async def test_large_node_evidence_keeps_full_artifact_and_a_bounded_note():
    from pathlib import Path

    assembly = TaskAssembly(TaskRequest("Explain a concept", task_spec={"workflow": {"plugin": "dynamic"}}))
    workflow = assembly.workflow
    workflow.controller.nodes = workflow.controller.validate([
        {"id": "prepare", "objective": "Prepare", "status": "running"},
        {"id": "report", "objective": "Report", "dependencies": ["prepare"]},
    ])
    workflow._active = "prepare"
    evidence = "Detailed evidence. " * 500
    result = await assembly.handlers()["complete_node"]({"evidence": evidence})
    assert thaw_json(result.unwrap().value)["accepted"]
    node = workflow.controller.nodes[0]
    assert len(node["provenance"]) < 2100
    artifact = node["artifact_refs"][0]
    assert artifact["kind"] == "workflow_evidence"
    assert json.loads(Path(artifact["local_path"]).read_text()) == {"evidence": evidence}
    await assembly.close()


@pytest.mark.asyncio
async def test_end_to_end_fetch_then_chinese_final_report(monkeypatch):
    serve(monkeypatch, response(gzip.compress(HTML.encode()), encoding="gzip"))

    class Provider(AnswerProvider):
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            self.calls += 1
            if self.calls == 1:
                return ok(LlmResponse("", (LlmToolCall("fetch", "fetch_url", json.dumps({"url": SOURCE})),)))
            report = f"Python 是免费开源的语言。\n\n来源：{SOURCE}"
            if self.calls == 2:
                fetched = next(json.loads(message.content) for message in messages if message.name == "fetch_url")
                assert "Python 用途" in fetched["text"]
                return ok(LlmResponse("", (LlmToolCall("finish", "finish", json.dumps({"report": report})),)))
            return ok(LlmResponse(json.dumps({"action": {"kind": "none", "description": "Final report", "input": {"report": report}}})))

    spec = load_task_spec("examples/task-specs/research.yaml")
    result = await run_generic_task(TaskRequest(f"读取 {SOURCE} 并总结", task_spec=spec), provider=Provider(), options=TaskRunOptions(max_steps=3))
    assert result.ok and result.value.output.startswith("Python 是免费开源")
    scratch = thaw_json(result.value.run_result.context.state.scratch)
    assert SOURCE in scratch["execution_plugins"]["verified_sources"]
    assert scratch["workflow"]["workflow"]["nodes"][0]["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_large_html_short_body_reaches_model_without_a_refetch_loop(monkeypatch, native):
    from pathlib import Path

    body = "Python 是免费开源的编程语言，适合快速开发。" * 180 + "正文结束标记"
    html = "<html><head><script>" + "irrelevant markup;" * 2600 + "</script></head><body><p>" + body + "</p></body></html>"
    serve(monkeypatch, response(gzip.compress(html.encode()), encoding="gzip"))

    class Provider(AnswerProvider):
        calls = 0

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            self.calls += 1
            assert self.calls <= 2, "The model should have the entire readable page after one fetch"
            if self.calls == 1:
                if native:
                    return ok(LlmResponse("", (LlmToolCall("fetch", "fetch_url", json.dumps({"url": SOURCE})),)))
                return ok(LlmResponse(json.dumps({"action": {"kind": "tool", "target": "fetch_url", "input": {"url": SOURCE}}})))
            if native:
                fetched = next(json.loads(message.content) for message in messages if message.name == "fetch_url")
            else:
                transcript = next(message.content for message in reversed(messages) if message.content.startswith("Tool execution transcript:\n"))
                fetched = json.loads(transcript.split("\n", 1)[1])[0]["result"]
            assert fetched["text"] == body
            assert not fetched["truncated"] and not fetched["page"]["has_more"]
            assert "content" not in fetched and "irrelevant markup" not in str(messages)
            original = json.loads(Path(fetched["artifact"]["local_path"]).read_text())
            assert original["content"] == html and original["text"] == body
            assert sum(len(message.content) for message in messages) < 24000
            return await super().chat(messages, tools, cancellation, tool_choice)

    provider = Provider()
    spec = load_task_spec("examples/task-specs/research.yaml")
    result = await run_generic_task(TaskRequest(f"读取 {SOURCE} 并总结", task_spec=spec), provider=provider)
    assert result.ok and result.value.output == "done" and provider.calls == 2


@pytest.mark.asyncio
async def test_long_source_pages_advance_without_refetch_or_losing_characters(monkeypatch):
    body = "用途与来源。" * 2300
    serve(monkeypatch, response(body.encode(), content_type="text/plain; charset=utf-8"))
    request = TaskRequest(f"读取 {SOURCE}", task_spec=load_task_spec("examples/task-specs/research.yaml"))
    assembly = TaskAssembly(request)
    handlers = assembly.handlers()
    value = thaw_json((await handlers["fetch_url"]({"url": SOURCE})).unwrap().value)
    assert not value["truncated"] and value["page"]["has_more"]
    assert len(value["content"]) == PAGE_CHARS
    parts = [value["content"]]
    request_page = value["read_more"]
    offsets = [0]
    while request_page:
        value = thaw_json((await handlers["read_artifact"](request_page)).unwrap().value)
        offsets.append(value["offset"])
        assert offsets[-1] > offsets[-2]
        assert len(value["content"]) <= PAGE_CHARS
        parts.append(value["content"])
        request_page = value["read_more"]
    assert "".join(parts) == body
    assert not value["has_more"] and value["next_offset"] is None
    assert len(assembly.runtime.operations) == 1
    await assembly.close()


@pytest.mark.asyncio
async def test_artifact_auto_reads_html_text_and_json_view_preserves_original(monkeypatch):
    serve(monkeypatch, response(HTML.encode()))
    assembly = TaskAssembly(TaskRequest(f"读取 {SOURCE}", task_spec=load_task_spec("examples/task-specs/research.yaml")))
    handlers = assembly.handlers()
    fetched = thaw_json((await handlers["fetch_url"]({"url": SOURCE})).unwrap().value)
    digest = fetched["artifact"]["sha256"]
    readable = thaw_json((await handlers["read_artifact"]({"digest": digest})).unwrap().value)
    assert readable["view"] == "text" and readable["content"] == fetched["text"]
    parts = []
    page = {"digest": digest, "view": "json", "limit": 100}
    while page:
        result = thaw_json((await handlers["read_artifact"](page)).unwrap().value)
        parts.append(result["content"])
        page = result["read_more"]
    assert json.loads("".join(parts))["content"] == HTML
    for args in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": PAGE_CHARS + 1}, {"view": "invalid"}):
        invalid = await handlers["read_artifact"]({"digest": digest, **args})
        assert not invalid.ok and invalid.error.code == "VALIDATION_FAILED"
    await assembly.close()


@pytest.mark.asyncio
async def test_conversation_archive_is_paged_without_creating_another_archive():
    assembly = TaskAssembly(TaskRequest("Explain", task_spec={"workflow": {"plugin": "dynamic"}}))
    archive = {"messages": [{"role": "tool", "content": "body " * 8000}], "previous_summary": "earlier"}
    ref = assembly.publish_artifact(archive, "context_window")
    initial_refs = list(assembly.context_manager.artifact_refs)
    page = {"digest": ref["sha256"]}
    parts = []
    while page:
        result = thaw_json((await assembly.handlers()["read_artifact"](page)).unwrap().value)
        assert result["view"] == "json" and len(result["content"]) <= PAGE_CHARS
        parts.append(result["content"])
        page = result["read_more"]
    assert json.loads("".join(parts)) == archive
    assert assembly.context_manager.artifact_refs == initial_refs
    await assembly.close()
