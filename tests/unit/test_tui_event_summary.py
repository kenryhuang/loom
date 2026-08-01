from __future__ import annotations

import pytest

from loom.tui.event_summary import EventSummary, is_redundant_observation, llm_response_text, summarize_event
from loom.tui.tui_collector import TuiEvent


def _event(event_type: str, data: dict, *, error: str | None = None) -> TuiEvent:
    return TuiEvent(0, event_type, {"type": event_type, **data}, error=error)


def _tool_event(tool_id: str, *, input_value=None, output=None, event_type: str = "tool.completed", error: str | None = None) -> TuiEvent:
    data = {"tool_id": tool_id}
    if input_value is not None:
        data["input"] = input_value
    if output is not None:
        data["output"] = output
    return _event(event_type, data, error=error)


def _observation(source: str, value) -> dict:
    return {"value": {"source": source, "value": value}}


def test_summarize_decision_uses_action_description():
    event = _event(
        "decision.recorded",
        {
            "decision": {
                "action": {"description": "List project structure", "target": "run_command"},
                "reasoning": "The first plan item requires discovery.",
                "confidence": 0.95,
            }
        },
    )

    assert summarize_event(event) == EventSummary("Decision", "List project structure", "text")


def test_summarize_action_uses_description_and_outcome():
    event = _event(
        "action.completed",
        {"action": {"description": "Inspect WAL implementation", "target": "read_file"}, "outcome": "pass"},
    )

    assert summarize_event(event) == EventSummary("Action completed", "Inspect WAL implementation · pass", "green")


def test_guard_observation_uses_code_and_message():
    event = _event(
        "observation.recorded",
        {
            "observation": {
                "source": "planning.guard",
                "value": {"accepted": False, "code": "PLAN_UPDATE_REQUIRED", "message": "Update the checklist"},
            }
        },
    )

    assert summarize_event(event) == EventSummary("Plan blocked", "PLAN_UPDATE_REQUIRED · Update the checklist", "red")
    assert is_redundant_observation(event) is False


@pytest.mark.parametrize(
    "source",
    ["llm", "read_file", "edit_file", "write_file", "shell_execute", "finish", "submit_plan", "update_plan"],
)
def test_success_observations_already_represented_elsewhere_are_redundant(source: str):
    event = _event("observation.recorded", {"observation": {"source": source, "value": {"accepted": True}}})

    assert is_redundant_observation(event) is True


def test_shell_completion_summarizes_test_progress():
    event = _tool_event(
        "shell_execute",
        input_value={"command": ["pytest", "-q"]},
        output=_observation(
            "shell_execute",
            {
                "exit_code": 0,
                "stdout": "43 passed, 8 skipped in 3.41s\n",
                "stderr": "",
                "duration_ms": 3410,
                "timed_out": False,
            },
        ),
    )

    assert summarize_event(event) == EventSummary("Bash", "Passed · exit 0 · 43 passed, 8 skipped · 3.41s", "green")


def test_shell_nonzero_completion_uses_stderr_signal():
    event = _tool_event(
        "shell_execute",
        output=_observation(
            "shell_execute",
            {"exit_code": 2, "stdout": "", "stderr": "error: invalid option\nusage: cmd", "duration_ms": 120, "timed_out": False},
        ),
    )

    assert summarize_event(event) == EventSummary("Bash", "Failed · exit 2 · error: invalid option · 0.12s", "red")


def test_shell_timeout_is_visible_as_failure():
    event = _tool_event(
        "shell_execute",
        output=_observation(
            "shell_execute",
            {"exit_code": 124, "stdout": "", "stderr": "Command timed out after 120s", "duration_ms": 120000, "timed_out": True},
        ),
    )

    summary = summarize_event(event)

    assert summary is not None
    assert summary.title == "Bash"
    assert summary.color_role == "red"
    assert summary.description == "Timed out · exit 124 · Command timed out after 120s · 2m"


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            _tool_event(
                "read_file",
                input_value={"path": "yakdb_core/src/wal.rs"},
                output=_observation(
                    "read_file",
                    {"path": "yakdb_core/src/wal.rs", "content": "one\ntwo\nthree\n", "bytes_read": 8192, "truncated": False},
                ),
            ),
            EventSummary("Read", "yakdb_core/src/wal.rs · 3 lines · 8 KB", "green"),
        ),
        (
            _tool_event(
                "edit_file",
                output=_observation(
                    "edit_file",
                    {
                        "path": "yakdb_core/src/wal.rs",
                        "replacements": 1,
                        "bytes_written": 8294,
                        "first_changed_line": 119,
                    },
                ),
            ),
            EventSummary("Edit", "yakdb_core/src/wal.rs · 1 replacement · first change line 119 · 8.1 KB", "green"),
        ),
        (
            _tool_event(
                "write_file",
                output=_observation("write_file", {"path": "docs/reliability-report.md", "bytes_written": 6350}),
            ),
            EventSummary("Write", "docs/reliability-report.md · 6.2 KB", "green"),
        ),
        (
            _tool_event("finish", output=_observation("finish", {"completed": True, "report": "done"})),
            EventSummary("Finish", "Completed", "green"),
        ),
        (
            _tool_event("search", input_value={"query": "loom"}, output={"value": {"summary": "found docs"}}),
            EventSummary("Search", "found docs", "green"),
        ),
    ],
)
def test_completed_tools_use_structured_results(event: TuiEvent, expected: EventSummary):
    assert summarize_event(event) == expected


def test_running_tool_shows_target_without_serialized_arguments():
    event = _tool_event(
        "edit_file",
        event_type="tool.started",
        input_value={"path": "yakdb_core/src/wal.rs", "edits": [{"old_text": "large", "new_text": "payload"}]},
    )

    assert summarize_event(event) == EventSummary("Edit", "yakdb_core/src/wal.rs", "orange")


def test_runtime_tool_failure_uses_code_and_message():
    event = _tool_event(
        "edit_file",
        event_type="tool.failed",
        input_value={"path": "wal.rs"},
        output={"code": "VALIDATION_FAILED", "message": "old_text was not found"},
        error="old_text was not found",
    )

    assert summarize_event(event) == EventSummary("Edit failed", "VALIDATION_FAILED · old_text was not found", "red")


def test_rejected_plan_tool_uses_guard_result():
    event = _tool_event(
        "update_plan",
        output={"value": {"accepted": False, "code": "PLAN_PHASE_INVALID", "message": "No active plan"}},
    )

    assert summarize_event(event) == EventSummary("Update Plan blocked", "PLAN_PHASE_INVALID · No active plan", "red")


@pytest.mark.parametrize(
    "response",
    [
        {"content": "", "tool_calls": []},
        {"content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {"content": '{"reasoning":"inspect","action":{"kind":"tool","target":"read_file"}}'},
        {"content": ('I will inspect the project.\n\n{"reasoning":"inspect","action":{"kind":"tool","target":"read_file","input":{"path":"README.md"}}}')},
    ],
)
def test_llm_response_text_suppresses_transport_and_action_envelopes(response: dict):
    event = _event("llm.completed", {"response": response})

    assert llm_response_text(event) is None
    assert summarize_event(event) is None


def test_llm_response_text_keeps_final_answer():
    event = _event("llm.completed", {"response": {"content": "Task complete.\n\nAll tests pass.", "tool_calls": []}})

    assert llm_response_text(event) == "Task complete.\n\nAll tests pass."
    assert summarize_event(event) == EventSummary("Answer", "Task complete.", "text")


def test_llm_response_text_extracts_custom_action_content():
    event = _event(
        "llm.completed",
        {
            "response": {
                "content": '{"action":{"kind":"custom","input":{"content":"Task complete. All tests pass."}}}',
                "tool_calls": [],
            }
        },
    )

    assert llm_response_text(event) == "Task complete. All tests pass."


def test_malformed_payload_does_not_raise_or_repeat_event_type():
    event = _event("decision.recorded", {"decision": "invalid"})

    assert summarize_event(event) is None
