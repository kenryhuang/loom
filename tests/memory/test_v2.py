import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from loom.autonomy.store import AgentStore
from loom.memory.migration import migrate_database, sync_job
from loom.memory.operations import MemoryOperations
from loom.memory.query import MemoryQueryService, available
from loom.memory.repository import Repository
from loom.memory.store import MemoryError, MemoryStore
from loom.service.store import SessionStore
from tests.memory.helpers import fact, seed


@pytest.fixture
def catalog(tmp_path):
    store = SessionStore(tmp_path / "data")
    AgentStore(store)
    workspace = tmp_path / "project"
    sid = seed(store, workspace)
    report = migrate_database(store.path)
    reader = MemoryQueryService(store.path, workspace=workspace)
    return store, workspace, sid, reader, report


def test_migration_idempotent_exact_duplicates_and_originals(catalog):
    store, workspace, _, reader, report = catalog
    seed(store, workspace, evaluation="another")
    again = migrate_database(store.path)
    assert again["knowledge"] == report["knowledge"] == 1
    assert again["observations"] == 2
    assert migrate_database(store.path)["observations"] == 2
    row = reader.browse()["records"][0]
    assert row["support_count"] == 1  # Re-evaluation of the same trace is one root.
    assert "citations" not in row
    assert reader.detail(row["memory_id"])["versions"][0]["citations"]
    original = reader.original("eval-memory", "memory:planet")
    assert original["historical"] and json.loads(original["text"])["citations"]
    with reader.repo.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_item_versions").fetchone()[0] == 1


def test_reads_do_not_scan_evaluation_json(catalog, monkeypatch):
    store, workspace, _, reader, _ = catalog

    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy record scanning is forbidden after cutover")

    monkeypatch.setattr(MemoryStore, "_records", forbidden)
    memory = MemoryStore(store.path, workspace=workspace)
    assert memory.browse()["total"] == 1
    assert memory.search("planets")["records"]
    assert memory.read("eval-memory", "memory:planet")["historical"]
    assert reader.browse(query="行星")["total"] == 1
    assert not MemoryQueryService(store.path, workspace=workspace / "other").enabled()


def test_progress_saves_do_not_change_epoch(catalog):
    store, _, _, reader, _ = catalog
    before = reader.epoch()
    with store.transaction() as db:
        job = json.loads(db.execute("SELECT body FROM semantic_jobs WHERE id='eval-memory'").fetchone()[0])
        job["tokens"] = 99999
        sync_job(db, job)
    assert reader.epoch() == before


def test_rejection_migrates_before_activation(tmp_path):
    from loom.autonomy.store import digest

    store = SessionStore(tmp_path / "data")
    AgentStore(store)
    seed(store, tmp_path)
    memory = MemoryStore(store.path, workspace=tmp_path)
    original = memory._records()[0][0]
    record, source = original["record"], original["source"]
    lineage = digest([source["source_digest"], record["kind"], record["title"], record["summary"], record["citations"]])
    annotation = dict(id="reject", source=source, actor="human", kind="reject", lineage=lineage, revision=1, created_at="2026-01-01", agent_id="a")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO memory_annotations VALUES(?,?,?,?,?,?,?)",
            ("reject", "a", "eval-memory", record["id"], source["content_digest"], 1, json.dumps(annotation)),
        )
        db.execute("INSERT INTO memory_suppressions VALUES(?,?,?)", ("a", lineage, json.dumps(annotation)))
    migrate_database(store.path)
    assert not memory.browse()["records"]
    seed(store, tmp_path, evaluation="repeated")
    migrate_database(store.path)
    assert not memory.browse()["records"]


def test_source_deletion_does_not_resurrect_or_lose_independent_support(catalog):
    store, workspace, sid, reader, _ = catalog
    seed(store, workspace, evaluation="second")
    migrate_database(store.path)
    store.update(sid, lambda state, emit, db: state.update(deleted_at="now"))
    assert reader.browse()["total"] == 1
    with store.transaction() as db:
        Repository(store.path).invalidate(db, evaluation_id="second")
    assert reader.browse()["total"] == 0
    assert not reader.search("planets")["records"]
    assert reader.original("eval-memory", "memory:planet")["source_available"] is False


def test_revision_conflicts_retirement_and_idempotency(catalog):
    store, _, _, reader, _ = catalog
    item = reader.browse()["records"][0]
    ops = MemoryOperations(store.path)
    payload = dict(memory_id=item["memory_id"], version_id=item["version_id"], expected_revision=item["revision"])
    result = ops.execute(reader.ns, "RETIRE", payload, command_id="reject")
    assert ops.execute(reader.ns, "RETIRE", payload, command_id="reject") == result
    assert reader.browse()["total"] == 0
    with pytest.raises(MemoryError, match="changed"):
        ops.execute(reader.ns, "CONFIRM", payload, command_id="stale")
    assert reader.browse(lifecycle="retired")["total"] == 1


def test_temporal_change_future_and_old_arrival(catalog):
    store, _, _, reader, _ = catalog
    item = reader.browse()["records"][0]
    original = reader.detail(item["memory_id"])["versions"][0]
    record = {k: original[k] for k in ("kind", "title", "summary", "scope")}
    record["summary"] = "Port 9090"
    ops = MemoryOperations(store.path)
    result = ops.execute(
        reader.ns,
        "SUPERSEDE",
        dict(memory_id=item["memory_id"], expected_revision=item["revision"], record=record, effective_at="2099-01-01T00:00:00Z"),
        command_id="future",
    )
    with reader.repo.connect() as db:
        assert available(db, item["version_id"], at="2098-01-01T00:00:00Z")
        assert not available(db, result["version_id"], at="2098-01-01T00:00:00Z")
        assert not available(db, item["version_id"], at="2100-01-01T00:00:00Z")
        assert available(db, result["version_id"], at="2100-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="Late observations"):
        ops.execute(
            reader.ns,
            "SUPERSEDE",
            dict(memory_id=item["memory_id"], expected_revision=result["revision"], record=record, effective_at="2090-01-01T00:00:00Z"),
            command_id="late",
        )


def test_typed_conflicts_and_conditions(catalog):
    store, _, _, reader, _ = catalog
    repo = Repository(store.path)
    base = reader.detail(reader.browse()["records"][0]["memory_id"])["versions"][0]
    source = base["source"]
    with repo.connect(write=True) as db:
        for index, (port, env) in enumerate([(8080, "dev"), (9090, "dev"), (8080, "prod")]):
            record = fact(f"port:{index}")
            record.update(
                title="Port",
                summary=f"Port {port}",
                claim=dict(subject="api", predicate="port", object=dict(type="integer", value=port), conditions=dict(environment=env)),
            )
            repo.ingest(db, reader.ns, record, {**source, "record_id": record["id"], "content_digest": str(index)})
    assert reader.browse(lifecycle="disputed")["total"] == 2
    dev = MemoryQueryService(store.path, workspace=store.directory.parent / "project", environment={"environment": "dev"})
    assert dev.search("Port")["conflicts"]
    assert not dev.search("Port")["records"]
    assert not reader.search("Port")["records"]  # Unknown environment is not applicable.


def test_parallel_same_source_is_idempotent(catalog):
    store, workspace, _, reader, _ = catalog
    seed(store, workspace, evaluation="parallel")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: migrate_database(store.path), range(2)))
    assert reader.browse()["total"] == 1
    with reader.repo.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_observations").fetchone()[0] == 2


def test_merge_undo_and_dependencies(catalog):
    store, workspace, _, reader, _ = catalog
    second = fact("second")
    second["summary"] = "A paraphrase about duplicate planets"
    seed(store, workspace, evaluation="second", records=[second])
    migrate_database(store.path)
    first, second = reader.browse()["records"]
    ops = MemoryOperations(store.path)
    ops.execute(
        reader.ns,
        "MERGE",
        dict(memory_id=first["memory_id"], expected_revision=first["revision"], target_memory_id=second["memory_id"], target_revision=second["revision"]),
        command_id="merge",
    )
    assert reader.browse()["total"] == 1
    ops.undo_merge(reader.ns, "merge", command_id="undo")
    assert reader.browse()["total"] == 2
    first, second = reader.browse()["records"]
    ops.execute(
        reader.ns, "DEPEND", dict(memory_id=first["memory_id"], expected_revision=first["revision"], depends_on=second["version_id"]), command_id="depend"
    )
    with pytest.raises(ValueError, match="cycle"):
        ops.execute(
            reader.ns, "DEPEND", dict(memory_id=second["memory_id"], expected_revision=second["revision"], depends_on=first["version_id"]), command_id="cycle"
        )
    ops.execute(reader.ns, "RETIRE", dict(memory_id=second["memory_id"], expected_revision=second["revision"]), command_id="retire")
    with reader.repo.connect() as db:
        assert not available(db, first["version_id"])


def test_cursor_is_scoped_and_epoch_guarded(catalog):
    store, workspace, _, reader, _ = catalog
    second = fact("second")
    second["summary"] = "A different fact"
    seed(store, workspace, evaluation="second", records=[second])
    migrate_database(store.path)
    page = reader.browse(limit=1)
    assert reader.browse(limit=1, cursor=page["next_cursor"])["records"][0]["memory_id"] != page["records"][0]["memory_id"]
    with pytest.raises(MemoryError, match="Reload"):
        reader.browse(query="different", cursor=page["next_cursor"])
    item = page["records"][0]
    MemoryOperations(store.path).execute(reader.ns, "CONFIRM", dict(memory_id=item["memory_id"], expected_revision=item["revision"]), command_id="review")
    with pytest.raises(MemoryError, match="Reload"):
        reader.browse(cursor=page["next_cursor"])


def test_correction_keeps_legacy_original_and_new_canonical_read(catalog):
    store, workspace, _, reader, _ = catalog
    item = reader.browse()["records"][0]
    result = MemoryOperations(store.path).execute(
        reader.ns,
        "REVISE",
        dict(
            memory_id=item["memory_id"],
            expected_revision=item["revision"],
            record=dict(kind="facts", title=item["title"], summary="Corrected memory content", scope=item["scope"]),
        ),
        command_id="correct",
    )
    assert json.loads(reader.original("eval-memory", "memory:planet")["text"])["summary"] != "Corrected memory content"
    current = MemoryStore(store.path, workspace=workspace).read(memory_id=item["memory_id"], version_id=result["version_id"])
    assert json.loads(current["text"])["summary"] == "Corrected memory content"
    assert reader.browse()["total"] == 1


def test_denial_survives_legacy_rollback(catalog):
    from loom.memory.migration import set_mode

    store, workspace, _, reader, _ = catalog
    item = reader.browse()["records"][0]
    MemoryOperations(store.path).execute(reader.ns, "RETIRE", dict(memory_id=item["memory_id"], expected_revision=item["revision"]), command_id="reject")
    set_mode(store.path, reader.ns, "legacy")
    assert MemoryStore(store.path, workspace=workspace).browse()["total"] == 0


def test_stale_external_hits_never_inject_index_text(catalog):
    from loom.memory.indexes import MemoryIndexProjection

    store, _, _, reader, _ = catalog
    item = reader.browse()["records"][0]

    class Adapter:
        def publish(self, upserts, deletes, generation):
            self.upserts = upserts

        def search(self, query, limit):
            return [dict(document_id=item["version_id"], text="Untrusted obsolete graph summary", graph_entities=["obsolete"])]

    adapter = Adapter()
    index = MemoryIndexProjection(store.path, reader.ns, "fake", "embedding-v1", adapter)
    assert index.sync()["updated"] == 1
    assert "Untrusted" not in json.dumps(index.search("planets"))
    assert index.search("planets")[0]["record"]["summary"] == item["summary"]
    MemoryOperations(store.path).execute(reader.ns, "RETIRE", dict(memory_id=item["memory_id"], expected_revision=item["revision"]), command_id="reject")
    assert index.search("planets") == []  # No index update is needed to withdraw the source.
    assert index.sync()["deleted"] == 1


def test_missing_necessary_support_blocks_but_independent_bundle_survives(catalog):
    store, workspace, _, reader, _ = catalog
    seed(store, workspace, evaluation="other")
    migrate_database(store.path)
    item = reader.browse()["records"][0]
    with reader.repo.connect(write=True) as db:
        observations = [r[0] for r in db.execute("SELECT id FROM memory_observations")]
        db.execute("DELETE FROM memory_supports WHERE version_id=?", (item["version_id"],))
        for obs in observations:
            db.execute("INSERT INTO memory_supports VALUES(?,?,?,?)", (item["version_id"], obs, "required-together", "supports"))
        Repository(store.path).refresh_support(db, [item["version_id"]])
        Repository(store.path).invalidate(db, evaluation_id="other")
        assert not available(db, item["version_id"])
        valid = db.execute("SELECT o.id FROM memory_observations o JOIN memory_sources s ON s.id=o.source_id WHERE s.available=1").fetchone()[0]
        db.execute("INSERT INTO memory_supports VALUES(?,?,?,?)", (item["version_id"], valid, "independent-sufficient", "supports"))
        Repository(store.path).refresh_support(db, [item["version_id"]])
        assert available(db, item["version_id"])


def test_runtime_consumes_exact_agent_packet_without_second_search(catalog, monkeypatch):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    from loom.memory.runtime import RuntimeMemory, compact_memory
    from loom.tasks.request import TaskRequest
    from loom.tasks.runner import make_task_context
    from tests.tasks.test_task_runner import RecordingTraceSink

    store, workspace, _, reader, _ = catalog
    packet = compact_memory(reader.search("planets"))
    context = make_task_context(TaskRequest("planets", workspace=workspace), plan_mode="off").unwrap()
    context = replace(context, metadata={"agent_context": {"id": "saved", "memory": packet}})
    runtime = RuntimeMemory(store.path, workspace=workspace)
    monkeypatch.setattr(runtime.store, "search", lambda *_: pytest.fail("Agent memory must not be searched twice"))
    trace = SimpleNamespace(trace_sink=RecordingTraceSink(), trace_id="test")
    attached = asyncio.run(runtime.attach(context, trace))
    from loom.core import thaw_json

    assert thaw_json(attached.metadata["retrieved_memory"]) == packet
    assert trace.trace_sink.events[0]["packet_digest"] == packet["packet_digest"]


def test_split_preserves_sources_and_source_update_retracts_old(catalog):
    store, _, _, reader, _ = catalog
    item = reader.browse()["records"][0]
    result = MemoryOperations(store.path).execute(
        reader.ns,
        "SPLIT",
        dict(
            memory_id=item["memory_id"],
            expected_revision=item["revision"],
            records=[dict(title="First", summary="First atomic claim"), dict(title="Second", summary="Second atomic claim")],
        ),
        command_id="split",
    )
    assert len(result["children"]) == reader.browse()["total"] == 2
    with store.transaction() as db:
        job = json.loads(db.execute("SELECT body FROM semantic_jobs WHERE id='eval-memory'").fetchone()[0])
        job["checkpoint"]["memory"]["records"] = {}
        sync_job(db, job)
    assert reader.browse()["total"] == 0


def test_shadow_proposals_are_persisted_once_and_require_budget(catalog):
    from loom.memory.consolidation import Consolidator

    store, workspace, _, reader, _ = catalog
    second = fact("second")
    second["summary"] = "duplicate planets need checking"
    seed(store, workspace, evaluation="second", records=[second])
    migrate_database(store.path)
    with reader.repo.connect() as db:
        jobs = list(db.execute("SELECT * FROM memory_jobs"))
        assert jobs and all(r["state"] == "shadow_pending" for r in jobs)
    assert Consolidator(store.path).prepare(jobs[0]["id"]) is None


def test_optional_atomic_claim_preserves_legacy_protocol_and_checks_types():
    from jsonschema import validate

    from loom.evaluation.memory.contracts import FIELDS
    from loom.evaluation.memory.wire import fields
    from loom.memory.repository import claim_slot

    record = fact()
    record = {k: v for k, v in record.items() if k in FIELDS["facts"]["items"]["properties"]}
    validate(record, FIELDS["facts"]["items"])
    assert "claim" not in fields()["facts"]["items"]["required"]
    record["claim"] = dict(subject="api", predicate="port", object=dict(type="integer", value=8080), conditions={"environment": "dev"}, cardinality="single")
    validate(record, FIELDS["facts"]["items"])
    claim_slot({**record, "kind": "facts"})
    record["claim"]["object"]["value"] = True
    with pytest.raises(ValueError, match="declared type"):
        claim_slot({**record, "kind": "facts"})


def test_calibration_cannot_self_grade_or_leak_evidence():
    from loom.memory.calibration import evaluate

    with pytest.raises(ValueError, match="human-labelled"):
        evaluate([dict(id="a", label_origin="model")])
    rows = [
        dict(id="a", label_origin="human", source_roots=["same-root"], split="calibration"),
        dict(id="b", label_origin="human", source_roots=["same-root"], split="test"),
    ]
    with pytest.raises(ValueError, match="leaks"):
        evaluate(rows)


def test_same_source_revision_updates_stable_identity_without_rewriting_history(catalog):
    store, _, _, reader, _ = catalog
    original = reader.browse()["records"][0]
    with store.transaction() as db:
        job = json.loads(db.execute("SELECT body FROM semantic_jobs WHERE id='eval-memory'").fetchone()[0])
        job["checkpoint"]["memory"]["records"]["memory:planet"]["summary"] = "The revised source clarifies duplicate planets"
        db.execute("UPDATE semantic_jobs SET body=? WHERE id='eval-memory'", (json.dumps(job),))
        sync_job(db, job)
    current = reader.browse()["records"][0]
    assert current["memory_id"] == original["memory_id"]
    assert current["version_id"] != original["version_id"]
    versions = reader.detail(current["memory_id"])["versions"]
    assert len(versions) == 2 and versions[1]["summary"] == original["summary"]


def test_known_paraphrase_does_not_reappear_after_merge(catalog):
    store, workspace, _, reader, _ = catalog
    alternate = fact("alternative")
    alternate["summary"] = "Check the array for duplicate planet identifiers"
    seed(store, workspace, evaluation="alternative", records=[alternate])
    migrate_database(store.path)
    rows = reader.browse()["records"]
    source = next(r for r in rows if r["summary"] == alternate["summary"])
    target = next(r for r in rows if r["memory_id"] != source["memory_id"])
    MemoryOperations(store.path).execute(
        reader.ns,
        "MERGE",
        dict(memory_id=source["memory_id"], expected_revision=source["revision"], target_memory_id=target["memory_id"], target_revision=target["revision"]),
        command_id="merge-alias",
    )
    seed(store, workspace, evaluation="repeated-alias", records=[alternate])
    migrate_database(store.path)
    assert reader.browse()["total"] == 1
