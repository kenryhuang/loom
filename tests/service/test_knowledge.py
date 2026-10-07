import json
import time

import pytest

from loom.core import ok
from loom.llm.api import LlmResponse, LlmToolCall
from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from tests.service.test_api import api  # noqa: F401

# Imported fixture is injected by pytest.
from tests.service.test_controller import command, wait_state


class KnowledgeProvider:
    model = "knowledge-test"

    def __init__(self, state):
        self.attached = bool(state["task"].get("knowledge_base_ids"))

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        names = {tool["function"]["name"] for tool in tools or []}
        assert ("knowledge_search" in names) == self.attached
        found = next((message for message in messages if message.name == "knowledge_search"), None)
        if self.attached and not found:
            return ok(LlmResponse("", (LlmToolCall("retrieve", "knowledge_search", '{"query":"Moonbase"}'),)))
        if found:
            assert "July 8" in found.content and "source_id" in found.content
        return ok(LlmResponse(json.dumps({"action": {"kind": "none", "input": {"report": "Retrieved July 8" if found else "No retrieval needed"}}})))


def knowledge_provider(state):
    return KnowledgeProvider(state)


@pytest.mark.parametrize("engine", ["sqlite_fts", "yakdb_local"])
def test_session_binding_real_worker_and_restart(tmp_path, engine):
    if engine == "yakdb_local":
        pytest.importorskip("yakdb")
    directory = tmp_path / "service"
    service = LoomService(directory, provider_factory=knowledge_provider)
    try:
        kb = service.store.knowledge.create({"name": "Reference", "engine": engine})
        service.store.knowledge.index(kb["id"], {"name": "dates.md", "content": "Moonbase launches July 8."})
        sid = service.create("create", {"plan_mode": "off", "task_spec": {"tools": {"collections": ["task_control"]}}})["session_id"]
        command(service, sid, "set_knowledge_bases", knowledge_base_ids=[kb["id"]])
        command(service, sid, "submit_message", content="When does Moonbase launch?")
        with pytest.raises(ServiceError, match="between tasks"):
            command(service, sid, "set_knowledge_bases", knowledge_base_ids=[])
        service.start()
        done = wait_state(service, sid, "idle")
        assert done["messages"][-1]["content"] == "Retrieved July 8"
        events = service.events(sid, limit=1000)
        assert any(e["type"] == "tool.completed" and e["payload"]["tool_id"] == "knowledge_search" for e in events)
    finally:
        service.close()
    service = LoomService(directory, provider_factory=knowledge_provider).start()
    try:
        assert service.snapshot(sid)["task"]["knowledge_base_ids"] == [kb["id"]]
        command(service, sid, "set_knowledge_bases", knowledge_base_ids=[])
        command(service, sid, "submit_message", content="Now answer without a knowledge base")
        done = wait_state(service, sid, "idle")
        assert done["messages"][-1]["content"] == "No retrieval needed"
    finally:
        service.close()


def test_knowledge_http_jobs_search_and_session_create(api):  # noqa: F811
    service, server, client, path = api
    prefix = "/v1/knowledge-bases"
    base = client._json(prefix, {"name": "API docs"})
    job = client._json(f"{prefix}/{base['id']}/documents", {"name": "guide.md", "content": "AlphaBeta protocol"})
    for _ in range(100):
        job = client._json(f"{prefix}/{base['id']}/jobs/{job['id']}")
        if job["state"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert job["state"] == "completed"
    hit = client._json(f"{prefix}/{base['id']}/search", {"query": "AlphaBeta"})["matches"][0]
    assert hit["document"] == "guide.md"
    sid = client.create({"knowledge_base_ids": [base["id"]]})["session_id"]
    state = client.snapshot(sid)
    assert "knowledge" in state["task"]["task_spec"]["tools"]["collections"]
    with pytest.raises(ServiceError):
        client.create({"knowledge_base_ids": ["missing"]})
    with pytest.raises(ServiceError):
        client._json(prefix, {"name": "Unknown", "engine": "unavailable"})
    with pytest.raises(ServiceError):
        client._json(prefix, {"name": "Hybrid", "engine": "sqlite_hybrid"})
    client._json(f"{prefix}/{base['id']}/documents/{hit['document_id']}/delete", {})
    assert not client._json(f"{prefix}/{base['id']}/search", {"query": "AlphaBeta"})["matches"]


def test_existing_create_receipts_without_knowledge_field_still_replay(tmp_path):
    from loom.service.contracts import canonical

    service = LoomService(tmp_path / "data")
    try:
        first = service.create("legacy", {})
        with service.store.transaction() as db:
            previous = json.loads(db.execute("SELECT input FROM commands WHERE id='legacy'").fetchone()[0])
            previous["payload"].pop("knowledge_base_ids")
            db.execute("UPDATE commands SET input=? WHERE id='legacy'", (canonical(previous),))
        assert service.create("legacy", {}) == first
    finally:
        service.close()


def test_yakdb_pdf_upload_job_and_page_search(api):  # noqa: F811
    pytest.importorskip("yakdb")
    from tests.knowledge.test_yakdb import pdf_payload

    _, _, client, _ = api
    prefix = "/v1/knowledge-bases"
    base = client._json(prefix, {"name": "PDF", "engine": "yakdb_local"})
    payload = pdf_payload()
    # A real binary request exceeding the default 1 MiB limit is accepted only on this upload route.
    import base64

    raw = base64.b64decode(payload["file_base64"]) + b"\n%" + b" " * 1_100_000
    payload["file_base64"] = base64.b64encode(raw).decode()
    job = client._json(f"{prefix}/{base['id']}/documents", payload)
    for _ in range(300):
        job = client._json(f"{prefix}/{base['id']}/jobs/{job['id']}")
        if job["state"] in {"completed", "failed"}:
            break
        time.sleep(0.05)
    assert job["state"] == "completed", job
    hit = client._json(f"{prefix}/{base['id']}/search", {"query": "Moonbase"})["matches"][0]
    assert hit["page_number"] == 2 and "July eight" in hit["text"]


def test_lightrag_source_and_graph_routes(api, monkeypatch):  # noqa: F811
    from loom.knowledge import lightrag_local
    service, server, client, path = api
    monkeypatch.setattr(lightrag_local, "require", lambda: None)
    monkeypatch.setattr(lightrag_local, "binding", lambda *a: "test")
    prefix = "/v1/knowledge-bases"
    profile = client._json(prefix + "/embedding-profiles", {"name": "Local", "endpoint": "http://localhost:8000/embeddings", "model": "test"})
    base = client._json(prefix, {"name": "Graph", "engine": "lightrag_local", "indexing_model": "main", "embedding_profile_id": profile["id"]})
    route = f"{prefix}/{base['id']}"
    source = client._json(route + "/sources", {"url": "https://example.com/", "max_pages": 10})
    assert client._json(route + "/sources")["sources"][0]["id"] == source["id"]
    updated = client._json(route + "/sources/" + source["id"], {"enabled": False, "interval_hours": 24})
    assert not updated["enabled"] and updated["interval_hours"] == 24
    assert client._json(route + "/graph", {}) == {"nodes": [], "edges": [], "is_truncated": False}
    with pytest.raises(ServiceError):
        client._json(route + "/sources/" + source["id"], {"url": "https://other.example/"})
    with pytest.raises(ServiceError):
        client._json(route + "/graph", {"limit": 10000})
