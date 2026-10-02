"""Offline version-two trace analysis orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from loom.core import Result, Trace, make_loom_error, new_loop_id, new_run_id, now_iso, ok
from loom.evaluation.analysis_report import EffectivenessArtifacts, write_effectiveness_artifacts
from loom.evaluation.diagnostics import DIMENSIONS, FactAnalysis
from loom.evaluation.effectiveness_judge import SemanticAnalysis, judge_effectiveness, unverified_criteria
from loom.evaluation.evidence_store import EvidenceStore
from loom.trace_analysis import EpisodeGraph, build_episode_graph


@dataclass(frozen=True, slots=True)
class EffectivenessResult:
    graph: EpisodeGraph
    facts: FactAnalysis
    semantic: SemanticAnalysis
    artifacts: EffectivenessArtifacts
    report: str


async def analyze_effectiveness(config: Any, *, judge_provider: Any = None, event_sink: Any = None) -> Result:
    from loom.evaluation.analyze import _create_judge_provider, _emit_event, _finish_analysis_error

    run_id, loop_id = new_run_id(), new_loop_id()
    started = time.monotonic()
    emitted = await _emit_event(event_sink, {"type": "run.started", "run_id": run_id, "loop_id": loop_id,
                                           "context_id": str(config.trace_path), "at": now_iso(),
                                           "metadata": {"role": "trace effectiveness analyzer", "analysis_version": "v2",
                                                        "objective": f"Analyze {config.trace_path}", "judge": config.judge}})
    if not emitted.ok:
        return emitted
    try:
        store = EvidenceStore.open(config.trace_path)
        graph = build_episode_graph(store.events)
        from loom.evaluation.trajectory import build_fact_analysis

        facts = build_fact_analysis(store, graph, task=config.task)
    except (OSError, ValueError, TypeError) as exc:
        return await _finish_analysis_error(event_sink, run_id, loop_id, started,
                                            make_loom_error("VALIDATION_FAILED", f"Could not analyze source trace: {exc}", retryable=False))
    for step in graph.steps:
        trace_id = f"{run_id}:{step.id}"
        at = now_iso()
        base = {"run_id": run_id, "loop_id": loop_id, "trace_id": trace_id, "step_number": step.step_number,
                "source_run_id": step.run_id, "source_loop_id": step.loop_id, "source_trace_id": step.trace_id, "at": at}
        emitted = await _emit_event(event_sink, {"type": "step.started", **base})
        if not emitted.ok:
            return emitted
        trace = Trace(id=trace_id, run_id=run_id, loop_id=loop_id, loop_version="trace-effectiveness.1", step_number=step.step_number,
                      root_trace_id=trace_id, started_at=at, ended_at=at, duration_ms=0, input_context_id=str(config.trace_path),
                      output_context_id=str(config.out_dir), outcome="facts_recorded",
                      metadata={"source_run_id": step.run_id, "source_loop_id": step.loop_id, "source_trace_id": step.trace_id})
        emitted = await _emit_event(event_sink, {"type": "step.completed", **base, "trace": trace})
        if not emitted.ok:
            return emitted
    semantic = SemanticAnalysis((), unverified_criteria(facts), (), (),
                                {"status": "not_evaluated", "dimensions": {d: "not_evaluated" for d in DIMENSIONS}}, {})
    if config.judge:
        provider = ok(judge_provider) if judge_provider is not None else _create_judge_provider(config)
        if not provider.ok:
            return await _finish_analysis_error(event_sink, run_id, loop_id, started, provider.error)
        judged = await judge_effectiveness(store, facts, provider.value, stream=config.stream, event_sink=event_sink,
                                           run_id=run_id, loop_id=loop_id, max_read_rounds=config.judge_max_read_rounds,
                                           max_evidence_chars=config.judge_max_evidence_chars,
                                           max_prompt_chars=config.judge_max_prompt_chars, batch_rounds=config.judge_batch_rounds)
        if not judged.ok:
            return await _finish_analysis_error(event_sink, run_id, loop_id, started, judged.error)
        semantic = judged.value
    try:
        artifacts = write_effectiveness_artifacts(config.out_dir, store, facts, semantic,
                                                 config={"analysis_version": "v2", "task": config.task, "judge": config.judge,
                                                         "stream": config.stream, "model_name": config.model_name,
                                                         "max_read_rounds": config.judge_max_read_rounds,
                                                         "max_evidence_chars": config.judge_max_evidence_chars,
                                                         "max_prompt_chars": config.judge_max_prompt_chars,
                                                         "batch_rounds": config.judge_batch_rounds})
        report = artifacts.report_path.read_text(encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        return await _finish_analysis_error(event_sink, run_id, loop_id, started,
                                            make_loom_error("VALIDATION_FAILED", f"Could not write effectiveness artifacts: {exc}", retryable=False))
    emitted = await _emit_event(event_sink, {"type": "evaluation.artifacts.written", "run_id": run_id, "loop_id": loop_id,
                                           "artifacts": {"evaluation_bundle_path": str(artifacts.evaluation_bundle_path),
                                                         "report_path": str(artifacts.report_path)}, "at": now_iso()})
    if not emitted.ok:
        return emitted
    emitted = await _emit_event(event_sink, {"type": "run.completed", "run_id": run_id, "loop_id": loop_id, "outcome": "pass",
                                           "steps": len(graph.steps), "analysis_status": semantic.coverage["status"],
                                           "task_verified": None, "diagnosis_count": len(semantic.diagnoses),
                                           "duration_ms": int((time.monotonic() - started) * 1000), "at": now_iso()})
    if not emitted.ok:
        return emitted
    return ok(EffectivenessResult(graph, facts, semantic, artifacts, report))
