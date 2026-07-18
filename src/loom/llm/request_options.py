"""Validation and copying for provider-specific chat request options."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FrozenJsonValue: TypeAlias = JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]

RESERVED_REQUEST_OPTIONS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "tools",
        "tool_choice",
        "temperature",
        "max_completion_tokens",
        "max_tokens",
    }
)


def normalize_request_options(options: Mapping[str, Any] | None) -> Mapping[str, FrozenJsonValue]:
    """Validate and recursively freeze provider request options."""

    if options is None:
        return MappingProxyType({})
    if not isinstance(options, Mapping):
        raise TypeError("request_options must be a mapping")

    conflicts = RESERVED_REQUEST_OPTIONS.intersection(options)
    if conflicts:
        raise ValueError(f"request_options contains reserved fields: {', '.join(sorted(conflicts))}")

    frozen: dict[str, FrozenJsonValue] = {}
    for key, value in options.items():
        if not isinstance(key, str):
            raise TypeError("request_options keys must be strings")
        frozen[key] = _freeze_json(value, f"request_options.{key}")
    return MappingProxyType(frozen)


def materialize_request_options(options: Mapping[str, FrozenJsonValue]) -> dict[str, JsonValue]:
    """Return a JSON-serializable copy of frozen provider request options."""

    return {key: _thaw_json(value) for key, value in options.items()}


def _freeze_json(value: Any, path: str) -> FrozenJsonValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must be a finite JSON number")
        return value
    if isinstance(value, list):
        return tuple(_freeze_json(item, f"{path}[{index}]") for index, item in enumerate(value))
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} keys must be strings")
            frozen[key] = _freeze_json(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    raise TypeError(f"{path} is not JSON-compatible")


def _thaw_json(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "FrozenJsonValue",
    "JsonValue",
    "RESERVED_REQUEST_OPTIONS",
    "materialize_request_options",
    "normalize_request_options",
]
