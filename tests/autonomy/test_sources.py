from loom.autonomy.context import build_snapshot
from loom.autonomy.sources import refresh


def test_source_versions_partial_retention_and_context_invalidation(agent_service):
    _, facade, aid, workspace = agent_service
    (workspace / "src").mkdir()
    (workspace / "tests").mkdir()
    source = workspace / "src" / "note.txt"
    source.write_text("one")
    first = refresh(facade.store, aid, force=True)[0]
    assert first["state"] == "current"
    before = build_snapshot(facade.store, aid)
    assert refresh(facade.store, aid, force=True)[0]["revision"] == first["revision"]
    source.write_text("two")
    second = refresh(facade.store, aid, force=True)[0]
    assert second["revision"] == first["revision"] + 1
    source.unlink()
    (workspace / "src").rmdir()
    partial = refresh(facade.store, aid, force=True)[0]
    assert partial["state"] != "current" and partial["last_success"]["digest"] == second["digest"]
    after = build_snapshot(facade.store, aid)
    assert before["id"] != after["id"]
    assert after["source_status"][0]["revalidate_before_use"]


def test_fts_refresh_reuses_document_identity_and_deleted_sources_stop_search(agent_service):
    from loom.knowledge.local_sources import run

    _, facade, aid, workspace = agent_service
    knowledge = facade.store.sessions.knowledge
    kid = knowledge.create({"name": "Bound local evidence"})["id"]
    source = workspace / "local.md"
    source.write_text("original moonbase evidence")
    run(knowledge, kid, dict(files=[str(source)]), lambda **_kw: None, lambda: False, lambda _v: None)
    original = knowledge.get(kid)["documents"][0]["id"]
    agent = facade.store.snapshot(aid)
    role = {k: v for k, v in agent["role"].items() if k != "schema_version"}
    role["knowledge_base_ids"] = [kid]
    role["source_routes"] = [dict(id="local", topic="facts", knowledge_base_ids=[kid])]
    facade.store.revise_role(aid, "bind-local", agent["revision"], role)
    source.write_text("replacement sunbase evidence")
    assert not knowledge.search([kid], "moonbase")["matches"]
    refresh(facade.store, aid, force=True)
    assert knowledge.get(kid)["documents"][0]["id"] == original
    assert knowledge.search([kid], "sunbase")["matches"]
    source.unlink()
    assert not knowledge.search([kid], "sunbase")["matches"]
    refresh(facade.store, aid, force=True)
    assert knowledge.get(kid)["documents"] == []
