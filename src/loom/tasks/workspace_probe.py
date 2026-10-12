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


def manifest(root, scope, *, max_entries=2048, max_file_bytes=8_000_000, max_total_bytes=32_000_000):
    """Hash complete scoped trees, including membership; never silently truncate."""
    result = {}
    entries, total_bytes = 0, 0
    for name in sorted(set(scope)):
        pending = [name]
        tree = {}
        while pending:
            current = pending.pop()
            entries += 1
            if entries > max_entries:
                raise ValueError(f"Verification scope exceeds {max_entries} entries; narrow scope near {current}")
            # Do not follow symlinks in trees, including links to ancestor directories.
            path = bound_path(root, current)
            lexical = Path(root).resolve()
            for part in Path(current).parts:
                lexical /= part
                if lexical.is_symlink():
                    raise ValueError(f"Verification scope contains a symbolic link: {current}")
            if not path.exists():
                tree[current] = None
            elif path.is_dir():
                tree[current] = {"type": "directory"}
                # Bound enumeration itself, not just the subsequent file reads.
                for child in path.iterdir():
                    if entries + len(pending) >= max_entries:
                        raise ValueError(f"Verification scope exceeds {max_entries} entries; narrow scope near {current}")
                    pending.append((Path(current) / child.name).as_posix())
            elif path.is_file():
                if path.stat().st_size > max_file_bytes:
                    raise ValueError(f"Verification file exceeds {max_file_bytes} bytes: {current}")
                with path.open("rb") as stream:
                    raw = stream.read(min(max_file_bytes, max_total_bytes - total_bytes) + 1)
                total_bytes += len(raw)
                if len(raw) > max_file_bytes:
                    raise ValueError(f"Verification file exceeds {max_file_bytes} bytes: {current}")
                if total_bytes > max_total_bytes:
                    raise ValueError(f"Verification scope exceeds {max_total_bytes} total bytes near {current}")
                tree[current] = hashlib.sha256(raw).hexdigest()
            else:
                raise ValueError(f"Verification scope requires a regular file or directory: {current}")
        result[name] = digest(tree) if isinstance(tree[name], dict) else tree[name]
    return result
