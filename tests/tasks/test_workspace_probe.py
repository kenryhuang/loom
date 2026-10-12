import pytest

from loom.tasks.workspace_probe import manifest


def test_directory_fingerprint_tracks_contents_membership_and_missing_paths(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    scope = ["src/", "missing.ts"]
    empty = manifest(tmp_path, scope)
    assert empty["missing.ts"] is None
    (source / "a.ts").write_text("first")
    added = manifest(tmp_path, scope)
    assert added != empty
    assert manifest(tmp_path, list(reversed(scope))) == added
    (source / "a.ts").write_text("second")
    assert manifest(tmp_path, scope) != added
    (source / "a.ts").unlink()
    assert manifest(tmp_path, scope) == empty
    (source / "nested").mkdir()
    assert manifest(tmp_path, scope) != empty


def test_directory_scope_blocks_secrets_and_symlinks(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / ".env").write_text("secret")
    with pytest.raises(ValueError, match="secret files"):
        manifest(tmp_path, ["src"])
    (source / ".env").unlink()
    (source / "loop").symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link: src/loop"):
        manifest(tmp_path, ["src"])
    (source / "loop").unlink()
    (source / "outside").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="leaves the bound workspace"):
        manifest(tmp_path, ["src"])


def test_directory_manifest_limits_fail_explicitly_without_partial_receipts(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.ts").write_bytes(b"12345")
    (source / "b.ts").write_bytes(b"67890")
    assert manifest(tmp_path, ["src"], max_entries=3, max_file_bytes=5, max_total_bytes=10)
    with pytest.raises(ValueError, match="exceeds 2 entries"):
        manifest(tmp_path, ["src"], max_entries=2)
    with pytest.raises(ValueError, match=r"file exceeds 4 bytes: src/"):
        manifest(tmp_path, ["src"], max_file_bytes=4)
    with pytest.raises(ValueError, match="exceeds 9 total bytes"):
        manifest(tmp_path, ["src"], max_total_bytes=9)
