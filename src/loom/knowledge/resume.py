"""Durable website snapshots and document-boundary LightRAG checkpoints."""

import hashlib
import json
import os
import shutil
import time

from loom.service.contracts import ServiceError, canonical, new_id

USAGE_KEYS = ("llm_calls", "reported_tokens", "embedding_inputs")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def index_snapshot(store, base, directory, workspace, key, documents, delete_ids, all_ids, call, *, progress, cancelled):
    """Only checkpoint finalized subprocess output; never reopen a half-written graph."""
    root = directory / key
    manifest = root / "index.json"
    identity = hashlib.sha256(canonical({
        "previous": base.get("lightrag_generation"), "model": base["model_fingerprint"],
        "dimension": base["embedding_dimension"], "profile": base["embedding_profile_id"],
        "documents": [(d["id"], d["digest"]) for d in documents], "delete_ids": delete_ids, "all_ids": all_ids,
    }).encode()).hexdigest()
    state = json.loads(manifest.read_text()) if manifest.exists() else {}
    if state.get("identity") != identity:
        shutil.rmtree(root, ignore_errors=True)
        state = {"identity": identity, "done": 0, "usage": dict.fromkeys(USAGE_KEYS, 0)}
    root.mkdir(parents=True, exist_ok=True)
    # A crash can leave an uncommitted working copy, but the manifest still points to the last safe copy.
    for child in root.glob("batch_*"):
        if child.name != state.get("workspace"):
            shutil.rmtree(child)
    total = len(documents)
    if state.get("workspace"):
        progress(stage="resuming_index", indexed=state["done"], total=total, checkpointed=state["done"],
                 resumed_documents=state["done"], **state["usage"])
    started = time.monotonic()
    start_calls = state["usage"]["llm_calls"]
    while not state.get("finished"):
        if cancelled():
            raise ServiceError("Knowledge job cancelled; completed document checkpoints retained", 409)
        if time.monotonic() - started >= 1800 or state["usage"]["llm_calls"] - start_calls >= 500:
            raise ServiceError("LightRAG sync budget reached; completed document checkpoints retained. Resume sync to continue", 504)
        done = state["done"]
        batch = documents[done:done + 1]
        working = root / new_id("batch")
        shutil.copytree(root / state["workspace"] if state.get("workspace") else workspace, working)
        baseline = dict(state["usage"])

        consumed = dict(baseline)

        def update(baseline=baseline, done=done, consumed=consumed, **value):
            # Worker counters reset per process; expose snapshot-wide usage and only durable progress.
            value.update({k: baseline[k] + value[k] for k in USAGE_KEYS if k in value})
            consumed.update({k: value[k] for k in USAGE_KEYS if k in value})
            value.update(indexed=done, total=total, checkpointed=done)
            progress(**value)

        try:
            remaining = {d["id"] for d in documents[done + len(batch):]}
            result = call(store, base, working, "index", documents=batch, delete_ids=delete_ids if done == 0 else [],
                          all_ids=sorted(set(all_ids) - remaining), progress=update, cancelled=cancelled)
            previous = state.get("workspace")
            state = {**state, "done": done + len(batch), "workspace": working.name, "result": result,
                     "usage": {k: baseline[k] + result.get("usage", {}).get(k, 0) for k in USAGE_KEYS},
                     "finished": done + len(batch) == total}
            save(manifest, state)
            if previous:
                shutil.rmtree(root / previous, ignore_errors=True)
            progress(stage="checkpointed", indexed=state["done"], total=total, checkpointed=state["done"], **state["usage"])
        except BaseException as exc:
            # Keep the manifest's last successful copy even if a progress callback fails after commit.
            committed = json.loads(manifest.read_text()) if manifest.exists() else {}
            if committed.get("workspace") != working.name:
                shutil.rmtree(working, ignore_errors=True)
                state["usage"] = consumed
                save(manifest, state)
            if isinstance(exc, ServiceError):
                message = f"{exc}; saved crawl and {state['done']} document checkpoints retained. Resume sync to continue"
                raise ServiceError(message, exc.status, exc.code) from exc
            raise
    shutil.rmtree(workspace)
    shutil.copytree(root / state["workspace"], workspace)
    return {**state["result"], "usage": state["usage"]}
