import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from loom.knowledge.store import KnowledgeStore, embed
from loom.knowledge.tools import knowledge_collection
from loom.service.contracts import ServiceError
from loom.tasks.request import TaskRequest


def test_lexical_index_is_persistent_multilingual_citable_and_replaces_atomically(tmp_path):
    store = KnowledgeStore(tmp_path)
    base = store.create({"name": "Operations"})
    first = store.index(base["id"], {"name": "manual.md", "content": "# 启动说明\n启动服务使用 loom serve。\nMoonbase launch date is July 8."})
    hits = store.search([base["id"]], "启动服务")["matches"]
    assert hits and hits[0]["document"] == "manual.md" and hits[0]["start_line"] == 1
    assert hits[0]["source_id"].startswith(f"{base['id']}/{first['document_id']}#")
    reopened = KnowledgeStore(tmp_path)
    assert reopened.search([base["id"]], "Moonbase")["matches"]
    reopened.index(base["id"], {"name": "manual.md", "content": "Replacement: sunbase opens in August."})
    assert not reopened.search([base["id"]], "Moonbase")["matches"]
    assert len(reopened.get(base["id"])["documents"]) == 1
    assert reopened.search([base["id"]], 'sunbase OR " --')["matches"]
    reopened.remove_document(base["id"], first["document_id"])
    assert reopened.get(base["id"])["chunk_count"] == 0
    assert not reopened.search([base["id"]], "sunbase")["matches"]


def test_hybrid_profile_is_pinned_and_failed_reindex_keeps_previous_passages(tmp_path):
    dimension = 2
    fail = False

    def fake(profile, texts):
        if fail:
            raise ServiceError("Embedding offline", 502)
        return [[1.0] + [0.0] * (dimension - 1) for _ in texts]

    store = KnowledgeStore(tmp_path, embedder=fake)
    profile = store.create_profile({"name": "Local", "endpoint": "http://localhost:11434/v1/embeddings", "model": "embed-v1"})
    base = store.create({"name": "Semantic", "engine": "sqlite_hybrid", "embedding_profile_id": profile["id"]})
    store.index(base["id"], {"name": "notes", "content": "Astronaut training"})
    assert store.search([base["id"]], "cosmonaut")["matches"]  # vector-only match
    assert store.get(base["id"])["embedding_dimension"] == 2
    fail = True
    with pytest.raises(ServiceError):
        store.index(base["id"], {"name": "notes", "content": "Overwrite"})
    fail = False
    dimension = 3
    with pytest.raises(ServiceError, match="dimensions changed"):
        store.index(base["id"], {"name": "notes", "content": "Overwrite"})
    with pytest.raises(ServiceError, match="dimensions changed"):
        store.search([base["id"]], "astronaut")
    dimension = 2
    assert store.search([base["id"]], "astronaut")["matches"][0]["text"] == "Astronaut training"


@pytest.mark.asyncio
async def test_tool_only_searches_attached_bases_and_does_not_force_retrieval(tmp_path):
    store = KnowledgeStore(tmp_path)
    one, two = store.create({"name": "Allowed"}), store.create({"name": "Unbound"})
    store.index(one["id"], {"name": "a.md", "content": "Launch protocol Alpha"})
    collection = knowledge_collection(TaskRequest("Task", metadata={"knowledge_base_ids": [one["id"]], "knowledge_directory": str(tmp_path)}))
    listing = await collection.entrypoints["knowledge/knowledge_list"]({})
    assert [base["id"] for base in listing.value.value["knowledge_bases"]] == [one["id"]]
    search = collection.entrypoints["knowledge/knowledge_search"]
    assert (await search({"query": "Alpha"})).value.value["matches"]
    denied = await search({"query": "Alpha", "knowledge_base_ids": [two["id"]]})
    assert not denied.ok and "restricted" in denied.error.message
    assert all(binding.effect_kind == "read_only" for binding in collection.bindings())


def test_embedding_http_contract_validation_and_secret_reference(monkeypatch):
    requests = []
    invalid = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.headers.get("Authorization"), body))
            result = {"data": [{"index": i, "embedding": [0, 0] if invalid else [3, 4]} for i in reversed(range(len(body["input"])))]}
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LOOM_TEST_EMBED_KEY", "test-secret")
    profile = {"endpoint": f"http://127.0.0.1:{server.server_port}/v1/embeddings", "model": "test", "api_key_env": "LOOM_TEST_EMBED_KEY"}
    try:
        assert embed(profile, ["one", "two"]) == [[0.6, 0.8], [0.6, 0.8]]
        assert requests[0][0] == "Bearer test-secret"
        invalid = True
        with pytest.raises(ServiceError, match="invalid vectors"):
            embed(profile, ["bad"])
        monkeypatch.delenv("LOOM_TEST_EMBED_KEY")
        with pytest.raises(ServiceError, match="not set"):
            embed(profile, ["no request"])
        assert len(requests) == 2
    finally:
        server.shutdown()
        server.server_close()


def test_failed_job_and_restart_keep_existing_index(tmp_path):
    import time

    from loom.knowledge.jobs import KnowledgeJobs
    from loom.service.contracts import canonical

    store = KnowledgeStore(tmp_path)
    base = store.create({"name": "Stable"})
    store.index(base["id"], {"name": "data", "content": "Previous evidence"})
    jobs = KnowledgeJobs(store)
    try:
        job = jobs.start(base["id"], {"name": "data", "content": "binary\x00payload"})
        for _ in range(100):
            done = jobs.get(base["id"], job["id"])
            if done["state"] == "failed":
                break
            time.sleep(0.01)
        assert done["state"] == "failed"
        assert store.search([base["id"]], "Previous")["matches"]
    finally:
        jobs.close()
    with store.connect() as db:
        db.execute("INSERT INTO jobs VALUES(?,?,?)", ("orphan", base["id"], canonical({"id": "orphan", "state": "running"})))
    restarted = KnowledgeJobs(store)
    try:
        assert restarted.get(base["id"], "orphan")["state"] == "failed"
    finally:
        restarted.close()


def test_embedding_key_uses_service_dotenv_and_batches_fit_dashscope(tmp_path, monkeypatch):
    from loom.tasks.config import _env_value

    dotenv = tmp_path / ".env"
    dotenv.write_text("LOOM_TEST_DASHSCOPE_KEY=test-only\n")
    monkeypatch.setenv("LOOM_ENV_FILE", str(dotenv))
    monkeypatch.delenv("LOOM_TEST_DASHSCOPE_KEY", raising=False)
    assert _env_value("LOOM_TEST_DASHSCOPE_KEY", None) == "test-only"
    batches = []

    def fake(profile, texts):
        batches.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    store = KnowledgeStore(tmp_path / "kb", embedder=fake)
    profile = store.create_profile(
        {"name": "DashScope", "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings", "model": "text-embedding-v4"}
    )
    base = store.create({"name": "Long document", "engine": "sqlite_hybrid", "embedding_profile_id": profile["id"]})
    store.index(base["id"], {"name": "long.md", "content": "hello " * 4000})
    assert len(batches) > 1 and max(batches) == 10


@pytest.mark.asyncio
async def test_knowledge_reader_is_bounded_scoped_and_distinguishes_document_checksums(tmp_path):
    from loom.core import thaw_json

    store = KnowledgeStore(tmp_path)
    base, other = store.create({"name": "Allowed"}), store.create({"name": "Other"})
    doc = store.index(base["id"], {"name": "manual.md", "content": "Launch Alpha. " * 50})
    foreign = store.index(other["id"], {"name": "private.md", "content": "Private"})
    collection = knowledge_collection(TaskRequest("Read", metadata={"knowledge_base_ids": [base["id"]], "knowledge_directory": str(tmp_path)}))
    listing = thaw_json((await collection.entrypoints["knowledge/knowledge_list"]({})).value.value)
    entry = listing["knowledge_bases"][0]["documents"][0]
    assert "digest" not in entry and entry["document_digest"] == doc["digest"]
    hits = thaw_json((await collection.entrypoints["knowledge/knowledge_search"]({"query": "Alpha"})).value.value)["matches"]
    assert "digest" not in hits[0] and hits[0]["read_with"] == entry["read_with"]
    read = collection.entrypoints["knowledge/knowledge_read"]
    first = thaw_json((await read({**entry["read_with"], "limit": 20})).value.value)
    second = thaw_json((await read(first["read_more"])).value.value)
    assert first["has_more"] and len(first["text"]) == 20 and second["offset"] == 20
    assert not (await read({"knowledge_base_id": other["id"], "document_id": foreign["document_id"]})).ok
    assert not (await read({"knowledge_base_id": base["id"], "document_id": foreign["document_id"]})).ok
    for args in ({"offset": -1}, {"limit": 12001}, {"page_number": 2}, {"offset": 10000}):
        with pytest.raises(ServiceError):
            store.read(base["id"], doc["document_id"], **args)


@pytest.mark.asyncio
async def test_knowledge_evidence_satisfies_research_contract_and_survives_restore(tmp_path):
    from loom.tasks.assembly import TaskAssembly

    store = KnowledgeStore(tmp_path)
    base = store.create({"name": "References"})
    store.index(base["id"], {"name": "manual.md", "content": "Launch Alpha safely."})
    request = TaskRequest("Research", metadata={"knowledge_base_ids": [base["id"]], "knowledge_directory": str(tmp_path)}, task_spec={
        "tools": {"collections": ["knowledge", "task_control"]},
        "outputs": [{"kind": "report", "format": "markdown", "require_evidence_refs": True, "require_verified_sources": True}],
    })
    assembly = TaskAssembly(request, plan_mode="off")
    assert assembly.completion_error()
    await assembly.handlers()["knowledge_search"]({"query": "zzzznonexistent"})
    assert assembly.completion_error()  # An empty search is not evidence.
    result = await assembly.handlers()["knowledge_search"]({"query": "Alpha"})
    assert result.ok
    assert assembly.completion_error() is None
    restored = TaskAssembly(request, plan_mode="off")
    restored.restore(assembly.snapshot())
    assert restored.completion_error() is None
    await assembly.close()
    await restored.close()
