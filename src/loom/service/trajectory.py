"""Session-scoped, cursor-pinned factual evaluation, independent of task execution."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from loom.core import now_iso
from loom.evaluation.evidence_store import EvidenceStore, text_value
from loom.evaluation.trajectory import build_fact_analysis
from loom.service.contracts import ServiceError, canonical, new_id
from loom.trace_analysis import build_episode_graph
from loom.trace_analysis.links import tool_output

VERSION = "session-trajectory.4.0"


def session_records(events, read_artifact):
    """Restore large payloads and join only uniquely recorded step identities."""
    records, scopes = [], {}
    coverage = {"hydrated_artifacts": 0, "missing_artifacts": [], "inferred_step_count": 0}
    for event in events:
        payload = dict(event["payload"])
        ref = payload.get("artifact", {})
        if isinstance(ref, dict) and ref.get("kind") == "event_detail":
            try:
                restored = read_artifact(ref["sha256"])
                if not isinstance(restored, dict):
                    raise ValueError("Event detail is not an object")
                payload = restored
                coverage["hydrated_artifacts"] += 1
            except (ServiceError, ValueError, KeyError, OSError) as exc:
                coverage["missing_artifacts"].append({"seq": event["seq"], "error": str(exc)})
        trace = payload.get("trace")
        trace = trace if isinstance(trace, dict) else {}
        payload = {**payload, "type": event["type"], "run_id": event.get("run_id"),
                   "at": payload.get("at", event.get("at")), "session_seq": event["seq"],
                   "command_id": event.get("command_id") or payload.get("command_id")}
        for name, nested in (("trace_id", "id"), ("loop_id", "loop_id"), ("step_number", "step_number")):
            if payload.get(name) is None:
                payload[name] = trace.get(nested, event.get(name))
        scope = payload["run_id"], payload.get("trace_id")
        if scope[1] and payload.get("loop_id") and isinstance(payload.get("step_number"), int) and not isinstance(payload["step_number"], bool):
            scopes.setdefault(scope, set()).add((payload["loop_id"], payload["step_number"]))
        records.append({"id": event["event_id"], "type": event["type"], "payload": payload})
    for record in records:
        payload = record["payload"]
        candidates = scopes.get((payload["run_id"], payload.get("trace_id")), set())
        candidates = [pair for pair in candidates if payload.get("loop_id") in {None, pair[0]}]
        if payload.get("step_number") is None and len(candidates) == 1:
            payload["loop_id"], payload["step_number"] = candidates[0]
            coverage["inferred_step_count"] += 1
    return records, coverage


def evidence_store(records):
    data = "".join(canonical(record) + "\n" for record in records).encode()
    return EvidenceStore(Path("session-trace.jsonl"), data)


def _duration(start, end):
    try:
        return max(0, round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000))
    except (TypeError, ValueError):
        return None


def analyze_session_trace(source, coverage):
    graph = build_episode_graph(source.events)
    facts = build_fact_analysis(source, graph)
    by_round = {row["id"]: row for row in facts.trajectory}
    usage = {row["round_id"]: row for row in facts.token_ledger}
    contexts = {row["round_id"]: row for row in facts.context_deltas}
    tools = []
    for episode, row in zip(graph.tool_calls, facts.tool_uses, strict=True):
        terminal = episode.completed_event or episode.failed_event
        output, _ = tool_output(terminal) if terminal else (None, None)
        output = output if isinstance(output, Mapping) else {}
        tools.append({key: row[key] for key in ("id", "round_id", "run_id", "tool_id", "tool_call_id", "input_ref", "raw_output_ref")} |
                     {"status": row["status"], "exit_code": output.get("exit_code"),
                      "seq": (terminal or episode.started_event).payload["session_seq"], "at": (terminal or episode.started_event).at,
                      "output_excerpt": row["output_excerpt"], "injection_count": len(row["injections"])})
    rounds = []
    for episode in graph.llm_rounds:
        request, response = episode.requested_event, episode.completed_event
        end = response or episode.failed_event
        data = response.payload.get("response", {}) if response else {}
        data = data if isinstance(data, Mapping) else {}
        content = data.get("content") or ""
        if not isinstance(content, str):
            content = text_value(content)
        context = contexts[episode.id]
        rounds.append({**by_round[episode.id], "model": request.payload.get("model") if request else None,
                       "seq": (request or end).payload["session_seq"], "at": (request or end).at,
                       "duration_ms": _duration(request.at if request else None, end.at if end else None),
                       "summary": content[:500], "usage": usage[episode.id],
                       "context": {key: context[key] for key in ("message_count", "content_char_length", "context_status")},
                       "context_changes": {key: len(context[key]) for key in ("added", "retained", "removed")}})
    failures = [{"seq": event.payload["session_seq"], "type": event.event_type, "run_id": event.run_id,
                 "at": event.at, "ref": asdict(source.ref(event)),
                 "message": str(event.payload.get("message") or event.payload.get("reason") or event.payload.get("error") or event.event_type)}
                for event in source.events if event.event_type in {"run.failed", "llm.failed", "tool.failed", "operation.uncertain", "run.recovery.required"}]
    summary = {"analysis_version": VERSION, "semantic_status": "not_evaluated", "task_completion": "unverified",
               "rounds": rounds, "tools": tools, "failures": failures,
               "run_ids": list(dict.fromkeys(row["run_id"] for row in rounds + tools)),
               "tokens": facts.coverage["tokens"], "coverage": {**coverage, "recorded_events": len(source.events),
                    "missing_request_count": facts.coverage["missing_request_count"],
                    "unlinked_tool_count": facts.coverage["unlinked_tool_count"],
                    "unscoped_model_events": sum(e.event_type in {"llm.requested", "llm.completed", "llm.failed"} for e in graph.orphaned_events)},
               "task_contracts": list(facts.task_contracts), "verification_count": len(facts.verification_evidence),
               "source_sha256": source.source_sha256}
    from loom.evaluation.behavior_facts import build_behavior_facts

    behavior = build_behavior_facts(source, legacy=facts, source_coverage=coverage)
    call_metrics = {row["id"]: row for row in behavior["base"]["statistics"]["calls"]["tools"]}
    for row in tools:
        measured = call_metrics[row["id"]]
        row.update(status={"success": "complete", "incomplete": "partial"}.get(measured["status"], measured["status"]),
                   duration_ms=measured["duration_ms"], failure_reason=measured["failure_reason"])
    summary["base"] = behavior["base"]
    summary["goal_revisions"] = behavior["goal_revisions"]
    summary["plan_revisions"] = behavior["plan_revisions"]
    return {"summary": summary, "facts": asdict(facts), "behavior": behavior}


class SessionTrajectory:
    """Bounded background jobs; completed snapshots survive a service restart."""

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="loom-trajectory")
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS session_analyses(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, cursor INTEGER NOT NULL, version TEXT NOT NULL,
                state TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(session_id,cursor,version))""")
            for row in db.execute("SELECT id,body FROM session_analyses WHERE state IN ('queued','running')").fetchall():
                body = json.loads(row["body"])
                body.update(state="failed", error="Service restarted during analysis. Refresh to retry.")
                db.execute("UPDATE session_analyses SET state='failed',body=? WHERE id=?", (canonical(body), row["id"]))

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True)

    def start(self, sid):
        with self.lock, self.store.transaction() as db:
            state = self.store._load(db, sid)
            if self.closed:
                raise ServiceError("Service is closing", 503)
            row = db.execute("SELECT body FROM session_analyses WHERE session_id=? AND cursor=? AND version=?",
                             (sid, state["event_cursor"], VERSION)).fetchone()
            previous = json.loads(row[0]) if row else None
            if previous and previous["state"] != "failed":
                return previous
            if db.execute("SELECT COUNT(*) FROM session_analyses WHERE state IN ('queued','running')").fetchone()[0] >= 4:
                raise ServiceError("Analysis queue is full. Try again shortly.", 429)
            body = {"id": previous["id"] if previous else new_id("analysis"), "session_id": sid,
                    "source_cursor": state["event_cursor"], "title": state["title"], "state": "queued", "created_at": now_iso()}
            db.execute("INSERT OR REPLACE INTO session_analyses VALUES(?,?,?,?,?,?)",
                       (body["id"], sid, body["source_cursor"], VERSION, body["state"], canonical(body)))
            self.executor.submit(self._work, dict(body))
            return body

    def _save(self, body):
        with self.store.transaction() as db:
            db.execute("UPDATE session_analyses SET state=?,body=? WHERE id=?", (body["state"], canonical(body), body["id"]))

    def _work(self, body):
        try:
            body["state"] = "running"
            self._save(body)
            with self.store.transaction() as db:
                events = [json.loads(row[0]) for row in db.execute(
                    "SELECT body FROM events WHERE session_id=? AND seq<=? AND json_extract(body,'$.type') NOT LIKE 'llm.%.delta' "
                    "AND json_extract(body,'$.type') NOT LIKE 'llm.stream.%' AND json_extract(body,'$.type') NOT LIKE 'llm.tool_call.%' ORDER BY seq",
                    (body["session_id"], body["source_cursor"]))]
                event_counts = {row[0]: row[1] for row in db.execute(
                    "SELECT json_extract(body,'$.type'),COUNT(*) FROM events WHERE session_id=? AND seq<=? GROUP BY json_extract(body,'$.type')",
                    (body["session_id"], body["source_cursor"]))}
                counts_by_run = {}
                for run, kind, count in db.execute(
                    "SELECT json_extract(body,'$.run_id'),json_extract(body,'$.type'),COUNT(*) FROM events WHERE session_id=? AND seq<=? "
                    "GROUP BY json_extract(body,'$.run_id'),json_extract(body,'$.type')",
                    (body["session_id"], body["source_cursor"])):
                    if run is not None:
                        counts_by_run.setdefault(run, {})[kind] = count
                owned = {row[0] for row in db.execute("SELECT digest FROM artifacts WHERE session_id=?", (body["session_id"],))}

            def read(digest):
                if digest not in owned:
                    raise ServiceError("Event detail does not belong to this session", 404)
                return json.loads(self.store.artifacts.read(digest))

            records, coverage = session_records(events, read)
            coverage.update(raw_event_count=sum(event_counts.values()), event_type_counts=event_counts, event_counts_by_run=counts_by_run)
            source = evidence_store(records)
            result = analyze_session_trace(source, coverage)
            refs = {"source": self.store.artifacts.publish(records, "trajectory_source"),
                    "result": self.store.artifacts.publish(result, "trajectory_analysis")}
            with self.store.transaction() as db:
                for ref in refs.values():
                    db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?)", (body["session_id"], ref["sha256"], canonical(ref)))
            body.update(state="completed", completed_at=now_iso(), **refs)
            self._remember(body["id"], source, result)
            self._save(body)
        except Exception as exc:
            body.update(state="failed", error=f"Could not analyze session trace: {exc}")
            self._save(body)

    def _remember(self, identifier, source, result):
        with self.lock:
            self.cache[identifier] = (source, result)
            self.cache.move_to_end(identifier)
            while len(self.cache) > 2:
                self.cache.popitem(last=False)
        return source, result

    def _load(self, sid, identifier):
        with self.store.transaction() as db:
            self.store._load(db, sid)
            row = db.execute("SELECT body FROM session_analyses WHERE id=? AND session_id=?", (identifier, sid)).fetchone()
            if row is None:
                raise ServiceError("Analysis not found for this session", 404)
            return json.loads(row[0])

    def _data(self, body):
        if body["state"] != "completed":
            raise ServiceError("Analysis is not complete", 409)
        with self.lock:
            cached = self.cache.get(body["id"])
            if cached:
                self.cache.move_to_end(body["id"])
                return cached
        source = evidence_store(json.loads(self.store.artifacts.read(body["source"]["sha256"])))
        result = json.loads(self.store.artifacts.read(body["result"]["sha256"]))
        return self._remember(body["id"], source, result)

    def get(self, sid, identifier):
        body = self._load(sid, identifier)
        return {**body, **({"analysis": self._data(body)[1]["summary"]} if body["state"] == "completed" else {})}

    def round(self, sid, identifier, round_id):
        _, result = self._data(self._load(sid, identifier))
        facts = result["facts"]
        match = next((row for row in facts["trajectory"] if row["id"] == round_id), None)
        if match is None:
            raise ServiceError("Analysis round not found", 404)
        return {"round": match, **{key: [row for row in facts[key] if row["round_id"] == round_id]
                                  for key in ("context_deltas", "tool_uses", "token_ledger", "verification_evidence")}}

    def evidence(self, sid, identifier, line, field, start, limit):
        source, _ = self._data(self._load(sid, identifier))
        if line < 1 or line > len(source.events):
            raise ServiceError("Evidence line not found", 404)
        event = source.events[line - 1]
        try:
            ref = source.ref(event, field, start=start)
            return {**source.read(ref, max_chars=limit), "seq": event.payload["session_seq"], "event_type": event.event_type}
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
