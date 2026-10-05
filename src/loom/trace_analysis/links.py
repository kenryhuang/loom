"""Causal links between reconstructed trace episodes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from loom.trace_analysis.schemas import EpisodeGraph, LlmRoundEpisode, NormalizedEvent, ToolCallEpisode


def linked_tools_for_round(
    graph: EpisodeGraph,
    round_item: LlmRoundEpisode,
) -> tuple[tuple[ToolCallEpisode, str], ...]:
    rounds = tuple(item for item in graph.llm_rounds if _same_step(item, round_item))
    tools = tuple(item for item in graph.tool_calls if _same_step(item, round_item))
    native_owners = _native_owners(rounds)
    tool_id_counts = _tool_id_counts(tools)
    linked: list[tuple[ToolCallEpisode, str]] = []
    for tool in tools:
        call_id = tool.tool_call_id
        explicit_parents = _explicit_parent_ids(tool)
        if explicit_parents:
            explicit_owners = {
                item.id
                for item in rounds
                if item.llm_call_id in explicit_parents
            }
            if len(explicit_parents) == 1 and explicit_owners == {round_item.id}:
                linked.append((tool, "explicit_parent_id"))
            elif (
                len(explicit_parents) == 1
                and len(explicit_owners) > 1
                and call_id is not None
                and tool_id_counts.get(call_id) == 1
                and native_owners.get(call_id) == {round_item.id}
            ):
                linked.append((tool, "native_tool_call_id"))
            continue

        if call_id is None:
            continue
        if tool_id_counts.get(call_id) == 1 and native_owners.get(call_id) == {round_item.id}:
            linked.append((tool, "native_tool_call_id"))
            continue

        legacy_owners = {item.id for item in rounds if _is_legacy_json_tool_id(call_id, item.llm_call_id)}
        if tool_id_counts.get(call_id) == 1 and legacy_owners == {round_item.id}:
            linked.append((tool, "legacy_id_convention"))
    # A checkpoint can preserve a managed call across an executor restart.
    # The resumed tool has a new loop ID, but its trace/call identity is stable.
    for tool in graph.tool_calls:
        if _same_step(tool, round_item) or not _same_trace(tool, round_item):
            continue
        if not tool.tool_call_id or not _is_managed_json_tool_id(tool.tool_call_id, round_item.llm_call_id):
            continue
        owners = [item for item in graph.llm_rounds if _same_trace(item, tool)
                  and _is_managed_json_tool_id(tool.tool_call_id, item.llm_call_id)]
        calls = [item for item in graph.tool_calls if _same_trace(item, tool) and item.tool_call_id == tool.tool_call_id]
        if len(owners) == len(calls) == 1 and not (_explicit_parent_ids(tool) - {round_item.llm_call_id}):
            linked.append((tool, "managed_resume_id_convention"))
    return tuple(linked)


def tool_output(event: NormalizedEvent) -> tuple[Any, str | None]:
    for field_name in ("output", "result", "error"):
        if field_name not in event.payload:
            continue
        value = event.payload[field_name]
        if isinstance(value, Mapping) and "value" in value and all(marker in value for marker in ("source", "id", "at")):
            return value["value"], f"{field_name}.value"
        return value, field_name
    return None, None


def _same_step(left: LlmRoundEpisode | ToolCallEpisode, right: LlmRoundEpisode) -> bool:
    return (
        left.run_id == right.run_id
        and left.loop_id == right.loop_id
        and left.trace_id == right.trace_id
        and left.step_number == right.step_number
    )


def _same_trace(left, right) -> bool:
    return (left.run_id, left.trace_id, left.step_number) == (right.run_id, right.trace_id, right.step_number)


def _explicit_parent_ids(tool: ToolCallEpisode) -> set[str]:
    return {
        event.llm_call_id
        for event in (tool.started_event, tool.completed_event, tool.failed_event)
        if event is not None and event.llm_call_id is not None
    }


def _native_owners(rounds: tuple[LlmRoundEpisode, ...]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for round_item in rounds:
        for call_id in _native_tool_call_ids(round_item):
            owners.setdefault(call_id, set()).add(round_item.id)
    return owners


def _tool_id_counts(tools: tuple[ToolCallEpisode, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tool in tools:
        if tool.tool_call_id is not None:
            counts[tool.tool_call_id] = counts.get(tool.tool_call_id, 0) + 1
    return counts


def _native_tool_call_ids(round_item: LlmRoundEpisode) -> tuple[str, ...]:
    if round_item.completed_event is None:
        return ()
    response = round_item.completed_event.payload.get("response")
    if not isinstance(response, Mapping):
        return ()
    tool_calls = response.get("tool_calls")
    if not isinstance(tool_calls, list | tuple):
        return ()
    ids: list[str] = []
    for item in tool_calls:
        call_id = (
            item.get("id") or item.get("tool_call_id")
            if isinstance(item, Mapping)
            else getattr(item, "id", None) or getattr(item, "tool_call_id", None)
        )
        if call_id is not None:
            ids.append(str(call_id))
    return tuple(ids)


def _is_legacy_json_tool_id(tool_call_id: str, llm_call_id: str) -> bool:
    if re.fullmatch(rf"{re.escape(llm_call_id)}-json-tool-[0-9]+", tool_call_id):
        return True
    return _is_managed_json_tool_id(tool_call_id, llm_call_id)


def _is_managed_json_tool_id(tool_call_id: str, llm_call_id: str) -> bool:
    # ManagedStep emits <trace>-llm-<call> and <trace>-json-<action>-<call>.
    managed = re.fullmatch(r"(.+)-llm-([0-9]+)", llm_call_id)
    return managed is not None and re.fullmatch(rf"{re.escape(managed[1])}-json-[0-9]+-{managed[2]}", tool_call_id) is not None


__all__ = ["linked_tools_for_round", "tool_output"]
