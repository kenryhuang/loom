"""Versioned, locally validated LLM responses with bounded protocol recovery."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from loom.llm.api import OpenAIProvider
from loom.llm.request_options import materialize_request_options


class ProtocolError(ValueError):
    def __init__(self, code, issues, *, retryable=True):
        self.code, self.issues, self.retryable = code, issues, retryable
        super().__init__("; ".join(f"{r['path']}: {r['message']}" for r in issues))


class UnsupportedFormat(ProtocolError):
    def __init__(self):
        super().__init__("unsupported_format", [{"path": "/", "message": "Provider does not support the requested structured output format"}])


class ProtocolExhausted(ProtocolError):
    pass


@dataclass(frozen=True)
class StructuredProtocol:
    name: str
    version: int
    schema: dict
    normalize: Any = None
    max_response_chars: int = 1_000_000

    def __post_init__(self):
        Draft202012Validator.check_schema(self.schema)

    @property
    def identity(self):
        return {"name": self.name, "version": self.version,
                "schema_digest": hashlib.sha256(json.dumps(self.schema, sort_keys=True).encode()).hexdigest()}


@dataclass(frozen=True)
class StructuredResult:
    value: Any
    attempts: int
    mode: str


def parse_response(response, max_chars=1_000_000):
    if response.finish_reason in {"length", "max_tokens"}:
        raise ProtocolError("truncated", [{"path": "/", "message": "Output was truncated; return a concise complete response"}])
    if response.finish_reason in {"content_filter", "refusal"}:
        raise ProtocolError("refused", [{"path": "/", "message": "Provider refused or filtered the response"}], retryable=False)
    raw = response.content or ""
    if len(raw) > max_chars:
        raise ProtocolError("response_too_large", [{"path": "/", "message": f"Response exceeds {max_chars} characters"}])
    match = re.fullmatch(r"\s*```(?:json)?\s*\n([\s\S]*?)\n```\s*", raw)
    if match:
        raw = match[1]

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("Non-finite JSON number")

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite JSON number")
        return parsed

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except (ValueError, RecursionError) as exc:
        reason = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise ProtocolError("invalid_json", [{"path": "/", "message": "Response is not valid JSON: " + reason}]) from exc
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 64:
            raise ProtocolError("invalid_json", [{"path": "/", "message": "JSON nesting exceeds 64 levels"}])
        if isinstance(node, dict):
            pending.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            pending.extend((v, depth + 1) for v in node)
    return value, ([{"path": "/", "rule": "unwrap_json_fence"}] if match else [])


def output_mode(provider):
    if not isinstance(provider, OpenAIProvider):
        return "text"
    options = materialize_request_options(provider.request_options).get("response_format", {})
    configured = options.get("type") if isinstance(options, dict) else None
    if configured in {"json_schema", "json_object"}:
        return configured
    host = urlsplit(provider.base_url).hostname or ""
    if (host == "dashscope.aliyuncs.com" or host.endswith(".aliyuncs.com")) and provider.model.startswith(
        ("qwen3.8-max", "qwen3.8-flash", "qwen3.7-max", "qwen3.7-plus", "qwen3.7-flash")
    ):
        return "json_schema"
    return "text"


def protocol_provider(provider, protocol, mode):
    """A private request copy: never mutate a provider shared with another stage."""
    if not isinstance(provider, OpenAIProvider):
        return provider
    options = materialize_request_options(provider.request_options)
    options.pop("response_format", None)
    if mode == "json_schema":
        options["response_format"] = {"type": "json_schema", "json_schema": {
            "name": f"{protocol.name}_v{protocol.version}", "strict": True, "schema": native_schema(protocol.schema)}}
    elif mode == "json_object":
        options["response_format"] = {"type": "json_object"}
    return replace(provider, request_options=options)


def native_schema(schema):
    """Encode optional properties as nullable for providers requiring all keys."""
    result = deepcopy(schema)
    if result.get("type") == "object":
        required = result.get("required", [])
        properties = result.get("properties", {})
        result["properties"] = {key: native_schema(value) if key in required else {"anyOf": [native_schema(value), {"type": "null"}]}
                                for key, value in properties.items()}
        result.update(required=list(properties), additionalProperties=False)
    if "items" in result:
        result["items"] = native_schema(result["items"])
    for key in ("anyOf", "oneOf"):
        if key in result:
            result[key] = [native_schema(v) for v in result[key]]
    return result


def unsupported_format(error):
    cause = error.cause or {}
    message = error.message.lower()
    return (isinstance(cause, Mapping) and cause.get("status") == 400
            and any(s in message for s in ("response_format", "json_schema", "structured output"))
            and any(s in message for s in ("not support", "unsupported", "not available")))


def schema_issues(validator, value):
    def leaves(error):
        if error.validator == "anyOf" and isinstance(error.instance, dict):
            kind = error.instance.get("verifier")
            candidates = [i for i, branch in enumerate(error.schema["anyOf"])
                          if kind in branch.get("properties", {}).get("verifier", {}).get("enum", [])]
            if candidates:
                yield from (leaf for child in error.context if child.schema_path[0] in candidates for leaf in leaves(child))
                return
        yield error

    issues = []
    for parent in validator.iter_errors(value):
        for error in leaves(parent):
            path = "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in error.absolute_path)
            message = f"Schema {error.validator} constraint failed"
            if error.validator == "type":
                message = f"Expected {error.validator_value}; received {type(error.instance).__name__}"
            issues.append({"path": path, "message": message})
            if len(issues) >= 16:
                return issues
    return issues


async def request_structured(*, protocol, payload, send, validate=lambda value: value,
                             event=None, mode="text", max_corrections=1, retry_truncated=True):
    """send(payload, attempt, mode) owns cancellation, tracing and total budgets.

    A correction never runs tools. Only a fully validated response is returned.
    Transport/budget/cancellation exceptions propagate without format retries.
    """
    async def emit(kind, **fields):
        if event:
            await event(kind, {**protocol.identity, "attempt": attempts, "mode": mode, **fields})

    attempts, corrections, fallback = 0, 0, False
    request = deepcopy(payload)
    validator = Draft202012Validator(protocol.schema)
    while True:
        attempts += 1
        proposal = None
        try:
            wire_payload = request if mode == "json_schema" else {**request, "response_contract": {**protocol.identity, "schema": protocol.schema}}
            response = await send(wire_payload, attempts, mode)
            proposal, changes = parse_response(response, protocol.max_response_chars)
            await emit("proposed", proposal=proposal)
            normalized = deepcopy(proposal)
            if protocol.normalize:
                normalized, edits = protocol.normalize(normalized)
                changes.extend(edits)
            if changes:
                await emit("normalized", changes=changes)
            issues = schema_issues(validator, normalized)
            if issues:
                # Pure business validators can supply a more actionable diagnosis
                # than a union's generic "anyOf" failure. Still never accept data
                # that failed the schema, even if business validation succeeds.
                if isinstance(normalized, dict):
                    try:
                        validate(normalized)
                    except (ValueError, TypeError) as exc:
                        issues.append({"path": "/", "message": str(exc)})
                raise ProtocolError("invalid_schema", issues)
            try:
                value = validate(normalized)
            except (ValueError, TypeError) as exc:
                raise ProtocolError("invalid_domain", [{"path": "/", "message": str(exc)}]) from exc
            await emit("completed")
            return StructuredResult(value, attempts, mode)
        except UnsupportedFormat as exc:
            if fallback or mode == "text":
                await emit("exhausted", code=exc.code, issues=exc.issues)
                raise ProtocolExhausted(exc.code, exc.issues) from exc
            fallback, mode = True, "text"
            await emit("fallback", code=exc.code, issues=exc.issues)
        except ProtocolError as exc:
            await emit("rejected", code=exc.code, issues=exc.issues, validation_error=str(exc))
            if not exc.retryable or corrections >= max_corrections or (exc.code == "truncated" and not retry_truncated):
                await emit("exhausted", code=exc.code, issues=exc.issues)
                raise ProtocolExhausted(exc.code, exc.issues, retryable=exc.retryable) from exc
            corrections += 1
            request = {**deepcopy(payload), "repair": {
                "validation_error": str(exc), "issues": exc.issues, "previous_proposal": proposal,
                "instruction": "Return a corrected complete JSON response. Preserve every user requirement and all required criteria. "
                               "Do not weaken checks, fabricate evidence, or remove requirements to evade validation."}}
            await emit("retrying", code=exc.code)
