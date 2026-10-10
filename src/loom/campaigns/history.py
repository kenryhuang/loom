"""Read-only campaign history imports and sanitization."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loom.campaigns.operations import CampaignOperation
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes
from loom.core import FrozenDict, Result, err, freeze_json, make_loom_error, ok
from loom.evaluation.bundle import load_evaluation_bundle

_SECRET_RE = re.compile(r"(?i)(?:sk|api[_-]?key|token)[=: _-]*[a-z0-9_-]{16,}")
_USER_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s]+")
_INJECTION_RE = re.compile(r"(?i)(ignore|disregard)\s+(all\s+)?(previous|prior)\s+instructions|system\s+prompt|you\s+are\s+now")
_SECRET_FIELDS = frozenset({"secret", "secrets", "credentials", "credential", "password", "api_key", "authorization", "private_key"})
_HIDDEN = object()


@dataclass(frozen=True, slots=True)
class SanitizedText:
    text: str
    trust: str
    prompt_injection_suspected: bool


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    kind: str
    record_id: str
    data: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        frozen = freeze_json(self.data)
        if not isinstance(frozen, FrozenDict):
            raise TypeError("History record data must be a mapping")
        object.__setattr__(self, "data", frozen)


@dataclass(frozen=True, slots=True)
class HistoryEnvelope:
    schema_version: str
    query: str
    trust: str
    data: FrozenDict

    def __post_init__(self) -> None:
        frozen = freeze_json(self.data)
        if not isinstance(frozen, FrozenDict):
            raise TypeError("History envelope data must be a mapping")
        object.__setattr__(self, "data", frozen)


class CampaignHistory:
    _ALLOWED_QUERIES = frozenset(
        {
            "campaign.status",
            "campaign.frontier",
            "candidate.list",
            "candidate.show",
            "candidate.diff",
            "experiment.compare",
            "finding.search",
            "evidence.show",
            "trace.slice",
            "failures.cluster",
        }
    )
    _HIDDEN_FIELDS = frozenset(
        {
            "task_text",
            "task_content",
            "validation_passed",
            "validation_score",
            "validation_failure",
            "holdout_passed",
            "holdout_score",
            "holdout_failure",
            "reference_answer",
            "verifier_internal",
            "membership",
        }
    )

    def __init__(self, store, spec, actor, records: tuple[HistoryRecord, ...] = ()):
        self.store = store
        self.spec = spec
        self.actor = actor
        self.records = tuple(records)

    async def query(self, query: str, arguments: Mapping[str, Any], *, operation_id: str) -> Result:
        if query not in self._ALLOWED_QUERIES:
            return err(
                make_loom_error(
                    "HISTORY_QUERY_FORBIDDEN",
                    "History query is not allowlisted",
                    retryable=False,
                    metadata={"query": query},
                )
            )
        selected = self._select(query, arguments)
        envelope = HistoryEnvelope(
            "loom.campaign.history-envelope.v1",
            query,
            "untrusted_historical_evidence",
            {"records": selected},
        )
        projection = await self.store.load(self.spec.campaign_id)
        if not projection.ok:
            return projection
        audit = CampaignOperation(
            operation_id,
            self.spec.campaign_id,
            canonical_digest({"query": query, "arguments": arguments}),
            "history.queried",
            actor=self.actor,
            payload={"query": query, "arguments_digest": canonical_digest(arguments), "result_count": len(selected)},
        )
        committed = await self.store.transact(audit, projection.value.aggregate_version)
        return ok(envelope) if committed.ok else committed

    def _select(self, query: str, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        kind = query.split(".", 1)[0]
        if query == "finding.search":
            kind = "finding"
        selected: list[dict[str, Any]] = []
        for record in self.records:
            if record.kind == "candidate" and not self.spec.history_visibility.candidate_artifacts:
                continue
            if record.kind in {"imported_experience", "experience"} and not self.spec.history_visibility.imported_experience:
                continue
            if kind not in {"campaign", "failures"} and record.kind != kind:
                continue
            phase = record.data.get("phase")
            if phase in {"validation", "holdout"}:
                continue
            surface = arguments.get("surface")
            if surface is not None and record.data.get("surface") != surface:
                continue
            sanitized = self._visible_record(record, trace=query == "trace.slice")
            selected.append(sanitized)
        return tuple(sorted(selected, key=lambda item: str(item["record_id"])))

    def _visible_record(self, record: HistoryRecord, *, trace: bool) -> dict[str, Any]:
        visible: dict[str, Any] = {
            "kind": record.kind,
            "record_id": record.record_id,
            "trust": "untrusted_historical_evidence",
        }
        allowed_fields = frozenset(self.spec.history_visibility.trace_fields if trace else self.spec.history_visibility.discovery_fields)
        injection = False
        for key, value in record.data.items():
            if key not in allowed_fields:
                continue
            if _hidden_history_field(key, self._HIDDEN_FIELDS):
                continue
            sanitized, nested_injection = _sanitize_history_value(value, self._HIDDEN_FIELDS)
            if sanitized is _HIDDEN:
                continue
            visible[key] = sanitized
            injection = injection or nested_injection
        visible["prompt_injection_suspected"] = injection
        return visible


def sanitize_untrusted_text(value: str) -> SanitizedText:
    redacted = _SECRET_RE.sub("[REDACTED_SECRET]", value)
    redacted = _USER_PATH_RE.sub("/[REDACTED_USER]", redacted)
    return SanitizedText(redacted, "untrusted_historical_evidence", bool(_INJECTION_RE.search(value)))


async def import_experience(store, campaign_id: str, path: str | Path, *, operation_id: str, actor) -> Result:
    source = Path(path)
    try:
        raw = source.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Imported experience is not readable JSON",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        )
    schema = payload.get("schema_version") if isinstance(payload, Mapping) else None
    if schema == "loom.evaluation.bundle.v1":
        loaded = load_evaluation_bundle(source)
        if not loaded.ok:
            return loaded
    elif schema == "loom.evolution.bundle.v1":
        if not isinstance(payload.get("summary"), Mapping):
            return err(make_loom_error("VALIDATION_FAILED", "Evolution bundle summary is missing", retryable=False))
    elif schema == "loom.evaluation.bundle.v3":
        from loom.evaluation.behavior_contracts import validate_bundle

        try:
            validate_bundle(payload)
        except ValueError as exc:
            return err(make_loom_error("VALIDATION_FAILED", str(exc), retryable=False))
    elif schema == "loom.evolution.hypotheses.v2":
        if not isinstance(payload.get("proposals"), list):
            return err(make_loom_error("VALIDATION_FAILED", "Behavior hypotheses missing", retryable=False))
    elif schema == "loom.research.reference.v1":
        pass
    else:
        return err(
            make_loom_error(
                "UNSUPPORTED_SCHEMA",
                "Imported experience schema is not supported",
                retryable=False,
                metadata={"schema_version": str(schema)},
            )
        )
    sanitized = _sanitize_value(payload)
    envelope = {
        "schema_version": "loom.campaign.imported-experience.v1",
        "source_schema": schema,
        "source_digest": canonical_digest(payload),
        "trust": "untrusted_historical_evidence",
        "prompt_injection_suspected": _contains_injection(payload),
        "content": sanitized,
    }
    published = store.artifacts.publish_bytes(
        canonical_json_bytes(envelope),
        kind="imported_experience",
        schema_version="loom.campaign.imported-experience.v1",
        suffix=".json",
    )
    if not published.ok:
        return published
    projection = await store.load(campaign_id)
    if not projection.ok:
        return projection
    operation = CampaignOperation(
        operation_id,
        campaign_id,
        canonical_digest({"campaign_id": campaign_id, "artifact": published.value}),
        "experience.imported",
        actor=actor,
        payload={"artifact_ref": published.value},
        output_refs=(published.value,),
    )
    committed = await store.transact(operation, projection.value.aggregate_version)
    return published if committed.ok else committed


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_untrusted_text(value).text
    if isinstance(value, Mapping):
        return {str(key): _sanitize_value(item) for key, item in value.items() if not _hidden_history_field(str(key), CampaignHistory._HIDDEN_FIELDS)}
    if isinstance(value, list | tuple):
        return [_sanitize_value(item) for item in value]
    return value


def _sanitize_history_value(value: Any, hidden_fields: frozenset[str]) -> tuple[Any, bool]:
    if isinstance(value, str):
        sanitized = sanitize_untrusted_text(value)
        return sanitized.text, sanitized.prompt_injection_suspected
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        injection = False
        for key, item in value.items():
            name = str(key)
            if _hidden_history_field(name, hidden_fields):
                continue
            sanitized, nested_injection = _sanitize_history_value(item, hidden_fields)
            if sanitized is not _HIDDEN:
                result[name] = sanitized
            injection = injection or nested_injection
        return result, injection
    if isinstance(value, tuple | list):
        result = []
        injection = False
        for item in value:
            sanitized, nested_injection = _sanitize_history_value(item, hidden_fields)
            if sanitized is not _HIDDEN:
                result.append(sanitized)
            injection = injection or nested_injection
        return result, injection
    return value, False


def _hidden_history_field(key: str, hidden_fields: frozenset[str]) -> bool:
    lowered = key.lower()
    return (
        lowered in hidden_fields
        or lowered in _SECRET_FIELDS
        or lowered in {"validation", "holdout"}
        or lowered.startswith("validation_")
        or lowered.startswith("holdout_")
    )


def _contains_injection(value: Any) -> bool:
    if isinstance(value, str):
        return sanitize_untrusted_text(value).prompt_injection_suspected
    if isinstance(value, Mapping):
        return any(_contains_injection(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_injection(item) for item in value)
    return False


__all__ = [
    "CampaignHistory",
    "HistoryEnvelope",
    "HistoryRecord",
    "SanitizedText",
    "import_experience",
    "sanitize_untrusted_text",
]
