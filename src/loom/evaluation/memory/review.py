"""Resumable memory pass with separate evidence delivery and shared eval budgets."""

import asyncio
import inspect
import time
from copy import deepcopy

from loom.evaluation.goal_loops.attribution import action_attributions
from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.issues import EvaluationFailure, describe, provider_failure, request_failure_event, request_timeout, stopped
from loom.evaluation.memory import VERSION
from loom.evaluation.memory.checkpoint import prepare_replay
from loom.evaluation.memory.contracts import protocol_for
from loom.evaluation.memory.evidence import card_evidence, sanitize
from loom.evaluation.memory.records import commit, commit_records
from loom.evaluation.memory.request import messages_for, scope
from loom.evaluation.memory.validation import validate_record
from loom.evaluation.provider import evaluator_provider
from loom.evaluation.repair import begin_cycle, retry
from loom.llm.api import request_llm_response
from loom.llm.structured import output_mode, protocol_provider, unsupported_format

VALIDATION_VERSION = "memory-validation.5"

# Compatibility exports; orchestration uses structured issues exclusively.
__all__ = ["review_memory", "commit", "validate_record", "messages_for", "protocol_for"]


async def review_memory(source, cp, *, run_id, provider, settings, save, emit, remaining_seconds):
    expected = dict(
        version=VERSION, source=source.source_sha256, run_id=run_id, graph_revision=cp["graph_revision"], model=getattr(provider, "model", "unknown")
    )
    memory = cp.setdefault("memory", dict(identity=expected, units={}, records={}, read_chars=0, cursor=0, complete=False))
    if memory["identity"] != expected:
        raise stopped("checkpoint.memory_identity", "Memory source or graph changed; create a new evaluation", category="state", stage="memory")
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
        begin_cycle(unit)
        prepare_replay(cp, unit, VALIDATION_VERSION)
        while not unit.get("complete"):
            if "pending_response" not in unit:
                messages, profile = messages_for(unit, mode)
                if profile["total_chars"] > settings["max_prompt_chars"]:
                    raise stopped("budget.request_chars", "Memory input budget reached; increase Request characters per call and Resume evaluation",
                                  category="budget", stage="memory", unit_id=unit["id"],
                                  details=dict(limit=settings["max_prompt_chars"], actual_count=profile["total_chars"]))
                if cp["usage"]["calls"] >= settings["max_calls"] or remaining_seconds() <= 0:
                    raise stopped("budget.calls_or_time", "Memory evaluation budget reached; increase call/time limits and Resume evaluation",
                                  category="budget", stage="memory", unit_id=unit["id"],
                                  details=dict(calls=cp["usage"]["calls"], call_limit=settings["max_calls"], remaining_seconds=remaining_seconds()))
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
                    wrapped = protocol_provider(evaluator_provider(provider, output_tokens=None, timeout_seconds=timeout), protocol_for(unit), mode)
                    async with asyncio.timeout(timeout):
                        response = await request_llm_response(wrapped, messages, tools=None)
                    if not response.ok:
                        if unsupported_format(response.error) and mode != "text":
                            mode = memory["output_mode"] = "text"
                            await emit(request_failure_event(meta, response.error))
                            await persist()
                            continue
                        raise provider_failure(response.error, stage="memory", unit_id=unit["id"], timeout_seconds=timeout)
                except TimeoutError as exc:
                    failure = request_timeout(stage="memory", unit_id=unit["id"], timeout_seconds=timeout)
                    await emit(request_failure_event(meta, failure))
                    await persist()
                    raise failure from exc
                except BaseException as exc:
                    await emit(request_failure_event(meta, exc))
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
                        response_schema=deepcopy(protocol_for(unit).schema),
                        finish_reason=answer.finish_reason,
                        duration_seconds=time.monotonic() - start,
                    )
                )
                unit["pending_response"] = sanitize(answer.content)
                unit["pending_finish_reason"] = answer.finish_reason
                unit["pending_schema"] = deepcopy(protocol_for(unit).schema)
                await persist()
                await emit(dict(type="llm.completed", **meta, response=answer))
            issues = commit_records(memory, unit, unit["pending_response"], schema=unit.get("pending_schema"),
                                    finish_reason=unit.get("pending_finish_reason"))
            errors = [describe(e) for e in issues]
            unit.pop("pending_response", None)
            unit.pop("pending_schema", None)
            unit.pop("pending_finish_reason", None)
            unit.update(issues=issues, errors=errors, complete=not issues, validation_version=VALIDATION_VERSION)
            for response_record in reversed(cp["responses"]):
                if response_record["batch_id"] == unit["id"]:
                    if response_record.get("validation_errors") != errors:
                        response_record.setdefault("validation_history", []).append(
                            dict(version=response_record.get("validation_version", "legacy"), errors=response_record.get("validation_errors", []))
                        )
                    response_record["validation_errors"] = list(errors)
                    response_record["validation_issues"] = deepcopy(issues)
                    response_record["validation_version"] = VALIDATION_VERSION
                    break
            await persist()
            if issues:
                should_retry = retry(unit, issues)
                await persist()
                if not should_retry:
                    raise EvaluationFailure("Memory validation incomplete; accepted records retained. Resume evaluation.", issues)
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
            raise stopped("budget.evidence_chars", "Memory evidence budget reached; increase Evidence characters total and Resume evaluation",
                          category="budget", stage="memory", unit_id=unit["id"],
                          details=dict(limit=settings["max_evidence_chars"], actual_count=memory["read_chars"] + cp.get("read_chars", 0) + chars))
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
