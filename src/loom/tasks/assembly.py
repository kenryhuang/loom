"""Shared, versioned task assembly for one-shot runners and durable workers."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import tomllib
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

from loom.contexts.bounded import BoundedContextManager, ResearchContextManager
from loom.core import Context, Observation, ToolRef, err, make_loom_error, new_trace_id, now_iso, ok, thaw_json
from loom.execution.native import NativeToolExecutionRuntime
from loom.llm.api import LlmToolCall
from loom.runtime.execution_contracts import Invocation, validate_tool_input
from loom.runtime.plugin_contracts import PluginRegistry, config_digest, json_value
from loom.service.artifacts import ServiceArtifacts
from loom.session_environments.session import SessionEnvironment
from loom.tasks.completion import requested_source_urls, source_url
from loom.tasks.evidence import PAGE_CHARS, artifact_page, source_preview
from loom.tools.collections import builtin_collection, document_collection, research_collection
from loom.tools.contracts import AffordanceBudget, ToolCatalog
from loom.tools.resolver import ToolResolver
from loom.workflows.dynamic import DynamicWorkflowPolicy
from loom.workflows.legacy import LegacyPlanningWorkflow


def normalize_task_spec(spec=None, *, workspace=None, plan_mode="auto"):
    legacy = spec is None
    spec = json_value({} if spec is None else spec)
    if not isinstance(spec, dict) or set(spec) - {"schema_version", "session_environment", "context", "execution_runtime", "tools", "workflow", "outputs"}:
        raise ValueError("Unknown task plugin configuration")
    if spec.get("schema_version", 1) != 1:
        raise ValueError("Unsupported task specification schema")
    if legacy:
        resources = [{"id": "workspace", "kind": "directory", "uri": str(Path(workspace or Path.cwd()).expanduser().resolve()), "access": "read-write"}]
        spec["session_environment"] = {"plugin": "session", "resources": resources}
    environment = spec.setdefault("session_environment", {"plugin": "session"})
    if not isinstance(environment, dict) or not isinstance(environment.get("resources", []), list):
        raise ValueError("Invalid session environment specification")
    if any(not isinstance(resource, dict) for resource in environment.get("resources", [])):
        raise ValueError("Resource bindings must be objects")
    directories = [r for r in environment.get("resources", []) if r.get("kind") == "directory"]
    spec.setdefault("context", {"plugin": "bounded_context"})
    spec.setdefault("execution_runtime", {"plugin": "native_os"})
    spec.setdefault("tools", {"collections": ["filesystem", "shell", "task_control"] if directories else ["task_control"]})
    spec.setdefault("workflow", {"plugin": "legacy_planning", "mode": str(plan_mode)})
    spec.setdefault("outputs", [])
    spec["schema_version"] = 1
    for name in ("session_environment", "context", "execution_runtime", "workflow"):
        if not isinstance(spec[name], dict) or not isinstance(spec[name].get("plugin"), str):
            raise ValueError(f"Invalid {name} plugin specification")
        spec[name].setdefault("version", "1")
    if directories:
        if not isinstance(directories[0].get("id"), str):
            raise ValueError("Directory resources require an ID")
        spec["execution_runtime"].setdefault("workspace_resource", directories[0]["id"])
    if not isinstance(spec["tools"], dict) or set(spec["tools"]) - {"collections", "budget"}:
        raise ValueError("Invalid tool collection specification")
    collections = spec["tools"].get("collections")
    if not isinstance(collections, list) or any(not isinstance(name, str) for name in collections) or len(set(collections)) != len(collections):
        raise ValueError("Tool collections must be a unique list of plugin IDs")
    if not isinstance(spec["outputs"], list) or any(not isinstance(output, dict) for output in spec["outputs"]):
        raise ValueError("Output contracts must be a list of objects")
    for output in spec["outputs"]:
        if (
            set(output) - {"kind", "format", "require_evidence_refs", "require_verified_sources"}
            or output.get("kind") != "report"
            or output.get("format", "markdown") != "markdown"
        ):
            raise ValueError("Unsupported output contract")
        if not isinstance(output.get("require_evidence_refs", False), bool):
            raise ValueError("require_evidence_refs must be boolean")
        if not isinstance(output.get("require_verified_sources", False), bool):
            raise ValueError("require_verified_sources must be boolean")
    budget = spec["tools"].get("budget", {})
    if not isinstance(budget, dict) or set(budget) - {"max_tools", "max_tool_schema_tokens", "max_composed_tools", "max_ephemeral_tools"}:
        raise ValueError("Invalid tool visibility budget")
    for name, value in budget.items():
        minimum = 1 if name in {"max_tools", "max_tool_schema_tokens"} else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError("Invalid tool visibility budget limit")
    return spec


def default_plugin_registry():
    registry = PluginRegistry()
    registry.register("session_environment", "session", lambda config, **_: SessionEnvironment(config))
    registry.register("context", "bounded_context", lambda config, **services: BoundedContextManager(config, **services))
    registry.register("context", "research_context", lambda config, **services: ResearchContextManager(config, **services))
    registry.register("execution_runtime", "native_os", lambda config, **services: NativeToolExecutionRuntime(config, **services))
    registry.register("workflow", "legacy_planning", lambda config, **_: LegacyPlanningWorkflow(config))
    registry.register("workflow", "dynamic", lambda config, **services: DynamicWorkflowPolicy(config, **services))
    for name in ("filesystem", "shell", "task_control"):
        registry.register("tools", name, lambda config, request, name=name: _builtin(name, config, request))
    registry.register("tools", "web_research", lambda config, request: _custom_collection(research_collection, config, request))
    from loom.knowledge.tools import knowledge_collection

    registry.register("tools", "knowledge", lambda config, request: _custom_collection(knowledge_collection, config, request))
    registry.register("tools", "document_outputs", lambda config, request: _custom_collection(document_collection, config, request))
    return registry


def load_task_spec(path):
    path = Path(path)
    text = path.read_text()
    if path.suffix.lower() in {".yaml", ".yml"}:
        from loom.tasks.config import parse_yaml_document

        parsed = parse_yaml_document(text, path)
        if not parsed.ok:
            raise ValueError(parsed.error.message)
        value = parsed.value
    elif path.suffix.lower() == ".toml":
        value = tomllib.loads(text)
    else:
        value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Task specification must be an object")
    # Relative directory resources belong to the specification file, not the
    # directory from which the service happens to be started.
    environment = value.get("session_environment", {})
    if not isinstance(environment, dict) or not isinstance(environment.get("resources", []), list):
        raise ValueError("Invalid session environment specification")
    for resource in environment.get("resources", []):
        if not isinstance(resource, dict):
            raise ValueError("Resource bindings must be objects")
        if resource.get("kind") == "directory":
            if not isinstance(resource.get("uri"), str) or not resource["uri"]:
                raise ValueError("Directory resources require a URI")
            resource["uri"] = str((path.resolve().parent / Path(resource["uri"]).expanduser()).resolve())
    return value


def _builtin(name, config, request):
    if config:
        raise ValueError("Builtin tool collections have no configuration fields")
    return builtin_collection(name, request)


def _custom_collection(factory, config, request):
    if config:
        raise ValueError("Tool collection has no configuration fields")
    return factory(request)


class TaskAssembly:
    def __init__(self, request, *, plan_mode="auto", harness=None, execution=None, registry=None, session_id="", run_id="", attempt_id="", loops=None):
        self.request = request
        self.execution = execution
        self.session_id, self.run_id, self.attempt_id = session_id, run_id, attempt_id
        self.spec = normalize_task_spec(request.task_spec, workspace=request.workspace, plan_mode=plan_mode)
        self.digest = config_digest(self.spec)
        self.registry = registry or default_plugin_registry()
        self.environment = self.registry.create("session_environment", self.spec["session_environment"]).open()
        self.resources = self.environment.resources()
        self.loops = loops or {}
        directories = [r for r in self.resources if r.kind == "directory"]
        workspace_id = self.spec["execution_runtime"].get("workspace_resource")
        workspace = next((r for r in directories if r.id == workspace_id), None)
        self.tool_request = replace(request, workspace=Path(workspace.uri) if workspace else None)
        self.report_workspace = Path(workspace.uri) if workspace and workspace.access == "read-write" else None
        self.collections = [
            self.registry.create(
                "tools", {"plugin": name},
                # Read-only sessions retain report artifacts without receiving a file writer.
                request=replace(self.tool_request, workspace=None) if name == "document_outputs" and self.report_workspace is None else self.tool_request,
            )
            for name in self.spec["tools"]["collections"]
        ]
        self.workspace_report_required = (
            self.report_workspace is not None and "document_outputs" in self.spec["tools"]["collections"] and bool(self.spec["outputs"])
        )
        self.bindings = {}
        self.entrypoints = {}
        self.expose_artifacts = harness is None or harness.allowed_tools is None or "read_artifact" in harness.allowed_tools
        for collection in self.collections:
            for binding in collection.bindings():
                if binding.ref.id in self.bindings:
                    raise ValueError(f"Ambiguous tool alias: {binding.ref.id}")
                if collection.manifest.plugin_id in {"filesystem", "shell"} and workspace is None:
                    raise ValueError("Filesystem and shell tools require an explicit workspace resource")
                if workspace and workspace.access in {"read", "read-only"} and binding.effect_kind == "side_effecting":
                    continue
                if collection.manifest.plugin_id in {"filesystem", "shell"} or (
                    collection.manifest.plugin_id == "document_outputs" and binding.effect_kind == "side_effecting"
                ):
                    binding = replace(binding, resource_refs=(workspace.id,))
                if not set(binding.resource_refs).issubset({r.id for r in self.resources}):
                    raise ValueError(f"Unbound resources required by tool {binding.id}")
                self.bindings[binding.ref.id] = binding
            for entrypoint, handler in collection.entrypoints.items():
                if entrypoint in self.entrypoints:
                    raise ValueError("Duplicate tool entrypoint")
                self.entrypoints[entrypoint] = handler
        if harness and harness.allowed_tools is not None:
            unknown = set(harness.allowed_tools) - self.bindings.keys() - {"read_artifact"}
            if unknown:
                raise ValueError(f"Unknown allowed tools: {sorted(unknown)}")
            self.bindings = {name: binding for name, binding in self.bindings.items() if name in harness.allowed_tools}
        self.required_source_urls = requested_source_urls(request.objective) if request.task_spec is not None and not directories else ()
        if self.required_source_urls and not any(binding.artifact_kind == "source" and binding.evidence_fields for binding in self.bindings.values()):
            raise ValueError("Task requires live URL retrieval, but selected tool collections provide no source reader")
        self.runtime = self.registry.create(
            "execution_runtime",
            self.spec["execution_runtime"],
            entrypoints={
                binding.entrypoint_id: self.entrypoints[binding.entrypoint_id] for binding in self.bindings.values() if binding.placement == "execution"
            },
            execution=execution,
            cancellable=[binding.entrypoint_id for binding in self.bindings.values() if binding.supports_cancel],
        )
        self.runtime.prepare(self.resources)
        for binding in self.bindings.values():
            if not set(binding.required_capabilities).issubset(self.runtime.manifest.capabilities):
                raise ValueError(f"Runtime lacks required capabilities for {binding.id}")
        self.context_manager = self.registry.create("context", self.spec["context"], publish_artifact=self.publish_artifact)
        self.workflow = self.registry.create(
            "workflow",
            self.spec["workflow"],
            capabilities=self.runtime.manifest.capabilities,
            resources=[r.id for r in self.resources],
            tools=list(self.bindings),
            loops=list(self.loops),
            execution=execution,
        )
        self.workflow.snapshot_plugins = self.snapshot
        self.workflow.completion_check = self.completion_error
        self.workflow.publish_artifact = self.publish_artifact
        self.normal_refs = tuple(binding.ref for binding in self.bindings.values())
        self.workflow.configure_normal_tool_refs(self.normal_refs)
        self.context = None
        self._local_artifacts = None
        self._evidence = []
        self._verified_sources = {}
        self._workspace_reports = {}
        plugins = [self.environment, self.context_manager, self.runtime, self.workflow, *self.collections]
        self.manifests = [asdict(plugin.manifest) for plugin in plugins]
        available = {f"{p.manifest.kind}/{p.manifest.plugin_id}" for p in plugins}
        for plugin in plugins:
            if not set(plugin.manifest.dependencies).issubset(available):
                raise ValueError(f"Missing dependencies for plugin {plugin.manifest.plugin_id}")
        if execution is not None:
            execution.binding_effects = {name: binding.effect_kind for name, binding in self.bindings.items()}
            execution.binding_effects.update({ref.id: "service_control" for ref in self.workflow.tool_refs()})

    def publish_artifact(self, value, kind):
        if self.execution:
            return self.execution.rpc("publish_artifact", {"value": value, "kind": kind})
        if self._local_artifacts is None:
            self._local_artifacts = ServiceArtifacts(Path(tempfile.mkdtemp(prefix="loom-task-artifacts-")))
        ref = self._local_artifacts.publish(value, kind)
        return {**ref, "local_path": str(self._local_artifacts.directory / ref["relative_path"])}

    def read_artifact(self, digest):
        if self.execution:
            return self.execution.rpc("read_artifact", {"digest": digest})
        if self._local_artifacts is None:
            raise ValueError("Artifact not found")
        return json.loads(self._local_artifacts.read(digest))

    def snapshot(self):
        return json_value(
            {
                "schema_version": 1,
                "config_digest": self.digest,
                "session_environment": self.environment.snapshot(),
                "context": self.context_manager.snapshot(),
                "execution_runtime": self.runtime.snapshot(),
                "workflow": self.workflow.plugin_snapshot(),
                "tools": {c.manifest.plugin_id: c.snapshot() for c in self.collections},
                "evidence": self._evidence,
                "verified_sources": self._verified_sources,
                **({"workspace_reports": self._workspace_reports} if self._workspace_reports else {}),
                "manifests": self.manifests,
            }
        )

    def restore(self, snapshot):
        snapshot = json_value(snapshot)
        if snapshot.get("schema_version") != 1 or snapshot.get("config_digest") != self.digest:
            raise ValueError("Task plugin configuration changed since checkpoint")
        if json_value(snapshot.get("manifests")) != json_value(self.manifests):
            raise ValueError("Task plugin manifests changed since checkpoint")
        self.environment.restore(snapshot["session_environment"])
        self.context_manager.restore(snapshot["context"])
        self.runtime.restore(snapshot["execution_runtime"])
        self.workflow.restore_plugin(snapshot["workflow"])
        self.workflow.configure_normal_tool_refs(self.normal_refs)
        if set(snapshot["tools"]) != {c.manifest.plugin_id for c in self.collections}:
            raise ValueError("Checkpoint tool collection set changed")
        for collection in self.collections:
            collection.restore(snapshot["tools"][collection.manifest.plugin_id])
        self._evidence = snapshot.get("evidence", [])
        self._verified_sources = snapshot.get("verified_sources", {})
        self._workspace_reports = snapshot.get("workspace_reports", {})
        if not self.environment.reconnect() or not self.runtime.reconnect():
            raise ValueError("Task execution resources could not be reconnected")

    def resolve_tools(self, context):
        refs = context.affordances.tools
        budget_config = self.spec["tools"].get("budget")
        if budget_config is None:
            return refs
        from loom.tools.contracts import CatalogTool, ToolLifecycle

        required = tuple(ref.id for ref in refs if ref.id not in self.bindings or self.bindings[ref.id].placement == "control")
        catalog = ToolCatalog(atomic=tuple(CatalogTool(ref, ToolLifecycle("atomic")) for ref in refs))
        return ToolResolver(required_atomic_ids=required).resolve(catalog, AffordanceBudget(**budget_config)).tools

    def update_goal(self, goal):
        self.required_source_urls = (
            requested_source_urls(goal.objective)
            if self.request.task_spec is not None and not any(resource.kind == "directory" for resource in self.resources)
            else ()
        )

    def handlers(self):
        handlers = {}
        for name, binding in self.bindings.items():

            async def invoke(value, options=None, binding=binding, name=name):
                value = thaw_json(value)
                try:
                    validate_tool_input(binding.ref.input_schema, value)
                except ValueError as exc:
                    hint = {
                        "shell_execute": 'Use {"command":"git fetch && git status"}; command must be a script string. For argv arrays use process_execute.',
                        "process_execute": 'Use {"argv":["git","status"]}; argv must be a string array. For shell syntax use shell_execute.',
                    }.get(name, "")
                    return err(
                        make_loom_error(
                            "VALIDATION_FAILED", f"{exc}. {hint}".strip(), retryable=False, metadata={"failureDomain": "tool", "error_kind": "invalid_input"}
                        )
                    )
                if name == "finish":
                    report = value.get("report") or value.get("content")
                    if not isinstance(report, str) or not report.strip():
                        return ok(Observation(new_trace_id(), name, {"accepted": False, "reason": "Final report is required"}, now_iso()))
                    if reason := self.completion_error():
                        return ok(Observation(new_trace_id(), name, {"accepted": False, "reason": reason}, now_iso()))
                if binding.placement == "control":
                    return await self.entrypoints[binding.entrypoint_id](value, options)
                metadata = (options or {}).get("metadata", {})
                call_id = metadata.get("tool_call_id") or uuid.uuid4().hex
                run_id = self.context.run_id if self.context else self.run_id
                oid = f"{run_id}:{call_id}"
                call = LlmToolCall(call_id, name, json.dumps(value, sort_keys=True))
                journal_here = self.execution is not None and not metadata.get("operation_journaled")
                persisted = None
                if journal_here:
                    persisted = self.execution.operation_start(call)
                    if persisted and persisted.get("defer"):
                        return err(make_loom_error("EXECUTION_DEFERRED", "Execution deferred at a control boundary", retryable=True))
                deadline = time.time() + binding.ref.timeout_ms / 1000 if binding.ref.timeout_ms else None
                invocation = Invocation(
                    oid,
                    call_id,
                    binding.id,
                    binding.entrypoint_id,
                    value,
                    self.session_id,
                    run_id,
                    self.attempt_id,
                    self.runtime.runtime_id,
                    tool_version=binding.version,
                    resource_refs=binding.resource_refs,
                    deadline=deadline,
                    goal_revision=getattr(self.execution, "goal_revision", 0),
                    workflow_revision=self.workflow.snapshot().get("workflow", self.workflow.snapshot().get("plan", {})).get("revision", 0),
                    node_id=getattr(self.workflow, "_active", None),
                )
                if persisted:
                    result = ok(persisted["value"]) if persisted["ok"] else err(persisted["error"])
                else:
                    executed = await self.runtime.execute(invocation, options)
                    result = executed.result
                if result.ok and isinstance(result.value, Observation):
                    output = thaw_json(result.value.value)
                    if binding.artifact_kind and not (isinstance(output, dict) and "artifact" in output):
                        ref = self.publish_artifact(output, binding.artifact_kind)
                        if not isinstance(output, dict):
                            output = {"value": output}
                        result = ok(replace(result.value, value={**output, "artifact": ref}))
                    self.ingest_tool_result(name, result.value)
                    result = ok(self.project_tool_result(name, result.value))
                if journal_here and not persisted:
                    self.execution.operation_finish(call, {"ok": result.ok, "value": result.value, "error": result.error})
                return result

            handlers[name] = invoke

        async def read_evidence(value, _options=None):
            try:
                validate_tool_input(self.artifact_tool_ref().input_schema, value)
                # Validate paging bounds before loading potentially large evidence.
                limit, offset = value.get("limit", PAGE_CHARS), value.get("offset", 0)
                if not 1 <= limit <= PAGE_CHARS or offset < 0:
                    raise ValueError(f"offset must be nonnegative and limit must be between 1 and {PAGE_CHARS}")
                page = artifact_page(self.read_artifact(value["digest"]), **value)
            except (ValueError, TypeError) as exc:
                return err(make_loom_error("VALIDATION_FAILED", str(exc), retryable=False))
            return ok(Observation(new_trace_id(), "read_artifact", page, now_iso()))

        if self.expose_artifacts:
            handlers["read_artifact"] = read_evidence
        return self.workflow.wrap_tools(handlers)

    @staticmethod
    def artifact_tool_ref():
        return ToolRef(
            "read_artifact",
            "Read retained session artifacts by artifact digest. For knowledge documents use knowledge_read, not document_digest. "
            "Auto prefers text; json returns serialized data. Follow read_more/next_offset to page forward; limit counts characters.",
            input_schema={
                "type": "object",
                "properties": {
                    "digest": {"type": "string"},
                    "view": {"type": "string", "enum": ["auto", "text", "json"]},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_CHARS},
                },
                "required": ["digest"],
                "additionalProperties": False,
            },
        )

    def context_tools(self):
        return (*self.normal_refs, self.artifact_tool_ref()) if self.expose_artifacts else self.normal_refs

    def project_tool_result(self, tool_id, observation):
        binding = self.bindings.get(tool_id)
        if binding is None or binding.artifact_kind != "source" or not isinstance(observation, Observation):
            return observation
        output = thaw_json(observation.value)
        if isinstance(output, dict) and isinstance(output.get("artifact"), dict):
            return replace(observation, value=source_preview(output))
        return observation

    def ingest_tool_result(self, tool_id, observation):
        """Retain proof from successful source tools, including journal replay."""
        binding = self.bindings.get(tool_id)
        if binding is None or not isinstance(observation, Observation):
            return
        output = thaw_json(observation.value)
        if not isinstance(output, dict):
            return
        if (
            binding.artifact_kind == "report" and self.report_workspace is not None
            and isinstance(output.get("path"), str) and isinstance(output.get("report"), str)
        ):
            self._workspace_reports[output["path"]] = {
                "sha256": hashlib.sha256(output["report"].encode("utf-8")).hexdigest(),
                "artifact": output.get("artifact"),
            }
        refs = []
        for field in binding.evidence_fields:
            evidence = output.get(field, [])
            evidence = [evidence] if isinstance(evidence, str) else evidence
            if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
                raise ValueError("Tool evidence references must be strings")
            refs.extend(item for item in evidence if item.strip())
        self._evidence = list(dict.fromkeys([*self._evidence, *refs]))
        artifact = output.get("artifact", {})
        if binding.artifact_kind == "knowledge_source" and isinstance(artifact, dict) and artifact.get("kind") == "knowledge_source":
            passages = output.get("matches", [output])
            for passage in passages:
                if isinstance(passage, dict) and passage.get("text", "").strip() and isinstance(passage.get("source_id"), str):
                    source = passage["source_id"]
                    self._verified_sources[source] = artifact
                    if source not in self._evidence:
                        self._evidence.append(source)
        if binding.artifact_kind == "source" and isinstance(artifact, dict) and artifact.get("kind") == "source":
            for url in [*refs, output.get("requested_url", "")]:
                if isinstance(url, str) and urlsplit(url).scheme in {"http", "https"}:
                    self._verified_sources[source_url(url)] = artifact

    def completion_error(self):
        missing = set(self.required_source_urls) - self._verified_sources.keys()
        if missing:
            return "Task requires successful retrieval of source URLs: " + ", ".join(sorted(missing))
        if any(o.get("require_verified_sources") for o in self.spec["outputs"]) and not self._verified_sources:
            return "Final report requires source evidence from a successful retrieval tool"
        if any(o.get("require_evidence_refs") for o in self.spec["outputs"]) and not self._evidence:
            return "Final report requires evidence source references"
        if self.workspace_report_required:
            if not self._workspace_reports:
                return "Save the complete analysis in the workspace using create_report with a relative path before finishing"
            from loom.tasks.tools import _resolve_workspace_path

            for path, report in self._workspace_reports.items():
                resolved = _resolve_workspace_path(self.report_workspace, path)
                if not resolved.ok:
                    return f"Saved report path is no longer inside the workspace: {path}. Save it again using create_report"
                try:
                    current = hashlib.sha256(resolved.value.read_bytes()).hexdigest()
                except OSError:
                    return f"Saved report is unavailable: {path}. Save it again using create_report"
                if current != report["sha256"]:
                    return f"Saved report changed since create_report: {path}. Save its final content again using create_report"
        return None

    def wrap_loop(self, definition):
        wrapped = self.workflow.wrap_loop(definition)

        async def task_step(context, runtime):
            self.context = context
            result = await wrapped.step(context, runtime)
            if result.ok:
                control = result.value.control
                completed = control is None or control.kind == "completed"
                if completed and (reason := self.completion_error()):
                    return err(make_loom_error("OUTPUT_CONTRACT_FAILED", reason, retryable=False))
                updated = result.value.context
                ingested = self.context_manager.ingest(updated, result.value)
                if not isinstance(ingested, Context) or any(
                    getattr(ingested, field) != getattr(updated, field) for field in ("goal", "identity", "affordances", "run_id")
                ):
                    return err(make_loom_error("CONTEXT_POLICY_INVALID", "Context policy changed protected execution state", retryable=False))
                updated = ingested
                scratch = dict(updated.state.scratch or {})
                if completed and self.spec["outputs"]:
                    from loom.tasks.runner import _report_from_run_result

                    report = _report_from_run_result(result.value)
                    if not report.strip():
                        return err(make_loom_error("OUTPUT_CONTRACT_FAILED", "Final report must be nonempty", retryable=False))
                    output = {"report": report, "sources": list(dict.fromkeys(self._evidence))}
                    if self._workspace_reports:
                        output["documents"] = [{"path": path, **saved} for path, saved in self._workspace_reports.items()]
                    ref = self.publish_artifact(output, "report")
                    if self._workspace_reports:
                        ref = {**ref, "workspace_paths": list(self._workspace_reports)}
                    scratch["output_artifacts"] = [ref]
                scratch["execution_plugins"] = self.snapshot()
                result = ok(replace(result.value, context=replace(updated, state=replace(updated.state, scratch=scratch))))
            return result

        return replace(wrapped, step=task_step)

    async def close(self):
        try:
            await self.runtime.close()
        finally:
            await self.environment.close()
