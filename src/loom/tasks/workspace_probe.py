"""Bounded, read-only project discovery. Never executes discovered configuration."""

import hashlib
import json
from pathlib import Path

IGNORE = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build", "runs"}
CONFIGS = {"pyproject.toml", "package.json", "Makefile", "Cargo.toml", "go.mod", "pytest.ini", "tox.ini", "setup.cfg", "requirements.txt"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def bound_path(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("Acceptance paths must be workspace-relative")
    path = (Path(root) / name).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError("Acceptance path leaves the bound workspace")
    if any(part.startswith(".env") or part.lower() in {"credentials", "secrets", ".ssh"} for part in Path(name).parts):
        raise ValueError("Acceptance cannot inspect secret files")
    return path


def probe(root, *, max_entries=200, max_bytes=24000):
    result = {"workspace": "present" if root else "absent", "files": [], "configuration": [], "limitations": []}
    if root:
        root = Path(root).resolve()
        pending, used = [(root, 0)], 0
        while pending and len(result["files"]) < max_entries:
            directory, depth = pending.pop(0)
            try:
                entries = sorted(directory.iterdir(), key=lambda p: p.name)
            except OSError:
                result["limitations"].append("A directory could not be read")
                continue
            for path in entries:
                if len(result["files"]) >= max_entries:
                    break
                if path.name in IGNORE or path.is_symlink() or path.name.startswith(".env") or path.name.lower() in {"credentials", "secrets", ".ssh"}:
                    continue
                name = path.relative_to(root).as_posix()
                result["files"].append(name + ("/" if path.is_dir() else ""))
                if path.is_dir() and depth < 2:
                    pending.append((path, depth + 1))
                elif path.is_file() and (path.name in CONFIGS or path.name.lower().startswith("readme")) and used < max_bytes:
                    try:
                        with path.open("rb") as stream:
                            raw = stream.read(min(4000, max_bytes - used) + 1)
                        limit = min(4000, max_bytes - used)
                        result["configuration"].append(
                            {
                                "path": name,
                                "excerpt": raw[:limit].decode("utf-8", "replace"),
                                "excerpt_sha256": hashlib.sha256(raw[:limit]).hexdigest(),
                                "truncated": len(raw) > limit,
                            }
                        )
                        used += min(len(raw), limit)
                    except OSError:
                        result["limitations"].append(f"Could not read {name}")
        if pending or len(result["files"]) >= max_entries:
            result["limitations"].append("Directory inventory is bounded; omitted paths have not been inspected")
    result["fingerprint"] = digest(result)
    return result


def manifest(root, scope):
    """Missing files are explicit; large files cannot silently become verified."""
    result = {}
    for name in sorted(set(scope)):
        path = bound_path(root, name)
        if not path.exists():
            result[name] = None
        elif not path.is_file() or path.stat().st_size > 8_000_000:
            raise ValueError("Verification scope requires files up to 8 MB")
        else:
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result
