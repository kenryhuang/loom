import asyncio
import time
from pathlib import Path

import pytest

from loom.diagnostics.evidence import EvidenceView, capture
from loom.diagnostics.jobs import SessionDiagnosis
from loom.diagnostics.runner import registry_factory, specification
from loom.diagnostics.tools import collection
from loom.diagnostics.validation import backend, run_check
from loom.diagnostics.workspace import DiagnosisWorkspace, git
from loom.service.controller import LoomService
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest
from tests.diagnostics.fakes import factory


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@localhost")
    (root / ".gitignore").write_text(".worktrees/\n.env\n__pycache__/\n.pytest_cache/\n")
    (root / "sample.py").write_text("value = 1\n")
    (root / "delete.py").write_text("old = True\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial")
    (root / "sample.py").write_text("value = 2\n")
    (root / "new.py").write_text("local = True\n")
    (root / "delete.py").unlink()
    (root / ".env").write_text("PRIVATE_KEY=must-not-be-read\n")
    return root


def worktree(tmp_path, repo, cleanup="delete"):
    directory = tmp_path / "diagnosis"
    directory.mkdir()
    return DiagnosisWorkspace.create(directory, repo, "diag_test123", cleanup)


def test_dirty_baseline_patch_cleanup_and_explicit_merge(tmp_path, repo):
    before = git(repo, "status", "--porcelain")
    index = git(repo, "write-tree")
    ws = worktree(tmp_path, repo)
    assert ws.path.parent == repo / ".worktrees"
    assert (ws.path / "sample.py").read_text() == "value = 2\n"
    assert (ws.path / "new.py").exists()
    assert not (ws.path / "delete.py").exists()
    assert not (ws.path / ".env").exists()
    assert git(repo, "write-tree") == index
    assert git(repo, "status", "--porcelain") == before
    (ws.path / "sample.py").write_text("value = 3\n")
    (ws.path / "test_regression.py").write_text("def test_value():\n    assert 3 == 3\n")
    ws.finish()
    assert ws.meta["state"] == "deleted"
    assert not ws.path.exists()
    assert not git(repo, "for-each-ref", "refs/heads/" + ws.meta["branch"])
    patch = (ws.directory / "changes.patch").read_text()
    assert "-value = 2" in patch and "value = 1" not in patch
    assert "test_regression.py" in patch and "new.py" not in patch
    assert (repo / "sample.py").read_text() == "value = 2\n"
    with pytest.raises(ValueError, match="changed"):
        ws.merge("wrong")
    (repo / "unrelated.py").write_text("unrelated = True\n")
    ws.merge(ws.meta["patch_sha256"])
    assert (repo / "sample.py").read_text() == "value = 3\n"
    assert (repo / "test_regression.py").exists()
    assert (repo / "unrelated.py").exists()
    assert git(repo, "write-tree") == index
    ws.merge(ws.meta["patch_sha256"])  # idempotent repeated HTTP request


def test_conflicts_and_symlinks_never_overwrite_source(tmp_path, repo):
    ws = worktree(tmp_path, repo, "keep")
    for name in ["../sample.py", "/tmp/outside", ".git", ".env", "runs/config.py"]:
        with pytest.raises(ValueError):
            ws.resolve(name)
    (ws.path / "link.py").symlink_to(repo / "sample.py")
    with pytest.raises(ValueError, match="escapes|Symlinks"):
        ws.resolve("link.py")
    (ws.path / "link.py").unlink()
    (ws.path / "sample.py").write_text("value = 3\n")
    ws.finish()
    assert ws.path.exists() and ws.meta["state"] == "review"
    (repo / "sample.py").write_text("value = 99\n")
    with pytest.raises(ValueError, match="changed since"):
        ws.merge(ws.meta["patch_sha256"])
    assert (repo / "sample.py").read_text() == "value = 99\n"
    ws.discard()
    ws.discard()


@pytest.mark.skipif(not backend(), reason="OS sandbox unavailable")
def test_real_test_runner_pass_fail_and_host_boundaries(tmp_path, repo):
    ws = worktree(tmp_path, repo)
    try:
        (ws.path / "test_regression.py").write_text(
            "import unittest\nfrom sample import value\nclass Test(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(value, 3)\n"
        )
        failed = asyncio.run(run_check(ws, ["python", "-m", "unittest", "test_regression"], 10))
        assert failed["status"] == "failed", failed
        (ws.path / "sample.py").write_text("value = 3\n")
        passed = asyncio.run(run_check(ws, ["python", "-m", "unittest", "test_regression"], 10))
        assert passed["status"] == "passed", passed
        assert passed["patch_sha256"] != failed["patch_sha256"]
        probe = "from pathlib import Path; import os, socket\n"
        probe += f"targets = [Path({str(repo / 'sample.py')!r}), Path({str(repo / '.env')!r}), Path('.git')]\n"
        probe += "for p in targets:\n try: p.read_text(); raise AssertionError(str(p))\n except (PermissionError, FileNotFoundError): pass\n"
        probe += f"try: Path({str(repo / 'outside.py')!r}).write_text('bad'); raise AssertionError('host write')\n"
        probe += "except (PermissionError, FileNotFoundError): pass\n"
        probe += "assert 'LOOM_LLM_API_KEY' not in os.environ\n"
        probe += "try: socket.create_connection(('127.0.0.1', 8765)); raise AssertionError('network')\nexcept OSError: pass\n"
        check = asyncio.run(run_check(ws, ["python", "-c", probe], 10))
        assert check["status"] == "passed", check
        timed = asyncio.run(run_check(ws, ["python", "-c", "import time; time.sleep(10)"], 1))
        assert timed["status"] == "timeout"
        assert not ws.meta.get("validation_process")
    finally:
        ws.finish()


def test_edit_tools_are_bound_to_worktree_and_citable(tmp_path, repo):
    service = LoomService(tmp_path / "data")
    try:
        sid = service.create("target", {"workspace": str(repo)})["session_id"]
        directory = tmp_path / "snapshot"
        capture(service.store, sid, directory, repo, {})
        ws = DiagnosisWorkspace.create(directory, repo, "diag_tools")
        request = TaskRequest(objective="Diagnose", task_spec=specification(repo, directory, ws.path))
        assembly = TaskAssembly(request, registry=registry_factory())
        assert {"write_file", "edit_file", "run_validation"} <= assembly.bindings.keys()
        tools = collection(request).entrypoints
        denied = asyncio.run(tools["session_diagnostics/write_file"]({"path": "../escape.py", "content": "bad"}))
        assert not denied.ok
        edit = asyncio.run(tools["session_diagnostics/edit_file"]({"path": "sample.py", "old_text": "value = 2", "new_text": "value = 3"}))
        assert edit.ok
        assert edit.value.value["id"] in EvidenceView(directory).ledger()["entries"]
        assert (repo / "sample.py").read_text() == "value = 2\n"
        assert (ws.path / "sample.py").read_text() == "value = 3\n"
        ws.finish()
    finally:
        service.close()


def test_default_worktree_real_worker_cleanup_and_restart_metadata(tmp_path, repo):
    service = LoomService(tmp_path / "data")
    manager = SessionDiagnosis(service, root=repo, provider_factory=factory)
    try:
        sid = service.create("target", {"workspace": str(repo)})["session_id"]
        original = service.store.snapshot(sid)
        job = manager.start(sid, {"request_id": "verify"})
        assert job["mode"] == "worktree"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = manager.get(sid, job["id"])
            if job.get("workspace", {}).get("state") == "deleted":
                break
            time.sleep(0.05)
        assert job["state"] == "completed", job
        assert job["workspace"]["state"] == "deleted", job
        assert not Path(job["workspace"]["path"]).exists()
        assert manager.changes(sid, job["id"])["patch"] == ""
        assert service.store.snapshot(sid) == original
    finally:
        manager.close()
        service.close()


@pytest.mark.skipif(not backend(), reason="OS sandbox unavailable")
def test_full_loop_writes_regression_verifies_and_publishes_patch(tmp_path, repo):
    from loom.service.contracts import ServiceError
    from tests.diagnostics.fakes import verification_factory

    service = LoomService(tmp_path / "data")
    manager = SessionDiagnosis(service, root=repo, provider_factory=verification_factory)
    try:
        sid = service.create("target", {"workspace": str(repo)})["session_id"]
        other = service.create("other", {"workspace": str(repo)})["session_id"]
        job = manager.start(sid, {"request_id": "verify", "cleanup": "keep"})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = manager.get(sid, job["id"])
            if job.get("workspace", {}).get("state") == "review":
                break
            time.sleep(0.05)
        assert job["state"] == "completed", job
        assert job["workspace"]["state"] == "review", job
        assert [c["status"] for c in job["workspace"]["checks"]] == ["failed", "passed"]
        assert {"sample.py", "test_regression.py"} == set(job["workspace"]["files"])
        assert (repo / "sample.py").read_text() == "value = 2\n"
        with pytest.raises(ServiceError, match="not found"):
            manager.workspace_action(other, job["id"], "merge", {"patch_sha256": job["workspace"]["patch_sha256"]})
        manager.workspace_action(sid, job["id"], "discard", {})
        assert not Path(job["workspace"]["path"]).exists()
        # Saved patch remains reviewable and mergeable after default or explicit cleanup.
        done = manager.workspace_action(sid, job["id"], "merge", {"patch_sha256": job["workspace"]["patch_sha256"]})
        assert done["workspace"]["state"] == "merged"
        assert (repo / "sample.py").read_text() == "value = 3\n"
    finally:
        manager.close()
        service.close()


def test_worktree_budget_pause_retains_workspace_and_cancel_cleans_it(tmp_path, repo):
    service = LoomService(tmp_path / "data")
    manager = SessionDiagnosis(service, root=repo, provider_factory=factory)
    try:
        sid = service.create("target", {"workspace": str(repo)})["session_id"]
        job = manager.start(sid, {"request_id": "pause", "budgets": {"max_tool_calls": 1}})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = manager.get(sid, job["id"])
            if job["state"] == "budget_exhausted" and not manager.runtime.active:
                break
            time.sleep(0.05)
        assert job["state"] == "budget_exhausted", job
        path = Path(job["workspace"]["path"])
        assert path.exists()
        manager.cancel(sid, job["id"])
        assert manager.get(sid, job["id"])["workspace"]["state"] == "deleted"
        assert not path.exists()
    finally:
        manager.close()
        service.close()
