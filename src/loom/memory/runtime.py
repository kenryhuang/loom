"""TaskAssembly adapter: bounded prompt references and scoped read-only tools."""

import asyncio
from dataclasses import replace

from loom.core import Observation, ToolRef, err, make_loom_error, new_trace_id, now_iso, ok, thaw_json
from loom.memory.store import MemoryError, MemoryStore
from loom.runtime.execution_contracts import validate_tool_input

TOOL_IDS = {"memory_search", "memory_read"}


def compact_memory(value):
    """Project searchable references; the scoped store retains complete records."""
    if value.get('storage') == 'memory_v2':
        from loom.memory.query import packet

        return packet(value)  # Already bounded; do not truncate applicability or conflict caveats.
    records = []
    for row in value.get("records", []):
        item = {k: row[k] for k in ("kind", "confidence", "basis", "validation", "review_state",
                                     "effective_confidence", "confidence_basis", "human_confirmed", "pinned") if k in row}
        source = row.get("source", {})
        item["source"] = {k: source[k] for k in ("evaluation_id", "record_id", "content_digest", "memory_id", "version_id") if k in source}
        truncated = []
        for key, limit in (("title", 100), ("summary", 160), ("scope", 100)):
            text = row.get(key)
            if isinstance(text, str):
                if key == "summary" and text == row.get("title"):
                    item["summary_same_as_title"] = True
                    continue
                item[key] = text[:limit]
                if len(text) > limit:
                    truncated.append(key)
        if truncated:
            item["truncated_fields"] = truncated
        records.append(item)
    from loom.memory.query import packet

    return packet({**value, "records": records})


class RuntimeMemory:
    def __init__(self, database, *, workspace=None, session_id="", allowed_tools=None):
        self.store = MemoryStore(database, workspace=workspace, session_id=session_id)
        self.allowed = TOOL_IDS if allowed_tools is None else TOOL_IDS & set(allowed_tools)
        self.query = None
        self.summary = None
        self.available = False
        self.epoch = None

    def refs(self, *, include_unavailable=False):
        if not include_unavailable and not self.available:
            return ()
        refs = (
            ToolRef("memory_search", "Search accepted Deep eval memories in this task's workspace (or current session without a workspace). "
                    "Historical references only; not current facts or acceptance evidence.", input_schema={
                        "type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 20000},
                                                          "limit": {"type": "integer", "minimum": 1, "maximum": 6}},
                        "required": ["query"], "additionalProperties": False}),
            ToolRef("memory_read", "Read an accepted historical memory and its original citations. "
                    "Use memory_id/version_id for canonical knowledge, or evaluation_id/record_id for the original historical extraction. "
                    "Reads remain scope-restricted.", input_schema={
                        "type": "object", "properties": {"evaluation_id": {"type": "string"}, "record_id": {"type": "string"},
                                                          "memory_id": {"type": "string"}, "version_id": {"type": "string"},
                                                          "offset": {"type": "integer", "minimum": 0},
                                                          "limit": {"type": "integer", "minimum": 1, "maximum": 6000}},
                        "anyOf": [{"required": ["evaluation_id", "record_id"]}, {"required": ["memory_id", "version_id"]}],
                        "additionalProperties": False}),
        )
        return tuple(ref for ref in refs if ref.id in self.allowed)

    def handlers(self):
        handlers = {}
        for ref in self.refs(include_unavailable=True):
            async def invoke(value, _options=None, ref=ref):
                try:
                    value = thaw_json(value)
                    validate_tool_input(ref.input_schema, value)
                    method = self.store.search if ref.id == "memory_search" else self.store.read
                    result = await asyncio.to_thread(method, **value)
                    return ok(Observation(new_trace_id(), ref.id, result, now_iso()))
                except (MemoryError, ValueError, TypeError) as exc:
                    return err(make_loom_error(getattr(exc, 'code', 'memory.query_invalid'), str(exc), retryable=False))
            handlers[ref.id] = invoke
        return handlers

    async def attach(self, context, runtime):
        # Short continuation replies (e.g. "continue") retain the interrupted goal's retrieval context.
        prior = (context.metadata or {}).get('prior_requests', ())
        query = "\n".join([context.goal.objective, *list(prior)[-2:]])[:20000]
        supplied = (context.metadata or {}).get('agent_context', {}).get('memory')
        if supplied is not None:
            # The persisted Agent snapshot is the exact packet consumed by execution.
            changed = self.summary != supplied
            self.summary = supplied
            self.available = supplied.get('available_records', len(supplied.get('records', []))) > 0
            metadata = dict(context.metadata or {})
            metadata.pop('retrieved_memory', None)
            if changed and runtime.trace_sink:
                (await runtime.trace_sink.emit(dict(type='memory.retrieved', packet_id=supplied.get('packet_id'),
                    packet_digest=supplied.get('packet_digest'), sources=[r['source'] for r in supplied.get('records', [])],
                    run_id=context.run_id, trace_id=runtime.trace_id, at=now_iso()))).unwrap()
            if supplied.get('records') or supplied.get('conflicts'):
                metadata['retrieved_memory'] = supplied
            return replace(context, metadata=metadata)
        epoch = await asyncio.to_thread(self.store.epoch)
        if self.query != query or self.epoch != epoch:
            self.query = query
            self.epoch = epoch
            try:
                self.summary = await asyncio.to_thread(self.store.search, query)
                self.available = self.summary['available_records'] > 0
                event = dict(type="memory.retrieved", query=query, sources=[r['source'] for r in self.summary['records']],
                             truncated=self.summary['truncated'], issues=self.summary['issues'])
            except MemoryError as exc:
                self.summary = None
                self.available = False
                event = dict(type="memory.unavailable", code=exc.code, message=str(exc))
            if runtime.trace_sink:
                (await runtime.trace_sink.emit({**event, "run_id": context.run_id, "trace_id": runtime.trace_id, "at": now_iso()})).unwrap()
        metadata = dict(context.metadata or {})
        metadata.pop('retrieved_memory', None)
        if self.summary and (self.summary['records'] or self.summary.get('conflicts')):
            metadata['retrieved_memory'] = (compact_memory(self.summary) if (context.metadata or {}).get('agent_context')
                                            and 'memory_read' in self.allowed else self.summary)
        return replace(context, metadata=metadata)
