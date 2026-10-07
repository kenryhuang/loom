"""Atomic LightRAG generations; model and vector identities are pinned per base."""

import fcntl
import hashlib
import importlib.metadata
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from loom.core import now_iso
from loom.llm.request_options import materialize_request_options
from loom.service.contracts import ServiceError, canonical, new_id
from loom.tasks.config import load_task_config

VERSION = "1.5.7"


def require():
    try:
        if importlib.metadata.version("lightrag-hku") != VERSION:
            raise ServiceError(f"Install lightrag-hku=={VERSION} in the Loom service environment", 422)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ServiceError(f"Install lightrag-hku=={VERSION} in the Loom service environment", 422) from exc


def binding(config_path, model):
    if not config_path:
        raise ServiceError("LightRAG requires a configured LLM model", 422)
    loaded = load_task_config(config_path)
    if not loaded.ok or model not in loaded.value.models:
        raise ServiceError("Select an available indexing model", 422)
    value = loaded.value.models[model]
    identity = {"provider": value.provider, "model": value.model, "base_url": value.base_url,
                "temperature": value.temperature, "request_options": materialize_request_options(value.request_options)}
    return hashlib.sha256(canonical(identity).encode()).hexdigest()


@contextmanager
def ownership(store, kb_id, cancelled=lambda: False):
    directory = store.directory / "lightrag" / kb_id
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "writer.lock").open("a+") as lock:
        started = time.monotonic()
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if cancelled() or time.monotonic() - started > 180:
                    raise ServiceError("LightRAG is busy; retry after the current operation", 409) from None
                time.sleep(0.1)
        try:
            yield directory
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def call(store, base, workspace, action, *, progress=lambda **_: None, cancelled=lambda: False, **values):
    require()
    if binding(store.config_path, base["indexing_model"]) != base["model_fingerprint"]:
        raise ServiceError("Indexing model configuration changed; create a new knowledge base to reindex", 409)
    request = {"workspace": str(workspace), "action": action, "config_path": str(store.config_path), "model": base["indexing_model"],
               "profile": store._profile(base), "dimension": base["embedding_dimension"], **values}
    process = subprocess.Popen([sys.executable, "-m", "loom.knowledge.lightrag_worker"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    messages = queue.Queue()

    def read():
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        process.stdin.write(canonical(request))
        process.stdin.close()
        deadline = time.monotonic() + (1800 if action == "index" else 180)
        result, finished = None, False
        while not finished:
            if cancelled():
                raise ServiceError("Knowledge job cancelled", 409)
            if time.monotonic() > deadline:
                raise ServiceError("LightRAG time limit reached; previous index retained", 504)
            try:
                line = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                finished = True
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "progress" in message:
                progress(**message["progress"])
            if "error" in message:
                raise ServiceError(message["error"], 502)
            if "result" in message:
                result = message["result"]
        if process.wait(timeout=5) or result is None:
            raise ServiceError("LightRAG worker exited without a result", 502)
        return result
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()
        reader.join(timeout=1)


def publish(store, kb_id, documents, delete_ids=(), *, progress=lambda **_: None, cancelled=lambda: False, publication=None):
    with ownership(store, kb_id, cancelled) as directory:
        base = store.get(kb_id)
        generation = new_id("generation")
        workspace = directory / generation
        previous = base.get("lightrag_generation")
        if previous:
            shutil.copytree(directory / previous, workspace)
        else:
            workspace.mkdir()
        try:
            with store.connect() as db:
                existing = {row["id"]: dict(row) for row in db.execute("SELECT * FROM documents WHERE kb_id=?", (kb_id,))}
            changed = [doc for doc in documents if doc["id"] not in existing or doc["digest"] != existing[doc["id"]]["digest"]]
            removed = set(delete_ids) & set(existing)
            replaced = [doc["id"] for doc in changed if doc["id"] in existing]
            if not base["embedding_dimension"]:
                progress(stage="checking_embedding")
                base["embedding_dimension"] = len(store.embedder(store._profile(base), ["embedding dimension probe"])[0])
            all_ids = (set(existing) - removed) | {d["id"] for d in changed}
            result = call(store, base, workspace, "index", documents=changed, delete_ids=sorted(removed | set(replaced)),
                          all_ids=sorted(all_ids), progress=progress, cancelled=cancelled)
            if cancelled():
                raise ServiceError("Knowledge job cancelled", 409)
            progress(stage="publishing")
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = json.loads(db.execute("SELECT body FROM bases WHERE id=?", (kb_id,)).fetchone()[0])
                if current.get("lightrag_generation") != previous:
                    raise ServiceError("Knowledge index changed during publication", 409)
                for identifier in removed:
                    db.execute("DELETE FROM documents WHERE kb_id=? AND id=?", (kb_id, identifier))
                    db.execute("DELETE FROM web_pages WHERE kb_id=? AND document_id=?", (kb_id, identifier))
                for doc in changed:
                    db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?)",
                               (doc["id"], kb_id, doc["name"], doc["digest"], doc["content"], now_iso()))
                current.update(lightrag_generation=generation, embedding_dimension=base["embedding_dimension"],
                               graph_stats={k: result[k] for k in ("entities", "relations", "graph_truncated", "chunks")})
                db.execute("UPDATE bases SET body=? WHERE id=?", (canonical(current), kb_id))
                if publication:
                    publication(db)
            return {**result, "documents_updated": len(changed), "documents_removed": len(removed), "generation": generation}
        except BaseException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise


def search(store, kb_id, query, limit, mode="mix"):
    with ownership(store, kb_id) as directory:
        base = store.get(kb_id)
        if not base.get("lightrag_generation"):
            return []
        result = call(store, base, directory / base["lightrag_generation"], "search", query=query, limit=limit, mode=mode)
        with store.connect() as db:
            docs = {r["id"]: dict(r) for r in db.execute("SELECT id,name,digest FROM documents WHERE kb_id=?", (kb_id,))}
            urls = {r["document_id"]: r["url"] for r in db.execute("SELECT document_id,url FROM web_pages WHERE kb_id=?", (kb_id,))}
        matches = []
        data = result.get("data", {})
        for rank, chunk in enumerate(data.get("chunks", [])[:limit]):
            doc = docs.get(chunk.get("document_id"))
            if not doc:
                continue
            matches.append({"knowledge_base_id": kb_id, "knowledge_base": base["name"], "document_id": doc["id"],
                            "document": doc["name"], "digest": doc["digest"], "source_id": f"{kb_id}/{doc['id']}#{chunk['chunk_id']}",
                            "source_url": urls.get(doc["id"]), "text": chunk["content"][:12000], "start_line": None, "end_line": None,
                            "score": 1 / (61 + rank), "graph_entities": data.get("entities", [])[:10],
                            "graph_relationships": data.get("relationships", [])[:10]})
        return matches


def graph(store, kb_id, label="", limit=150):
    if not isinstance(label, str) or len(label) > 200 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 300:
        raise ServiceError("Graph label must be at most 200 characters and limit between 1 and 300")
    with ownership(store, kb_id) as directory:
        base = store.get(kb_id)
        if base["engine"] != "lightrag_local":
            raise ServiceError("This engine does not build a knowledge graph")
        if not base.get("lightrag_generation"):
            return {"nodes": [], "edges": [], "is_truncated": False}
        return call(store, base, directory / base["lightrag_generation"], "graph", label=label, limit=limit)
