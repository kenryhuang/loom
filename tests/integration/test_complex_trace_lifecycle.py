"""
Complex project-level test: Trace archive, restore, and cross-run query.

Tests the full observability lifecycle:
1. Run multiple loops producing traces across different runs
2. Query traces with complex filters (tags, outcome, metadata, run_id)
3. Archive a run to JSONL with hash verification
4. Restore traces from JSONL file via JsonlTraceStore
5. Build trace trees with parent-child relationships
6. Summarize traces across multiple runs
7. Verify trace snapshot creation and stripping

Exercises: observability, runtime, core models.
"""

from __future__ import annotations

import pytest

from loom.core import (
    Context,
    GoalLayer,
    IdentityLayer,
    Observation,
    Trace,
    TraceSnapshot,
    as_step_number,
    empty_affordances,
    empty_knowledge,
    empty_state,
    freeze_context,
    new_context_id,
    new_loop_id,
    new_run_id,
    new_trace_id,
    now_iso,
)
from loom.observability import (
    DefaultTraceReader,
    InMemoryTraceStore,
    JsonlTraceStore,
    archive_run,
    make_trace_snapshot,
    stable_json_hash,
    strip_trace_snapshots,
    validate_archive_manifest,
)


def _make_trace(
    *,
    run_id: str,
    loop_id: str,
    step_number: int = 0,
    outcome: str = "pass",
    tags: tuple[str, ...] = (),
    metadata: dict | None = None,
    parent_trace_id: str | None = None,
    root_trace_id: str | None = None,
    observations: tuple[Observation, ...] = (),
):
    trace_id = new_trace_id()
    return Trace(
        id=trace_id,
        run_id=run_id,
        loop_id=loop_id,
        loop_version="v1",
        step_number=as_step_number(step_number),
        root_trace_id=root_trace_id or trace_id,
        parent_trace_id=parent_trace_id,
        started_at=now_iso(),
        ended_at=now_iso(),
        duration_ms=step_number * 10,
        input_context_id="ctx_in",
        output_context_id="ctx_out",
        outcome=outcome,
        observations=observations,
        tags=tags,
        metadata=metadata,
    )


class TestMultiRunTraceQuery:
    """Test querying traces across multiple runs with complex filters."""

    @pytest.mark.asyncio
    async def test_query_by_multiple_filters(self):
        store = InMemoryTraceStore()
        run_id_a = new_run_id()
        run_id_b = new_run_id()
        loop_id = new_loop_id()

        # Run A: 3 traces (2 pass, 1 fail) with tags
        traces_a = [
            _make_trace(run_id=run_id_a, loop_id=loop_id, step_number=0, outcome="pass", tags=("llm", "tool")),
            _make_trace(run_id=run_id_a, loop_id=loop_id, step_number=1, outcome="pass", tags=("llm",)),
            _make_trace(run_id=run_id_a, loop_id=loop_id, step_number=2, outcome="fail", tags=("tool",)),
        ]
        # Run B: 2 traces (both pass) with different tags
        traces_b = [
            _make_trace(run_id=run_id_b, loop_id=loop_id, step_number=0, outcome="pass", tags=("example",)),
            _make_trace(run_id=run_id_b, loop_id=loop_id, step_number=1, outcome="pass", tags=("example", "llm")),
        ]

        for trace in traces_a + traces_b:
            await store.append(trace)

        reader = DefaultTraceReader(store)

        # Query: run A, only pass
        results = [t async for t in reader.query({"run_id": run_id_a})]
        assert len(results) == 3

        # Query: run A, fail outcome
        results = [t async for t in reader.query({"run_id": run_id_a, "outcome": ["fail"]})]
        assert len(results) == 1
        assert results[0].step_number == 2

        # Query: tag "llm" across all runs
        # Run A: traces 0 (llm,tool) and 1 (llm) = 2
        # Run B: trace 1 (example,llm) = 1
        # Total = 3
        results = [t async for t in reader.query({"tags": ("llm",)})]
        assert len(results) == 3

        # Query: run B, tag "example", pass
        results = [
            t
            async for t in reader.query(
                {
                    "run_id": run_id_b,
                    "tags": ("example",),
                    "outcome": ["pass"],
                }
            )
        ]
        assert len(results) == 2

        # Query with limit
        results = [t async for t in reader.query({"tags": ("llm",), "limit": 2})]
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_query_by_metadata(self):
        store = InMemoryTraceStore()
        run_id = new_run_id()
        loop_id = new_loop_id()

        traces = [
            _make_trace(run_id=run_id, loop_id=loop_id, metadata={"model": "gpt-4", "region": "us"}),
            _make_trace(run_id=run_id, loop_id=loop_id, metadata={"model": "gpt-4", "region": "eu"}),
            _make_trace(run_id=run_id, loop_id=loop_id, metadata={"model": "claude", "region": "us"}),
        ]
        for trace in traces:
            await store.append(trace)

        reader = DefaultTraceReader(store)

        # Query by model metadata
        results = [t async for t in reader.query({"metadata": {"model": "gpt-4"}})]
        assert len(results) == 2

        # Query by combined metadata
        results = [t async for t in reader.query({"metadata": {"model": "gpt-4", "region": "eu"}})]
        assert len(results) == 1


class TestTraceTree:
    """Test trace tree building with parent-child relationships."""

    @pytest.mark.asyncio
    async def test_trace_tree_three_levels(self):
        store = InMemoryTraceStore()
        run_id = new_run_id()
        loop_id = new_loop_id()

        root = _make_trace(run_id=run_id, loop_id=loop_id)
        await store.append(root)

        child_1 = _make_trace(run_id=run_id, loop_id=loop_id, parent_trace_id=root.id, root_trace_id=root.id)
        child_2 = _make_trace(run_id=run_id, loop_id=loop_id, parent_trace_id=root.id, root_trace_id=root.id)
        await store.append(child_1)
        await store.append(child_2)

        grandchild = _make_trace(run_id=run_id, loop_id=loop_id, parent_trace_id=child_1.id, root_trace_id=root.id)
        await store.append(grandchild)

        reader = DefaultTraceReader(store)
        tree = await reader.tree(root.id)

        assert tree["trace"].id == root.id
        assert len(tree["children"]) == 2

        # Find child_1's subtree
        child_1_node = next(c for c in tree["children"] if c["trace"].id == child_1.id)
        assert len(child_1_node["children"]) == 1
        assert child_1_node["children"][0]["trace"].id == grandchild.id

        # child_2 has no children
        child_2_node = next(c for c in tree["children"] if c["trace"].id == child_2.id)
        assert len(child_2_node["children"]) == 0

    @pytest.mark.asyncio
    async def test_trace_tree_max_depth(self):
        store = InMemoryTraceStore()
        run_id = new_run_id()
        loop_id = new_loop_id()

        root = _make_trace(run_id=run_id, loop_id=loop_id)
        child = _make_trace(run_id=run_id, loop_id=loop_id, parent_trace_id=root.id, root_trace_id=root.id)
        grandchild = _make_trace(run_id=run_id, loop_id=loop_id, parent_trace_id=child.id, root_trace_id=root.id)
        for t in (root, child, grandchild):
            await store.append(t)

        reader = DefaultTraceReader(store)
        tree = await reader.tree(root.id, max_depth=1)

        assert tree["trace"].id == root.id
        assert len(tree["children"]) == 1
        # max_depth=1 means child's children are not expanded
        assert len(tree["children"][0]["children"]) == 0

    @pytest.mark.asyncio
    async def test_trace_path_to_root(self):
        store = InMemoryTraceStore()
        run_id = new_run_id()
        loop_id = new_loop_id()

        root = _make_trace(run_id=run_id, loop_id=loop_id)
        child = _make_trace(run_id=run_id, loop_id=loop_id, parent_trace_id=root.id, root_trace_id=root.id)
        leaf = _make_trace(run_id=run_id, loop_id=loop_id, parent_trace_id=child.id, root_trace_id=root.id)
        for t in (root, child, leaf):
            await store.append(t)

        reader = DefaultTraceReader(store)
        path = await reader.path(leaf.id)

        assert len(path) == 3
        assert path[0].id == root.id
        assert path[1].id == child.id
        assert path[2].id == leaf.id


class TestTraceArchiveAndRestore:
    """Test archiving runs to JSONL and restoring from file."""

    @pytest.mark.asyncio
    async def test_archive_and_validate_manifest(self, tmp_path):
        store = InMemoryTraceStore()
        run_id = new_run_id()
        loop_id = new_loop_id()

        traces = [_make_trace(run_id=run_id, loop_id=loop_id, step_number=i, outcome="pass", tags=("archive-test",)) for i in range(5)]
        for trace in traces:
            await store.append(trace)

        manifest = await archive_run(run_id, store, tmp_path / "archive")

        assert manifest.run_id == run_id
        assert manifest.record_count == 5
        assert len(manifest.chunks) == 1
        assert len(manifest.chunk_hashes) == 1

        # Validate manifest integrity
        validation = validate_archive_manifest(manifest)
        assert validation.ok

    @pytest.mark.asyncio
    async def test_restore_from_jsonl_file(self, tmp_path):
        """Write traces to JSONL, then restore via JsonlTraceStore."""
        jsonl_path = tmp_path / "traces.jsonl"
        run_id = new_run_id()
        loop_id = new_loop_id()

        # Write traces to first store
        store1 = JsonlTraceStore(jsonl_path)
        traces = [_make_trace(run_id=run_id, loop_id=loop_id, step_number=i, outcome="pass") for i in range(3)]
        for trace in traces:
            await store1.append(trace)

        # Restore from file into a new store instance
        store2 = JsonlTraceStore(jsonl_path)
        reader = DefaultTraceReader(store2)
        restored = [t async for t in reader.query({"run_id": run_id})]

        assert len(restored) == 3
        restored_ids = {t.id for t in restored}
        original_ids = {t.id for t in traces}
        assert restored_ids == original_ids

    @pytest.mark.asyncio
    async def test_archive_tamper_detection(self, tmp_path):
        """Tampering with archive file should fail validation."""
        store = InMemoryTraceStore()
        run_id = new_run_id()
        loop_id = new_loop_id()

        trace = _make_trace(run_id=run_id, loop_id=loop_id)
        await store.append(trace)

        manifest = await archive_run(run_id, store, tmp_path / "archive")

        # Tamper with the archive file
        archive_file = manifest.chunks[0]
        content = archive_file.read_text()
        archive_file.write_text(content.replace("pass", "tampered"))

        validation = validate_archive_manifest(manifest)
        assert not validation.ok
        assert "hash mismatch" in validation.error.message.lower()


class TestTraceSummarize:
    """Test trace summarization across runs."""

    @pytest.mark.asyncio
    async def test_summarize_multi_run(self):
        store = InMemoryTraceStore()
        loop_id = new_loop_id()

        run_a = new_run_id()
        run_b = new_run_id()

        for i in range(3):
            await store.append(_make_trace(run_id=run_a, loop_id=loop_id, step_number=i, outcome="pass"))
        for i in range(2):
            await store.append(_make_trace(run_id=run_b, loop_id=loop_id, step_number=i, outcome="fail"))

        reader = DefaultTraceReader(store)

        # Summarize all
        summary = await reader.summarize({})
        assert summary["count"] == 5
        assert summary["by_outcome"]["pass"] == 3
        assert summary["by_outcome"]["fail"] == 2

        # Summarize run A only
        summary_a = await reader.summarize({"run_id": run_a})
        assert summary_a["count"] == 3
        assert summary_a["by_outcome"] == {"pass": 3}

        # Summarize run B only
        summary_b = await reader.summarize({"run_id": run_b})
        assert summary_b["count"] == 2
        assert summary_b["by_outcome"] == {"fail": 2}


class TestTraceSnapshot:
    """Test trace snapshot creation and stripping."""

    def test_snapshot_preserves_context(self):
        ctx = freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(role="test"),
                goal=GoalLayer(objective="test"),
                state=empty_state(),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(),
            )
        )
        snapshot = make_trace_snapshot(ctx, at=now_iso(), include_context=True)
        assert snapshot.context_id == ctx.id
        assert snapshot.context is not None
        assert snapshot.hash is not None

    def test_snapshot_without_context(self):
        ctx = freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(role="test"),
                goal=GoalLayer(objective="test"),
                state=empty_state(),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(),
            )
        )
        snapshot = make_trace_snapshot(ctx, at=now_iso(), include_context=False)
        assert snapshot.context_id == ctx.id
        assert snapshot.context is None

    def test_strip_snapshots_from_trace(self):
        ctx = freeze_context(
            Context(
                id=new_context_id(),
                run_id=new_run_id(),
                created_at=now_iso(),
                identity=IdentityLayer(role="test"),
                goal=GoalLayer(objective="test"),
                state=empty_state(),
                knowledge=empty_knowledge(),
                affordances=empty_affordances(),
            )
        )
        trace_id = new_trace_id()
        trace = Trace(
            id=trace_id,
            run_id=new_run_id(),
            loop_id=new_loop_id(),
            loop_version="v1",
            step_number=0,
            root_trace_id=trace_id,
            started_at=now_iso(),
            ended_at=now_iso(),
            duration_ms=0,
            input_context_id=ctx.id,
            output_context_id=ctx.id,
            outcome="pass",
            input_snapshot=TraceSnapshot(context_id=ctx.id, at=now_iso(), context=ctx, hash="abc"),
            output_snapshot=TraceSnapshot(context_id=ctx.id, at=now_iso(), context=ctx, hash="def"),
        )

        stripped = strip_trace_snapshots(trace)
        assert stripped.input_snapshot is not None
        assert stripped.input_snapshot.context is None
        assert stripped.output_snapshot is not None
        assert stripped.output_snapshot.context is None


class TestStableJsonHash:
    """Test stable JSON hashing for trace integrity."""

    def test_deterministic_hash(self):
        data = {"key": "value", "nested": {"a": 1, "b": [1, 2, 3]}}
        hash1 = stable_json_hash(data)
        hash2 = stable_json_hash(data)
        assert hash1 == hash2

    def test_key_order_independent(self):
        data1 = {"a": 1, "b": 2}
        data2 = {"b": 2, "a": 1}
        assert stable_json_hash(data1) == stable_json_hash(data2)

    def test_different_content_different_hash(self):
        data1 = {"key": "value1"}
        data2 = {"key": "value2"}
        assert stable_json_hash(data1) != stable_json_hash(data2)


class TestTraceStoreDuplicateDetection:
    """Test that trace store rejects duplicate trace IDs."""

    @pytest.mark.asyncio
    async def test_duplicate_trace_rejected(self):
        store = InMemoryTraceStore()
        trace = _make_trace(run_id=new_run_id(), loop_id=new_loop_id())

        result1 = await store.append(trace)
        assert result1.ok

        result2 = await store.append(trace)
        assert not result2.ok
        assert result2.error.code == "VALIDATION_FAILED"
        assert "already exists" in result2.error.message.lower()
