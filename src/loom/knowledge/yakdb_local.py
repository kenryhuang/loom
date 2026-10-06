"""Versioned local YakDB indexes, accessed exclusively through isolated workers."""

import base64
import binascii
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from loom.core import now_iso
from loom.service.contracts import ServiceError, canonical, new_id, object_value, text

MAX_UPLOAD = 20 * 1024 * 1024


def available():
    return importlib.util.find_spec("yakdb") is not None and importlib.util.find_spec("aiosqlite") is not None


def require():
    if not available():
        raise ServiceError("Install yakdb[embedded] into the Loom service Python environment to use yakdb_local", 422)


def document_payload(payload):
    value = object_value(payload)
    name = text(value.get("name"), "name", max_length=300)
    if set(value) not in ({"name", "content"}, {"name", "file_base64"}):
        raise ServiceError("Document requires name and exactly one of content or file_base64")
    if "content" in value:
        data = text(value["content"], "content", max_length=500000).encode()
    else:
        encoded = value["file_base64"]
        if not isinstance(encoded, str) or len(encoded) > (MAX_UPLOAD + 2) // 3 * 4:
            raise ServiceError("File exceeds 20 MiB", 413)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ServiceError("Invalid base64 file") from exc
    if not data or len(data) > MAX_UPLOAD:
        raise ServiceError("File must contain between 1 byte and 20 MiB", 413)
    return name, data


def call(workspace, action, **values):
    require()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "loom.knowledge.yakdb_worker"],
            input=canonical({"workspace": str(workspace), "action": action, **values}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=180 if action == "index" else 60,
        )
        reply = json.loads(result.stdout)
        if result.returncode or "error" in reply:
            raise ServiceError(reply.get("error", "YakDB operation failed"), 422)
        return reply["result"]
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        raise ServiceError("YakDB worker failed or timed out; previous index retained", 502) from exc


def publish(store, base, payload=None, document_id=None):
    require()
    kb_id = base["id"]
    with store.connect() as db:
        documents = [dict(row) for row in db.execute("SELECT * FROM documents WHERE kb_id=?", (kb_id,))]
    if payload is not None:
        name, data = document_payload(payload)
        previous = next((doc for doc in documents if doc["name"] == name), None)
        digest = hashlib.sha256(data).hexdigest()
        if any(doc["digest"] == digest and doc["name"] != name for doc in documents):
            raise ServiceError("This file is already indexed under another name in this knowledge base", 409)
    else:
        previous = next((doc for doc in documents if doc["id"] == document_id), None)
        if not previous:
            raise ServiceError("Document not found", 404)
        name = previous["name"]
    root = store.directory / "yakdb" / kb_id
    root.mkdir(parents=True, exist_ok=True)
    generation = new_id("generation")
    workspace = root / generation
    database = workspace / ".yakdb" / "metadata.db"
    database.parent.mkdir(parents=True)
    old_generation = base.get("yakdb_generation")
    if old_generation:
        with sqlite3.connect(root / old_generation / ".yakdb" / "metadata.db") as source, sqlite3.connect(database) as target:
            source.backup(target)
    try:
        if payload is not None:
            with tempfile.NamedTemporaryFile(dir=workspace) as upload:
                upload.write(data)
                upload.flush()
                result = call(workspace, "index", name=name, upload=upload.name)
            pages = result["pages"]
            digest = hashlib.sha256(data).hexdigest()
            doc_id = previous["id"] if previous else new_id("doc")
        else:
            call(workspace, "delete", name=name)
            pages = 0
        total = sum(json.loads(doc["content"])["pages"] for doc in documents if doc["name"] != name) + pages
        if total > 20000:
            raise ServiceError("Knowledge base limit is 20000 pages", 413)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(db.execute("SELECT body FROM bases WHERE id=?", (kb_id,)).fetchone()[0])
            if current.get("yakdb_generation") != old_generation:
                raise ServiceError("Knowledge base changed during indexing; retry", 409)
            current.update(yakdb_generation=generation, yakdb_pages=total)
            db.execute("UPDATE bases SET body=? WHERE id=?", (canonical(current), kb_id))
            if payload is not None:
                db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?)", (doc_id, kb_id, name, digest, canonical({"pages": pages}), now_iso()))
            else:
                db.execute("DELETE FROM documents WHERE id=?", (document_id,))
        return {"document_id": doc_id, "name": name, "chunks": pages, "digest": digest} if payload is not None else {"deleted": document_id}
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def search(store, base_id, query, limit):
    with store.connect() as db:
        db.execute("BEGIN")
        base = json.loads(db.execute("SELECT body FROM bases WHERE id=?", (base_id,)).fetchone()[0])
        docs = {row["name"]: dict(row) for row in db.execute("SELECT id,name,digest FROM documents WHERE kb_id=?", (base_id,))}
    if not base.get("yakdb_generation"):
        return []
    workspace = store.directory / "yakdb" / base_id / base["yakdb_generation"]
    hits = call(workspace, "search", query=query, limit=limit)["results"]
    matches = []
    for rank, hit in enumerate(hits):
        doc = docs[hit["filename"]]
        page = hit["page_number"]
        matches.append(
            {
                "knowledge_base_id": base_id,
                "knowledge_base": base["name"],
                "document_id": doc["id"],
                "document": doc["name"],
                "digest": doc["digest"],
                "page_number": page,
                "section_title": hit.get("section_title"),
                "source_id": f"{base_id}/{doc['id']}#page={page}",
                "text": hit["text"],
                "truncated": hit["truncated"],
                "score": 1 / (61 + rank),
            }
        )
    return matches
