"""Persistent website sources and crawl-to-index synchronization."""

import hashlib
import json
from urllib.parse import urlsplit

from loom.core import now_iso
from loom.knowledge.crawler import crawl, normalize
from loom.service.contracts import ServiceError, canonical, new_id, object_value


def sources(store, kb_id):
    store.get(kb_id)
    with store.connect() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT body FROM web_sources WHERE kb_id=? ORDER BY rowid", (kb_id,))]


def source(store, kb_id, identifier):
    return next((s for s in sources(store, kb_id) if s["id"] == identifier), None)


def save_source(store, kb_id, payload, identifier=None):
    if store.get(kb_id)["engine"] != "lightrag_local":
        raise ServiceError("Website sources currently require a LightRAG local knowledge base")
    value = object_value(payload)
    fields = {"url", "path_prefix", "max_pages", "max_depth", "delay_seconds", "interval_hours", "delete_missing", "enabled"}
    if set(value) - fields:
        raise ServiceError("Unknown website source fields")
    previous = source(store, kb_id, identifier) if identifier else None
    if identifier and previous is None:
        raise ServiceError("Website source not found", 404)
    result = {"id": identifier or new_id("web"), "knowledge_base_id": kb_id, "max_pages": 200, "max_depth": 3,
              "delay_seconds": 0.3, "interval_hours": 0, "delete_missing": False, "enabled": True, **(previous or {}), **value}
    result["url"] = normalize(result.get("url", ""))
    result.setdefault("path_prefix", urlsplit(result["url"]).path.rstrip("/") or "/")
    prefix = result["path_prefix"]
    if not isinstance(prefix, str) or not prefix.startswith("/") or "?" in prefix or "#" in prefix:
        raise ServiceError("Website path prefix must be an absolute URL path")
    if previous and any(result[k] != previous[k] for k in ("url", "path_prefix")):
        raise ServiceError("Create a new source to change its URL or scope")
    path = urlsplit(result["url"]).path
    if prefix != "/" and path != prefix.rstrip("/") and not path.startswith(prefix.rstrip("/") + "/"):
        raise ServiceError("The starting URL must be within the path prefix")
    for key, low, high in (("max_pages", 1, 1000), ("max_depth", 0, 10), ("interval_hours", 0, 720)):
        v = result[key]
        if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
            raise ServiceError(f"{key} must be an integer between {low} and {high}")
    if isinstance(result["delay_seconds"], bool) or not isinstance(result["delay_seconds"], (int, float)) or not 0.1 <= result["delay_seconds"] <= 60:
        raise ServiceError("delay_seconds must be between 0.1 and 60")
    if any(not isinstance(result[k], bool) for k in ("delete_missing", "enabled")):
        raise ServiceError("Website enabled/delete_missing flags must be boolean")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO web_sources VALUES(?,?,?)", (result["id"], kb_id, canonical(result)))
    return result


def sync(store, kb_id, identifier, *, progress=lambda **_: None, cancelled=lambda: False, crawler=crawl):
    from loom.knowledge.lightrag_local import publish

    spec = source(store, kb_id, identifier)
    if not spec:
        raise ServiceError("Website source not found", 404)
    result = crawler(spec, progress, cancelled)
    with store.connect() as db:
        previous = {r["url"]: dict(r) for r in db.execute("SELECT * FROM web_pages WHERE source_id=?", (identifier,))}
    documents, updates = [], []
    for page in result["pages"]:
        old = previous.get(page["url"])
        doc_id = old["document_id"] if old else "doc_" + hashlib.sha256(f"{kb_id}/{identifier}/{page['url']}".encode()).hexdigest()[:32]
        name = f"{identifier}_{hashlib.sha256(page['url'].encode()).hexdigest()[:12]}_{page['title'][:150]}.md"
        content = f"# {page['title']}\n\nSource: {page['url']}\n\n{page['content']}"
        digest = hashlib.sha256(content.encode()).hexdigest()
        documents.append({"id": doc_id, "name": name, "content": content, "digest": digest, "url": page["url"]})
        updates.append({"url": page["url"], "document_id": doc_id, "digest": digest, "title": page["title"]})
    present = {p["url"] for p in updates}
    # Page/depth caps, network failures and robots exclusions never imply deletions.
    removed = [p["document_id"] for url, p in previous.items() if url not in present] if spec["delete_missing"] and result["complete"] else []
    changed = [doc for doc, page in zip(documents, updates, strict=True)
               if page["url"] not in previous or previous[page["url"]]["digest"] != page["digest"]]
    summary = {"pages": len(updates), "updated": len(changed), "unchanged": len(updates) - len(changed), "removed": len(removed),
               "complete": result["complete"], "errors": result["errors"][:100], "skipped": result["skipped"][:100]}

    def publication(db):
        current = json.loads(db.execute("SELECT body FROM web_sources WHERE id=? AND kb_id=?", (identifier, kb_id)).fetchone()[0])
        current.update(last_sync_at=now_iso(), last_result=summary)
        db.execute("UPDATE web_sources SET body=? WHERE id=?", (canonical(current), identifier))
        for page in updates:
            db.execute("INSERT OR REPLACE INTO web_pages VALUES(?,?,?,?,?,?)",
                       (identifier, kb_id, page["url"], page["document_id"], page["digest"], canonical(page)))

    progress(stage="diffing", **{k: summary[k] for k in ("pages", "updated", "unchanged", "removed")})
    if changed or removed:
        index = publish(store, kb_id, changed, removed, progress=progress, cancelled=cancelled, publication=publication)
    else:
        if cancelled():
            raise ServiceError("Knowledge job cancelled", 409)
        with store.connect() as db:
            publication(db)
        index = {"chunks": store.get(kb_id)["chunk_count"]}
    return {**index, **summary}
