"""Chronological card review → atomic deltas → free graph/report projection."""

import asyncio
import inspect
import time
from copy import deepcopy

from jsonschema import Draft202012Validator

from loom.core import ok
from loom.evaluation.evidence_store import text_value
from loom.evaluation.goal_loops.cards import VERSION as EXTRACTOR
from loom.evaluation.goal_loops.cards import card_payload, compile_cards, link_cards
from loom.evaluation.goal_loops.facts import identity, run_facts
from loom.evaluation.goal_loops.graph_contract import ANALYZER, PROMPT, REQUEST, SYSTEM, UPGRADABLE_VERSIONS, compact_contract, protocol
from loom.evaluation.goal_loops.graph_projection import project
from loom.evaluation.goal_loops.graph_state import initialize, occurrences
from loom.evaluation.goal_loops.graph_updates import commit_updates, parse_updates
from loom.evaluation.goal_loops.packing import REQUEST_CHARS, bounded_state, request_profile, serialized
from loom.evaluation.provider import evaluator_provider
from loom.llm.api import LlmMessage, request_llm_response
from loom.llm.structured import output_mode, protocol_provider, unsupported_format


def context(cp, cards, cutoff):
    terms = set(serialized([c["features"] for c in cards]).lower().replace("/", " ").split())
    goals = [g for g in cp["entities"]["goals"] if g["origin_line"] <= cutoff]
    roots = [g for g in goals if not g.get("parent_goal_id") and g["end_line"] >= min(c["start_line"] for c in cards)]

    def rank(goal):
        return (len(terms.intersection(goal["objective"].lower().split())), goal["origin_line"])

    selected = sorted([g for g in goals if g.get("parent_goal_id")], key=rank, reverse=True)[:6]
    ids = {g["id"] for g in roots + selected}
    stages = [s for s in cp["stages"].values() if s["goal_id"] in ids][-8:]
    return bounded_state(
        dict(
            roots=[{k: g[k] for k in ("id", "objective", "conditions")} for g in roots],
            goals=[{k: g[k] for k in ("id", "parent_goal_id", "objective", "conditions", "goal_disposition")} for g in selected],
            stages=[{k: s[k] for k in ("id", "goal_id", "attempt_id", "title", "kind")} for s in stages],
            closures=[{k: r[k] for k in ("goal_id", "claim", "evidence_state", "open_questions")} for g, r in cp["closures"].items() if g in ids],
            older_goal_index=[dict(id=g["id"], objective=g["objective"][:100]) for g in goals if g["id"] not in ids][-16:],
            omitted_goals=max(0, len(goals) - len(ids) - 16),
        )
    )


def read_requests(source, registry, requests, *, cutoff, inspected, remaining_chars):
    """Validate the complete plan before reading; aliases cannot bypass range dedup."""
    pending, used = [], 0
    for request in requests:
        Draft202012Validator(REQUEST).validate(request)
        if not request["question"].strip() or not request["purpose"].strip():
            raise ValueError("Evidence read requires a question and purpose")
        if request["ref"] not in registry or registry[request["ref"]]["line"] > cutoff:
            raise ValueError("Cannot read unregistered or future evidence")
        base = registry[request["ref"]]["ref"]
        text = text_value(source.resolve(base))
        if request["start"] >= len(text):
            raise ValueError("Evidence range is outside the registered field")
        start = (base.get("start") or 0) + request["start"]
        end = start + min(request["length"], len(text) - request["start"])
        pointer = {**base, "start": start, "end": end}
        if any(
            r["line_number"] == pointer["line_number"]
            and r.get("field_path") == pointer.get("field_path")
            and (r.get("start") or 0) <= start
            and (r.get("end") or 0) >= end
            for r in inspected + [p["ref"] for p in pending]
        ):
            raise ValueError("Evidence range was already delivered; request an unseen range or conclude unknown")
        used += end - start
        if used > remaining_chars:
            raise ValueError("Evidence read budget reached")
        pending.append(
            dict(
                id="e" + identity(pointer)[:12],
                ref=pointer,
                line=pointer["line_number"],
                text=text[request["start"] : request["start"] + end - start],
                question=request["question"],
            )
        )
    return pending


async def review(source, facts, provider, *, run_id, checkpoint, save_checkpoint, event_sink, settings, remaining_seconds):
    view = run_facts(source, facts, run_id)
    extraction = checkpoint.get("extraction") or compile_cards(source, facts, view)
    cards, registry = extraction["cards"], dict(extraction["registry"])
    extraction["metadata_links"] = link_cards(cards)
    semantic_settings = {k: v for k, v in settings.items() if k not in {"max_calls", "max_tokens", "max_seconds"}}
    expected = dict(
        source=source.source_sha256,
        scope=view["scope_digest"],
        extractor=EXTRACTOR,
        analyzer=ANALYZER,
        prompt=PROMPT,
        model=getattr(provider, "model", "unknown"),
        settings=identity(semantic_settings),
    )
    cp = checkpoint
    if cp.get("identity") and cp["identity"] != expected:
        old = cp["identity"]
        if (old.get("analyzer"), old.get("prompt")) not in UPGRADABLE_VERSIONS or {**old, "analyzer": ANALYZER, "prompt": PROMPT} != expected:
            raise ValueError("Incremental graph identity changed; create a new evaluation")
        cp.setdefault("protocol_upgrades", []).append(dict(from_prompt=old["prompt"], to_prompt=PROMPT, revision=cp["graph_revision"]))
    for key, value in initialize(view).items():
        cp.setdefault(key, value)
    cp["identity"] = expected
    cp["extraction"] = extraction
    cp["total_cards"] = len(cards)
    cp.setdefault("mode", output_mode(provider))
    cp.setdefault("read_chars", 0)
    cp.setdefault("units", {})
    card_map = {c["id"]: c for c in cards}
    registry.update(cp.get("extra_evidence", {}))
    cp.setdefault("extra_evidence", {})
    cp.setdefault("delivered_ranges", [])

    async def save():
        value = save_checkpoint(deepcopy(cp))
        if inspect.isawaitable(value):
            await value

    async def emit(event):
        value = event_sink.emit(event)
        if inspect.isawaitable(value):
            value = await value
        if value is not None and not value.ok:
            raise ValueError(value.error.message)

    # A resumable unit retains its cutoff and bounded read state. Failed earlier
    # groups are retried without exposing judgments from later windows.
    pending = sorted((u for u in cp["units"].values() if not u.get("complete")), key=lambda u: (u["cutoff"], u["id"]))
    for unit in pending:
        unit["repair"] = 0
    while pending or cp["cursor"] < len(cards):
        if not pending:
            subset = cards[cp["cursor"] : cp["cursor"] + min(8, settings["batch_rounds"])]
            unit = dict(
                id="batch:" + identity([c["id"] for c in subset]),
                cards=[c["id"] for c in subset],
                cutoff=max(c["end_line"] for c in subset),
                read_round=0,
                extra=[],
                repair=0,
                complete=False,
            )
            pending.append(unit)
        unit = pending[0]
        subset = [card_map[c] for c in unit["cards"]]
        cutoff = unit["cutoff"]
        # Earlier retries use the state as of their cutoff, not later conclusions.
        # Avoid copying extraction, raw responses, reservations and revision history
        # just to build the bounded semantic view of this window.
        local = {"entities": {"goals": deepcopy(cp["entities"]["goals"])}, "stages": cp["stages"]}
        local["entities"]["goals"] = [g for g in local["entities"]["goals"] if g.get("knowledge_cutoff", g["origin_line"]) <= cutoff]
        local["stages"] = {
            k: s for k, s in local["stages"].items() if s.get("knowledge_cutoff", 0) <= cutoff and all(r["line_number"] <= cutoff for r in s["evidence_refs"])
        }
        local["closures"] = {}
        for gid, row in cp["closures"].items():
            known = [r for r in row.get("history", []) + [row] if r["knowledge_cutoff"] <= cutoff]
            if known:
                local["closures"][gid] = known[-1]
        for goal in local["entities"]["goals"]:
            closure = local["closures"].get(goal["id"], {})
            goal.update(goal_disposition=closure.get("disposition", "open"), completion_claim=closure.get("claim", "unknown"))
        local["assessments"] = {}
        for cid, row in cp["assessments"].items():
            known = [r for r in row.get("history", []) + [row] if r["knowledge_cutoff"] <= cutoff]
            if known:
                local["assessments"][cid] = known[-1]
        frontier = occurrences(local, cards)[-4:]
        boundary = {}
        frontier_rows = []
        for occurrence in frontier:
            evidence_ids = []
            for ref in occurrence["evidence_refs"][:2]:
                value = text_value(source.resolve(ref))
                pointer = {**ref, "start": ref.get("start") or 0, "end": (ref.get("start") or 0) + min(len(value), 240)}
                eid = "e" + identity(pointer)[:12]
                boundary[eid] = dict(id=eid, ref=pointer, line=pointer["line_number"], text=value[:240])
                evidence_ids.append(eid)
            frontier_rows.append(
                {
                    **{k: occurrence[k] for k in ("id", "stage_id", "goal_id", "card_ids", "end_line")},
                    "delta": occurrence["delta"][:240],
                    "evidence_ids": evidence_ids,
                }
            )
        # Action IDs/order/caller metadata are factual. A compact prior-action index
        # lets later batches attach an earlier unassigned action or cite its result.
        prior_actions = []
        for card in [c for c in cards if c["id"] in local["assessments"] and c["end_line"] < min(s["start_line"] for s in subset)][-6:]:
            assessment = local["assessments"][card["id"]]
            evidence_ids = []
            for ref in assessment["evidence_refs"][:2]:
                value = text_value(source.resolve(ref))
                pointer = {**ref, "start": ref.get("start") or 0, "end": (ref.get("start") or 0) + min(len(value), 240)}
                eid = "e" + identity(pointer)[:12]
                boundary[eid] = dict(id=eid, ref=pointer, line=pointer["line_number"], text=value[:240])
                evidence_ids.append(eid)
            prior_actions.append(dict(id=card["id"], kind=card["kind"], start_line=card["start_line"], end_line=card["end_line"],
                                      stage_ids=assessment["stage_ids"], delta=assessment["delta"][:160], evidence_ids=evidence_ids))
        registry.update(boundary)
        allow_reads = unit["read_round"] < min(2, settings["max_read_rounds"]) and not unit.get("conclude")
        payload = dict(
            batch_id=unit["id"],
            base_graph_revision=cp["graph_revision"],
            knowledge_cutoff=cutoff,
            **context(local, subset, cutoff),
            cards=[card_payload(c, cutoff) for c in subset],
            extra_evidence=[{k: registry[e][k] for k in ("id", "text", "question")} for e in unit["extra"]],
            review_control=dict(mode="review" if allow_reads else "conclude", remaining_reads=max(0, min(2, settings["max_read_rounds"]) - unit["read_round"])),
            feedback=unit.get("feedback", []),
            frontier=frontier_rows,
            prior_actions=prior_actions,
            accepted_card_ids=[c["id"] for c in subset if c["id"] in cp["assessments"] and cp["assessments"][c["id"]]["status"] != "needs_evidence"],
            local_aliases=cp.get("record_aliases", {}).get(unit["id"], {}),
            boundary_evidence=[{k: e[k] for k in ("id", "text")} for e in boundary.values()],
        )
        if cp["mode"] != "json_schema":
            payload["contract"] = compact_contract(cp["mode"])
        messages = [
            LlmMessage(
                role="system", content=SYSTEM + ("\nNo more evidence reads; conclude with supported findings or explicit unknown." if not allow_reads else "")
            ),
            LlmMessage(role="user", content=serialized(payload)),
        ]
        profile = request_profile(messages, payload, cp["mode"], protocol(allow_reads))
        chars = profile["total_chars"]
        if chars > min(settings["max_prompt_chars"], REQUEST_CHARS):
            if unit["id"] not in cp["units"] and len(subset) > 1:
                half = subset[: max(1, len(subset) // 2)]
                unit.update(id="batch:" + identity([c["id"] for c in half]), cards=[c["id"] for c in half], cutoff=max(c["end_line"] for c in half))
                continue
            raise ValueError(
                f"Evaluation input budget reached: {chars} chars including response schema, "
                f"limit {min(settings['max_prompt_chars'], REQUEST_CHARS)}, {len(subset)} cards; checkpoint retained"
            )
        if unit["id"] not in cp["units"]:
            cp["units"][unit["id"]] = deepcopy(unit)
            cp["cursor"] += len(subset)
        unit = cp["units"][unit["id"]]
        pending[0] = unit
        # UTF-8 bytes plus protocol allowance is a conservative estimate, not tokenizer usage.
        input_estimate = profile["input_estimate"]
        if cp["usage"]["calls"] >= settings["max_calls"] or remaining_seconds() <= 0:
            await save()
            raise ValueError(
                f"Evaluation budget reached before request reservation: calls {cp['usage']['calls']}/{settings['max_calls']}; "
                f"reported tokens {cp['usage']['total_tokens']} (accounting only); "
                "committed graph retained"
            )
        reservation = dict(
            id="request:" + str(len(cp["reservations"]) + 1),
            batch_id=unit["id"],
            input_digest=identity(payload),
            base_revision=cp["graph_revision"],
            reserved_tokens=input_estimate,
            output_token_limit=None,
            input_estimate=input_estimate,
            estimation=profile["estimation"],
            input_profile=profile,
            state="unknown",
        )
        cp["reservations"].append(reservation)
        cp["usage"]["calls"] += 1
        cp["usage"]["unreported_calls"] += 1
        timeout = min(settings["max_call_seconds"], remaining_seconds())
        for eid in unit["extra"]:
            pointer = registry[eid]["ref"]
            if pointer not in cp["delivered_ranges"]:
                cp["delivered_ranges"].append(pointer)
        await save()
        meta = dict(
            analysis_stage=unit["id"],
            evaluation_activity="repair" if unit["repair"] else "read_review" if unit["extra"] else "scan",
            output_token_limit=None,
            timeout_seconds=timeout,
            prompt_chars=chars,
            input_estimate=input_estimate,
            input_profile=profile,
            activity_label=unit["id"],
            repair_attempt=unit["repair"],
        )
        await emit(dict(type="llm.requested", **meta, messages=messages, model=expected["model"]))
        start = time.monotonic()
        try:
            wrapped = protocol_provider(evaluator_provider(provider, output_tokens=None, timeout_seconds=timeout), protocol(allow_reads), cp["mode"])
            async with asyncio.timeout(timeout):
                response = await request_llm_response(wrapped, messages, tools=None)
        except TimeoutError as exc:
            message = f"Evaluator request timed out after {timeout:g}s at {unit['id']}; accepted records retained"
            await emit(dict(type="llm.failed", **meta, error=message))
            await save()
            raise ValueError(message) from exc
        except BaseException:
            await emit(dict(type="llm.failed", **meta))
            await save()
            raise
        if not response.ok:
            await emit(dict(type="llm.failed", **meta))
            await save()
            if unsupported_format(response.error) and cp["mode"] != "text":
                cp["mode"] = "text"
                continue
            raise ValueError(response.error.message)
        answer = response.unwrap()
        usage = {k: getattr(answer.usage, k, 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        if usage["total_tokens"] > 0:
            reservation.update(state="settled", usage=usage)
            cp["usage"]["unreported_calls"] -= 1
        for field, value in usage.items():
            cp["usage"][field] += value
        cp["responses"].append(
            dict(
                request_id=reservation["id"],
                input_digest=reservation["input_digest"],
                text=answer.content,
                finish_reason=answer.finish_reason,
                usage=usage,
                duration_seconds=time.monotonic() - start,
                activity=meta["evaluation_activity"],
                batch_id=unit["id"],
            )
        )
        await emit(dict(type="llm.completed", **meta, response=answer))
        await save()
        delivered_ids = {e for c in payload["cards"] for e in c["refs"]} | set(unit["extra"]) | set(boundary)
        delivered = {e: registry[e] for e in delivered_ids}
        records, errors = parse_updates(answer)
        rejected, requests = commit_updates(cp, records, batch_id=unit["id"], cards=cards, delivered=delivered,
                                           cutoff=cutoff, target_ids=set(unit["cards"]))
        errors += rejected
        cp["responses"][-1]["validation_errors"] = deepcopy(errors)
        await save()
        # commit_group replaces nested state; reacquire unit before persistence.
        unit = cp["units"][unit["id"]]
        pending[0] = unit
        missing = [c for c in unit["cards"] if c not in cp["assessments"] or cp["assessments"][c]["status"] == "needs_evidence"]
        if missing and not requests:
            errors.append(dict(reason="Cards need a terminal assessment (reviewed or explicit unknown)", card_ids=missing))
        if requests:
            try:
                if not allow_reads:
                    raise ValueError("Reading is closed; conclude unknown for unresolved questions")
                additions = read_requests(
                    source,
                    registry,
                    requests,
                    cutoff=cutoff,
                    inspected=cp["reviewed_ranges"],
                    remaining_chars=min(settings["max_evidence_chars"], 24000) - cp["read_chars"],
                )
                for entry in additions:
                    registry[entry["id"]] = entry
                    cp["extra_evidence"][entry["id"]] = entry
                    cp["reviewed_ranges"].append(entry["ref"])
                    cp["read_chars"] += len(entry["text"])
                unit.update(extra=list(dict.fromkeys(unit["extra"] + [e["id"] for e in additions])), read_round=unit["read_round"] + 1, feedback=errors)
                await save()
                continue
            except ValueError as exc:
                unit["conclude"] = True
                errors.append(dict(reason=str(exc)))
        if errors and unit["repair"] < 1:
            unit.update(repair=unit["repair"] + 1, feedback=errors)
            await save()
            continue
        unit.update(complete=not errors, feedback=errors)
        cp["conflicts"] = [c for c in cp["conflicts"] if c["batch_id"] != unit["id"]]
        cp["conflicts"] += [dict(batch_id=unit["id"], **e) for e in errors]
        await save()
        await emit(
            dict(
                type="evaluation.activity",
                stage=unit["id"],
                activity="graph.published",
                message=f"Graph revision {cp['graph_revision']} · {len(cp['assessments'])}/{len(cards)} cards processed",
            )
        )
        pending.pop(0)
    cp["complete"] = all(u["complete"] for u in cp["units"].values())
    await save()
    return ok(project(source, facts, run_id, cp, extraction))
