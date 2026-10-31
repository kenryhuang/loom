"""Bounded file-only branches and crash-reconcilable explicit integration.

This is capability isolation for host file tools, not an OS process sandbox.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from loom.service.contracts import canonical

MAX_FILE = 2_000_000
MAX_TOTAL = 20_000_000
EXCLUDE = {".git", ".venv", "node_modules", "__pycache__", ".worktrees"}


def fingerprint(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".loom-pending-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as out:
            if path.exists():
                os.fchmod(out.fileno(), path.stat().st_mode & 0o777)
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe(root, name):
    relative = Path(name)
    if not name or relative.is_absolute() or ".." in relative.parts or any(p in EXCLUDE or p.startswith(".loom-pending-") for p in relative.parts):
        raise ValueError("Branch path must stay inside its file workspace")
    path = root / relative
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ValueError("Symlinks are unsupported in isolated branches")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Branch path escaped workspace")
    return path


def manifest(root):
    entries, total = {}, 0

    def fail(error):
        raise error

    for directory, names, files in os.walk(root, followlinks=False, onerror=fail):
        names[:] = sorted(n for n in names if n not in EXCLUDE and not (Path(directory) / n).is_symlink())
        for name in sorted(files):
            if name.startswith(".loom-pending-"):
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Branch snapshots require regular files")
            size = path.stat().st_size
            total += size
            if size > MAX_FILE or total > MAX_TOTAL or len(entries) >= 2000:
                raise ValueError("Branch snapshot exceeds bounded file capacity")
            entries[str(path.relative_to(root))] = fingerprint(path.read_bytes())
    return entries


class FileBranch:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "branch.json"
        self.state = json.loads(self.path.read_text())
        self.root = self.directory / "work"

    @classmethod
    def create(cls, workspace, directory, *, identity):
        workspace, directory = Path(workspace).resolve(), Path(directory).resolve()
        if directory.is_relative_to(workspace) or workspace.is_relative_to(directory):
            raise ValueError("Branch storage must be separate from source workspace")
        if (directory / "branch.json").exists():
            branch = cls(directory)
            if branch.state["identity"] != identity or branch.state["workspace"] != str(workspace):
                raise ValueError("Branch identity changed")
            return branch
        baseline = manifest(workspace)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "work").mkdir(exist_ok=True)
        for name, expected in baseline.items():
            raw = safe(workspace, name).read_bytes()
            if fingerprint(raw) != expected:
                raise ValueError("Workspace changed while snapshotting")
            atomic(safe(directory / "work", name), raw)
        if manifest(workspace) != baseline:
            raise ValueError("Workspace changed while snapshotting")
        state = dict(version=1, identity=identity, workspace=str(workspace), baseline=baseline, state="working", journal=[], integrated=[])
        atomic(directory / "branch.json", canonical(state).encode())
        return cls(directory)

    def save(self):
        atomic(self.path, canonical(self.state).encode())

    def read(self, name):
        path = safe(self.root, name)
        if path.stat().st_size > MAX_FILE:
            raise ValueError("File too large")
        return path.read_text()

    def write(self, name, content):
        if self.state["state"] != "working" or not isinstance(content, str) or len(content.encode()) > MAX_FILE:
            raise ValueError("Branch is frozen or content exceeds capacity")
        target = safe(self.root, name)
        current = manifest(self.root)
        size = sum(safe(self.root, p).stat().st_size for p in current if p != name) + len(content.encode())
        if size > MAX_TOTAL or len(current) >= 2000 and name not in current:
            raise ValueError("Branch capacity exhausted")
        atomic(target, content.encode())

    def integrate(self, *, fault=lambda point: None):
        workspace = Path(self.state["workspace"])
        if self.state["state"] == "integrated":
            return self.state
        if self.state["state"] == "conflict":
            raise ValueError("Branch conflict requires explicit review and a new baseline")
        if self.state["state"] == "working":
            if manifest(workspace) != self.state["baseline"]:
                self.state.update(state="conflict", reason="Workspace baseline changed")
                self.save()
                raise ValueError("Workspace baseline changed; branch retained for review")
            current = manifest(self.root)
            if set(self.state["baseline"]) - set(current):
                raise ValueError("Branch deletion is unsupported")
            self.state["journal"] = [
                dict(path=name, before=self.state["baseline"].get(name), after=sha) for name, sha in current.items() if self.state["baseline"].get(name) != sha
            ]
            self.state["state"] = "integrating"
            self.save()
        for entry in self.state["journal"]:
            target = safe(workspace, entry["path"])
            observed = fingerprint(target.read_bytes()) if target.exists() else None
            if observed == entry["after"]:
                continue  # Crash after replace and before receipt; exact content proves completion.
            if observed != entry["before"]:
                self.state.update(state="conflict", reason="Integration path changed: " + entry["path"])
                self.save()
                raise ValueError(self.state["reason"])
            raw = safe(self.root, entry["path"]).read_bytes()
            if fingerprint(raw) != entry["after"]:
                raise ValueError("Frozen branch output changed")
            fault("before_write")
            atomic(target, raw)
            fault("after_write")
            self.state["integrated"].append(entry["path"])
            self.save()
        self.state["state"] = "integrated"
        self.save()
        return self.state
