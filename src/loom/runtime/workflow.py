"""Validated workflow revisions; policies submit proposals to this controller."""

from __future__ import annotations

from copy import deepcopy

from loom.runtime.plugin_contracts import json_value

TERMINAL = frozenset({"succeeded", "skipped", "superseded"})
STATUSES = TERMINAL | {"pending", "ready", "running", "blocked", "failed"}


class WorkflowController:
    def __init__(self, *, capabilities=(), resources=(), tools=(), loops=(), max_nodes=30, max_revisions=20, max_depth=10):
        self.capabilities, self.resources = set(capabilities), set(resources)
        self.tools, self.loops = set(tools), set(loops)
        self.max_nodes, self.max_revisions, self.max_depth = max_nodes, max_revisions, max_depth
        self.revision = 0
        self.nodes = []

    def validate(self, nodes):
        if not isinstance(nodes, list) or not nodes or len(nodes) > self.max_nodes:
            raise ValueError("Workflow must contain a bounded nonempty list of nodes")
        if any(not isinstance(node, dict) for node in nodes):
            raise ValueError("Workflow nodes must be objects")
        nodes = json_value(nodes)
        ids = [node.get("id") for node in nodes]
        if any(not isinstance(nid, str) or not nid for nid in ids) or len(set(ids)) != len(ids):
            raise ValueError("Workflow node IDs must be nonempty and unique")
        by_id = {node["id"]: node for node in nodes}
        depths = {}
        visiting = set()

        def depth(nid):
            if nid in visiting:
                raise ValueError("Workflow dependencies contain a cycle")
            if nid in depths:
                return depths[nid]
            visiting.add(nid)
            dependencies = by_id[nid].get("dependencies", [])
            if not isinstance(dependencies, list) or any(dep not in by_id for dep in dependencies):
                raise ValueError("Workflow dependency does not exist")
            value = 1 + max((depth(dep) for dep in dependencies), default=0)
            visiting.remove(nid)
            if value > self.max_depth:
                raise ValueError("Workflow dependency depth exceeded")
            depths[nid] = value
            return value

        for node in nodes:
            if set(node) - {
                "id",
                "objective",
                "dependencies",
                "executor_kind",
                "executor_config",
                "required_capabilities",
                "resource_refs",
                "completion_criteria",
                "status",
                "artifact_refs",
                "provenance",
            }:
                raise ValueError("Unknown workflow node field")
            if not isinstance(node.get("objective"), str) or not node["objective"].strip():
                raise ValueError("Workflow node objective is required")
            if node.get("status", "pending") not in STATUSES:
                raise ValueError("Invalid workflow node status")
            if not set(node.get("required_capabilities", [])).issubset(self.capabilities):
                raise ValueError("Workflow requires unregistered capabilities")
            if not set(node.get("resource_refs", [])).issubset(self.resources):
                raise ValueError("Workflow requires unbound resources")
            executor = node.get("executor_kind", "llm")
            config = node.get("executor_config", {})
            if not isinstance(config, dict):
                raise ValueError("Workflow executor configuration must be an object")
            if executor not in {"llm", "tool", "loop"}:
                raise ValueError("Unregistered workflow executor")
            if executor == "tool" and config.get("tool_id") not in self.tools:
                raise ValueError("Workflow tool is unavailable")
            if executor == "loop" and config.get("loop_id") not in self.loops:
                raise ValueError("Workflow loop is unavailable")
            allowed_fields = {"llm": {"allowed_tools"}, "tool": {"tool_id", "input"}, "loop": {"loop_id"}}[executor]
            if set(config) - allowed_fields:
                raise ValueError("Unknown workflow executor configuration")
            if "allowed_tools" in config and (not isinstance(config["allowed_tools"], list) or not set(config["allowed_tools"]).issubset(self.tools)):
                raise ValueError("Workflow node requests unavailable tools")
            node.setdefault("status", "pending")
            node.setdefault("dependencies", [])
            node.setdefault("executor_kind", "llm")
            node.setdefault("executor_config", {})
            depth(node["id"])
        if sum(node["status"] == "running" for node in nodes) > 1:
            raise ValueError("Parallel workflow execution is not enabled")
        return nodes

    def propose(self, proposal):
        if not isinstance(proposal, dict) or isinstance(proposal.get("base_revision"), bool) or not isinstance(proposal.get("base_revision"), int):
            raise ValueError("Workflow proposal requires an integer base_revision")
        if proposal.get("base_revision") != self.revision:
            raise ValueError("Stale workflow revision")
        if self.revision >= self.max_revisions:
            raise ValueError("Workflow revision budget exceeded")
        if not isinstance(proposal.get("reason"), str) or not proposal["reason"].strip():
            raise ValueError("Workflow revision requires a reason")
        nodes = self.validate(proposal["nodes"])
        proposed = {node["id"]: node for node in nodes}
        for previous in self.nodes:
            nid = previous["id"]
            if previous["status"] in TERMINAL | {"running"}:
                if proposed.get(nid) != previous:
                    raise ValueError("Completed and running workflow nodes cannot be rewritten")
            elif nid not in proposed:
                raise ValueError("Keep historical nodes; mark obsolete pending nodes superseded")
            elif proposed[nid]["status"] not in {previous["status"], "pending", "blocked", "skipped", "superseded"}:
                raise ValueError("Workflow proposals cannot mark unexecuted nodes completed or running")
        existing = {node["id"] for node in self.nodes}
        if any(node["id"] not in existing and node["status"] != "pending" for node in nodes):
            raise ValueError("New workflow nodes must start pending")
        self.nodes, self.revision = nodes, self.revision + 1
        return self.snapshot()

    def ready(self):
        active = [node for node in self.nodes if node["status"] == "running"]
        if active:
            return active
        finished = {node["id"] for node in self.nodes if node["status"] in TERMINAL}
        return [node for node in self.nodes if node["status"] in {"pending", "ready"} and set(node["dependencies"]).issubset(finished)]

    def snapshot(self):
        return {"revision": self.revision, "nodes": deepcopy(self.nodes)}

    def restore(self, value):
        revision = value["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision <= self.max_revisions:
            raise ValueError("Invalid workflow checkpoint revision")
        nodes = self.validate(value["nodes"]) if value["nodes"] else []
        if not nodes and revision:
            raise ValueError("A revised workflow cannot be empty")
        self.revision, self.nodes = revision, nodes
