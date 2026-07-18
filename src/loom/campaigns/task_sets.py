"""Task-set provenance, fingerprints, and contamination checks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from loom.campaigns.contracts import ExperimentPhase
from loom.campaigns.serialization import canonical_digest
from loom.core import Result, err, make_loom_error, ok

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DEFAULT_THRESHOLD = 0.80


@dataclass(frozen=True, slots=True)
class TaskManifestRow:
    task_id: str
    owner: str
    source_evidence_ref: str
    use_basis: str
    project_snapshot_digest: str
    template_lineage: str
    sanitizer_version: str
    content: str


@dataclass(frozen=True, slots=True)
class TaskFingerprint:
    task_id: str
    normalized_sha256: str
    project_snapshot_digest: str
    template_lineage: str
    minhash: tuple[int, ...]
    tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FingerprintedTaskSet:
    role: ExperimentPhase
    entries: tuple[TaskFingerprint, ...]
    fingerprint_digest: str
    detector_version: str = "loom.task-contamination.v1"
    threshold: float = _DEFAULT_THRESHOLD

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))


def load_task_manifest(path: str | Path) -> Result:
    source = Path(path)
    try:
        rows = []
        for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise TypeError(f"row {line_number} must be an object")
            rows.append(
                TaskManifestRow(
                    str(payload["task_id"]),
                    str(payload["owner"]),
                    str(payload["source_evidence_ref"]),
                    str(payload["use_basis"]),
                    str(payload["project_snapshot_digest"]),
                    str(payload["template_lineage"]),
                    str(payload["sanitizer_version"]),
                    str(payload["content"]),
                )
            )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "TASK_SET_INVALID",
                "Task manifest is invalid",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(source)},
            )
        )
    return ok(tuple(rows))


def fingerprint_task_rows(rows: tuple[TaskManifestRow, ...], role: ExperimentPhase) -> Result:
    if role is ExperimentPhase.MONITORING or not rows:
        return _task_error("Task set role or rows are invalid")
    entries: list[TaskFingerprint] = []
    for row in rows:
        required = (
            row.task_id,
            row.owner,
            row.source_evidence_ref,
            row.use_basis,
            row.project_snapshot_digest,
            row.template_lineage,
            row.sanitizer_version,
            row.content,
        )
        if not all(required):
            return _task_error("Task row provenance is incomplete", task_id=row.task_id)
        tokens = tuple(_TOKEN_RE.findall(row.content.casefold()))
        normalized = " ".join(tokens)
        entries.append(
            TaskFingerprint(
                row.task_id,
                hashlib.sha256(normalized.encode()).hexdigest(),
                row.project_snapshot_digest,
                row.template_lineage,
                _minhash(tokens),
                tokens,
            )
        )
    entries.sort(key=lambda item: item.task_id)
    digest = canonical_digest(
        {
            "role": role.value,
            "entries": [
                {
                    "task_id": item.task_id,
                    "normalized_sha256": item.normalized_sha256,
                    "project_snapshot_digest": item.project_snapshot_digest,
                    "template_lineage": item.template_lineage,
                    "minhash": item.minhash,
                }
                for item in entries
            ],
        }
    )
    return ok(FingerprintedTaskSet(role, tuple(entries), digest))


def validate_task_set_isolation(task_sets: tuple[FingerprintedTaskSet, ...], *, threshold: float = _DEFAULT_THRESHOLD) -> Result:
    if threshold > _DEFAULT_THRESHOLD or threshold <= 0:
        return _task_error("Contamination threshold cannot be weaker than 0.80", threshold=threshold)
    roles = [item.role for item in task_sets]
    if len(roles) != len(set(roles)):
        return _task_error("Task-set roles must be unique")
    matches: list[dict[str, object]] = []
    for left_index, left_set in enumerate(task_sets):
        for right_set in task_sets[left_index + 1 :]:
            for left in left_set.entries:
                for right in right_set.entries:
                    similarity = _jaccard(left.tokens, right.tokens)
                    reasons = []
                    if left.normalized_sha256 == right.normalized_sha256:
                        reasons.append("exact_content")
                    if left.project_snapshot_digest == right.project_snapshot_digest:
                        reasons.append("project_snapshot")
                    if left.template_lineage == right.template_lineage:
                        reasons.append("template_lineage")
                    if similarity >= threshold:
                        reasons.append("token_similarity")
                    if reasons:
                        matches.append(
                            {
                                "left_task": left.task_id,
                                "right_task": right.task_id,
                                "left_role": left_set.role.value,
                                "right_role": right_set.role.value,
                                "similarity": similarity,
                                "reasons": reasons,
                            }
                        )
    if matches:
        return err(
            make_loom_error(
                "TASK_SET_CONTAMINATION",
                "Task sets contain exact or near-duplicate tasks",
                retryable=False,
                metadata={"threshold": threshold, "matches": matches},
            )
        )
    return ok(None)


def _minhash(tokens: tuple[str, ...], count: int = 32) -> tuple[int, ...]:
    shingles = {" ".join(tokens[index : index + 3]) for index in range(max(1, len(tokens) - 2))}
    if not shingles:
        shingles = {""}
    return tuple(min(int.from_bytes(hashlib.sha256(f"{seed}:{shingle}".encode()).digest()[:8], "big") for shingle in shingles) for seed in range(count))


def _jaccard(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    left_set, right_set = set(left), set(right)
    union = left_set | right_set
    return 1.0 if not union else len(left_set & right_set) / len(union)


def _task_error(message: str, **metadata) -> Result:
    return err(make_loom_error("TASK_SET_INVALID", message, retryable=False, metadata=metadata))


__all__ = [
    "FingerprintedTaskSet",
    "TaskFingerprint",
    "TaskManifestRow",
    "fingerprint_task_rows",
    "load_task_manifest",
    "validate_task_set_isolation",
]
