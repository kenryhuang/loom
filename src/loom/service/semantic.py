"""Durable bounded semantic evaluation jobs, isolated from solver events and budgets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

from loom.core import err, make_loom_error, now_iso, ok
from loom.evaluation.diagnostics import ANALYZER_VERSION, DIMENSIONS, PROMPT_VERSION, FactAnalysis
from loom.evaluation.effectiveness_judge import judge_effectiveness, unverified_criteria
from loom.evaluation.goal_loops import ANALYZER as GOAL_ANALYZER
from loom.evaluation.goal_loops import ENGINE as GOAL_ENGINE
from loom.evaluation.goal_loops import PROMPT as GOAL_PROMPT
from loom.evaluation.goal_loops.execution import UPGRADABLE_VERSIONS
from loom.evaluation.goal_loops.graph_contract import ANALYZER as GRAPH_ANALYZER
from loom.evaluation.goal_loops.graph_contract import ENGINE as GRAPH_ENGINE
from loom.evaluation.goal_loops.graph_contract import PROMPT as GRAPH_PROMPT
from loom.evaluation.goal_loops.graph_contract import RESOURCE_LIMITS as GRAPH_RESOURCE_LIMITS
from loom.evaluation.goal_loops.graph_contract import UPGRADABLE_VERSIONS as GRAPH_UPGRADABLE_VERSIONS
from loom.evaluation.issues import EvaluationFailure, describe, failure_state, issue, request_budget_failure, request_failure_event
from loom.evaluation.legacy_issues import legacy_job_state
from loom.evaluation.provider import create_evaluation_provider
from loom.evolution.diagnoses import proposals_from_diagnoses
from loom.memory.store import bound_workspace
from loom.service.contracts import ServiceError, canonical, new_id
from loom.service.evaluation_artifacts import EvaluationArtifacts
from loom.service.evaluation_progress import finish_request, present_legacy_request_failure
from loom.service.insights import build_insights
from loom.tasks.config import load_task_config

GOAL_ENGINES = {GOAL_ENGINE, GRAPH_ENGINE}

VERSION = "session-semantic.1"
EVALUATION_WORKERS = 2
MAX_ACTIVE_EVALUATIONS = 4
RESOURCE_LIMITS = {"max_calls", "max_tokens", "max_seconds"}
DEFAULTS = {
    "max_calls": 160,
    "max_tokens": 2000000,
    "max_seconds": 3600,
    "max_evidence_chars": 512 * 1024,
    "max_prompt_chars": 200000,
    "batch_rounds": 8,
    "max_read_rounds": 8,
    "max_rounds": 20000,
    "max_call_seconds": 240,
}
V3_DEFAULTS = {
    **DEFAULTS,
    "max_calls": 48,
    "max_tokens": 240000,
    "max_rounds": 16,
    "max_read_rounds": 2,
    "max_prompt_chars": 96000,
}
GOAL_DEFAULTS = {**V3_DEFAULTS, "max_read_rounds": 4}
GRAPH_DEFAULTS = {
    **GOAL_DEFAULTS,
    "max_calls": 48,
    "max_tokens": 480000,
    "max_prompt_chars": 512 * 1024,
    "max_call_seconds": 180,
}
BOUNDS = {
    "max_calls": (1, 500),
    "max_tokens": (100, 10000000),
    "max_seconds": (1, 7200),
    "max_evidence_chars": (1000, 5000000),
    "max_prompt_chars": (10000, 1000000),
    "batch_rounds": (1, 16),
    "max_read_rounds": (0, 10),
    "max_rounds": (1, 20000),
    "max_call_seconds": (1, 600),
}


def resource_limits(version):
    return GRAPH_RESOURCE_LIMITS if version == GRAPH_ENGINE else RESOURCE_LIMITS


def _behavior_facts(source, factual, body):
    from loom.evaluation.behavior_contracts import ANALYZER_VERSION as current_version
    from loom.evaluation.behavior_facts import build_behavior_facts

    existing = factual.get("behavior")
    if existing and (
        body.get("analyzer_version") not in {current_version, GOAL_ANALYZER, GRAPH_ANALYZER} or existing.get("analyzer_version") == current_version
    ):
        return existing
    return build_behavior_facts(source, source_coverage=factual.get("summary", {}).get("coverage"))


class SessionSemantic:
    def __init__(self, trajectory, config_path=None, *, provider_factory=None):
        self.trajectory, self.store = trajectory, trajectory.store
        self.config_path, self.provider_factory = config_path, provider_factory
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.cancels = {}
        self.futures = {}
        self.pool = ThreadPoolExecutor(max_workers=EVALUATION_WORKERS, thread_name_prefix="loom-evaluation")
        with self.store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS semantic_jobs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, cache_key TEXT UNIQUE, body TEXT NOT NULL)")
            for row in db.execute("SELECT id,body FROM semantic_jobs").fetchall():
                body = json.loads(row["body"])
                if body["state"] in {"queued", "running"}:
                    body.update(state="interrupted", error="Service restarted. Resume reuses completed batches.")
                    db.execute("UPDATE semantic_jobs SET body=? WHERE id=?", (canonical(body), row["id"]))
        self.outputs = EvaluationArtifacts(self.store)

    def options(self):
        models, default = [], None
        if self.provider_factory:
            models, default = [{"id": "test", "label": "test"}], "test"
        elif self.config_path:
            loaded = load_task_config(self.config_path)
            if loaded.ok:
                default = loaded.value.evaluation_model or loaded.value.default_model
                models = [{"id": key, "label": key if "/" in key else f"{key} · {value.model}"} for key, value in loaded.value.models.items()]
        return {
            "supports_delete": True,
            "supports_memory": True,
            "memory_default": True,
            "models": models,
            "default_model": default,
            "defaults": GRAPH_DEFAULTS,
            "defaults_by_version": {"v2": DEFAULTS, "v3": V3_DEFAULTS, GOAL_ENGINE: GOAL_DEFAULTS, GRAPH_ENGINE: GRAPH_DEFAULTS},
            "bounds": BOUNDS,
            "analysis_versions": [GRAPH_ENGINE, GOAL_ENGINE, "v3", "v2"],
            "default_analysis_version": GRAPH_ENGINE,
        }

    def close(self):
        self.closed.set()
        self.pool.shutdown(wait=True)

    def _save(self, body):
        with self.store.transaction() as db:
            db.execute("UPDATE semantic_jobs SET body=? WHERE id=?", (canonical(body), body["id"]))
            db.execute("INSERT OR IGNORE INTO skill_dirty_sessions VALUES(?)", (body['session_id'],))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_versions'").fetchone():
                db.execute("UPDATE memory_versions SET value=value+1 WHERE id='effective'")

    def _load(self, sid, aid, identifier):
        self.trajectory._load(sid, aid)
        with self.store.transaction() as db:
            row = db.execute("SELECT body FROM semantic_jobs WHERE id=? AND session_id=?", (identifier, sid)).fetchone()
        if not row:
            raise ServiceError("Evaluation not found", 404)
        body = json.loads(row[0])
        if body["analysis_id"] != aid:
            raise ServiceError("Evaluation belongs to another snapshot", 404)
        return body

    def list(self, sid, aid):
        parent = self.trajectory._load(sid, aid)
        with self.store.transaction() as db:
            rows = [json.loads(row[0]) for row in db.execute("SELECT body FROM semantic_jobs WHERE session_id=? ORDER BY rowid DESC", (sid,))]
        runs = []
        if parent["state"] == "completed":
            source, _ = self.trajectory._data(parent)
            runs = list(dict.fromkeys(e.run_id for e in source.events if e.run_id))
        return {"jobs": [self._public(row) for row in rows if row["analysis_id"] == aid], "run_ids": runs, **self.options()}

    def _public(self, body):
        public = {k: v for k, v in body.items() if k not in {"checkpoint", "config_digest"}}
        # Present historical combined errors precisely without rewriting checkpoints.
        legacy_budget = next((e for e in body.get('error_details', []) if e['code'] == 'budget.calls_or_time'), None)
        if legacy_budget and body.get('state') == 'budget_exhausted':
            failure = request_budget_failure(body['usage'], body['settings'],
                body['settings']['max_seconds'] - body['elapsed_seconds'],
                stage=legacy_budget.get('stage', 'graph'), unit_id=legacy_budget.get('unit_id'))
            if failure:
                public.update(error=str(failure), error_details=failure.issues)
        if body.get('analysis_version') == GRAPH_ENGINE and body.get('checkpoint', {}).get('entities'):
            from loom.evaluation.work_items import status

            public.update(status(body['checkpoint']))
            if body['state'] in {'queued', 'running', 'failed', 'cancelled'}:
                public['execution_state'] = body['state']
            elif body['state'] in {'budget_exhausted', 'interrupted'}:
                public['execution_state'] = 'paused'
        if public["state"] == "failed" and not public.get("error_details"):
            public["state"] = legacy_job_state(public.get("error") or "")
        return present_legacy_request_failure(public)

    def get(self, sid, aid, identifier):
        body = self._load(sid, aid, identifier)
        result = self._public(body)
        if body["state"] == "queued":
            result["queue"] = self._queue_info(identifier)
        if body.get("analysis_version") == GRAPH_ENGINE and body.get("checkpoint", {}).get("entities"):
            from loom.evaluation.goal_loops.graph_projection import project

            source, factual = self.trajectory._data(self.trajectory._load(sid, aid))
            semantic = project(source, _behavior_facts(source, factual, body), body["run_id"], body["checkpoint"])
            result["evaluation"] = self._result(body, source, factual, semantic)
        elif body.get("result"):
            result["evaluation"] = json.loads(self.store.artifacts.read(body["result"]["sha256"]))
            if body.get("analysis_version") in GOAL_ENGINES:
                from loom.evaluation.goal_loops.execution_view import VERSION as execution_version
                from loom.evaluation.goal_loops.execution_view import execution_view

                bundle = result["evaluation"]
                if bundle.get("execution_view", {}).get("version") != execution_version:
                    source, _ = self.trajectory._data(self.trajectory._load(sid, aid))
                    bundle["execution_view"] = execution_view(source, bundle)
            semantic = result["evaluation"].get("semantic", {})
            process = semantic.get("reasoning_process")
            if body.get("analysis_version") == "v3" and process:
                from loom.evaluation.reasoning_graph import VERSION as graph_version

                if process.get("graph", {}).get("schema_version") != graph_version:
                    from loom.evaluation.reasoning_metrics import build_reasoning_process

                    _, factual = self.trajectory._data(self.trajectory._load(sid, aid))
                    semantic["reasoning_process"] = build_reasoning_process(factual["behavior"], semantic)
                    semantic["reasoning_process"]["graph"]["projection_basis"] = "pinned_source_and_saved_judgments_no_model_call"
        elif body.get("analysis_version") in GOAL_ENGINES:
            if body["analysis_version"] == GRAPH_ENGINE:
                from loom.evaluation.goal_loops.graph_projection import project
            else:
                from loom.evaluation.goal_loops.review import project

            source, factual = self.trajectory._data(self.trajectory._load(sid, aid))
            semantic = project(source, _behavior_facts(source, factual, body), body["run_id"], body["checkpoint"])
            result["evaluation"] = self._result(body, source, factual, semantic)
        elif body.get("analysis_version") == "v3" and body.get("checkpoint"):
            # Expose validated local results while subsequent stages are still running.
            from loom.evaluation.behavior_judge import partial_semantic

            source, factual = self.trajectory._data(self.trajectory._load(sid, aid))
            facts = _behavior_facts(source, factual, body)
            factual = {**factual, "behavior": facts}
            result["evaluation"] = self._result(body, source, factual, partial_semantic(facts, body["checkpoint"]))
        elif body["state"] not in {"queued", "running"} and body["checkpoint"].get("batches"):
            source, factual = self.trajectory._data(self.trajectory._load(sid, aid))
            outputs = [item["result"] for item in body["checkpoint"]["batches"].values()]
            reviews = {row["round_id"]: row for output in outputs for row in output.get("round_analyses", [])}
            semantic = {
                "diagnoses": [d for output in outputs for d in output["diagnoses"]],
                "verification": list(unverified_criteria(FactAnalysis(**factual["facts"]))),
                "preserved_behaviors": [p for output in outputs for p in output["preserved_behaviors"]],
                "verification_framework": [],
                "round_analyses": list(reviews.values()),
                "usage": body["usage"],
                "coverage": {
                    "status": "incomplete",
                    "limitations": [body.get("error") or "Evaluation unfinished"],
                    "round_dimension_statuses": {
                        d: {
                            status: sum(r["dimensions"][d]["status"] == status for r in reviews.values())
                            for status in ("effective", "ineffective", "mixed", "unknown")
                        }
                        for d in DIMENSIONS
                    },
                },
            }
            result["evaluation"] = self._result(body, source, factual, semantic)
        return result

    def memory_epoch(self, sid, aid, identifier, epoch):
        """Read-only archived memory view; never roll live runtime memory back."""
        from loom.evaluation.memory.projection import project_memory

        body = self._load(sid, aid, identifier)
        cp = body.get('checkpoint', {})
        saved = next((r for r in cp.get('analysis_epochs', []) if r['kind'] == 'memory' and str(r['epoch']) == str(epoch)), None)
        if saved is None:
            raise ServiceError('Memory epoch not found', 404)
        snapshot = saved['snapshot']
        stages = {s['id']: s for u in snapshot['units'].values() for s in u['scope']['stages']}
        return dict(evaluation_id=identifier, epoch=saved['epoch'], read_only=True,
                    memory=project_memory(dict(memory=snapshot, memory_requested=True, stages=stages,
                        graph_revision=snapshot['identity']['graph_revision'], extraction=cp.get('extraction', {}))))

    def _queue_info(self, identifier):
        with self.store.transaction() as db:
            jobs = [
                json.loads(row[0])
                for row in db.execute("SELECT body FROM semantic_jobs WHERE json_extract(body,'$.state') IN ('queued','running') ORDER BY rowid")
            ]
        queued = [job["id"] for job in jobs if job["state"] == "queued"]
        return {
            "position": queued.index(identifier) + 1 if identifier in queued else None,
            "workers": EVALUATION_WORKERS,
            "running": [{key: job.get(key) for key in ("id", "session_id", "model", "stage")} for job in jobs if job["state"] == "running"],
        }

    @staticmethod
    def _result(body, source, factual, semantic):
        if body.get("analysis_version") in GOAL_ENGINES:
            report = semantic.get("report")
            if report and isinstance(report.get("text"), str):
                report = {**report, "text": f"Session: {body['session_id']}\n\n" + report["text"]}
            return {
                **semantic,
                **({"report": report} if report else {}),
                "session_id": body["session_id"],
                "analysis_id": body["id"],
                "source": {**semantic["source"], "session_id": body["session_id"],
                           "analysis_id": body["analysis_id"], "snapshot_boundary": body["source_cursor"]},
                "analyzer": {
                    **semantic["analyzer"],
                    "model_config_digest": body["config_digest"],
                    "semantic_settings_digest": hashlib.sha256(
                        canonical({k: v for k, v in body["settings"].items() if k not in resource_limits(body["analysis_version"])}).encode()
                    ).hexdigest(),
                },
                "evaluator_usage": {**semantic["evaluator_usage"], "elapsed_seconds": body["elapsed_seconds"]},
                "evaluator": {"model": body["model"], "config_digest": body["config_digest"]},
            }
        if body.get("analysis_version") == "v3":
            from loom.evaluation.behavior import evaluation_payload

            return evaluation_payload(
                source, _behavior_facts(source, factual, body), semantic, model=body["model"], checkpoint_identity=body["checkpoint"].get("identity")
            )
        return {
            "semantic": semantic,
            "insights": build_insights(factual["summary"], semantic, factual["facts"]),
            "proposals": proposals_from_diagnoses(
                semantic["diagnoses"], source_sha256=source.source_sha256, evaluator={"model": body["model"], "prompt_version": PROMPT_VERSION}
            ),
            "source_sha256": source.source_sha256,
        }

    def start(self, sid, aid, options, *, budget_context=None, resume_request_id=None):
        allowed = {"model", "resume_id", "retry_failed", "new_evaluation", "analysis_version", "run_id", "extract_memory", *DEFAULTS}
        if not isinstance(options, dict) or set(options) - allowed:
            raise ServiceError("Invalid evaluation options")
        resume_id = options.get("resume_id")
        if resume_id is not None and (not isinstance(resume_id, str) or not resume_id):
            raise ServiceError("Invalid resume_id")
        create_new = options.get("new_evaluation", False)
        if type(create_new) is not bool:
            raise ServiceError("new_evaluation must be a boolean")
        if create_new and resume_id:
            raise ServiceError("new_evaluation and resume_id cannot be used together")
        resumed = self._load(sid, aid, resume_id) if resume_id else None
        retry_failed_items = options.get('retry_failed', False)
        if type(retry_failed_items) is not bool or (retry_failed_items and not resumed):
            raise ServiceError('retry_failed requires a saved evaluation and must be a boolean')
        # Explicit new histories have independent cache identities, retained on resume.
        # Requests without this flag preserve the existing deduplication contract.
        instance_id = new_id("evaluation") if create_new else resumed.get("instance_id") if resumed else None
        version = options.get("analysis_version", resumed.get("analysis_version", "v2") if resumed else "v3")
        if version not in {"v2", "v3", *GOAL_ENGINES}:
            raise ServiceError("Unsupported analysis_version")
        from loom.evaluation.behavior_contracts import ANALYZER_VERSION as V3_ANALYZER
        from loom.evaluation.behavior_contracts import PROMPT_VERSION as V3_PROMPT

        analyzer_version = (
            GRAPH_ANALYZER if version == GRAPH_ENGINE else GOAL_ANALYZER if version == GOAL_ENGINE else V3_ANALYZER if version == "v3" else ANALYZER_VERSION
        )
        prompt_version = (
            GRAPH_PROMPT if version == GRAPH_ENGINE else GOAL_PROMPT if version == GOAL_ENGINE else V3_PROMPT if version == "v3" else PROMPT_VERSION
        )
        settings = {
            **(GRAPH_DEFAULTS if version == GRAPH_ENGINE else GOAL_DEFAULTS if version == GOAL_ENGINE else V3_DEFAULTS if version == "v3" else DEFAULTS),
            **(resumed["settings"] if resumed else {}),
        }
        if "extract_memory" in options:
            if version != GRAPH_ENGINE or type(options["extract_memory"]) is not bool:
                raise ServiceError("extract_memory must be a boolean for incremental graph evaluation")
            settings["extract_memory"] = options["extract_memory"]
        for key, value in options.items():
            if key in {"model", "resume_id", "retry_failed", "new_evaluation", "analysis_version", "run_id", "extract_memory"}:
                continue
            low, high = BOUNDS[key]
            if type(value) is not int or not low <= value <= high:
                raise ServiceError(f"{key} must be an integer between {low} and {high}")
            settings[key] = value
        if version == GOAL_ENGINE and settings["max_prompt_chars"] < 20000:
            raise ServiceError("Goal-loop max_prompt_chars must be at least 20000 to fit the structured protocol and evidence")
        available = self.options()
        model = options.get("model") or (resumed["model"] if resumed else available["default_model"])
        if isinstance(model, str) and self.config_path:
            configured = load_task_config(self.config_path)
            if configured.ok:
                model = configured.value.resolve_model_name(model)
        if not isinstance(model, str) or model not in {row["id"] for row in available["models"]}:
            raise ServiceError("Select a configured judge model")
        parent = self.trajectory._load(sid, aid)
        if parent["state"] != "completed":
            raise ServiceError("Wait for factual analysis to complete", 409)
        run_id = options.get("run_id", resumed.get("run_id") if resumed else None)
        if version in GOAL_ENGINES:
            source, _ = self.trajectory._data(parent)
            if not isinstance(run_id, str) or run_id not in {e.run_id for e in source.events if e.run_id}:
                raise ServiceError("Select a run recorded in this snapshot")
        elif run_id is not None:
            raise ServiceError("run_id is supported only for goal-loop analysis")
        digest = hashlib.sha256(Path(self.config_path).read_bytes()).hexdigest() if self.config_path else "test"
        identity = {
            "source": parent["source"]["sha256"],
            "model": model,
            "config": digest,
            "version": VERSION,
            "analysis_version": version,
            "analyzer": analyzer_version,
            "prompt": prompt_version,
            "settings": settings,
            "session": sid,
            "analysis": aid,
            **({"instance_id": instance_id} if instance_id else {}),
            **({"run_id": run_id} if version in GOAL_ENGINES else {}),
        }
        key = hashlib.sha256(canonical(identity).encode()).hexdigest()
        with self.lock, self.store.transaction() as db:
            session = self.store._load(db, sid)
            if self.closed.is_set():
                raise ServiceError("Service is closing", 503)
            row = db.execute("SELECT body FROM semantic_jobs WHERE cache_key=?", (key,)).fetchone()
            previous = json.loads(row[0]) if row else None
            if resumed:
                if previous and previous["id"] != resume_id:
                    raise ServiceError("An evaluation with these settings already exists; select it from history", 409)
                saved = db.execute("SELECT body FROM semantic_jobs WHERE id=?", (resume_id,)).fetchone()
                if not saved:
                    raise ServiceError("Evaluation not found", 404)
                resumed = json.loads(saved[0])
                if resume_request_id and resume_request_id in resumed.get("host_resume_requests", []):
                    return self._public(resumed)
                if resumed["state"] in {"queued", "running", "completed"}:
                    raise ServiceError("Only stopped evaluations can be resumed", 409)
                protocol_upgrade = version == GOAL_ENGINE and (resumed["analyzer_version"], resumed["prompt_version"]) in UPGRADABLE_VERSIONS
                protocol_upgrade = protocol_upgrade or (
                    version == GRAPH_ENGINE and (resumed["analyzer_version"], resumed["prompt_version"]) in GRAPH_UPGRADABLE_VERSIONS
                )
                if (
                    model != resumed["model"]
                    or digest != resumed["config_digest"]
                    or resumed.get("analysis_version", "v2") != version
                    or resumed.get("run_id") != run_id
                    or (not protocol_upgrade and (resumed["analyzer_version"] != analyzer_version or resumed["prompt_version"] != prompt_version))
                    or any(settings[k] != resumed["settings"].get(k, DEFAULTS[k]) for k in DEFAULTS if k not in resource_limits(version))
                    or settings.get("extract_memory", False) != resumed["settings"].get("extract_memory", False)
                ):
                    raise ServiceError("Resume must keep the same model, snapshot and analysis settings; only resource limits may change")
                if any(settings[k] < resumed["settings"][k] for k in resource_limits(version) if k != "max_tokens" or version != GRAPH_ENGINE):
                    raise ServiceError("Resuming cannot reduce resource limits")
                if version == GRAPH_ENGINE:
                    from loom.evaluation.goal_loops.incremental import upgrade_budget_identity
                    from loom.evaluation.work_items import retry_failed, status

                    upgrade_budget_identity(resumed["checkpoint"], resumed["settings"])
                    work_status = status(resumed['checkpoint'])
                    if retry_failed_items:
                        if not work_status['retryable_work']:
                            raise ServiceError('No settled failed items are available to retry', 409)
                        retry_failed(resumed['checkpoint'])
                    elif work_status['execution_state'] == 'completed' and work_status['retryable_work']:
                        raise ServiceError('Processing has finished with quality gaps. Use Retry failed items instead of Resume.', 409)
                previous = resumed
            if previous and previous["state"] in {"queued", "running", "completed"}:
                return self._public(previous)
            active = db.execute("SELECT COUNT(*) FROM semantic_jobs WHERE json_extract(body,'$.state') IN ('queued','running')").fetchone()[0]
            if active >= MAX_ACTIVE_EVALUATIONS:
                raise ServiceError("Evaluation queue is full", 429)
            body = previous or {
                "id": instance_id or new_id("evaluation"),
                **({"instance_id": instance_id} if instance_id else {}),
                "session_id": sid,
                "memory_scope": {"workspace": bound_workspace(session["task"])},
                "analysis_id": aid,
                "source_cursor": parent["source_cursor"],
                "model": model,
                "settings": settings,
                **({"run_id": run_id} if version in GOAL_ENGINES else {}),
                "config_digest": digest,
                "analysis_version": version,
                "analyzer_version": analyzer_version,
                "prompt_version": prompt_version,
                "created_at": now_iso(),
                "checkpoint": {},
                "usage": {"calls": 0, "total_tokens": 0, "unreported_calls": 0},
                "elapsed_seconds": 0,
                **({'autonomy_budget': budget_context} if budget_context else {}),
            }
            consumed = {"max_calls": body["usage"]["calls"], "max_tokens": body["usage"]["total_tokens"], "max_seconds": body["elapsed_seconds"]}
            exhausted = [k for k, value in consumed.items() if value >= settings[k] and (k != "max_tokens" or version != GRAPH_ENGINE)]
            if exhausted:
                limits = ", ".join(f"{k} > {consumed[k]}" for k in exhausted)
                raise ServiceError(f"Budget exhausted. Increase total limits ({limits}) to resume saved batches", 409)
            if body.get("result"):
                self.outputs.track(db, body, body["result"])
            body.pop("result", None)
            body.update(state="queued", error=None, error_details=[], settings=settings, analyzer_version=analyzer_version, prompt_version=prompt_version)
            if resume_request_id:
                body.setdefault("host_resume_requests", []).append(resume_request_id)
            db.execute("INSERT OR REPLACE INTO semantic_jobs VALUES(?,?,?,?)", (body["id"], sid, key, canonical(body)))
            self.cancels[body["id"]] = threading.Event()
            self.futures[body["id"]] = self.pool.submit(self._work, body)
            return self._public(body)

    def delete(self, sid, aid, identifier):
        self.trajectory._load(sid, aid)
        with self.lock:
            with self.store.transaction() as db:
                row = db.execute("SELECT body FROM semantic_jobs WHERE id=? AND session_id=?", (identifier, sid)).fetchone()
                if row:
                    body = json.loads(row[0])
                    if body["analysis_id"] != aid:
                        raise ServiceError("Evaluation belongs to another snapshot", 404)
                    future = self.futures.get(identifier)
                    if body["state"] in {"queued", "running"} or (future and not future.done()):
                        raise ServiceError("Cancel the evaluation and wait for it to stop before deleting", 409)
                    self.outputs.prepare(db, body)
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_versions'").fetchone():
                        db.execute("UPDATE memory_versions SET value=value+1 WHERE id='effective'")
                    db.execute('INSERT OR IGNORE INTO skill_dirty_sessions VALUES(?)', (sid,))
                elif not db.execute(
                    "SELECT 1 FROM evaluation_deletions WHERE id=? AND session_id=? AND analysis_id=?", (identifier, sid, aid)
                ).fetchone():
                    raise ServiceError("Evaluation not found", 404)
            try:
                self.outputs.finish(identifier)
            except OSError as exc:
                raise ServiceError("Evaluation metadata deleted; file cleanup pending. Retry deletion to finish cleanup.", 503) from exc
            self.cancels.pop(identifier, None)
            self.futures.pop(identifier, None)
        return {"id": identifier, "deleted": True}

    def cancel(self, sid, aid, identifier):
        body = self._load(sid, aid, identifier)
        with self.lock:
            flag = self.cancels.get(identifier)
            if flag:
                flag.set()
            future = self.futures.get(identifier)
            if future and future.cancel():
                body.update(state="cancelled", error="Evaluation cancelled before starting", current_call=None)
                body["progress"] = [
                    *body.get("progress", []),
                    {
                        "kind": "cancelled",
                        "message": body["error"],
                        "at": now_iso(),
                        "elapsed_seconds": body["elapsed_seconds"],
                    },
                ][-100:]
                self._save(body)
                self.futures.pop(identifier, None)
                self.cancels.pop(identifier, None)
        return self._public(body)

    def _work(self, body):
        started = time.monotonic()
        prior_elapsed = body["elapsed_seconds"]
        stop = self.cancels[body["id"]]
        settings = body["settings"]

        def progress(kind, message):
            body["progress"] = [
                *body.get("progress", []),
                {"kind": kind, "message": message, "at": now_iso(), "elapsed_seconds": prior_elapsed + round(time.monotonic() - started, 2)},
            ][-100:]

        def persist():
            body["elapsed_seconds"] = prior_elapsed + round(time.monotonic() - started, 2)
            self._save(body)

        def interruption():
            if self.closed.is_set():
                return "interrupted", "runtime.interrupted", "Service is shutting down; evaluator request interrupted; accepted records retained"
            if stop.is_set():
                return "cancelled", "runtime.cancelled", "Cancellation requested; evaluator request cancelled; accepted records retained"
            if prior_elapsed + time.monotonic() - started >= settings["max_seconds"]:
                return "budget_exhausted", "budget.seconds", f"Evaluation time budget reached ({settings['max_seconds']}s); accepted records retained"
            return None

        def failed_request(error, code=None, state="failed"):
            event = request_failure_event({}, error)
            reason, code = event["error"], code or event["error_code"]
            stopped = interruption()
            if code == "request.cancelled" or (code == "request.timeout" and stopped and stopped[0] == "budget_exhausted"):
                if stopped:
                    state, code, reason = stopped
                else:
                    state = "cancelled"
            message = finish_request(body, elapsed_seconds=prior_elapsed + time.monotonic() - started,
                                     state=state, error=reason, error_code=code)
            if message:
                progress("call." + state, message)

        class Sink:
            def emit(_, event):
                if event["type"] == "llm.requested":
                    if stop.is_set() or self.closed.is_set():
                        return err(make_loom_error("EVALUATION_CANCELLED", "Evaluation cancelled", retryable=True))
                    if prior_elapsed + time.monotonic() - started >= settings["max_seconds"]:
                        return err(make_loom_error("EVALUATION_BUDGET", "Evaluation time budget reached; completed batches retained", retryable=True))
                    if body["usage"]["calls"] >= settings["max_calls"] or (
                        body["analysis_version"] != GRAPH_ENGINE and body["usage"]["total_tokens"] >= settings["max_tokens"]
                    ):
                        return err(make_loom_error("EVALUATION_BUDGET", "Evaluation budget reached; completed batches retained", retryable=True))
                    body["usage"]["calls"] += 1
                    body["usage"]["unreported_calls"] += 1
                    body["stage"] = event.get("analysis_stage")
                    body["current_call"] = {
                        "stage": body["stage"],
                        "activity": event.get("evaluation_activity", "review"),
                        "output_token_limit": event.get("output_token_limit"),
                        "timeout_seconds": event.get("timeout_seconds"),
                        "prompt_chars": event.get("prompt_chars"),
                        "input_estimate": event.get("input_estimate"),
                        "input_profile": event.get("input_profile"),
                        "state": "waiting",
                        "started_at": now_iso(),
                        "started_elapsed_seconds": prior_elapsed + round(time.monotonic() - started, 2),
                    }
                    progress("call.started", f"{event.get('activity_label', body['stage'])} (call {body['usage']['calls']})")
                elif event["type"] == "llm.completed":
                    usage = event["response"].usage
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        body["usage"][key] = body["usage"].get(key, 0) + getattr(usage, key, 0)
                    if body.get("analysis_version") != GRAPH_ENGINE or usage.total_tokens > 0:
                        body["usage"]["unreported_calls"] -= 1
                    body["current_call"]["state"] = "received"
                    body["current_call"]["duration_seconds"] = round(
                        prior_elapsed + time.monotonic() - started - body["current_call"]["started_elapsed_seconds"], 2
                    )
                    body["current_call"]["usage"] = {k: getattr(usage, k, 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
                    body["call_history"] = [*body.get("call_history", []), dict(body["current_call"])][-500:]
                    progress("call.completed", f"Response received: {body.get('stage')} · {usage.total_tokens} reported tokens")
                elif event["type"] == "llm.failed":
                    failed_request(event.get("error"), event.get("error_code"))
                elif event["type"] == "evaluation.activity":
                    body["stage"] = event["stage"]
                    body["activity"] = event["activity"]
                    progress(event["activity"], event["message"])
                else:
                    return ok(None)
                persist()
                return ok(None)

        async def run(source, facts, provider):
            def checkpoint(value):
                if body.get("analysis_version") in GOAL_ENGINES:
                    body["checkpoint"] = value
                    body.update(
                        completed_batches=len(value.get("stages", {})),
                        structure_complete=value.get("structure_complete", False),
                        reconciliation_complete=value.get("reconciliation_complete", False),
                        reviewed_packets=len(value.get("packet_status", {})),
                        total_packets=sum(map(len, value.get("batches", []))),
                        diagnosed_goals=len(value.get("diagnosed_goals", [])),
                        execution_protocol=value.get("execution_protocol", 1),
                        reconciliation_candidates=len(value.get("reconcile_candidates", [])),
                        split_requests=len(value.get("splits", [])),
                        review_gaps=len(value.get("review_gaps", [])),
                    )
                    if body["analysis_version"] == GRAPH_ENGINE:
                        body.update(
                            graph_revision=value.get("graph_revision", 0),
                            completed_batches=sum(u.get("complete", False) for u in value.get("units", {}).values()),
                            reviewed_packets=len(value.get("assessments", {})),
                            total_packets=value.get("total_cards", 0),
                            structure_complete=value.get("complete", False) and not value.get("pending_structure_units"),
                            reconciliation_complete=not value.get("conflicts"),
                            review_gaps=len(value.get("conflicts", [])),
                        )
                    persist()
                    return
                if body.get("analysis_version") == "v3":
                    body["checkpoint"] = value
                    body["scanned_pages"] = sum(k.startswith("scan:") for k in value.get("stages", {}))
                    body["scan_pages"] = value.get("scan_pages", 0)
                    body["stage_timings"] = value.get("stage_usage", {})
                    body["completed_batches"] = len(value.get("stages", {}))
                    assessments = [
                        row for key, stage in value.get("stages", {}).items() if key.startswith("focus:") for row in stage["result"].get("assessments", [])
                    ]
                    reviewed = {row["segment_id"] for row in assessments}
                    body["reviewed_segments"] = len(reviewed)
                    body["supported_segments"] = len({row["segment_id"] for row in assessments if row["status"] != "unknown"})
                    body["selected_segments"] = len(value.get("selected", []))
                    body["reviewed_rounds"] = len({rid for segment in facts["segments"] if segment["id"] in reviewed for rid in segment["round_ids"]})
                    body["scan_complete"] = value.get("scan_complete", False)
                    body["synthesis_complete"] = "synthesis" in value.get("stages", {})
                    persist()
                    return
                previous_batches = body.get("completed_batches", 0)
                body["checkpoint"] = value
                body["completed_batches"] = len(value.get("batches", {}))
                body["reviewed_rounds"] = len(
                    {row["round_id"] for batch in value.get("batches", {}).values() for row in batch["result"].get("round_analyses", [])}
                )
                if body["completed_batches"] > previous_batches:
                    progress("batch.saved", f"Saved {body['completed_batches']} batches · {body['reviewed_rounds']} rounds reviewed")
                persist()

            if body.get("analysis_version") in GOAL_ENGINES:
                if body["analysis_version"] == GRAPH_ENGINE:
                    from loom.evaluation.goal_loops.incremental import review
                else:
                    from loom.evaluation.goal_loops.review import review
                work = review(
                    source,
                    facts,
                    provider,
                    run_id=body["run_id"],
                    checkpoint=body["checkpoint"],
                    save_checkpoint=checkpoint,
                    event_sink=Sink(),
                    settings=settings,
                    remaining_seconds=lambda: settings["max_seconds"] - prior_elapsed - (time.monotonic() - started),
                )
            elif body.get("analysis_version") == "v3":
                from loom.evaluation.behavior_judge import judge_behavior

                work = judge_behavior(
                    source,
                    facts,
                    provider,
                    event_sink=Sink(),
                    checkpoint=body["checkpoint"],
                    save_checkpoint=checkpoint,
                    **{key: settings[key] for key in ("max_calls", "max_tokens", "max_read_rounds", "max_evidence_chars", "max_prompt_chars")},
                    max_segments=settings["max_rounds"],
                    max_call_seconds=settings.get("max_call_seconds", 120),
                    time_budget_seconds=settings["max_seconds"],
                    remaining_seconds=lambda: settings["max_seconds"] - prior_elapsed - (time.monotonic() - started),
                )
            else:
                work = judge_effectiveness(
                    source,
                    facts,
                    provider,
                    event_sink=Sink(),
                    **{key: settings[key] for key in ("max_read_rounds", "max_evidence_chars", "max_prompt_chars", "batch_rounds")},
                    checkpoint=body["checkpoint"],
                    save_checkpoint=checkpoint,
                    repair_invalid=True,
                )
            task = asyncio.create_task(work)
            heartbeat = time.monotonic()
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.2)
                    if time.monotonic() - heartbeat >= 1:
                        persist()
                        heartbeat = time.monotonic()
                    if stop.is_set() or self.closed.is_set():
                        raise InterruptedError("Evaluation stopped; completed batches retained")
                    if prior_elapsed + time.monotonic() - started >= settings["max_seconds"]:
                        raise TimeoutError("Evaluation time budget reached; completed batches retained")
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        try:
            if stop.is_set() or self.closed.is_set():
                raise InterruptedError("Evaluation stopped before starting")
            body.update(state="running", started_at=now_iso(), current_call=None)
            progress("started", "Preparing evaluation; previously saved batches will be reused")
            persist()
            parent = self.trajectory._load(body["session_id"], body["analysis_id"])
            source, result = self.trajectory._data(parent)
            body["total_rounds"] = len(result["summary"]["rounds"])
            persist()
            if self.provider_factory:
                provider = self.provider_factory(body["model"])
            else:
                if hashlib.sha256(Path(self.config_path).read_bytes()).hexdigest() != body["config_digest"]:
                    raise ValueError("Model configuration changed; start a new evaluation")
                loaded = load_task_config(self.config_path)
                if not loaded.ok:
                    raise ValueError("Could not load evaluator configuration")
                created = create_evaluation_provider(loaded.value, model_name=body["model"])
                if not created.ok:
                    raise ValueError(created.error.message)
                provider = created.value
            if body.get('autonomy_budget'):
                from loom.autonomy.budget import BudgetLedger, BudgetProvider

                provider = BudgetProvider(
                    provider, BudgetLedger(self.store.path), {**body['autonomy_budget'], 'evaluation_id': body['id']}, category='learning',
                    output_tokens=body['autonomy_budget'].get('output_tokens', 8192))
            if body.get("analysis_version") in {"v3", *GOAL_ENGINES}:
                behavior = _behavior_facts(source, result, body)
                result = {**result, "behavior": behavior}
                body["total_segments"] = len(behavior["segments"])
                judged = asyncio.run(run(source, behavior, provider))
                if not judged.ok:
                    raise ValueError(judged.error.message)
                semantic = judged.value
            else:
                facts = FactAnalysis(**result["facts"])
                selected = facts.trajectory[: settings.get("max_rounds", 10000)]
                body["selected_rounds"] = len(selected)
                selected_facts = replace(facts, trajectory=selected)
                if len(selected) < len(facts.trajectory):
                    runs = {row["run_id"] for row in selected}
                    selected_facts = replace(selected_facts, task_contracts=tuple(t for t in facts.task_contracts if t.get("run_id") in runs))
                judged = asyncio.run(run(source, selected_facts, provider))
                if not judged.ok:
                    raise ValueError(judged.error.message)
                semantic = asdict(judged.value)
                semantic["coverage"]["scope"] = {
                    "selected_rounds": len(selected),
                    "source_rounds": len(facts.trajectory),
                    "selection": "First recorded rounds in source order",
                }
                if len(selected) < len(facts.trajectory):
                    semantic["coverage"]["status"] = "incomplete"
                    semantic["coverage"]["limitations"].append("Only the selected prefix was reviewed; remaining rounds are unknown.")
                    semantic["verification"] = [
                        dict(row, status="unverified", limitation="Review scope excludes later task execution") if row["status"] == "supported" else row
                        for row in semantic["verification"]
                    ]
                verified = {row["criterion_id"]: row for row in semantic["verification"]}
                semantic["verification"] = [verified.get(row["criterion_id"], row) for row in unverified_criteria(facts)]
            if stop.is_set() or self.closed.is_set():
                raise InterruptedError("Evaluation stopped; completed stages retained")
            payload = self._result(body, source, result, semantic)
            with self.store.transaction() as db:
                ref = self.store.artifact_in_transaction(db, body["session_id"], payload, "trajectory_evaluation")
                self.outputs.track(db, body, ref)
            body.update(state="completed", completed_at=now_iso(), result=ref)
            if body.get("analysis_version") == GRAPH_ENGINE and semantic["coverage"]["status"] != "complete":
                gaps = [describe(g) for g in sorted(semantic["coverage"].get("review_gaps", []), key=lambda e: e["category"] == "coverage")]
                detail = "; ".join(dict.fromkeys(gaps))[:400]
                body.update(
                    state="incomplete",
                    error_details=semantic["coverage"].get("review_gaps", []),
                    error=f"Incremental analysis incomplete: {semantic['coverage']['pending_cards']} cards pending; "
                    f"{len(semantic['coverage'].get('pending_structure_units', []))} goal-structure windows pending; "
                    f"{semantic['coverage']['validation_issue_count']} validation issues. "
                    + (f"Issues: {detail}. " if detail else "")
                    + "Accepted graph revisions retained. Resume evaluation to retry pending windows.",
                )
            if body.get('analysis_version') == GRAPH_ENGINE:
                from loom.evaluation.work_items import status

                assessment = status(body['checkpoint'])
                body.update(assessment)
                if assessment['result_state'] == 'partial':
                    details = semantic['coverage'].get('review_gaps', []) + semantic.get('memory', {}).get('coverage', {}).get('issues', [])
                    body.update(state='incomplete', error_details=details,
                        error=('Processing finished with quality gaps. Accepted records remain available. Use Retry failed items.'
                               if assessment['execution_state'] == 'completed' else 'Analysis has unfinished work. Resume evaluation to continue.'))
                    if details:
                        body['error'] += ' ' + '; '.join(describe(e) for e in details[:2])
            if body.get("analysis_version") == "v3" and semantic["coverage"]["status"] != "complete":
                body.update(state="budget_exhausted", error="Evaluation budget reached; focused stages remain unfinished")
        except InterruptedError as exc:
            state, code, reason = interruption() or ("interrupted", "runtime.interrupted", str(exc) or "Evaluation interrupted")
            detail = issue(code, reason, category="cancelled", phase="orchestrate", unit_id=body.get("stage"))
            body.update(state=state, error=describe(detail), error_details=[detail])
        except TimeoutError as exc:
            state, code, reason = interruption() or ("failed", "runtime.timeout", str(exc) or "Evaluation timed out")
            detail = issue(code, reason, category="budget" if state == "budget_exhausted" else "internal",
                           phase="orchestrate", unit_id=body.get("stage"))
            body.update(state=state, error=describe(detail), error_details=[detail])
        except EvaluationFailure as exc:
            stopped = interruption()
            if stopped and stopped[0] == "budget_exhausted" and any(e["code"] == "request.timeout" for e in exc.issues):
                state, code, reason = stopped
                detail = issue(code, reason, category="budget", phase="orchestrate", unit_id=body.get("stage"))
                body.update(state=state, error=describe(detail), error_details=[detail])
            else:
                body.update(state=failure_state(exc.issues), error=str(exc), error_details=exc.issues)
        except Exception as exc:
            if body.get("analysis_version") == GRAPH_ENGINE:
                detail = issue("runtime.internal", f"{type(exc).__name__}: {exc}", category="internal", phase="orchestrate",
                               unit_id=body.get("stage"))
                body.update(state="failed", error=describe(detail), error_details=[detail])
            else:
                body.update(state=legacy_job_state(str(exc)), error=str(exc) or type(exc).__name__)
        finally:
            if (body.get("current_call") or {}).get("state") == "waiting":
                details = body.get("error_details") or []
                failed_request(body.get("error"), details[0]["code"] if details else "runtime." + body["state"], state=body["state"])
            progress(body["state"], body.get("error") or ("Evaluation completed" if body["state"] == "completed" else f"Evaluation {body['state']}"))
            with self.lock:
                persist()
                self.cancels.pop(body["id"], None)
                self.futures.pop(body["id"], None)
