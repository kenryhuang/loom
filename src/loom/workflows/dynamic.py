"""Sequential dynamic workflows with validated revisions and plan projection."""

from __future__ import annotations

from dataclasses import replace

from loom.core import Constraint, Observation, StepResult, ToolRef, Trace, err, make_loom_error, new_trace_id, now_iso, ok, thaw_json
from loom.llm.api import LlmStepPolicy, TokenUsage
from loom.runtime.control import StepControl
from loom.runtime.plugin_contracts import PluginManifest, json_value
from loom.runtime.workflow import TERMINAL, WorkflowController


class DynamicWorkflowPolicy:
    manifest = PluginManifest("dynamic", "workflow")

    def __init__(self, config, *, capabilities=(), resources=(), tools=(), loops=(), execution=None):
        if set(config) - {"nodes", "limits"}:
            raise ValueError("Unknown dynamic workflow configuration")
        self.config = json_value(config)
        limits = config.get("limits", {})
        if not isinstance(limits, dict):
            raise ValueError("Workflow limits must be an object")
        if "nodes" in config and not isinstance(config["nodes"], list):
            raise ValueError("Workflow nodes must be a list")
        if set(limits) - {"max_nodes", "max_revisions", "max_depth"}:
            raise ValueError("Unknown workflow limit")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in limits.values()):
            raise ValueError("Workflow limits must be positive integers")
        self.controller = WorkflowController(capabilities=capabilities, resources=resources, tools=tools, loops=loops, **limits)
        self.execution = execution
        self._normal_refs = ()
        self._runtime = None
        self._context = None
        self._active = None
        if config.get("nodes"):
            self.controller.nodes = self.controller.validate(config["nodes"])
            if any(node["status"] != "pending" for node in self.controller.nodes):
                raise ValueError("Initial workflow nodes must start pending")

    def snapshot(self):
        return {"schema_version": 1, "workflow": self.controller.snapshot(), "active": self._active}

    def restore(self, state):
        try:
            if state["schema_version"] != 1:
                raise ValueError("Incompatible workflow checkpoint")
            self.controller.restore(state["workflow"])
            self._active = state["active"]
            if self._active is not None and self._active not in {node["id"] for node in self.controller.nodes}:
                raise ValueError("Invalid active workflow node")
            return ok(None)
        except (ValueError, KeyError, TypeError) as exc:
            return err(make_loom_error("VALIDATION_FAILED", str(exc), retryable=False))

    def plugin_snapshot(self):
        return self.manifest.state(self.config, self.snapshot())

    def restore_plugin(self, state):
        self.restore(self.manifest.restore(self.config, state)).unwrap()

    def configure_normal_tool_refs(self, refs):
        self._normal_refs = tuple(refs)

    def tool_refs(self):
        return (
            ToolRef(
                "revise_workflow",
                "Revise future workflow nodes; retain completed/running nodes exactly, and use the current base_revision.",
                input_schema={
                    "type": "object",
                    "properties": {"base_revision": {"type": "integer"}, "reason": {"type": "string"}, "nodes": {"type": "array", "items": {"type": "object"}}},
                    "required": ["base_revision", "reason", "nodes"],
                },
            ),
            ToolRef(
                "complete_node",
                "Complete the active workflow node with evidence; the next dependency-ready node runs afterwards.",
                input_schema={"type": "object", "properties": {"evidence": {"type": "string"}, "artifact_refs": {"type": "array"}}, "required": ["evidence"]},
            ),
        )

    def visible_tool_refs(self, refs):
        node = next((node for node in self.controller.nodes if node["id"] == self._active), None)
        if node and "allowed_tools" in node["executor_config"]:
            refs = tuple(ref for ref in refs if ref.id in node["executor_config"]["allowed_tools"])
        return (*refs, *self.tool_refs())

    def step_policy(self, _context):
        return LlmStepPolicy()

    def observe_tool(self, _context, observation):
        return observation

    def unfinished(self):
        return any(node["status"] not in TERMINAL for node in self.controller.nodes)

    def is_final_node(self):
        return not any(node["status"] not in TERMINAL and node["id"] != self._active for node in self.controller.nodes)

    def plan_view(self):
        statuses = {"succeeded": "completed", "running": "in_progress", "superseded": "skipped", "skipped": "skipped"}
        return {
            "revision": self.controller.revision,
            "phase": "executing" if self.unfinished() else "completed",
            "items": [
                {"id": node["id"], "content": node["objective"], "status": statuses.get(node["status"], "pending"), "note": node.get("provenance", "")}
                for node in self.controller.nodes
            ],
        }

    def project_state(self, context):
        scratch = dict(context.state.scratch or {})
        scratch.update(workflow=self.snapshot(), plan=self.plan_view())
        return replace(context, state=replace(context.state, scratch=scratch))

    async def _emit(self, kind):
        if self._runtime:
            result = await self._runtime.trace_sink.emit(
                {
                    "type": kind,
                    "workflow": self.snapshot(),
                    "plan": self.plan_view(),
                    "run_id": self._runtime.run_id,
                    "trace_id": self._runtime.trace_id,
                    "at": now_iso(),
                }
            )
            result.unwrap()

    async def complete_active(self, evidence="", artifact_refs=()):
        node = next((node for node in self.controller.nodes if node["id"] == self._active), None)
        if node is None or node["status"] != "running":
            raise ValueError("No running workflow node")
        if node.get("completion_criteria") and not evidence.strip():
            raise ValueError("Node completion requires evidence")
        if self.is_final_node() and (reason := getattr(self, "completion_check", lambda: None)()):
            raise ValueError(reason)
        artifact_refs = list(artifact_refs)
        if len(evidence) > 2000 and (artifact_refs or hasattr(self, "publish_artifact")):
            if not artifact_refs and hasattr(self, "publish_artifact"):
                artifact_refs.append(self.publish_artifact({"evidence": evidence}, "workflow_evidence"))
            evidence = evidence[:2000] + "\n… Full evidence retained in artifact_refs."
        node.update(status="succeeded", provenance=evidence, artifact_refs=artifact_refs)
        await self._emit("workflow.node.completed")

    def _observation(self, name):
        return ok(
            Observation(
                new_trace_id(),
                name,
                {"accepted": True, "workflow": self.snapshot(), "plan": self.plan_view()},
                now_iso(),
                metadata={"controlFlow": {"stepBoundary": True, "reason": "workflow_transition"}},
            )
        )

    def wrap_tools(self, handlers):
        wrapped = dict(handlers)

        async def revise(value, _options=None):
            try:
                self.controller.propose(thaw_json(value))
                await self._emit("workflow.revised")
                return self._observation("revise_workflow")
            except (ValueError, KeyError, TypeError) as exc:
                return ok(Observation(new_trace_id(), "revise_workflow", {"accepted": False, "code": "WORKFLOW_INVALID", "reason": str(exc)}, now_iso()))

        async def complete(value, _options=None):
            try:
                if not isinstance(value.get("evidence"), str) or not value["evidence"].strip():
                    raise ValueError("Node completion requires evidence")
                if "finish" in handlers and self.is_final_node():
                    raise ValueError("Final node requires a final report; use finish instead of complete_node")
                await self.complete_active(value["evidence"], value.get("artifact_refs", []))
                return self._observation("complete_node")
            except ValueError as exc:
                return ok(Observation(new_trace_id(), "complete_node", {"accepted": False, "reason": str(exc)}, now_iso()))

        if "finish" in handlers:

            async def finish(value, options=None):
                if not self.is_final_node():
                    return ok(
                        Observation(new_trace_id(), "finish", {"accepted": False, "reason": "Complete remaining workflow nodes before finishing"}, now_iso())
                    )
                result = await handlers["finish"](value, options)
                if result.ok and thaw_json(result.value.value).get("completed") and self.unfinished():
                    await self.complete_active(str(value.get("report") or value.get("content") or ""))
                return result

            wrapped["finish"] = finish
        wrapped.update(revise_workflow=revise, complete_node=complete)
        return wrapped

    def wrap_loop(self, definition):
        async def workflow_step(context, runtime):
            # A suspended terminal boundary belongs to the previous node. Replay
            # that boundary before choosing a new node; never complete the next
            # node with the previous node's model response.
            if self.execution and self.execution.checkpoint and self.execution.checkpoint.get("terminal"):
                return await definition.step(self.project_state(context), runtime)
            if not self.controller.nodes:
                self.controller.nodes = self.controller.validate([{"id": "task", "objective": context.goal.objective}])
            ready = self.controller.ready()
            if not ready:
                return err(make_loom_error("WORKFLOW_BLOCKED", "No dependency-ready workflow node", retryable=False))
            node = ready[0]
            self._active = node["id"]
            node["status"] = "running"
            self._runtime, self._context = runtime, context
            try:
                await self._emit("workflow.node.started")
                constraint = Constraint(
                    "dynamic-workflow",
                    f"Current node: {node['id']}: {node['objective']}. "
                    f"Workflow revision {self.controller.revision}: {json_value(self.controller.nodes)}. "
                    "Use complete_node when this node is done, revise_workflow to change future work, and finish for the final report.",
                )
                prompt = replace(
                    self.project_state(context),
                    identity=replace(context.identity, constraints=(*[c for c in context.identity.constraints if c.id != constraint.id], constraint)),
                    affordances=replace(context.affordances, tools=self.visible_tool_refs(self._normal_refs)),
                )
                if node["executor_kind"] == "llm":
                    result = await definition.step(prompt, runtime)
                    node = next(item for item in self.controller.nodes if item["id"] == self._active)
                    if result.ok:
                        control = result.value.control
                        decision = result.value.context.state.decisions[-1] if result.value.context.state.decisions else None
                        final_answer = control.kind == "completed" if control else decision and not (decision.metadata or {}).get("parseFallback")
                        if node["status"] == "running" and final_answer:
                            try:
                                await self.complete_active(str(thaw_json(result.value.output) or ""))
                            except ValueError as exc:
                                return err(make_loom_error("OUTPUT_CONTRACT_FAILED", str(exc), retryable=False))
                else:
                    if self.execution is not None:
                        cp = {
                            "schema_version": 1,
                            "phase": "workflow_node",
                            "context": prompt,
                            "trace_id": runtime.trace_id,
                            "input_cursor": self.execution.input_cursor,
                            "llm_calls": self.execution.counters.get("llm_calls", 0),
                            "usage": self.execution.counters.get("usage", TokenUsage()),
                            "planning": self.snapshot(),
                            "plugin_states": self._snapshot_plugins(),
                            "observations": [],
                        }
                        directive = self.execution.boundary(cp)
                        if directive.get("control"):
                            return ok(self._node_result(prompt, runtime, None, directive["control"]["kind"]))
                        if directive.get("inputs"):
                            guidance = "\n".join(item["content"] for item in directive["inputs"])
                            prompt = replace(prompt, goal=replace(prompt.goal, objective=f"{prompt.goal.objective}\n\nUser guidance:\n{guidance}"))
                            cp["context"] = prompt
                            cp["input_cursor"] = directive["inputs"][-1]["seq"]
                            self.execution.boundary(cp)
                    config = node["executor_config"]
                    if node["executor_kind"] == "tool":
                        result = await runtime.call_tool(
                            config["tool_id"],
                            config.get("input", {}),
                            metadata={"tool_call_id": f"{runtime.trace_id}:{node['id']}", "trace_id": runtime.trace_id},
                        )
                    else:
                        if self.execution:
                            raise ValueError("Durable child-loop continuation is not enabled")
                        result = await runtime.run_loop(config["loop_id"], prompt, cancellation=runtime.cancellation)
                    if result.ok:
                        value = result.value
                        output = thaw_json(getattr(value, "value", getattr(value, "output", value)))
                        try:
                            await self.complete_active(str(output), [output["artifact"]] if isinstance(output, dict) and "artifact" in output else [])
                        except ValueError as exc:
                            return err(make_loom_error("OUTPUT_CONTRACT_FAILED", str(exc), retryable=False))
                        observation = value if isinstance(value, Observation) else Observation(new_trace_id(), "workflow_node", str(value), now_iso())
                        updated = replace(prompt, state=replace(prompt.state, observations=(*prompt.state.observations, observation)))
                        result = ok(self._node_result(updated, runtime, thaw_json(observation.value), "continue" if self.unfinished() else "completed"))
                if not result.ok:
                    node["status"] = "failed"
                    await self._emit("workflow.node.failed")
                    return result
                value = result.value
                control = value.control
                if control and control.kind in {"paused", "waiting_input", "stopped"}:
                    return ok(replace(value, context=self.project_state(value.context)))
                control = StepControl("continue" if self.unfinished() else "completed")
                value = replace(value, context=self.project_state(value.context), control=control)
                if self.execution and self.execution.checkpoint:
                    cp = self.execution.checkpoint
                    cp["planning"] = self.snapshot()
                    cp["plugin_states"] = self._snapshot_plugins()
                    if cp.get("terminal"):
                        cp["terminal"]["kind"] = control.kind
                    else:
                        cp["phase"] = "step_done"
                        cp["context"] = value.context
                    self.execution.boundary(cp)
                return ok(value)
            finally:
                self._runtime = self._context = None

        async def done(_context, _runtime):
            return ok(bool(self.controller.nodes) and not self.unfinished())

        return replace(definition, step=workflow_step, done=done)

    def _snapshot_plugins(self):
        return self.snapshot_plugins() if hasattr(self, "snapshot_plugins") else {}

    @staticmethod
    def _node_result(context, runtime, output, kind):
        trace = Trace(
            runtime.trace_id,
            context.run_id,
            runtime.loop_id,
            "v1",
            len(context.state.observations),
            runtime.trace_id,
            now_iso(),
            now_iso(),
            0,
            context.id,
            context.id,
            "pass",
        )
        return StepResult(context, trace, output=output, control=StepControl(kind))
