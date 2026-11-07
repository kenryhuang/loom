import json

import pytest

from loom.autonomy.context import build_snapshot
from loom.memory.annotations import annotate
from loom.memory.store import MemoryError, MemoryStore
from loom.service.contracts import ServiceError
from tests.autonomy.helpers import goals
from tests.memory.helpers import seed


def test_snapshot_lists_recent_completed_tool_calls_for_record_use_linking(agent_service):
    _, facade, aid, _ = agent_service
    with facade.store.sessions.transaction() as db:
        ops = [
            ("run_1:call_0", "sess_1", dict(call=dict(id="call_0", name="shell_execute", arguments="{}"), status="completed")),
            ("run_1:call_1", "sess_1", dict(call=dict(id="call_1", name="edit_file", arguments="{}"), status="started")),
            ("run_1:call_2", "sess_1", dict(call=dict(id="call_2", name="write_file", arguments="{}"), status="reconciled")),
            ("run_2:call_3", "sess_1", dict(call=dict(id="call_3", name="read_file", arguments="{}"), status="completed")),
            ("run_1:call_4", "sess_2", dict(call=dict(id="call_4", name="read_file", arguments="{}"), status="completed")),
        ]
        for oid, sid, op in ops:
            db.execute("INSERT INTO operations VALUES(?,?,?)", (oid, sid, json.dumps(op)))
    snapshot = build_snapshot(facade.store, aid, session_id="sess_1", run_id="run_1")
    assert snapshot["recent_tool_calls"] == [dict(id="call_0", name="shell_execute"), dict(id="call_2", name="write_file")]
    other_run = build_snapshot(facade.store, aid, session_id="sess_1", run_id="run_2")
    assert other_run["recent_tool_calls"] == [dict(id="call_3", name="read_file")]


def test_snapshot_bounds_recent_tool_calls_to_twenty(agent_service):
    _, facade, aid, _ = agent_service
    with facade.store.sessions.transaction() as db:
        for i in range(25):
            op = dict(call=dict(id=f"call_{i:02d}", name="shell_execute", arguments="{}"), status="completed")
            db.execute("INSERT INTO operations VALUES(?,?,?)", (f"run_1:call_{i:02d}", "sess_1", json.dumps(op)))
    snapshot = build_snapshot(facade.store, aid, session_id="sess_1", run_id="run_1")
    assert [c["id"] for c in snapshot["recent_tool_calls"]] == [f"call_{i:02d}" for i in range(5, 25)]


def test_record_use_rejects_empty_links_before_service_round_trip():
    import asyncio

    from loom.autonomy.context import RuntimeAgentContext

    class FakeExecution:
        def __init__(self):
            self.calls = []

        def rpc(self, kind, payload):
            self.calls.append((kind, payload))
            return {"id": "use_1"}

    execution = FakeExecution()
    record = RuntimeAgentContext(execution).handlers()["agent_record_use"]
    result = asyncio.run(record({"disposition": "adopted", "tool_call_ids": [], "explanation": "adopted a memory"}))
    assert not result.ok and result.error.code == "agent.use_invalid"
    assert "recent_tool_calls" in result.error.message
    assert not execution.calls  # The rejection never crosses the RPC boundary.
    declined = asyncio.run(record({"disposition": "declined", "tool_call_ids": [], "explanation": "not used"}))
    assert declined.ok and execution.calls == [("agent_record_use", {"disposition": "declined", "tool_call_ids": [], "explanation": "not used"})]


def test_record_use_service_rejection_returns_tool_error_without_raising():
    import asyncio

    from loom.autonomy.context import RuntimeAgentContext

    class RejectingExecution:
        def rpc(self, kind, payload):
            raise RuntimeError("Link at least one completed tool call")

    record = RuntimeAgentContext(RejectingExecution()).handlers()["agent_record_use"]
    result = asyncio.run(record({"disposition": "adopted", "tool_call_ids": ["call_1"], "explanation": "used"}))
    assert not result.ok and result.error.code == "agent.use_invalid"
    assert "Link at least one completed tool call" in result.error.message


def test_recent_tool_calls_ride_above_the_explicit_obligation_size_cap(agent_service):
    _, facade, aid, _ = agent_service
    base = len(json.dumps(build_snapshot(facade.store, aid), ensure_ascii=False))
    index = 0
    while base < 23800:  # Fill explicit obligations to just below the 24K cap.
        facade.guidance(aid, dict(command_id=f"big{index}", text="x" * min(3900, 23800 - base), scope="persistent"))
        base = len(json.dumps(build_snapshot(facade.store, aid), ensure_ascii=False))
        index += 1
    assert base < 24000
    with facade.store.sessions.transaction() as db:
        for i in range(20):
            op = dict(call=dict(id=f"call_{i:02d}", name="shell_execute", arguments="{}"), status="completed")
            db.execute("INSERT INTO operations VALUES(?,?,?)", (f"run_1:call_{i:02d}", "sess_1", json.dumps(op)))
    snapshot = build_snapshot(facade.store, aid, session_id="sess_1", run_id="run_1")
    assert len(snapshot["recent_tool_calls"]) == 20
    assert sum(len(g["text"]) for g in snapshot["guidance"]) >= 15000  # Explicit obligations are never truncated.


def test_review_confidence_reaches_context_without_rewriting_evaluation(agent_service):
    from tests.memory.helpers import fact

    service, facade, aid, path = agent_service
    record = {**fact(), 'confidence': 'low', 'basis': 'inferred'}
    competing = {**fact('other'), 'title': 'duplicate planets rendering duplicate planets', 'summary': 'Check duplicate planets'}
    seed(service.store, path, records=[competing, record])
    memory = MemoryStore(service.store.path, workspace=path)
    original = next(row for row in memory.browse()['records'] if row['id'] == record['id'])
    ref = original['source']
    assert original['effective_confidence'] == 'low'
    assert original['review_state'] == 'unreviewed'
    payload = dict(evaluation_id=ref['evaluation_id'], record_id=ref['record_id'], content_digest=ref['content_digest'])
    annotate(facade.store, aid, 'tag-only', dict(**payload, expected_revision=0, kind='tag', value='needs-review'))
    assert next(row for row in memory.browse()['records'] if row['id'] == record['id'])['review_state'] == 'unreviewed'
    annotate(facade.store, aid, 'human-confirm', dict(**payload, expected_revision=1, kind='confirm'))
    found = memory.search('duplicate planets')['records'][0]
    assert found['source']['record_id'] == record['id']
    assert found['confidence'] == 'low' and found['effective_confidence'] == 'high'
    assert found['basis'] == 'inferred' and found['confidence_basis'] == 'human_review'
    assert found['human_review']['agent_id'] == aid
    snapshot = build_snapshot(facade.store, aid, goal={'objective': 'duplicate planets'}, purpose='planning')
    assert snapshot['memory']['records'][0]['review_state'] == 'confirmed'
    with service.store.transaction() as db:
        context = facade.store.get(db, 'agent_contexts', snapshot['id'], aid)
    assert context['memory_refs'][0] == {k: ref[k] for k in ('record_id', 'evaluation_id', 'content_digest')}
    assert 'request_input' in snapshot['decision_policy']
    annotate(facade.store, aid, 'human-correct', dict(**payload, expected_revision=2, kind='correct', value='Check duplicate planets in this project only'))
    corrected = memory.browse(review='reviewed')['records'][0]
    assert corrected['review_state'] == 'corrected'
    assert corrected['citations'] == record['citations']
    assert corrected['confidence'] == 'low'
    with service.store.transaction() as db:
        raw = json.loads(db.execute("SELECT body FROM semantic_jobs WHERE id='eval-memory'").fetchone()[0])
        assert raw['checkpoint']['memory']['records'][record['id']] == record


def test_annotations_preserve_source_and_tools_share_view(agent_service):
    service, facade, aid, path = agent_service
    seed(service.store, path)
    memory = MemoryStore(service.store.path, workspace=path)
    original = memory.search("duplicate planets")["records"][0]

    def mark(cid, kind, revision, **values):
        return annotate(
            facade.store,
            aid,
            cid,
            dict(
                evaluation_id=original["source"]["evaluation_id"],
                record_id=original["source"]["record_id"],
                content_digest=original["source"]["content_digest"],
                expected_revision=revision,
                kind=kind,
                **values,
            ),
        )

    epoch = memory.epoch()
    mark("correct", "correct", 0, value="Inspect duplicate planets in the current version; this is a human correction.")
    assert memory.epoch() > epoch
    found = memory.search("duplicate planets")["records"][0]
    assert found["basis"] == "human_asserted"
    assert "human correction" in memory.read("eval-memory", "memory:planet")["text"]
    with service.store.transaction() as db:
        raw = json.loads(db.execute("SELECT body FROM semantic_jobs WHERE id='eval-memory'").fetchone()[0])
        assert raw["checkpoint"]["memory"]["records"]["memory:planet"]["basis"] == "observed"
    with pytest.raises(ServiceError, match="revision"):
        mark("stale", "reject", 0)
    mark("reject", "reject", 1)
    assert memory.search("duplicate planets")["records"] == []
    with pytest.raises(MemoryError):
        memory.read("eval-memory", "memory:planet")
    mark("restore", "restore", 2)
    assert memory.search("duplicate planets")["records"]


def test_context_revisions_and_user_method_are_not_validated_skills(agent_service):
    service, facade, aid, _ = agent_service
    goal = facade.store.add_goal(aid, "goal", goals()[0])
    first = build_snapshot(facade.store, aid, goal=goal, purpose="planning")
    guidance = facade.guidance(aid, dict(command_id="guide", text="以后用中文且区分提案", scope="persistent"))
    method = facade.guidance(aid, dict(command_id="method", title="Evidence first", method_text="Inspect before changing", scope="persistent"), method=True)
    second = build_snapshot(facade.store, aid, goal=goal, purpose="planning")
    assert first["id"] != second["id"]
    assert second["guidance"][0]["text"] == guidance["text"]
    with service.store.transaction() as db:
        state = db.execute("SELECT state FROM skill_versions WHERE id=?", (method["method_version_id"],)).fetchone()[0]
        assert state == "user_guidance"
        assert not db.execute("SELECT 1 FROM skill_validations WHERE version_id=?", (method["method_version_id"],)).fetchone()
    facade.profile_command(aid, dict(command_id="retire", type="retire", entry_id=guidance["id"], expected_revision=1))
    third = build_snapshot(facade.store, aid, goal=goal, purpose="planning")
    assert guidance["id"] not in {g["id"] for g in third["guidance"]}


def test_annotation_cannot_reach_other_workspace(agent_service, tmp_path):
    service, facade, aid, _ = agent_service
    other = tmp_path / "unrelated"
    seed(service.store, other)
    ref = MemoryStore(service.store.path, workspace=other).search("planets")["records"][0]["source"]
    with pytest.raises(ServiceError):
        annotate(
            facade.store,
            aid,
            "reject-other",
            dict(evaluation_id=ref["evaluation_id"], record_id=ref["record_id"], content_digest=ref["content_digest"], expected_revision=0, kind="reject"),
        )
