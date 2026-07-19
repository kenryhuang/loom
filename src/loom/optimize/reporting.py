"""Stable, redacted result and Markdown exports for optimization runs."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from loom.campaigns.serialization import canonical_json_bytes

_SECRET_KEYS = frozenset({"api_key", "authorization", "credential", "credentials", "password", "private_key", "secret", "secrets"})
_HIDDEN_KEYS = frozenset({"task_content", "task_text", "reference_answer", "verifier_internal", "holdout_tasks"})
_SECRET_VALUE = re.compile(r"(?i)\b(?:sk|api[_-]?key|token)[=: _-]*[a-z0-9_-]{12,}\b")
_USER_PATH = re.compile(r"/(?:Users|home)/[^/\s]+")


def render_report(summary: Mapping[str, Any]) -> str:
    safe = _sanitize(summary)
    optimization_id = safe.get("optimization_id", "unknown")
    disposition = safe.get("disposition", "unknown")
    lines = [
        "# Loom Meta-Harness Optimization Report",
        "",
        "## Outcome",
        "",
        f"- optimization: `{optimization_id}`",
        f"- disposition: `{disposition}`",
    ]
    if safe.get("candidate_id"):
        lines.append(f"- candidate: `{safe['candidate_id']}`")
    if safe.get("next_action"):
        lines.extend(["", "## Next action", "", str(safe["next_action"])])
    lines.extend(
        [
            "",
            "## Evidence summary",
            "",
            "```json",
            json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(output_dir: str | Path, summary: Mapping[str, Any]) -> Path:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "report.md"
    _atomic_write(path, render_report(summary).encode("utf-8"))
    return path


def write_result(output_dir: str | Path, result: Mapping[str, Any] | Any) -> Path:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    value = {field.name: getattr(result, field.name) for field in fields(result)} if is_dataclass(result) and not isinstance(result, type) else result
    if not isinstance(value, Mapping):
        raise TypeError("Optimization result must be a mapping or dataclass")
    path = destination / "result.json"
    _atomic_write(path, canonical_json_bytes(_sanitize(value)) + b"\n")
    return path


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.casefold() in _SECRET_KEYS:
        return "[REDACTED]"
    if key is not None and key.casefold() in _HIDDEN_KEYS:
        return "[HIDDEN]"
    if is_dataclass(value) and not isinstance(value, type):
        value = {field.name: getattr(value, field.name) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(item_key): _sanitize(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _USER_PATH.sub("/[REDACTED_USER]", _SECRET_VALUE.sub("[REDACTED_SECRET]", value))
    return value


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["render_report", "write_report", "write_result"]
