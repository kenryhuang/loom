"""One-level tool composition helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from loom.core.models import Result, ToolRef, err, make_loom_error, ok
from loom.tools.contracts import CatalogTool, ToolLifecycle


def create_composed_tool(
    tool_id: str,
    description: str,
    atomic_tools: Iterable[CatalogTool],
    *,
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    timeout_ms: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    trace_ids: Iterable[str] | None = None,
    ttl_steps: int | None = None,
) -> Result:
    dependencies = tuple(atomic_tools)
    validation = _validate_atomic_dependencies(dependencies)
    if not validation.ok:
        return validation
    ref = ToolRef(tool_id, description, input_schema=input_schema, output_schema=output_schema, timeout_ms=timeout_ms, metadata=metadata)
    lifecycle = ToolLifecycle(layer="composed", created_from_trace_ids=tuple(trace_ids or ()), ttl_steps=ttl_steps)
    return ok(CatalogTool(ref, lifecycle, tuple(tool.id for tool in dependencies)))


def create_ephemeral_tool(
    tool_id: str,
    description: str,
    atomic_tools: Iterable[CatalogTool],
    *,
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    timeout_ms: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    trace_ids: Iterable[str] | None = None,
) -> Result:
    dependencies = tuple(atomic_tools)
    validation = _validate_atomic_dependencies(dependencies)
    if not validation.ok:
        return validation
    ref = ToolRef(tool_id, description, input_schema=input_schema, output_schema=output_schema, timeout_ms=timeout_ms, metadata=metadata)
    lifecycle = ToolLifecycle(layer="ephemeral", created_from_trace_ids=tuple(trace_ids or ()), ttl_steps=0)
    return ok(CatalogTool(ref, lifecycle, tuple(tool.id for tool in dependencies)))


def _validate_atomic_dependencies(tools: tuple[CatalogTool, ...]) -> Result:
    if len(tools) < 2:
        return err(make_loom_error("VALIDATION_FAILED", "Tool composition requires at least two atomic tools", retryable=False))
    non_atomic = tuple(tool.id for tool in tools if tool.layer != "atomic")
    if non_atomic:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Tool composition can only depend on atomic tools",
                retryable=False,
                metadata={"non_atomic_dependencies": non_atomic},
            )
        )
    return ok(None)
