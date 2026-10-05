# Session trajectory analysis

Open a session in the web client and choose the **Trajectory** tab below its header.
The route is `/web/#/sessions/<session-id>/trajectory`; authentication stays in
the current browser tab. The **Conversation** tab returns to the same session while preserving analysis
filters and expanded details. Browser
Back/Forward also navigate between these views.

The page shows recorded model and tool calls, provider token usage, context
additions/removals, errors, recovery events and verification observations.
Filter by run, failed/incomplete calls or text. Expanding a model call loads
its context and verification details. Request, Response, Input and Result
buttons read evidence from the analyzed snapshot in bounded character pages.
Tools with no established model-call link are listed separately.

These are factual observations. Semantic effectiveness is `not_evaluated`,
and task completion is `unverified`. A successful tool, runtime completion or
test output is not an independent proof that the task was satisfied. Missing
artifacts, unscoped events and unmeasured tokens are reported explicitly.

## Existing evaluation and the service adapter

`loom.evaluation` already provides two offline pipelines:

- v1 reconstructs episode graphs and deterministic metrics, with optional
  model/step judges.
- v2 builds evidence-addressed task contracts, trajectories, context deltas,
  tool-use records, token ledgers and verification evidence. Optional semantic
  judging covers context, tools, progress, tokens and verification.

The service integration reuses v2 `EvidenceStore`, `build_episode_graph` and
`build_fact_analysis`. It does not execute trace commands or call a model.
The existing CLI remains available for explicit semantic judging, whose
coverage and model calibration limits are documented in
[trace effectiveness analysis](trace-effectiveness-analysis.md).

Session events cannot be passed directly to the offline graph builder:

1. The session envelope owns the persistent run ID and sequence, while engine
   scope information is nested in the payload or completed trace.
2. Large requests/results are replaced with `event_detail` artifact references.
   The adapter verifies session ownership and artifact integrity before reading
   them. Unavailable detail remains a coverage gap.
3. Managed model events omit step numbers. The adapter fills them only when
   recorded events establish one unique matching run/trace/loop scope.
4. Managed JSON actions use `<trace>-json-<action>-<call>` IDs. The shared tool
   linker now recognizes this producer convention as well as legacy and native
   IDs. A managed call preserved across pause/resume can also link across loop
   IDs when its run, trace and step match and both call identities are unique;
   ambiguous ownership remains unlinked.

Individual stream chunks and stream/argument boundaries are omitted from the
analysis source. Complete requests/responses and tool results are retained.
Each normalized source record carries `session_seq`, so evidence reads can
point back to the original event. The immutable exported source has its own
SHA-256; it does not claim that the original events had producer hashes.

## Jobs, persistence and API

Analysis captures the session event cursor when requested. A single background
worker calculates facts outside the session-store transaction, so task
execution, snapshots and SSE readers can continue. At most four jobs can be
queued/running. Repeated requests at the same cursor and analyzer version reuse
the same job. Results are content-addressed service artifacts registered to the
session; job metadata is stored in `session_analyses`.

Completed analyses survive restarts. Interrupted jobs become retryable failures.
The web view warns when new session events exist beyond its snapshot; Refresh
analysis captures a newer cursor. Navigating away aborts browser polling but
allows the background job to finish and populate the cache. Analysis does not
append bookkeeping events to the task's event stream or alter task budgets.

All endpoints require the normal bearer credential and enforce session scope:

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `/v1/sessions/{sid}/trajectory` | Empty JSON object; returns/reuses a cursor-pinned job |
| GET | `/v1/sessions/{sid}/trajectory/{id}` | Job status and compact analysis when completed |
| GET | `/v1/sessions/{sid}/trajectory/{id}/round?round_id=...` | One model call's factual details |
| GET | `/v1/sessions/{sid}/trajectory/{id}/evidence?line=...&field=...&start=...&limit=...` | Evidence from this job's source; at most 32,000 characters |

Clients cannot supply filesystem trace paths or cross-session artifact IDs.
Evidence references are tied to the job snapshot even after the session grows.
The service retains two recently used source/result pairs in memory; historical
results reload from integrity-checked artifacts as needed.

Adding semantic analysis later should be an explicit job mode with its own
provider configuration, bounded judge calls, usage and coverage. It should
preserve this source/evidence boundary rather than mix evaluator events or
token charges into the task execution.
