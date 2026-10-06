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
from loom.evolution.diagnoses import proposals_from_diagnoses
from loom.service.contracts import ServiceError, canonical, new_id
from loom.service.insights import build_insights
from loom.tasks.config import create_provider_from_task_config, load_task_config

VERSION = "session-semantic.1"
RESOURCE_LIMITS = {"max_calls", "max_tokens", "max_seconds"}
DEFAULTS = {"max_calls": 80, "max_tokens": 1000000, "max_seconds": 1800,
            "max_evidence_chars": 400000, "max_prompt_chars": 100000, "batch_rounds": 8, "max_read_rounds": 4, "max_rounds": 10000}
BOUNDS = {"max_calls": (1, 500), "max_tokens": (100, 10000000), "max_seconds": (1, 7200),
          "max_evidence_chars": (1000, 5000000), "max_prompt_chars": (10000, 200000),
          "batch_rounds": (1, 16), "max_read_rounds": (0, 10), "max_rounds": (1, 10000)}


class SessionSemantic:
    def __init__(self, trajectory, config_path=None, *, provider_factory=None):
        self.trajectory, self.store = trajectory, trajectory.store
        self.config_path, self.provider_factory = config_path, provider_factory
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.cancels = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="loom-evaluation")
        with self.store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS semantic_jobs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, cache_key TEXT UNIQUE, body TEXT NOT NULL)")
            for row in db.execute("SELECT id,body FROM semantic_jobs").fetchall():
                body = json.loads(row["body"])
                if body["state"] in {"queued", "running"}:
                    body.update(state="interrupted", error="Service restarted. Resume reuses completed batches.")
                    db.execute("UPDATE semantic_jobs SET body=? WHERE id=?", (canonical(body), row["id"]))

    def options(self):
        models, default = [], None
        if self.provider_factory:
            models, default = [{"id": "test", "label": "test"}], "test"
        elif self.config_path:
            loaded = load_task_config(self.config_path)
            if loaded.ok:
                default = loaded.value.default_model
                models = [{"id": key, "label": f"{key} · {value.model}"} for key, value in loaded.value.models.items()]
        return {"models": models, "default_model": default, "defaults": DEFAULTS, "bounds": BOUNDS}

    def close(self):
        self.closed.set()
        self.pool.shutdown(wait=True)

    def _save(self, body):
        with self.store.transaction() as db:
            db.execute("UPDATE semantic_jobs SET body=? WHERE id=?", (canonical(body), body["id"]))

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
        self.trajectory._load(sid, aid)
        with self.store.transaction() as db:
            rows = [json.loads(row[0]) for row in db.execute("SELECT body FROM semantic_jobs WHERE session_id=? ORDER BY rowid DESC", (sid,))]
        return {"jobs": [self._public(row) for row in rows if row["analysis_id"] == aid], **self.options()}

    def _public(self, body):
        public = {k: v for k, v in body.items() if k not in {"checkpoint", "config_digest"}}
        if public["state"] == "failed" and "budget reached" in (public.get("error") or "").lower():
            public["state"] = "budget_exhausted"
        return public

    def get(self, sid, aid, identifier):
        body = self._load(sid, aid, identifier)
        result = self._public(body)
        if body.get("result"):
            result["evaluation"] = json.loads(self.store.artifacts.read(body["result"]["sha256"]))
        elif body["state"] not in {"queued", "running"} and body["checkpoint"].get("batches"):
            source, factual = self.trajectory._data(self.trajectory._load(sid, aid))
            outputs = [item["result"] for item in body["checkpoint"]["batches"].values()]
            reviews = {row["round_id"]: row for output in outputs for row in output.get("round_analyses", [])}
            semantic = {"diagnoses": [d for output in outputs for d in output["diagnoses"]],
                        "verification": list(unverified_criteria(FactAnalysis(**factual["facts"]))),
                        "preserved_behaviors": [p for output in outputs for p in output["preserved_behaviors"]],
                        "verification_framework": [], "round_analyses": list(reviews.values()), "usage": body["usage"],
                        "coverage": {"status": "incomplete", "limitations": [body.get("error") or "Evaluation unfinished"],
                                     "round_dimension_statuses": {d: {status: sum(r["dimensions"][d]["status"] == status
                                         for r in reviews.values()) for status in ("effective", "ineffective", "mixed", "unknown")} for d in DIMENSIONS}}}
            result["evaluation"] = self._result(body, source, factual, semantic)
        return result

    def _result(self, body, source, factual, semantic):
        return {"semantic": semantic, "insights": build_insights(factual["summary"], semantic, factual["facts"]),
                "proposals": proposals_from_diagnoses(semantic["diagnoses"], source_sha256=source.source_sha256,
                    evaluator={"model": body["model"], "prompt_version": PROMPT_VERSION}),
                "source_sha256": source.source_sha256}

    def start(self, sid, aid, options):
        if not isinstance(options, dict) or set(options) - {"model", "resume_id", *DEFAULTS}:
            raise ServiceError("Invalid evaluation options")
        resume_id = options.get("resume_id")
        if resume_id is not None and (not isinstance(resume_id, str) or not resume_id):
            raise ServiceError("Invalid resume_id")
        resumed = self._load(sid, aid, resume_id) if resume_id else None
        settings = {**DEFAULTS, **(resumed["settings"] if resumed else {})}
        for key, value in options.items():
            if key in {"model", "resume_id"}:
                continue
            low, high = BOUNDS[key]
            if type(value) is not int or not low <= value <= high:
                raise ServiceError(f"{key} must be an integer between {low} and {high}")
            settings[key] = value
        available = self.options()
        model = options.get("model") or (resumed["model"] if resumed else available["default_model"])
        if not isinstance(model, str) or model not in {row["id"] for row in available["models"]}:
            raise ServiceError("Select a configured judge model")
        parent = self.trajectory._load(sid, aid)
        if parent["state"] != "completed":
            raise ServiceError("Wait for factual analysis to complete", 409)
        digest = hashlib.sha256(Path(self.config_path).read_bytes()).hexdigest() if self.config_path else "test"
        identity = {"source": parent["source"]["sha256"], "model": model, "config": digest,
                    "version": VERSION, "analyzer": ANALYZER_VERSION, "prompt": PROMPT_VERSION, "settings": settings,
                    "session": sid, "analysis": aid}
        key = hashlib.sha256(canonical(identity).encode()).hexdigest()
        with self.lock, self.store.transaction() as db:
            if self.closed.is_set():
                raise ServiceError("Service is closing", 503)
            row = db.execute("SELECT body FROM semantic_jobs WHERE cache_key=?", (key,)).fetchone()
            previous = json.loads(row[0]) if row else None
            if resumed:
                if previous and previous["id"] != resume_id:
                    raise ServiceError("An evaluation with these settings already exists; select it from history", 409)
                resumed = json.loads(db.execute("SELECT body FROM semantic_jobs WHERE id=?", (resume_id,)).fetchone()[0])
                if resumed["state"] in {"queued", "running", "completed"}:
                    raise ServiceError("Only stopped evaluations can be resumed", 409)
                if (model != resumed["model"] or digest != resumed["config_digest"]
                        or resumed["analyzer_version"] != ANALYZER_VERSION or resumed["prompt_version"] != PROMPT_VERSION
                        or any(settings[k] != resumed["settings"].get(k, DEFAULTS[k]) for k in DEFAULTS if k not in RESOURCE_LIMITS)):
                    raise ServiceError("Resume must keep the same model, snapshot and analysis settings; only resource limits may change")
                if any(settings[k] < resumed["settings"][k] for k in RESOURCE_LIMITS):
                    raise ServiceError("Resuming cannot reduce resource limits")
                previous = resumed
            if previous and previous["state"] in {"queued", "running", "completed"}:
                return self._public(previous)
            active = db.execute("SELECT COUNT(*) FROM semantic_jobs WHERE json_extract(body,'$.state') IN ('queued','running')").fetchone()[0]
            if active >= 4:
                raise ServiceError("Evaluation queue is full", 429)
            body = previous or {"id": new_id("evaluation"), "session_id": sid, "analysis_id": aid,
                                "source_cursor": parent["source_cursor"], "model": model, "settings": settings,
                                "config_digest": digest, "analyzer_version": ANALYZER_VERSION, "prompt_version": PROMPT_VERSION,
                                "created_at": now_iso(), "checkpoint": {}, "usage": {"calls": 0, "total_tokens": 0, "unreported_calls": 0},
                                "elapsed_seconds": 0}
            consumed = {"max_calls": body["usage"]["calls"], "max_tokens": body["usage"]["total_tokens"],
                        "max_seconds": body["elapsed_seconds"]}
            exhausted = [k for k, value in consumed.items() if value >= settings[k]]
            if exhausted:
                limits = ", ".join(f"{k} > {consumed[k]}" for k in exhausted)
                raise ServiceError(f"Budget exhausted. Increase total limits ({limits}) to resume saved batches", 409)
            body.update(state="queued", error=None, settings=settings)
            db.execute("INSERT OR REPLACE INTO semantic_jobs VALUES(?,?,?,?)", (body["id"], sid, key, canonical(body)))
            self.cancels[body["id"]] = threading.Event()
            self.pool.submit(self._work, body)
            return self._public(body)

    def cancel(self, sid, aid, identifier):
        body = self._load(sid, aid, identifier)
        with self.lock:
            flag = self.cancels.get(identifier)
            if flag:
                flag.set()
        return self._public(body)

    def _work(self, body):
        started = time.monotonic()
        prior_elapsed = body["elapsed_seconds"]
        stop = self.cancels[body["id"]]
        settings = body["settings"]
        def progress(kind, message):
            body["progress"] = [*body.get("progress", []), {"kind": kind, "message": message, "at": now_iso(),
                "elapsed_seconds": prior_elapsed + round(time.monotonic() - started, 2)}][-100:]

        def persist():
            body["elapsed_seconds"] = prior_elapsed + round(time.monotonic() - started, 2)
            self._save(body)

        class Sink:
            def emit(_, event):
                if event["type"] == "llm.requested":
                    if stop.is_set() or self.closed.is_set():
                        return err(make_loom_error("EVALUATION_CANCELLED", "Evaluation cancelled", retryable=True))
                    if prior_elapsed + time.monotonic() - started >= settings["max_seconds"]:
                        return err(make_loom_error("EVALUATION_BUDGET", "Evaluation time budget reached; completed batches retained", retryable=True))
                    if body["usage"]["calls"] >= settings["max_calls"] or body["usage"]["total_tokens"] >= settings["max_tokens"]:
                        return err(make_loom_error("EVALUATION_BUDGET", "Evaluation budget reached; completed batches retained", retryable=True))
                    body["usage"]["calls"] += 1
                    body["usage"]["unreported_calls"] += 1
                    body["stage"] = event.get("analysis_stage")
                    body["current_call"] = {"stage": body["stage"], "state": "waiting", "started_at": now_iso(),
                                            "started_elapsed_seconds": prior_elapsed + round(time.monotonic() - started, 2)}
                    progress("call.started", f"Requesting evaluator: {body['stage']} (call {body['usage']['calls']})")
                elif event["type"] == "llm.completed":
                    usage = event["response"].usage
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        body["usage"][key] = body["usage"].get(key, 0) + getattr(usage, key, 0)
                    body["usage"]["unreported_calls"] -= 1
                    body["current_call"]["state"] = "received"
                    progress("call.completed", f"Response received: {body.get('stage')} · {usage.total_tokens} reported tokens")
                elif event["type"] == "llm.failed":
                    body["current_call"]["state"] = "failed"
                    progress("call.failed", f"Evaluator request failed: {body.get('stage')}")
                else:
                    return ok(None)
                persist()
                return ok(None)

        async def run(source, facts, provider):
            def checkpoint(value):
                previous_batches = body.get("completed_batches", 0)
                body["checkpoint"] = value
                body["completed_batches"] = len(value.get("batches", {}))
                body["reviewed_rounds"] = len({row["round_id"] for batch in value.get("batches", {}).values()
                                              for row in batch["result"].get("round_analyses", [])})
                if body["completed_batches"] > previous_batches:
                    progress("batch.saved", f"Saved {body['completed_batches']} batches · {body['reviewed_rounds']} rounds reviewed")
                persist()
            task = asyncio.create_task(judge_effectiveness(source, facts, provider, event_sink=Sink(),
                **{key: settings[key] for key in ("max_read_rounds", "max_evidence_chars", "max_prompt_chars", "batch_rounds")},
                checkpoint=body["checkpoint"], save_checkpoint=checkpoint, repair_invalid=True))
            heartbeat = time.monotonic()
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=.2)
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
                created = create_provider_from_task_config(loaded.value, model_name=body["model"])
                if not created.ok:
                    raise ValueError(created.error.message)
                provider = created.value
            facts = FactAnalysis(**result["facts"])
            selected = facts.trajectory[:settings.get("max_rounds", 10000)]
            body["selected_rounds"] = len(selected)
            selected_facts = replace(facts, trajectory=selected)
            if len(selected) < len(facts.trajectory):
                runs = {row["run_id"] for row in selected}
                selected_facts = replace(selected_facts, task_contracts=tuple(t for t in facts.task_contracts if t.get("run_id") in runs))
            judged = asyncio.run(run(source, selected_facts, provider))
            if not judged.ok:
                raise ValueError(judged.error.message)
            semantic = asdict(judged.value)
            semantic["coverage"]["scope"] = {"selected_rounds": len(selected), "source_rounds": len(facts.trajectory),
                                             "selection": "First recorded rounds in source order"}
            if len(selected) < len(facts.trajectory):
                semantic["coverage"]["status"] = "incomplete"
                semantic["coverage"]["limitations"].append("Only the selected prefix was reviewed; remaining rounds are unknown.")
                semantic["verification"] = [dict(row, status="unverified", limitation="Review scope excludes later task execution")
                                            if row["status"] == "supported" else row for row in semantic["verification"]]
            verified = {row["criterion_id"]: row for row in semantic["verification"]}
            semantic["verification"] = [verified.get(row["criterion_id"], row) for row in unverified_criteria(facts)]
            payload = self._result(body, source, result, semantic)
            ref = self.store.artifacts.publish(payload, "trajectory_evaluation")
            with self.store.transaction() as db:
                db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?)", (body["session_id"], ref["sha256"], canonical(ref)))
            body.update(state="completed", completed_at=now_iso(), result=ref)
        except InterruptedError as exc:
            body.update(state="interrupted" if self.closed.is_set() else "cancelled", error=str(exc))
        except Exception as exc:
            body.update(state="budget_exhausted" if "budget reached" in str(exc).lower() else "failed", error=str(exc))
        finally:
            if (body.get("current_call") or {}).get("state") == "waiting":
                body["current_call"]["state"] = "interrupted"
            progress(body["state"], body.get("error") or "Evaluation completed")
            with self.lock:
                persist()
                self.cancels.pop(body["id"], None)
