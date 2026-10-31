import pytest

from loom.skills.promotion import promote
from tests.skills.test_runtime_validation import version_fixture


def test_promotion_new_scope_candidate_requires_target_validation_and_source_revocation(tmp_path):
    _, store, _, source = version_fixture(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    environment = {k + "_digest": "a" * 64 for k in ("environment", "solver", "tools", "permissions", "evaluator", "knowledge", "memory", "budget")}
    candidate = promote(
        store,
        source["id"],
        revision=source["revision"],
        target_workspace=target,
        conditions=["Only HTTP services with executable acceptance"],
        environment=environment,
    )
    assert candidate["workspace"] == str(target) and candidate["state"] == "candidate"
    assert not any(c.get("evidence_refs") for c in candidate["contract"]["claims"])
    assert store.version(source["id"])["workspace"] == str(tmp_path)
    assert store.check_version(candidate["id"])["sources_available"]
    assert (
        promote(
            store,
            source["id"],
            revision=source["revision"],
            target_workspace=target,
            conditions=["Only HTTP services with executable acceptance"],
            environment=environment,
        )["id"]
        == candidate["id"]
    )
    with pytest.raises(ValueError, match="executable validation"):
        store.transition(candidate["id"], "active", candidate["revision"])
    store.transition(source["id"], "retired", source["revision"])
    assert store.check_version(candidate["id"])["state"] == "suspended"
