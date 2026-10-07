# Cooperative stopping in service sessions

A model/tool exchange is not necessarily a runtime step. A single managed step can contain many model requests and tool calls. `max_steps` alone therefore does not bound research loops.

Service sessions now retain a bounded retrieval ledger in their execution checkpoint and committed context, independently of prompt compaction. It records recent queries and readable source excerpts, and fingerprints evidence rather than query wording. The model receives an execution-progress reminder to identify a concrete missing requirement before another tool call and synthesize once the requested answer is supported. Research instructions no longer imply reading every remaining page of a source.

Eight consecutive retrieval calls without new readable evidence trigger a partial-result stop. This applies to knowledge searches/reads/listing, artifact reads and URL fetches. New evidence resets the streak; explicit user guidance resets the ledger. The ledger retains up to 512 fingerprints and 40 excerpts; repeated topics with different substantive evidence are not treated as duplicates. This is a conservative heuristic, not a semantic proof that research is complete.

Before further exploration, the supervisor reserves:

- One remaining model call for synthesis.
- Token headroom based on 15% of the limit, a 2,048-token floor, and twice the most recent request cost, capped at 80% of the limit. Checks use reported usage, not estimates of unreported usage.
- The last 20% of active time, capped at 120 seconds, for synthesis.
- The last runtime step when the configured limit is greater than one.

The wrap-up request has no tools, a compact evidence context, a maximum completion length of 2,048 tokens on configurable providers, and a timeout of at most 60 seconds within remaining active time. It must explain work done, supported findings, unfinished/unverified requirements and next steps. Its usage is accounted as a normal task model call. If there is no usable budget, synthesis times out or the provider fails, a deterministic report preserves the available excerpts and explicitly states that completion has not been verified. An in-flight model response can still exceed a token threshold; this is not a provider-side exact token reservation.

Partial reports are persisted as assistant messages in the conversation. The run remains suspended and the task paused; incomplete workflow nodes and output contracts are not falsely marked successful. Increase budget and explicitly resume to continue. A response already received at a hard token boundary is retained so resuming does not issue the same model request again. A normal accepted `finish` still completes immediately.

`run.wrapping_up` records the reason in the trace and updates Session Info. It does not add a status row to the conversation event stream. The final partial report is an ordinary readable conversation result.
