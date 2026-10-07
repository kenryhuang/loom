# Session knowledge bases

Loom service supports shared local knowledge bases and explicit per-session bindings. This follows DeepTutor's separation of engine selection (`services/rag/factory.py`), embedding-space binding (`embedding_binding.py`), and context-dependent retrieval tools (`agents/_shared/tool_composition.py`, `tools/rag_tool.py`). It does not require DeepTutor or its RAG dependencies.

## Engines and storage

- `sqlite_fts` (default): SQLite FTS5 keyword retrieval, including Chinese tokenization. No embedding service is needed.
- `yakdb_local`: separately installed YakDB, using its own local SQLite index, document parsers and page-level retrieval. Supports PDF, DOCX, PPTX, XLSX and text; no embedding endpoint is used.
- `sqlite_hybrid`: keyword retrieval plus cosine vector retrieval, combined with reciprocal rank fusion. Documents and vectors remain in local SQLite; embeddings come from a configured OpenAI-compatible endpoint.

Data lives in `<service-data-dir>/knowledge/knowledge.sqlite`. An embedding profile contains a name, the complete HTTP(S) embeddings endpoint, model, and optional API-key environment-variable name. The service resolves the secret from its environment or the `.env` file selected by `LOOM_ENV_FILE`; the secret itself is not stored in the profile. For example, a local endpoint may be `http://localhost:11434/v1/embeddings` with an installed embedding model. Endpoint redirects are rejected. Chunks and search queries are sent to that endpoint.

Profiles and a knowledge base's engine/profile binding are immutable. The first successful import pins the vector dimension. Use a new profile and knowledge base, then reimport documents when changing embedding models. Dimension checks cannot detect a provider silently replacing a model with another model of the same dimension.

## Web workflow

1. Open **Knowledge bases** in the sidebar.
2. For hybrid search, add an embedding profile; create a knowledge base using the selected engine.
3. Import UTF-8 text or Markdown by file or paste. Indexing runs in the background and exposes completion/failure status. Use **Search** to check retrieved passages.
4. Select knowledge bases when creating a session, or use **Knowledge bases → Configure** in the session info panel between tasks.

An import with the same document name atomically replaces the previous document. Failed imports retain the previous searchable version. Interrupted indexing jobs are marked failed after service restart; reimport to retry. Documents can be removed when that base has no pending indexing job.

The SQLite engines accept UTF-8 text. The YakDB engine additionally accepts binary PDF/Office documents. File uploads are limited to 500 KB in the web UI, text to 500,000 characters per API import, and HTTP request bodies to 1 MiB. There is a 20,000-passage limit per base and at most 20 attached bases per session. Local vector search scans the base's vectors; this engine is intended for modest local collections.

## Model behavior and evidence

Attached sessions expose two read-only tools: `knowledge_list` and `knowledge_search`. The model decides whether and when to retrieve; merely attaching a base does not issue a search or inject its entire contents into the conversation. Tools can only access bases attached to that session. Search results include document name, line range, source ID, document digest, and passage text. Tool instructions ask the model to cite evidence and treat retrieved text as reference data, not instructions.

Bindings persist across service restarts. Changing bindings between tasks rebuilds the next task's tool context while retaining conversation and trace history. Existing sessions default to no attached bases. Tool calls use the existing process/trajectory event pipeline. Embedding requests are separate from LLM task token usage and currently have no usage-meter integration. Retrieval ranking scores are relevance ordering signals, not confidence probabilities.

## HTTP API

All routes use the service's existing bearer authentication.

| Method | Route | Body / purpose |
| --- | --- | --- |
| GET | `/v1/knowledge-bases` | Catalog, profiles, engines |
| POST | `/v1/knowledge-bases` | `{ "name": "Manuals", "engine": "sqlite_fts" }` |
| POST | `/v1/knowledge-bases/embedding-profiles` | `{ "name": "Local", "endpoint": "http://localhost:11434/v1/embeddings", "model": "your-model" }` |
| GET | `/v1/knowledge-bases/{id}` | Documents and recent indexing jobs |
| POST | `/v1/knowledge-bases/{id}/documents` | `{ "name": "manual.md", "content": "Reference text" }`; returns 202 and job ID |
| GET | `/v1/knowledge-bases/{id}/jobs/{job_id}` | Persistent indexing status |
| POST | `/v1/knowledge-bases/{id}/search` | `{ "query": "startup command", "limit": 5 }` |
| POST | `/v1/knowledge-bases/{id}/documents/{doc_id}/delete` | `{}` |

For hybrid creation, set `engine` to `sqlite_hybrid` and supply `embedding_profile_id`. Session creation accepts `knowledge_base_ids`; existing sessions use the normal revision-checked, idempotent `set_knowledge_bases` command with a `knowledge_base_ids` payload.


## Installing and using YakDB local

YakDB is an optional, separately installed dependency. Loom contains an adapter only, with no copied YakDB implementation. The tested version is the local source checkout reporting `0.2.4` (that exact version is not currently available from the configured package index). Install into the same Python environment as Loom:

```sh
.venv/bin/python -m pip install "$HOME/workspace/yakDB[embedded]"
# Or, if using uv:
uv pip install --python .venv/bin/python "$HOME/workspace/yakDB[embedded]"
```

This builds a normal installed package; subsequent edits to the source checkout require reinstalling. YakDB also needs system libmagic; scanned PDFs use Tesseract and the applicable language packs. The current machine has English OCR but no Chinese language packs. Text-based Chinese PDFs do not require OCR. YakDB's existing mixed text/scanned PDF OCR heuristic remains unchanged. Remote vision calls are disabled in the adapter.

Choose **YakDB local** when creating a base. Select a PDF/Office/text file (up to 20 MiB) or paste text. For API uploads use `{ "name": "manual.pdf", "file_base64": "..." }`; this engine's document route permits a 28 MiB JSON request. Other routes keep their existing 1 MiB limit. Search results expose `page_number`, `section_title`, `source_id`, and page text (up to 12,000 characters, with a `truncated` flag). Indexing is limited to 180 seconds and search to 60 seconds per worker operation.

Each operation runs the installed package in a separate process to isolate YakDB's global backend and settings. Imports/deletions create a new SQLite snapshot and atomically publish its pointer after success. Originals remain under versioned directories in `<service-data-dir>/knowledge/yakdb/<kb-id>/`; prior generations are retained to protect in-flight readers. Automatic reclamation is not yet implemented, so repeated replacements consume additional disk space. Deleting a document removes it from current search results, not from retained historical snapshots.

Within one knowledge base, importing identical bytes under another filename is rejected explicitly because YakDB deduplicates by content. The same filename replaces the existing document. Bases remain isolated, and only session-bound bases can be searched. YakDB uses keyword retrieval, including its existing Chinese substring fallback; it is not a vector engine.


## DashScope embedding profile

The web form has a **Use DashScope · text-embedding-v4** preset. For the existing DashScope service configuration use:

```json
{
  "name": "DashScope text-embedding-v4",
  "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
  "model": "text-embedding-v4",
  "api_key_env": "LOOM_LLM_API_KEY"
}
```

This reuses the task model's key variable, including the service's configured `.env` fallback. The Beijing endpoint above was verified with the existing installation; use the endpoint matching your account's region for another deployment. Indexing submits at most 10 passages per embedding request, matching the [DashScope v4 synchronous API batch limit](https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api). The preset fills the form; click **Add embedding profile** to save, then select it when creating a hybrid knowledge base.

## Reading retrieved documents

`knowledge_read` reads only documents from bases attached to the session. Use
`read_with` from `knowledge_list` or `knowledge_search`, then `read_more` to
continue. PDF/Office pages use `page_number`; plain text uses page 1 and character
offsets. Each call returns at most 12,000 characters.

Model-facing document checksums are named `document_digest`; these are not
session artifact IDs. `read_artifact` remains the reader for retained session
artifacts. Successful knowledge retrieval is retained as `knowledge_source`
evidence and satisfies research output contracts; empty results do not.

Malformed tool-call JSON is returned as validation feedback to the model without
executing the call. Corrections remain subject to the existing run budgets.

## LightRAG local and website sources

Install the optional engine in the service environment (no LightRAG source is vendored):

```sh
uv pip install --python .venv/bin/python '.[knowledge-graph]'
```

This pins `lightrag-hku==1.5.7` and installs BeautifulSoup for local HTML extraction. Restart the service after installation. In **Knowledge → New**, choose **LightRAG local**, an indexing LLM from the service configuration, and an embedding profile (for example DashScope `text-embedding-v4`). The LLM extracts entities and relationships; embeddings support semantic retrieval. NetworkX, NanoVectorDB, and JSON stores run locally without Neo4j or another database server. Initial tokenizer setup may download tiktoken data.

Open the base in the main panel and use **Import URL**. Supply a starting URL, same-origin path scope, page/depth limits and optionally a periodic sync interval. **Import URL** starts a durable background job. Progress includes crawl counts/current URL, indexing stage/document, LLM calls, reported tokens and embedding inputs. **Cancel indexing** stops the current operation; **Sync now** retries. **Sync settings** changes limits, delay and interval; **Pause source** disables automatic scheduling. Scheduling runs only while Loom service is running.

The crawler follows public HTTP(S) HTML links, extracts readable HTML/Markdown/text, preserves original URLs, respects robots.txt and rate limits, and validates/pins public addresses on every request and redirect. It excludes query-string links and cannot log in or render JavaScript-only pages. Export those pages to Markdown/HTML first. Limits are 1,000 visited pages, depth 10, 4 MB per HTTP response, 500,000 characters per extracted page and 50 MB per crawl snapshot. Scope changes require a new source.

Sync hashes extracted content: unchanged pages skip LLM/embedding work, changed pages replace their graph contributions. Optional missing-page removal applies only to complete successful crawls; page/depth caps, robots exclusions or failures retain old pages. Crawl errors are shown on the source card. Each indexing operation has a 30-minute timeout and 500 LLM callback limit; retrieval/graph requests have a 3-minute timeout. Requests already in flight can finish after cancellation. Indexing usage is reported separately from session task usage; it is not charged against the task's token budget.

Documents, source definitions, URL provenance and jobs are in `knowledge/knowledge.sqlite`. Graph/vector/cache data is in `knowledge/lightrag/<kb-id>/<generation-id>/loom/`. Indexing uses a copied generation and atomically publishes its pointer only after successful completion. Failures/cancellation retain the previous searchable graph. Historical generations are retained and consume additional disk space; automatic reclamation is not implemented. Interrupted jobs are marked failed with an interrupted stage on restart; retry manually. LLM configuration and embedding profile/dimension are pinned; create a new base if changing them.

**Explore knowledge graph** loads up to 150 nodes, or an entity's neighborhood. Click nodes/relationships to inspect descriptions and source URLs; export the displayed graph as JSON. Overview counts are capped at 1,000 nodes and explicitly marked when truncated. Attach the base to a session normally: `knowledge_search` performs LightRAG mixed graph/vector retrieval and returns source passages plus related entities/relationships; `knowledge_read` reads original locally stored text with source URLs. The model decides when to retrieve. Crawled text is sent to the configured model and embedding services; storage remains local.

Additional authenticated routes:

| Method | Route | Body / purpose |
| --- | --- | --- |
| POST | `/v1/knowledge-bases` | `{"name":"Docs","engine":"lightrag_local","indexing_model":"main","embedding_profile_id":"emb_..."}` |
| GET / POST | `/v1/knowledge-bases/{id}/sources` | List / add a source (`url`, optional `path_prefix`, `max_pages`, `max_depth`, `delay_seconds`, `interval_hours`, `delete_missing`, `enabled`) |
| POST | `/v1/knowledge-bases/{id}/sources/{source_id}` | Update limits/schedule/flags; URL and scope are immutable |
| POST | `/v1/knowledge-bases/{id}/sources/{source_id}/sync` | `{}`; returns 202 with job ID |
| POST | `/v1/knowledge-bases/{id}/jobs/{job_id}/cancel` | `{}`; request cancellation |
| POST | `/v1/knowledge-bases/{id}/graph` | `{"label":"","limit":150}`; maximum 300 nodes |

LightRAG document deletion is asynchronous and returns 202 with a job ID. Text/Markdown imports use the existing document API; binary PDF/Office parsing remains available through YakDB local.
