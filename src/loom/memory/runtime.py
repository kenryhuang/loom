"""TaskAssembly adapter: bounded prompt references and scoped read-only tools."""

import asyncio
from dataclasses import replace

from loom.core import Observation, ToolRef, err, make_loom_error, new_trace_id, now_iso, ok, thaw_json
from loom.memory.store import MemoryError, MemoryStore
from loom.runtime.execution_contracts import validate_tool_input

TOOL_IDS = {"memory_search", "memory_read"}


class RuntimeMemory:
    def __init__(self, database, *, workspace=None, session_id="", allowed_tools=None):
        self.store = MemoryStore(database, workspace=workspace, session_id=session_id)
        self.allowed = TOOL_IDS if allowed_tools is None else TOOL_IDS & set(allowed_tools)
        self.query = None
        self.summary = None
        self.available = False

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
                    "Use evaluation_id and record_id from memory_search or supplied summaries; reads remain scope-restricted.", input_schema={
                        "type": "object", "properties": {"evaluation_id": {"type": "string"}, "record_id": {"type": "string"},
                                                          "offset": {"type": "integer", "minimum": 0},
                                                          "limit": {"type": "integer", "minimum": 1, "maximum": 6000}},
                        "required": ["evaluation_id", "record_id"], "additionalProperties": False}),
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
        if self.query != query:
            self.query = query
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
        if self.summary and self.summary['records']:
            metadata['retrieved_memory'] = self.summary
        return replace(context, metadata=metadata)
