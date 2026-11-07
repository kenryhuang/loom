"""Strict append-only collection migration for a committed task checkpoint."""

from copy import deepcopy

from loom.runtime.plugin_contracts import config_digest
from loom.tasks.recovery import TOOL_COLLECTIONS


def extension(previous, current):
    """Return an explicit migration, or reject any change beyond builtin tools."""
    before, after = deepcopy(previous), deepcopy(current)
    old = before["tools"].pop("collections")
    new = after["tools"].pop("collections")
    additions = [c for c in new if c not in old]
    if (before != after or [c for c in new if c in old] != old
            or not additions or not set(additions) <= set(TOOL_COLLECTIONS.values())):
        raise ValueError("Tool extension must only append registered builtin collections; task and resources must stay unchanged")
    return {"source_digest": config_digest(previous), "target_digest": config_digest(current), "additions": additions}


def migrate(snapshot, current, change):
    """Preserve all committed state; only initialize the newly added collections."""
    if (snapshot.get("config_digest") != change["source_digest"]
            or current["config_digest"] != change["target_digest"]):
        raise ValueError("Tool extension does not match checkpoint configuration")
    additions = set(change["additions"])
    fixed = [m for m in current["manifests"] if not (m["kind"] == "tools" and m["plugin_id"] in additions)]
    if snapshot.get("manifests") != fixed or set(current["tools"]) - set(snapshot["tools"]) != additions:
        raise ValueError("Tool extension changed existing plugin manifests")
    snapshot.update(config_digest=current["config_digest"], manifests=current["manifests"])
    snapshot["tools"].update({key: current["tools"][key] for key in additions})
    return snapshot
