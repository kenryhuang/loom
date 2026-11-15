import hashlib
import json

import pytest

from loom.service.artifacts import ServiceArtifacts
from loom.service.contracts import ServiceError


def test_publish_read_delete_sharded_roundtrip(tmp_path):
    artifacts = ServiceArtifacts(tmp_path)
    ref = artifacts.publish({"hello": "world"}, "candidate")
    digest = ref["sha256"]
    assert ref["relative_path"] == f"artifacts/{digest[:2]}/{digest}.json"
    assert artifacts.location(digest).exists()
    assert artifacts.location(digest).parent.name == digest[:2]
    assert json.loads(artifacts.read(digest)) == {"hello": "world"}
    # Dedup: same content, same location.
    assert artifacts.publish({"hello": "world"}, "candidate")["sha256"] == digest
    artifacts.delete(digest)
    assert not artifacts.location(digest).exists()
    with pytest.raises(ServiceError) as error:
        artifacts.read(digest)
    assert error.value.status == 404


def test_read_falls_back_to_flat_layout(tmp_path):
    artifacts = ServiceArtifacts(tmp_path)
    ref = artifacts.publish({"v": 1}, "candidate")
    digest = ref["sha256"]
    flat = artifacts.root / f"{digest}.json"
    artifacts.location(digest).replace(flat)  # Simulate a pre-sharding file.
    assert json.loads(artifacts.read(digest)) == {"v": 1}
    artifacts.delete(digest)
    assert not flat.exists()


def test_init_migrates_flat_files_into_shards(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / ".uploads").mkdir()  # Upload staging must be left alone.
    digests = []
    for index in range(3):
        raw = json.dumps({"i": index}).encode()
        digest = hashlib.sha256(raw).hexdigest()
        (root / f"{digest}.json").write_bytes(raw)
        digests.append(digest)
    (root / "tmp stray").write_text("x")  # Crash leftovers stay put.

    artifacts = ServiceArtifacts(tmp_path)
    for index, digest in enumerate(digests):
        assert not (root / f"{digest}.json").exists()
        assert artifacts.location(digest).exists()
        assert json.loads(artifacts.read(digest)) == {"i": index}
    assert (root / ".uploads").is_dir()
    assert (root / "tmp stray").read_text() == "x"

    # Migration is idempotent and a no-op on the second construction.
    ServiceArtifacts(tmp_path)


def test_migration_drops_duplicate_when_shard_already_present(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    raw = b'{"x": 1}'
    digest = hashlib.sha256(raw).hexdigest()
    shard = root / digest[:2]
    shard.mkdir()
    (shard / f"{digest}.json").write_bytes(raw)
    (root / f"{digest}.json").write_bytes(raw)

    artifacts = ServiceArtifacts(tmp_path)
    assert not (root / f"{digest}.json").exists()
    assert artifacts.read(digest) == raw
