"""Host-owned Git lifecycle. Diagnostic edits never share the user's index or branch."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from loom.diagnostics.evidence import write_json

MAX_FILE = 2_000_000
MAX_PATCH = 8_000_000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def allowed(path):
    parts = Path(path).parts
    return (
        bool(parts)
        and not Path(path).is_absolute()
        and all(
            part not in {"..", "runs", "node_modules", "venv", "env", "__pycache__"}
            and (not part.startswith(".") or part == ".gitignore")
            and not re.search(r"(?i)(^config\.ya?ml$|credential|secret|\.pem$|\.key$)", part)
            for part in parts
        )
    )


def git(root, *args, env=None, data=None):
    result = subprocess.run(
        ["git", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(root), *args],
        input=data,
        capture_output=True,
        env={**os.environ, **(env or {})},
        timeout=60,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace")[:2000].strip() or "Git operation failed")
    return result.stdout


class DiagnosisWorkspace:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.file = self.directory / "workspace.json"
        self.meta = json.loads(self.file.read_text()) if self.file.exists() else None

    def save(self):
        write_json(self.file, self.meta)

    @property
    def path(self):
        return Path(self.meta["path"])

    @classmethod
    def create(cls, directory, root, job_id, cleanup="delete", excluded=()):
        if not re.fullmatch(r"diag_[a-z0-9]+", job_id):
            raise ValueError("Invalid diagnosis workspace identity")
        obj = cls(directory)
        root = Path(root).resolve()
        if Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
            raise ValueError("Worktree verification requires the Loom repository root")
        head = git(root, "rev-parse", "HEAD").decode().strip()
        branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD").decode().strip()
        parent = root / ".worktrees"
        if parent.is_symlink():
            raise ValueError(".worktrees must not be a symlink")
        parent.mkdir(exist_ok=True)
        path = parent / job_id
        if path.exists():
            raise ValueError("Diagnosis worktree already exists")
        obj.meta = {
            "path": str(path),
            "root": str(root),
            "branch": "loom-diagnosis/" + job_id,
            "target_branch": branch,
            "head": head,
            "cleanup": cleanup,
            "state": "preparing",
            "checks": [],
            "excluded": [],
        }
        obj.save()  # Persist ownership before any Git mutation, including crash recovery.
        files = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
        entries = []
        total = 0
        for name in sorted(set(files) - {""}):
            source = root / name
            if not allowed(name) or source.is_symlink() or any(source.resolve().is_relative_to(Path(p).resolve()) for p in excluded):
                obj.meta["excluded"].append(name)
                continue
            if not source.is_file():
                continue  # Preserve local deletions; do not silently resurrect HEAD content.
            if source.stat().st_size > MAX_FILE:
                raise ValueError(f"Workspace baseline file exceeds 2 MB: {name}")
            raw = source.read_bytes()
            total += len(raw)
            if total > 64_000_000:
                raise ValueError("Workspace baseline exceeds 64 MB")
            oid = git(root, "hash-object", "-w", "--stdin", data=raw).decode().strip()
            mode = "100755" if source.stat().st_mode & 0o111 else "100644"
            entries.append(f"{mode} {oid}\t{name}\0".encode())
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            env = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            git(root, "read-tree", "--empty", env=env)
            git(root, "update-index", "-z", "--index-info", env=env, data=b"".join(entries))
            tree = git(root, "write-tree", env=env).decode().strip()
        author = {
            "GIT_AUTHOR_NAME": "Loom diagnosis",
            "GIT_AUTHOR_EMAIL": "diagnosis@localhost",
            "GIT_COMMITTER_NAME": "Loom diagnosis",
            "GIT_COMMITTER_EMAIL": "diagnosis@localhost",
        }
        baseline = git(root, "commit-tree", tree, "-p", head, env=author, data=b"Private diagnosis baseline\n").decode().strip()
        obj.meta["baseline"] = baseline
        obj.save()
        git(root, "worktree", "add", "-b", obj.meta["branch"], str(path), baseline)
        obj.meta["state"] = "active"
        obj.save()
        return obj

    def resolve(self, name):
        if self.meta["state"] != "active":
            raise ValueError("Diagnosis workspace is no longer editable")
        if not isinstance(name, str) or not allowed(name):
            raise ValueError("Only non-sensitive worktree-relative files are available")
        target = self.path / name
        if not target.resolve().is_relative_to(self.path.resolve()):
            raise ValueError("Path escapes diagnosis worktree")
        if any(p.is_symlink() for p in [target, *target.parents] if p != self.path.parent):
            raise ValueError("Symlinks are not available to diagnosis tools")
        return target

    def patch(self):
        """Build a delta against the private baseline using an alternate index (including new tests)."""
        root = self.path
        baseline = self.meta["baseline"]
        names = set(git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")) - {""}
        with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
            env = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            git(root, "read-tree", baseline, env=env)
            paths = []
            for name in sorted(names):
                target = root / name
                if not allowed(name) or target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
                    # Ignored output is excluded, but unsafe tracked changes cannot enter a patch.
                    if git(root, "ls-files", "--", name).strip():
                        raise ValueError(f"Unsafe workspace change: {name}")
                    continue
                if target.exists() and target.stat().st_size > MAX_FILE:
                    raise ValueError(f"Changed file exceeds 2 MB: {name}")
                paths.append(name)
            if paths:
                git(root, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", env=env, data="\0".join(paths).encode() + b"\0")
            patch = git(root, "diff", "--cached", "--binary", "--no-ext-diff", "--no-renames", baseline, env=env)
            files = git(root, "diff", "--cached", "--name-only", "-z", baseline, env=env).decode().split("\0")
        if len(patch) > MAX_PATCH:
            raise ValueError("Diagnosis patch exceeds 8 MB; workspace retained")
        return patch, [p for p in files if p]

    def reap_validation(self):
        running = self.meta.get("validation_process")
        if running:
            from loom.service.workers import reap_group

            if not reap_group(running["pid"], running["identity"]):
                raise ValueError("Validation subprocesses are still stopping; workspace retained")
            self.meta.pop("validation_process", None)
            self.save()

    def finish(self):
        if self.meta["state"] != "active":
            return
        patch, files = self.patch()
        (self.directory / "changes.patch").write_bytes(patch)
        before = {}
        for name in files:
            listed = git(self.path, "ls-tree", self.meta["baseline"], "--", name)
            if listed:
                mode, _, oid = listed.split(b"\t", 1)[0].decode().split()
                before[name] = {"sha256": digest(git(self.path, "cat-file", "blob", oid)), "executable": mode == "100755"}
            else:
                before[name] = None
        self.meta.update(state="review", files=files, before=before, patch_sha256=digest(patch), patch_bytes=len(patch))
        self.save()  # Patch and review state are durable before cleanup.
        if self.meta["cleanup"] == "delete":
            self.discard()

    def discard(self):
        root = Path(self.meta["root"])
        # Only remove our recorded worktree and branch; never prune arbitrary worktrees.
        registered = git(root, "worktree", "list", "--porcelain").decode()
        if f"worktree {self.path}\n" in registered:
            git(root, "worktree", "remove", "--force", str(self.path))
        refs = git(root, "for-each-ref", "--format=%(refname)", "refs/heads/" + self.meta["branch"]).decode().splitlines()
        if "refs/heads/" + self.meta["branch"] in refs:
            git(root, "branch", "-D", self.meta["branch"])
        scratch = self.directory / "scratch"
        if scratch.is_dir():
            shutil.rmtree(scratch)
        if self.meta["state"] != "merged":
            self.meta["state"] = "deleted"
        self.save()

    def merge(self, expected_sha):
        if self.meta["state"] == "merged":
            if expected_sha != self.meta.get("patch_sha256"):
                raise ValueError("Reviewed patch has changed")
            return
        if self.meta["state"] not in {"review", "deleted"} or not self.meta.get("files"):
            raise ValueError("No reviewed changes are available to merge")
        patch = (self.directory / "changes.patch").read_bytes()
        if digest(patch) != expected_sha or expected_sha != self.meta["patch_sha256"]:
            raise ValueError("Reviewed patch has changed; refresh before merging")
        root = Path(self.meta["root"])
        if git(root, "symbolic-ref", "--short", "HEAD").decode().strip() != self.meta["target_branch"]:
            raise ValueError("Target branch changed; switch back before merging")
        if git(root, "rev-parse", "HEAD").decode().strip() != self.meta["head"]:
            raise ValueError("Target HEAD changed; start a new diagnosis or apply the patch manually")
        for name, before in self.meta["before"].items():
            target = root / name
            if any(p.is_symlink() for p in [target, *target.parents]):
                raise ValueError(f"Target path became a symlink: {name}")
            current = {"sha256": digest(target.read_bytes()), "executable": bool(target.stat().st_mode & 0o111)} if target.is_file() else None
            if current != before or (before is None and target.exists()):
                raise ValueError(f"Target file changed since diagnosis: {name}; merge refused")
        # No --index, staging, stash or commit: preserve the user's current index and unrelated edits.
        git(root, "apply", "--check", "--whitespace=nowarn", "-", data=patch)
        git(root, "apply", "--whitespace=nowarn", "-", data=patch)
        self.meta["state"] = "merged"
        self.save()
        self.discard()

    def public(self):
        return {k: v for k, v in self.meta.items() if k not in {"before", "validation_process"}}
