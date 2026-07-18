"""Bounded candidate authoring workspace."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from loom.core import Result, err, make_loom_error, ok


@dataclass(frozen=True, slots=True)
class CandidateWorkspace:
    root: Path

    @classmethod
    def allocate(cls, parent: str | os.PathLike[str], candidate_id: str) -> Result:
        if not candidate_id or "/" in candidate_id or "\\" in candidate_id or candidate_id in {".", ".."}:
            return _workspace_error(candidate_id)
        root = Path(parent).resolve() / candidate_id
        try:
            root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            return err(make_loom_error("WORKSPACE_EXISTS", "Candidate workspace already exists", retryable=False))
        except OSError as exc:
            return err(
                make_loom_error(
                    "WORKSPACE_FAILED",
                    "Could not allocate candidate workspace",
                    retryable=False,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                )
            )
        return ok(cls(root))

    def resolve(self, relative_path: str) -> Result:
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or not relative_path:
            return _workspace_error(relative_path)
        candidate = (self.root / path).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return _workspace_error(relative_path)
        current = self.root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                return _workspace_error(relative_path)
        return ok(candidate)

    def write_text(self, relative_path: str, content: str) -> Result:
        resolved = self.resolve(relative_path)
        if not resolved.ok:
            return resolved
        try:
            resolved.value.parent.mkdir(parents=True, exist_ok=True)
            resolved.value.write_text(content, encoding="utf-8")
        except OSError as exc:
            return err(
                make_loom_error(
                    "WORKSPACE_FAILED",
                    "Could not write candidate workspace file",
                    retryable=False,
                    cause={"name": type(exc).__name__, "message": str(exc)},
                )
            )
        return ok(resolved.value)


def _workspace_error(relative_path: str) -> Result:
    return err(
        make_loom_error(
            "WORKSPACE_PATH_INVALID",
            "Candidate workspace path escapes its root",
            retryable=False,
            metadata={"relative_path": relative_path},
        )
    )


__all__ = ["CandidateWorkspace"]
