import json

import pytest

from loom.service.contracts import ServiceError, canonical
from loom.service.semantic import SessionSemantic
from tests.service.test_semantic import evaluation as evaluation
from tests.service.test_semantic import wait_job


def completed(semantic, sid, aid, **options):
    job = semantic.start(sid, aid, {"analysis_version": "v2", **options})
    result = wait_job(semantic, sid, aid, job["id"])
    assert result["state"] == "completed", result
    return result


def test_delete_removes_metadata_records_outputs_and_cache_but_keeps_source(evaluation):
    store, trajectory, semantic, _, sid, aid = evaluation
    original = store.snapshot(sid)
    parent = trajectory.get(sid, aid)
    job = completed(semantic, sid, aid)
    digest = job["result"]["sha256"]
    assert (store.artifacts.root / f"{digest}.json").exists()
    assert semantic.delete(sid, aid, job["id"]) == {"id": job["id"], "deleted": True}
    with store.transaction() as db:
        assert not db.execute("SELECT 1 FROM semantic_jobs WHERE id=?", (job["id"],)).fetchone()
        assert not db.execute("SELECT 1 FROM evaluation_artifacts").fetchone()
        assert not db.execute("SELECT 1 FROM evaluation_deletions").fetchone()
        assert not db.execute("SELECT 1 FROM artifacts WHERE digest=?", (digest,)).fetchone()
    assert not (store.artifacts.root / f"{digest}.json").exists()
    assert not semantic.list(sid, aid)["jobs"]
    with pytest.raises(ServiceError) as error:
        semantic.get(sid, aid, job["id"])
    assert error.value.status == 404
    with pytest.raises(ServiceError):
        store.read_artifact(sid, digest)
    assert store.snapshot(sid) == original
    assert trajectory.get(sid, aid) == parent
    store.artifacts.read(parent["source"]["sha256"])
    store.artifacts.read(parent["result"]["sha256"])
    assert completed(semantic, sid, aid)["id"] != job["id"]


def test_delete_cleans_all_revisions_and_legacy_orphans_preserves_other_job(evaluation):
    store, _, semantic, _, sid, aid = evaluation
    job = completed(semantic, sid, aid)
    other = completed(semantic, sid, aid, max_calls=81)
    # Identical v2 results are content-addressed and shared by these two jobs.
    assert job["result"]["sha256"] == other["result"]["sha256"]
    digests = []
    with store.transaction() as db:
        prior = store.artifact_in_transaction(db, sid, {"old_revision": 1}, "trajectory_evaluation")
        semantic.outputs.track(db, job, prior)
        digests.append(prior["sha256"])
        for payload in ({"analysis_id": job["id"], "old_revision": 2}, {"old_v2_orphan": True}):
            digests.append(store.artifact_in_transaction(db, sid, payload, "trajectory_evaluation")["sha256"])
        other_prior = store.artifact_in_transaction(db, sid, {"analysis_id": other["id"], "old_revision": 3}, "trajectory_evaluation")
    semantic.delete(sid, aid, job["id"])
    for digest in digests:
        assert not (store.artifacts.root / f"{digest}.json").exists()
    assert semantic.get(sid, aid, other["id"])["evaluation"] == other["evaluation"]
    store.artifacts.read(other_prior["sha256"])
    semantic.delete(sid, aid, other["id"])
    assert not (store.artifacts.root / f"{other_prior['sha256']}.json").exists()
    assert not (store.artifacts.root / f"{job['result']['sha256']}.json").exists()


def test_delete_keeps_cross_session_registration_and_explicit_trace_reference(evaluation, tmp_path):
    store, _, semantic, _, sid, aid = evaluation
    job = completed(semantic, sid, aid)
    other_sid = store.create("other", {"objective": "Other", "workspace": str(tmp_path)})["session_id"]
    ref = job["result"]
    with store.transaction() as db:
        db.execute("INSERT INTO artifacts VALUES(?,?,?)", (other_sid, ref["sha256"], canonical(ref)))
    retained = store.save_artifact(sid, {"explicitly_referenced": True}, "trajectory_evaluation")
    store.update(sid, lambda state, emit, db: emit("test.reference", {"artifact": retained}))
    semantic.delete(sid, aid, job["id"])
    store.read_artifact(other_sid, ref["sha256"])
    store.read_artifact(sid, retained["sha256"])
    with pytest.raises(ServiceError):
        store.read_artifact(sid, ref["sha256"])


def test_active_and_queued_evaluations_must_stop_before_deletion(evaluation):
    store, _, semantic, provider, sid, aid = evaluation
    provider.block_after = 0
    jobs = [semantic.start(sid, aid, {"analysis_version": "v2", "max_calls": count}) for count in (80, 81, 82)]
    assert provider.blocked.wait(5)
    assert semantic.get(sid, aid, jobs[-1]["id"])["state"] == "queued"
    for job in jobs:
        with pytest.raises(ServiceError) as error:
            semantic.delete(sid, aid, job["id"])
        assert error.value.status == 409
    for job in jobs:
        semantic.cancel(sid, aid, job["id"])
    for job in jobs:
        assert wait_job(semantic, sid, aid, job["id"])["state"] == "cancelled"
        semantic.delete(sid, aid, job["id"])
    semantic.pool.shutdown(wait=True)
    with store.transaction() as db:
        assert not db.execute("SELECT 1 FROM semantic_jobs").fetchone()
        assert not db.execute("SELECT 1 FROM evaluation_artifacts").fetchone()


def test_delete_is_scoped_to_session_and_snapshot(evaluation, tmp_path):
    store, trajectory, semantic, _, sid, aid = evaluation
    job = completed(semantic, sid, aid)
    other_sid = store.create("other", {"objective": "Other", "workspace": str(tmp_path)})["session_id"]
    store.update(sid, lambda state, emit, db: emit("test.new_snapshot", {}))
    other_aid = trajectory.start(sid)["id"]
    assert other_aid != aid
    for scope in ((other_sid, aid), (sid, other_aid)):
        with pytest.raises(ServiceError) as error:
            semantic.delete(*scope, job["id"])
        assert error.value.status == 404
    assert semantic.get(sid, aid, job["id"])["state"] == "completed"


@pytest.mark.parametrize("restart", [False, True])
def test_failed_file_cleanup_is_durable_and_retryable(evaluation, monkeypatch, restart):
    store, trajectory, semantic, provider, sid, aid = evaluation
    job = completed(semantic, sid, aid)
    original = store.artifacts.delete

    def fail(_):
        raise PermissionError("test filesystem failure")

    monkeypatch.setattr(store.artifacts, "delete", fail)
    with pytest.raises(ServiceError, match="cleanup pending") as error:
        semantic.delete(sid, aid, job["id"])
    assert error.value.status == 503
    with store.transaction() as db:
        assert not db.execute("SELECT 1 FROM semantic_jobs WHERE id=?", (job["id"],)).fetchone()
        assert db.execute("SELECT 1 FROM evaluation_deletions WHERE id=?", (job["id"],)).fetchone()
    monkeypatch.setattr(store.artifacts, "delete", original)
    if restart:
        semantic.close()
        restored = SessionSemantic(trajectory, provider_factory=lambda _: provider)
        restored.close()
    else:
        semantic.delete(sid, aid, job["id"])
    assert not (store.artifacts.root / f"{job['result']['sha256']}.json").exists()
    with store.transaction() as db:
        assert not db.execute("SELECT 1 FROM evaluation_deletions").fetchone()


def test_resume_tracks_prior_result_even_before_ownership_migration(evaluation):
    store, _, semantic, _, sid, aid = evaluation
    job = completed(semantic, sid, aid)
    # Simulate an old stopped job with a saved result and no ownership index.
    with store.transaction() as db:
        body = json.loads(db.execute("SELECT body FROM semantic_jobs WHERE id=?", (job["id"],)).fetchone()[0])
        prior = store.artifact_in_transaction(db, sid, {"prior_partial_result": True}, "trajectory_evaluation")
        body.update(state="incomplete", result=prior)
        db.execute("UPDATE semantic_jobs SET body=? WHERE id=?", (canonical(body), job["id"]))
        db.execute("DELETE FROM evaluation_artifacts")
    semantic.start(sid, aid, {"resume_id": job["id"]})
    wait_job(semantic, sid, aid, job["id"])
    with store.transaction() as db:
        assert db.execute("SELECT 1 FROM evaluation_artifacts WHERE evaluation_id=? AND digest=?", (job["id"], prior["sha256"])).fetchone()
    semantic.delete(sid, aid, job["id"])
    assert not (store.artifacts.root / f"{prior['sha256']}.json").exists()


def test_pending_cleanup_preserves_a_republished_output(evaluation, monkeypatch, tmp_path):
    store, _, semantic, _, sid, aid = evaluation
    job = completed(semantic, sid, aid)
    original = store.artifacts.delete

    def fail(_):
        raise OSError("temporarily unavailable")

    monkeypatch.setattr(store.artifacts, "delete", fail)
    with pytest.raises(ServiceError):
        semantic.delete(sid, aid, job["id"])
    other_sid = store.create("other", {"objective": "Other", "workspace": str(tmp_path)})["session_id"]
    republished = store.save_artifact(other_sid, job["evaluation"], "trajectory_evaluation")
    assert republished["sha256"] == job["result"]["sha256"]
    monkeypatch.setattr(store.artifacts, "delete", original)
    semantic.delete(sid, aid, job["id"])
    store.read_artifact(other_sid, republished["sha256"])
    with store.transaction() as db:
        assert not db.execute("SELECT 1 FROM evaluation_deletions").fetchone()
