"""Resumable memory pass with separate evidence delivery and shared eval budgets."""

import asyncio
import inspect
import time
from copy import deepcopy

from loom.evaluation.evidence_delivery import cited_evidence, page_catalog, prepare, read_page
from loom.evaluation.goal_loops.attribution import action_attributions
from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.issues import EvaluationFailure, describe, issue, provider_failure, request_failure_event, request_timeout, stopped
from loom.evaluation.memory.contracts import protocol_for
from loom.evaluation.memory.evidence import card_evidence, sanitize
from loom.evaluation.memory.lifecycle import VERSION, dependencies, migrate
from loom.evaluation.memory.records import commit, commit_records
from loom.evaluation.memory.request import focus_repair, memory_input, messages_for, scope
from loom.evaluation.memory.validation import validate_record
from loom.evaluation.provider import evaluator_provider
from loom.evaluation.repair import provider_retry, reserve_repair
from loom.evaluation.rules import fatal
from loom.evaluation.work_items import settle, terminal
from loom.llm.api import request_llm_response
from loom.llm.structured import output_mode, protocol_provider, unsupported_format

VALIDATION_VERSION = "memory-validation.6"

# Compatibility exports; orchestration uses structured issues exclusively.
__all__ = ["review_memory", "commit", "validate_record", "messages_for", "protocol_for"]


async def review_memory(source, cp, *, run_id, provider, settings, save, emit, remaining_seconds):
    expected = dict(
        version=VERSION, source=source.source_sha256, run_id=run_id, graph_revision=cp["graph_revision"], model=getattr(provider, "model", "unknown")
    )
    memory = migrate(cp, expected)
    if memory.get('execution_complete'):
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
        if unit.get('dependency_changed'):
            unit.pop('delivery_span_ids', None)
            subset = [c for c in cards if c['id'] in {a['id'] for a in unit['scope']['actions']}]
            unit['scope'] = scope(cp, subset, memberships)
            unit['accepted_records'] = [{k: r[k] for k in ('kind', 'stage_id', 'rating', 'title', 'summary') if k in r}
                                        for r in memory['records'].values() if r['unit_id'] == unit['id'] and r['publication_state'] == 'published']
            if unit['mode'] == 'synthesis':
                inputs = [memory['records'][key] for key in unit.get('source_record_ids', [])
                          if key in memory['records'] and memory['records'][key].get('publication_state') == 'published']
                if not inputs:
                    settle(unit, [issue('memory.dependencies_pending', 'No current published supporting records remain',
                                        category='coverage', stage='memory', unit_id=unit['id'])])
                    await persist()
                    return
                unit['memories'] = [memory_input(r) for r in inputs]
                unit['evidence'] = cited_evidence(inputs, [memory['units'][r['unit_id']] for r in inputs])
                unit.pop('delivery_span_ids', None)
            unit.pop('dependency_changed')
        prepare(unit)
        if unit.get('repair_targets'):
            focus_repair(unit)
        unit.setdefault('pages', page_catalog(unit, source) if unit['mode'] == 'extract' else {})
        while not terminal(unit):
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
                reserved, requested = unit.pop('repair_reserved', False), unit.pop('retry_requested', False)
                is_repair = bool(reserved or requested)
                if is_repair:
                    unit['repair_attempts'] = unit.get('repair_attempts', 0) + 1
                cp["usage"]["calls"] += 1
                cp["usage"]["unreported_calls"] += 1
                await persist()
                meta = dict(
                    analysis_stage=unit["id"],
                    evaluation_activity="memory",
                    activity_label="Repairing memory candidates" if is_repair else "Extracting trace memory",
                    repair_attempt=unit.get('repair_attempts', 0),
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
                    retry_request = provider_retry(cp, failure, settings)
                    await persist()
                    if retry_request:
                        await asyncio.sleep(.1)
                        continue
                    raise failure from exc
                except EvaluationFailure as exc:
                    await emit(request_failure_event(meta, exc))
                    retry_request = provider_retry(cp, exc, settings)
                    await persist()
                    if retry_request:
                        await asyncio.sleep(.1)
                        continue
                    raise
                except BaseException as exc:
                    await emit(request_failure_event(meta, exc))
                    await persist()
                    raise
                answer = response.unwrap()
                cp['provider_failures'] = 0
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
                        manifest=deepcopy(unit['manifest']),
                        analysis_epoch=memory['epoch'],
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
            unit.update(issues=issues, errors=errors, validation_version=VALIDATION_VERSION)
            accepted = [r for r in memory['records'].values() if r['unit_id'] == unit['id'] and r.get('publication_state') == 'published']
            for record in accepted:
                record['dependencies'] = dependencies(record, cp)
            settle(unit, issues, accepted=len(accepted), unknown=any(r.get('rating') == 'unknown' for r in accepted))
            for response_record in reversed(cp["responses"]):
                if response_record["batch_id"] == unit["id"]:
                    response_record.setdefault('revalidations', []).append(dict(version=VALIDATION_VERSION, issues=deepcopy(issues)))
                    response_record.setdefault("validation_errors", list(errors))
                    response_record.setdefault("validation_issues", deepcopy(issues))
                    response_record["validation_version"] = VALIDATION_VERSION
                    break
            await persist()
            if fatal(issues):
                raise EvaluationFailure('Memory request stopped; accepted records retained.', fatal(issues))
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
        if not terminal(unit):
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
            prepare(unit)
            unit['pages'] = page_catalog(unit, source)
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

    async def repairs(kind):
        for unit in list(memory['units'].values()):
            if unit['mode'] != kind or not unit.get('issues'):
                continue
            remaining = int('synthesis_ids' not in memory and any(r['kind'] != 'workflows' for r in memory['records'].values()))
            if not reserve_repair(cp, unit, unit['issues'], settings, remaining_work=remaining):
                continue
            if unit.get('requested_pages') and not unit.get('page_reads'):
                from loom.evaluation.issues import RuleViolation

                try:
                    extra = read_page(unit, unit['requested_pages'][0], source)
                    extra = [e for e in extra if e['id'] not in {r['id'] for r in unit['evidence']}]
                    chars = sum(len(e['text']) for e in extra)
                    if memory['read_chars'] + cp.get('read_chars', 0) + chars > settings['max_evidence_chars']:
                        raise stopped('budget.evidence_chars', 'Memory evidence page exceeds total evidence capacity', category='budget', stage='memory')
                    memory['read_chars'] += chars
                    unit['evidence'] += extra
                    unit['page_reads'] = 1
                    prepare(unit)
                except RuleViolation as exc:
                    unit['issues'].append(exc.issue)
                    unit.pop('repair_reserved', None)
                    await persist()
                    continue
            rejected = [r for r in memory['quarantine'].values() if r['unit_id'] == unit['id']]
            unit['repair_targets'] = [r['id'] for r in rejected]
            unit['rejected_records'] = rejected
            focus_repair(unit)
            unit.update(work_state='pending', complete=False)
            await persist()
            await evaluate(unit)

    await repairs('extract')

    # Consolidate reusable routes across actions/stages from cited memory records,
    # without retransmitting raw trace bodies. Large histories stay in bounded pages.
    if "synthesis_ids" not in memory:
        memory["synthesis_ids"] = [r["id"] for r in memory["records"].values()
                                   if r["kind"] != "workflows" and r.get('publication_state') == 'published' and r.get('citations')]
        memory["synthesis_cursor"] = 0
    # New or corrected facts after an explicit retry get a new synthesis input
    # generation. Existing workflows remain linked to their original input set.
    additional = [r['id'] for r in memory['records'].values() if r['kind'] != 'workflows'
                  and r.get('publication_state') == 'published' and r.get('citations') and r['id'] not in memory['synthesis_ids']]
    memory['synthesis_ids'] += additional
    while memory["synthesis_cursor"] < len(memory["synthesis_ids"]):
        ids = memory["synthesis_ids"][memory["synthesis_cursor"] : memory["synthesis_cursor"] + 16]
        while True:
            records = [memory["records"][key] for key in ids]
            units = [memory["units"][r["unit_id"]] for r in records]
            evidence = cited_evidence(records, units)
            scoped = {key: list({r["id"]: r for u in units for r in u["scope"][key]}.values()) for key in ("goals", "stages", "actions")}
            unit = dict(
                id="memory:synthesis:" + identity(ids),
                mode="synthesis",
                scope=scoped,
                evidence=evidence,
                memories=[memory_input(r) for r in records],
                complete=False,
                source_record_ids=ids,
            )
            prepare(unit)
            if messages_for(unit, mode)[1]["total_chars"] <= settings["max_prompt_chars"] or len(ids) == 1:
                break
            ids = ids[: max(1, len(ids) // 2)]
        memory["units"][unit["id"]] = unit
        memory["synthesis_cursor"] += len(ids)
        await persist()
        await evaluate(unit)
    await repairs('synthesis')
    memory['execution_complete'] = True
    memory["complete"] = all(u.get('complete') for u in memory['units'].values()) and not memory['quarantine']
    await persist()
