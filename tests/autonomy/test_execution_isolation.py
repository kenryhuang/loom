from pathlib import Path

import pytest

from loom.session_environments.branches import FileBranch


def test_isolated_write_crash_reconcile_idempotent_and_no_shared_writes(tmp_path):
    root = tmp_path / "main"
    root.mkdir()
    (root / "a.txt").write_text("before")
    branch = FileBranch.create(root, tmp_path / "branch", identity={"lane": "one"})
    branch.write("a.txt", "after")
    branch.write("new.txt", "new")
    assert (root / "a.txt").read_text() == "before" and not (root / "new.txt").exists()
    fired = []

    def fault(point):
        if point == "after_write" and not fired:
            fired.append(point)
            raise RuntimeError("process interrupted")

    with pytest.raises(RuntimeError):
        branch.integrate(fault=fault)
    restored = FileBranch(tmp_path / "branch")
    assert restored.integrate()["state"] == "integrated"
    assert restored.integrate()["state"] == "integrated"
    assert (root / "a.txt").read_text() == "after" and (root / "new.txt").read_text() == "new"


def test_changed_baseline_and_symlink_cannot_overwrite_main(tmp_path):
    root = tmp_path / "main"
    root.mkdir()
    (root / "a").write_text("base")
    branch = FileBranch.create(root, tmp_path / "branch", identity={})
    branch.write("a", "branch")
    (root / "a").write_text("human")
    with pytest.raises(ValueError, match="baseline changed"):
        branch.integrate()
    assert (root / "a").read_text() == "human"
    with pytest.raises(ValueError):
        branch.write("../outside", "escape")
    other = FileBranch.create(root, tmp_path / "other", identity={})
    (Path(other.root) / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="Symlink"):
        other.write("link/escape", "no")


def test_model_configuration_pinned_and_unknown_models_refused():
    from types import SimpleNamespace

    from loom.tasks.lane_capabilities import provider_for

    fake = SimpleNamespace(model="model-a")
    policy = SimpleNamespace(provider=fake, lane_providers={"a": fake}, assembly=SimpleNamespace(request=SimpleNamespace(metadata={})))
    lane = {}
    assert provider_for(policy, {"model": "a"}, lane) is fake
    policy.lane_providers["a"] = SimpleNamespace(model="model-b")
    with pytest.raises(ValueError, match="changed"):
        provider_for(policy, {"model": "a"}, lane)
    with pytest.raises(ValueError, match="catalog"):
        provider_for(policy, {"model": "unknown"}, {})
