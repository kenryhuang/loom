"""Validated commands and dependency-neutral service contracts."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

EVENT_SCHEMA = "loom.session.event.v1"
COMMAND_TYPES = frozenset({"submit_message", "answer_input", "supersede_input", "pause", "resume", "stop_run", "complete_task", "reopen_task"})
LIMITS = {"max_steps": 100, "max_llm_calls": 200, "max_tokens": 100_000, "max_duration_seconds": 1800, "max_window_chars": 240_000}


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "VALIDATION_FAILED"):
        super().__init__(message)
        self.status = status
        self.code = code


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stream_key(event: dict) -> str:
    channel = event["type"].removeprefix("llm.").removesuffix(".delta")
    key = f"{event['llm_call_id']}:{channel}"
    return f"{key}:{event['tool_call_id']}" if event.get("tool_call_id") else key


def text(value: Any, name: str, *, max_length: int = 100_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ServiceError(f"{name} must be non-empty text of at most {max_length} characters")
    return value


def object_value(value: Any, name: str = "payload") -> dict:
    if not isinstance(value, Mapping):
        raise ServiceError(f"{name} must be an object")
    return dict(value)


def validate_create(payload: Any) -> dict:
    payload = object_value(payload)
    allowed = {"objective", "workspace", "title", "model", "plan_mode", "limits"}
    if set(payload) - allowed:
        raise ServiceError("Unknown session fields")
    objective = text(payload.get("objective"), "objective")
    workspace = Path(text(payload.get("workspace", str(Path.cwd())), "workspace", max_length=4096)).expanduser().resolve()
    if not workspace.is_dir():
        raise ServiceError("workspace must be an existing directory")
    mode = payload.get("plan_mode", "auto")
    if not isinstance(mode, str) or mode not in {"auto", "force", "off"}:
        raise ServiceError("Invalid plan_mode")
    limits = {**LIMITS, **object_value(payload.get("limits", {}), "limits")}
    if set(limits) != set(LIMITS) or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in limits.values()):
        raise ServiceError("Invalid execution limits")
    model = payload.get("model")
    if model is not None:
        text(model, "model", max_length=200)
    return {
        "objective": objective,
        "workspace": str(workspace),
        "title": text(payload.get("title", objective[:80]), "title", max_length=200),
        "model": model,
        "plan_mode": mode,
        "limits": limits,
    }


def validate_command(command: Any) -> dict:
    command = object_value(command, "command")
    if set(command) - {"command_id", "type", "payload", "expected_task_revision"}:
        raise ServiceError("Unknown command fields")
    command_id = text(command.get("command_id"), "command_id", max_length=200)
    kind = command.get("type")
    if not isinstance(kind, str) or kind not in COMMAND_TYPES:
        raise ServiceError("Unknown command type")
    payload = object_value(command.get("payload", {}))
    allowed = {"reason"}
    if kind == "submit_message":
        allowed = {"content"}
        text(payload.get("content"), "content")
    elif kind in {"answer_input", "supersede_input"}:
        allowed = {"request_id", "answer" if kind == "answer_input" else "content"}
        text(payload.get("request_id"), "request_id", max_length=200)
        field = "answer" if kind == "answer_input" else "content"
        text(payload.get(field), field)
    elif "reason" in payload:
        text(payload["reason"], "reason")
    if set(payload) - allowed:
        raise ServiceError("Unknown command payload fields")
    revision = command.get("expected_task_revision")
    if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
        raise ServiceError("expected_task_revision must be a positive integer")
    return {"command_id": command_id, "type": kind, "payload": payload, "expected_task_revision": revision}
