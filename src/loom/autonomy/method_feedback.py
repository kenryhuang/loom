"""Traceable adoption and outcome summaries, with no inferred causal benefit."""

import json

from loom.autonomy.store import digest


def reconcile(store, aid):
    with store.sessions.transaction() as db:
        works = store.rows(db, "agent_work_packages", aid)
        contexts = store.rows(db, "agent_contexts", aid)
        uses = [json.loads(r[0]) for r in db.execute("SELECT body FROM agent_memory_uses WHERE agent_id=?", (aid,))]
        for work in works:
            if not work.get("outcome"):
                continue
            skill_uses = (
                [
                    json.loads(r[0])
                    for r in db.execute("SELECT body FROM skill_uses WHERE run_id IN (" + ",".join("?" for _ in work["run_ids"]) + ")", work["run_ids"])
                ]
                if work["run_ids"]
                else []
            )
            fingerprint = digest(
                [
                    work["outcome"],
                    [u for u in uses if u["run_id"] in work["run_ids"]],
                    skill_uses,
                    [c["id"] for c in contexts if c.get("run_id") in work["run_ids"]],
                ]
            )
            if work.get("feedback_fingerprint") == fingerprint:
                continue
            for rid in work["run_ids"]:
                offered = {}
                run_uses = list(uses)
                for usage in skill_uses:
                    if usage["run_id"] != rid:
                        continue
                    offered[("skill", usage["version_id"])] = dict(source={"version_id": usage["version_id"]})
                    if usage["kind"] == "step_executed":
                        run_uses.append(
                            dict(
                                run_id=rid,
                                references=dict(
                                    method_version_id=usage["version_id"], tool_call_ids=[r["tool_call_id"] for r in usage["payload"]["call_refs"]]
                                ),
                                evidence=usage,
                                action_refs_validated=True,
                            )
                        )
                for ctx in contexts:
                    if ctx.get("run_id") != rid:
                        continue
                    snapshot = json.loads(store.sessions.artifacts.read(ctx["artifact_ref"]["sha256"]))
                    for m in snapshot.get("methods", []):
                        offered[("method", m["id"])] = m
                    for m in snapshot.get("memory", {}).get("records", []):
                        source = m.get("source", {})
                        offered[("memory", source.get("record_id", m.get("id", "")))] = m
                for (kind, identifier), ref in offered.items():
                    selected = [
                        u
                        for u in run_uses
                        if u["run_id"] == rid and u.get("references", {}).get("method_version_id" if kind in {"method", "skill"} else "record_id") == identifier
                    ]
                    adopted = [u for u in selected if u["references"].get("disposition", "adopted") == "adopted"]
                    state = "unknown" if not selected else "not_adopted" if not adopted else "failed" if work["state"] == "failed" else "used"
                    tokens = db.execute("SELECT SUM(actual),SUM(actual IS NULL) FROM agent_budget_calls WHERE work_package_id=?", (work["id"],)).fetchone()
                    row = dict(
                        id="feedback_" + digest([aid, rid, kind, identifier])[:32],
                        agent_id=aid,
                        kind=kind,
                        reference_id=identifier,
                        work_package_id=work["id"],
                        run_id=rid,
                        state=state,
                        decision_evidence=selected,
                        action_ids=[t for u in selected for t in u["references"]["tool_call_ids"]],
                        acceptance=work["outcome"]["acceptance"],
                        outcome=work["outcome"],
                        causal_effect="unknown",
                        actual_tokens=tokens[0],
                        unknown_cost_calls=tokens[1] or 0,
                        predecessor_id=work.get("predecessor_id"),
                        source=ref.get("source"),
                        updated_at=store.now(),
                    )
                    store.put(db, "agent_method_feedback", row)
            work["feedback_fingerprint"] = fingerprint
            store.put(db, "agent_work_packages", work)


def report(store, aid):
    reconcile(store, aid)
    with store.sessions.transaction() as db:
        rows = store.rows(db, "agent_method_feedback", aid)
        works = store.rows(db, "agent_work_packages", aid)
        profile = store.rows(db, "agent_profile", aid)
        goals = store.rows(db, "agent_goals", aid)
        corrections = db.execute("SELECT COUNT(*) FROM memory_annotations WHERE agent_id=? AND json_extract(body,'$.kind')='correct'", (aid,)).fetchone()[0]
        cost = db.execute("SELECT SUM(actual),SUM(actual IS NULL),COUNT(*) FROM agent_budget_calls WHERE agent_id=?", (aid,)).fetchone()
    failures = {}
    for work in works:
        if work["state"] == "failed":
            key = digest([work["objective"], work.get("outcome", {}).get("acceptance")])
            failures[key] = failures.get(key, 0) + 1
    return dict(
        records=rows,
        sample_runs=len({r["run_id"] for r in rows}),
        failed_work_packages=sum(w["state"] == "failed" for w in works),
        corrections=corrections + sum(len(p["counterexamples"]) for p in profile),
        rework_attempts=sum(bool(g.get("predecessor_id")) for g in goals),
        repeated_failure_groups={key: count for key, count in failures.items() if count > 1},
        cost=dict(actual_tokens=cost[0], unknown_calls=cost[1] or 0, calls=cost[2]),
        causal_effect="unknown",
        limitations=[
            "Adoption is model reported and linked to recorded actions.",
            "Acceptance, citations and repeated families do not establish causal improvement.",
        ],
    )


def evolve(facade, aid, body):
    from loom.autonomy.contracts import strict
    from loom.service.contracts import ServiceError

    strict(body, {"session_id", "query", "parent_version_id", "settings"})
    snapshot = facade.store.snapshot(aid)
    if body.get("session_id") not in {s["session_id"] for s in snapshot["sessions"]}:
        raise ServiceError("Method learning needs an owned Session", 404)
    options = {k: v for k, v in body.items() if k != "session_id"}
    return facade.background.skills.start(body["session_id"], options, agent_binding=dict(agent_id=aid))


def advance(store, background, aid):
    """At most one frozen induction per newly completed cycle; retries are explicit."""
    reconcile(store, aid)
    agent = store.snapshot(aid)
    if agent["state"] != "enabled" or not agent["role"].get("method_learning"):
        return
    from zoneinfo import ZoneInfo

    budget_key = digest([agent["role"]["budget"], store.clock().astimezone(ZoneInfo(agent["role"]["timezone"])).date().isoformat()])
    for cycle in agent["learning"]:
        if cycle["state"] != "completed":
            continue
        prior = cycle.get("method_evolution")
        if prior:
            if not prior.get("job_id"):
                continue
            job = background.skills.store.job(prior["job_id"])
            outcome = {**prior, "state": job["state"], "reason": job.get("error"), "version_id": job.get("version_id")}
            if job["state"] == "deferred" and prior.get("budget_key") != budget_key and prior.get("resumes", 0) < 3:
                background.skills.resume(job["id"])
                outcome.update(state="queued", budget_key=budget_key, resumes=prior.get("resumes", 0) + 1)
            if outcome != prior:
                with store.sessions.transaction() as db:
                    current = store.get(db, "agent_learning_cycles", cycle["id"], aid)
                    current["method_evolution"] = outcome
                    store.put(db, "agent_learning_cycles", current)
            continue
        if agent.get('pending_configuration'):
            continue
        try:
            job = background.skills.start(
                cycle["session_id"], dict(query=agent["role"]["role_text"][:4000]), agent_binding=dict(agent_id=aid, goal_id=cycle["goal_id"])
            )
            outcome = dict(job_id=job["id"], state=job["state"], budget_key=budget_key, resumes=0)
        except (ValueError, RuntimeError) as exc:
            outcome = dict(state="deferred", reason=str(exc))
        with store.sessions.transaction() as db:
            current = store.get(db, "agent_learning_cycles", cycle["id"], aid)
            current["method_evolution"] = outcome
            store.put(db, "agent_learning_cycles", current)
        return
