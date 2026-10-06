"""Evidence-preserving v2 improvement hypotheses; no scores or mutations invented."""

import hashlib
from collections import defaultdict

SURFACES = {"context_effectiveness": "context_policy", "tool_effectiveness": "tool_contract",
            "loop_progress": "loop_control", "token_efficiency": "context_policy", "verify_gate": "verification"}


def proposals_from_diagnoses(diagnoses, *, source_sha256, evaluator):
    groups = defaultdict(list)
    for diagnosis in diagnoses:
        if diagnosis.get("epistemic_status") == "unknown" or not diagnosis.get("supporting_refs"):
            continue
        hypothesis = diagnosis.get("improvement_hypothesis", "").strip()
        if not hypothesis:
            continue
        key = (diagnosis["dimension"], diagnosis.get("mechanism", "unknown"), hypothesis)
        groups[key].append(diagnosis)
    proposals = []
    for (dimension, mechanism, hypothesis), rows in groups.items():
        identity = hashlib.sha256(repr((source_sha256, dimension, mechanism, hypothesis)).encode()).hexdigest()[:24]
        proposals.append({"id": f"hypothesis_{identity}", "state": "proposed", "dimension": dimension,
                          "surface": SURFACES[dimension], "mechanism": mechanism, "hypothesis": hypothesis,
                          "affected_scopes": sorted({row["scope"] for row in rows}),
                          "supporting_refs": [ref for row in rows for ref in row["supporting_refs"]],
                          "counterevidence_refs": [ref for row in rows for ref in row.get("counterevidence_refs", [])],
                          "preserve": list(dict.fromkeys(row["preserve"] for row in rows if row.get("preserve"))),
                          "source_sha256": source_sha256, "evaluator": evaluator,
                          "validation_status": "not_run", "risk": "unassessed", "expected_savings": None,
                          "validation": "Compare baseline and candidate on the same task, environment and evaluator; check preserved behaviors."})
    return proposals
