import json
from copy import deepcopy

import pytest
from textual.widgets import Markdown

from loom.client.tui import SessionTuiApp
from loom.tui.tui_app import EventFeedWidget
from tests.client.test_tui import FakeClient


def envelope(seq, kind, payload=None, *, run_id="run-1"):
    return {"session_id": "one", "seq": seq, "type": kind, "payload": payload or {}, "run_id": run_id}


@pytest.mark.asyncio
async def test_lifecycle_events_share_a_folded_group_with_a_working_toggle():
    app = SessionTuiApp(FakeClient(), "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        for event in (
            envelope(1, "run.started"),
            envelope(2, "step.started", {"step_number": 1}),
            envelope(3, "workflow.route.selected", {"route": "react"}),
            envelope(4, "decision.recorded", {"decision": {"reasoning": "Inspect the parser.", "action": {"kind": "tool"}}}),
            envelope(5, "step.completed", {"trace": {"outcome": "pass"}}),
        ):
            assert app.apply_session_event(event, app.subscription_generation)
        feed = app.query_one(EventFeedWidget)
        assert feed.event_count == 1
        group = feed.get_item(0)
        assert group.event.event_type == "process.group"
        assert group.event.data["count"] == 5
        assert not group.is_expanded
        await pilot.pause()
        assert "过程" in app.export_screenshot()
        await pilot.click(".process-toggle")
        assert group.is_expanded
        assert "step.started" in "\n".join(row.query_one(".compact-summary").render().plain for row in group.records)
        assert len(group.records) == 5 and feed.event_count == 6
        assert all(row.query_one(".compact-summary").region.height == 1 for row in group.records)
        await pilot.click(group.records[1].query_one(".compact-summary"))
        assert group.records[1].is_expanded
        assert "step_number" in group.records[1].query_one(".compact-details").render().plain
        assert app.apply_session_event(envelope(6, "step.started", {"step_number": 2}), app.subscription_generation)
        assert group.is_expanded
        await pilot.pause(0.4)
        await pilot.click(".process-toggle")
        assert not group.is_expanded


@pytest.mark.asyncio
async def test_thought_is_one_sentence_and_stream_updates_one_row():
    app = SessionTuiApp(FakeClient(), "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        for seq, delta in enumerate(("先检查索引更新路径。", "再检查测试。\n更多细节。"), 1):
            assert app.apply_session_event(envelope(seq, "llm.reasoning.delta", {"llm_call_id": "l", "delta": delta}), app.subscription_generation)
        feed = app.query_one(EventFeedWidget)
        assert feed.event_count == 1
        await pilot.pause()
        group = feed.get_item(0)
        assert not group.is_expanded
        assert "Thought:" in str(group.query_one(".process-toggle").label)
        await pilot.click(group.query_one(".process-toggle"))
        await pilot.pause()
        text = str(group.records[0].query_one(".compact-summary").render())
        assert "先检查索引更新路径。" in text
        assert "Thought:" in text and "🤔" not in text
        assert "再检查测试" not in text
        item = group.records[0]
        assert not item.is_expanded
        assert item.query_one(".compact-summary").region.height == 1
        await pilot.click(item.query_one(".compact-summary"))
        assert item.is_expanded
        assert item.query_one(".compact-details").render().plain == "先检查索引更新路径。再检查测试。\n更多细节。"
        assert app.apply_session_event(envelope(3, "llm.reasoning.delta", {"llm_call_id": "l", "delta": "\n最新思考。"}), app.subscription_generation)
        await pilot.pause()
        assert item.is_expanded and feed.event_count == 2
        assert "最新思考。" in item.query_one(".compact-details").render().plain
        await pilot.click(item.query_one(".compact-details"))
        assert item.is_expanded
        await pilot.click(item.query_one(".compact-summary"))
        assert not item.is_expanded
        await pilot.press("space")
        assert item.is_expanded


@pytest.mark.asyncio
async def test_tools_hide_results_and_final_markdown_lists_only_successful_writes():
    app = SessionTuiApp(FakeClient(), "one")
    report = "# Result\n\n**Fixed** the parser.\n\n| Check | Status |\n| --- | --- |\n| Tests | Passed |\n\n```python\nprint('ok')\n```"
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        seq = 0
        for call_id, tool, path, kind in (
            ("edit-1", "edit_file", "src/parser.py", "tool.completed"),
            ("edit-2", "edit_file", "src/parser.py", "tool.completed"),
            ("write", "write_file", "tests/test_parser.py", "tool.completed"),
            ("read", "read_file", "README.md", "tool.completed"),
            ("failed", "edit_file", "failed.py", "tool.failed"),
        ):
            seq += 1
            app.apply_session_event(
                envelope(seq, "tool.started", {"tool_id": tool, "tool_call_id": call_id, "input": {"path": path}}), app.subscription_generation
            )
            seq += 1
            app.apply_session_event(
                envelope(
                    seq,
                    kind,
                    {
                        "tool_id": tool,
                        "tool_call_id": call_id,
                        "output": {"value": {"path": path, "diff": "SECRET TOOL OUTPUT"}},
                        **({"error": {"message": "Permission denied"}} if kind == "tool.failed" else {}),
                    },
                ),
                app.subscription_generation,
            )
        feed = app.query_one(EventFeedWidget)
        assert feed.event_count == 1
        await pilot.pause()
        assert not any(item.is_expanded for item in feed._event_items)
        group = feed.get_item(0)
        assert "Permission denied" in str(group.query_one(".process-toggle").label)
        await pilot.click(group.query_one(".process-toggle"))
        await pilot.pause()
        assert len(group.records) == 5 and feed.event_count == 6
        assert "src/parser.py" in str(group.records[0].query_one(".compact-summary").render())
        assert "SECRET TOOL OUTPUT" not in "\n".join(str(widget.render()) for widget in feed.query("Static"))
        tool = group.records[0]
        assert "Tool call (edit_file)" in tool.query_one(".compact-summary").render().plain
        await pilot.click(tool.query_one(".compact-summary"))
        assert tool.is_expanded
        details = tool.query_one(".compact-details").render().plain
        assert "Input" in details and "Output" in details
        assert "src/parser.py" in details and "SECRET TOOL OUTPUT" in details
        await pilot.click(tool.query_one(".compact-summary"))
        assert not tool.is_expanded
        seq += 1
        app.apply_session_event(envelope(seq, "message.created", {"id": "answer", "role": "assistant", "content": report}), app.subscription_generation)
        await pilot.pause()
        result = feed.get_item(feed.event_count - 1)
        assert result.event.event_type == "message.created"
        assert result.is_expanded
        markdown = result.query_one(Markdown)
        assert markdown.query("MarkdownH1")
        assert markdown.query("MarkdownTable")
        assert markdown.query("MarkdownFence")
        files = str(result.query_one(".modified-files").render())
        assert files.count("src/parser.py") == 1
        assert "tests/test_parser.py" in files
        assert "README.md" not in files
        assert "failed.py" not in files
        assert "Permission denied" in str(group.records[4].query_one(".compact-summary").render())


@pytest.mark.asyncio
async def test_only_authoritative_session_answer_is_rendered_as_a_result():
    app = SessionTuiApp(FakeClient(), "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        for event in (
            envelope(1, "llm.completed", {"llm_call_id": "l", "response": {"content": "Intermediate reply", "usage": {"total_tokens": 12}}}),
            envelope(2, "tool.completed", {"tool_id": "finish", "tool_call_id": "finish", "output": {"value": {"report": "# Done", "completed": True}}}),
            envelope(3, "message.created", {"id": "answer", "role": "assistant", "content": "# Done"}),
        ):
            app.apply_session_event(event, app.subscription_generation)
        await pilot.pause()
        feed = app.query_one(EventFeedWidget)
        assert len(feed.query(Markdown)) == 1
        assert feed.get_event(feed.event_count - 1).data["content"] == "# Done"


@pytest.mark.asyncio
async def test_replay_and_resume_use_the_same_compact_presentation():
    history = [
        envelope(1, "run.started"),
        envelope(2, "tool.started", {"tool_id": "edit_file", "tool_call_id": "edit", "input": {"path": "src/parser.py"}}),
        envelope(3, "tool.completed", {"tool_id": "edit_file", "tool_call_id": "edit", "output": {"value": {"path": "src/parser.py"}}}),
        envelope(4, "message.created", {"id": "answer", "role": "assistant", "content": "## Fixed"}),
    ]

    class HistoryClient(FakeClient):
        def history(self, sid, **kwargs):
            return {"events": deepcopy(history), "next_before": None}

    client = HistoryClient()
    client.states["one"].update(event_cursor=4, run={"id": "run-1", "state": "completed"})
    app = SessionTuiApp(client, "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        feed = app.query_one(EventFeedWidget)
        before = [(event.event_type, event.data) for event in feed.get_events()]
        await app.select_session("one")
        await pilot.pause()
        assert [(event.event_type, event.data) for event in feed.get_events()] == before
        assert feed.query(Markdown)
        assert "src/parser.py" in str(feed.query_one(".modified-files").render())


@pytest.mark.asyncio
async def test_resume_collects_modified_files_from_earlier_pages_of_the_current_run():
    class PagedClient(FakeClient):
        def history(self, sid, *, before=None, **kwargs):
            if before is None:
                return {
                    "events": [envelope(201, "message.created", {"id": "answer", "role": "assistant", "content": "# Fixed"})],
                    "next_before": 201,
                }
            assert before == 201
            return {
                "events": [
                    envelope(1, "run.started"),
                    envelope(2, "tool.completed", {"tool_id": "edit_file", "tool_call_id": "edit", "output": {"value": {"path": "old.py"}}}),
                ],
                "next_before": None,
            }

    client = PagedClient()
    client.states["one"].update(event_cursor=201, run={"id": "run-1", "state": "completed"})
    app = SessionTuiApp(client, "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert "old.py" in str(app.query_one(".modified-files").render())


@pytest.mark.asyncio
async def test_tool_rejection_and_blockers_remain_visible():
    app = SessionTuiApp(FakeClient(), "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        for event in (
            envelope(
                1,
                "tool.completed",
                {"tool_id": "edit_file", "tool_call_id": "edit", "output": {"value": {"path": "no.py", "accepted": False, "message": "Rejected"}}},
            ),
            envelope(2, "input.requested", {"id": "q", "question": "Which module?", "kind": "clarification", "state": "pending"}),
            envelope(3, "run.failed", {"message": "Provider disconnected"}),
        ):
            app.apply_session_event(event, app.subscription_generation)
        await pilot.pause()
        summaries = "\n".join(str(widget.render()) for widget in app.query(".compact-summary"))
        assert "Rejected" in summaries
        assert "Which module?" in summaries
        assert "Provider disconnected" in summaries


@pytest.mark.asyncio
async def test_shell_failure_shows_status_without_tool_output():
    app = SessionTuiApp(FakeClient(), "one")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.apply_session_event(
            envelope(
                1, "tool.completed", {"tool_id": "shell_execute", "tool_call_id": "shell", "output": {"value": {"exit_code": 1, "stdout": "SECRET OUTPUT"}}}
            ),
            app.subscription_generation,
        )
        await pilot.pause()
        text = str(app.query_one(".compact-summary").render())
        assert "Tool call (shell_execute)" in text
        assert "exit 1" in text
        assert "SECRET OUTPUT" not in text


@pytest.mark.asyncio
async def test_json_tool_transport_never_appears_as_a_thought():
    app = SessionTuiApp(FakeClient(), "one")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for seq, delta in enumerate(("{", '"action": {"kind": "tool", "target": "read_file"}}'), 1):
            app.apply_session_event(envelope(seq, "llm.content.delta", {"llm_call_id": "l", "delta": delta}), app.subscription_generation)
        assert app.query_one(EventFeedWidget).event_count == 0


@pytest.mark.asyncio
async def test_modified_files_are_scoped_to_each_run():
    app = SessionTuiApp(FakeClient(), "one")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for event in (
            envelope(1, "tool.completed", {"tool_id": "edit_file", "tool_call_id": "edit", "output": {"value": {"path": "first.py"}}}),
            envelope(2, "message.created", {"id": "answer-1", "role": "assistant", "content": "First run"}),
            envelope(3, "run.started", run_id="run-2"),
            envelope(4, "tool.completed", {"tool_id": "write_file", "tool_call_id": "edit", "output": {"value": {"path": "second.py"}}}, run_id="run-2"),
            envelope(5, "message.created", {"id": "answer-2", "role": "assistant", "content": "Second run"}, run_id="run-2"),
        ):
            app.apply_session_event(event, app.subscription_generation)
        await pilot.pause()
        files = [str(widget.render()) for widget in app.query(".modified-files")]
        assert "first.py" in files[0] and "second.py" not in files[0]
        assert "second.py" in files[1] and "first.py" not in files[1]


@pytest.mark.asyncio
async def test_expanded_tool_updates_and_loads_full_artifact_in_place():
    command = "print('long command')\n" * 60 + "COMMAND END"
    output = "line of output\n" * 3000 + "OUTPUT END [bold]literal[/bold]"

    class ArtifactClient(FakeClient):
        loads = 0

        def artifact(self, sid, digest):
            self.loads += 1
            assert sid == "one" and digest == "full-output"
            return json.dumps({"type": "tool.completed", "tool_id": "shell_execute", "tool_call_id": "shell", "output": {"value": {"stdout": output}}}).encode()

    client = ArtifactClient()
    app = SessionTuiApp(client, "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.apply_session_event(
            envelope(1, "tool.started", {"tool_id": "shell_execute", "tool_call_id": "shell", "input": {"command": command}}),
            app.subscription_generation,
        )
        await pilot.pause()
        feed = app.query_one(EventFeedWidget)
        group = feed.get_item(0)
        assert not group.is_expanded
        assert "shell_execute" in str(group.query_one(".process-toggle").label)
        await pilot.click(group.query_one(".process-toggle"))
        await pilot.pause()
        item = group.records[0]
        assert not item.is_expanded and client.loads == 0
        await pilot.click(item.query_one(".compact-summary"))
        assert item.is_expanded
        assert "COMMAND END" in item.query_one(".compact-details").render().plain
        app.apply_session_event(
            envelope(2, "tool.completed", {"tool_id": "shell_execute", "tool_call_id": "shell", "artifact": {"sha256": "full-output"}}),
            app.subscription_generation,
        )
        await pilot.pause()
        assert item.is_expanded and feed.event_count == 2 and client.loads == 1
        details = item.query_one(".compact-details").render().plain
        assert "COMMAND END" in details and "OUTPUT END [bold]literal[/bold]" in details
        assert item.query_one(".compact-detail-scroll").max_scroll_y > 0
        await pilot.click(item.query_one(".compact-summary"))
        assert not item.is_expanded
        await pilot.click(item.query_one(".compact-summary"))
        assert item.is_expanded and client.loads == 1
        await pilot.click("#artifact")
        assert not item.is_expanded


@pytest.mark.asyncio
async def test_collapsed_process_tracks_current_event_and_keeps_result_outside():
    app = SessionTuiApp(FakeClient(), "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        feed = app.query_one(EventFeedWidget)
        for event in (
            envelope(1, "run.started"),
            envelope(2, "llm.requested", {"llm_call_id": "l"}),
            envelope(3, "llm.reasoning.delta", {"llm_call_id": "l", "delta": "Inspect the search results. Then check the parser."}),
        ):
            app.apply_session_event(event, app.subscription_generation)
        await pilot.pause()
        group = feed.get_item(0)
        assert feed.event_count == 1 and not group.is_expanded
        assert "Thought: Inspect the search results." in str(group.query_one(".process-toggle").label)
        app.apply_session_event(
            envelope(4, "tool.started", {"tool_id": "search_file", "tool_call_id": "search", "input": {"query": "parser"}}),
            app.subscription_generation,
        )
        app.apply_session_event(envelope(5, "operation.started", {"operation_id": "search"}), app.subscription_generation)
        await pilot.pause()
        assert "Tool call (search_file)" in str(group.query_one(".process-toggle").label)
        assert feed.event_count == 1 and not group.is_expanded
        await pilot.click(group.query_one(".process-toggle"))
        await pilot.pause()
        thought = next(row for row in group.records if row.kind == "thought")
        await pilot.click(thought.query_one(".compact-summary"))
        assert thought.is_expanded
        assert "Then check the parser." in thought.query_one(".compact-details").render().plain
        app.apply_session_event(envelope(6, "llm.requested", {"llm_call_id": "next"}), app.subscription_generation)
        await pilot.pause()
        assert "思考中" in str(group.query_one(".process-toggle").label)
        assert group.is_expanded and thought.is_expanded
        await pilot.click(group.query_one(".process-toggle"))
        assert not group.is_expanded and feed.get_selected_event().event_type == "process.group"
        app.apply_session_event(
            envelope(7, "message.created", {"id": "answer", "role": "assistant", "content": "# Done\n\nVerified."}),
            app.subscription_generation,
        )
        await pilot.pause()
        assert len(feed._event_items) == 2 and feed.event_count == 2
        result = feed.get_item(1)
        assert result.kind == "result" and result.is_expanded and result.group is None
        assert "已完成" in str(group.query_one(".process-toggle").label)


@pytest.mark.asyncio
async def test_nested_process_pruning_keeps_current_rows_and_message_receipts():
    app = SessionTuiApp(FakeClient(), "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        feed = app.query_one(EventFeedWidget)
        for event in (
            envelope(1, "run.started"),
            envelope(2, "message.created", {"id": "guidance", "role": "user", "content": "Check the parser", "command_id": "guidance"}),
            envelope(3, "step.started", {"step_number": 1}),
            envelope(4, "step.completed", {"trace": {"outcome": "pass"}}),
        ):
            app.apply_session_event(event, app.subscription_generation)
        app.apply_session_event({**envelope(5, "command.applied"), "command_id": "guidance"}, app.subscription_generation)
        assert app.message_indices["guidance"].event.data["state"] == "applied"
        for seq in range(6, 12):
            app.apply_session_event(envelope(seq, "step.started", {"step_number": seq}), app.subscription_generation)
        group = feed.get_item(0)
        await pilot.pause()
        await feed.prune_oldest(5)
        await pilot.pause()
        assert feed.record_count == 5 and group in feed._event_items
        assert [row.event.data["step_number"] for row in group.records] == [9, 10, 11]
        assert app.message_indices["guidance"].event.data["state"] == "applied"
        await pilot.click(group.query_one(".process-toggle"))
        await pilot.pause()
        assert feed.event_count == 5
