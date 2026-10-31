"""One host-owned restore intent and manifest for every worker attempt."""

from dataclasses import asdict, dataclass, replace

from loom.core import thaw_json
from loom.runtime.checkpoints import decode, encode
from loom.service.contracts import ServiceError
from loom.tasks.workspace_probe import digest


@dataclass(frozen=True)
class RestoreIntent:
    mode: str  # new_task, restart, checkpoint, same_task
    source_run_id: str | None
    task_episode_id: str | None
    manifest: dict | None = None
    schema_version: int = 1

    def snapshot(self):
        return asdict(self)


def prepare_restore(state, checkpoint, *, restarting=False):
    """Called inside the run replacement transaction, before losing the old run."""
    old = state.get("run")
    new_input = any(m["role"] == "user" and m["seq"] > state["input_cursor"] for m in state["messages"])
    if restarting or not old or old["state"] == "completed" or old["state"] == "stopped" and new_input:
        return RestoreIntent("restart" if restarting else "new_task", old["id"] if old else None, None)
    context = checkpoint["context"] if checkpoint else decode(state["context"]) if state.get("context") else None
    if context is None:
        return RestoreIntent("same_task", old["id"], old.get("task_episode_id"))
    if checkpoint:
        observations = {o.id: o for o in (*context.state.observations, *checkpoint.get("observations", []))}
        scratch = dict(context.state.scratch or {})
        if checkpoint.get("plugin_states"):
            scratch["execution_plugins"] = checkpoint["plugin_states"]
        elif checkpoint.get("planning"):
            scratch["restore_planning"] = checkpoint["planning"]
        context = replace(context, state=replace(context.state, observations=tuple(observations.values()), scratch=scratch))
    plugins = thaw_json((context.state.scratch or {}).get("execution_plugins", {}))
    manifest = {
        "schema_version": 1, "source_run_id": old["id"], "source_checkpoint": old.get("checkpoint"),
        "task_episode_id": old.get("task_episode_id"), "goal_digest": (plugins.get("acceptance") or {}).get("goal_digest"),
        "plugin_config_digest": plugins.get("config_digest"), "plugin_state_digest": digest(plugins),
        "context_digest": digest(encode(context)), "autonomy": state.get("autonomy"),
        "counters": old.get("counters"), "active_seconds": old.get("active_seconds", 0), "steps": old.get("steps", 0),
        "conversation_recovery": "reproject_committed_context; do not replay unexecuted model actions across runs",
        "journal_run_id": old["id"],
    }
    state["context"] = encode(context)
    mode = "checkpoint" if checkpoint and old["state"] not in {"failed", "stopped"} else "same_task"
    return RestoreIntent(mode, old["id"], old.get("task_episode_id"), manifest)


def validate_restore(state, context):
    intent = state["run"].get("restore_intent")
    if not intent or intent["mode"] not in {"same_task", "checkpoint"} or not intent.get("manifest"):
        return
    manifest = intent["manifest"]
    if manifest["task_episode_id"] and manifest["task_episode_id"] != state["run"].get("task_episode_id"):
        raise ServiceError("Restore task episode does not match the committed manifest", 409)
    if manifest["autonomy"] != state.get("autonomy"):
        raise ServiceError("Restore work package does not match the committed manifest", 409)
    if intent["mode"] == "same_task":
        plugins = thaw_json((context.state.scratch or {}).get("execution_plugins", {}))
        if digest(encode(context)) != manifest["context_digest"] or digest(plugins) != manifest["plugin_state_digest"]:
            raise ServiceError("Restore context and plugin snapshot do not match the committed manifest", 409)
