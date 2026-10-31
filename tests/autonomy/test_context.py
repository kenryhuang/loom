import json

import pytest

from loom.autonomy.context import build_snapshot
from loom.memory.annotations import annotate
from loom.memory.store import MemoryError, MemoryStore
from loom.service.contracts import ServiceError
from tests.autonomy.helpers import goals
from tests.memory.helpers import seed


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
