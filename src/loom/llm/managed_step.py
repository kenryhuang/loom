"""Checkpointed LLM execution for durable service sessions.

The legacy one-shot step remains unchanged. This adapter uses the same provider,
prompt, decision and observation contracts, with explicit resumable phases.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import replace

from loom.core import Decision, Observation, StepResult, ToolRef, Trace, err, freeze_context, make_loom_error, new_context_id, now_iso, ok, thaw_json
from loom.llm.api import (
    LlmMessage,
    LlmToolCall,
    TokenUsage,
    _apply_observation_policy,
    _json_tool_actions,
    _parse_decision,
    _recoverable_tool_failure,
    _requests_step_boundary,
    _tool_failure_observation,
    build_messages,
    request_llm_response,
    to_llm_tools,
)
from loom.runtime.control import StepControl
from loom.service.contracts import LIMITS, canonical

INPUT_TOOL = ToolRef(
    "request_input",
    "Ask the user a question and suspend until they answer. Use when missing information prevents meaningful progress.",
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
        "required": ["question"],
        "additionalProperties": False,
    },
)


class ManagedStep:
    def __init__(self, provider, execution, *, planning=None, limits=None, stream=False, assembly=None):
        self.provider = provider
        self.execution = execution
        self.planning = planning
        self.limits = {**LIMITS, **(limits or {})}
        self.stream = stream
        self.assembly = assembly

    async def _emit(self, runtime, kind, **payload):
        result = await runtime.trace_sink.emit(
            {"type": kind, "run_id": runtime.run_id, "loop_id": runtime.loop_id, "trace_id": runtime.trace_id, "at": runtime.now(), **payload}
        )
        if not result.ok:
            raise RuntimeError(result.error.message)

    def _checkpoint(self, cp):
        if self.planning:
            cp["planning"] = self.planning.snapshot()
        if self.assembly:
            cp["plugin_states"] = self.assembly.snapshot()
        return self.execution.boundary(cp)

    def _terminal(self, cp, runtime):
        directive = self._checkpoint(cp)
        control = directive.get("control")
        if control:
            return self._result(cp, runtime, control["kind"], control.get("reason", ""))
        terminal = cp["terminal"]
        return self._result(cp, runtime, terminal["kind"], parsed=terminal["parsed"])

    def _context(self, cp, *, parsed=None):
        base = cp["context"]
        decisions = base.state.decisions
        observations = list(cp["observations"])
        if parsed is not None:
            tid = cp["trace_id"]
            observations.append(Observation(f"{tid}-llm", "llm", parsed["output"], now_iso()))
            decisions = (
                *decisions,
                Decision(
                    f"{tid}-decision",
                    parsed["action"],
                    parsed["reasoning"],
                    tuple(parsed["alternatives"]),
                    parsed["confidence"],
                    now_iso(),
                    metadata={"parseFallback": parsed["parse_fallback"]},
                ),
            )
        context = freeze_context(
            replace(base, id=new_context_id(), state=replace(base.state, observations=(*base.state.observations, *observations), decisions=decisions))
        )
        if self.planning:
            if hasattr(self.planning, "project_state"):
                context = self.planning.project_state(context)
            else:
                from loom.runtime.planning import plan_state_dict
                from loom.runtime.workflow_routing import workflow_route_state_dict

                scratch = dict(context.state.scratch or {})
                scratch.update(plan=plan_state_dict(self.planning.controller.state), workflowRoute=workflow_route_state_dict(self.planning.route.state))
                context = replace(context, state=replace(context.state, scratch=scratch))
        return context

    def _result(self, cp, runtime, kind, reason="", request_id=None, *, parsed=None):
        context = self._context(cp, parsed=parsed)
        trace = Trace(
            cp["trace_id"],
            context.run_id,
            runtime.loop_id,
            "v1",
            len(cp["context"].state.observations),
            cp["trace_id"],
            cp["started_at"],
            now_iso(),
            0,
            cp["context"].id,
            context.id,
            "pass" if kind in {"continue", "completed"} else kind,
            observations=tuple(cp["observations"]),
            metadata={
                "tokenUsage": thaw_json(cp["usage"].__dict__)
                if hasattr(cp["usage"], "__dict__")
                else {"prompt_tokens": cp["usage"].prompt_tokens, "completion_tokens": cp["usage"].completion_tokens, "total_tokens": cp["usage"].total_tokens}
            },
        )
        return ok(StepResult(context, trace, output=parsed["output"] if parsed else None, control=StepControl(kind, reason, request_id)))

    async def _interrupt_batch(self, cp, runtime, reason):
        calls = cp.get("calls", [])
        for call in calls[cp["tool_index"] :]:
            value = {"ok": False, "interrupted": True, "reason": reason}
            cp["messages"].append(
                LlmMessage("tool" if cp["native"] else "user", canonical(value), name=call.name, tool_call_id=call.id if cp["native"] else None)
            )
            await self._emit(runtime, "tool.interrupted", tool_call_id=call.id, tool_id=call.name, reason=reason)
        cp["tool_index"] = len(calls)
        cp["pending_input"] = None
        cp["phase"] = "before_llm"

    async def _boundary(self, cp, runtime):
        directive = self._checkpoint(cp)
        answer = directive.get("input_answer")
        pending = cp.get("pending_input")
        if pending and answer and answer["request_id"] == pending["request_id"]:
            call = pending["call"]
            observation = Observation(f"{call.id}-answer", "request_input", answer, now_iso())
            self.execution.operation_finish(call, {"ok": True, "value": observation})
            cp["observations"].append(observation)
            cp["messages"].append(LlmMessage("tool", canonical(answer), name=call.name, tool_call_id=call.id))
            cp["tool_index"] += 1
            cp["pending_input"] = None
            await self._emit(runtime, "tool.completed", tool_call_id=call.id, tool_id=call.name, output=observation)
            if answer.get("superseded"):
                await self._interrupt_batch(cp, runtime, "User superseded the question")
        inputs = directive.get("inputs", [])
        if inputs:
            if cp["phase"] == "after_llm":
                response = cp["response"]
                if response.tool_calls:
                    cp["calls"] = list(response.tool_calls)
                    cp["native"] = True
                    cp["tool_index"] = 0
                    cp["messages"].append(LlmMessage("assistant", response.content or "", tool_calls=response.tool_calls))
                    await self._interrupt_batch(cp, runtime, "New user guidance superseded the old response")
                cp["phase"] = "before_llm"
            if cp["phase"] == "tool_batch" and not cp.get("pending_input"):
                await self._interrupt_batch(cp, runtime, "New user guidance superseded unexecuted calls")
            for item in inputs:
                cp["messages"].append(LlmMessage("user", item["content"]))
            cp["input_cursor"] = inputs[-1]["seq"]
            guidance = "\n".join(item["content"] for item in inputs)
            base = cp["context"]
            cp["context"] = replace(base, goal=replace(base.goal, objective=f"{base.goal.objective}\n\nUser guidance:\n{guidance}"))
            if self.assembly:
                cp["messages"][0] = self.assembly.context_manager.project(cp["context"])[0]
            # Persist the input projection and cursor before taking a new action.
            directive = self._checkpoint(cp)
        control = directive.get("control")
        if control:
            return self._result(cp, runtime, control["kind"], control.get("reason", ""))
        if cp.get("pending_input"):
            return self._result(cp, runtime, "waiting_input", request_id=cp["pending_input"]["request_id"])
        if cp["usage"].total_tokens > self.limits["max_tokens"]:
            return self._result(cp, runtime, "paused", "Token budget exceeded")
        return None

    async def __call__(self, context, runtime):
        restored = getattr(self.execution, "checkpoint", None)
        if restored and restored.get("terminal"):
            if self.planning and restored.get("planning"):
                self.planning.restore(restored["planning"]).unwrap()
            return self._terminal(restored, runtime)
        if restored and restored.get("phase") != "step_done":
            cp = restored
            if self.planning and cp.get("planning"):
                restored_plan = self.planning.restore(cp["planning"])
                if not restored_plan.ok:
                    return restored_plan
        else:
            counters = getattr(self.execution, "counters", {})
            cp = {
                "schema_version": 1,
                "context": context,
                "trace_id": runtime.trace_id,
                "started_at": now_iso(),
                "phase": "before_llm",
                "messages": self.assembly.context_manager.project(context) if self.assembly else list(build_messages(context, max_history_steps=5)),
                "observations": [],
                "response": None,
                "calls": [],
                "tool_index": 0,
                "native": True,
                "pending_input": None,
                "llm_calls": counters.get("llm_calls", 0),
                "usage": counters.get("usage", TokenUsage()),
                "input_cursor": getattr(self.execution, "input_cursor", 0),
                "missing_retries": 0,
            }
        while True:
            suspended = await self._boundary(cp, runtime)
            if suspended is not None:
                return suspended
            if cp["phase"] == "before_llm":
                if cp["llm_calls"] >= self.limits["max_llm_calls"]:
                    return self._result(cp, runtime, "paused", "LLM call budget exceeded")
                refs = self.assembly.resolve_tools(cp["context"]) if self.assembly else cp["context"].affordances.tools
                tools = to_llm_tools((*refs, INPUT_TOOL))
                if self.assembly:
                    try:
                        window, compacted = self.assembly.context_manager.compact(
                            cp["messages"], self.limits["max_window_chars"], schema_chars=len(json.dumps(tools))
                        )
                    except ValueError as exc:
                        return self._result(cp, runtime, "paused", str(exc))
                    cp["messages"] = window
                    if compacted:
                        await self._emit(runtime, "context.compacted", **compacted)
                        self._checkpoint(cp)
                elif sum(len(m.content or "") + sum(len(t.arguments) for t in m.tool_calls) for m in cp["messages"]) > self.limits["max_window_chars"]:
                    return self._result(cp, runtime, "paused", "Model context window limit exceeded")
                policy = self.planning.step_policy(cp["context"]) if self.planning else None
                cp["llm_calls"] += 1
                llm_id = f"{cp['trace_id']}-llm-{cp['llm_calls']}"
                suspended = await self._boundary(cp, runtime)
                if suspended is not None:
                    return suspended
                await self._emit(runtime, "llm.requested", llm_call_id=llm_id, model=self.provider.model, messages=tuple(cp["messages"]), tools=tools)
                request = asyncio.create_task(
                    request_llm_response(
                        self.provider,
                        cp["messages"],
                        tools,
                        runtime.cancellation,
                        tool_choice=policy.tool_choice if policy else None,
                        stream=self.stream,
                        emit_event=runtime.trace_sink.emit,
                        event_metadata={"run_id": runtime.run_id, "loop_id": runtime.loop_id, "trace_id": cp["trace_id"], "llm_call_id": llm_id},
                    )
                )
                poll = getattr(self.execution, "poll_control", None)
                try:
                    while poll and not request.done():
                        done, _ = await asyncio.wait({request}, timeout=0.1)
                        if done:
                            break
                        control = poll()
                        if control:
                            request.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await request
                            self._checkpoint(cp)
                            return self._result(cp, runtime, control["kind"], control.get("reason", ""))
                    response = await request
                finally:
                    if not request.done():
                        request.cancel()
                if not response.ok:
                    await self._emit(runtime, "llm.failed", llm_call_id=llm_id, error=response.error)
                    return response
                cp["response"] = response.value
                usage = response.value.usage
                previous = cp["usage"]
                cp["usage"] = TokenUsage(
                    previous.prompt_tokens + usage.prompt_tokens,
                    previous.completion_tokens + usage.completion_tokens,
                    previous.total_tokens + usage.total_tokens,
                )
                cp["phase"] = "after_llm"
                await self._emit(runtime, "llm.completed", llm_call_id=llm_id, response=response.value)
                suspended = await self._boundary(cp, runtime)
                if suspended is not None:
                    return suspended
                continue
            if cp["phase"] == "after_llm":
                response = cp["response"]
                calls = list(response.tool_calls)
                cp["native"] = bool(calls)
                if not calls:
                    parsed = _parse_decision(response.content, cp["trace_id"])
                    actions = _json_tool_actions(parsed, frozenset(t.id for t in cp["context"].affordances.tools))
                    calls = [
                        LlmToolCall(f"{cp['trace_id']}-json-{i}-{cp['llm_calls']}", a.target, canonical(thaw_json(a.input) or {}))
                        for i, a in enumerate(actions)
                    ]
                if calls:
                    cp["calls"] = calls
                    cp["tool_index"] = 0
                    cp["messages"].append(LlmMessage("assistant", response.content or "", tool_calls=tuple(calls) if cp["native"] else ()))
                    cp["phase"] = "tool_batch"
                    continue
                policy = self.planning.step_policy(cp["context"]) if self.planning else None
                if policy and policy.require_tool_call:
                    if cp["missing_retries"] >= policy.missing_tool_call_retries:
                        return err(make_loom_error(policy.failure_code, "Model did not select the required workflow tool", retryable=False))
                    cp["missing_retries"] += 1
                    cp["messages"].extend([LlmMessage("assistant", response.content or ""), LlmMessage("user", policy.retry_prompt)])
                    cp["phase"] = "before_llm"
                    continue
                parsed = _parse_decision(response.content, cp["trace_id"])
                unfinished_plan = self.planning and (
                    self.planning.unfinished()
                    if hasattr(self.planning, "unfinished")
                    else self.planning.controller.state.phase.value in {"planning", "executing"}
                )
                finished = any(o.source == "finish" and thaw_json(o.value).get("completed") for o in cp["observations"]) or not parsed["parse_fallback"]
                completion_due = not hasattr(self.planning, "is_final_node") or self.planning.is_final_node()
                if finished and completion_due and self.assembly and self.assembly.completion_error():
                    cp["messages"].extend(
                        [
                            LlmMessage("assistant", response.content or ""),
                            LlmMessage("user", self.assembly.completion_error() + ". Gather source evidence before finishing."),
                        ]
                    )
                    cp["phase"] = "before_llm"
                    cp["output_retries"] = cp.get("output_retries", 0) + 1
                    if cp["output_retries"] > 1:
                        return self._result(cp, runtime, "paused", self.assembly.completion_error())
                    continue
                if finished and hasattr(self.planning, "complete_active"):
                    running = any(node["status"] == "running" for node in self.planning.controller.nodes)
                    if running:
                        await self.planning.complete_active(str(parsed["output"]))
                    unfinished_plan = self.planning.unfinished()
                kind = "completed" if finished and not unfinished_plan else "continue"
                cp["phase"] = "step_done"
                cp["terminal"] = {"kind": kind, "parsed": parsed}
                return self._terminal(cp, runtime)
            if cp["phase"] == "tool_batch":
                if cp["tool_index"] >= len(cp["calls"]):
                    cp["phase"] = "before_llm"
                    continue
                call = cp["calls"][cp["tool_index"]]
                try:
                    value = json.loads(call.arguments)
                    if not isinstance(value, dict):
                        raise ValueError("Tool arguments must be an object")
                except (ValueError, TypeError) as exc:
                    return err(make_loom_error("VALIDATION_FAILED", str(exc), retryable=False))
                if call.name == "request_input":
                    if not isinstance(value.get("question"), str) or not value["question"].strip():
                        return err(make_loom_error("VALIDATION_FAILED", "Input question must be non-empty", retryable=False))
                    await self._emit(runtime, "tool.started", tool_call_id=call.id, tool_id=call.name, input=value)
                    request = self.execution.request_input(call, value)
                    cp["pending_input"] = {"call": call, "request_id": request["id"]}
                    suspended = await self._boundary(cp, runtime)
                    if suspended is not None:
                        return suspended
                    continue
                persisted = self.execution.operation_start(call)
                if persisted and persisted.get("defer"):
                    continue
                if persisted is not None:
                    result = ok(persisted["value"]) if persisted["ok"] else err(persisted["error"])
                elif call.name not in {t.id for t in cp["context"].affordances.tools}:
                    result = err(
                        make_loom_error("TOOL_FAILED", "Tool is not available in the current phase", retryable=False, metadata={"failureDomain": "tool"})
                    )
                else:
                    result = await runtime.call_tool(call.name, value, metadata={"tool_call_id": call.id, "operation_journaled": True})
                self.execution.operation_finish(
                    call, {"ok": result.ok, "value": result.value if result.ok else None, "error": result.error if not result.ok else None}
                )
                if not result.ok:
                    if not _recoverable_tool_failure(result.error):
                        return result
                    observation = _tool_failure_observation(result.error, observation_id=f"{call.id}-failure", source=call.name, at=runtime.now()).unwrap()
                else:
                    observation = result.value
                if persisted and self.planning and isinstance(observation, Observation):
                    recovered = thaw_json(observation.value)
                    if isinstance(recovered, dict) and recovered.get("accepted"):
                        if recovered.get("workflow") and hasattr(self.planning, "restore_plugin"):
                            self.planning.restore(recovered["workflow"]).unwrap()
                            recovered = {}
                        participant = self.planning.snapshot()
                        if recovered.get("plan"):
                            participant["plan"] = recovered["plan"]
                            participant["next_item_number"] = max(participant["next_item_number"], len(recovered["plan"]["items"]) + 1)
                        if recovered.get("route"):
                            participant["route"] = recovered["route"]
                        self.planning.restore(participant).unwrap()
                if not isinstance(observation, Observation):
                    observation = Observation(f"{call.id}-observation", call.name, observation, runtime.now())
                if persisted and result.ok and self.assembly:
                    self.assembly.ingest_tool_result(call.name, observation)
                    observation = self.assembly.project_tool_result(call.name, observation)
                controlled = await _apply_observation_policy(self.planning.observe_tool if self.planning else None, cp["context"], observation)
                if not controlled.ok:
                    return controlled
                observation = controlled.value
                cp["observations"].append(observation)
                cp["messages"].append(
                    LlmMessage(
                        "tool" if cp["native"] else "user",
                        canonical(thaw_json(observation.value)),
                        name=call.name,
                        tool_call_id=call.id if cp["native"] else None,
                    )
                )
                cp["tool_index"] += 1
                suspended = await self._boundary(cp, runtime)
                if suspended is not None:
                    return suspended
                if _requests_step_boundary(observation):
                    await self._interrupt_batch(cp, runtime, "Workflow phase changed")
                    cp["phase"] = "step_done"
                    parsed = _parse_decision(cp["response"].content, cp["trace_id"])
                    kind = "completed" if observation.source == "finish" else "continue"
                    cp["terminal"] = {"kind": kind, "parsed": parsed}
                    return self._terminal(cp, runtime)
