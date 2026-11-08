"""Offline evaluation of human-labelled proposals; no model calls or self-grading."""

import math

from loom.memory.repository import Repository, digest, now
from loom.memory.store import serialized


def evaluate(rows):
    ids = set()
    roots = {}
    true_positive = false_positive = conflicts = detected = critical_errors = 0
    for row in rows:
        if row["id"] in ids or row.get("label_origin") != "human":
            raise ValueError("Calibration needs unique, human-labelled cases")
        ids.add(row["id"])
        for root in row["source_roots"]:
            if root in roots and roots[root] != row["split"]:
                raise ValueError("Evidence root leaks between calibration and test partitions")
            roots[root] = row["split"]
        if row["split"] != "test":
            continue
        if row["predicted"] == "equivalent":
            true_positive += row["label"] == "equivalent"
            false_positive += row["label"] != "equivalent"
        if row["label"] == "contradicts":
            conflicts += 1
            detected += row["predicted"] == "contradicts"
        if row.get("critical") and row["predicted"] != row["label"]:
            critical_errors += 1
    n = true_positive + false_positive
    p = true_positive / n if n else 0
    z = 1.96
    lower = (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n) if n else 0
    recall = detected / conflicts if conflicts else 0
    passed = len(rows) >= 1200 and n >= 600 and conflicts >= 200 and p >= 0.995 and lower >= 0.99 and recall >= 0.95 and critical_errors == 0
    return dict(
        corpus_digest=digest(rows),
        cases=len(rows),
        predicted_merges=n,
        precision=p,
        precision_lower_95=lower,
        conflict_cases=conflicts,
        conflict_recall=recall,
        critical_errors=critical_errors,
        passed=passed,
    )


def activate(database, namespace, rows, *, model_fingerprint):
    if not isinstance(model_fingerprint, str) or not model_fingerprint:
        raise ValueError('Pin the calibrated model and protocol fingerprint')
    report = {**evaluate(rows), 'model_fingerprint': model_fingerprint}
    if not report["passed"]:
        raise ValueError("Semantic calibration gate failed: " + serialized(report))
    repo = Repository(database)
    with repo.connect(write=True) as db:
        if not db.execute("SELECT 1 FROM memory_namespaces WHERE id=?", (namespace,)).fetchone():
            raise ValueError("Unknown namespace")
        db.execute("INSERT OR REPLACE INTO memory_policies VALUES(?,?,?,?)", (namespace, 1, serialized(report), now()))
        repo.event(db, namespace, "CALIBRATION", report)
    return report
