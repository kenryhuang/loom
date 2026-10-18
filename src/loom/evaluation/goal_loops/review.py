"""Resumable full-source structural review, reconciliation and cited diagnosis."""

import asyncio
import inspect
import json
from copy import deepcopy

from loom.core import now_iso, ok
from loom.evaluation.goal_loops import ANALYZER, PROMPT, SCHEMA
from loom.evaluation.goal_loops.context import brief, catalog, detail, window
from loom.evaluation.goal_loops.contracts import STAGE_READS, STAGE_SYSTEMS, STAGE_TOKENS, stage_protocol
from loom.evaluation.goal_loops.costs import ledger, summarize
from loom.evaluation.goal_loops.execution import reconciliation_candidates, record_review, split_unit, upgrade_checkpoint
from loom.evaluation.goal_loops.facts import packets, run_facts
from loom.evaluation.goal_loops.projection import process_graph
from loom.evaluation.goal_loops.state import apply_patch, overlaps, root_of
from loom.evaluation.provider import evaluator_provider
from loom.llm.api import LlmMessage, request_llm_response
from loom.llm.structured import ProtocolExhausted, UnsupportedFormat, output_mode, protocol_provider, request_structured, unsupported_format


def initial_state(view):
    return dict(goals=deepcopy(view["roots"]), attempts=[], phases=[], activities=[], relations=[], diagnostics=[], validation_diagnostics=[], aliases={})


def project(source, facts, run_id, checkpoint):
    view = run_facts(source, facts, run_id)
    state = deepcopy(checkpoint.get("entities") or initial_state(view))
    accounting = ledger(view, state)
    states = checkpoint.get("packet_status", {})
    all_packets = packets(source, view)
    call_packets = {a["id"]: {p["id"] for p in all_packets if a["id"] in p["call_ids"]} for a in view["atoms"]}
    reviewed_calls = {c for c, ids in call_packets.items() if ids and ids <= states.keys()}
    assigned = {a["cost_atom_id"] for a in accounting["assignments"] if a["owner"] != "analysis:unassigned"}
    covered = sum(a.get("total_tokens") or 0 for a in accounting["atoms"] if a["id"] in assigned)
    root_ids = {g["id"] for g in state["goals"] if not g.get("parent_goal_id")}
    for goal in state["goals"]:
        recorded = view["statistics"].get("execution_status", "unknown") if goal["id"] in root_ids else "unknown"
        goal["recorded_execution_status"] = recorded
        goal["execution_state"] = (
            "ended"
            if recorded in {"completed", "failed", "stopped", "cancelled"}
            else "paused"
            if recorded in {"paused", "suspended", "awaiting_input"}
            else "active"
            if recorded in {"running", "queued"}
            else "unknown"
        )
        goal["end_boundary"] = "explicit" if goal["execution_state"] == "ended" else "snapshot_end"
        boundary = [r for r in view["records"] if goal.get("origin_line", -1) <= r["line"] <= goal.get("end_line", -1)]
        goal["end_refs"] = [boundary[-1]["source_ref"]] if goal["id"] in root_ids and boundary else []
        goal["verification"] = [v for v in view["verification"] if goal["origin_line"] <= v["line"] <= goal["end_line"]] if goal["id"] in root_ids else []
    for diagnostic in state["diagnostics"]:
        diagnostic["involved_cost"] = summarize([a for a in view["atoms"] if a["id"] in diagnostic["call_ids"]])
    bundle = dict(
        schema_version=SCHEMA,
        source=dict(
            digest=source.source_sha256,
            run_id=run_id,
            scope_digest=view["scope_digest"],
            snapshot_boundary=max((r["seq"] or r["line"] for r in view["records"]), default=0),
            adapter_version=ANALYZER,
        ),
        analyzer=dict(version=checkpoint.get("identity", {}).get("analyzer", ANALYZER), prompt_version=checkpoint.get("identity", {}).get("prompt", PROMPT)),
        graph_revision=checkpoint.get("graph_revision", 0),
        **state,
        facts=dict(events=view["records"], verification=view["verification"], statistics=view["statistics"]),
        graph=process_graph(state),
        cost_ledger=accounting,
        coverage=dict(
            status="complete" if checkpoint.get("complete") and not checkpoint.get("review_gaps") else "partial",
            pipeline_complete=checkpoint.get("complete", False),
            source_events=len(view["records"]),
            eligible_calls=len(view["atoms"]),
            reviewed_calls=len(reviewed_calls),
            assigned_calls=len(assigned),
            unassigned_calls=len(view["atoms"]) - len(assigned),
            pending_calls=len(view["atoms"]) - len(reviewed_calls),
            source_packets=len(all_packets),
            reviewed_packets=len(states),
            classified_packets=sum(v == "classified" for v in states.values()),
            unknown_packets=sum(v == "reviewed_unknown" for v in states.values()),
            pending_packets=len(all_packets) - len(states),
            structure_complete=checkpoint.get("structure_complete", False),
            reconciliation_complete=checkpoint.get("reconciliation_complete", False),
            diagnosed_goals=checkpoint.get("diagnosed_goals", []),
            diagnosis_windows=checkpoint.get("diagnosis_windows", {}),
            packet_states=states,
            execution_protocol=checkpoint.get("execution_protocol", 1),
            split_requests=len(checkpoint.get("splits", [])),
            review_gaps=checkpoint.get("review_gaps", []),
            reconciliation_decisions=checkpoint.get("reconciliation_decisions", []),
            protocol_migrations=len(checkpoint.get("protocol_migrations", [])),
            attributed_known_tokens=covered,
            known_tokens=accounting["totals"]["total_tokens"]["known_sum"],
            source_quality=view["statistics"].get("quality", {}),
            limitations=view["limitations"] + checkpoint.get("limitations", []),
        ),
        evaluator_usage=deepcopy(checkpoint.get("usage", {})),
    )

    from loom.evaluation.goal_loops.execution_view import execution_view

    bundle["execution_view"] = execution_view(source, bundle)
    return bundle


class SplitRequest(ValueError):
    """The request needs smaller work units, not a whole-response repair."""


async def review(source, facts, provider, *, run_id, checkpoint, save_checkpoint, event_sink, settings, remaining_seconds):
    view = run_facts(source, facts, run_id)
    original = packets(source, view)
    registry = {p["id"]: p for p in original}
    expected = dict(source=source.source_sha256, scope=view["scope_digest"], analyzer=ANALYZER, prompt=PROMPT, model=getattr(provider, "model", "unknown"))
    upgraded = upgrade_checkpoint(checkpoint, expected)
    checkpoint.clear()
    checkpoint.update(upgraded)
    cp = checkpoint
    defaults = dict(
        identity=expected,
        entities=initial_state(view),
        stages={},
        stage_work={},
        revisions=[],
        graph_revision=0,
        packet_status={},
        delivered_ids=[],
        derived_packets={},
        splits=[],
        review_gaps=[],
        reconciliation_decisions=[],
        limitations=[],
        diagnosed_goals=[],
        diagnosis_windows={},
        usage=dict(calls=0, prompt_tokens=0, completion_tokens=0, total_tokens=0, unreported_calls=0),
    )
    for key, value in defaults.items():
        cp.setdefault(key, value)
    cp["execution_protocol"] = 2
    registry.update(cp["derived_packets"])

    async def save():
        value = save_checkpoint(deepcopy(cp))
        if inspect.isawaitable(value):
            await value

    async def emit(event):
        result = event_sink.emit(event)
        if inspect.isawaitable(result):
            result = await result
        if result is not None and not result.ok:
            raise ValueError(result.error.message)

    async def activity(stage, kind, message):
        await emit(dict(type="evaluation.activity", stage=stage, activity=kind, message=message))

    def objects():
        return {r["id"]: r for kind in ("goals", "attempts", "phases", "activities") for r in cp["entities"][kind] if not r.get("superseded_by")}

    def unit(family, ids, **kwargs):
        return dict(id="0", family=family, packet_ids=list(ids), object_ids=[], candidate_ids=[], read_round=0, depth=0, **kwargs)

    if "batches" not in cp:
        batches, batch, size = [], [], 0
        for packet in original:
            length = len(json.dumps(packet, ensure_ascii=False))
            if batch and size + length > settings["max_prompt_chars"] // 3:
                batches.append(batch)
                batch, size = [], 0
            batch.append(packet["id"])
            size += length
        cp["batches"] = batches + ([batch] if batch else [])
    await save()

    async def stage(key, initial):
        if key in cp["stages"]:
            return
        work = cp["stage_work"].setdefault(key, dict(pending=[initial], valid_units=0))
        await save()
        while work["pending"]:
            current = work["pending"][0]
            family = current["family"]
            index = objects()
            delivered = {pid: registry[pid] for pid in current["packet_ids"] if pid in registry}
            if family == "structure" and not any(p.get("evidence_ref") for p in delivered.values()):
                record_review(cp, delivered, set(), registry)
                work["pending"].pop(0)
                await save()
                continue
            pages = catalog(index)
            selected_pages = [p for p in current.get("index_pages", []) if 0 <= p < len(pages)]
            candidates = [c for c in cp.get("reconcile_candidates", []) if c["id"] in current["candidate_ids"]]
            entity_ids = current["object_ids"]
            if family == "reconcile":
                entity_ids = list(dict.fromkeys(i for c in candidates for i in (c["source_id"], c["target_id"])))
            payload = dict(
                stage=key,
                work_unit=current["id"],
                read_round=current["read_round"],
                roots=[brief(r) for r in cp["entities"]["goals"] if not r.get("parent_goal_id")],
                evidence_packets=[{k: p[k] for k in ("id", "line", "type", "content", "call_ids", "full_field") if k in p} for p in delivered.values()],
                calls=[
                    {k: a[k] for k in ("id", "kind", "name", "status") if k in a}
                    for a in view["atoms"]
                    if any(a["id"] in p["call_ids"] for p in delivered.values())
                ],
                objects=[detail(index[i], registry) for i in entity_ids if i in index],
                candidates=candidates,
                goal_id=current.get("goal_id"),
                coverage=dict(total_packets=len(original), reviewed_packets=len(cp["packet_status"]), diagnostic_window_only=family == "diagnose"),
            )
            if family != "reconcile":
                visible = list(index.values())
                if family == "diagnose":
                    selected_goal = current["goal_id"]
                    scoped = {selected_goal}
                    for _ in range(len(index)):
                        prior = len(scoped)
                        scoped.update(i for i, r in index.items() if (r.get("parent_goal_id") or r.get("goal_id") or r.get("attempt_id")) in scoped)
                        if len(scoped) == prior:
                            break
                    visible = [r for r in visible if r["id"] in scoped]
                payload.update(
                    index_page_count=len(pages),
                    index_pages={p: pages[p] for p in selected_pages},
                    active_entities=[brief(r) for r in visible[-12:]],
                    goal_attempt_index=[brief(r) for r in visible if "objective" in r or "approach" in r][-16:],
                )
            issues = []
            protocol = stage_protocol(family, issues)

            async def send(wire, attempt, mode, *, family=family, delivered=delivered, current=current, protocol=protocol):
                messages = [LlmMessage(role="system", content=STAGE_SYSTEMS[family]), LlmMessage(role="user", content=json.dumps(wire, ensure_ascii=False))]
                if sum(len(m.content) for m in messages) > settings["max_prompt_chars"]:
                    raise SplitRequest("Prompt exceeds stage input limit")
                usage = cp["usage"]
                if usage["calls"] >= settings["max_calls"] or usage["total_tokens"] >= settings["max_tokens"]:
                    raise ValueError("Evaluation budget reached; completed work retained")
                seconds = min(remaining_seconds(), settings.get("max_call_seconds", 120))
                if seconds <= 0:
                    raise ValueError("Evaluation time budget reached; completed work retained")
                originals = {p.get("original_id", pid) for pid, p in delivered.items()}
                already = set(cp["delivered_ids"])
                total = already | originals
                if sum(len(registry[pid]["content"]) for pid in total) > settings["max_evidence_chars"]:
                    raise ValueError("Evaluation evidence budget reached; completed work retained")
                cap = min(STAGE_TOKENS[family], max(1, settings["max_tokens"] - usage["total_tokens"]))
                metadata = dict(
                    analysis_stage=key,
                    evaluation_activity=family,
                    output_token_limit=cap,
                    timeout_seconds=seconds,
                    activity_label=f"{family} · {current['id']}",
                )
                await emit(dict(type="llm.requested", **metadata, messages=tuple(messages), model=expected["model"]))
                usage["calls"] += 1
                usage["unreported_calls"] += 1
                cp["delivered_ids"] = sorted(total)
                await save()
                capped = evaluator_provider(provider, output_tokens=cap, timeout_seconds=seconds)
                capped = protocol_provider(capped, protocol, mode)
                try:
                    async with asyncio.timeout(seconds):
                        response = await request_llm_response(capped, messages, tools=None)
                except BaseException:
                    await emit(dict(type="llm.failed", **metadata))
                    raise
                if not response.ok:
                    await emit(dict(type="llm.failed", **metadata))
                    if unsupported_format(response.error):
                        raise UnsupportedFormat()
                    raise ValueError(response.error.message)
                value = response.unwrap()
                for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    usage[field] = usage.get(field, 0) + getattr(value.usage, field, 0)
                usage["unreported_calls"] -= 1
                await emit(dict(type="llm.completed", **metadata, response=value))
                await save()
                return value

            try:
                response = await request_structured(
                    protocol=protocol, payload=payload, send=send, mode=cp.get("mode", output_mode(provider)), retry_truncated=False
                )
            except (SplitRequest, ProtocolExhausted) as exc:
                if isinstance(exc, ProtocolExhausted) and not exc.retryable:
                    raise
                children = split_unit(current, registry, cp["derived_packets"])
                if children:
                    work["pending"][:1] = children
                    cp["splits"].append(dict(stage=key, unit=current["id"], reason=str(exc), children=[c["id"] for c in children]))
                    await save()
                    await activity(key, "stage.split", f"Split {family} work {current['id']}; saved results retained")
                else:
                    cp["review_gaps"].append(
                        dict(stage=key, unit=current["id"], packet_ids=current["packet_ids"], candidate_ids=current["candidate_ids"], reason=str(exc))
                    )
                    work["pending"].pop(0)
                    await save()
                    await activity(key, "stage.unresolved", f"Atomic {family} work could not be reviewed; retained as a coverage gap")
                continue
            cp["mode"] = response.mode
            patch = response.value
            revision = f"{key}:{current['id']}:{current['read_round']}"
            if family == "reconcile":
                allowed = set(entity_ids)
                updates = [r for r in patch.get("updates", []) if r["id"] in allowed]
                issues.extend(
                    dict(field="updates", key=r["id"], reason="Not a supplied candidate entity") for r in patch.get("updates", []) if r["id"] not in allowed
                )
                candidate_map = {c["id"]: c for c in candidates}
                decisions = []
                for raw in patch.get("decisions", []):
                    if raw["candidate_id"] not in candidate_map or any(pid not in delivered or not delivered[pid].get("evidence_ref") for pid in raw["refs"]):
                        issues.append(dict(field="decisions", key=raw["candidate_id"], reason="Unknown candidate or undelivered evidence"))
                        continue
                    candidate = candidate_map[raw["candidate_id"]]
                    if any(r["candidate_id"] == raw["candidate_id"] for r in decisions):
                        issues.append(dict(field="decisions", key=raw["candidate_id"], reason="Duplicate candidate decision"))
                        continue
                    if raw["resolution"] != "unresolved" and not all(
                        any(overlaps(ref, delivered[pid]["evidence_ref"]) for ref in index.get(endpoint, {}).get("evidence_refs", []) for pid in raw["refs"])
                        for endpoint in (candidate["source_id"], candidate["target_id"])
                    ):
                        issues.append(dict(field="decisions", key=raw["candidate_id"], reason="Decision needs both candidate endpoints' evidence"))
                        continue
                    decisions.append(raw)
                merges = [{**candidate_map[r["candidate_id"]], "refs": r["refs"], "rationale": r["rationale"]} for r in decisions if r["resolution"] == "merge"]
                patch = dict(updates=updates, merges=merges)
            else:
                decisions = []
                if family == "diagnose":
                    rows = patch.get("diagnostics", [])
                    patch["diagnostics"] = [r for r in rows if r["goal_id"] == current["goal_id"]]
                    issues.extend(
                        dict(field="diagnostics", key=r["key"], reason="Diagnostic belongs to another goal") for r in rows if r["goal_id"] != current["goal_id"]
                    )
            state, references, domain_issues = apply_patch(cp["entities"], patch, delivered, view["atoms"], revision)
            state["validation_diagnostics"].extend({**i, "stage": revision} for i in issues)
            cp["entities"] = state
            cp["graph_revision"] += 1
            cp["revisions"].append(dict(revision=cp["graph_revision"], stage=revision, at=now_iso(), patch=deepcopy(patch), issues=[*issues, *domain_issues]))
            for candidate in candidates:
                decision = next((r for r in decisions if r["candidate_id"] == candidate["id"]), None)
                if decision is None:
                    decision = dict(candidate_id=candidate["id"], resolution="unresolved", refs=[], rationale="No valid decision returned")
                elif decision["resolution"] == "merge" and state["aliases"].get(candidate["source_id"]) != candidate["target_id"]:
                    decision = {**decision, "resolution": "unresolved", "rationale": "Merge failed evidence validation"}
                cp["reconciliation_decisions"].append(decision)
            record_review(cp, delivered, references, registry)
            if family == "diagnose":
                scope = cp["diagnosis_windows"][current["goal_id"]]
                reviewed = scope["delivered_packet_ids"]
                reviewed.extend(pid for pid in delivered if pid not in reviewed)
                originals_read = {registry[pid].get("original_id", pid) for pid in reviewed}
                scope["partial"] = len(originals_read) < scope["available_packets"] or any(registry[pid].get("original_id") for pid in reviewed)
            work["valid_units"] += 1
            read_ids = [i for i in patch.get("read_packet_ids", []) if i in registry and i not in delivered]
            object_ids = [i for i in patch.get("read_object_ids", []) if i in index][:2]
            index_pages = patch.get("read_index_pages", [])[:2]
            for identifier in object_ids:
                read_ids.extend(p["id"] for p in registry.values() if p.get("evidence_ref") in index[identifier].get("evidence_refs", []))
            requested = bool(read_ids or object_ids or index_pages)
            if requested and current["read_round"] < min(settings["max_read_rounds"], STAGE_READS[family]):
                # Persist accepted deltas before any follow-up; resume never regenerates these objects.
                ids = window(list(dict.fromkeys(read_ids)), registry, settings["max_prompt_chars"] // 3)
                work["pending"][0] = {
                    **current,
                    "packet_ids": ids,
                    "object_ids": object_ids,
                    "index_pages": index_pages,
                    "read_round": current["read_round"] + 1,
                }
                if len(ids) < len(set(read_ids)):
                    cp["limitations"].append(f"{key}: additional evidence request exceeded bounded read window")
            else:
                if requested:
                    cp["limitations"].append(f"{key}: additional read limit reached")
                work["pending"].pop(0)
            await save()
        cp["stages"][key] = dict(valid_units=work["valid_units"], at=now_iso())
        cp["stage_work"].pop(key, None)
        await save()
        await activity(key, "stage.saved", f"Saved {key}")

    for i, ids in enumerate(cp["batches"]):
        await stage(f"structure:{i}", unit("structure", ids))
    cp["structure_complete"] = len(cp["packet_status"]) == len(original)
    if "reconcile_candidates" not in cp:
        cp["reconcile_candidates"] = reconciliation_candidates(cp["entities"])
    await save()
    for candidate in cp["reconcile_candidates"]:
        index = objects()
        refs = [ref for i in (candidate["source_id"], candidate["target_id"]) if i in index for ref in index[i].get("evidence_refs", [])]
        ids = window([pid for pid, p in registry.items() if p.get("evidence_ref") in refs], registry, settings["max_prompt_chars"] // 3)
        item = unit("reconcile", ids)
        item["candidate_ids"] = [candidate["id"]]
        await stage("reconcile:" + candidate["id"], item)
    cp["reconciliation_complete"] = True
    if not cp["reconcile_candidates"]:
        await activity("reconcile", "stage.skipped", "No duplicate candidates; reconciliation skipped without a model call")
    await save()
    if "diagnosis_groups" not in cp:
        accounting = ledger(view, cp["entities"])
        goals = [g for g in cp["entities"]["goals"] if not g.get("superseded_by")]
        goals.sort(key=lambda g: accounting["by_owner"].get(g["id"], {}).get("inclusive", {}).get("total_tokens", {}).get("known_sum", 0), reverse=True)
        limit = settings["max_rounds"]
        selected = goals[:limit]
        if len(goals) > limit and limit > 1:
            selected[-1] = goals[-1]  # Low-cost comparison, within the same diagnosis budget.
        cp["diagnosis_groups"] = [g["id"] for g in selected]
    await save()
    for goal_id in cp["diagnosis_groups"]:
        index = objects()
        root = index[root_of(goal_id, index)]
        # Prefer evidence tied to this goal, with head/tail and distributed source samples.
        descendants = {goal_id}
        for _ in range(len(index)):
            expanded = descendants | {i for i, r in index.items() if (r.get("parent_goal_id") or r.get("goal_id") or r.get("attempt_id")) in descendants}
            if expanded == descendants:
                break
            descendants = expanded
        refs = [ref for i in descendants for ref in index[i].get("evidence_refs", [])]
        eligible = [
            p["id"]
            for p in original
            if p.get("evidence_ref") and root["origin_line"] <= p["line"] <= root["end_line"] and (goal_id == root["id"] or p["evidence_ref"] in refs)
        ]
        priority = []
        if eligible:
            priority = [eligible[0], eligible[-1]] + [eligible[round(i * (len(eligible) - 1) / 7)] for i in range(8)]
        ids = window(list(dict.fromkeys(priority + eligible)), registry, settings["max_prompt_chars"] // 3)
        cp["diagnosis_windows"].setdefault(goal_id, dict(delivered_packet_ids=[], available_packets=len(eligible), partial=True))
        item = unit("diagnose", ids, goal_id=goal_id)
        item["object_ids"] = [goal_id]
        await stage("diagnose:" + goal_id, item)
        if cp["stages"]["diagnose:" + goal_id]["valid_units"] and goal_id not in cp["diagnosed_goals"]:
            cp["diagnosed_goals"].append(goal_id)
        await save()
    cp["complete"] = True
    await save()
    return ok(project(source, facts, run_id, cp))
