"""LLM integration for Loom."""

from __future__ import annotations

import inspect
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from loom.core.models import (
    Action,
    Context,
    Decision,
    Observation,
    Result,
    StepResult,
    Trace,
    as_step_number,
    err,
    freeze_context,
    freeze_json,
    make_loom_error,
    new_context_id,
    new_loop_version,
    new_trace_id,
    ok,
    thaw_json,
)


@dataclass(frozen=True, slots=True)
class LlmMessage:
    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[LlmToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class LlmToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ToolSelectionConfig:
    """Configuration for LLM-based dynamic tool selection.

    When enabled, each loop step begins with a lightweight LLM call that
    analyzes the current context and selects a minimal tool subset from
    the full affordances. The main LLM call then only sees the selected
    tools, reducing context window usage and improving decision quality.
    """

    enabled: bool = True
    provider: Any = None  # None = reuse main provider
    max_tokens: int = 256
    min_tools: int = 1
    max_tools: int | None = None  # None = no limit
    fallback: str = "all"  # "all" | "none" | "default"
    default_tools: tuple[str, ...] = ()  # tool ids for fallback="default"
    always_include: tuple[str, ...] = ()  # tool ids always kept regardless of selection

    def __post_init__(self) -> None:
        object.__setattr__(self, "default_tools", tuple(self.default_tools))
        object.__setattr__(self, "always_include", tuple(self.always_include))


@dataclass(frozen=True, slots=True)
class LlmResponse:
    content: str | None = "{}"
    tool_calls: tuple[LlmToolCall, ...] = ()
    usage: TokenUsage = TokenUsage()
    finish_reason: str = "stop"

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))


@dataclass(frozen=True, slots=True)
class LlmStreamEvent:
    kind: str
    content_delta: str | None = None
    reasoning_delta: str | None = None
    reasoning_context_delta: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str | None = None
    response: LlmResponse | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EnvOpenAIConfig:
    model: str
    base_url: str
    api_key: str
    env_path: str


class TokenTracker:
    def __init__(self) -> None:
        self._total = TokenUsage()

    @property
    def total(self) -> TokenUsage:
        return TokenUsage(
            self._total.prompt_tokens,
            self._total.completion_tokens,
            self._total.total_tokens,
        )

    def add(self, usage: TokenUsage) -> None:
        self._total = TokenUsage(
            self._total.prompt_tokens + usage.prompt_tokens,
            self._total.completion_tokens + usage.completion_tokens,
            self._total.total_tokens + usage.total_tokens,
        )

    def is_within_budget(self, max_tokens: int | None = None) -> bool:
        return max_tokens is None or self._total.total_tokens <= max_tokens

    def reset(self) -> None:
        self._total = TokenUsage()


def create_token_tracker() -> TokenTracker:
    return TokenTracker()


def _accepts_keyword(callable_value: Any, keyword: str) -> bool:
    try:
        signature = inspect.signature(callable_value)
    except (TypeError, ValueError):
        return False
    return keyword in signature.parameters or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())


def _tool_choice_retryable(error: Any) -> bool:
    if error is None:
        return False
    message = getattr(error, "message", "")
    cause = getattr(error, "cause", None)
    text = f"{message} {cause}".lower()
    return "tool_choice" in text and ("invalidparameter" in text or "not support" in text or "unsupported" in text)


def build_system_prompt(context: Context) -> str:
    lines = [
        f"You are the loop brain for this Loom context. Your role is: {context.identity.role}.",
        "",
        "Capabilities:",
        *[f"- {_field(capability, 'id')}: {_field(capability, 'description')}" for capability in context.identity.capabilities],
        "",
        "Constraints:",
        *[f"- {_field(constraint, 'severity', 'must').upper()}: {_field(constraint, 'description')}" for constraint in context.identity.constraints],
        "",
        "Goal:",
        f"- Objective: {context.goal.objective}",
        "Success criteria:",
        *[f"- {criterion.id} ({'required' if criterion.required else 'optional'}): {criterion.description}" for criterion in context.goal.criteria],
        "",
        "Available tools:",
        *format_tools(context),
        "",
        "Output format:",
        "Return only valid JSON with this shape:",
        "\n".join(
            [
                "{",
                '  "reasoning": "why this action is the best next step",',
                '  "action": {',
                '    "kind": "tool" | "none" | "custom",',
                '    "description": "short executable action description",',
                '    "target": "tool id when kind is tool",',
                '    "input": {}',
                "  },",
                '  "alternatives": [],',
                '  "confidence": 0.0',
                "}",
            ]
        ),
    ]
    return "\n".join(lines)


def build_user_prompt(
    context: Context,
    *,
    include_history: bool = True,
    include_knowledge: bool = True,
    max_history_steps: int = 5,
) -> str:
    lines = [
        "Current loop state:",
        f"- Context id: {context.id}",
        f"- Step number: {len(context.state.observations)}",
        f"- Budget: {format_budget(context)}",
    ]
    if include_history:
        lines.extend(
            [
                "",
                "Recent observations:",
                *format_observations(context.state.observations[-max_history_steps:]),
                "",
                "Recent decisions:",
                *format_decisions(context.state.decisions[-max_history_steps:]),
            ]
        )
    if include_knowledge:
        lines.extend(
            [
                "",
                "Knowledge facts:",
                *format_knowledge(context.knowledge.facts),
                "",
                "Knowledge heuristics:",
                *format_knowledge(context.knowledge.heuristics),
            ]
        )
    lines.extend(["", "Choose the next decision and action using the required JSON format."])
    return "\n".join(lines)


def build_messages(context: Context, **options: Any) -> tuple[LlmMessage, ...]:
    return (
        LlmMessage("system", build_system_prompt(context)),
        LlmMessage("user", build_user_prompt(context, **options)),
    )


def to_llm_tool(tool: Any) -> dict[str, Any]:
    parameters = (
        thaw_json(tool.input_schema) if getattr(tool, "input_schema", None) is not None else {"type": "object", "properties": {}, "additionalProperties": True}
    )
    return {
        "type": "function",
        "function": {
            "name": tool.id,
            "description": tool.description,
            "parameters": parameters,
        },
    }


def to_llm_tools(tools: tuple[Any, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(to_llm_tool(tool) for tool in tools)


def create_llm_step_function(
    provider: Any,
    *,
    prompt_options: dict[str, Any] | None = None,
    enable_tool_calling: bool = True,
    max_tool_calls_per_step: int | None = 5,
    required_tools: tuple[str, ...] = (),
    tool_selection: ToolSelectionConfig | None = None,
    tool_resolver: Any = None,
    stream: bool = False,
):
    async def llm_step(context: Context, runtime: Any) -> Result:
        started_at = runtime.now()
        trace_id = getattr(runtime, "trace_id", None) or new_trace_id()
        tracker = create_token_tracker()

        # ── Phase 0: Tool Resolution ───────────────────────────────────
        all_tools = context.affordances.tools
        prompt_context = context
        tool_resolution_metadata: dict[str, Any] | None = None

        if enable_tool_calling and tool_resolver is not None:
            resolved = tool_resolver(context)
            if inspect.isawaitable(resolved):
                resolved = await resolved
            if isinstance(resolved, Result):
                if not resolved.ok:
                    return resolved
                resolved = resolved.value
            all_tools, tool_resolution_metadata = _normalize_tool_resolution(all_tools, resolved)
            prompt_context = replace(context, affordances=replace(context.affordances, tools=all_tools))

        # ── Phase 1: Tool Selection ────────────────────────────────────
        effective_tools = all_tools
        tool_selection_result: ToolSelectionResult | None = None

        if enable_tool_calling and all_tools and tool_selection is not None and tool_selection.enabled:
            selection_provider = tool_selection.provider or provider
            tool_selection_result = await _select_tools(prompt_context, selection_provider, tool_selection, runtime, trace_id)

            # Build the effective tool list from selection
            selected_ids = set(tool_selection_result.selected_tools)
            effective_tools = tuple(t for t in all_tools if t.id in selected_ids)

        tools = to_llm_tools(effective_tools) if enable_tool_calling and effective_tools else None
        effective_tool_ids = frozenset(tool.id for tool in effective_tools)
        pending_required_tools = {tool_id for tool_id in required_tools if tool_id in effective_tool_ids}
        messages = list(build_messages(prompt_context, **(prompt_options or {})))
        final_response: LlmResponse | None = None
        llm_call_count = 0
        tool_call_count = 0
        tool_observations: list[Observation] = []

        while True:
            llm_call_count += 1
            llm_call_id = f"{trace_id}-llm-{llm_call_count}"
            tool_choice = "required" if tools and pending_required_tools else None
            requested = await _emit_runtime_event(
                runtime,
                {
                    "type": "llm.requested",
                    "run_id": context.run_id,
                    "loop_id": runtime.loop_id,
                    "trace_id": trace_id,
                    "llm_call_id": llm_call_id,
                    "step_number": as_step_number(len(context.state.observations)),
                    "model": provider.model,
                    "messages": tuple(messages),
                    "tools": tools,
                    "tool_choice": tool_choice,
                    "at": runtime.now(),
                },
            )
            if not requested.ok:
                return requested
            response = await _chat_or_stream(
                provider,
                messages,
                tools,
                getattr(runtime, "cancellation", None),
                runtime=runtime,
                context=context,
                trace_id=trace_id,
                llm_call_id=llm_call_id,
                tool_choice=tool_choice,
                stream=stream,
            )
            if not response.ok:
                failed = await _emit_runtime_event(
                    runtime,
                    {
                        "type": "llm.failed",
                        "run_id": context.run_id,
                        "loop_id": runtime.loop_id,
                        "trace_id": trace_id,
                        "llm_call_id": llm_call_id,
                        "step_number": as_step_number(len(context.state.observations)),
                        "model": provider.model,
                        "error": response.error,
                        "at": runtime.now(),
                    },
                )
                if not failed.ok:
                    return failed
                return response
            final_response = response.value
            completed = await _emit_runtime_event(
                runtime,
                {
                    "type": "llm.completed",
                    "run_id": context.run_id,
                    "loop_id": runtime.loop_id,
                    "trace_id": trace_id,
                    "llm_call_id": llm_call_id,
                    "step_number": as_step_number(len(context.state.observations)),
                    "model": provider.model,
                    "response": final_response,
                    "at": runtime.now(),
                },
            )
            if not completed.ok:
                return completed
            tracker.add(final_response.usage)
            if not tracker.is_within_budget(context.goal.budget.max_tokens):
                return _token_budget_exceeded(tracker.total, context.goal.budget.max_tokens)

            if enable_tool_calling and final_response.tool_calls:
                if max_tool_calls_per_step is not None and tool_call_count >= max_tool_calls_per_step:
                    return _max_tool_calls_exceeded(max_tool_calls_per_step)

                messages.append(
                    LlmMessage(
                        "assistant",
                        "" if final_response.content is None else final_response.content,
                        tool_calls=final_response.tool_calls,
                    )
                )
                native_tool_results = []
                for tool_call in final_response.tool_calls:
                    if max_tool_calls_per_step is not None and tool_call_count >= max_tool_calls_per_step:
                        return _max_tool_calls_exceeded(max_tool_calls_per_step)
                    parsed_input = _parse_tool_arguments(tool_call)
                    if not parsed_input.ok:
                        return parsed_input
                    observation = await runtime.call_tool(
                        tool_call.name,
                        parsed_input.value,
                        metadata={
                            "tool_call_id": tool_call.id,
                            "tool_name": tool_call.name,
                            "model": provider.model,
                        },
                    )
                    if not observation.ok:
                        return observation
                    tool_call_count += 1
                    pending_required_tools.discard(tool_call.name)
                    tool_observations.append(observation.value)
                    native_tool_results.append((tool_call, observation.value))
                    messages.append(
                        LlmMessage(
                            "tool",
                            json.dumps(thaw_json(observation.value.value), separators=(",", ":")),
                            name=tool_call.name,
                            tool_call_id=tool_call.id,
                        )
                    )
                messages.append(_native_tool_execution_transcript(native_tool_results))
                continue

            json_tool_actions = ()
            if enable_tool_calling:
                json_tool_actions = _json_tool_actions(_parse_decision(final_response.content, trace_id), effective_tool_ids)
            if not json_tool_actions:
                break
            json_tool_results = []
            for json_tool_action in json_tool_actions:
                if max_tool_calls_per_step is not None and tool_call_count >= max_tool_calls_per_step:
                    return _max_tool_calls_exceeded(max_tool_calls_per_step)

                tool_call_id = f"{llm_call_id}-json-tool-{tool_call_count + 1}"
                tool_input = {} if json_tool_action.input is None else thaw_json(json_tool_action.input)
                observation = await runtime.call_tool(
                    json_tool_action.target,
                    tool_input,
                    metadata={
                        "tool_call_id": tool_call_id,
                        "tool_name": json_tool_action.target,
                        "tool_call_source": "json_action",
                        "model": provider.model,
                    },
                )
                if not observation.ok:
                    return observation
                tool_call_count += 1
                pending_required_tools.discard(json_tool_action.target or "")
                tool_observations.append(observation.value)
                json_tool_results.append((json_tool_action, observation.value))
            messages.append(LlmMessage("assistant", "" if final_response.content is None else final_response.content))
            messages.append(LlmMessage("assistant", _json_tool_result_feedback(tuple(json_tool_results))))

        if final_response is None:
            return err(make_loom_error("LLM_FAILED", "LLM provider returned no response", retryable=False))

        # Include tool selection tokens in the tracker
        if tool_selection_result is not None:
            tracker.add(tool_selection_result.token_usage)

        parsed = _parse_decision(final_response.content, trace_id)
        ended_at = runtime.now()

        # Build trace metadata including tool selection info
        trace_metadata: dict[str, Any] = {
            "model": provider.model,
            "finishReason": final_response.finish_reason,
            "tokenUsage": _token_usage_metadata(tracker.total),
            "streaming": bool(stream and hasattr(provider, "stream_chat")),
        }
        if tool_resolution_metadata is not None:
            trace_metadata["toolResolution"] = tool_resolution_metadata
        if tool_selection_result is not None:
            trace_metadata["toolSelection"] = {
                "selected": tool_selection_result.selected_tools,
                "reasoning": tool_selection_result.reasoning,
                "confidence": tool_selection_result.confidence,
                "model": tool_selection_result.model,
                "duration_ms": tool_selection_result.duration_ms,
                "fallback": tool_selection_result.fallback,
                "tokenUsage": _token_usage_metadata(tool_selection_result.token_usage),
            }

        llm_observation = Observation(
            f"{trace_id}-llm-observation",
            "llm",
            parsed["output"],
            ended_at,
            metadata={
                "model": provider.model,
                "finishReason": final_response.finish_reason,
                "tokenUsage": _token_usage_metadata(tracker.total),
            },
        )
        decision = Decision(
            f"{trace_id}-decision",
            parsed["action"],
            parsed["reasoning"],
            tuple(parsed["alternatives"]),
            parsed["confidence"],
            ended_at,
            metadata={
                "model": provider.model,
                "finishReason": final_response.finish_reason,
                "parseFallback": parsed["parse_fallback"],
                "tokenUsage": _token_usage_metadata(tracker.total),
            },
        )
        observations = (*tool_observations, llm_observation)
        next_context = freeze_context(
            replace(
                context,
                id=new_context_id(),
                state=replace(
                    context.state,
                    observations=(*context.state.observations, *observations),
                    decisions=(*context.state.decisions, decision),
                ),
            )
        )
        trace = Trace(
            id=trace_id,
            run_id=context.run_id,
            loop_id=runtime.loop_id,
            loop_version=new_loop_version(),
            step_number=as_step_number(len(context.state.observations)),
            root_trace_id=trace_id,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=0,
            input_context_id=context.id,
            output_context_id=next_context.id,
            outcome="pass",
            observations=observations,
            decisions=(decision,),
            actions=(decision.action,),
            tags=("llm",),
            metadata=trace_metadata,
        )
        emitted = await _emit_llm_events(runtime, trace, observations, decision)
        if not emitted.ok:
            return emitted
        return ok(StepResult(next_context, trace, llm_observation, parsed["output"]))

    return llm_step


async def _chat_or_stream(
    provider: Any,
    messages: list[LlmMessage],
    tools: tuple[dict[str, Any], ...] | None,
    cancellation: Any,
    *,
    runtime: Any,
    context: Context,
    trace_id: str,
    llm_call_id: str,
    tool_choice: Any,
    stream: bool,
) -> Result:
    if not stream or not hasattr(provider, "stream_chat"):
        if tool_choice is not None and _accepts_keyword(provider.chat, "tool_choice"):
            response = await provider.chat(messages, tools=tools, cancellation=cancellation, tool_choice=tool_choice)
            if response.ok or not _tool_choice_retryable(response.error):
                return response
        return await provider.chat(messages, tools, cancellation)
    response = await _consume_streaming_chat(
        provider,
        messages,
        tools,
        cancellation,
        runtime,
        context,
        trace_id,
        llm_call_id,
        tool_choice,
    )
    if response.ok or tool_choice is None or not _tool_choice_retryable(response.error):
        return response
    return await _consume_streaming_chat(
        provider,
        messages,
        tools,
        cancellation,
        runtime,
        context,
        trace_id,
        llm_call_id,
        None,
    )


async def _consume_streaming_chat(
    provider: Any,
    messages: list[LlmMessage],
    tools: tuple[dict[str, Any], ...] | None,
    cancellation: Any,
    runtime: Any,
    context: Context,
    trace_id: str,
    llm_call_id: str,
    tool_choice: Any,
) -> Result:
    step_number = as_step_number(len(context.state.observations))
    started = await _emit_runtime_event(
        runtime,
        {
            "type": "llm.stream.started",
            "run_id": context.run_id,
            "loop_id": runtime.loop_id,
            "trace_id": trace_id,
            "llm_call_id": llm_call_id,
            "step_number": step_number,
            "model": provider.model,
            "at": runtime.now(),
        },
    )
    if not started.ok:
        return started

    final_response: LlmResponse | None = None
    try:
        stream_kwargs = {"tools": tools, "cancellation": cancellation}
        if tool_choice is not None and _accepts_keyword(provider.stream_chat, "tool_choice"):
            stream_kwargs["tool_choice"] = tool_choice
        async for event in provider.stream_chat(messages, **stream_kwargs):
            if event.kind == "completed":
                final_response = event.response
                continue
            emitted = await _emit_stream_delta_event(runtime, event, context, trace_id, llm_call_id, provider.model, step_number)
            if not emitted.ok:
                return emitted
    except BaseException as exc:
        return err(
            make_loom_error(
                "LLM_FAILED",
                str(exc),
                retryable=True,
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        )

    if final_response is None:
        return err(make_loom_error("LLM_FAILED", "Streaming provider returned no completed response", retryable=True))

    completed = await _emit_runtime_event(
        runtime,
        {
            "type": "llm.stream.completed",
            "run_id": context.run_id,
            "loop_id": runtime.loop_id,
            "trace_id": trace_id,
            "llm_call_id": llm_call_id,
            "step_number": step_number,
            "model": provider.model,
            "at": runtime.now(),
        },
    )
    if not completed.ok:
        return completed
    return ok(final_response)


async def _emit_stream_delta_event(
    runtime: Any,
    event: LlmStreamEvent,
    context: Context,
    trace_id: str,
    llm_call_id: str,
    model: str,
    step_number: int,
) -> Result:
    event_type = {
        "content.delta": "llm.content.delta",
        "reasoning.delta": "llm.reasoning.delta",
        "reasoning_context.delta": "llm.reasoning_context.delta",
        "tool_call.started": "llm.tool_call.started",
        "tool_call.arguments.delta": "llm.tool_call.arguments.delta",
        "tool_call.completed": "llm.tool_call.completed",
    }.get(event.kind)
    if event_type is None:
        return ok(None)
    return await _emit_runtime_event(
        runtime,
        {
            "type": event_type,
            "run_id": context.run_id,
            "loop_id": runtime.loop_id,
            "trace_id": trace_id,
            "llm_call_id": llm_call_id,
            "step_number": step_number,
            "model": model,
            "delta": event.content_delta or event.reasoning_delta or event.reasoning_context_delta or event.tool_arguments_delta,
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "raw": event.raw,
            "at": runtime.now(),
        },
    )


@dataclass(frozen=True, slots=True)
class OpenAIProvider:
    api_key: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    base_url: str = "https://api.openai.com/v1"
    http_client: Any = None

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None) -> Result:
        base_url = self.base_url.rstrip("/")
        body = {
            "model": self.model,
            "messages": [_to_openai_message(message) for message in messages],
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        if tools:
            body["tools"] = tools
        if tools and tool_choice is not None:
            body["tool_choice"] = tool_choice

        request = {
            "method": "POST",
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            "body": body,
        }
        url = f"{base_url}/chat/completions"
        try:
            response = await self._send(url, request)
        except BaseException as exc:
            return err(
                make_loom_error(
                    "LLM_FAILED",
                    str(exc),
                    retryable=True,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                )
            )

        if not response.get("ok", False):
            status = int(response.get("status", 0))
            payload = response.get("json") or {}
            message = (payload.get("error", {}).get("message") if isinstance(payload, dict) else None) or f"OpenAI request failed with status {status}"
            return err(
                make_loom_error(
                    "LLM_FAILED",
                    message,
                    retryable=status == 429 or status >= 500,
                    cause={"status": status, "body": payload},
                )
            )

        return _parse_openai_chat_response(response.get("json"))

    async def stream_chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        base_url = self.base_url.rstrip("/")
        body = {
            "model": self.model,
            "messages": [_to_openai_message(message) for message in messages],
            "stream": True,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        if tools:
            body["tools"] = tools
        if tools and tool_choice is not None:
            body["tool_choice"] = tool_choice

        request = {
            "method": "POST",
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            "body": body,
        }
        response = await self._send_stream(f"{base_url}/chat/completions", request)
        if not response.get("ok", False):
            status = int(response.get("status", 0))
            payload = response.get("json") or {}
            message = (payload.get("error", {}).get("message") if isinstance(payload, dict) else None) or f"OpenAI stream request failed with status {status}"
            raise RuntimeError(message)

        async for event in _parse_openai_sse_stream(response.get("chunks", ())):
            yield event

    async def _send(self, url: str, request: dict[str, Any]) -> dict[str, Any]:
        if self.http_client is not None:
            return await self.http_client(url, request)

        def send_sync() -> dict[str, Any]:
            req = urllib.request.Request(
                url,
                data=json.dumps(request["body"]).encode("utf-8"),
                method="POST",
                headers=request["headers"],
            )
            try:
                with urllib.request.urlopen(req) as response:  # noqa: S310
                    payload = json.loads(response.read().decode("utf-8"))
                    return {"status": response.status, "ok": 200 <= response.status < 300, "json": payload}
            except urllib.error.HTTPError as exc:
                payload = json.loads(exc.read().decode("utf-8"))
                return {"status": exc.code, "ok": False, "json": payload}

        import asyncio

        return await asyncio.to_thread(send_sync)

    async def _send_stream(self, url: str, request: dict[str, Any]) -> dict[str, Any]:
        if self.http_client is not None:
            return await self.http_client(url, request)

        return {"status": 200, "ok": True, "chunks": _urlopen_stream_chunks(url, request)}


def create_openai_provider(**config: Any) -> OpenAIProvider:
    return OpenAIProvider(**config)


def load_env_openai_config(
    *,
    env_path: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> Result:
    path = Path(env_path) if env_path is not None else Path(os.environ.get("LOOM_ENV_FILE", ".env"))
    dotenv = _read_dotenv(path)
    effective_env = {**dotenv, **dict(os.environ if env is None else env)}

    model_name = model or _first_env(effective_env, ("LOOM_LLM_MODEL", "OPENAI_MODEL"))
    resolved_base_url = base_url or _first_env(effective_env, ("LOOM_LLM_BASE_URL", "OPENAI_BASE_URL"))
    resolved_api_key = api_key or _first_env(effective_env, ("LOOM_LLM_API_KEY", "OPENAI_API_KEY"))

    if not model_name:
        return err(make_loom_error("VALIDATION_FAILED", "LLM model is missing; set LOOM_LLM_MODEL in .env", retryable=False))
    if not resolved_base_url:
        return err(make_loom_error("VALIDATION_FAILED", "LLM base URL is missing; set LOOM_LLM_BASE_URL in .env", retryable=False))
    if not resolved_api_key:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "LLM API key is missing; set LOOM_LLM_API_KEY in .env",
                retryable=False,
            )
        )

    return ok(
        EnvOpenAIConfig(
            model=model_name,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            env_path=str(path),
        )
    )


def create_env_openai_provider(
    *,
    env_path: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    http_client: Any = None,
) -> Result:
    config = load_env_openai_config(
        env_path=env_path,
        env=env,
        api_key=api_key,
        model=model,
        base_url=base_url,
    )
    if not config.ok:
        return config
    return ok(
        create_openai_provider(
            api_key=config.value.api_key,
            model=config.value.model,
            base_url=config.value.base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            http_client=http_client,
        )
    )


def format_tools(context: Context) -> list[str]:
    if not context.affordances.tools:
        return ["- none"]
    return [f"- {tool.id}: {tool.description}" for tool in context.affordances.tools]


def format_budget(context: Context) -> str:
    budget = context.goal.budget
    return ", ".join(
        [
            f"maxSteps: {'unbounded' if budget.max_steps is None else budget.max_steps}",
            f"maxDurationMs: {'unbounded' if budget.max_duration_ms is None else budget.max_duration_ms}",
            f"maxTokens: {'unbounded' if budget.max_tokens is None else budget.max_tokens}",
            f"maxCostUsd: {'unbounded' if budget.max_cost_usd is None else budget.max_cost_usd}",
        ]
    )


def format_observations(observations) -> list[str]:
    if not observations:
        return ["- none"]
    return [
        f"- {observation.id} from {observation.source} at {observation.at}: {json.dumps(thaw_json(observation.value), indent=2)}"
        for observation in observations
    ]


def format_decisions(decisions) -> list[str]:
    if not decisions:
        return ["- none"]
    return [f"- {decision.id} at {decision.at}: {decision.reasoning}; action={decision.action.kind} {decision.action.description}" for decision in decisions]


def format_knowledge(items) -> list[str]:
    if not items:
        return ["- none"]
    return [f"- {item.id} ({item.kind}, confidence {item.confidence}): {json.dumps(thaw_json(item.content), indent=2)}" for item in items]


def _field(value: Any, key: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


# ─── Tool Selection ────────────────────────────────────────────────────


def _normalize_tool_resolution(original_tools: tuple[Any, ...], resolved: Any) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if hasattr(resolved, "tools"):
        tools = tuple(resolved.tools)
        included = tuple(getattr(resolved, "included_ids", tuple(tool.id for tool in tools)))
        pruned = tuple(getattr(resolved, "pruned_ids", tuple(tool.id for tool in original_tools if tool.id not in included)))
        metadata = {
            "included": included,
            "pruned": pruned,
            "tokenEstimate": getattr(resolved, "token_estimate", None),
            "overBudget": getattr(resolved, "over_budget", False),
        }
        return tools, metadata

    tools = tuple(resolved)
    included = tuple(tool.id for tool in tools)
    pruned = tuple(tool.id for tool in original_tools if tool.id not in included)
    return tools, {"included": included, "pruned": pruned, "tokenEstimate": None, "overBudget": False}


def build_tool_selection_prompt(
    context: Context,
    *,
    max_history_steps: int = 3,
) -> str:
    """Build a prompt asking the LLM to select the minimal tool set for the next step.

    The LLM receives the goal, recent observations/decisions, and the full tool
    catalog. It returns a JSON with selected tool IDs and reasoning.
    """
    lines = [
        "You are a tool selection assistant.",
        "",
        f"Goal: {context.goal.objective}",
        f"Current step: {len(context.state.observations)}",
    ]

    # Recent context
    recent_obs = context.state.observations[-max_history_steps:]
    if recent_obs:
        lines.extend(["", "Recent observations:", *format_observations(recent_obs)])

    recent_dec = context.state.decisions[-max_history_steps:]
    if recent_dec:
        lines.extend(["", "Recent decisions:", *format_decisions(recent_dec)])

    # Available tools
    lines.extend(["", "Available tools:"])
    for tool in context.affordances.tools:
        schema_hint = ""
        if tool.input_schema:
            try:
                schema = thaw_json(tool.input_schema)
                props = schema.get("properties", {})
                if props:
                    schema_hint = f" (params: {', '.join(props.keys())})"
            except Exception:
                pass
        lines.append(f"- {tool.id}: {tool.description}{schema_hint}")

    lines.extend(
        [
            "",
            "Select the MINIMAL tool set needed for the NEXT step.",
            "Return ONLY valid JSON with this shape:",
            "",
            "{",
            '  "reasoning": "why these tools are the best choice",',
            '  "selected_tools": ["tool-id-1", "tool-id-2"],',
            '  "confidence": 0.9',
            "}",
            "",
            "Rules:",
            "- Select at least 1 tool.",
            "- Prefer fewer tools when possible.",
            "- Only use tool IDs from the available list above.",
        ]
    )

    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ToolSelectionResult:
    selected_tools: tuple[str, ...]
    reasoning: str
    confidence: float
    token_usage: TokenUsage
    duration_ms: int
    model: str
    fallback: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_tools", tuple(self.selected_tools))


def _fallback_tool_selection(
    config: ToolSelectionConfig,
    all_tool_ids: tuple[str, ...],
    reason: str,
) -> ToolSelectionResult:
    """Return a fallback tool selection when the LLM call fails."""
    if config.fallback == "none":
        selected: tuple[str, ...] = ()
    elif config.fallback == "default" and config.default_tools:
        selected = tuple(tid for tid in config.default_tools if tid in all_tool_ids)
    else:  # "all"
        selected = all_tool_ids

    # Always include always_include tools
    always = tuple(tid for tid in config.always_include if tid in all_tool_ids)
    selected = tuple(dict.fromkeys(selected + always))

    return ToolSelectionResult(
        selected_tools=selected,
        reasoning=f"Fallback ({config.fallback}): {reason}",
        confidence=0.0,
        token_usage=TokenUsage(),
        duration_ms=0,
        model="fallback",
        fallback=True,
    )


async def _select_tools(
    context: Context,
    provider: Any,
    config: ToolSelectionConfig,
    runtime: Any,
    trace_id: str,
) -> ToolSelectionResult:
    """Run a lightweight LLM call to select tools for the current step.

    Returns a ToolSelectionResult with the selected tool IDs.
    On any failure, returns a fallback selection based on config.fallback.
    """
    import time

    all_tool_ids = tuple(tool.id for tool in context.affordances.tools)

    # Short circuit: not enough tools to bother selecting
    if len(all_tool_ids) <= max(config.min_tools, 2):
        return ToolSelectionResult(
            selected_tools=all_tool_ids,
            reasoning="Tool count below selection threshold, using all.",
            confidence=1.0,
            token_usage=TokenUsage(),
            duration_ms=0,
            model=provider.model,
            fallback=False,
        )

    prompt = build_tool_selection_prompt(context)
    selection_messages = (
        LlmMessage("system", "You are a tool selection assistant. Return ONLY valid JSON."),
        LlmMessage("user", prompt),
    )

    started_ms = time.monotonic()
    selection_call_id = f"{trace_id}-tool-selection"
    step_number = as_step_number(len(context.state.observations))

    # Emit selection request event
    await _emit_runtime_event(
        runtime,
        {
            "type": "tool_selection.requested",
            "run_id": context.run_id,
            "loop_id": runtime.loop_id,
            "trace_id": trace_id,
            "selection_call_id": selection_call_id,
            "step_number": step_number,
            "model": provider.model,
            "available_tools": all_tool_ids,
            "at": runtime.now(),
        },
    )

    # Call LLM (no tools in this call — it's a pure text response)
    response = await provider.chat(selection_messages, tools=None, cancellation=getattr(runtime, "cancellation", None))

    duration_ms = int((time.monotonic() - started_ms) * 1000)

    if not response.ok:
        await _emit_runtime_event(
            runtime,
            {
                "type": "tool_selection.failed",
                "run_id": context.run_id,
                "loop_id": runtime.loop_id,
                "trace_id": trace_id,
                "selection_call_id": selection_call_id,
                "step_number": step_number,
                "model": provider.model,
                "error": response.error,
                "duration_ms": duration_ms,
                "at": runtime.now(),
            },
        )
        return _fallback_tool_selection(config, all_tool_ids, f"LLM call failed: {response.error.message}")

    llm_response = response.value
    content = llm_response.content or ""
    usage = llm_response.usage

    # Parse the selection
    parsed = _parse_json_object(content)
    if parsed is None:
        await _emit_runtime_event(
            runtime,
            {
                "type": "tool_selection.failed",
                "run_id": context.run_id,
                "loop_id": runtime.loop_id,
                "trace_id": trace_id,
                "selection_call_id": selection_call_id,
                "step_number": step_number,
                "model": provider.model,
                "error": make_loom_error("LLM_PARSE_ERROR", "Failed to parse tool selection JSON", retryable=False),
                "raw_content": content,
                "duration_ms": duration_ms,
                "at": runtime.now(),
            },
        )
        return _fallback_tool_selection(config, all_tool_ids, "LLM returned unparseable response")

    # Extract selected tool IDs
    raw_selected = parsed.get("selected_tools", [])
    if isinstance(raw_selected, str):
        raw_selected = [raw_selected]
    if not isinstance(raw_selected, list | tuple):
        raw_selected = []

    # Validate: only keep tools that actually exist
    valid_selected = tuple(tid for tid in raw_selected if tid in all_tool_ids)
    reasoning = parsed.get("reasoning", "No reasoning provided")
    confidence = parsed.get("confidence", 0.0)
    if not isinstance(confidence, int | float):
        confidence = 0.0

    # Enforce min_tools
    if len(valid_selected) < config.min_tools:
        # Add tools from the full set until we meet the minimum
        remaining = tuple(tid for tid in all_tool_ids if tid not in valid_selected)
        valid_selected = valid_selected + remaining[: config.min_tools - len(valid_selected)]

    # Enforce max_tools
    if config.max_tools is not None and len(valid_selected) > config.max_tools:
        valid_selected = valid_selected[: config.max_tools]

    # Always include always_include tools
    always = tuple(tid for tid in config.always_include if tid in all_tool_ids and tid not in valid_selected)
    valid_selected = valid_selected + always

    result = ToolSelectionResult(
        selected_tools=valid_selected,
        reasoning=reasoning,
        confidence=max(0.0, min(1.0, float(confidence))),
        token_usage=usage,
        duration_ms=duration_ms,
        model=provider.model,
        fallback=False,
    )

    # Emit selection decided event
    await _emit_runtime_event(
        runtime,
        {
            "type": "tool_selection.decided",
            "run_id": context.run_id,
            "loop_id": runtime.loop_id,
            "trace_id": trace_id,
            "selection_call_id": selection_call_id,
            "step_number": step_number,
            "model": provider.model,
            "available_tools": all_tool_ids,
            "selected_tools": valid_selected,
            "excluded_tools": tuple(tid for tid in all_tool_ids if tid not in valid_selected),
            "reasoning": reasoning,
            "confidence": result.confidence,
            "token_usage": _token_usage_metadata(usage),
            "duration_ms": duration_ms,
            "raw_content": content,
            "at": runtime.now(),
        },
    )

    return result


def _parse_tool_arguments(tool_call: LlmToolCall) -> Result:
    try:
        return ok(freeze_json(json.loads(tool_call.arguments)))
    except BaseException as exc:
        return err(
            make_loom_error(
                "LLM_PARSE_ERROR",
                f"Failed to parse tool call {tool_call.id} arguments",
                retryable=False,
                cause={"toolCallId": tool_call.id, "cause": str(exc)},
            )
        )


def _native_tool_execution_transcript(results: list[tuple[LlmToolCall, Observation]]) -> LlmMessage:
    payload = [
        {
            "tool_call_id": tool_call.id,
            "tool": tool_call.name,
            "arguments": _json_or_text(tool_call.arguments),
            "result": thaw_json(observation.value),
        }
        for tool_call, observation in results
    ]
    return LlmMessage("assistant", "Tool execution transcript:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _json_or_text(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _json_tool_actions(parsed: Mapping[str, Any], available_tool_ids: frozenset[str]) -> tuple[Action, ...]:
    action = parsed.get("action")
    if not isinstance(action, Action):
        return ()
    if action.kind != "tool" or not action.target:
        return ()
    targets = _resolve_json_tool_targets(action.target, available_tool_ids)
    if not targets:
        return ()
    if targets == (action.target,):
        return (action,)
    return tuple(replace(action, id=f"{action.id}-{index + 1}", target=target) for index, target in enumerate(targets))


def _resolve_json_tool_targets(target: str, available_tool_ids: frozenset[str]) -> tuple[str, ...]:
    if target in available_tool_ids:
        return (target,)

    matches: list[tuple[int, str]] = []
    for tool_id in available_tool_ids:
        pattern = rf"(?<![A-Za-z0-9_-]){re.escape(tool_id)}(?![A-Za-z0-9_-])"
        matches.extend((match.start(), tool_id) for match in re.finditer(pattern, target))

    ordered = []
    seen = set()
    for _position, tool_id in sorted(matches, key=lambda item: item[0]):
        if tool_id not in seen:
            ordered.append(tool_id)
            seen.add(tool_id)
    return tuple(ordered)


def _json_tool_result_feedback(results: tuple[tuple[Action, Observation], ...]) -> str:
    payload = [
        {
            "tool": action.target,
            "input": None if action.input is None else thaw_json(action.input),
            "result": thaw_json(observation.value),
        }
        for action, observation in results
    ]
    return "Tool execution transcript:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _parse_decision(content: str | None, trace_id: str) -> dict[str, Any]:
    if content is None or not content.strip():
        return _fallback_decision("LLM returned no decision content", trace_id)
    try:
        parsed = _parse_json_object(content)
    except BaseException:
        return _fallback_decision(content, trace_id)
    if parsed is None:
        return _fallback_decision(content, trace_id)
    action = _parse_action(parsed.get("action"), f"{trace_id}-action", "LLM selected action")
    alternatives = tuple(
        _parse_action(item, f"{trace_id}-alternative-{index + 1}", "Alternative action") for index, item in enumerate(parsed.get("alternatives") or [])
    )
    confidence = parsed.get("confidence", 0)
    if not isinstance(confidence, int | float):
        confidence = 0
    return {
        "output": freeze_json(parsed),
        "reasoning": parsed.get("reasoning", content),
        "action": action,
        "alternatives": alternatives,
        "confidence": max(0, min(1, float(confidence))),
        "parse_fallback": False,
    }


def _parse_json_object(content: str) -> dict[str, Any] | None:
    for candidate in _json_candidates(content):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _json_candidates(content: str):
    stripped = content.strip()
    yield stripped

    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
        yield "\n".join(lines[1:-1]).strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        yield stripped[start : end + 1]


def _fallback_decision(reasoning: str, trace_id: str) -> dict[str, Any]:
    action = Action(
        f"{trace_id}-action",
        "custom",
        "Use unstructured LLM response",
        input={"content": reasoning},
    )
    return {
        "output": {
            "reasoning": reasoning,
            "action": {
                "kind": action.kind,
                "description": action.description,
                "input": {"content": reasoning},
            },
            "alternatives": [],
            "confidence": 0,
        },
        "reasoning": reasoning,
        "action": action,
        "alternatives": (),
        "confidence": 0,
        "parse_fallback": True,
    }


def _parse_action(value: Any, action_id: str, fallback_description: str) -> Action:
    if not isinstance(value, dict):
        return Action(action_id, "custom", fallback_description)
    kind = value.get("kind")
    if kind not in {"tool", "loop", "context", "knowledge", "none", "custom"}:
        kind = "custom"
    description = value.get("description") or fallback_description
    return Action(
        action_id,
        kind,
        description,
        input=value.get("input"),
        target=value.get("target"),
    )


async def _emit_llm_events(runtime: Any, trace: Trace, observations, decision: Decision) -> Result:
    events = [
        {
            "type": "decision.recorded",
            "run_id": trace.run_id,
            "loop_id": trace.loop_id,
            "trace_id": trace.id,
            "step_number": trace.step_number,
            "decision": decision,
            "at": decision.at,
        },
        {
            "type": "action.started",
            "run_id": trace.run_id,
            "loop_id": trace.loop_id,
            "trace_id": trace.id,
            "step_number": trace.step_number,
            "action": decision.action,
            "at": decision.at,
        },
        *[
            {
                "type": "observation.recorded",
                "run_id": trace.run_id,
                "loop_id": trace.loop_id,
                "trace_id": trace.id,
                "step_number": trace.step_number,
                "observation": observation,
                "at": observation.at,
            }
            for observation in observations
        ],
        {
            "type": "action.completed",
            "run_id": trace.run_id,
            "loop_id": trace.loop_id,
            "trace_id": trace.id,
            "step_number": trace.step_number,
            "action": decision.action,
            "outcome": trace.outcome,
            "at": trace.ended_at,
        },
    ]
    for event in events:
        emitted = await _emit_runtime_event(runtime, event)
        if not emitted.ok:
            return emitted
    return ok(None)


async def _emit_runtime_event(runtime: Any, event: Mapping[str, Any]) -> Result:
    emitted = runtime.trace_sink.emit(event)
    if inspect.isawaitable(emitted):
        emitted = await emitted
    return emitted


def _token_usage_metadata(usage: TokenUsage) -> dict[str, int]:
    return {
        "promptTokens": usage.prompt_tokens,
        "completionTokens": usage.completion_tokens,
        "totalTokens": usage.total_tokens,
    }


def _token_budget_exceeded(usage: TokenUsage, max_tokens: int | None) -> Result:
    return err(
        make_loom_error(
            "TOKEN_BUDGET_EXCEEDED",
            "LLM token budget exceeded",
            retryable=False,
            cause={"maxTokens": max_tokens, "usage": _token_usage_metadata(usage)},
        )
    )


def _max_tool_calls_exceeded(max_tool_calls: int) -> Result:
    return err(
        make_loom_error(
            "LLM_FAILED",
            "Maximum LLM tool calls per step exceeded",
            retryable=False,
            cause={"maxToolCalls": max_tool_calls},
        )
    )


def _to_openai_message(message: Any) -> dict[str, Any]:
    role = _field(message, "role")
    content = _field(message, "content")
    tool_calls = _field(message, "tool_calls", ())
    result = {"role": role, "content": None if role == "assistant" and tool_calls and content == "" else content}
    name = _field(message, "name", None)
    tool_call_id = _field(message, "tool_call_id", None)
    if name is not None:
        result["name"] = name
    if tool_call_id is not None:
        result["tool_call_id"] = tool_call_id
    if tool_calls:
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in tool_calls
        ]
    return result


def _parse_openai_chat_response(payload: Any) -> Result:
    try:
        choice = payload["choices"][0]
        message = choice["message"]
    except BaseException:
        return err(
            make_loom_error(
                "LLM_FAILED",
                "OpenAI response did not include a chat message",
                retryable=False,
                cause=payload,
            )
        )
    usage = payload.get("usage", {})
    return ok(
        LlmResponse(
            content=message.get("content"),
            tool_calls=tuple(_parse_openai_tool_calls(message.get("tool_calls"))),
            usage=TokenUsage(
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            ),
            finish_reason=choice.get("finish_reason", "unknown"),
        )
    )


async def _parse_openai_sse_stream(chunks: Any):
    content_parts: list[str] = []
    tool_parts: dict[int, dict[str, Any]] = {}
    finish_reason = "stop"
    usage = TokenUsage()

    async for payload in _iter_sse_payloads(chunks):
        if payload == "[DONE]":
            break
        data = json.loads(payload)
        usage_data = data.get("usage") or {}
        if usage_data:
            usage = TokenUsage(
                usage_data.get("prompt_tokens", 0),
                usage_data.get("completion_tokens", 0),
                usage_data.get("total_tokens", 0),
            )
        choices = data.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, Mapping):
            continue
        delta = choice.get("delta") or {}
        if not isinstance(delta, Mapping):
            delta = {}
        finish_reason = choice.get("finish_reason") or finish_reason

        content = delta.get("content")
        if content:
            content_parts.append(content)
            yield LlmStreamEvent(kind="content.delta", content_delta=content, raw=data)

        for stream_event in _parse_openai_stream_tool_deltas(delta, tool_parts, data):
            yield stream_event

        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
        if reasoning:
            yield LlmStreamEvent(kind="reasoning.delta", reasoning_delta=reasoning, raw=data)

        reasoning_context = delta.get("reasoning_context")
        if reasoning_context:
            yield LlmStreamEvent(kind="reasoning_context.delta", reasoning_context_delta=reasoning_context, raw=data)

    yield LlmStreamEvent(
        kind="completed",
        response=LlmResponse(
            content="".join(content_parts) or None,
            tool_calls=tuple(_assembled_openai_stream_tool_calls(tool_parts)),
            usage=usage,
            finish_reason=finish_reason,
        ),
    )


async def _iter_sse_payloads(chunks: Any):
    buffer = ""
    async for chunk in _aiter_chunks(chunks):
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        buffer += text
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            payload = _sse_payload(event)
            if payload is not None:
                yield payload
    payload = _sse_payload(buffer)
    if payload is not None:
        yield payload


async def _aiter_chunks(chunks: Any):
    if hasattr(chunks, "__aiter__"):
        async for chunk in chunks:
            yield chunk
        return
    if isinstance(chunks, str | bytes):
        yield chunks
        return
    for chunk in chunks or ():
        yield chunk


async def _urlopen_stream_chunks(url: str, request: dict[str, Any]):
    import asyncio
    import threading

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    sentinel = object()

    def put(item: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def read_stream() -> None:
        req = urllib.request.Request(
            url,
            data=json.dumps(request["body"]).encode("utf-8"),
            method="POST",
            headers=request["headers"],
        )
        try:
            with urllib.request.urlopen(req) as response:  # noqa: S310
                for line in response:
                    put(line)
        except BaseException as exc:
            put(exc)
        finally:
            put(sentinel)

    threading.Thread(target=read_stream, daemon=True).start()

    while True:
        item = await queue.get()
        if item is sentinel:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


def _sse_payload(event: str) -> str | None:
    lines = []
    for line in event.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        if stripped.startswith("data:"):
            lines.append(stripped.removeprefix("data:").strip())
    return "\n".join(lines) if lines else None


def _parse_openai_stream_tool_deltas(delta: Mapping[str, Any], tool_parts: dict[int, dict[str, Any]], raw: Mapping[str, Any]) -> list[LlmStreamEvent]:
    events: list[LlmStreamEvent] = []
    tool_calls = delta.get("tool_calls")
    if not isinstance(tool_calls, list):
        return events

    for item in tool_calls:
        if not isinstance(item, Mapping):
            continue
        index = int(item.get("index", len(tool_parts)))
        state = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": "", "started": False})
        call_id = item.get("id")
        if call_id:
            state["id"] = call_id
        function = item.get("function", {})
        if isinstance(function, Mapping):
            name = function.get("name")
            if name:
                state["name"] = name
            arguments = function.get("arguments")
        else:
            arguments = None

        if not state["started"] and (state["id"] or state["name"]):
            state["started"] = True
            events.append(
                LlmStreamEvent(
                    kind="tool_call.started",
                    tool_call_id=state["id"] or None,
                    tool_name=state["name"] or None,
                    raw=raw,
                )
            )

        if arguments:
            state["arguments"] += arguments
            events.append(
                LlmStreamEvent(
                    kind="tool_call.arguments.delta",
                    tool_call_id=state["id"] or None,
                    tool_name=state["name"] or None,
                    tool_arguments_delta=arguments,
                    raw=raw,
                )
            )
    return events


def _assembled_openai_stream_tool_calls(tool_parts: dict[int, dict[str, Any]]) -> list[LlmToolCall]:
    calls: list[LlmToolCall] = []
    for index in sorted(tool_parts):
        state = tool_parts[index]
        if state.get("id") or state.get("name"):
            calls.append(
                LlmToolCall(
                    state.get("id", ""),
                    state.get("name", ""),
                    state.get("arguments", "{}") or "{}",
                )
            )
    return calls


def _send_stream_sync(url: str, request: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(request["body"]).encode("utf-8"),
        method="POST",
        headers=request["headers"],
    )
    try:
        with urllib.request.urlopen(req) as response:  # noqa: S310
            chunks = [line.decode("utf-8") for line in response]
            return {"status": response.status, "ok": 200 <= response.status < 300, "chunks": chunks}
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        return {"status": exc.code, "ok": False, "json": payload}


def _parse_openai_tool_calls(value: Any) -> list[LlmToolCall]:
    if not isinstance(value, list):
        return []
    calls = []
    for item in value:
        function = item.get("function", {}) if isinstance(item, dict) else {}
        if isinstance(item, dict) and isinstance(function, dict):
            calls.append(
                LlmToolCall(
                    item.get("id", ""),
                    function.get("name", ""),
                    function.get("arguments", "{}"),
                )
            )
    return calls


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("'\"")
        if name:
            values[name] = value
    return values


def _first_env(env: dict[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name)
        if value:
            return value
    return None


__all__ = [
    "LlmMessage",
    "LlmResponse",
    "LlmStreamEvent",
    "LlmToolCall",
    "EnvOpenAIConfig",
    "OpenAIProvider",
    "TokenTracker",
    "TokenUsage",
    "build_messages",
    "build_system_prompt",
    "build_user_prompt",
    "create_env_openai_provider",
    "create_llm_step_function",
    "create_openai_provider",
    "create_token_tracker",
    "load_env_openai_config",
    "to_llm_tool",
    "to_llm_tools",
]
