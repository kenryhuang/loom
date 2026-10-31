from loom.autonomy.context import build_snapshot
from loom.autonomy.profile import command
from loom.service.scheduler import conflicts
from tests.autonomy.helpers import role
from tests.autonomy.test_profile_learning import explicit


def test_shared_owner_preferences_local_exception_and_isolated_control(agent_service):
    _, facade, aid, workspace = agent_service
    other = facade.store.create("other", role(workspace))
    shared = explicit(aid, facade, scope="owner")
    assert build_snapshot(facade.store, other["id"])["preferences"][0]["id"] == shared["id"]
    local = explicit(other["id"], facade, content="Use English")
    command(facade.store, other["id"], dict(command_id="lock-other", entry_id=local["id"], type="lock", expected_revision=local["revision"]))
    explicit(aid, facade, content="Use Japanese", scope="owner")
    assert build_snapshot(facade.store, other["id"])["preferences"][0]["text"] == "Use English"
    facade.store.command(aid, "pause-a", "pause", facade.store.snapshot(aid)["revision"])
    assert facade.store.snapshot(other["id"])["state"] == "draft"
    assert not facade.store.snapshot(other["id"])["sessions"]


def test_parent_child_directory_conflicts_and_sibling_reads(tmp_path):
    a = [dict(resource_id="directory:" + str(tmp_path), access_mode="exclusive_write")]
    b = [dict(resource_id="directory:" + str(tmp_path / "child"), access_mode="shared_read")]
    assert conflicts(a, b) and conflicts(b, a)
    assert not conflicts(b, [dict(resource_id="directory:" + str(tmp_path / "sibling"), access_mode="exclusive_write")])
    assert not conflicts(b, [{**a[0], "access_mode": "shared_read"}])


def test_session_transfer_is_explicit_revision_checked_and_preserves_prior_run_ownership(agent_service):
    import pytest

    from loom.service.contracts import ServiceError

    service, facade, aid, workspace = agent_service
    target = facade.store.create("transfer-target", role(workspace))
    sid = service.store.create("empty-session", dict(objective=None, workspace=str(workspace)))["session_id"]
    facade.session_command(aid, sid, dict(command_id="bind-source", type="bind", expected_revision=facade.store.snapshot(aid)["revision"]))
    source = facade.store.snapshot(aid)
    with pytest.raises(ServiceError, match="Revision"):
        facade.session_command(
            aid, sid, dict(command_id="stale-transfer", type="transfer", expected_revision=source["revision"], target_agent_id=target["id"], target_revision=0)
        )
    receipt = facade.session_command(
        aid,
        sid,
        dict(command_id="transfer", type="transfer", expected_revision=source["revision"], target_agent_id=target["id"], target_revision=target["revision"]),
    )
    assert receipt["state"] == "transfer"
    assert not facade.store.snapshot(aid)["sessions"]
    assert facade.store.snapshot(target["id"])["sessions"][0]["session_id"] == sid
