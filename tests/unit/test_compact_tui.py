import pytest
from textual.widgets import Markdown

from loom.tui.plugin import TuiPlugin
from loom.tui.tui_app import EventFeedWidget
from loom.tui.tui_collector import TuiEvent, TuiEventCollector


@pytest.mark.asyncio
async def test_default_loop_tui_exits_on_ctrl_c():
    app = TuiPlugin()._make_app(TuiEventCollector())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("ctrl+c")
        assert app._exit


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["llm", "finish"])
async def test_default_loop_tui_folds_lifecycle_and_renders_final_markdown(source):
    app = TuiPlugin()._make_app(TuiEventCollector())
    async with app.run_test(size=(100, 30)) as pilot:
        app._handle_event(TuiEvent(0, "run.started", {}, run_id="run"))
        app._handle_event(TuiEvent(0, "step.started", {}, run_id="run"))
        feed = app.query_one(EventFeedWidget)
        assert feed.event_count == 1
        assert feed.get_event(0).event_type == "process.group"
        assert not feed.get_item(0).is_expanded
        if source == "llm":
            event = TuiEvent(0, "llm.completed", {"response": {"content": "# Done\n\n**Tests passed.**", "usage": {"total_tokens": 7}}}, run_id="run")
        else:
            event = TuiEvent(
                0,
                "tool.completed",
                {"tool_id": "finish", "tool_call_id": "f", "output": {"value": {"report": "# Done\n\n**Tests passed.**", "completed": True}}},
                run_id="run",
            )
        app._handle_event(event)
        app._handle_event(TuiEvent(0, "_tui_done", {}))
        await pilot.pause()
        assert len(feed.query(Markdown)) == 1
        assert feed.get_item(feed.event_count - 1).is_expanded
        assert app._run_done


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["tool", "llm", "stream"])
async def test_one_shot_task_delivers_final_report_to_tui_before_quitting(monkeypatch, source):
    import json
    from pathlib import Path

    from loom.core import ok
    from loom.llm.api import LlmResponse, LlmStreamEvent
    from loom.tasks.assembly import load_task_spec
    from loom.tasks.request import TaskRequest, TaskRunOptions
    from loom.tasks.runner import run_generic_task
    from loom.tui.compact import CompactLoomTuiApp

    captured = []

    class RecordingApp:
        def __init__(self, collector):
            self.collector = collector

        def set_loop_info(self, **info):
            pass

        async def run_async(self):
            while True:
                event = await self.collector.queue.get()
                captured.append(event)
                if event.event_type == "_tui_done":
                    return

    monkeypatch.setattr(TuiPlugin, "_make_app", lambda self, collector: RecordingApp(collector))
    report = "# 事件驱动架构\n\n组件通过事件解耦，适用于异步业务流程。"

    class Provider:
        model = "test"

        async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            assert source != "tool", "A deterministic tool node must not call the model"
            return ok(LlmResponse(json.dumps({"reasoning": "Explain", "action": {"kind": "none", "input": {"report": report}}})))

        async def stream_chat(self, messages, tools=None, cancellation=None, tool_choice=None):
            response = (await self.chat(messages, tools, cancellation, tool_choice)).unwrap()
            yield LlmStreamEvent(kind="content.delta", content_delta=response.content)
            yield LlmStreamEvent(kind="completed", response=response)

    spec = load_task_spec(Path(__file__).resolve().parents[2] / "examples/task-specs/general.yaml")
    if source == "tool":
        spec["tools"]["collections"].append("document_outputs")
        spec["workflow"]["nodes"] = [
            {"id": "report", "objective": "Report", "executor_kind": "tool", "executor_config": {"tool_id": "create_report", "input": {"content": report}}}
        ]
    result = (
        await run_generic_task(TaskRequest("Explain", task_spec=spec), provider=Provider(), options=TaskRunOptions(tui=True, stream=source == "stream"))
    ).unwrap()
    assert result.output == report
    assert captured[-2].event_type == "run.result" and captured[-1].event_type == "_tui_done"
    app = CompactLoomTuiApp(TuiEventCollector())
    async with app.run_test(size=(100, 30)) as pilot:
        for event in captured:
            app._handle_event(event)
        await pilot.pause()
        feed = app.query_one(EventFeedWidget)
        assert len(feed.query(Markdown)) == 1
        item = feed.get_item(feed.event_count - 1)
        assert item.kind == "result" and item.is_expanded and item.group is None
        assert item.event.event_type == "run.result"
        assert item.event.data["content"] == result.output
        assert item.query_one(Markdown).query("MarkdownH1")
        assert app._run_done


@pytest.mark.asyncio
async def test_authoritative_one_shot_result_replaces_preview_and_expands_it():
    app = TuiPlugin()._make_app(TuiEventCollector())
    async with app.run_test(size=(100, 30)) as pilot:
        app._handle_event(TuiEvent(0, "llm.completed", {"response": {"content": "Preview"}}, run_id="run"))
        feed = app.query_one(EventFeedWidget)
        previous = feed.result_items["run"]
        previous.set_expanded(False)
        app._handle_event(TuiEvent(0, "run.result", {"content": "# Final report"}, run_id="run"))
        await pilot.pause()
        assert len(feed.query(Markdown)) == 1
        assert feed.result_items["run"] is previous
        assert previous.is_expanded and previous.event.data["content"] == "# Final report"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["run.result", "message.created"])
async def test_json_report_envelope_renders_markdown_blocks(kind):
    import json

    report = "# 事件驱动\n\n**异步**业务。\n\n| 场景 | 用途 |\n| --- | --- |\n| 订单 | 通知 |\n\n```python\nprint('event')\n```"
    content = json.dumps({"reasoning": "internal", "action": {"kind": "none", "input": {"report": report}}})
    app = TuiPlugin()._make_app(TuiEventCollector())
    async with app.run_test(size=(100, 36)) as pilot:
        app._handle_event(TuiEvent(0, kind, {"role": "assistant", "content": content}, run_id="run"))
        await pilot.pause()
        item = app.query_one(EventFeedWidget).result_items["run"]
        markdown = item.query_one(Markdown)
        assert markdown.query("MarkdownH1") and markdown.query("MarkdownTable") and markdown.query("MarkdownFence")
        assert markdown.query("MarkdownFence").first().lexer != "json"
        assert item.is_expanded


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_structured_json_result_is_indented_highlighted_and_updates_to_markdown(native):
    import json

    data = {"架构": "事件驱动", "场景": ["订单", "审计"], "active": True}
    app = TuiPlugin()._make_app(TuiEventCollector())
    async with app.run_test(size=(100, 30)) as pilot:
        app._handle_event(TuiEvent(0, "run.result", {"content": data if native else json.dumps(data)}, run_id="run"))
        await pilot.pause()
        feed = app.query_one(EventFeedWidget)
        item = feed.result_items["run"]
        fence = item.query_one("MarkdownFence")
        assert fence.lexer == "json"
        assert json.loads(fence.code) == data and '\n  "场景"' in fence.code
        app._handle_event(TuiEvent(0, "run.result", {"content": json.dumps({"content": "# 完成\n\n正文"})}, run_id="run"))
        await pilot.pause()
        assert feed.result_items["run"] is item
        assert item.query_one(Markdown).query("MarkdownH1")
        assert not item.query("MarkdownFence")
