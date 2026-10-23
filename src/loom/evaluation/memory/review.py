"""Resumable memory pass with separate evidence delivery and shared eval budgets."""

import asyncio
import inspect
import time
from copy import deepcopy

from jsonschema import Draft202012Validator

from loom.evaluation.goal_loops.attribution import action_attributions
from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.goal_loops.packing import serialized
from loom.evaluation.memory import VERSION
from loom.evaluation.memory.contracts import FIELDS, PROTOCOL, SYSTEM
from loom.evaluation.memory.evidence import card_evidence, sanitize
from loom.evaluation.provider import evaluator_provider
from loom.llm.api import LlmMessage, LlmResponse, request_llm_response
from loom.llm.structured import native_schema, output_mode, parse_response, protocol_provider, unsupported_format

VALIDATION_VERSION = "memory-validation.2"


def scope(cp, cards, memberships):
    ids = {c["id"] for c in cards}
    links = [r for r in memberships if r["card_id"] in ids]
    stage_ids = {r["stage_id"] for r in links if r["stage_id"]}
    goals = {g["id"]: g for g in cp["entities"]["goals"]}
    goal_ids = {r["goal_id"] for r in links}
    for gid in list(goal_ids):
        while goals.get(gid, {}).get("parent_goal_id"):
            gid = goals[gid]["parent_goal_id"]
            goal_ids.add(gid)
    return dict(
        goals=[
            {
                **{k: g[k] for k in ("id", "parent_goal_id", "objective")},
                "conditions": [serialized(c)[:400] for c in g.get("conditions", [])[:12]],
                "omitted_conditions": max(0, len(g.get("conditions", [])) - 12),
            }
            for g in goals.values()
            if g["id"] in goal_ids
        ],
        stages=[{k: s[k] for k in ("id", "goal_id", "title", "kind")} for s in cp["stages"].values() if s["id"] in stage_ids],
        actions=[
            dict(
                id=c["id"],
                kind=c["kind"],
                name=c["name"],
                status=c["status"],
                start_line=c["start_line"],
                end_line=c["end_line"],
                memberships=[{k: r[k] for k in ("goal_id", "stage_id", "basis")} for r in links if r["card_id"] == c["id"]],
            )
            for c in cards
        ],
    )


def validate_record(kind, row, unit):
    Draft202012Validator(FIELDS[kind]["items"]).validate(row)
    if sanitize(row) != row or "[REDACTED]" in serialized(row):
        raise ValueError("Memory must not contain credentials or redacted values")
    stages = {s["id"]: s for s in unit["scope"]["stages"]}
    goals = {g["id"] for g in unit["scope"]["goals"]}
    actions = {c["id"] for c in unit["scope"]["actions"]}
    if kind == "contributions":
        if row["stage_id"] not in stages or row["goal_id"] != stages[row["stage_id"]]["goal_id"]:
            raise ValueError("Contribution must target the delivered stage and its owning goal")
        if not set(row["key_action_ids"]) <= actions:
            raise ValueError("Unknown key action")
        for cid in row["key_action_ids"]:
            action = next(c for c in unit["scope"]["actions"] if c["id"] == cid)
            if not any(m["stage_id"] == row["stage_id"] for m in action["memberships"]):
                raise ValueError("Key action is not attributed to this stage")
        if row["rating"] == "key" and not row["key_action_ids"]:
            raise ValueError("Key contribution requires key actions")
    elif not (set(row["goal_ids"]) <= goals and set(row["stage_ids"]) <= set(stages) and set(row["action_ids"]) <= actions):
        raise ValueError("Memory refers to undelivered goal, stage or action")
    elif not row["goal_ids"]:
        raise ValueError("Memory needs a goal scope")
    evidence = {e["id"]: e for e in unit["evidence"]}
    refs, cited = [], []
    for citation in row["citations"]:
        entry = evidence.get(citation["ref"])
        if not entry or not citation["quote"].strip() or citation["quote"] not in entry["text"]:
            raise ValueError("Citation must quote evidence delivered in this memory request")
        if entry.get("quotes") and not any(citation["quote"] in q for q in entry["quotes"]):
            raise ValueError("Synthesis citation must stay inside an original quote")
        cited.append(entry)
        if entry["ref"] not in refs:
            refs.append(entry["ref"])
    if not refs and not (kind == "contributions" and row["rating"] == "unknown"):
        raise ValueError("Memory claim requires source evidence")
    record = deepcopy(row)
    if kind != "contributions":
        owners = {e["action_id"] for e in cited}
        if not owners <= actions:
            raise ValueError("Cited evidence owner is outside the delivered action scope")
        # Evidence ownership is host metadata, not another semantic judgment for
        # the model to reproduce. In particular, verification boundaries often
        # support a fact alongside the tool actions the model lists explicitly.
        record["action_ids"] = sorted(set(row["action_ids"]) | owners)
        if len(record["action_ids"]) > FIELDS[kind]["items"]["properties"]["action_ids"]["maxItems"]:
            raise ValueError("Memory action scope exceeds the record limit")
        if added := sorted(owners - set(row["action_ids"])):
            record["normalizations"] = [dict(rule="include_cited_evidence_owners", added_action_ids=added)]
        observed = (
            kind == "facts"
            and row["basis"] == "observed"
            or kind == "lessons"
            and row["validation"] == "observed_recovery"
            or kind == "workflows"
            and row["validation"] == "observed"
        )
        if observed and not any(e["basis"] == "observed_output" for e in cited):
            raise ValueError("Observed memory requires recorded tool output; intent alone is not observation")
    elif cited:
        related = {a["id"] for a in unit["scope"]["actions"] if any(m["stage_id"] == row["stage_id"] for m in a["memberships"])}
        if not any(e["action_id"] in related for e in cited):
            raise ValueError("Contribution needs evidence from its stage's actions")
    return {**record, "evidence_refs": deepcopy(refs)}


def commit(memory, unit, response):
    value, _ = parse_response(LlmResponse(content=response), PROTOCOL.max_response_chars)
    if not isinstance(value, dict) or set(value) - set(FIELDS):
        raise ValueError("Return a memory object with contributions, facts, lessons and workflows arrays")
    errors = []
    accepted_stages = {r["stage_id"] for r in memory["records"].values() if r["unit_id"] == unit["id"] and r["kind"] == "contributions"}
    for kind in FIELDS:
        rows = value.get(kind)
        if not isinstance(rows, list) or len(rows) > FIELDS[kind]["maxItems"]:
            errors.append(f"{kind}: missing or oversized array")
            continue
        if unit["mode"] == "synthesis" and kind != "workflows" and rows:
            errors.append("Synthesis may only add workflows")
            continue
        if unit["mode"] == "extract" and kind == "workflows" and rows:
            errors.append("Extract facts, lessons and contributions first; workflows belong to synthesis")
            continue
        for row in rows:
            try:
                record = validate_record(kind, row, unit)
                # Contributions are replaced within a window; memories deduplicate
                # identical claims, while scoped contradictions remain visible.
                if kind == "contributions":
                    key = identity(kind, unit["id"], row["stage_id"])
                elif kind == "workflows":
                    key = identity(kind, unit["id"], sorted(row["goal_ids"]))
                else:
                    key = identity(kind, {k: record[k] for k in FIELDS[kind]["items"]["properties"]})
                record.update(
                    id="memory:" + key,
                    kind=kind,
                    unit_id=unit["id"],
                    source_digest=memory["identity"]["source"],
                    run_id=memory["identity"]["run_id"],
                    graph_revision=memory["identity"]["graph_revision"],
                )
                memory["records"][record["id"]] = record
                if kind == "contributions":
                    accepted_stages.add(row["stage_id"])
            except (ValueError, TypeError, KeyError) as exc:
                errors.append(f"{kind}: {str(exc)[:300]}")
            except Exception as exc:
                # JSON schema validation messages may contain raw model output.
                from jsonschema import ValidationError

                if not isinstance(exc, ValidationError):
                    raise
                errors.append(f"{kind}: invalid {exc.validator} at {'/'.join(map(str, exc.path))}")
    if unit["mode"] == "extract":
        missing = {s["id"] for s in unit["scope"]["stages"]} - accepted_stages
        if missing:
            errors.append("Rate every delivered stage, using unknown when needed: " + ", ".join(sorted(missing)))
    unit["accepted_records"] = [
        {k: r[k] for k in ("kind", "title", "summary", "stage_id", "rating") if k in r} for r in memory["records"].values() if r["unit_id"] == unit["id"]
    ][:48]
    return errors


def messages_for(unit, mode):
    payload = dict(
        mode=unit["mode"],
        **unit["scope"],
        evidence=[{k: e[k] for k in ("id", "text", "action_id", "basis", "truncated")} for e in unit["evidence"]],
        previous_memories=unit.get("memories", []),
        accepted_records=unit.get("accepted_records", []),
        feedback=unit.get("errors", []),
    )
    if mode != "json_schema":
        payload["response_contract"] = PROTOCOL.schema
    messages = [LlmMessage(role="system", content=SYSTEM), LlmMessage(role="user", content=serialized(sanitize(payload)))]
    schema = serialized(native_schema(PROTOCOL.schema)) if mode == "json_schema" else ""
    chars = sum(len(m.content) for m in messages) + len(schema) + 256
    estimate = sum(len(m.content.encode()) for m in messages) + len(schema.encode()) + 512
    return messages, dict(total_chars=chars, input_estimate=estimate, estimation="utf8_bytes_plus_schema_and_512_not_tokenizer")


async def review_memory(source, cp, *, run_id, provider, settings, save, emit, remaining_seconds):
    expected = dict(
        version=VERSION, source=source.source_sha256, run_id=run_id, graph_revision=cp["graph_revision"], model=getattr(provider, "model", "unknown")
    )
    memory = cp.setdefault("memory", dict(identity=expected, units={}, records={}, read_chars=0, cursor=0, complete=False))
    if memory["identity"] != expected:
        raise ValueError("Memory source or graph changed; create a new evaluation")
    if memory["complete"]:
        return
    cards, registry = cp["extraction"]["cards"], cp["extraction"]["registry"]
    memberships = action_attributions(cp, cards)
    mode = memory.get("output_mode", output_mode(provider))

    async def persist():
        result = save()
        if inspect.isawaitable(result):
            await result

    async def evaluate(unit):
        nonlocal mode
        repairs = 0
        if "pending_response" not in unit and unit.get("validation_version") != VALIDATION_VERSION:
            previous = next((r for r in reversed(cp["responses"]) if r.get("activity") == "memory" and r.get("batch_id") == unit["id"] and r.get("text")), None)
            if previous:
                unit["pending_response"] = previous["text"]
        while not unit.get("complete"):
            if "pending_response" not in unit:
                messages, profile = messages_for(unit, mode)
                if profile["total_chars"] > settings["max_prompt_chars"]:
                    raise ValueError("Memory input budget reached; increase Request characters per call and Resume evaluation")
                if cp["usage"]["calls"] >= settings["max_calls"] or remaining_seconds() <= 0:
                    raise ValueError("Memory evaluation budget reached; increase call/time limits and Resume evaluation")
                timeout = min(settings["max_call_seconds"], remaining_seconds())
                reservation = dict(
                    id="request:" + str(len(cp["reservations"]) + 1),
                    batch_id=unit["id"],
                    state="unknown",
                    input_digest=identity([m.content for m in messages]),
                    reserved_tokens=profile["input_estimate"],
                    input_estimate=profile["input_estimate"],
                    input_profile=profile,
                    output_token_limit=None,
                )
                cp["reservations"].append(reservation)
                cp["usage"]["calls"] += 1
                cp["usage"]["unreported_calls"] += 1
                await persist()
                meta = dict(
                    analysis_stage=unit["id"],
                    evaluation_activity="memory",
                    activity_label="Extracting trace memory",
                    prompt_chars=profile["total_chars"],
                    input_estimate=profile["input_estimate"],
                    input_profile=profile,
                    timeout_seconds=timeout,
                    output_token_limit=None,
                )
                await emit(dict(type="llm.requested", **meta, messages=messages, model=getattr(provider, "model", "unknown")))
                start = time.monotonic()
                try:
                    wrapped = protocol_provider(evaluator_provider(provider, output_tokens=None, timeout_seconds=timeout), PROTOCOL, mode)
                    async with asyncio.timeout(timeout):
                        response = await request_llm_response(wrapped, messages, tools=None)
                    if not response.ok:
                        if unsupported_format(response.error) and mode != "text":
                            mode = memory["output_mode"] = "text"
                            await emit(dict(type="llm.failed", **meta))
                            await persist()
                            continue
                        raise ValueError(response.error.message)
                except BaseException:
                    await emit(dict(type="llm.failed", **meta))
                    await persist()
                    raise
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
                        batch_id=unit["id"],
                        activity="memory",
                        usage=usage,
                        text=sanitize(answer.content),
                        finish_reason=answer.finish_reason,
                        duration_seconds=time.monotonic() - start,
                    )
                )
                unit["pending_response"] = sanitize(answer.content)
                await persist()
                await emit(dict(type="llm.completed", **meta, response=answer))
            try:
                errors = commit(memory, unit, unit["pending_response"])
            except ValueError as exc:
                errors = [str(exc)[:500]]
            unit.pop("pending_response", None)
            unit.update(errors=errors, complete=not errors, validation_version=VALIDATION_VERSION)
            for response_record in reversed(cp["responses"]):
                if response_record["batch_id"] == unit["id"]:
                    if response_record.get("validation_errors") != errors:
                        response_record.setdefault("validation_history", []).append(
                            dict(version=response_record.get("validation_version", "legacy"), errors=response_record.get("validation_errors", []))
                        )
                    response_record["validation_errors"] = list(errors)
                    response_record["validation_version"] = VALIDATION_VERSION
                    break
            await persist()
            if errors:
                if repairs >= 1:
                    raise ValueError("Memory validation incomplete; accepted records retained. Resume evaluation. " + "; ".join(errors[:2]))
                repairs += 1
        await emit(
            dict(
                type="evaluation.activity",
                stage=unit["id"],
                activity="memory.saved",
                message=f"Memory saved · {len(memory['records'])} evidence-linked records",
            )
        )

    # Finish interrupted units before admitting fresh evidence. Their exact delivered
    # slices and responses survive cancellation, format repair and budget increases.
    for unit in list(memory["units"].values()):
        if not unit.get("complete"):
            await evaluate(unit)
    while memory["cursor"] < len(cards):
        subset = cards[memory["cursor"] : memory["cursor"] + min(8, settings["batch_rounds"])]
        while True:
            evidence = [e for c in subset for e in card_evidence(source, c, registry)]
            unit = dict(
                id="memory:extract:" + identity([c["id"] for c in subset]),
                mode="extract",
                scope=scope(cp, subset, memberships),
                evidence=evidence,
                complete=False,
            )
            if messages_for(unit, mode)[1]["total_chars"] <= settings["max_prompt_chars"] or len(subset) == 1:
                break
            subset = subset[: max(1, len(subset) // 2)]
        chars = sum(len(e["text"]) for e in evidence)
        if memory["read_chars"] + cp.get("read_chars", 0) + chars > settings["max_evidence_chars"]:
            await persist()
            raise ValueError("Memory evidence budget reached; increase Evidence characters total and Resume evaluation")
        memory["read_chars"] += chars
        memory["units"][unit["id"]] = unit
        memory["cursor"] += len(subset)
        await persist()
        await evaluate(unit)

    # Consolidate reusable routes across actions/stages from cited memory records,
    # without retransmitting raw trace bodies. Large histories stay in bounded pages.
    if "synthesis_ids" not in memory:
        memory["synthesis_ids"] = [r["id"] for r in memory["records"].values() if r["kind"] != "workflows"]
        memory["synthesis_cursor"] = 0
    while memory["synthesis_cursor"] < len(memory["synthesis_ids"]):
        ids = memory["synthesis_ids"][memory["synthesis_cursor"] : memory["synthesis_cursor"] + 16]
        while True:
            records = [memory["records"][key] for key in ids]
            units = [memory["units"][r["unit_id"]] for r in records]
            cites = {c["ref"] for r in records for c in r["citations"]}
            evidence = {
                e["id"]: {
                    **e,
                    "quotes": list(dict.fromkeys(c["quote"] for r in records for c in r["citations"] if c["ref"] == e["id"])),
                    "text": "\n".join(dict.fromkeys(c["quote"] for r in records for c in r["citations"] if c["ref"] == e["id"])),
                }
                for u in units
                for e in u["evidence"]
                if e["id"] in cites
            }
            scoped = {key: list({r["id"]: r for u in units for r in u["scope"][key]}.values()) for key in ("goals", "stages", "actions")}
            unit = dict(
                id="memory:synthesis:" + identity(ids),
                mode="synthesis",
                scope=scoped,
                evidence=list(evidence.values()),
                memories=[{k: v for k, v in r.items() if k not in {"evidence_refs", "source_digest", "run_id", "graph_revision"}} for r in records],
                complete=False,
            )
            if messages_for(unit, mode)[1]["total_chars"] <= settings["max_prompt_chars"] or len(ids) == 1:
                break
            ids = ids[: max(1, len(ids) // 2)]
        memory["units"][unit["id"]] = unit
        memory["synthesis_cursor"] += len(ids)
        await persist()
        await evaluate(unit)
    memory["complete"] = True
    await persist()
