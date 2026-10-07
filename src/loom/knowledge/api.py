"""Authenticated knowledge routes (authentication is owned by SessionHandler)."""

import json

from loom.knowledge.store import ENGINES
from loom.service.contracts import ServiceError, object_value


def dispatch(handler, method, parts):
    store = handler.server.service.store.knowledge
    jobs = handler.server.knowledge_jobs
    tail = parts[2:]
    if not tail and method == "GET":
        from loom.tasks.config import load_task_config

        loaded = load_task_config(store.config_path) if store.config_path else None
        models = [{"id": k, "label": f"{k} · {v.model}"} for k, v in loaded.value.models.items()] if loaded and loaded.ok else []
        return 200, {"knowledge_bases": store.list(), "embedding_profiles": store.profiles(), "engines": list(ENGINES), "models": models}
    if not tail and method == "POST":
        return 201, store.create(handler._body())
    if tail == ["embedding-profiles"] and method == "POST":
        return 201, store.create_profile(handler._body())
    if not tail:
        raise ServiceError("Route not found", 404)
    kb_id = tail[0]
    if len(tail) == 1 and method == "GET":
        base = store.get(kb_id)
        with store.connect() as db:
            base["jobs"] = [json.loads(row[0]) for row in db.execute("SELECT body FROM jobs WHERE kb_id=? ORDER BY rowid DESC LIMIT 20", (kb_id,))]
        return 200, base
    if tail[1:] == ["sources"]:
        from loom.knowledge.web_sources import save_source, sources

        if method == "GET":
            return 200, {"sources": sources(store, kb_id)}
        if method == "POST":
            return 201, save_source(store, kb_id, handler._body())
    if len(tail) == 3 and tail[1] == "sources" and method == "POST":
        from loom.knowledge.web_sources import save_source

        return 200, save_source(store, kb_id, handler._body(), tail[2])
    if len(tail) == 4 and tail[1] == "sources" and tail[3] == "sync" and method == "POST":
        if handler._body() != {}:
            raise ServiceError("Sync requires an empty object")
        return 202, jobs.sync(kb_id, tail[2])
    if tail[1:] == ["graph"] and method == "POST":
        from loom.knowledge.lightrag_local import graph

        body = object_value(handler._body())
        if set(body) - {"label", "limit"}:
            raise ServiceError("Graph accepts label and limit")
        return 200, graph(store, kb_id, body.get("label", ""), body.get("limit", 150))
    if len(tail) == 4 and tail[1] == "jobs" and tail[3] == "cancel" and method == "POST":
        if handler._body() != {}:
            raise ServiceError("Cancel requires an empty object")
        return 202, jobs.cancel(kb_id, tail[2])
    if tail[1:] == ["documents"] and method == "POST":
        return 202, jobs.start(kb_id, handler._body(max_bytes=28 * 1_048_576) if store.get(kb_id)["engine"] == "yakdb_local" else handler._body())
    if tail[1:] == ["search"] and method == "POST":
        body = object_value(handler._body())
        if set(body) - {"query", "limit"}:
            raise ServiceError("Search accepts query and limit")
        return 200, store.search([kb_id], body.get("query"), body.get("limit", 5))
    if len(tail) == 3 and tail[1] == "jobs" and method == "GET":
        return 200, jobs.get(kb_id, tail[2])
    if len(tail) == 4 and tail[1] == "documents" and tail[3] == "delete" and method == "POST":
        if handler._body() != {}:
            raise ServiceError("Delete requires an empty object")
        if store.get(kb_id)["engine"] == "lightrag_local":
            return 202, jobs.remove(kb_id, tail[2])
        # Serialise deletion against queued/running publication for this KB.
        with jobs.lock, store.connect() as db:
            if db.execute("SELECT 1 FROM jobs WHERE kb_id=? AND json_extract(body,'$.state') IN ('queued','running')", (kb_id,)).fetchone():
                raise ServiceError("Wait for indexing to finish before removing documents", 409)
            return 200, store.remove_document(kb_id, tail[2])
    raise ServiceError("Route not found", 404)
