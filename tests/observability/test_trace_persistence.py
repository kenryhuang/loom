import asyncio
from dataclasses import replace

from loom.core import (
    Trace,
    as_step_number,
    new_loop_id,
    new_loop_version,
    new_run_id,
    new_trace_id,
)
from loom.examples import make_initial_counter_context
from loom.observability import (
    DefaultTraceReader,
    JsonlTraceStore,
    archive_run,
    default_trace_sample_policy,
    make_trace_snapshot,
    stable_json_hash,
    strip_trace_snapshots,
    validate_archive_manifest,
)

NOW = "2026-06-04T00:00:00.000Z"


def test_trace_tree_path_summary_jsonl_sampling_and_archive(tmp_path):
    async def scenario():
        path = tmp_path / "traces.jsonl"
        store = JsonlTraceStore(path)
        run_id = new_run_id()
        parent = make_trace(run_id=run_id, tags=("root",), metadata={"forkIndex": 0})
        child = make_trace(
            run_id=run_id,
            parent_trace_id=parent.id,
            root_trace_id=parent.id,
            outcome="fail",
            tags=("child",),
            metadata={"gap": "missing context", "forkIndex": 1},
        )
        await store.append(parent)
        await store.append(child)

        reopened = JsonlTraceStore(path)
        assert (await reopened.get(parent.id)).value.id == parent.id
        assert [trace.id async for trace in reopened.query({"metadata": {"forkIndex": 1}})] == [child.id]

        reader = DefaultTraceReader(reopened)
        tree = await reader.tree(parent.id)
        assert tree["trace"].id == parent.id
        assert tree["children"][0]["trace"].id == child.id
        assert [trace.id for trace in await reader.path(child.id)] == [parent.id, child.id]
        summary = await reader.summarize({"root_trace_id": parent.id})
        assert summary["by_outcome"]["fail"] == 1

        snapshot = make_trace_snapshot(make_initial_counter_context(1), at=NOW)
        assert snapshot.hash == stable_json_hash(snapshot.context)
        with_snapshot = replace(parent, input_snapshot=snapshot)
        stripped = strip_trace_snapshots(with_snapshot)
        assert stripped.input_snapshot.context is None

        policy = default_trace_sample_policy()
        assert policy.include_full_on_failure is True

        manifest = await archive_run(run_id, reopened, tmp_path / "archive")
        assert manifest.record_count == 2
        assert validate_archive_manifest(manifest).ok

    asyncio.run(scenario())


def make_trace(*, run_id, parent_trace_id=None, root_trace_id=None, outcome="pass", tags=(), metadata=None):
    trace_id = new_trace_id()
    return Trace(
        id=trace_id,
        run_id=run_id,
        loop_id=new_loop_id(),
        loop_version=new_loop_version(),
        step_number=as_step_number(0),
        parent_trace_id=parent_trace_id,
        root_trace_id=root_trace_id or trace_id,
        started_at=NOW,
        ended_at=NOW,
        duration_ms=1,
        input_context_id="ctx-in",
        output_context_id="ctx-out",
        outcome=outcome,
        tags=tags,
        metadata=metadata,
    )
