"""Versioned factual ledgers, diagnostics and readable evidence coverage reports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from loom.core import now_iso
from loom.evaluation.diagnostics import ANALYZER_VERSION, DIMENSIONS, PROMPT_VERSION, SCHEMA_VERSION, FactAnalysis
from loom.evaluation.effectiveness_judge import SemanticAnalysis
from loom.evaluation.evidence_store import EvidenceStore


@dataclass(frozen=True, slots=True)
class EffectivenessArtifacts:
    out_dir: Path
    evaluation_bundle_path: Path
    report_path: Path
    paths: dict[str, Path]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_effectiveness_artifacts(
    out_dir: Path, store: EvidenceStore, facts: FactAnalysis, semantic: SemanticAnalysis, *, config: dict[str, Any],
) -> EffectivenessArtifacts:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = {"task_contracts": facts.task_contracts, "trajectory": facts.trajectory, "context_deltas": facts.context_deltas,
            "tool_uses": facts.tool_uses, "token_ledger": facts.token_ledger, "verification_evidence": facts.verification_evidence,
            "diagnoses": tuple(asdict(d) for d in semantic.diagnoses), "verification_coverage": semantic.verification,
            "round_analyses": semantic.round_analyses,
            "preserved_behaviors": semantic.preserved_behaviors, "verification_framework": semantic.verification_framework,
            "evidence_index": store.index()}
    paths = {key: out_dir / (key.replace("_", "-") + ".jsonl") for key in rows}
    targets = (*paths.values(), out_dir / "report.md", out_dir / "evaluation-bundle.json")
    if any(path.resolve() == store.path.resolve() or (path.exists() and path.samefile(store.path)) for path in targets):
        raise ValueError("Artifact output would overwrite the registered source trace")
    for key, items in rows.items():
        with paths[key].open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
    report_path = out_dir / "report.md"
    report_path.write_text(render_effectiveness_report(store, facts, semantic), encoding="utf-8")
    paths["report"] = report_path
    manifest_path = out_dir / "evaluation-bundle.json"
    _write_json(manifest_path, {
        "schema_version": SCHEMA_VERSION, "bundle_id": f"effectiveness:{store.source_sha256}", "created_at": now_iso(),
        "source_trace": {"path": str(store.path.resolve()), "sha256": store.source_sha256, "kind": "jsonl"},
        "analysis": {"analyzer_version": ANALYZER_VERSION, "prompt_version": PROMPT_VERSION, "config": config,
                     "source_coverage": store.coverage, "factual_coverage": facts.coverage,
                     "semantic_coverage": semantic.coverage, "analyzer_usage": semantic.usage},
        "summary": {key: len(items) for key, items in rows.items()},
        "artifacts": {key: path.name for key, path in paths.items()},
    })
    return EffectivenessArtifacts(out_dir, manifest_path, report_path, paths)


def _cell(value: Any) -> str:
    return str(value if value is not None else "unknown").replace("|", "\\|").replace("\n", " ")


def _refs(store: EvidenceStore, refs) -> str:
    parts = []
    for ref in refs:
        if hasattr(ref, "line_number"):
            ref = asdict(ref)
        line = ref.get("line_number")
        field = ref.get("field_path") or "payload"
        parts.append(f"[line {line}]({store.path.resolve()}:{line}) `{field}`")
    return "; ".join(parts) or "no evidence supplied"


def render_effectiveness_report(store: EvidenceStore, facts: FactAnalysis, semantic: SemanticAnalysis) -> str:
    lines = ["# Trace Effectiveness Analysis", "", f"Source: `{store.path}`", f"SHA-256: `{store.source_sha256}`", "",
             "## Task and requirements", ""]
    for task in facts.task_contracts:
        lines.extend([f"### {_cell(task.get('id'))}", "", _cell(task.get("objective", "unknown")), ""])
        for criterion in task.get("criteria", ()):
            lines.append(f"- `{_cell(criterion.get('id'))}`: {_cell(criterion.get('description'))}")
        lines.append("")
    if not facts.task_contracts:
        lines.extend(["Task requirements are unknown; task completion cannot be assessed.", ""])
    lines.extend(["## Evidence coverage", "", f"Semantic analysis: **{semantic.coverage.get('status', 'not_evaluated')}**.",
                  "Runtime pass, reported completion and verified task success are separate facts.", "",
                  f"Recorded events: {len(store.events)}; rounds: {len(facts.trajectory)}; tools: {len(facts.tool_uses)}.",
                  f"Missing producer hashes: {len(store.coverage['missing_hash_lines'])}; "
                  f"hash mismatches: {len(store.coverage['hash_mismatches'])}.", ""])
    for limitation in semantic.coverage.get("limitations", ()):
        lines.append(f"- {_cell(limitation)}")
    if "round_reviews" in semantic.coverage:
        reviews = semantic.coverage["round_reviews"]
        lines.extend(["", f"Round reviews: {reviews['reviewed']} / {reviews['total']}. Unknown judgments remain unresolved.", ""])
    lines.extend(["", "Factual coverage:", "", "```json", json.dumps(facts.coverage, ensure_ascii=False, sort_keys=True, indent=2), "```", "",
                  "## Round timeline", "", "| Round | Scope | Recorded status | Evidence |", "| --- | --- | --- | --- |"])
    for number, row in enumerate(facts.trajectory, 1):
        refs = row.get("evidence_refs", [])
        if not refs:
            refs = [r for k in ("request_ref", "response_ref") if (r := row.get(k))]
        lines.append(f"| {number} | `{_cell(row.get('id'))}` | {_cell(row.get('status', 'unknown'))} | {_refs(store, refs)} |")
    if semantic.round_analyses:
        lines.extend(["", "## Round state changes", ""])
        for row in semantic.round_analyses:
            lines.extend([f"### {_cell(row['round_id'])}", "",
                          f"Before: {_cell(row.get('pre_state'))}", "", f"Intent: {_cell(row.get('intent'))}", "",
                          f"Action: {_cell(row.get('action'))}", "", f"Observed change: {_cell(row.get('observed_change'))}", "",
                          f"After: {_cell(row.get('post_state'))}; progress: {_cell(row.get('progress_kind'))}", "",
                          f"Review coverage: {_cell(row.get('evidence_coverage'))}; {_cell(row.get('limitation', ''))}", "",
                          f"Request-visible evidence: {_refs(store, row.get('decision_time_refs', []))}", "",
                          f"Earlier recorded evidence: {_refs(store, row.get('prior_record_refs', []))}", "",
                          f"Later evidence: {_refs(store, row.get('hindsight_refs', []))}", ""])
            for dimension, assessment in row.get("dimensions", {}).items():
                lines.append(f"- {dimension}: **{_cell(assessment.get('status'))}** — {_cell(assessment.get('rationale'))}")
            lines.append("")
    lines.extend(["", "## Five-dimensional diagnoses", ""])
    for dimension in DIMENSIONS:
        lines.extend([f"### {dimension}", ""])
        diagnoses = [d for d in semantic.diagnoses if d.dimension == dimension]
        if not diagnoses:
            status = semantic.coverage.get("dimensions", {}).get(dimension, semantic.coverage.get("status", "not_evaluated"))
            lines.extend([f"Analysis status: {status}. No supported diagnosis recorded; this is not a passing score.", ""])
        for d in diagnoses:
            lines.extend([f"**{_cell(d.scope)} — {d.epistemic_status}**", "", d.observation, "", d.interpretation, "",
                          f"Evidence: {_refs(store, d.supporting_refs)}", "", f"Coverage: {d.evidence_coverage}", "",
                          f"Mechanism: {d.mechanism}", "", f"Consequence: {d.consequence}", ""])
            if d.counterevidence_refs:
                lines.extend([f"Counterevidence: {_refs(store, d.counterevidence_refs)}", ""])
            if d.improvement_hypothesis:
                lines.extend([f"Hypothesis and validation: {d.improvement_hypothesis}", ""])
            if d.preserve:
                lines.extend([f"Preserve: {d.preserve}", ""])
    lines.extend(["## Measured token ledger", "", "| Field | Recorded sum | Missing entries |", "| --- | ---: | ---: |"])
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [row.get(key) for row in facts.token_ledger]
        numbers = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
        lines.append(f"| {key} | {sum(numbers) if numbers else 'unknown'} | {len(values) - len(numbers)} |")
    lines.extend(["", "Sums cover recorded usage only. Missing usage is unknown; repeated input is not automatically wasted.",
                  f"Analyzer usage (separate from task): `{json.dumps(semantic.usage, ensure_ascii=False, sort_keys=True)}`", "",
                  "## Verification coverage", "", "| Criterion | Status | Rationale / limitation | Evidence |", "| --- | --- | --- | --- |"])
    for row in semantic.verification:
        lines.append(f"| {_cell(row['criterion_id'])} | {row['status']} | {_cell(row.get('rationale', ''))} "
                     f"{_cell(row.get('limitation', ''))} | {_refs(store, row.get('evidence_refs', []))} |")
    lines.extend(["", "## Preserved behaviors", ""])
    for row in semantic.preserved_behaviors:
        lines.extend([f"- {_cell(row['behavior'])} — {_refs(store, row['evidence_refs'])}", ""])
    lines.extend(["## Proposed verification framework", "", "These checks are recommendations and have not been executed by this analyzer.", ""])
    for row in semantic.verification_framework:
        lines.extend([f"- `{_cell(row.get('criterion_id'))}`: {_cell(row.get('check'))}; oracle: {_cell(row.get('oracle'))}; "
                      f"failure signal: {_cell(row.get('failure_signal'))}", ""])
    return "\n".join(lines).rstrip() + "\n"
