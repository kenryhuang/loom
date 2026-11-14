"""Assembly adapter for optional review contexts and durable control receipts."""

import uuid
from contextvars import ContextVar
from dataclasses import replace

from loom.contexts.working_set import notes_tool_ref
from loom.core import Observation, new_trace_id, now_iso, ok, thaw_json
from loom.llm.api import LlmToolCall
from loom.runtime.plugin_contracts import json_value
from loom.service.contracts import canonical

ACTIVE_REVIEW = ContextVar("loom_review_lane", default=None)


def working_set(assembly):
    active = ACTIVE_REVIEW.get()
    if active is not None and active[0] is assembly:
        return active[1]
    return getattr(getattr(assembly, "context_manager", None), "working_set", None)


def sync_goal(assembly):
    state = working_set(assembly)
    if state is not None:
        state.set_goal(assembly.acceptance.data["goal_digest"])
    return state


def observe(assembly, receipt, observation):
    state = sync_goal(assembly)
    if state is None:
        return observation
    output = thaw_json(observation.value)
    state.observe_tool(receipt, output, workspace=str(assembly.tool_request.workspace or ""))
    metadata = {**thaw_json(observation.metadata or {}), "review_receipt": receipt}
    if receipt["id"] in state.data["sources"] and isinstance(output, dict):
        output["context_source_id"] = receipt["id"]
    return replace(observation, value=output, metadata=metadata)


def replay(assembly, observation):
    """Replay host state from the journal; never reacquire a source or rerun notes."""
    state = working_set(assembly)
    if state is None:
        return observation
    metadata = thaw_json(observation.metadata or {})
    receipt = metadata.get("review_receipt")
    if receipt:
        assembly._acceptance_calls = [r for r in assembly._acceptance_calls if r["id"] != receipt["id"]] + [receipt]
        observation = observe(assembly, receipt, observation)
    snapshot = metadata.get("review_notes_snapshot")
    if snapshot and snapshot["revision"] > state.data["revision"]:
        state.restore(snapshot)
    return observation


def validate_current_sources(assembly, change):
    """Check only cited file versions; no workspace scan on every model request."""
    from loom.tasks.workspace_probe import ManifestUnavailable, manifest

    state = working_set(assembly)
    if state is None or change.get("change_id") in state.data["changes"]:
        return
    citations = {e["evidence_id"] for row in change.get("findings", []) for e in row.get("evidence", [])}
    stale = []
    for row in assembly._acceptance_calls:
        if row["id"] not in citations or row.get("dependencies") is None:
            continue
        try:
            current = manifest(assembly.tool_request.workspace, row["paths"])
        except (OSError, ValueError, ManifestUnavailable):
            current = None
        if current != row["dependencies"]:
            stale.append(row["id"])
    state.mark_stale(stale)
    if stale:
        raise ValueError("Cited files changed or are unavailable; refresh affected evidence and use the new working set revision")


def notes_handler(assembly):
    async def record(value, options=None):
        from loom.runtime.execution_contracts import validate_tool_input

        state = sync_goal(assembly)
        value = thaw_json(value)
        metadata = (options or {}).get("metadata", {})
        call = LlmToolCall(metadata.get("tool_call_id") or uuid.uuid4().hex, "record_context_notes", canonical(value))
        journal_here = assembly.execution is not None and not metadata.get("operation_journaled")
        saved = assembly.execution.operation_start(call) if journal_here else None
        if saved:
            if saved.get("defer"):
                from loom.core import err, make_loom_error

                return err(make_loom_error("EXECUTION_DEFERRED", "Execution deferred at a control boundary", retryable=True))
            if saved["ok"]:
                return ok(replay(assembly, saved["value"]))
        try:
            validate_tool_input(notes_tool_ref().input_schema, value)
            validate_current_sources(assembly, value)
            result = state.update_notes(value)
        except (ValueError, KeyError, TypeError) as exc:
            result = {"accepted": False, "reason": str(exc), "revision": state.data["revision"]}
        observation = Observation(new_trace_id(), "record_context_notes", result, now_iso(),
                                  metadata={"review_notes_snapshot": json_value(state.snapshot())})
        if journal_here:
            assembly.execution.operation_finish(call, {"ok": True, "value": observation, "error": None})
        return ok(observation)

    return record
