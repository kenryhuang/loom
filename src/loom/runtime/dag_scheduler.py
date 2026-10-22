"""Opt-in DAG contracts. Readiness is not permission to fork a context."""

from loom.runtime.plugin_contracts import json_value
from loom.runtime.workflow import WorkflowController

FIELDS = {"inputs", "outputs", "execution_hint", "work_group_hint", "independence_reason", "stable_inputs", "context_independent"}


class DagController(WorkflowController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.results = {}

    def propose(self, proposal):
        # Upsert future contracts. The host retains history, so the model need
        # not reproduce every completed node byte-for-byte in a small prompt.
        if not isinstance(proposal, dict) or not isinstance(proposal.get("nodes"), list):
            raise ValueError("DAG revision requires a list of node updates")
        rows = proposal["nodes"]
        if any(not isinstance(n, dict) or not isinstance(n.get("id"), str) for n in rows):
            raise ValueError("DAG updates need node IDs")
        if len({n["id"] for n in rows}) != len(rows):
            raise ValueError("Duplicate DAG update ID")
        updates = {n["id"]: n for n in rows}
        nodes = [{**old, **updates.pop(old["id"], {})} for old in self.nodes]
        nodes.extend(updates.values())
        return super().propose({**proposal, "nodes": nodes})

    def validate(self, nodes):
        if not isinstance(nodes, list) or any(not isinstance(n, dict) for n in nodes):
            raise ValueError("DAG nodes must be objects")
        extras = [{k: v for k, v in n.items() if k in FIELDS} for n in nodes]
        statuses = [n.get("status", "pending") for n in nodes]
        base = [{k: v for k, v in n.items() if k not in FIELDS} for n in nodes]
        for n in base:
            if n.get("status") == "running":
                n["status"] = "pending"
        checked = super().validate(base)
        for n, extra, status in zip(checked, extras, statuses, strict=True):
            n.update(json_value(extra), status=status)
            if n["executor_kind"] == "loop":
                raise ValueError("DAG uses persistent lanes, not child-loop executors")
            n.setdefault("execution_hint", "reuse_preferred")
            if n["execution_hint"] not in {"reuse_preferred", "parallel_candidate"}:
                raise ValueError("Unknown DAG execution hint")
            if type(n.get("context_independent", False)) is not bool:
                raise ValueError("context_independent must be boolean")
            for field in ("work_group_hint", "independence_reason"):
                if field in n and (not isinstance(n[field], str) or len(n[field]) > 2000):
                    raise ValueError(f"Invalid {field}")
            stable = n.get("stable_inputs", {})
            if not isinstance(stable, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in stable.items()):
                raise ValueError("stable_inputs must map input names to self-contained text")
            for field in ("inputs", "outputs"):
                n.setdefault(field, [])
                if not isinstance(n[field], list) or any(not isinstance(v, dict) for v in n[field]):
                    raise ValueError(f"DAG {field} must be a list of objects")
            names = [o.get("name") for o in n["outputs"]]
            if any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
                raise ValueError("Output names must be nonempty and unique")
            if any(set(o) - {"name", "kind"} for o in n["outputs"]):
                raise ValueError("Output declarations only contain name and kind")
        by_id = {n["id"]: n for n in checked}
        for n in checked:
            for item in n["inputs"]:
                if (set(item) - {"from_node", "output_name", "required"} or type(item.get("required", True)) is not bool
                        or item.get("from_node") not in n["dependencies"]
                        or item.get("output_name") not in {o["name"] for o in by_id[item["from_node"]]["outputs"]}):
                    raise ValueError("Named input must refer to an explicit dependency's declared output")
        return checked

    def ready(self):
        succeeded = {n["id"] for n in self.nodes if n["status"] == "succeeded"}
        return [n for n in self.nodes if n["status"] == "running" or (
            n["status"] in {"pending", "ready"} and set(n["dependencies"]).issubset(succeeded)
            and all(not i.get("required", True) or i["output_name"] in self.results.get(i["from_node"], {}).get("outputs", {})
                    for i in n["inputs"]))]


def branch_admission(node, bindings):
    """Conservative host gate; empty deps and a model hint are insufficient."""
    if node.get("execution_hint") != "parallel_candidate" or node["executor_kind"] != "llm":
        return "Reuse main context"
    if not node.get("context_independent") or not node.get("independence_reason", "").strip() or not node.get("stable_inputs"):
        return "Independence and stable self-contained inputs are not established"
    allowed = node["executor_config"].get("allowed_tools")
    if not isinstance(allowed, list):
        return "A bounded explicit tool set is required"
    if any(t not in bindings or bindings[t].effect_kind != "read_only" or bindings[t].placement != "execution" for t in allowed):
        return "Only host-registered read-only execution tools can run in parallel"
    if not node["outputs"]:
        return "A named result is required for a single handoff"
    return None
