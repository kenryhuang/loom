"""Dependency-neutral immutable artifact references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from loom.core.experiments import ExperimentPhase

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    schema_version: str
    kind: str
    relative_path: str
    sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        if not self.schema_version or not self.kind:
            raise ValueError("Artifact schema_version and kind are required")
        path = PurePosixPath(self.relative_path)
        if path.is_absolute():
            raise ValueError("Artifact path must be relative")
        if ".." in path.parts:
            raise ValueError("Artifact path traversal is forbidden")
        if not self.relative_path or self.relative_path != path.as_posix() or "." in path.parts:
            raise ValueError("Artifact path must be normalized")
        validate_sha256(self.sha256, field="sha256")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int) or self.byte_size < 0:
            raise ValueError("Artifact byte_size must be a non-negative integer")

    @classmethod
    def unsafe(cls, schema_version: str, kind: str, relative_path: str, sha256: str, byte_size: int) -> ArtifactRef:
        value = object.__new__(cls)
        object.__setattr__(value, "schema_version", schema_version)
        object.__setattr__(value, "kind", kind)
        object.__setattr__(value, "relative_path", relative_path)
        object.__setattr__(value, "sha256", sha256)
        object.__setattr__(value, "byte_size", byte_size)
        return value


@dataclass(frozen=True, slots=True)
class TaskSetRef:
    task_set_id: str
    role: ExperimentPhase
    manifest_ref: ArtifactRef
    fingerprint_digest: str

    def __post_init__(self) -> None:
        if self.role is ExperimentPhase.MONITORING:
            raise ValueError("Monitoring is not a sealed campaign task-set role")
        validate_sha256(self.fingerprint_digest, field="fingerprint_digest")


def validate_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


__all__ = ["ArtifactRef", "TaskSetRef", "validate_sha256"]
