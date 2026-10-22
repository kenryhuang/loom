"""Content-addressed, integrity-checked service artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from loom.service.contracts import ServiceError, canonical


class ServiceArtifacts:
    def __init__(self, directory: Path):
        self.directory = directory
        self.root = directory / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(self, value, kind: str) -> dict:
        return self._publish(canonical(value).encode(), kind, "application/json", "json")

    def _publish(self, raw, kind, media_type, extension):
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / f"{digest}.{extension}"
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
        return {"schema_version": "1", "kind": kind, "relative_path": f"artifacts/{digest}.{extension}", "sha256": digest, "byte_size": len(raw),
                **({"media_type": media_type} if extension != "json" else {})}

    def read(self, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ServiceError("Invalid artifact digest")
        try:
            path = self.root / f"{digest}.json"
            raw = (path if path.exists() else self.root / f"{digest}.bin").read_bytes()
        except OSError as exc:
            raise ServiceError("Artifact not found", 404) from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ServiceError("Artifact integrity failure", 500)
        return raw

    def delete(self, digest: str):
        """Unlink an unreferenced artifact; caller must serialize with publishers."""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ServiceError("Invalid artifact digest")
        for extension in ("json", "bin"):
            (self.root / f"{digest}.{extension}").unlink(missing_ok=True)


    def publish_bytes(self, raw: bytes, media_type="image/png", kind="screenshot"):
        from loom.llm.images import image_dimensions

        if not isinstance(raw, bytes) or not 0 < len(raw) <= 20_000_000:
            raise ServiceError("Image artifact exceeds the 20 MB limit")
        image_dimensions(raw, media_type)
        return self._publish(raw, kind, media_type, "bin")

    def begin_upload(self, owner, digest, byte_size, media_type, kind):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ServiceError("Invalid artifact digest")
        if type(byte_size) is not int or not 0 < byte_size <= 20_000_000 or media_type != "image/png":
            raise ServiceError("Invalid image upload size or media type")
        folder = self.root / ".uploads"
        folder.mkdir(exist_ok=True)
        identifier = uuid.uuid4().hex
        (folder / f"{identifier}.json").write_text(canonical(dict(owner=owner, digest=digest, byte_size=byte_size, media_type=media_type, kind=kind)))
        (folder / f"{identifier}.part").touch(mode=0o600)
        return identifier

    def upload(self, owner, identifier, *, offset=None, data=None, finish=False):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ServiceError("Invalid upload ID")
        path = self.root / ".uploads" / identifier
        try:
            meta = json.loads(path.with_suffix(".json").read_text())
        except (OSError, ValueError) as exc:
            raise ServiceError("Image upload not found", 404) from exc
        if meta["owner"] != owner:
            raise ServiceError("Image upload does not belong to this worker", 403)
        target = path.with_suffix(".part")
        if finish:
            raw = target.read_bytes()
            if len(raw) != meta["byte_size"] or hashlib.sha256(raw).hexdigest() != meta["digest"]:
                raise ServiceError("Image upload integrity failure")
            try:
                return self.publish_bytes(raw, meta["media_type"], meta["kind"])
            finally:
                target.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
        if not isinstance(data, str) or len(data) > 65536:
            raise ServiceError("Image upload chunk exceeds limit")
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise ServiceError("Invalid upload encoding") from exc
        if type(offset) is not int or offset != target.stat().st_size or offset + len(raw) > meta["byte_size"]:
            raise ServiceError("Invalid upload offset or size")
        with target.open("ab") as stream:
            stream.write(raw)
        return {"offset": offset + len(raw)}
