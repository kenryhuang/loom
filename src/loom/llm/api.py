"""LLM integration for Loom."""

from __future__ import annotations

import inspect
import json
import os
import urllib.error
import urllib.request
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
class LlmResponse:
    content: str | None = "{}"
    tool_calls: tuple[LlmToolCall, ...] = ()
    usage: TokenUsage = TokenUsage()
    finish_reason: str = "stop"

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))


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
    max_tool_calls_per_step: int = 5,
):
    async def llm_step(context: Context, runtime: Any) -> Result:
        started_at = runtime.now()
        trace_id = new_trace_id()
        tracker = create_token_tracker()
        tools = to_llm_tools(context.affordances.tools) if enable_tool_calling and context.affordances.tools else None
        messages = list(build_messages(context, **(prompt_options or {})))
        final_response: LlmResponse | None = None
        tool_call_count = 0
        tool_observations: list[Observation] = []

        while True:
            response = await provider.chat(messages, tools, getattr(runtime, "cancellation", None))
            if not response.ok:
                return response
            final_response = response.value
            tracker.add(final_response.usage)
            if not tracker.is_within_budget(context.goal.budget.max_tokens):
                return _token_budget_exceeded(tracker.total, context.goal.budget.max_tokens)
            if not enable_tool_calling or not final_response.tool_calls:
                break
            if tool_call_count >= max_tool_calls_per_step:
                return _max_tool_calls_exceeded(max_tool_calls_per_step)

            messages.append(
                LlmMessage(
                    "assistant",
                    "" if final_response.content is None else final_response.content,
                    tool_calls=final_response.tool_calls,
                )
            )
            for tool_call in final_response.tool_calls:
                if tool_call_count >= max_tool_calls_per_step:
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
                tool_observations.append(observation.value)
                messages.append(
                    LlmMessage(
                        "tool",
                        json.dumps(thaw_json(observation.value.value), separators=(",", ":")),
                        name=tool_call.name,
                        tool_call_id=tool_call.id,
                    )
                )

        if final_response is None:
            return err(make_loom_error("LLM_FAILED", "LLM provider returned no response", retryable=False))

        parsed = _parse_decision(final_response.content, trace_id)
        ended_at = runtime.now()
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
            metadata={
                "model": provider.model,
                "finishReason": final_response.finish_reason,
                "tokenUsage": _token_usage_metadata(tracker.total),
            },
        )
        emitted = await _emit_llm_events(runtime, trace, observations, decision)
        if not emitted.ok:
            return emitted
        return ok(StepResult(next_context, trace, llm_observation, parsed["output"]))

    return llm_step


@dataclass(frozen=True, slots=True)
class OpenAIProvider:
    api_key: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    base_url: str = "https://api.openai.com/v1"
    http_client: Any = None

    async def chat(self, messages, tools=None, cancellation=None) -> Result:
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
        {"type": "decision.recorded", "trace_id": trace.id, "decision": decision, "at": decision.at},
        {"type": "action.started", "trace_id": trace.id, "action": decision.action, "at": decision.at},
        *[
            {
                "type": "observation.recorded",
                "trace_id": trace.id,
                "observation": observation,
                "at": observation.at,
            }
            for observation in observations
        ],
    ]
    for event in events:
        emitted = runtime.trace_sink.emit(event)
        if inspect.isawaitable(emitted):
            emitted = await emitted
        if not emitted.ok:
            return emitted
    return ok(None)


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
