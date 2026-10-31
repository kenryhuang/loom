"""Host-only model selection and isolated file handlers for DAG lanes."""

import hashlib
import tempfile
from pathlib import Path

from loom.core import Observation, err, make_loom_error, new_trace_id, now_iso, ok
from loom.runtime.plugin_contracts import config_digest
from loom.session_environments.branches import FileBranch


def provider_for(policy, node, lane):
    name = node.get("model")
    provider = policy.provider
    identity = dict(model=name or provider.model)
    if name:
        if name in policy.lane_providers:
            provider = policy.lane_providers[name]
            identity["config"] = config_digest(dict(model=provider.model, host_config=name))
        else:
            from loom.tasks.config import load_task_config
            from loom.tasks.runner import _create_provider

            path = policy.assembly.request.metadata.get("knowledge_config_path")
            if not path:
                raise ValueError("Named branch model requires a configured model catalog")
            config = load_task_config(path).unwrap()
            if name not in config.models:
                raise ValueError("Unknown branch model")
            identity["config"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            provider = _create_provider(config, model_name=name).unwrap()
            binding = policy.assembly.request.metadata.get("autonomy")
            if binding:
                from loom.autonomy.budget import BudgetLedger, BudgetProvider

                provider = BudgetProvider(
                    provider, BudgetLedger(policy.assembly.request.metadata["memory_database"]), binding, output_tokens=policy.parallel["max_output_tokens"]
                )
    if lane.get("model_identity") and lane["model_identity"] != identity:
        raise ValueError("Branch model configuration changed since checkpoint")
    lane["model_identity"] = identity
    return provider


def file_branch(policy, node, lane):
    if not node.get("isolated_write"):
        return None
    if not policy.assembly.report_workspace:
        raise ValueError("Isolated writes require a writable workspace capability")
    if lane.get("branch_directory"):
        return FileBranch(lane["branch_directory"])
    database = policy.assembly.request.metadata.get("memory_database")
    if database:
        root = Path(database).parent / "branches"
        root.mkdir(exist_ok=True)
    else:
        root = Path(tempfile.mkdtemp(prefix="loom-branches-"))
    directory = root / hashlib.sha256((lane["id"] + ":" + node["id"]).encode()).hexdigest()[:32]
    branch = FileBranch.create(policy.assembly.tool_request.workspace, directory, identity=dict(lane_id=lane["id"], node_id=node["id"]))
    lane["branch_directory"] = str(directory)
    return branch


def handlers_for(branch):
    async def read(value, _options=None):
        try:
            content = branch.read(value["path"])
            result = dict(path=value["path"], content=content, bytes_read=len(content.encode()))
            return ok(Observation(new_trace_id(), "read_file", result, now_iso()))
        except (OSError, ValueError, KeyError) as exc:
            return err(make_loom_error("branch.read_failed", str(exc)))

    async def write(value, _options=None):
        try:
            branch.write(value["path"], value["content"])
            return ok(Observation(new_trace_id(), "write_file", dict(path=value["path"], written=True), now_iso()))
        except (OSError, ValueError, KeyError) as exc:
            return err(make_loom_error("branch.write_failed", str(exc)))

    return dict(read_file=read, write_file=write)
