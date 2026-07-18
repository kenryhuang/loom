"""Canonical serialization, identifiers, and timestamps for campaign artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import time
import uuid
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

_ID_PREFIXES = frozenset({"cmp_", "cand_", "exp_", "op_", "evt_", "frontier_", "decision_", "lease_", "review_", "monitor_"})


def utc_now() -> str:
    """Return a normative UTC RFC 3339 timestamp with microseconds."""

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_prefixed_id(prefix: str) -> str:
    """Create a UUIDv7 identifier with a domain prefix."""

    if prefix not in _ID_PREFIXES:
        raise ValueError(f"Unsupported identifier prefix: {prefix!r}")
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (timestamp_ms << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    return f"{prefix}{uuid.UUID(int=value)}"


def prefixed_id_from_digest(prefix: str, digest: str) -> str:
    """Derive a stable UUIDv7-shaped identifier for restart-safe materialization."""
    if prefix not in _ID_PREFIXES or len(digest) != 64:
        raise ValueError("Deterministic IDs require a supported prefix and SHA-256 digest")
    raw = bytearray.fromhex(digest[:32])
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return f"{prefix}{uuid.UUID(bytes=bytes(raw))}"


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize supported domain values using the RFC 8785 JCS rules."""

    normalized = _to_plain(value)
    return _jcs(normalized).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _to_plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _to_plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Canonical JSON numbers must be finite")
        return format(value, "f")
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Canonical JSON object keys must be strings")
        return {key: _to_plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_plain(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Canonical JSON numbers must be finite")
        return 0 if value == 0 else value
    if value is None or isinstance(value, str | int | bool):
        return value
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def _jcs(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _ecmascript_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_jcs(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda item: item.encode("utf-16be", errors="surrogatepass"))
        return "{" + ",".join(f"{_jcs(key)}:{_jcs(value[key])}" for key in keys) + "}"
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def _ecmascript_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("Canonical JSON numbers must be finite")
    if value == 0:
        return "0"
    negative = value < 0
    absolute = -value if negative else value
    source = repr(absolute).lower()
    if "e" not in source:
        result = source[:-2] if source.endswith(".0") else source
        return f"-{result}" if negative else result
    mantissa, exponent_text = source.split("e", 1)
    exponent = int(exponent_text)
    digits = mantissa.replace(".", "")
    decimal_position = (mantissa.index(".") if "." in mantissa else len(mantissa)) + exponent
    if 1e-6 <= absolute < 1e21:
        if decimal_position <= 0:
            result = "0." + "0" * (-decimal_position) + digits
        elif decimal_position >= len(digits):
            result = digits + "0" * (decimal_position - len(digits))
        else:
            result = digits[:decimal_position] + "." + digits[decimal_position:]
    else:
        fraction = digits[1:].rstrip("0")
        coefficient = digits[0] + (f".{fraction}" if fraction else "")
        normalized_exponent = decimal_position - 1
        sign = "+" if normalized_exponent >= 0 else ""
        result = f"{coefficient}e{sign}{normalized_exponent}"
    return f"-{result}" if negative else result


__all__ = ["canonical_digest", "canonical_json_bytes", "new_prefixed_id", "prefixed_id_from_digest", "utc_now"]
