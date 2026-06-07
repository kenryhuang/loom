"""Trace storage and observability helpers."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

from loom.core.models import (
    Context,
    FrozenDict,
    LoomError,
    Result,
    Trace,
    TraceSnapshot,
    err,
    make_loom_error,
    ok,
)


class InMemoryTraceStore:
    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}
        self._order: list[str] = []
        self._events: list[Mapping[str, Any]] = []
        self._by_run_id: dict[str, list[str]] = defaultdict(list)
        self._by_loop_id: dict[str, list[str]] = defaultdict(list)
        self._by_root_trace_id: dict[str, list[str]] = defaultdict(list)
        self._by_parent_trace_id: dict[str, list[str]] = defaultdict(list)
        self._by_outcome: dict[str, list[str]] = defaultdict(list)
        self._by_tag: dict[str, list[str]] = defaultdict(list)

    async def append(self, trace: Trace) -> Result:
        if trace.id in self._traces:
            return err(
                make_loom_error(
                    "VALIDATION_FAILED",
                    "Trace already exists",
                    retryable=False,
                    trace_id=trace.id,
                )
            )

        self._traces[trace.id] = trace
        self._order.append(trace.id)
        self._by_run_id[trace.run_id].append(trace.id)
        self._by_loop_id[trace.loop_id].append(trace.id)
        self._by_root_trace_id[trace.root_trace_id].append(trace.id)
        if trace.parent_trace_id is not None:
            self._by_parent_trace_id[trace.parent_trace_id].append(trace.id)
        self._by_outcome[trace.outcome].append(trace.id)
        for tag in trace.tags:
            self._by_tag[tag].append(trace.id)
        return ok(None)

    async def append_event(self, event: Mapping[str, Any]) -> Result:
        self._events.append(event)
        if event.get("type") == "step.completed":
            return await self.append(event["trace"])
        return ok(None)

    async def get(self, trace_id: str) -> Result:
        trace = self._traces.get(trace_id)
        if trace is None:
            return err(
                make_loom_error(
                    "VALIDATION_FAILED",
                    "Trace not found",
                    retryable=False,
                    trace_id=trace_id,
                )
            )
        return ok(trace)

    async def query(self, query: Mapping[str, Any] | None = None) -> AsyncIterator[Trace]:
        query = query or {}
        yielded = 0
        limit = query.get("limit")
        for trace_id in self._candidate_ids(query):
            trace = self._traces.get(trace_id)
            if trace is None or not _matches_trace(trace, query):
                continue
            yield trace
            yielded += 1
            if limit is not None and yielded >= limit:
                return

    async def children(self, trace_id: str) -> AsyncIterator[Trace]:
        async for trace in self.query({"parent_trace_id": trace_id}):
            yield trace

    def events(self, trace_id: str | None = None) -> tuple[Mapping[str, Any], ...]:
        if trace_id is None:
            return tuple(self._events)
        return tuple(event for event in self._events if _event_trace_id(event) == trace_id)

    def _candidate_ids(self, query: Mapping[str, Any]) -> list[str]:
        groups: list[list[str]] = []
        if "run_id" in query:
            groups.append(self._by_run_id.get(query["run_id"], []))
        if "loop_id" in query:
            groups.append(self._by_loop_id.get(query["loop_id"], []))
        if "root_trace_id" in query:
            groups.append(self._by_root_trace_id.get(query["root_trace_id"], []))
        if "parent_trace_id" in query:
            groups.append(self._by_parent_trace_id.get(query["parent_trace_id"], []))
        if "outcome" in query:
            outcomes = query["outcome"]
            groups.extend(self._by_outcome.get(outcome, []) for outcome in outcomes)
        if "tags" in query:
            tags = query["tags"]
            groups.extend(self._by_tag.get(tag, []) for tag in tags)

        if not groups:
            return list(self._order)

        first, *rest = groups
        rest_sets = [set(group) for group in rest]
        return [trace_id for trace_id in first if all(trace_id in group for group in rest_sets)]


@dataclass(frozen=True, slots=True)
class TraceSink:
    store: InMemoryTraceStore

    async def emit(self, event: Mapping[str, Any]) -> Result:
        return await self.store.append_event(event)


def create_in_memory_trace_sink(store: InMemoryTraceStore) -> TraceSink:
    return TraceSink(store)


@dataclass(frozen=True, slots=True)
class DefaultTraceReader:
    store: InMemoryTraceStore

    async def query(self, query: Mapping[str, Any] | None = None) -> AsyncIterator[Trace]:
        async for trace in self.store.query(query):
            yield trace

    async def get(self, trace_id: str) -> Result:
        return await self.store.get(trace_id)

    async def tree(self, root_trace_id: str, max_depth: int | None = None) -> dict[str, Any]:
        root_result = await self.store.get(root_trace_id)
        if not root_result.ok:
            return {"trace": None, "children": ()}
        return await _tree_from_store(self.store, root_result.value, 0, max_depth)

    async def path(self, trace_id: str) -> tuple[Trace, ...]:
        current = (await self.store.get(trace_id)).value if (await self.store.get(trace_id)).ok else None
        traces: list[Trace] = []
        while current is not None:
            traces.append(current)
            if current.parent_trace_id is None:
                break
            parent_result = await self.store.get(current.parent_trace_id)
            current = parent_result.value if parent_result.ok else None
        return tuple(reversed(traces))

    async def summarize(self, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
        traces = [trace async for trace in self.store.query(query)]
        by_outcome: dict[str, int] = defaultdict(int)
        total_duration = 0
        errors: list[dict[str, Any]] = []
        for trace in traces:
            by_outcome[trace.outcome] += 1
            total_duration += trace.duration_ms
            if trace.error is not None:
                errors.append({"code": trace.error.code, "message": trace.error.message})
        return {
            "count": len(traces),
            "by_outcome": dict(by_outcome),
            "average_duration_ms": total_duration / len(traces) if traces else 0,
            "errors": errors,
        }


async def _tree_from_store(store: InMemoryTraceStore, trace: Trace, depth: int, max_depth: int | None) -> dict[str, Any]:
    if max_depth is not None and depth >= max_depth:
        return {"trace": trace, "children": ()}
    children = [await _tree_from_store(store, child, depth + 1, max_depth) async for child in store.children(trace.id)]
    return {"trace": trace, "children": tuple(children)}


def _matches_trace(trace: Trace, query: Mapping[str, Any]) -> bool:
    if "run_id" in query and trace.run_id != query["run_id"]:
        return False
    if "loop_id" in query and trace.loop_id != query["loop_id"]:
        return False
    if "root_trace_id" in query and trace.root_trace_id != query["root_trace_id"]:
        return False
    if "parent_trace_id" in query and trace.parent_trace_id != query["parent_trace_id"]:
        return False
    if "outcome" in query and trace.outcome not in query["outcome"]:
        return False
    if "tags" in query and not all(tag in trace.tags for tag in query["tags"]):
        return False
    if "metadata" in query:
        metadata = trace.metadata or {}
        for key, value in query["metadata"].items():
            if metadata.get(key) != value:
                return False
    return True


def _event_trace_id(event: Mapping[str, Any]) -> str | None:
    if event.get("type") == "step.completed":
        return event["trace"].id
    return event.get("trace_id")


class JsonlTraceStore(InMemoryTraceStore):
    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("type") == "trace":
                    trace = _trace_from_dict(record["payload"])
                    self._traces[trace.id] = trace
                    self._order.append(trace.id)
                    self._by_run_id[trace.run_id].append(trace.id)
                    self._by_loop_id[trace.loop_id].append(trace.id)
                    self._by_root_trace_id[trace.root_trace_id].append(trace.id)
                    if trace.parent_trace_id is not None:
                        self._by_parent_trace_id[trace.parent_trace_id].append(trace.id)
                    self._by_outcome[trace.outcome].append(trace.id)
                    for tag in trace.tags:
                        self._by_tag[tag].append(trace.id)

    async def append(self, trace: Trace) -> Result:
        result = await super().append(trace)
        if result.ok:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                payload = _trace_to_dict(trace)
                handle.write(
                    json.dumps(
                        {
                            "type": "trace",
                            "id": trace.id,
                            "runId": trace.run_id,
                            "payload": payload,
                            "hash": stable_json_hash(payload),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        return result


def _loom_error_to_dict(error: LoomError | None) -> dict[str, Any] | None:
    if error is None:
        return None
    return {"code": error.code, "message": error.message, "retryable": error.retryable}


@dataclass(frozen=True, slots=True)
class TraceSamplePolicy:
    include_full_on_start: bool = True
    include_full_on_end: bool = True
    include_full_on_failure: bool = True
    every_n_steps: int | None = None
    max_inline_snapshot_bytes: int = 4096


def default_trace_sample_policy() -> TraceSamplePolicy:
    return TraceSamplePolicy()


def stable_json_hash(value: Any) -> str:
    payload = json.dumps(_to_plain(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_trace_snapshot(context: Context, *, at: str, include_context: bool = True) -> TraceSnapshot:
    return TraceSnapshot(
        context_id=context.id,
        at=at,
        context=context if include_context else None,
        hash=stable_json_hash(context),
    )


def strip_trace_snapshots(trace: Trace) -> Trace:
    def strip(snapshot: TraceSnapshot | None) -> TraceSnapshot | None:
        if snapshot is None:
            return None
        return replace(snapshot, context=None)

    return replace(trace, input_snapshot=strip(trace.input_snapshot), output_snapshot=strip(trace.output_snapshot))


@dataclass(frozen=True, slots=True)
class TraceArchiveManifest:
    run_id: str
    root_trace_ids: tuple[str, ...]
    record_count: int
    chunks: tuple[Path, ...]
    chunk_hashes: tuple[str, ...]


async def archive_run(run_id: str, store: InMemoryTraceStore, archive_dir: str | Path) -> TraceArchiveManifest:
    archive_path = Path(archive_dir)
    archive_path.mkdir(parents=True, exist_ok=True)
    traces = [trace async for trace in store.query({"run_id": run_id})]
    chunk = archive_path / "traces.jsonl"
    with chunk.open("w", encoding="utf-8") as handle:
        for trace in traces:
            handle.write(json.dumps(_trace_to_dict(trace), sort_keys=True) + "\n")
    chunk_hash = _file_hash(chunk)
    return TraceArchiveManifest(
        run_id=run_id,
        root_trace_ids=tuple(trace.id for trace in traces if trace.parent_trace_id is None),
        record_count=len(traces),
        chunks=(chunk,),
        chunk_hashes=(chunk_hash,),
    )


def validate_archive_manifest(manifest: TraceArchiveManifest) -> Result:
    for path, expected_hash in zip(manifest.chunks, manifest.chunk_hashes, strict=True):
        if not path.exists():
            return err(make_loom_error("SERIALIZATION_FAILED", "Archive chunk missing", retryable=False))
        if _file_hash(path) != expected_hash:
            return err(make_loom_error("SERIALIZATION_FAILED", "Archive chunk hash mismatch", retryable=False))
    return ok(None)


def _trace_to_dict(trace: Trace) -> dict[str, Any]:
    return {
        "id": trace.id,
        "run_id": trace.run_id,
        "loop_id": trace.loop_id,
        "loop_version": trace.loop_version,
        "step_number": trace.step_number,
        "parent_trace_id": trace.parent_trace_id,
        "root_trace_id": trace.root_trace_id,
        "started_at": trace.started_at,
        "ended_at": trace.ended_at,
        "duration_ms": trace.duration_ms,
        "input_context_id": trace.input_context_id,
        "output_context_id": trace.output_context_id,
        "outcome": trace.outcome,
        "error": _loom_error_to_dict(trace.error),
        "tags": list(trace.tags),
        "metadata": _to_plain(trace.metadata),
    }


def _trace_from_dict(data: Mapping[str, Any]) -> Trace:
    error = data.get("error")
    return Trace(
        id=data["id"],
        run_id=data["run_id"],
        loop_id=data["loop_id"],
        loop_version=data["loop_version"],
        step_number=data["step_number"],
        parent_trace_id=data.get("parent_trace_id"),
        root_trace_id=data["root_trace_id"],
        started_at=data["started_at"],
        ended_at=data["ended_at"],
        duration_ms=data["duration_ms"],
        input_context_id=data["input_context_id"],
        output_context_id=data["output_context_id"],
        outcome=data["outcome"],
        error=(LoomError(error["code"], error["message"], error["retryable"]) if error is not None else None),
        tags=tuple(data.get("tags") or ()),
        metadata=data.get("metadata"),
    )


def _to_plain(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return {key: _to_plain(item) for key, item in value.items()}
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_to_plain(item) for item in value]
    if is_dataclass(value):
        return {field.name: _to_plain(getattr(value, field.name)) for field in fields(value)}
    return value


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "DefaultTraceReader",
    "InMemoryTraceStore",
    "JsonlTraceStore",
    "TraceSink",
    "TraceArchiveManifest",
    "TraceSamplePolicy",
    "archive_run",
    "create_in_memory_trace_sink",
    "default_trace_sample_policy",
    "make_trace_snapshot",
    "stable_json_hash",
    "strip_trace_snapshots",
    "validate_archive_manifest",
]
