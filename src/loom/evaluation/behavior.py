"""Explicit v3 offline entry point and self-contained versioned artifacts."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from loom.core import err, make_loom_error, new_loop_id, new_run_id, now_iso, ok
from loom.evaluation.behavior_contracts import ANALYZER_VERSION, PROMPT_VERSION, SCHEMA_VERSION
from loom.evaluation.behavior_facts import build_behavior_facts
from loom.evaluation.behavior_judge import empty_semantic, judge_behavior, partial_semantic
from loom.evaluation.evidence_store import EvidenceStore
from loom.evolution.behavior import proposals_from_behavior
from loom.trace_analysis import build_episode_graph


@dataclass(frozen=True)
class BehaviorArtifacts:
    evaluation_bundle_path: Path
    report_path: Path


@dataclass(frozen=True)
class BehaviorResult:
    graph: object
    facts: dict
    semantic: dict
    artifacts: BehaviorArtifacts
    report: str


def evaluation_payload(source, facts, semantic, *, model=None, checkpoint_identity=None):
    evaluator = {"model": model, "analyzer_version": ANALYZER_VERSION, "prompt_version": PROMPT_VERSION}
    if checkpoint_identity:
        evaluator.update(resolved_model=checkpoint_identity.get("model"), model_options_digest=checkpoint_identity.get("model_options_digest"))
    return {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": source.source_sha256,
        "base": facts["base"],
        "facts": facts,
        "semantic": semantic,
        "evaluator": evaluator,
        "proposals": proposals_from_behavior(semantic, source_sha256=source.source_sha256, evaluator=evaluator),
    }


def render_report(payload):
    base, semantic = payload["base"], payload["semantic"]
    lines = [
        "# Behavior evaluation v3",
        "",
        f"Source SHA-256: `{payload['source_sha256']}`",
        "",
        "## Base evaluation",
        "",
        f"Goal outcome: **{base['task_completion']}**",
        "",
        f"Model calls: {base['metrics']['model_calls']} · Tool calls: {base['metrics']['tool_calls']} · Steps: {base['metrics']['steps']}",
        "",
    ]
    for o in base["outcomes"]:
        lines.extend([f"### {o['episode_id']}", "", f"{o['status']} · goal coverage: {o['goal_coverage']}", ""])
        lines.extend(f"- {c['status']}: {c['description']}" for c in o["criteria"])
        lines.append("")
    if base.get("statistics"):
        from loom.evaluation.base_report import render_base_report

        lines = lines[:4] + render_base_report(base)
    lines.extend(
        [
            "## Deep evaluation",
            "",
            f"Pipeline: {semantic['coverage']['status']}",
            "",
            f"Attempted segments: {semantic['coverage']['attempted_segments']}; supported segments: {semantic['coverage']['supported_segments']}",
            "",
            semantic.get("summary", ""),
            "",
        ]
    )
    for d in semantic["diagnoses"]:
        lines.extend([f"### {d['dimension']} · {d['epistemic_status']}", "", d["observation"], "", d["mechanism"], ""])
    for limitation in semantic["coverage"]["limitations"]:
        lines.extend([f"- Limitation: {limitation}"])
    return "\n".join(lines) + "\n"


async def analyze_behavior(config, *, judge_provider=None, event_sink=None):
    from loom.evaluation.analyze import _create_judge_provider, _emit_event

    run_id, loop_id = new_run_id(), new_loop_id()
    emitted = await _emit_event(
        event_sink,
        {
            "type": "run.started",
            "run_id": run_id,
            "loop_id": loop_id,
            "at": now_iso(),
            "metadata": {"role": "behavior evaluation", "analysis_version": "v3", "objective": f"Evaluate {config.trace_path}"},
        },
    )
    if not emitted.ok:
        return emitted
    try:
        source = EvidenceStore.open(config.trace_path)
        facts = build_behavior_facts(source, task=config.task)
        semantic = empty_semantic(facts)
        out = config.out_dir
        out.mkdir(parents=True, exist_ok=True)
        ledger_names = ("goal_revisions", "plan_revisions", "episodes", "segments", "artifact_versions", "verification_receipts", "token_ledger")
        targets = [out / name for name in ("evaluation-bundle.json", "report.md", "behavior-checkpoint.json")]
        extra_targets = [out / "source-trace.jsonl", *(out / f"{name}.jsonl" for name in ledger_names)]
        if any(p.resolve() == source.path.resolve() or (p.exists() and p.samefile(source.path)) for p in targets + extra_targets):
            raise ValueError("Output would overwrite the registered source trace")
        checkpoint = {}
        if config.judge:
            created = ok(judge_provider) if judge_provider is not None else _create_judge_provider(config)
            if not created.ok:
                return created
            cp_path = targets[2]
            if cp_path.exists():
                checkpoint = json.loads(cp_path.read_text())

            def save(value):
                temp = cp_path.with_suffix(".tmp")
                temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
                temp.replace(cp_path)

            try:
                semantic = (
                    await judge_behavior(
                        source,
                        facts,
                        created.value,
                        event_sink=event_sink,
                        run_id=run_id,
                        loop_id=loop_id,
                        stream=config.stream,
                        checkpoint=checkpoint,
                        save_checkpoint=save,
                        max_evidence_chars=config.judge_max_evidence_chars,
                        max_prompt_chars=config.judge_max_prompt_chars,
                        max_read_rounds=config.judge_max_read_rounds,
                        max_calls=config.judge_max_calls,
                        max_tokens=config.judge_max_tokens,
                        max_segments=config.judge_max_segments,
                    )
                ).unwrap()
            except ValueError as exc:
                if "Checkpoint" in str(exc):
                    raise
                checkpoint.setdefault("limitations", []).append(str(exc))
                save(checkpoint)
                semantic = partial_semantic(facts, checkpoint)
        payload = evaluation_payload(source, facts, semantic, model=config.model_name, checkpoint_identity=checkpoint.get("identity"))
        payload["artifacts"] = {}
        blobs = {
            "source-trace": source.source_bytes,
            **{name: "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in facts[name]).encode() for name in ledger_names},
        }
        for name, data in blobs.items():
            path = out / f"{name}.jsonl"
            path.write_bytes(data)
            payload["artifacts"][name] = {"path": path.name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        report = render_report(payload)
        targets[0].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        targets[1].write_text(report, encoding="utf-8")
        emitted = await _emit_event(
            event_sink,
            {
                "type": "run.completed",
                "run_id": run_id,
                "loop_id": loop_id,
                "at": now_iso(),
                "outcome": "pass",
                "analysis_status": semantic["coverage"]["status"],
                "task_verified": True if facts["base"]["task_completion"] == "achieved" else None,
                "goal_outcome": facts["base"]["task_completion"],
                "artifacts": {"evaluation_bundle_path": str(targets[0]), "report_path": str(targets[1])},
            },
        )
        if not emitted.ok:
            return emitted
        return ok(BehaviorResult(build_episode_graph(source.events), facts, semantic, BehaviorArtifacts(*targets[:2]), report))
    except (OSError, ValueError, TypeError) as exc:
        return err(make_loom_error("VALIDATION_FAILED", f"Behavior evaluation failed: {exc}", retryable=False))
