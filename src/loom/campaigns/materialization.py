"""Typed declarative patch compilation into Loom mutations."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from loom.campaigns.contracts import CandidatePolicy
from loom.campaigns.serialization import canonical_digest
from loom.core import FrozenDict, Result, err, freeze_json, make_loom_error, ok, thaw_json
from loom.evolution.mutations import MutationBundle


@dataclass(frozen=True, slots=True)
class SurfaceDefinition:
    surface_id: str
    mutable_fields: tuple[str, ...]
    field_types: Mapping[str, type]
    numeric_limits: Mapping[str, tuple[float, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mutable_fields", tuple(self.mutable_fields))
        object.__setattr__(self, "field_types", MappingProxyType(dict(self.field_types)))
        limits = {key: tuple(value) for key, value in self.numeric_limits.items()}
        if any(len(value) != 2 or value[0] > value[1] for value in limits.values()):
            raise ValueError("Surface numeric limits must be ordered lower/upper pairs")
        object.__setattr__(self, "numeric_limits", MappingProxyType(limits))


@dataclass(frozen=True, slots=True)
class CompiledDeclarativeCandidate:
    base: FrozenDict
    materialized: FrozenDict
    operations: tuple[FrozenDict, ...]
    inverse_operations: tuple[FrozenDict, ...]
    mutation_bundle: MutationBundle
    base_digest: str
    result_digest: str
    inverse_digest: str
    compiler_version: str = "loom.declarative-patch.v1"


class DeclarativePatchCompiler:
    def __init__(self, surfaces: tuple[SurfaceDefinition, ...]):
        self._surfaces = {surface.surface_id: surface for surface in surfaces}

    def compile(
        self,
        base: Mapping[str, Any],
        operations: tuple[Mapping[str, Any], ...],
        policy: CandidatePolicy,
        *,
        evidence_trace_ids: tuple[str, ...],
    ) -> Result:
        if not evidence_trace_ids:
            return _patch_error("Declarative patch requires evidence trace IDs")
        applied = self._apply(base, operations, policy, build_inverse=True)
        if not applied.ok:
            return applied
        materialized, normalized, inverse = applied.value
        base_frozen = _frozen_mapping(base)
        restored = self._apply(materialized, inverse, policy, build_inverse=False)
        if not restored.ok or restored.value[0] != base_frozen:
            return _patch_error("Declarative patch inverse does not restore the exact base")
        base_digest = canonical_digest(base_frozen)
        result_digest = canonical_digest(materialized)
        inverse_frozen = tuple(_frozen_mapping(item) for item in inverse)
        bundle = MutationBundle(base_digest, tuple(normalized), tuple(evidence_trace_ids))
        return ok(
            CompiledDeclarativeCandidate(
                base_frozen,
                materialized,
                tuple(_frozen_mapping(item) for item in normalized),
                inverse_frozen,
                bundle,
                base_digest,
                result_digest,
                canonical_digest(inverse_frozen),
            )
        )

    def apply(self, base: Mapping[str, Any], operations: tuple[Mapping[str, Any], ...], policy: CandidatePolicy) -> Result:
        result = self._apply(base, operations, policy, build_inverse=False)
        return result if not result.ok else ok(result.value[0])

    def _apply(self, base, operations, policy, *, build_inverse: bool) -> Result:
        if not operations:
            return _patch_error("Declarative patch must contain operations")
        materialized = copy.deepcopy(thaw_json(base))
        normalized: list[dict[str, Any]] = []
        inverse: list[dict[str, Any]] = []
        for index, operation in enumerate(operations):
            if not isinstance(operation, Mapping):
                return _patch_error("Patch operation must be an object", operation_index=index)
            op = operation.get("op")
            path = operation.get("path")
            if op not in policy.allowed_patch_operations or not isinstance(path, str):
                return _patch_error("Patch operation or path is not allowed", operation_index=index)
            resolved = _resolve_surface_field(path, tuple(self._surfaces))
            if resolved is None:
                return _patch_error("Patch path must select one typed field", operation_index=index)
            surface_id, field_name = resolved
            if surface_id in policy.forbidden_surfaces or surface_id not in policy.editable_surfaces:
                return _patch_error("Patch targets a forbidden or non-editable surface", operation_index=index, path=path)
            surface = self._surfaces.get(surface_id)
            if surface is None or field_name not in surface.mutable_fields:
                return _patch_error("Patch path is unknown or immutable", operation_index=index, path=path)
            target = materialized.get(surface_id)
            if not isinstance(target, dict) or field_name not in target:
                return _patch_error("Patch target does not exist", operation_index=index, path=path)
            previous = target[field_name]
            value = thaw_json(operation.get("value"))
            normalized_value = copy.deepcopy(value)
            expected_type = surface.field_types.get(field_name)
            if op == "append_rule":
                if not isinstance(previous, list | tuple):
                    return _patch_error("append_rule requires a list-valued field", operation_index=index, path=path)
                value = [*previous, copy.deepcopy(value)]
            if expected_type is not None and (isinstance(value, bool) != (expected_type is bool) or not isinstance(value, expected_type)):
                return _patch_error("Patch value has the wrong type", operation_index=index, path=path)
            if op == "replace" and ("old" not in operation or operation["old"] != previous):
                return _patch_error("Replace operation old value does not match exactly", operation_index=index, path=path)
            if op == "set_limit":
                bounds = surface.numeric_limits.get(field_name)
                if bounds is None or isinstance(value, bool) or not isinstance(value, int | float):
                    return _patch_error("set_limit requires a declared numeric range", operation_index=index, path=path)
                if not bounds[0] <= value <= bounds[1]:
                    return _patch_error("set_limit value is outside its declared range", operation_index=index, path=path)
            target[field_name] = copy.deepcopy(value)
            normalized.append({"op": str(op), "path": path, "value": normalized_value, **({"old": previous} if op == "replace" else {})})
            inverse.insert(0, {"op": "set", "path": path, "value": previous})
        return ok((_frozen_mapping(materialized), tuple(normalized), tuple(inverse) if build_inverse else ()))


def _frozen_mapping(value: Mapping[str, Any]) -> FrozenDict:
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenDict):
        raise TypeError("Expected mapping")
    return frozen


def _resolve_surface_field(path: str, surface_ids: tuple[str, ...]) -> tuple[str, str] | None:
    matches: list[tuple[str, str]] = []
    for surface_id in surface_ids:
        prefix = f"{surface_id}."
        if not path.startswith(prefix):
            continue
        field_name = path[len(prefix) :]
        if field_name and "." not in field_name:
            matches.append((surface_id, field_name))
    if len(matches) != 1:
        return None
    return matches[0]


def _patch_error(message: str, **metadata) -> Result:
    return err(make_loom_error("CANDIDATE_PATCH_INVALID", message, retryable=False, metadata=metadata))


__all__ = ["CompiledDeclarativeCandidate", "DeclarativePatchCompiler", "SurfaceDefinition"]
