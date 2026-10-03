"""Semantic summaries for the curated TUI event timeline."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loom.tui.tui_collector import TuiEvent

_REDUNDANT_OBSERVATION_SOURCES = frozenset(
    {
        "llm",
        "read_file",
        "edit_file",
        "write_file",
        "shell_execute",
        "finish",
        "enter_plan",
        "continue_react",
        "submit_plan",
        "update_plan",
    }
)
_TEST_RESULT_RE = re.compile(
    r"(?P<counts>\d+\s+passed(?:,\s*\d+\s+(?:failed|skipped|xfailed|xpassed|errors?))*)"
    r"(?:\s+in\s+(?P<duration>\d+(?:\.\d+)?)s)?",
    re.IGNORECASE,
)
_ACTION_ENVELOPE_RE = re.compile(r'"action"\s*:\s*\{', re.IGNORECASE)
_TOOL_KIND_RE = re.compile(r'"kind"\s*:\s*"tool"', re.IGNORECASE)


@dataclass(frozen=True)
class EventSummary:
    """Collapsed presentation text independent of Rich/Textual."""

    title: str
    description: str = ""
    color_role: str = "text"


def summarize_event(event: TuiEvent) -> EventSummary | None:
    """Return a concise semantic summary, or ``None`` for non-semantic data."""
    if event.event_type == "message.created":
        title = "You" if event.data.get("role") == "user" else "Loom"
        status = event.data.get("state", "accepted")
        return EventSummary(f"{title} · {status}", _first_meaningful_line(_text(event.data.get("content"))), "text")
    if event.event_type.startswith("input."):
        return EventSummary("Input " + event.event_type.split(".")[1], _text(event.data.get("question")), "orange")
    if event.data.get("artifact"):
        return EventSummary("Execution detail", _text(event.data.get("summary")), "text")
    if event.event_type.startswith("workflow."):
        return _workflow_summary(event.event_type, event.data)
    if event.event_type == "decision.recorded":
        return _decision_summary(event.data)
    if event.event_type.startswith("action."):
        return _action_summary(event.event_type, event.data)
    if event.event_type == "observation.recorded":
        return _observation_summary(event.data)
    if event.event_type.startswith("tool."):
        return _tool_summary(event.event_type, event.data, event.error)
    if event.event_type == "llm.completed":
        content = llm_response_text(event)
        if content:
            return EventSummary("Answer", _first_meaningful_line(content), "text")
        return None
    return None


def _workflow_summary(event_type: str, data: Mapping[str, Any]) -> EventSummary | None:
    reason = _truncate(_text(data.get("reason")))
    if event_type == "workflow.routing.requested":
        title = "Workflow evaluating" if data.get("trigger") == "initial" else "Workflow reviewing"
        return EventSummary(title, reason, "orange")
    if event_type == "workflow.route.selected":
        route = _text(data.get("route")).lower()
        label = "ReAct" if route == "react" else "Plan" if route == "plan" else _humanize(route)
        return EventSummary(f"Workflow {label}", reason, "blue" if route == "plan" else "green")
    return None


def is_redundant_observation(event: TuiEvent) -> bool:
    """Whether an observation is already represented by another semantic row."""
    if event.event_type != "observation.recorded":
        return False
    observation = _mapping(event.data.get("observation"))
    if observation is None:
        return True
    source = str(observation.get("source") or "")
    value = _mapping(observation.get("value"))
    if source == "planning.guard":
        return False
    if value and (value.get("accepted") is False or value.get("code") or value.get("error")):
        return False
    return source in _REDUNDANT_OBSERVATION_SOURCES or not source


def llm_response_text(event: TuiEvent) -> str | None:
    """Extract user-facing final text while rejecting tool/action envelopes."""
    response = _mapping(event.data.get("response"))
    if response is None:
        return None
    tool_calls = response.get("tool_calls")
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, str | bytes) and tool_calls:
        return None
    content = response.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    parsed = _parse_json_mapping(text)
    if parsed is not None:
        action = _mapping(parsed.get("action"))
        if action is None:
            return text
        kind = str(action.get("kind") or "")
        if kind == "tool":
            return None
        action_input = _mapping(action.get("input"))
        if action_input:
            final_text = action_input.get("content") or action_input.get("report")
            if isinstance(final_text, str) and final_text.strip():
                return final_text.strip()
        return None
    if _ACTION_ENVELOPE_RE.search(text) and _TOOL_KIND_RE.search(text):
        return None
    return text


def _decision_summary(data: Mapping[str, Any]) -> EventSummary | None:
    decision = _mapping(data.get("decision"))
    if decision is None:
        return None
    action = _mapping(decision.get("action")) or {}
    description = _text(action.get("description")) or _text(action.get("target"))
    if not description:
        description = _first_meaningful_line(_text(decision.get("reasoning")))
    if not description:
        return None
    return EventSummary("Decision", _truncate(description), "text")


def _action_summary(event_type: str, data: Mapping[str, Any]) -> EventSummary | None:
    action = _mapping(data.get("action"))
    if action is None:
        return None
    description = _text(action.get("description")) or _text(action.get("target"))
    if not description:
        return None
    transition = event_type.removeprefix("action.").replace("_", " ")
    outcome = _text(data.get("outcome")) if event_type == "action.completed" else ""
    suffix = f" · {outcome}" if outcome else ""
    color = "red" if outcome in {"fail", "failed", "error"} else "green" if event_type == "action.completed" else "orange"
    return EventSummary(f"Action {transition}", _truncate(f"{description}{suffix}"), color)


def _observation_summary(data: Mapping[str, Any]) -> EventSummary | None:
    observation = _mapping(data.get("observation"))
    if observation is None:
        return None
    source = _text(observation.get("source"))
    value = _mapping(observation.get("value"))
    if source == "planning.guard":
        code, message = _code_and_message(value)
        return EventSummary("Plan blocked", _join_parts(code, message), "red")
    if value and (value.get("accepted") is False or value.get("code") or value.get("error")):
        code, message = _code_and_message(value)
        return EventSummary(f"{_humanize(source) or 'Observation'} failed", _join_parts(code, message), "red")
    if source and source not in _REDUNDANT_OBSERVATION_SOURCES:
        message = _generic_result(value if value is not None else observation.get("value"))
        return EventSummary(_humanize(source), message, "text") if message else None
    return None


def _tool_summary(event_type: str, data: Mapping[str, Any], event_error: str | None) -> EventSummary:
    tool_id = _text(data.get("tool_name") or data.get("tool_id") or data.get("tool_call_id")) or "tool"
    title = _tool_title(tool_id)
    if event_type == "tool.failed":
        output = _unwrap_output(data.get("output"))
        code, message = _code_and_message(output, fallback=event_error or _text(data.get("error")))
        return EventSummary(f"{title} failed", _join_parts(code, message) or "Failed", "red")

    input_value = _mapping(_parse_jsonish(data.get("arguments") if "arguments" in data else data.get("input")))
    if event_type != "tool.completed":
        return EventSummary(title, _running_tool_description(tool_id, input_value), "orange")

    output = _unwrap_output(data.get("output"))
    output_mapping = _mapping(output)
    if output_mapping and (output_mapping.get("accepted") is False or output_mapping.get("code")):
        code, message = _code_and_message(output_mapping)
        return EventSummary(f"{title} blocked", _join_parts(code, message) or "Rejected", "red")
    if tool_id in {"enter_plan", "submit_plan", "update_plan"}:
        return _plan_tool_summary(title, output)
    if tool_id == "shell_execute":
        return _shell_summary(title, output)
    if tool_id == "read_file":
        return _read_summary(title, output, input_value)
    if tool_id == "edit_file":
        return _edit_summary(title, output, input_value)
    if tool_id == "write_file":
        return _write_summary(title, output, input_value)
    if tool_id == "finish":
        completed = output.get("completed") if isinstance(output, Mapping) else False
        return EventSummary(title, "Completed" if completed else "Finished", "green")
    description = _generic_result(output)
    if not description:
        description = _running_tool_description(tool_id, input_value)
    return EventSummary(title, _truncate(description), "green")


def _plan_tool_summary(title: str, output: Any) -> EventSummary:
    value = _mapping(output)
    if value and value.get("accepted") is False:
        code, message = _code_and_message(value)
        return EventSummary(f"{title} blocked", _join_parts(code, message) or "Rejected", "red")
    return EventSummary(title, "Accepted", "green")


def _shell_summary(title: str, output: Any) -> EventSummary:
    value = _mapping(output) or {}
    exit_code = value.get("exit_code")
    timed_out = value.get("timed_out") is True
    failed = timed_out or (isinstance(exit_code, int) and exit_code != 0)
    status = "Timed out" if timed_out else "Failed" if failed else "Passed"
    parts = [status]
    if exit_code is not None:
        parts.append(f"exit {exit_code}")
    signal = _shell_signal(_text(value.get("stdout")), _text(value.get("stderr")), failed=failed)
    if signal:
        parts.append(signal)
    duration = _format_duration(value.get("duration_ms"))
    if duration and not (_TEST_RESULT_RE.search(signal) if signal else False):
        parts.append(duration)
    elif duration:
        match = _TEST_RESULT_RE.search(signal)
        if match is None or match.group("duration") is None:
            parts.append(duration)
    return EventSummary(title, _join_parts(*parts), "red" if failed else "green")


def _read_summary(title: str, output: Any, input_value: Mapping[str, Any] | None) -> EventSummary:
    value = _mapping(output) or {}
    path = _text(value.get("path")) or _text((input_value or {}).get("path"))
    parts = [path]
    content = value.get("content")
    if isinstance(content, str):
        parts.append(f"{len(content.splitlines())} lines")
    byte_count = _format_bytes(value.get("bytes_read"))
    if byte_count:
        parts.append(byte_count)
    if value.get("truncated") is True:
        parts.append("truncated")
    return EventSummary(title, _join_parts(*parts), "green")


def _edit_summary(title: str, output: Any, input_value: Mapping[str, Any] | None) -> EventSummary:
    value = _mapping(output) or {}
    path = _text(value.get("path")) or _text((input_value or {}).get("path"))
    parts = [path]
    replacements = value.get("replacements")
    if isinstance(replacements, int):
        parts.append(f"{replacements} replacement{'s' if replacements != 1 else ''}")
    first_line = value.get("first_changed_line")
    if isinstance(first_line, int):
        parts.append(f"first change line {first_line}")
    byte_count = _format_bytes(value.get("bytes_written"))
    if byte_count:
        parts.append(byte_count)
    return EventSummary(title, _join_parts(*parts), "green")


def _write_summary(title: str, output: Any, input_value: Mapping[str, Any] | None) -> EventSummary:
    value = _mapping(output) or {}
    path = _text(value.get("path")) or _text((input_value or {}).get("path"))
    byte_count = _format_bytes(value.get("bytes_written"))
    return EventSummary(title, _join_parts(path, byte_count), "green")


def _running_tool_description(tool_id: str, input_value: Mapping[str, Any] | None) -> str:
    value = input_value or {}
    if tool_id == "shell_execute":
        command = value.get("command")
        parts = command if isinstance(command, Sequence) and not isinstance(command, str | bytes) else str(command or "").split()
        executable = str(parts[0]).rsplit("/", 1)[-1] if parts else ""
        if executable in {"pytest", "cargo", "go", "npm", "pnpm", "yarn"} or "pytest" in [str(part) for part in parts]:
            return "Running tests"
        if executable in {"ls", "find", "rg", "grep"}:
            return "Inspecting files"
        if executable == "git":
            return "Checking repository"
        return f"Running {executable}" if executable else "Running command"
    for key in ("path", "file", "query", "description"):
        item = value.get(key)
        if item:
            return _truncate(_text(item))
    return "Running"


def _shell_signal(stdout: str, stderr: str, *, failed: bool) -> str:
    combined = "\n".join(part for part in ((stderr if failed else stdout), stdout if failed else stderr) if part)
    match = _TEST_RESULT_RE.search(combined)
    if match:
        return match.group("counts")
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    return _truncate(lines[0] if lines else "")


def _unwrap_output(output: Any) -> Any:
    value = output.value if hasattr(output, "value") else output
    for _ in range(3):
        mapping = _mapping(value)
        if mapping is None or "value" not in mapping:
            break
        wrapper_keys = set(mapping) - {"at", "id", "metadata", "source", "value"}
        if wrapper_keys:
            break
        value = mapping.get("value")
    return value


def _generic_result(value: Any) -> str:
    mapping = _mapping(value)
    if mapping:
        for key in ("summary", "message", "status", "result"):
            item = mapping.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        for key in ("count", "total", "matches", "items"):
            item = mapping.get(key)
            if isinstance(item, int | float):
                return f"{key} {item}"
        return ""
    if isinstance(value, str | int | float | bool):
        return _text(value)
    return ""


def _code_and_message(value: Any, *, fallback: str = "") -> tuple[str, str]:
    mapping = _mapping(value) or {}
    error = _mapping(mapping.get("error"))
    code = _text(mapping.get("code") or (error or {}).get("code"))
    message = _text(mapping.get("message") or (error or {}).get("message") or fallback)
    return code, _truncate(_first_meaningful_line(message))


def _tool_title(tool_id: str) -> str:
    return {
        "read_file": "Read",
        "edit_file": "Edit",
        "write_file": "Write",
        "shell_execute": "Bash",
        "finish": "Finish",
        "enter_plan": "Enter Plan",
        "submit_plan": "Submit Plan",
        "update_plan": "Update Plan",
    }.get(tool_id, _humanize(tool_id) or "Tool")


def _humanize(value: str) -> str:
    return value.replace("_", " ").replace(".", " ").strip().title()


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    parsed = _parse_json_mapping(value)
    return parsed if parsed is not None else value


def _parse_json_mapping(value: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _first_meaningful_line(value: str) -> str:
    for line in value.replace("\\n", "\n").splitlines():
        normalized = " ".join(line.strip().split())
        if normalized:
            return normalized
    return ""


def _truncate(value: str, max_chars: int = 96) -> str:
    normalized = " ".join(value.replace("\\n", " ").split())
    return normalized if len(normalized) <= max_chars else f"{normalized[: max_chars - 3]}..."


def _join_parts(*parts: Any) -> str:
    return " · ".join(_text(part) for part in parts if _text(part))


def _format_bytes(value: Any) -> str:
    if not isinstance(value, int | float) or value < 0:
        return ""
    if value < 1024:
        return f"{int(value)} B"
    if value < 1024 * 1024:
        number = value / 1024
        return f"{number:.0f} KB" if number >= 10 or number.is_integer() else f"{number:.1f} KB"
    number = value / (1024 * 1024)
    return f"{number:.0f} MB" if number >= 10 or number.is_integer() else f"{number:.1f} MB"


def _format_duration(value: Any) -> str:
    if not isinstance(value, int | float) or value < 0:
        return ""
    seconds = value / 1000
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds / 60:g}m"
    if seconds >= 10:
        return f"{seconds:.1f}s".replace(".0s", "s")
    return f"{seconds:.2f}s".rstrip("0").rstrip(".") + ("s" if not f"{seconds:.2f}s".rstrip("0").rstrip(".").endswith("s") else "")


__all__ = ["EventSummary", "is_redundant_observation", "llm_response_text", "summarize_event"]
