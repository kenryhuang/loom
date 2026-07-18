"""Content-addressed artifact publication and fail-closed resolution."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from loom.campaigns.contracts import ArtifactRef
from loom.core import Result, err, make_loom_error, ok


class ArtifactStore:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def publish_bytes(
        self,
        content: bytes,
        *,
        kind: str,
        schema_version: str,
        suffix: str = "",
    ) -> Result:
        digest = hashlib.sha256(content).hexdigest()
        safe_suffix = suffix if not suffix or (suffix.startswith(".") and "/" not in suffix and "\\" not in suffix) else ""
        relative = Path("objects") / digest[:2] / f"{digest}{safe_suffix}"
        destination = self.root / relative
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                existing = destination.read_bytes()
                if existing != content:
                    return _integrity_error("Artifact digest collision", relative.as_posix())
            else:
                descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=destination.parent)
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                        return _integrity_error("Published artifact checksum mismatch", relative.as_posix())
                    os.replace(temporary, destination)
                    _fsync_directory(destination.parent)
                finally:
                    temporary.unlink(missing_ok=True)
            return ok(ArtifactRef(schema_version, kind, relative.as_posix(), digest, len(content)))
        except OSError as exc:
            return err(
                make_loom_error(
                    "ARTIFACT_PUBLICATION_FAILED",
                    "Failed to publish artifact",
                    retryable=True,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                    metadata={"relative_path": relative.as_posix()},
                )
            )

    def resolve(self, ref: ArtifactRef) -> Result:
        relative = Path(ref.relative_path)
        if relative.is_absolute() or ".." in relative.parts or not ref.relative_path:
            return _path_error(ref.relative_path)
        try:
            candidate = self.root.joinpath(relative).resolve(strict=False)
            candidate.relative_to(self.root)
        except (OSError, ValueError):
            return _path_error(ref.relative_path)
        if candidate.is_symlink() or _contains_symlink(self.root, relative):
            return _path_error(ref.relative_path)
        return ok(candidate)

    def read_bytes(self, ref: ArtifactRef, *, expected_schema: str | None = None) -> Result:
        if expected_schema is not None and ref.schema_version != expected_schema:
            return err(
                make_loom_error(
                    "UNSUPPORTED_SCHEMA",
                    "Artifact schema is not supported",
                    retryable=False,
                    metadata={"expected": expected_schema, "actual": ref.schema_version},
                )
            )
        resolved = self.resolve(ref)
        if not resolved.ok:
            return resolved
        try:
            content = resolved.value.read_bytes()
        except OSError as exc:
            return err(
                make_loom_error(
                    "ARTIFACT_NOT_FOUND",
                    "Referenced artifact is unavailable",
                    retryable=False,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                    metadata={"relative_path": ref.relative_path},
                )
            )
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != ref.byte_size or digest != ref.sha256:
            return _integrity_error("Artifact size or digest mismatch", ref.relative_path)
        return ok(content)


def _contains_symlink(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _path_error(relative_path: str) -> Result:
    return err(
        make_loom_error(
            "ARTIFACT_PATH_INVALID",
            "Artifact path escapes its declared root",
            retryable=False,
            metadata={"relative_path": relative_path},
        )
    )


def _integrity_error(message: str, relative_path: str) -> Result:
    return err(make_loom_error("ARTIFACT_INTEGRITY_FAILED", message, retryable=False, metadata={"relative_path": relative_path}))


__all__ = ["ArtifactStore"]
