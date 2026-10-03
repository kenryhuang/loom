"""Content-addressed, integrity-checked service artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from loom.service.contracts import ServiceError, canonical


class ServiceArtifacts:
    def __init__(self, directory: Path):
        self.directory = directory
        self.root = directory / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(self, value, kind: str) -> dict:
        raw = canonical(value).encode()
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / f"{digest}.json"
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=self.root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {"schema_version": "1", "kind": kind, "relative_path": f"artifacts/{digest}.json", "sha256": digest, "byte_size": len(raw)}

    def read(self, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ServiceError("Invalid artifact digest")
        try:
            raw = (self.root / f"{digest}.json").read_bytes()
        except OSError as exc:
            raise ServiceError("Artifact not found", 404) from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ServiceError("Artifact integrity failure", 500)
        return raw
