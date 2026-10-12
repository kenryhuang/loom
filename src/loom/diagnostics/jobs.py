"""Durable diagnosis jobs backed by an isolated instance of the normal Loom service."""

import hashlib
import json
import os
import shutil
import threading
from contextlib import suppress
from pathlib import Path

from loom.core import now_iso
from loom.diagnostics.evidence import EvidenceView, capture, redact, source_root, write_json
from loom.diagnostics.report import markdown, validate_report
from loom.diagnostics.runner import objective, registry_factory, specification
from loom.diagnostics.validation import backend
from loom.diagnostics.workspace import DiagnosisWorkspace
from loom.runtime.checkpoints import decode, encode, plain
from loom.service.contracts import ServiceError, canonical, new_id
from loom.service.controller import LoomService
from loom.tasks.config import _read_dotenv, load_task_config

DEFAULTS = {"max_steps": 20, "max_llm_calls": 30, "max_tokens": 300000, "max_duration_seconds": 600, "max_tool_calls": 100}
MAXIMUM = {"max_steps": 100, "max_llm_calls": 200, "max_tokens": 2000000, "max_duration_seconds": 3600, "max_tool_calls": 500}
ACTIVE = {"preparing", "queued", "running", "cancelling"}


def budgets(value, previous=None):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ServiceError("Invalid diagnosis budgets")
    result = {**(previous or DEFAULTS), **value}
    if any(type(v) is not int or not 1 <= v <= MAXIMUM[k] for k, v in result.items()):
        raise ServiceError("Diagnosis budgets exceed allowed limits")
    if previous and any(result[k] < v for k, v in previous.items()):
        raise ServiceError("Resume budgets must not decrease")
    return result


class SessionDiagnosis:
    def __init__(self, service, *, root=None, provider_factory=None):
        self.service = service
        self.directory = service.store.directory / "diagnostics"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        self.source_error = None
        try:
            self.root = source_root(root or os.environ.get("LOOM_DIAGNOSTICS_SOURCE_ROOT"))
        except ServiceError as exc:
            self.root = None
            self.source_error = str(exc)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        with service.store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS diagnosis_jobs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, body TEXT NOT NULL)")
        self.runtime = LoomService(
            self.directory / "runtime",
            config_path=service.config_path,
            max_active_runs=1,
            provider_factory=provider_factory or service.provider_factory,
            plugin_registry_factory=registry_factory,
        )
        # Shared admission lock prevents the two supervisors from racing for the final slot.
        self.runtime.lock = service.lock
        self.runtime.shared_capacity = service.capacity
        self.runtime.shared_active = lambda: service.active

        def operation_limit(state):
            directory = next(r["uri"] for r in state["task"]["task_spec"]["session_environment"]["resources"] if r["kind"] == "diagnosis_snapshot")
            return EvidenceView(directory).ledger().get("max_tool_calls", 100)

        self.runtime.operation_limit = operation_limit
        service.shared_active = lambda: self.runtime.active
        self.runtime.start()
        self.thread = threading.Thread(target=self._monitor, daemon=True, name="loom-diagnoses")
        self.thread.start()

    def _save(self, job):
        with self.service.store.transaction() as db:
            db.execute(
                "INSERT INTO diagnosis_jobs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (job["id"], job["session_id"], canonical(job))
            )

    def _jobs(self, sid=None):
        with self.service.store.transaction() as db:
            rows = db.execute("SELECT body FROM diagnosis_jobs" + (" WHERE session_id=?" if sid else "") + " ORDER BY rowid DESC", (sid,) if sid else ())
            return [json.loads(row[0]) for row in rows]

    def _load(self, sid, jid):
        self.service.store.snapshot(sid)
        with self.service.store.transaction() as db:
            row = db.execute("SELECT body FROM diagnosis_jobs WHERE id=? AND session_id=?", (jid, sid)).fetchone()
        if not row:
            raise ServiceError("Diagnosis not found", 404)
        return json.loads(row[0])

    def _configuration(self, state=None):
        environment = {**_read_dotenv(Path(os.environ.get("LOOM_ENV_FILE", ".env"))), **os.environ}
        values = {k: v for k, v in environment.items() if any(s in k.lower() for s in ("key", "secret", "password", "authorization", "credential", "token"))}
        secrets = list(values.values())
        config = {"temporal_scope": "current_configuration_at_snapshot", "models": {}}
        if self.service.config_path:
            loaded = load_task_config(self.service.config_path)
            if not loaded.ok:
                raise ServiceError("Cannot load model configuration for diagnosis", 422)
            model_config = loaded.value
            config.update(default_model=model_config.default_model, verification_model=model_config.verification_model)
            for name, model in model_config.models.items():
                if model.api_key:
                    secrets.append(model.api_key)
                if environment.get(model.api_key_env):
                    secrets.append(environment[model.api_key_env])
                view = {
                    key: plain(getattr(model, key))
                    for key in (
                        "provider",
                        "model",
                        "base_url",
                        "api_key_env",
                        "temperature",
                        "max_completion_tokens",
                        "context_window_tokens",
                        "request_options",
                    )
                }
                view["api_key_present"] = bool(model.api_key or environment.get(model.api_key_env))
                config["models"][name] = view
        config["external_connections"] = []
        for identifier in sorted({b["connector_id"] for b in (state or {}).get("task", {}).get("external_tool_bindings", [])}):
            try:
                connection = self.service.store.external_tools.get(identifier)
                settings = connection["config"]
                env_names = set(settings.get("headers_env", {}).values()) | set(settings.get("env", {}).values())
                secrets.extend(environment[name] for name in env_names if environment.get(name))
                config["external_connections"].append({k: connection[k] for k in ("id", "name", "kind", "config")})
                config["external_connections"][-1]["credentials_present"] = {name: bool(environment.get(name)) for name in env_names}
            except ServiceError:
                config["external_connections"].append({"id": identifier, "unavailable": True})
        # Whitelisted model settings and bound connections only; never hand raw config to tools.
        secrets = sorted(
            {part for value in secrets for part in (value, value[7:] if value.lower().startswith("bearer ") else value) if part},
            key=len,
            reverse=True,
        )
        config = redact(config, secrets)
        return config, secrets

    def list(self, sid):
        state = self.service.store.snapshot(sid)
        config, _ = self._configuration()
        with self.lock:
            jobs = [self._public(self._sync(j)) for j in self._jobs(sid)]
        runs = list(dict.fromkeys(p["run_id"] for p in self.service.store.processes(sid)))
        return {
            "jobs": jobs,
            "runs": runs,
            "models": list(config["models"]),
            "default_model": state["task"].get("model") or config.get("default_model"),
            "budgets": DEFAULTS,
            "source_root": str(self.root) if self.root else None,
            "source_error": self.source_error,
            "data_dir": str(self.service.store.directory),
            "source_cursor": state["event_cursor"],
            "validation_backend": backend(),
            "default_mode": "worktree",
        }

    def start(self, sid, payload):
        if not isinstance(payload, dict) or set(payload) - {"request_id", "run_id", "question", "model", "budgets", "mode", "cleanup"}:
            raise ServiceError("Invalid diagnosis request")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ServiceError("A request_id is required for idempotent diagnosis creation")
        question = payload.get("question", "")
        if not isinstance(question, str) or len(question) > 4000:
            raise ServiceError("Question must contain at most 4000 characters")
        mode = payload.get("mode", "worktree")
        cleanup = payload.get("cleanup", "delete")
        if mode not in {"read_only", "worktree"} or cleanup not in {"delete", "keep"}:
            raise ServiceError("Select read_only/worktree mode and delete/keep cleanup")
        limits = budgets(payload.get("budgets", {}))
        state = self.service.store.snapshot(sid)
        fingerprint = hashlib.sha256(canonical({**payload, "budgets": limits}).encode()).hexdigest()
        with self.lock:
            for existing in self._jobs(sid):
                if existing["request_id"] == request_id:
                    if existing["request_fingerprint"] != fingerprint:
                        raise ServiceError("request_id already used for a different diagnosis", 409)
                    return self._public(self._sync(existing))
        if self.source_error:
            raise ServiceError(self.source_error, 422)
        config, secret_values = self._configuration(state)
        model = payload.get("model") or state["task"].get("model") or config.get("default_model")
        if model is not None and (not isinstance(model, str) or model not in config["models"]):
            raise ServiceError("Select a configured model")
        run = payload.get("run_id", "latest")
        if not isinstance(run, str):
            raise ServiceError("Invalid run_id")
        with self.lock:
            jobs = self._jobs()
            for existing in jobs:
                if existing["session_id"] == sid and existing["request_id"] == request_id:
                    if existing["request_fingerprint"] != fingerprint:
                        raise ServiceError("request_id already used for a different diagnosis", 409)
                    return self._public(self._sync(existing))
            if sum(j["state"] in ACTIVE for j in jobs) >= 5:
                raise ServiceError("Diagnosis queue is full (one active, four queued)", 429)
            jid = new_id("diag")
            directory = self.directory / jid
            job = {
                "id": jid,
                "session_id": sid,
                "request_id": request_id,
                "request_fingerprint": fingerprint,
                "state": "preparing",
                "model": model,
                "question": redact(question, secret_values),
                "budgets": limits,
                "created_at": now_iso(),
                "policy_version": 2,
                "mode": mode,
                "cleanup": cleanup,
                "runtime_session_id": None,
                "error": None,
            }
            self._save(job)
            try:
                view = capture(self.service.store, sid, directory, self.root, config, secret_values, requested_run=run)
                ledger = EvidenceView(directory).ledger()
                ledger["max_tool_calls"] = limits["max_tool_calls"]
                write_json(directory / "ledger.json", ledger)
                job.update(
                    source_cursor=view["source_cursor"],
                    target_run_ids=view["selected_run_ids"],
                    snapshot={k: v for k, v in view.items() if k not in {"state", "config"}},
                )
                workspace = None
                if mode == "worktree":
                    workspace = DiagnosisWorkspace.create(directory, self.root, jid, cleanup, excluded=[self.service.store.directory])
                    job["workspace"] = workspace.public()
                    self._save(job)
                # Creating idle first makes the durable job→private session binding visible before admission.
                receipt = self.runtime.create(
                    jid,
                    {
                        "title": "Session diagnosis",
                        "model": model,
                        "task_spec": specification(self.root, directory, workspace.path if workspace else None),
                        "limits": {k: v for k, v in limits.items() if k != "max_tool_calls"},
                    },
                )
                job.update(runtime_session_id=receipt["session_id"], state="queued")
                self._save(job)
                self.runtime.submit(
                    receipt["session_id"],
                    {"command_id": jid + "-start", "type": "submit_message", "payload": {"content": objective(job["question"], mode == "worktree")}},
                )
            except Exception as exc:
                job.update(state="failed", error=redact(str(exc), secret_values), finished_at=now_iso())
                self._settle_workspace(job)
                self._save(job)
                raise ServiceError(job["error"], 422) from exc
            return self._public(job)

    def _sync(self, job):
        if not job.get("runtime_session_id"):
            self._settle_workspace(job)
            self._save(job)
            return job
        state = self.runtime.store.snapshot(job["runtime_session_id"])
        run = state["run"] or {}
        counters = decode(run.get("counters", encode({})))
        usage = counters.get("usage")
        ledger = EvidenceView(self.directory / job["id"]).ledger()
        job["counters"] = {
            "steps": run.get("steps", 0),
            "model_calls": counters.get("llm_calls", 0),
            "tokens": getattr(usage, "total_tokens", 0),
            "tool_calls": ledger["tool_calls"],
            "read_chars": ledger["read_chars"],
            "seconds": round(run.get("active_seconds", 0), 1),
        }
        job["trace_cursor"] = state["event_cursor"]
        with self.runtime.store.transaction() as db:
            rows = db.execute(
                "SELECT body FROM events WHERE session_id=? AND seq>? AND seq<=? AND json_extract(body,'$.type') IN "
                "('operation.started','llm.requested','llm.completed','llm.failed','acceptance.verifying') ORDER BY seq",
                (state["session_id"], job.get("_audit_cursor", 0), state["event_cursor"]),
            )
            for row in rows:
                event = json.loads(row[0])
                payload = event["payload"]
                if payload.get("artifact", {}).get("kind") == "event_detail":
                    with suppress(ServiceError, OSError, ValueError):
                        payload = json.loads(self.runtime.store.artifacts.read(payload["artifact"]["sha256"]))
                audit = job.setdefault("_audit", {"tool_calls": 0, "model_calls": 0, "usage_missing": 0, "model_completed": 0})
                if event["type"] == "operation.started":
                    audit["tool_calls"] += 1
                    job["current_activity"] = payload.get("call", {}).get("name", "Reading evidence")
                elif event["type"] == "llm.requested":
                    audit["model_calls"] += 1
                    job["current_activity"] = "Reviewing evidence / planning next action"
                elif event["type"] in {"llm.completed", "llm.failed"}:
                    audit["model_completed"] += 1
                    if not payload.get("response", {}).get("usage", {}).get("total_tokens"):
                        audit["usage_missing"] += 1
                else:
                    job["current_activity"] = "Verifying diagnosis report and citations"
        job["_audit_cursor"] = state["event_cursor"]
        audit = job.get("_audit", {})
        job["counters"].update(
            tool_calls=audit.get("tool_calls", 0),
            evidence_reads=ledger["tool_calls"],
            model_calls=max(counters.get("llm_calls", 0), audit.get("model_calls", 0)),
            calls_without_reported_usage=audit.get("usage_missing", 0),
            unfinished_model_calls=max(0, audit.get("model_calls", 0) - audit.get("model_completed", 0)),
        )
        job["current_activity"] = run.get("reason") or job.get("current_activity", state["task"]["state"])
        job["acceptance"] = state.get("acceptance")
        if state["messages"] and state["messages"][-1]["role"] == "assistant" and run.get("state") != "completed":
            job["partial_report"] = state["messages"][-1]["content"]
        if job["state"] in {"cancelled", "completed", "failed"}:
            self._settle_workspace(job)
            self._save(job)
            return job
        if job.get("cancel_requested"):
            job["state"] = "cancelling" if job["runtime_session_id"] in self.runtime.active else "cancelled"
        elif run.get("state") == "completed":
            try:
                candidate = next(m["content"] for m in reversed(state["messages"]) if m["role"] == "assistant")
                report = validate_report(candidate, ledger)
                job.update(state="completed", report=report, markdown=markdown(report), finished_at=now_iso())
                with self.runtime.store.transaction() as db:
                    job["report_ref"] = self.runtime.store.artifact_in_transaction(db, state["session_id"], report, "diagnosis_report")
                    job["markdown_ref"] = self.runtime.store.artifact_in_transaction(db, state["session_id"], job["markdown"], "diagnosis_markdown")
            except (ValueError, StopIteration) as exc:
                job.update(state="failed", error="Invalid final diagnosis report: " + str(exc), finished_at=now_iso())
        elif state["task"]["state"] == "queued":
            job["state"] = "queued"
        elif state["task"]["state"] in {"running", "pausing"}:
            job["state"] = "running"
        else:
            reason = str(run.get("reason") or run.get("failure") or "Diagnosis paused; inspect its trace")
            status = "budget_exhausted" if "budget" in reason.lower() else "paused"
            if run.get("failure") or any(word in reason.lower() for word in ("shutdown", "shut down", "restarted", "worker exited")):
                status = "interrupted" if run.get("checkpoint") else "failed"
            job.update(state=status, error=redact(reason))
        self._settle_workspace(job)
        self._save(job)
        return job

    def _settle_workspace(self, job):
        workspace = DiagnosisWorkspace(self.directory / job["id"])
        if not workspace.meta:
            return
        try:
            stopped = job.get("runtime_session_id") not in self.runtime.active
            if stopped:
                workspace.reap_validation()
            if job["state"] in {"completed", "cancelled", "failed"} and stopped:
                if workspace.meta["state"] == "preparing":
                    workspace.discard()
                else:
                    workspace.finish()
                    if workspace.meta["state"] == "review" and workspace.meta["cleanup"] == "delete":
                        workspace.discard()
            if workspace.meta.get("patch_sha256") and job.get("runtime_session_id") and not job.get("changes_ref"):
                with self.runtime.store.transaction() as db:
                    job["changes_ref"] = self.runtime.store.artifact_in_transaction(
                        db,
                        job["runtime_session_id"],
                        {
                            "patch": (workspace.directory / "changes.patch").read_text(errors="replace"),
                            "files": workspace.meta["files"],
                            "patch_sha256": workspace.meta["patch_sha256"],
                            "baseline": workspace.meta["baseline"],
                            "checks": workspace.meta["checks"],
                        },
                        "diagnosis_changes",
                    )
            job.pop("workspace_error", None)
        except (ValueError, OSError) as exc:
            job["workspace_error"] = redact(str(exc))
        job["workspace"] = workspace.public()

    def changes(self, sid, jid):
        with self.lock:
            job = self._sync(self._load(sid, jid))
            workspace = DiagnosisWorkspace(self.directory / jid)
            if not workspace.meta or not workspace.meta.get("patch_sha256"):
                raise ServiceError("Changes are available after diagnosis stops", 409)
            return {"workspace": job["workspace"], "patch": (workspace.directory / "changes.patch").read_text(errors="replace")}

    def workspace_action(self, sid, jid, action, payload):
        with self.lock, self.service.lock:
            job = self._sync(self._load(sid, jid))
            if job["state"] not in {"completed", "cancelled", "failed"} or job.get("runtime_session_id") in self.runtime.active:
                raise ServiceError("Stop the diagnosis and wait for its worker before managing changes", 409)
            workspace = DiagnosisWorkspace(self.directory / jid)
            if not workspace.meta:
                raise ServiceError("This diagnosis has no worktree", 409)
            try:
                if action == "merge":
                    if not isinstance(payload, dict) or set(payload) != {"patch_sha256"}:
                        raise ServiceError("Merge requires the reviewed patch_sha256")
                    if self.service.active or self.runtime.active:
                        raise ServiceError("Wait for active session workers before merging source changes", 409)
                    workspace.merge(payload["patch_sha256"])
                else:
                    if payload != {}:
                        raise ServiceError("Discard accepts an empty object")
                    # Never delete unarchived work; finish must have saved the patch first.
                    if workspace.meta["state"] == "active":
                        workspace.finish()
                    workspace.discard()
            except (ValueError, OSError) as exc:
                raise ServiceError(str(exc), 409) from exc
            job["workspace"] = workspace.public()
            self._save(job)
            return self._public(job)

    def _public(self, job):
        return {k: v for k, v in job.items() if not k.startswith("_") and k not in {"runtime_session_id", "request_fingerprint", "request_id"}}

    def get(self, sid, jid):
        with self.lock:
            job = self._sync(self._load(sid, jid))
            value = self._public(job)
            value["stale"] = self.service.store.snapshot(sid)["event_cursor"] > job.get("source_cursor", 0)
            return value

    def events(self, sid, jid, after=0):
        job = self._load(sid, jid)
        if not job.get("runtime_session_id"):
            return {"events": [], "next_after": after}
        events = self.runtime.events(job["runtime_session_id"], after, 200)
        return redact({"events": events, "next_after": events[-1]["seq"] if events else after})

    def evidence(self, sid, jid, ref):
        self._load(sid, jid)
        page = EvidenceView(self.directory / jid).ledger()["entries"].get(ref)
        if page is None:
            raise ServiceError("Evidence not read by this diagnosis", 404)
        return page

    def artifact(self, sid, jid, digest):
        job = self._load(sid, jid)
        if not job.get("runtime_session_id"):
            raise ServiceError("Diagnosis artifact not found", 404)
        return redact(json.loads(self.runtime.store.read_artifact(job["runtime_session_id"], digest)))

    def cancel(self, sid, jid):
        with self.lock:
            job = self._sync(self._load(sid, jid))
            if job["state"] in {"completed", "failed", "cancelled"}:
                return self._public(job)
            job.update(cancel_requested=True, state="cancelling")
            self._save(job)
            self.runtime.submit(job["runtime_session_id"], {"command_id": jid + "-cancel", "type": "pause", "payload": {}})
            return self._public(self._sync(job))

    def resume(self, sid, jid, payload):
        if not isinstance(payload, dict) or set(payload) != {"budgets"}:
            raise ServiceError("Resume requires budgets")
        with self.lock:
            job = self._sync(self._load(sid, jid))
            if job["state"] not in {"paused", "budget_exhausted", "interrupted"}:
                raise ServiceError("Only a paused diagnosis can resume", 409)
            limits = budgets(payload["budgets"], job["budgets"])
            used = job["counters"]
            if (
                limits["max_duration_seconds"] <= used["seconds"]
                or limits["max_tokens"] <= used["tokens"]
                or limits["max_llm_calls"] <= used["model_calls"]
                or limits["max_steps"] <= used["steps"]
                or limits["max_tool_calls"] <= used["tool_calls"]
            ):
                raise ServiceError("Increase exhausted total budgets before resuming", 409)
            with self.runtime.lock:
                if job["runtime_session_id"] in self.runtime.active:
                    raise ServiceError("Wait for the diagnosis worker to stop", 409)

                def update(state, emit, db):
                    state["task"]["limits"].update({k: v for k, v in limits.items() if k != "max_tool_calls"})
                    state["task"]["limits"]["max_duration_seconds"] = max(1, int(limits["max_duration_seconds"] - used["seconds"]))
                    emit("diagnosis.budget.changed", {"total_budgets": limits})

                self.runtime.store.update(job["runtime_session_id"], update)
                ledger = EvidenceView(self.directory / jid).ledger()
                ledger["max_tool_calls"] = limits["max_tool_calls"]
                write_json(self.directory / jid / "ledger.json", ledger)
                self.runtime.submit(job["runtime_session_id"], {"command_id": new_id("resume"), "type": "resume", "payload": {}})
            job.update(budgets=limits, state="queued", error=None)
            self._save(job)
            return self._public(job)

    def before_delete(self, sid):
        with self.lock:
            try:
                self.service.store.snapshot(sid)
            except ServiceError as exc:
                if exc.status == 404:
                    return
                raise
            for job in self._jobs(sid):
                self.cancel(sid, job["id"])
            if any(j["runtime_session_id"] in self.runtime.active for j in self._jobs(sid)):
                raise ServiceError("Diagnosis is stopping; retry deletion shortly", 409)

    def after_delete(self, sid):
        with self.lock:
            for job in self._jobs(sid):
                if job.get("runtime_session_id"):
                    self.runtime.delete(job["runtime_session_id"])
                directory = self.directory / job["id"]
                workspace = DiagnosisWorkspace(directory)
                if workspace.meta:
                    workspace.reap_validation()
                    workspace.discard()
                if directory.is_dir() and directory.parent == self.directory:
                    shutil.rmtree(directory)
            with self.service.store.transaction() as db:
                db.execute("DELETE FROM diagnosis_jobs WHERE session_id=?", (sid,))

    def _monitor(self):
        while not self.stopping.wait(0.5):
            with self.lock:
                for job in self._jobs():
                    if (
                        job["state"] not in {"completed", "cancelled", "failed"}
                        or (job.get("mode") == "worktree" and job.get("workspace", {}).get("state") in {None, "preparing", "active"})
                        or job.get("workspace_error")
                    ):
                        try:
                            if job["state"] == "preparing" and not job.get("runtime_session_id"):
                                job.update(state="failed", error="Snapshot preparation was interrupted", finished_at=now_iso())
                                self._settle_workspace(job)
                                self._save(job)
                            else:
                                self._sync(job)
                        except (ServiceError, OSError, ValueError) as exc:
                            job.update(error=redact(str(exc)))
                            self._save(job)

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=3)
        self.runtime.close()
        for job in self._jobs():
            self._settle_workspace(job)
            self._save(job)
        self.service.shared_active = lambda: {}
