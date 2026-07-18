from __future__ import annotations

import hashlib
from pathlib import Path

from loom.campaigns.artifacts import ArtifactStore
from loom.campaigns.contracts import ArtifactRef


def test_artifact_store_publishes_content_atomically_and_deduplicates(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")

    first = store.publish_bytes(b'{"answer":42}', kind="candidate", schema_version="candidate.v1", suffix=".json")
    second = store.publish_bytes(b'{"answer":42}', kind="candidate", schema_version="candidate.v1", suffix=".json")

    assert first.ok and second.ok
    assert first.value == second.value
    assert first.value.sha256 == hashlib.sha256(b'{"answer":42}').hexdigest()
    assert not Path(first.value.relative_path).is_absolute()
    assert store.read_bytes(first.value).unwrap() == b'{"answer":42}'
    assert not tuple((tmp_path / "artifacts").rglob("*.tmp"))


def test_artifact_reader_rejects_size_and_digest_mismatch(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    published = store.publish_bytes(b"trusted", kind="evidence", schema_version="evidence.v1").unwrap()
    path = store.resolve(published).unwrap()
    path.write_bytes(b"tampered")

    result = store.read_bytes(published)

    assert not result.ok
    assert result.error.code == "ARTIFACT_INTEGRITY_FAILED"


def test_artifact_reader_rejects_absolute_traversal_and_symlink_escape(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    link = tmp_path / "artifacts" / "link"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    digest = hashlib.sha256(b"secret").hexdigest()

    unsafe_refs = (
        ArtifactRef.unsafe("v1", "evidence", str(outside), digest, 6),
        ArtifactRef.unsafe("v1", "evidence", "../secret.txt", digest, 6),
        ArtifactRef.unsafe("v1", "evidence", "link", digest, 6),
    )

    for ref in unsafe_refs:
        result = store.resolve(ref)
        assert not result.ok
        assert result.error.code == "ARTIFACT_PATH_INVALID"


def test_artifact_reader_rejects_unexpected_schema_before_parsing(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.publish_bytes(b"{}", kind="campaign", schema_version="campaign.v1", suffix=".json").unwrap()

    result = store.read_bytes(ref, expected_schema="campaign.v2")

    assert not result.ok
    assert result.error.code == "UNSUPPORTED_SCHEMA"
