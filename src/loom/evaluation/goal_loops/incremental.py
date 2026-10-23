"""Chronological card review → atomic deltas → free graph/report projection."""

import asyncio
import inspect
import time
from copy import deepcopy

from jsonschema import Draft202012Validator

from loom.core import ok
from loom.evaluation.evidence_store import text_value
from loom.evaluation.goal_loops.attribution import action_attributions
from loom.evaluation.goal_loops.cards import VERSION as EXTRACTOR
from loom.evaluation.goal_loops.cards import compile_cards, link_cards
from loom.evaluation.goal_loops.checkpoint_repair import complete_framework_boundaries, reconcile_checkpoint
from loom.evaluation.goal_loops.facts import identity, run_facts
from loom.evaluation.goal_loops.graph_contract import ANALYZER, PROMPT, REQUEST, RESOURCE_LIMITS, SYSTEM, UPGRADABLE_VERSIONS, compact_contract, protocol
from loom.evaluation.goal_loops.graph_projection import project
from loom.evaluation.goal_loops.graph_state import initialize, occurrences
from loom.evaluation.goal_loops.graph_updates import commit_updates, parse_updates
from loom.evaluation.goal_loops.judge_metadata import VERSION as INPUT_VERSION
from loom.evaluation.goal_loops.judge_metadata import card_metadata, feedback_metadata, terminal_metadata_records
from loom.evaluation.goal_loops.packing import REQUEST_CHARS, bounded_state, request_profile, serialized
from loom.evaluation.goal_loops.structure import pending_structure_units, structure_pending, structure_review_context
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
            goals=[{**{k: g[k] for k in ("id", "parent_goal_id", "objective", "conditions", "goal_disposition")},
                    "root_relation": g.get("root_relation", "")} for g in selected],
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


def upgrade_budget_identity(checkpoint, saved_settings):
    """Recognize the old exact settings digest before separating capacity limits.

    The service passes the saved job settings, never the new requested settings.
    No source, model, protocol or semantic identity component is relaxed.
    """
    old = checkpoint.get("identity")
    legacy = {k: v for k, v in saved_settings.items() if k not in {"max_calls", "max_tokens", "max_seconds"}}
    if old and old.get("settings") == identity(legacy):
        old["settings"] = identity({k: v for k, v in saved_settings.items() if k not in RESOURCE_LIMITS})
        checkpoint.setdefault("resource_limits", {k: saved_settings[k] for k in RESOURCE_LIMITS})
        checkpoint.setdefault("resource_policy_upgrades", []).append(
            dict(revision=checkpoint.get("graph_revision", 0), reason="Character capacities are resumable resource limits")
        )


async def review(source, facts, provider, *, run_id, checkpoint, save_checkpoint, event_sink, settings, remaining_seconds):
    view = run_facts(source, facts, run_id)
    extraction = checkpoint.get("extraction") or compile_cards(source, facts, view)
    cards, registry = extraction["cards"], dict(extraction["registry"])
    extraction["metadata_links"] = link_cards(cards)
    judge_cards = {c["id"]: card_metadata(source, c, registry) for c in cards}
    extraction["registry"] = registry
    semantic_settings = {k: v for k, v in settings.items() if k not in RESOURCE_LIMITS and k != "extract_memory"}
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
    upgrade_budget_identity(cp, settings)
    if cp.get("identity") and cp["identity"] != expected:
        old = cp["identity"]
        if (old.get("analyzer"), old.get("prompt")) not in UPGRADABLE_VERSIONS or {**old, "analyzer": ANALYZER, "prompt": PROMPT} != expected:
            raise ValueError("Incremental graph identity changed; create a new evaluation")
        cp.setdefault("protocol_upgrades", []).append(dict(from_prompt=old["prompt"], to_prompt=PROMPT, revision=cp["graph_revision"]))
    if any(settings[k] < value for k, value in cp.get("resource_limits", {}).items() if k != "max_tokens"):
        raise ValueError("Resuming cannot reduce resource limits")
    for key, value in initialize(view).items():
        cp.setdefault(key, value)
    cp["identity"] = expected
    cp["memory_requested"] = settings.get("extract_memory", False)
    cp["resource_limits"] = {k: settings[k] for k in RESOURCE_LIMITS}
    cp["extraction"] = extraction
    cp["input_policy"] = dict(version=INPUT_VERSION, source_reads=False, raw_tool_outputs=False, trace_history=False)
    cp["total_cards"] = len(cards)
    cp.setdefault("mode", output_mode(provider))
    cp.setdefault("read_chars", 0)
    cp.setdefault("units", {})
    card_map = {c["id"]: c for c in cards}
    registry.update(cp.get("extra_evidence", {}))
    cp.setdefault("extra_evidence", {})
    cp.setdefault("delivered_ranges", [])

    async def save():
        cp["action_attributions"] = action_attributions(cp, cards)
        cp["pending_structure_units"] = pending_structure_units(cp)
        value = save_checkpoint(deepcopy(cp))
        if inspect.isawaitable(value):
            await value

    async def emit(event):
        value = event_sink.emit(event)
        if inspect.isawaitable(value):
            value = await value
        if value is not None and not value.ok:
            raise ValueError(value.error.message)

    reconcile_checkpoint(cp, cards, metadata=judge_cards)
    # A resumable unit retains its cutoff and bounded read state. Failed earlier
    # groups are retried without exposing judgments from later windows.
    for unit in cp["units"].values():
        if structure_pending(cp, unit):
            unit["complete"] = False
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
        # A small previous-action frontier carries metadata, never raw historical
        # evidence slices or the complete tree. Source pointers remain local.
        frontier, boundary = [], {}
        prior_actions = []
        before = min(s["start_line"] for s in subset)
        for card in [c for c in cards if c["id"] in local["assessments"] and c["end_line"] < before][-2:]:
            assessment = local["assessments"][card["id"]]
            supplied = judge_cards[card["id"]]["fields"][:1]
            for field in supplied:
                boundary[field["ref"]] = {**registry[field["ref"]], "text": field["text"]}
            prior_actions.append(dict(id=card["id"], kind=card["kind"], end_line=card["end_line"],
                                      stage_ids=assessment["stage_ids"], delta=assessment["delta"][:120],
                                      evidence_ids=[f["ref"] for f in supplied]))
        # Preserve exact occurrence IDs for compatibility, without retransmitting
        # original output/response text from earlier judgments.
        for occurrence in occurrences(local, cards)[-2:]:
            matching = next((r for r in prior_actions if r["id"] in occurrence["card_ids"]), None)
            frontier.append({**{k: occurrence[k] for k in ("id", "stage_id", "goal_id", "end_line")},
                             "evidence_ids": matching["evidence_ids"] if matching else []})
        allow_reads = False
        known_state = context(local, subset, cutoff)
        known_ids = {r["id"] for key in ("roots", "goals", "stages") for r in known_state[key]}
        aliases = {k: v for k, v in cp.get("record_aliases", {}).get(unit["id"], {}).items() if v in known_ids}
        accepted_ids = [c["id"] for c in subset if c["id"] in cp["assessments"] and cp["assessments"][c["id"]]["status"] != "needs_evidence"]
        structure_only = len(accepted_ids) == len(subset) and structure_pending(cp, unit) and not any(
            e.get("record_type") not in {"structure", "goals", "stages", "attributions"} for e in unit.get("feedback", [])
        )
        payload = dict(
            batch_id=unit["id"],
            base_graph_revision=cp["graph_revision"],
            knowledge_cutoff=cutoff,
            **known_state,
            input_version=INPUT_VERSION,
            cards=[judge_cards[c["id"]] for c in subset],
            extra_evidence=[],
            review_control=dict(mode="structure_only" if structure_only else "metadata_only", remaining_reads=0),
            feedback=feedback_metadata(unit.get("feedback", [])),
            frontier=frontier,
            prior_actions=prior_actions,
            accepted_card_ids=accepted_ids,
            local_aliases=dict(list(aliases.items())[-12:]),
            boundary_evidence=[{k: e[k] for k in ("id", "text")} for e in boundary.values()],
        )
        if structure_only:
            payload["structure_review"] = structure_review_context({**local, "extraction": extraction}, unit)
        response_protocol = protocol(allow_reads, metadata_only=True, structure_only=structure_only)
        if cp["mode"] != "json_schema":
            payload["contract"] = compact_contract(cp["mode"], metadata_only=True, structure_only=structure_only)
        messages = [
            LlmMessage(
                role="system", content=SYSTEM + ("\nNo more evidence reads; conclude with supported findings or explicit unknown." if not allow_reads else "")
            ),
            LlmMessage(role="user", content=serialized(payload)),
        ]
        profile = request_profile(messages, payload, cp["mode"], response_protocol)
        chars = profile["total_chars"]
        if chars > min(settings["max_prompt_chars"], REQUEST_CHARS) and unit["id"] not in cp["units"] and len(subset) > 1:
            half = subset[: max(1, len(subset) // 2)]
            unit.update(id="batch:" + identity([c["id"] for c in half]), cards=[c["id"] for c in half], cutoff=max(c["end_line"] for c in half))
            continue
        if chars > settings["max_prompt_chars"]:
            await save()
            raise ValueError(
                f"Evaluation input budget reached: {chars} chars including response schema, "
                f"limit {settings['max_prompt_chars']} (Request characters · per call / max_prompt_chars), "
                f"{len(subset)} cards; checkpoint retained. Increase Request characters · per call, then Resume evaluation; "
                "Evidence characters controls cumulative evidence reads, not request size"
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
        await save()
        meta = dict(
            analysis_stage=unit["id"],
            evaluation_activity="repair" if unit["repair"] else "scan",
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
            wrapped = protocol_provider(
                evaluator_provider(provider, output_tokens=None, timeout_seconds=timeout), response_protocol, cp["mode"]
            )
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
        delivered_ids = {e for c in payload["cards"] for e in c["refs"]} | set(boundary)
        cp["responses"][-1]["delivered_ref_ids"] = sorted(delivered_ids)
        delivered = {e: registry[e] for e in delivered_ids}
        records, errors = parse_updates(answer)
        rejected, _ = commit_updates(cp, terminal_metadata_records(
            records, normalizations=cp.setdefault("protocol_normalizations", []), batch_id=unit["id"]), batch_id=unit["id"], cards=cards, delivered=delivered,
                                           cutoff=cutoff, target_ids=set(unit["cards"]))
        errors += rejected
        cp["responses"][-1]["validation_errors"] = deepcopy(errors)
        complete_framework_boundaries(cp, cards, unit)
        await save()
        # commit_group replaces nested state; reacquire unit before persistence.
        unit = cp["units"][unit["id"]]
        pending[0] = unit
        missing = [c for c in unit["cards"] if c not in cp["assessments"] or cp["assessments"][c]["status"] == "needs_evidence"]
        if missing:
            errors.append(dict(reason="Cards need a terminal assessment (reviewed or explicit unknown)", card_ids=missing))
        if structure_pending(cp, unit):
            errors.insert(0, dict(record_type="structure",
                                  reason="Subgoal analysis pending: link actions to result-oriented child goals, or explain single_goal with current refs"))
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
    if cp["complete"] and cp["memory_requested"]:
        from loom.evaluation.memory.review import review_memory

        await review_memory(source, cp, run_id=run_id, provider=provider, settings=settings,
                            save=save, emit=emit, remaining_seconds=remaining_seconds)
    return ok(project(source, facts, run_id, cp, extraction))
