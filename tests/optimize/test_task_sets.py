from __future__ import annotations

import json
from pathlib import Path

from loom.campaigns.artifacts import ArtifactStore
from loom.optimize.contracts import TaskSetConfig
from loom.optimize.task_sets import (
    load_simple_tasks,
    prepare_explicit_task_sets,
    prepare_task_sets,
    publish_prepared_task_sets,
)


def _write_rows(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _project(root: Path, index: int) -> Path:
    path = root / f"project-{index}"
    path.mkdir()
    (path / "README.md").write_text(f"fixture project {index}\nunique-token-{index}\n", encoding="utf-8")
    return path


def _independent_rows(tmp_path: Path, count: int = 9) -> list[dict]:
    objectives = (
        "Map dependency cycles and explain package ownership boundaries.",
        "Verify Unicode parser behavior for malformed configuration documents.",
        "Measure transaction recovery after an interrupted SQLite write.",
        "Inspect command routing and identify ambiguous argument combinations.",
        "Exercise workspace path containment against symbolic link escapes.",
        "Evaluate token accounting across streamed completion events.",
        "Check rollback registration for a low risk declarative candidate.",
        "Analyze report redaction for credential shaped configuration values.",
        "Validate deterministic ordering of concurrent experiment results.",
    )
    rows = []
    for index in range(count):
        project = _project(tmp_path, index)
        rows.append(
            {
                "task_id": f"task-{index}",
                "objective": objectives[index],
                "workspace": project.name,
                "profile": "project_audit",
                "constraints": [f"Use evidence category unique-{index}."],
                "expected_outputs": [f"Write finding class unique-{index}."],
            }
        )
    return rows


def test_load_simple_tasks_rejects_unknown_fields(tmp_path: Path):
    _project(tmp_path, 0)
    source = _write_rows(
        tmp_path / "tasks.jsonl",
        [{"task_id": "a", "objective": "Audit", "workspace": "project-0", "objctive": "typo"}],
    )

    result = load_simple_tasks(source)

    assert result.error.code == "TASK_SET_INVALID"
    assert result.error.metadata["line"] == 1


def test_load_simple_tasks_resolves_workspace_and_verifier(tmp_path: Path):
    workspace = _project(tmp_path, 0)
    source = _write_rows(
        tmp_path / "tasks.jsonl",
        [
            {
                "task_id": "a",
                "objective": "Audit",
                "workspace": workspace.name,
                "verifier": {"argv": ["python", "smoke_test.py"], "timeout_ms": 5000, "expected_exit_code": 0},
            }
        ],
    )

    task = load_simple_tasks(source).unwrap()[0]

    assert task.workspace == workspace.resolve()
    assert task.verifier.argv == ("python", "smoke_test.py")
    assert task.verifier.timeout_ms == 5000


def test_prepare_task_sets_is_stable_and_keeps_snapshot_groups_together(tmp_path: Path):
    source = _write_rows(tmp_path / "tasks.jsonl", _independent_rows(tmp_path))
    config = TaskSetConfig(seed=42, repetitions=3, minimum_pairs=5)

    first = prepare_task_sets(source, config=config, output_dir=tmp_path / "one", owner="tester").unwrap()
    second = prepare_task_sets(source, config=config, output_dir=tmp_path / "two", owner="tester").unwrap()

    assert first.fingerprint_digests == second.fingerprint_digests
    assert first.role_task_ids == second.role_task_ids
    assert {len(first.discovery.tasks), len(first.validation.tasks), len(first.holdout.tasks)} == {2, 5}
    roles_by_snapshot = {}
    for prepared_set in (first.discovery, first.validation, first.holdout):
        for task in prepared_set.tasks:
            previous = roles_by_snapshot.setdefault(task.snapshot.digest, prepared_set.role)
            assert previous is prepared_set.role


def test_prepare_task_sets_rejects_one_project(tmp_path: Path):
    workspace = _project(tmp_path, 0)
    rows = [
        {
            "task_id": f"task-{index}",
            "objective": f"Inspect unique subsystem {index}.",
            "workspace": workspace.name,
            "constraints": [f"condition {index}"],
        }
        for index in range(6)
    ]
    source = _write_rows(tmp_path / "tasks.jsonl", rows)

    result = prepare_task_sets(source, config=TaskSetConfig(), output_dir=tmp_path / "out", owner="tester")

    assert result.error.code == "TASK_SPLIT_INSUFFICIENT"


def test_prepare_task_sets_rejects_common_template_lineage(tmp_path: Path):
    rows = []
    for index in range(6):
        project = _project(tmp_path, index)
        rows.append(
            {
                "task_id": f"task-{index}",
                "objective": f"Audit {project.name}.",
                "workspace": project.name,
                "profile": "project_audit",
            }
        )
    source = _write_rows(tmp_path / "tasks.jsonl", rows)

    result = prepare_task_sets(source, config=TaskSetConfig(), output_dir=tmp_path / "out", owner="tester")

    assert result.error.code == "TASK_SPLIT_INSUFFICIENT"


def test_prepare_explicit_task_sets_still_checks_contamination(tmp_path: Path):
    objectives = (
        "Trace cache invalidation through layered adapters.",
        "Inspect exception normalization for provider failures.",
        "Measure command latency under bounded retry pressure.",
        "Verify immutable artifact checksums after export.",
        "Audit role separation in approval transactions.",
        "Validate holdout sealing after finalist selection.",
    )
    paths = []
    for role_index, role in enumerate(("discovery", "validation", "holdout")):
        rows = []
        for offset in range(2):
            index = role_index * 2 + offset
            project = _project(tmp_path, index)
            rows.append(
                {
                    "task_id": f"{role}-{offset}",
                    "objective": objectives[index],
                    "workspace": project.name,
                    "constraints": [f"unique constraint {index}"],
                }
            )
        paths.append(_write_rows(tmp_path / f"{role}.jsonl", rows))

    prepared = prepare_explicit_task_sets(
        paths[0],
        paths[1],
        paths[2],
        config=TaskSetConfig(),
        output_dir=tmp_path / "out",
        owner="tester",
    ).unwrap()

    assert len(prepared.discovery.tasks) == 2
    assert len(prepared.validation.tasks) == 2
    assert len(prepared.holdout.tasks) == 2


def test_publish_prepared_task_sets_binds_manifest_fingerprints(tmp_path: Path):
    source = _write_rows(tmp_path / "tasks.jsonl", _independent_rows(tmp_path))
    prepared = prepare_task_sets(source, config=TaskSetConfig(), output_dir=tmp_path / "out", owner="tester").unwrap()

    published = publish_prepared_task_sets(prepared, ArtifactStore(tmp_path / "artifacts")).unwrap()

    assert published.discovery.fingerprint_digest == prepared.discovery.fingerprint.fingerprint_digest
    assert published.validation.fingerprint_digest == prepared.validation.fingerprint.fingerprint_digest
    assert published.holdout.fingerprint_digest == prepared.holdout.fingerprint.fingerprint_digest
    assert published.holdout.manifest_ref.sha256


def test_snapshot_rejects_symlinks(tmp_path: Path):
    project = _project(tmp_path, 0)
    (project / "escape").symlink_to(tmp_path / "outside")
    source = _write_rows(
        tmp_path / "tasks.jsonl",
        [{"task_id": "a", "objective": "Audit", "workspace": project.name}],
    )

    result = prepare_task_sets(source, config=TaskSetConfig(), output_dir=tmp_path / "out", owner="tester")

    assert result.error.code == "TASK_SNAPSHOT_INVALID"
