"""Serializable tool execution identities and resource claims."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from loom.core import Result, ToolRef


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    resource_id: str
    access_mode: str = "exclusive_write"

    def __post_init__(self):
        if self.access_mode not in {"shared_read", "exclusive_write", "none"}:
            raise ValueError("Invalid resource access mode")


@dataclass(frozen=True, slots=True)
class ToolBinding:
    ref: ToolRef
    entrypoint_id: str
    collection_id: str
    version: str = "1"
    placement: str = "execution"
    effect_kind: str = "side_effecting"
    required_capabilities: tuple[str, ...] = ()
    resource_refs: tuple[str, ...] = ()
    supports_cancel: bool = False
    artifact_kind: str | None = None
    evidence_fields: tuple[str, ...] = ()

    def __post_init__(self):
        if self.placement not in {"execution", "control"} or self.effect_kind not in {"read_only", "side_effecting", "service_control"}:
            raise ValueError("Invalid tool execution binding")

    @property
    def id(self):
        return f"{self.collection_id}/{self.ref.id}"


@dataclass(frozen=True, slots=True)
class Invocation:
    operation_id: str
    tool_call_id: str
    tool_id: str
    entrypoint_id: str
    input: Any
    session_id: str = ""
    run_id: str = ""
    attempt_id: str = ""
    runtime_id: str = ""
    tool_version: str = "1"
    resource_refs: tuple[str, ...] = ()
    deadline: float | None = None
    execution_limits: Mapping = field(default_factory=dict)
    goal_revision: int = 0
    workflow_revision: int = 0
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    execution_id: str
    status: str
    result: Result
    error_kind: str | None = None


class ToolExecutionRuntime(Protocol):
    runtime_id: str

    def prepare(self, resources: tuple) -> str: ...

    async def execute(self, invocation: Invocation, options: Mapping | None = None) -> ExecutionResult: ...

    def inspect(self, operation_id: str) -> str: ...

    async def cancel(self, operation_id: str) -> bool: ...

    def snapshot(self) -> dict: ...

    def restore(self, state: Mapping) -> None: ...

    def reconnect(self) -> bool: ...

    async def close(self) -> None: ...


def validate_tool_input(schema, value):
    """Validate the JSON-schema subset used by registered Loom tools."""
    if schema is None:
        return
    if "oneOf" in schema:
        accepted = 0
        for candidate in schema["oneOf"]:
            try:
                validate_tool_input(candidate, value)
                accepted += 1
            except ValueError:
                pass
        if accepted != 1:
            raise ValueError("Tool input must match exactly one schema alternative")
        return
    kinds = schema.get("type")
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        checks = {
            "object": isinstance(value, Mapping),
            "array": isinstance(value, list | tuple),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, int | float) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }
        if not any(checks.get(kind, False) for kind in kinds):
            raise ValueError("Tool input type does not match schema")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Tool input is outside its allowed values")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise ValueError("Tool input string is too short")
    if isinstance(value, Mapping):
        if not set(schema.get("required", ())).issubset(value):
            raise ValueError("Required tool input fields are missing")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - properties.keys():
            raise ValueError("Unexpected tool input fields")
        for key, item in value.items():
            if key in properties:
                validate_tool_input(properties[key], item)
    if isinstance(value, list | tuple):
        if len(value) < schema.get("minItems", 0):
            raise ValueError("Tool input array is too short")
        for item in value:
            validate_tool_input(schema.get("items", {}), item)
