"""Read-only memory views, including explicit unknowns and partial coverage."""

from copy import deepcopy

from loom.evaluation.memory import VERSION


def project_memory(cp):
    memory = cp.get("memory")
    requested = cp.get("memory_requested", False)
    all_records = list((memory or {}).get("records", {}).values())
    records = [r for r in all_records if r.get('publication_state', 'published') == 'published']
    units = list((memory or {}).get("units", {}).values())
    ratings = []
    for stage in cp.get("stages", {}).values():
        slices = [r for r in records if r["kind"] == "contributions" and r["stage_id"] == stage["id"]]
        values = {r["rating"] for r in slices} - {"unknown"}
        rating = (
            "mixed"
            if "hindering" in values and values - {"hindering", "none"}
            else next((v for v in ("key", "supporting", "hindering", "none") if v in values), "unknown")
        )
        ratings.append(
            dict(
                stage_id=stage["id"],
                goal_id=stage["goal_id"],
                title=stage["title"],
                rating=rating,
                partial=not slices or not (memory or {}).get("complete", False) or any(r["rating"] == "unknown" for r in slices),
                slices=deepcopy(slices),
                key_action_ids=list(dict.fromkeys(cid for r in slices for cid in r["key_action_ids"])),
            )
        )
    return dict(
        version=VERSION,
        status="complete" if memory and memory["complete"] else "partial" if memory else "pending" if requested else "not_requested",
        graph_revision=cp.get("graph_revision", 0),
        contributions=ratings,
        **{kind: deepcopy([r for r in records if r["kind"] == kind]) for kind in ("facts", "lessons", "workflows")},
        coverage=dict(
            reviewed_actions=sum(len(u["scope"]["actions"]) for u in units if u["mode"] == "extract" and u.get("complete")),
            completed_windows=sum(u.get("complete", False) for u in units),
            total_windows=len(units),
            settled_windows=sum(u.get('work_state') == 'settled' or u.get('complete', False) for u in units),
            source_actions=len(cp.get('extraction', {}).get('cards', [])),
            processed_actions=sum(len(u['scope']['actions']) for u in units if u['mode'] == 'extract'
                                  and (u.get('work_state') == 'settled' or u.get('complete'))),
            published_records=len(records),
            quarantined_records=len((memory or {}).get('quarantine', {})),
            stale_records=sum(r.get('publication_state') == 'stale' for r in all_records),
            unknown_contributions=sum(r.get('rating') == 'unknown' for r in records),
            evidence_chars=(memory or {}).get("read_chars", 0),
            truncated_slices=sum(e["truncated"] for u in units if u["mode"] == "extract" for e in u["evidence"]),
            errors=[error for u in units for error in u.get("errors", [])],
            issues=[deepcopy(error) for u in units for error in u.get("issues", [])],
        ),
        execution_complete=(memory or {}).get('execution_complete', (memory or {}).get('complete', False)),
        quarantine=[{k: deepcopy(v) for k, v in row.items() if k != 'record'} for row in (memory or {}).get('quarantine', {}).values()],
        analysis_epoch=(memory or {}).get('epoch', 1),
        previous_epochs=[{k: deepcopy(v) for k, v in row.items() if k != 'snapshot'}
                         for row in cp.get('analysis_epochs', []) if row.get('kind') == 'memory'],
        limitations=[
            "Scoped to the recorded run; revalidate environment, configuration and capabilities before reuse.",
            "Contribution is a qualitative assessment of supplied evidence, not an additive percentage or a causal measurement.",
            "Bounded evidence excerpts may omit relevant output. Complete means scheduled memory windows were reviewed.",
            "Workflow improvements marked proposed are untested. Relevant accepted memories may be retrieved by tasks "
            "in the same workspace; revalidate before use.",
        ],
    )


def memory_report(memory):
    lines = ["", "## Memory", "", f"Status: {memory['status']}", ""]
    for row in memory["contributions"]:
        lines.append(f"- {row['title']}: **{row['rating']}**" + (" (partial evidence)" if row["partial"] else ""))
        for part in row["slices"]:
            lines.append(f"  - {part['rationale']}")
            lines.extend(f"    - L{r['line_number']} · {r.get('field_path', '')}" for r in part["evidence_refs"])
    for key, label in (("facts", "Key facts"), ("lessons", "Failure lessons"), ("workflows", "Efficient workflows")):
        lines.extend(["", f"### {label}", ""])
        for row in memory[key]:
            lines.extend([f"- **{row['title']}**: {row['summary']}", f"  - Scope: {row['scope']}"])
            for field in (
                "basis",
                "validation",
                "revalidate_when",
                "trigger",
                "failed_approach",
                "failure_reason",
                "better_approach",
                "preconditions",
                "avoid",
                "expected_benefit",
                "preserve",
            ):
                if field in row:
                    lines.append(f"  - {field.replace('_', ' ')}: {row[field]}")
            for i, step in enumerate(row.get("steps", []), 1):
                lines.append(f"  - {i}. {step}")
            lines.extend(f"  - Evidence: L{r['line_number']} · {r.get('field_path', '')}" for r in row["evidence_refs"])
    lines.extend(["", *memory["limitations"]])
    return "\n".join(lines)
