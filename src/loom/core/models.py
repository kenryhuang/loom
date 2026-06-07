"""Core contracts for Loom."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, TypeVar

JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
Metadata = Mapping[str, JsonValue]

_T = TypeVar("_T")
_E = TypeVar("_E")
_sequence = 0


class FrozenDict(Mapping[str, JsonValue]):
    """Tiny immutable mapping that compares like an ordinary dict."""

    __slots__ = ("_data",)

    def __init__(self, items: Mapping[str, Any] | None = None):
        self._data = MappingProxyType({str(key): freeze_json(value) for key, value in (items or {}).items()})

    def __getitem__(self, key: str) -> JsonValue:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return repr(dict(self._data))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self._data) == dict(other)
        return False


def freeze_json(value: Any) -> JsonValue:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, list | tuple):
        return tuple(freeze_json(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"Value is not JSON-compatible: {type(value).__name__}")


def thaw_json(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def _optional_metadata(value: Mapping[str, Any] | None) -> FrozenDict | None:
    return None if value is None else FrozenDict(value)


def _tuple(value: Iterable[_T] | None) -> tuple[_T, ...]:
    return tuple(value or ())


def _next_id(prefix: str) -> str:
    global _sequence
    _sequence += 1
    millis = int(time.time() * 1000)
    return f"{prefix}{millis:x}_{_sequence:x}_{uuid.uuid4().hex[:8]}"


def new_loop_id() -> str:
    return _next_id("loop_")


def new_loop_version() -> str:
    return "v1"


def new_context_id() -> str:
    return _next_id("ctx_")


def new_trace_id() -> str:
    return _next_id("trace_")


def new_run_id() -> str:
    return _next_id("run_")


def as_step_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**53 - 1:
        raise ValueError(f"StepNumber must be a non-negative safe integer: {value!r}")
    return value


LoomErrorCode = Literal[
    "ABORTED",
    "TIMEOUT",
    "BUDGET_EXCEEDED",
    "VALIDATION_FAILED",
    "TOOL_FAILED",
    "LLM_FAILED",
    "LLM_PARSE_ERROR",
    "TOKEN_BUDGET_EXCEEDED",
    "LOOP_FAILED",
    "MERGE_CONFLICT",
    "MUTATION_REJECTED",
    "SERIALIZATION_FAILED",
    "INTERNAL",
]


@dataclass(frozen=True, slots=True)
class LoomError:
    code: str
    message: str
    retryable: bool
    trace_id: str | None = None
    cause: JsonValue | None = None
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cause", None if self.cause is None else freeze_json(self.cause))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


def make_loom_error(
    code: str,
    message: str,
    *,
    retryable: bool,
    trace_id: str | None = None,
    cause: Any = None,
    metadata: Mapping[str, Any] | None = None,
) -> LoomError:
    return LoomError(code, message, retryable, trace_id, cause, metadata)


def to_loom_error(error: object) -> LoomError:
    if isinstance(error, LoomError):
        return error
    if isinstance(error, BaseException):
        return make_loom_error(
            "INTERNAL",
            str(error),
            retryable=False,
            cause={"name": type(error).__name__, "message": str(error)},
        )
    if isinstance(error, str):
        return make_loom_error("INTERNAL", error, retryable=False, cause=error)
    return make_loom_error(
        "INTERNAL",
        "Internal error",
        retryable=False,
        cause={"value": type(error).__name__},
    )


class _ResultOkDescriptor:
    def __get__(self, instance: Result | None, owner: type[Result]) -> Any:
        if instance is None:
            return lambda value=None: owner(True, value=value)
        return instance._ok


class _ResultErrDescriptor:
    def __get__(self, instance: Result | None, owner: type[Result]) -> Any:
        if instance is None:
            return lambda error: owner(False, error=error)
        return not instance._ok


class Result:
    """Result value with TypeScript-like `Result.ok(value)` construction."""

    __slots__ = ("_ok", "error", "value")

    ok = _ResultOkDescriptor()
    err = _ResultErrDescriptor()

    def __init__(self, ok: bool, value: Any = None, error: LoomError | None = None):
        self._ok = ok
        self.value = value
        self.error = error

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Result) and self.ok == other.ok and self.value == other.value and self.error == other.error

    def __repr__(self) -> str:
        if self.ok:
            return f"Result.ok({self.value!r})"
        return f"Result.err({self.error!r})"

    def unwrap(self) -> Any:
        if not self.ok:
            message = self.error.message if self.error else "Result is err"
            raise RuntimeError(message)
        return self.value


def ok(value: Any = None) -> Result:
    return Result.ok(value)


def err(error: LoomError) -> Result:
    return Result.err(error)


def is_ok(result: Result) -> bool:
    return result.ok


def is_err(result: Result) -> bool:
    return not result.ok


@dataclass(frozen=True, slots=True)
class Constraint:
    id: str
    description: str
    severity: str = "must"
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class Capability:
    id: str
    description: str
    input_schema: JsonValue | None = None
    output_schema: JsonValue | None = None
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", None if self.input_schema is None else freeze_json(self.input_schema))
        object.__setattr__(
            self,
            "output_schema",
            None if self.output_schema is None else freeze_json(self.output_schema),
        )
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class IdentityLayer:
    role: str
    capabilities: tuple[Capability, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", _tuple(self.capabilities))
        object.__setattr__(self, "constraints", _tuple(self.constraints))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class Budget:
    max_steps: int | None = None
    max_duration_ms: int | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class SuccessCriterion:
    id: str
    description: str
    evaluator: str | None = None
    required: bool = True
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class GoalLayer:
    objective: str
    criteria: tuple[SuccessCriterion, ...] = ()
    budget: Budget = field(default_factory=Budget)
    parent_goal_id: str | None = None
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "criteria", _tuple(self.criteria))
        if isinstance(self.budget, Mapping):
            object.__setattr__(self, "budget", Budget(**self.budget))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class Action:
    id: str
    kind: str
    description: str
    input: JsonValue | None = None
    target: str | None = None
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input", None if self.input is None else freeze_json(self.input))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class Observation:
    id: str
    source: str
    value: JsonValue
    at: str
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", freeze_json(self.value))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class Decision:
    id: str
    action: Action
    reasoning: str
    alternatives: tuple[Action, ...]
    confidence: float
    at: str
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "alternatives", _tuple(self.alternatives))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class PendingLoop:
    id: str
    loop_id: str
    goal: GoalLayer
    started_at: str
    trace_id: str | None = None
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class StateLayer:
    observations: tuple[Observation, ...] = ()
    decisions: tuple[Decision, ...] = ()
    pending: tuple[PendingLoop, ...] = ()
    scratch: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", _tuple(self.observations))
        object.__setattr__(self, "decisions", _tuple(self.decisions))
        object.__setattr__(self, "pending", _tuple(self.pending))
        object.__setattr__(self, "scratch", _optional_metadata(self.scratch))


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    id: str
    kind: str
    content: JsonValue
    confidence: float
    created_at: str
    source_trace_id: str | None = None
    updated_at: str | None = None
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", freeze_json(self.content))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class KnowledgeLayer:
    facts: tuple[KnowledgeItem, ...] = ()
    heuristics: tuple[KnowledgeItem, ...] = ()
    memories: tuple[KnowledgeItem, ...] = ()
    version: str = "v1"
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", _tuple(self.facts))
        object.__setattr__(self, "heuristics", _tuple(self.heuristics))
        object.__setattr__(self, "memories", _tuple(self.memories))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class ToolRef:
    id: str
    description: str
    input_schema: JsonValue | None = None
    output_schema: JsonValue | None = None
    timeout_ms: int | None = None
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", None if self.input_schema is None else freeze_json(self.input_schema))
        object.__setattr__(
            self,
            "output_schema",
            None if self.output_schema is None else freeze_json(self.output_schema),
        )
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class LoopRef:
    loop_id: str
    description: str
    version: str | None = None
    input_projection: str | None = None
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class ResourceRef:
    id: str
    kind: str
    uri: str
    access: str
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class AffordanceLayer:
    tools: tuple[ToolRef, ...] = ()
    loops: tuple[LoopRef, ...] = ()
    resources: tuple[ResourceRef, ...] = ()
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", _tuple(self.tools))
        object.__setattr__(self, "loops", _tuple(self.loops))
        object.__setattr__(self, "resources", _tuple(self.resources))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class Context:
    id: str
    run_id: str
    created_at: str
    identity: IdentityLayer
    goal: GoalLayer
    state: StateLayer
    knowledge: KnowledgeLayer
    affordances: AffordanceLayer
    parent_context_id: str | None = None
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class TraceSnapshot:
    context_id: str
    at: str
    context: Context | None = None
    hash: str | None = None
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class Trace:
    id: str
    run_id: str
    loop_id: str
    loop_version: str
    step_number: int
    root_trace_id: str
    started_at: str
    ended_at: str
    duration_ms: int
    input_context_id: str
    output_context_id: str
    outcome: str
    parent_trace_id: str | None = None
    input_snapshot: TraceSnapshot | None = None
    output_snapshot: TraceSnapshot | None = None
    error: LoomError | None = None
    observations: tuple[Observation, ...] = ()
    decisions: tuple[Decision, ...] = ()
    actions: tuple[Action, ...] = ()
    children: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_number", as_step_number(self.step_number))
        object.__setattr__(self, "observations", _tuple(self.observations))
        object.__setattr__(self, "decisions", _tuple(self.decisions))
        object.__setattr__(self, "actions", _tuple(self.actions))
        object.__setattr__(self, "children", _tuple(self.children))
        object.__setattr__(self, "tags", _tuple(self.tags))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class StepResult:
    context: Context
    trace: Trace
    observation: Observation | None = None
    output: JsonValue | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", None if self.output is None else freeze_json(self.output))


@dataclass(frozen=True, slots=True)
class MinimalLoopDefinition:
    id: str
    version: str
    identity: IdentityLayer
    goal: GoalLayer
    step: Callable[..., Any]
    done: Callable[..., Any]
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class LoopHandle:
    id: str
    version: str
    definition: MinimalLoopDefinition
    trace_reader: Any
    created_at: str


@dataclass(frozen=True, slots=True)
class RunMetrics:
    steps: int
    started_at: str
    ended_at: str
    duration_ms: int
    trace_count: int
    outcome: str


@dataclass(frozen=True, slots=True)
class RunResult:
    context: Context
    traces: tuple[Trace, ...]
    metrics: RunMetrics
    output: JsonValue | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "traces", _tuple(self.traces))
        object.__setattr__(self, "output", None if self.output is None else freeze_json(self.output))


def empty_state(
    *,
    observations: Iterable[Observation] | None = None,
    decisions: Iterable[Decision] | None = None,
    pending: Iterable[PendingLoop] | None = None,
) -> StateLayer:
    return StateLayer(_tuple(observations), _tuple(decisions), _tuple(pending))


def empty_knowledge(
    *,
    facts: Iterable[KnowledgeItem] | None = None,
    heuristics: Iterable[KnowledgeItem] | None = None,
    memories: Iterable[KnowledgeItem] | None = None,
    version: str = "v1",
) -> KnowledgeLayer:
    return KnowledgeLayer(_tuple(facts), _tuple(heuristics), _tuple(memories), version)


def empty_affordances(
    *,
    tools: Iterable[ToolRef] | None = None,
    loops: Iterable[LoopRef] | None = None,
    resources: Iterable[ResourceRef] | None = None,
) -> AffordanceLayer:
    return AffordanceLayer(_tuple(tools), _tuple(loops), _tuple(resources))


def freeze_context(context: Context) -> Context:
    return context


@dataclass(frozen=True, slots=True)
class ContextPatch:
    base_context_id: str
    operations: tuple[Mapping[str, Any], ...]
    reason: str
    metadata: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))
        object.__setattr__(self, "metadata", _optional_metadata(self.metadata))


def apply_context_patch(context: Context, patch: ContextPatch) -> Result:
    if patch.base_context_id != context.id:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Context patch baseContextId does not match context id",
                retryable=False,
            )
        )

    state = context.state
    knowledge = context.knowledge
    identity = context.identity
    goal = context.goal
    affordances = context.affordances
    metadata = dict(context.metadata or {})

    for operation in patch.operations:
        op = operation.get("op")
        value = operation.get("value")
        if op in {"appendObservation", "append_observation"}:
            state = replace(state, observations=(*state.observations, value))
        elif op in {"appendDecision", "append_decision"}:
            state = replace(state, decisions=(*state.decisions, value))
        elif op in {"appendPending", "append_pending"}:
            state = replace(state, pending=(*state.pending, value))
        elif op in {"clearPending", "clear_pending"}:
            pending_id = operation.get("id")
            state = replace(
                state,
                pending=tuple(item for item in state.pending if item.id != pending_id),
            )
        elif op in {"addKnowledge", "add_knowledge"}:
            knowledge = _add_knowledge(knowledge, value)
        elif op in {"replaceGoal", "replace_goal"}:
            goal = value
        elif op in {"replaceIdentity", "replace_identity"}:
            identity = value
        elif op in {"replaceAffordances", "replace_affordances"}:
            affordances = value
        elif op in {"setMetadata", "set_metadata"}:
            metadata[str(operation["key"])] = freeze_json(operation.get("value"))
        else:
            return err(
                make_loom_error(
                    "VALIDATION_FAILED",
                    f"Unsupported patch operation: {op}",
                    retryable=False,
                )
            )

    return ok(
        freeze_context(
            replace(
                context,
                id=new_context_id(),
                state=state,
                knowledge=knowledge,
                identity=identity,
                goal=goal,
                affordances=affordances,
                metadata=metadata or None,
            )
        )
    )


class KnowledgeView:
    def __init__(self, knowledge: KnowledgeLayer):
        self.knowledge = knowledge

    def search(
        self,
        *,
        text: str | None = None,
        kind: str | None = None,
        min_confidence: float | None = None,
        limit: int | None = None,
    ) -> tuple[KnowledgeItem, ...]:
        items = (*self.knowledge.facts, *self.knowledge.heuristics, *self.knowledge.memories)
        matches: list[KnowledgeItem] = []
        for item in items:
            if kind is not None and item.kind != kind:
                continue
            if min_confidence is not None and item.confidence < min_confidence:
                continue
            if text is not None and text.lower() not in str(thaw_json(item.content)).lower():
                continue
            matches.append(item)
            if limit is not None and len(matches) >= limit:
                break
        return tuple(matches)


def create_knowledge_view(knowledge: KnowledgeLayer) -> KnowledgeView:
    return KnowledgeView(knowledge)


def project(
    parent: Context,
    child_goal: GoalLayer,
    *,
    identity: IdentityLayer,
    tool_ids: Iterable[str] | None = None,
    loop_ids: Iterable[str] | None = None,
    resource_ids: Iterable[str] | None = None,
    include_state_summary: bool = False,
) -> Result:
    tool_set = None if tool_ids is None else set(tool_ids)
    loop_set = None if loop_ids is None else set(loop_ids)
    resource_set = None if resource_ids is None else set(resource_ids)
    state = empty_state()
    if include_state_summary:
        state = StateLayer(
            observations=(
                Observation(
                    id=f"{parent.id}-summary",
                    source="project",
                    value={"observationCount": len(parent.state.observations)},
                    at=now_iso(),
                ),
            )
        )
    return ok(
        freeze_context(
            Context(
                id=new_context_id(),
                run_id=parent.run_id,
                created_at=now_iso(),
                identity=identity,
                goal=child_goal,
                state=state,
                knowledge=parent.knowledge,
                affordances=AffordanceLayer(
                    tools=tuple(tool for tool in parent.affordances.tools if tool_set is None or tool.id in tool_set),
                    loops=tuple(loop for loop in parent.affordances.loops if loop_set is None or loop.loop_id in loop_set),
                    resources=tuple(resource for resource in parent.affordances.resources if resource_set is None or resource.id in resource_set),
                ),
                parent_context_id=parent.id,
            )
        )
    )


@dataclass(frozen=True, slots=True)
class ChildOutput:
    context: Context
    status: str
    observations: tuple[Observation, ...]
    decisions: tuple[Decision, ...]
    knowledge_candidates: tuple[KnowledgeItem, ...]
    trace_root_id: str | None = None
    metrics: Metadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", _tuple(self.observations))
        object.__setattr__(self, "decisions", _tuple(self.decisions))
        object.__setattr__(self, "knowledge_candidates", _tuple(self.knowledge_candidates))
        object.__setattr__(self, "metrics", _optional_metadata(self.metrics))


def emit_child_output(
    child_context: Context,
    *,
    status: str,
    trace_root_id: str | None = None,
    metrics: Mapping[str, Any] | None = None,
) -> ChildOutput:
    return ChildOutput(
        context=child_context,
        status=status,
        observations=child_context.state.observations,
        decisions=child_context.state.decisions,
        knowledge_candidates=(
            *child_context.knowledge.facts,
            *child_context.knowledge.heuristics,
            *child_context.knowledge.memories,
        ),
        trace_root_id=trace_root_id,
        metrics=metrics,
    )


def merge_child_output(
    parent: Context,
    child_output: ChildOutput,
    *,
    accept_knowledge_ids: Iterable[str] = (),
    append_child_summary: bool = True,
) -> Result:
    accept_set = set(accept_knowledge_ids)
    parent_knowledge_ids = {
        item.id
        for item in (
            *parent.knowledge.facts,
            *parent.knowledge.heuristics,
            *parent.knowledge.memories,
        )
    }
    accepted = [item for item in child_output.knowledge_candidates if item.id in accept_set]
    conflict = next((item for item in accepted if item.id in parent_knowledge_ids), None)
    if conflict is not None:
        return err(
            make_loom_error(
                "MERGE_CONFLICT",
                "Knowledge item already exists",
                retryable=False,
                metadata={"knowledgeId": conflict.id},
            )
        )

    observations = list(parent.state.observations)
    if append_child_summary:
        observations.append(
            Observation(
                id=f"child-summary-{new_trace_id()}",
                source="child",
                value={
                    "status": child_output.status,
                    "observationCount": len(child_output.observations),
                    "decisionCount": len(child_output.decisions),
                },
                at=now_iso(),
            )
        )
    observations.extend(child_output.observations)

    knowledge = parent.knowledge
    for item in accepted:
        knowledge = _add_knowledge(knowledge, item)

    return ok(
        freeze_context(
            replace(
                parent,
                id=new_context_id(),
                state=replace(
                    parent.state,
                    observations=tuple(observations),
                    decisions=(*parent.state.decisions, *child_output.decisions),
                ),
                knowledge=knowledge,
            )
        )
    )


def _add_knowledge(knowledge: KnowledgeLayer, item: KnowledgeItem) -> KnowledgeLayer:
    if item.kind == "fact":
        return replace(knowledge, facts=(*knowledge.facts, item))
    if item.kind == "heuristic":
        return replace(knowledge, heuristics=(*knowledge.heuristics, item))
    if item.kind == "memory":
        return replace(knowledge, memories=(*knowledge.memories, item))
    return replace(knowledge, facts=(*knowledge.facts, item))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


__all__ = [
    "Action",
    "AffordanceLayer",
    "Budget",
    "Capability",
    "ChildOutput",
    "Constraint",
    "Context",
    "ContextPatch",
    "Decision",
    "FrozenDict",
    "GoalLayer",
    "IdentityLayer",
    "JsonValue",
    "KnowledgeItem",
    "KnowledgeLayer",
    "KnowledgeView",
    "LoomError",
    "LoopHandle",
    "LoopRef",
    "MinimalLoopDefinition",
    "Observation",
    "PendingLoop",
    "ResourceRef",
    "Result",
    "RunMetrics",
    "RunResult",
    "StateLayer",
    "StepResult",
    "SuccessCriterion",
    "ToolRef",
    "Trace",
    "TraceSnapshot",
    "apply_context_patch",
    "as_step_number",
    "empty_affordances",
    "empty_knowledge",
    "empty_state",
    "emit_child_output",
    "err",
    "freeze_context",
    "freeze_json",
    "create_knowledge_view",
    "is_err",
    "is_ok",
    "make_loom_error",
    "merge_child_output",
    "new_context_id",
    "new_loop_id",
    "new_loop_version",
    "new_run_id",
    "new_trace_id",
    "now_iso",
    "ok",
    "project",
    "thaw_json",
    "to_loom_error",
]
