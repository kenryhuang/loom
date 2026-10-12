"""Immutable, sanitized source and session views; never expose a live data root."""

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from loom.core import now_iso
from loom.service.contracts import ServiceError, canonical

PAGE = 16000
EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", "runs", "dist", "build", ".ssh", "secrets", "credentials"}
EXTENSIONS = {".py", ".mjs", ".js", ".ts", ".tsx", ".css", ".html", ".md", ".toml"}
SECRET = re.compile(r"api[_-]?key|secret|password|credential|authorization|access[_-]?token", re.I)


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {
            str(k): (
                {str(name): redact(env, secrets) for name, env in v.items()}
                if (str(k) in {"headers_env", "env"} or str(k).endswith("_present")) and isinstance(v, dict)
                else "[REDACTED]"
                if SECRET.search(str(k)) and not str(k).endswith(("_env", "_present"))
                else redact(v, secrets)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret and len(secret) >= 6:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"'<>\\]+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
        value = re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED]@", value)
        value = re.sub(r'(?i)([?&](?:token|key|api_key|access_token|signature)=)[^&\s#"\x27]+', r"\1[REDACTED]", value)
        value = re.sub(r"(?i)((?:api[_-]?key|password|secret|authorization)\s*[\"\x27]?\s*[:=]\s*[\"\x27]?)[^\s\"\x27,}]+", r"\1[REDACTED]", value)
    return value


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(canonical(value), encoding="utf-8")
    os.replace(temporary, path)


def source_root(configured=None):
    import loom

    if configured:
        root = Path(configured).expanduser().resolve()
        if not root.is_dir():
            raise ServiceError("Diagnosis source_root is not a directory", 422)
        return root
    module = Path(loom.__file__).resolve()
    for parent in module.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src/loom").is_dir():
            return parent
    return module.parent


def safe_source(path, root, data_root):
    relative = path.relative_to(root)
    if any(part.lower() in EXCLUDED or part.startswith(".") for part in relative.parts):
        return False
    if path.is_symlink() or path.resolve().is_relative_to(data_root):
        return False
    if path.name.lower().startswith(("config.", "credential")) or SECRET.search(path.name):
        return False
    return path.suffix in EXTENSIONS


def refs(value):
    if isinstance(value, dict):
        if isinstance(value.get("sha256"), str) and re.fullmatch("[0-9a-f]{64}", value["sha256"]):
            yield value["sha256"]
        for child in value.values():
            yield from refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from refs(child)


def capture(store, sid, directory, root, config_view, secrets=(), *, requested_run="latest"):
    """Capture raw envelopes without the trajectory adapter; artifacts are session-owned."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    target = sqlite3.connect(directory / "evidence.sqlite")
    target.executescript(
        "CREATE TABLE events(seq INTEGER PRIMARY KEY, run_id TEXT, type TEXT, body TEXT);"
        "CREATE TABLE evidence(id TEXT PRIMARY KEY, body TEXT);"
        "CREATE TABLE source(path TEXT PRIMARY KEY, sha256 TEXT, body TEXT);"
    )
    artifact_ids, runs = set(), []
    # A read transaction pins both snapshot and event selection without blocking writers.
    db = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    missing, total = [], 0
    try:
        db.execute("BEGIN")
        row = db.execute("SELECT body FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise ServiceError("Session not found", 404)
        state = json.loads(row[0])
        if state.get("deleted_at"):
            raise ServiceError("Session not found", 404)
        cursor = state["event_cursor"]
        for (body,) in db.execute("SELECT body FROM events WHERE session_id=? AND seq<=? ORDER BY seq", (sid, cursor)):
            event = json.loads(body)
            if event.get("run_id") and event["run_id"] not in runs:
                runs.append(event["run_id"])
            artifact_ids.update(refs(event))
            clean = canonical(redact(event, secrets))
            total += len(clean)
            if total > 64_000_000:
                raise ServiceError("Session snapshot exceeds 64M characters; select a smaller source session", 422)
            target.execute("INSERT INTO events VALUES(?,?,?,?)", (event["seq"], event.get("run_id"), event["type"], clean))
        artifact_ids.update(refs(state))
        owned = {r[0] for r in db.execute("SELECT digest FROM artifacts WHERE session_id=?", (sid,))}
        db.commit()
        selected = runs[-1:] if requested_run == "latest" else runs if requested_run == "all" else [requested_run]
        if any(run not in runs for run in selected):
            raise ServiceError("Selected run does not belong to this snapshot", 422)
        seen = set()
        while artifact_ids - seen:
            digest = sorted(artifact_ids - seen)[0]
            seen.add(digest)
            if digest not in owned:
                missing.append(digest)
                continue
            try:
                # Do not materialize unbounded artifacts in the diagnostic model input.
                if (store.artifacts.root / f"{digest}.json").stat().st_size > 8_000_000:
                    raise ValueError("Artifact exceeds snapshot per-file limit")
                value = json.loads(store.read_artifact(sid, digest))
                artifact_ids.update(refs(value))
                clean = canonical(redact(value, secrets))
                total += len(clean)
                if total > 96_000_000:
                    raise ValueError("Artifact snapshot byte budget reached")
                target.execute("INSERT OR IGNORE INTO evidence VALUES(?,?)", (f"artifact:{digest}", clean))
            except (ValueError, OSError, ServiceError):
                missing.append(digest)
        source_count, source_bytes, omitted = 0, 0, 0
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if d.lower() not in EXCLUDED
                and not d.startswith(".")
                and not (Path(folder) / d).is_symlink()
                and not (Path(folder) / d).resolve().is_relative_to(store.directory)
            )
            for name in sorted(files):
                path = Path(folder) / name
                if not safe_source(path, root, store.directory):
                    continue
                if source_count >= 5000 or path.stat().st_size > 1_000_000 or source_bytes + path.stat().st_size > 32_000_000:
                    omitted += 1
                    continue
                try:
                    with path.open("rb") as stream:
                        raw = stream.read(1_000_001)
                    if len(raw) > 1_000_000 or source_bytes + len(raw) > 32_000_000:
                        omitted += 1
                        continue
                    value = redact(raw.decode("utf-8"), secrets)
                except (OSError, UnicodeError):
                    omitted += 1
                    continue
                target.execute("INSERT INTO source VALUES(?,?,?)", (path.relative_to(root).as_posix(), hashlib.sha256(raw).hexdigest(), value))
                source_count += 1
                source_bytes += len(raw)
        from importlib.metadata import version

        import loom

        view = {
            "session_id": sid,
            "source_cursor": cursor,
            "run_ids": runs,
            "selected_run_ids": selected,
            "captured_at": now_iso(),
            "source_root": str(root),
            "data_dir": str(store.directory),
            "source_identity": {
                "package_version": version("loom"),
                "loaded_module": str(Path(loom.__file__).resolve()),
                "source_contains_loaded_module": Path(loom.__file__).resolve().is_relative_to(root),
                "temporal_scope": "current_source_snapshot; historical source identity may be unavailable",
            },
            "source_files": source_count,
            "source_omitted": omitted,
            "missing_artifacts": sorted(set(missing)),
            "state": redact({k: state.get(k) for k in ("session_id", "title", "task", "run", "acceptance", "plan")}, secrets),
            "config": redact(config_view, secrets),
        }
        # The source identity covers the actual file manifest, including dirty working-tree content.
        view["source_identity"]["manifest_sha256"] = hashlib.sha256(
            canonical(target.execute("SELECT path,sha256 FROM source ORDER BY path").fetchall()).encode()
        ).hexdigest()
        target.execute("INSERT INTO evidence VALUES(?,?)", ("snapshot:overview", canonical(view)))
        target.commit()
        write_json(directory / "manifest.json", {k: v for k, v in view.items() if k not in {"state", "config"}})
        write_json(directory / "ledger.json", {"read_chars": 0, "tool_calls": 0, "entries": {}})
        return view
    finally:
        db.close()
        target.close()


class EvidenceView:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.manifest = json.loads((self.directory / "manifest.json").read_text())

    def query(self, sql, parameters=()):
        with sqlite3.connect(f"file:{self.directory / 'evidence.sqlite'}?mode=ro", uri=True) as db:
            return db.execute(sql, parameters).fetchall()

    def record(self, identity, value, *, start=0, limit=PAGE):
        if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= PAGE:
            raise ValueError("Evidence page requires start >= 0 and limit between 1 and 16000")
        text = value if isinstance(value, str) else canonical(value)
        chunk = text[start : start + limit]
        key = f"{identity}@{start}:{start + len(chunk)}"
        ledger = json.loads((self.directory / "ledger.json").read_text())
        if ledger["read_chars"] + len(chunk) > 1_000_000:
            raise ValueError("Diagnosis cumulative evidence read budget exhausted")
        ledger["read_chars"] += len(chunk)
        page = {
            "id": key,
            "content": chunk,
            "start": start,
            "total_chars": len(text),
            "next_start": start + len(chunk) if start + len(chunk) < len(text) else None,
            "view_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "sanitized": True,
        }
        ledger["entries"][key] = page
        write_json(self.directory / "ledger.json", ledger)
        return page

    def read(self, identity, *, field="", **page):
        rows = self.query("SELECT body FROM evidence WHERE id=?", (identity,))
        if not rows:
            raise ValueError("Evidence is missing or outside this session snapshot")
        value = json.loads(rows[0][0])
        if field:
            if not isinstance(field, str) or not field.startswith("/") or len(field) > 512:
                raise ValueError("field must be a bounded JSON pointer such as /evidence")
            try:
                for part in field[1:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    value = value[int(part)] if isinstance(value, list) and part.isdecimal() else value[part]
            except (IndexError, KeyError, TypeError) as exc:
                raise ValueError("Artifact field is unavailable") from exc
        result = self.record(identity + ("#" + field if field else ""), value, **page)
        if isinstance(value, dict):
            result["fields"] = list(value)[:100]
        return result

    def ledger(self):
        return json.loads((self.directory / "ledger.json").read_text())
