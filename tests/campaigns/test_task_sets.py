from __future__ import annotations

import json

from loom.campaigns.contracts import ExperimentPhase
from loom.campaigns.task_set_cli import main as task_set_main
from loom.campaigns.task_sets import TaskManifestRow, fingerprint_task_rows, validate_task_set_isolation


def _row(task_id: str, content: str, *, snapshot: str = "a" * 64, lineage: str = "template-a") -> TaskManifestRow:
    return TaskManifestRow(
        task_id=task_id,
        owner="loom",
        source_evidence_ref=f"evidence:{task_id}",
        use_basis="internal-test",
        project_snapshot_digest=snapshot,
        template_lineage=lineage,
        sanitizer_version="sanitizer.v1",
        content=content,
    )


def test_task_fingerprints_are_stable_and_include_provenance():
    rows = (_row("a", "Audit the Python project and run tests."),)

    first = fingerprint_task_rows(rows, ExperimentPhase.DISCOVERY).unwrap()
    second = fingerprint_task_rows(rows, ExperimentPhase.DISCOVERY).unwrap()

    assert first.fingerprint_digest == second.fingerprint_digest
    assert first.entries[0].normalized_sha256
    assert first.entries[0].project_snapshot_digest == "a" * 64
    assert first.entries[0].template_lineage == "template-a"
    assert first.entries[0].minhash


def test_task_fingerprint_rejects_missing_required_provenance():
    invalid = _row("a", "content")
    invalid = TaskManifestRow(
        invalid.task_id,
        "",
        invalid.source_evidence_ref,
        invalid.use_basis,
        invalid.project_snapshot_digest,
        invalid.template_lineage,
        "",
        invalid.content,
    )

    result = fingerprint_task_rows((invalid,), ExperimentPhase.DISCOVERY)

    assert not result.ok
    assert result.error.code == "TASK_SET_INVALID"


def test_task_set_isolation_rejects_exact_snapshot_lineage_and_near_duplicate_content():
    discovery = fingerprint_task_rows((_row("a", "audit project read files run tests summarize findings"),), ExperimentPhase.DISCOVERY).unwrap()
    exact = fingerprint_task_rows(
        (_row("b", "audit project read files run tests summarize findings", snapshot="b" * 64, lineage="other"),),
        ExperimentPhase.VALIDATION,
    ).unwrap()
    same_snapshot = fingerprint_task_rows((_row("c", "completely different words here", snapshot="a" * 64, lineage="other"),), ExperimentPhase.HOLDOUT).unwrap()
    near = fingerprint_task_rows(
        (_row("d", "audit project read files run tests summarize findings carefully", snapshot="d" * 64, lineage="different"),),
        ExperimentPhase.HOLDOUT,
    ).unwrap()

    for other in (exact, same_snapshot, near):
        result = validate_task_set_isolation((discovery, other), threshold=0.80)
        assert not result.ok
        assert result.error.code == "TASK_SET_CONTAMINATION"
        assert result.error.metadata["matches"]


def test_task_set_isolation_accepts_distinct_sets_and_rejects_weaker_threshold():
    discovery = fingerprint_task_rows((_row("a", "audit python package"),), ExperimentPhase.DISCOVERY).unwrap()
    validation = fingerprint_task_rows((_row("b", "plan a travel itinerary", snapshot="b" * 64, lineage="b"),), ExperimentPhase.VALIDATION).unwrap()

    assert validate_task_set_isolation((discovery, validation), threshold=0.80).ok
    assert not validate_task_set_isolation((discovery, validation), threshold=0.81).ok


def test_task_set_cli_fingerprints_and_rejects_contamination(tmp_path, capsys):
    def write(name, task_id, content):
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "owner": "loom",
                    "source_evidence_ref": "evidence:test",
                    "use_basis": "test",
                    "project_snapshot_digest": task_id[0] * 64,
                    "template_lineage": "template-" + task_id,
                    "sanitizer_version": "v1",
                    "content": content,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    discovery = write("discovery.jsonl", "discovery", "same task content")
    duplicate = write("duplicate.jsonl", "duplicate", "same task content")

    assert task_set_main(["fingerprint", str(discovery), "--json"]) == 0
    assert "fingerprint_digest" in capsys.readouterr().out
    assert task_set_main(["validate", str(discovery), "--against", str(duplicate), "--json"]) == 1
    assert "TASK_SET_CONTAMINATION" in capsys.readouterr().out
