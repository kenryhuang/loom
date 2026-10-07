"""Private subprocess bridge to the separately installed LightRAG package."""

import asyncio
import contextlib
import json
import sys
from dataclasses import replace
from urllib.error import HTTPError, URLError

from loom.llm.api import LlmMessage
from loom.llm.request_options import materialize_request_options
from loom.service.contracts import ServiceError
from loom.tasks.config import create_provider_from_task_config, load_task_config


class WorkerError(Exception):
    """Safe adapter diagnostic; never constructed from provider response bodies."""


def emit(value):
    print(json.dumps(value, ensure_ascii=False), file=sys.__stdout__, flush=True)


async def embed_batch(embedder, profile, texts, progress, sleep=asyncio.sleep):
    """Retry only transient transport/rate-limit failures, never invalid vectors."""
    for attempt in range(3):
        try:
            return await asyncio.to_thread(embedder, profile, texts)
        except ServiceError as exc:
            cause = exc.__cause__
            transient = ((cause.code == 429 or cause.code >= 500) if isinstance(cause, HTTPError)
                         else isinstance(cause, (TimeoutError, URLError, ConnectionError)))
            if not transient or attempt == 2:
                raise
            progress(stage="embedding_retry", retry=attempt + 1, detail=f"Transient embedding failure ({type(cause).__name__}); retrying batch")
            await sleep(2 ** (attempt + 1))


async def execute(value):
    emit({"progress": {"stage": "initializing"}})
    import numpy as np
    from lightrag import LightRAG, QueryParam
    from lightrag.utils import EmbeddingFunc

    from loom.knowledge.store import embed

    config = load_task_config(value["config_path"]).unwrap()
    name = value["model"]
    selected = config.models[name]
    options = materialize_request_options(selected.request_options)
    if "enable_thinking" in options:
        options["enable_thinking"] = False
    selected = replace(selected, max_completion_tokens=8192, request_options=options)
    provider = create_provider_from_task_config(replace(config, models={**config.models, name: selected}), model_name=name).unwrap()
    usage = {"llm_calls": 0, "reported_tokens": 0, "embedding_inputs": 0}
    failures = []

    async def llm(prompt, system_prompt=None, history_messages=None, **_):
        if usage["llm_calls"] >= value.get("max_calls", 500):
            raise WorkerError("Indexing model call budget reached")
        usage["llm_calls"] += 1
        emit({"progress": {"stage": "extracting", **usage}})
        messages = [LlmMessage("system", system_prompt)] if system_prompt else []
        messages.extend(LlmMessage(m["role"], m.get("content", "")) for m in history_messages or [])
        messages.append(LlmMessage("user", prompt))
        response = await provider.chat(messages, tools=None)
        if not response.ok or not response.value.content or response.value.finish_reason == "length":
            detail = response.error.code if not response.ok else (response.value.finish_reason or "empty content")
            failures.append(f"Indexing model request failed ({detail})")
            raise WorkerError(failures[-1])
        usage["reported_tokens"] += response.value.usage.total_tokens
        emit({"progress": {"stage": "extracting", **usage}})
        return response.value.content

    async def embeddings(texts, **_):
        usage["embedding_inputs"] += len(texts)
        emit({"progress": {"stage": "embedding", **usage}})
        vectors = []
        for offset in range(0, len(texts), 10):
            try:
                vectors.extend(await embed_batch(embed, value["profile"], texts[offset:offset + 10],
                                                lambda **p: emit({"progress": {**p, **usage}})))
            except Exception as exc:
                detail = str(exc) if isinstance(exc, ServiceError) else type(exc).__name__
                cause = type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__
                failures.append(f"Embedding failed: {detail} ({cause})")
                emit({"progress": {"stage": "embedding_failed", "detail": failures[-1], **usage}})
                raise
        if any(len(v) != value["dimension"] for v in vectors):
            raise WorkerError("Embedding dimensions changed; create a new knowledge base")
        return np.asarray(vectors)

    rag = LightRAG(
        working_dir=value["workspace"], workspace="loom", llm_model_func=llm, llm_model_name=selected.model,
        embedding_func=EmbeddingFunc(embedding_dim=value["dimension"], max_token_size=4096, func=embeddings),
        kv_storage="JsonKVStorage", vector_storage="NanoVectorDBStorage", graph_storage="NetworkXStorage",
        doc_status_storage="JsonDocStatusStorage", llm_model_max_async=2, embedding_func_max_async=2,
        embedding_batch_num=10, chunk_token_size=900, chunk_overlap_token_size=100, entity_extract_max_gleaning=0,
    )
    await rag.initialize_storages()
    try:
        if value["action"] == "index":
            for identifier in value.get("delete_ids", []):
                result = await rag.adelete_by_doc_id(identifier)
                if result.status not in {"success", "not_found"}:
                    raise WorkerError("LightRAG could not remove an old document")
            docs = value.get("documents", [])
            for i, doc in enumerate(docs):
                emit({"progress": {"stage": "indexing", "indexed": i, "total": len(docs), "current_document": doc["name"], **usage}})
                await rag.ainsert(doc["content"], ids=doc["id"], file_paths=doc.get("url") or doc["name"])
                status = await rag.doc_status.get_by_id(doc["id"])
                if not status or status.get("status") != "processed":
                    detail = failures[-1] if failures else "LightRAG did not finish indexing the document"
                    raise WorkerError(f"{doc['name']}: {detail}; previous index retained")
                emit({"progress": {"stage": "indexing", "indexed": i + 1, "total": len(docs), **usage}})
            graph = await rag.get_knowledge_graph("*", max_depth=2, max_nodes=1000)
            counts = [await rag.doc_status.get_by_id(identifier) for identifier in value.get("all_ids", [])]
            return {"entities": len(graph.nodes), "relations": len(graph.edges), "graph_truncated": graph.is_truncated,
                    "chunks": sum((status or {}).get("chunks_count", 0) or 0 for status in counts), "usage": usage}
        if value["action"] == "graph":
            graph = await rag.get_knowledge_graph(value.get("label") or "*", max_depth=2, max_nodes=value.get("limit", 150))
            return graph.model_dump()
        data = await rag.aquery_data(value["query"], QueryParam(
            mode=value.get("mode", "mix"), top_k=20, chunk_top_k=value["limit"], enable_rerank=False,
            max_total_tokens=6000, max_entity_tokens=1500, max_relation_tokens=1500,
        ))
        for chunk in data.get("data", {}).get("chunks", []):
            stored = await rag.text_chunks.get_by_id(chunk["chunk_id"])
            chunk["document_id"] = (stored or {}).get("full_doc_id")
        return data
    finally:
        await rag.finalize_storages()


def main():
    try:
        value = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(execute(value))
        emit({"result": result})
    except Exception as exc:
        # Never forward provider bodies, credentials, or raw document text into status logs.
        emit({"error": str(exc) if isinstance(exc, WorkerError) else
              f"LightRAG operation failed ({type(exc).__name__}). Check model/embedding availability and indexing limits."})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
