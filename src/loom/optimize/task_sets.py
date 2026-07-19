"""Simple optimization tasks, immutable snapshots, and isolated task sets."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from loom.campaigns.contracts import ExperimentPhase, TaskSetRef
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes
from loom.campaigns.task_sets import FingerprintedTaskSet, TaskManifestRow, fingerprint_task_rows, validate_task_set_isolation
from loom.core import FrozenDict, Result, err, freeze_json, make_loom_error, ok
from loom.optimize.contracts import TaskSetConfig

_TASK_FIELDS = frozenset({"task_id", "objective", "workspace", "profile", "constraints", "expected_outputs", "risk_level", "verifier", "metadata"})
_VERIFIER_FIELDS = frozenset({"argv", "timeout_ms", "expected_exit_code"})
_SKIPPED_DIRECTORIES = frozenset({".git", ".loom"})
_PROJECT_TOKEN = re.compile(r"(?i)\b(?:project|repo|workspace)[-_ ]?\d+\b")


@dataclass(frozen=True, slots=True)
class VerifierSpec:
    argv: tuple[str, ...]
    timeout_ms: int = 120_000
    expected_exit_code: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        if not self.argv or not all(isinstance(item, str) and item for item in self.argv):
            raise ValueError("verifier argv must contain non-empty strings")
        if isinstance(self.timeout_ms, bool) or not isinstance(self.timeout_ms, int) or self.timeout_ms < 1:
            raise ValueError("verifier timeout_ms must be positive")
        if isinstance(self.expected_exit_code, bool) or not isinstance(self.expected_exit_code, int):
            raise ValueError("verifier expected_exit_code must be an integer")


@dataclass(frozen=True, slots=True)
class OptimizeTask:
    task_id: str
    objective: str
    workspace: Path
    profile: str = "auto"
    constraints: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    risk_level: str = "auto"
    verifier: VerifierSpec | None = None
    metadata: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).resolve())
        for name in ("constraints", "expected_outputs"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        metadata = freeze_json(self.metadata)
        if not isinstance(metadata, FrozenDict):
            raise TypeError("task metadata must be a mapping")
        object.__setattr__(self, "metadata", metadata)


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    digest: str
    path: Path
    file_count: int
    total_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True, slots=True)
class PreparedTask:
    task: OptimizeTask
    snapshot: WorkspaceSnapshot
    manifest_row: TaskManifestRow
    fingerprint_key: str


@dataclass(frozen=True, slots=True)
class PreparedTaskSet:
    role: ExperimentPhase
    tasks: tuple[PreparedTask, ...]
    manifest_path: Path
    fingerprint: FingerprintedTaskSet

    def __post_init__(self) -> None:
        object.__setattr__(self, "tasks", tuple(self.tasks))
        object.__setattr__(self, "manifest_path", Path(self.manifest_path))


@dataclass(frozen=True, slots=True)
class PreparedTaskSets:
    discovery: PreparedTaskSet
    validation: PreparedTaskSet
    holdout: PreparedTaskSet
    task_by_fingerprint: Mapping[str, PreparedTask]

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_by_fingerprint", MappingProxyType(dict(self.task_by_fingerprint)))

    @property
    def fingerprint_digests(self) -> Mapping[str, str]:
        return {
            "discovery": self.discovery.fingerprint.fingerprint_digest,
            "validation": self.validation.fingerprint.fingerprint_digest,
            "holdout": self.holdout.fingerprint.fingerprint_digest,
        }

    @property
    def role_task_ids(self) -> Mapping[str, tuple[str, ...]]:
        return {
            "discovery": tuple(item.task.task_id for item in self.discovery.tasks),
            "validation": tuple(item.task.task_id for item in self.validation.tasks),
            "holdout": tuple(item.task.task_id for item in self.holdout.tasks),
        }


@dataclass(frozen=True, slots=True)
class PublishedTaskSets:
    discovery: TaskSetRef
    validation: TaskSetRef
    holdout: TaskSetRef


def load_simple_tasks(path: str | Path) -> Result:
    source = Path(path)
    try:
        tasks: list[OptimizeTask] = []
        normalized_ids: set[str] = set()
        for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise _TaskInputError(line_number, "task row must be an object")
            unknown = sorted(set(value) - _TASK_FIELDS)
            if unknown:
                raise _TaskInputError(line_number, f"unknown task field: {unknown[0]}")
            task_id = _required_text(value.get("task_id"), line_number, "task_id")
            normalized_id = unicodedata.normalize("NFC", task_id)
            if normalized_id in normalized_ids:
                raise _TaskInputError(line_number, "task_id is duplicated after Unicode normalization")
            normalized_ids.add(normalized_id)
            objective = _required_text(value.get("objective"), line_number, "objective")
            workspace_value = _required_text(value.get("workspace"), line_number, "workspace")
            workspace = Path(workspace_value)
            if not workspace.is_absolute():
                workspace = source.parent / workspace
            workspace = workspace.resolve()
            if not workspace.is_dir():
                raise _TaskInputError(line_number, "workspace must be an existing directory")
            verifier = _parse_verifier(value.get("verifier"), line_number)
            metadata = value.get("metadata", {})
            if not isinstance(metadata, Mapping):
                raise _TaskInputError(line_number, "metadata must be an object")
            tasks.append(
                OptimizeTask(
                    normalized_id,
                    objective,
                    workspace,
                    _optional_text(value.get("profile"), "auto", line_number, "profile"),
                    _text_sequence(value.get("constraints", ()), line_number, "constraints"),
                    _text_sequence(value.get("expected_outputs", ()), line_number, "expected_outputs"),
                    _optional_text(value.get("risk_level"), "auto", line_number, "risk_level"),
                    verifier,
                    metadata,
                )
            )
        if not tasks:
            raise _TaskInputError(0, "task file must contain at least one row")
        return ok(tuple(tasks))
    except _TaskInputError as exc:
        return _task_error("TASK_SET_INVALID", str(exc), path=source, line=exc.line)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        line = getattr(exc, "lineno", 0)
        return _task_error("TASK_SET_INVALID", "task manifest is invalid", path=source, line=line, cause=exc)


def prepare_task_sets(
    path: str | Path,
    *,
    config: TaskSetConfig,
    output_dir: str | Path,
    owner: str,
    visible_snapshot_digests: tuple[str, ...] = (),
) -> Result:
    loaded = load_simple_tasks(path)
    if not loaded.ok:
        return loaded
    prepared = _prepare_tasks(loaded.value, Path(output_dir), owner)
    if not prepared.ok:
        return prepared
    groups = _connected_groups(prepared.value)
    assignment = _assign_groups(groups, config, frozenset(visible_snapshot_digests))
    if not assignment.ok:
        return assignment
    return _build_sets(assignment.value, Path(output_dir), config)


def prepare_explicit_task_sets(
    discovery_path: str | Path,
    validation_path: str | Path,
    holdout_path: str | Path,
    *,
    config: TaskSetConfig,
    output_dir: str | Path,
    owner: str,
) -> Result:
    roles_and_paths = (
        (ExperimentPhase.DISCOVERY, discovery_path),
        (ExperimentPhase.VALIDATION, validation_path),
        (ExperimentPhase.HOLDOUT, holdout_path),
    )
    assigned: dict[ExperimentPhase, tuple[PreparedTask, ...]] = {}
    destination = Path(output_dir)
    all_ids: set[str] = set()
    for role, path in roles_and_paths:
        loaded = load_simple_tasks(path)
        if not loaded.ok:
            return loaded
        duplicate = all_ids.intersection(task.task_id for task in loaded.value)
        if duplicate:
            return _task_error("TASK_SET_INVALID", "task IDs must be unique across explicit sets", task_ids=sorted(duplicate))
        all_ids.update(task.task_id for task in loaded.value)
        prepared = _prepare_tasks(loaded.value, destination, owner)
        if not prepared.ok:
            return prepared
        assigned[role] = prepared.value
    return _build_sets(assigned, destination, config)


def publish_prepared_task_sets(prepared: PreparedTaskSets, artifact_store) -> Result:
    refs = []
    for value in (prepared.discovery, prepared.validation, prepared.holdout):
        try:
            content = value.manifest_path.read_bytes()
        except OSError as exc:
            return _task_error("TASK_SET_INVALID", "prepared task manifest is unreadable", path=value.manifest_path, cause=exc)
        published = artifact_store.publish_bytes(content, kind="task_set", schema_version="loom.task-set.manifest.v1", suffix=".jsonl")
        if not published.ok:
            return published
        refs.append(
            TaskSetRef(
                f"tasks-{value.role.value}-{value.fingerprint.fingerprint_digest[:12]}",
                value.role,
                published.value,
                value.fingerprint.fingerprint_digest,
            )
        )
    return ok(PublishedTaskSets(*refs))


def _prepare_tasks(tasks: tuple[OptimizeTask, ...], output_dir: Path, owner: str) -> Result:
    if not owner.strip():
        return _task_error("TASK_SET_INVALID", "task owner is required")
    snapshot_root = output_dir / "inputs" / "workspaces"
    prepared: list[PreparedTask] = []
    for task in tasks:
        snapshot = _snapshot_workspace(task.workspace, snapshot_root)
        if not snapshot.ok:
            return snapshot
        lineage = _template_lineage(task)
        content_parts = [task.objective, *task.constraints, *task.expected_outputs]
        if task.verifier is not None:
            content_parts.extend(task.verifier.argv)
        content = "\n".join(content_parts)
        row = TaskManifestRow(
            task.task_id,
            owner,
            f"task:{canonical_digest({'task_id': task.task_id, 'snapshot': snapshot.value.digest, 'content': content})}",
            "optimize",
            snapshot.value.digest,
            lineage,
            "loom.optimize-task-sanitizer.v1",
            content,
        )
        prepared.append(PreparedTask(task, snapshot.value, row, ""))
    return ok(tuple(prepared))


def _snapshot_workspace(source: Path, root: Path) -> Result:
    try:
        entries: list[tuple[str, Path, int, bytes]] = []
        total_bytes = 0
        for current_root, directory_names, file_names in os.walk(source, topdown=True, followlinks=False):
            current = Path(current_root)
            directory_names[:] = sorted(name for name in directory_names if name not in _SKIPPED_DIRECTORIES)
            for directory_name in directory_names:
                if (current / directory_name).is_symlink():
                    raise ValueError(f"workspace contains symlink: {(current / directory_name).relative_to(source)}")
            for file_name in sorted(file_names):
                path = current / file_name
                relative = path.relative_to(source).as_posix()
                if path.is_symlink():
                    raise ValueError(f"workspace contains symlink: {relative}")
                mode = path.stat().st_mode
                if not stat.S_ISREG(mode):
                    raise ValueError(f"workspace contains special file: {relative}")
                content = path.read_bytes()
                total_bytes += len(content)
                entries.append((relative, path, stat.S_IMODE(mode), content))
        digest = hashlib.sha256()
        for relative, _path, mode, content in entries:
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(str(mode).encode())
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
        snapshot_digest = digest.hexdigest()
        root.mkdir(parents=True, exist_ok=True)
        destination = root / snapshot_digest
        if not destination.exists():
            temporary = Path(tempfile.mkdtemp(prefix=f".{snapshot_digest}.", dir=root))
            try:
                for relative, _path, mode, content in entries:
                    target = temporary / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    target.chmod(mode)
                try:
                    os.replace(temporary, destination)
                except OSError:
                    if not destination.exists():
                        raise
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return ok(WorkspaceSnapshot(snapshot_digest, destination, len(entries), total_bytes))
    except (OSError, ValueError) as exc:
        return _task_error("TASK_SNAPSHOT_INVALID", "workspace snapshot is invalid", workspace=source, cause=exc)


def _template_lineage(task: OptimizeTask) -> str:
    objective = unicodedata.normalize("NFKC", task.objective).casefold()
    for marker in (str(task.workspace), task.workspace.name):
        if marker:
            objective = objective.replace(marker.casefold(), "<project>")
    objective = _PROJECT_TOKEN.sub("<project>", objective)
    return canonical_digest(
        {
            "detector": "loom.optimize-template-lineage.v1",
            "objective": " ".join(objective.split()),
            "profile": task.profile,
            "constraints": tuple(" ".join(value.casefold().split()) for value in task.constraints),
            "expected_outputs": tuple(" ".join(value.casefold().split()) for value in task.expected_outputs),
            "verifier": None if task.verifier is None else {"argv": task.verifier.argv, "expected_exit_code": task.verifier.expected_exit_code},
        }
    )


def _connected_groups(tasks: tuple[PreparedTask, ...]) -> tuple[tuple[PreparedTask, ...], ...]:
    parent = list(range(len(tasks)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(tasks)):
        for right in range(left + 1, len(tasks)):
            if (
                tasks[left].snapshot.digest == tasks[right].snapshot.digest
                or tasks[left].manifest_row.template_lineage == tasks[right].manifest_row.template_lineage
            ):
                union(left, right)
    groups: dict[int, list[PreparedTask]] = {}
    for index, task in enumerate(tasks):
        groups.setdefault(find(index), []).append(task)
    return tuple(tuple(sorted(group, key=lambda item: item.task.task_id)) for _, group in sorted(groups.items()))


def _assign_groups(
    groups: tuple[tuple[PreparedTask, ...], ...],
    config: TaskSetConfig,
    visible_snapshot_digests: frozenset[str],
) -> Result:
    total = sum(len(group) for group in groups)
    minimum_tasks = math.ceil(config.minimum_pairs / config.repetitions)
    if len(groups) < 3 or total < minimum_tasks * 3:
        return _task_error("TASK_SPLIT_INSUFFICIENT", "task corpus lacks three independent groups", groups=len(groups), tasks=total)
    validation_target = max(minimum_tasks, round(total * float(config.split.validation)))
    holdout_target = max(minimum_tasks, round(total * float(config.split.holdout)))
    discovery_target = total - validation_target - holdout_target
    if discovery_target < minimum_tasks:
        return _task_error("TASK_SPLIT_INSUFFICIENT", "task split cannot satisfy minimum pairs", tasks=total)
    targets = {
        ExperimentPhase.DISCOVERY: discovery_target,
        ExperimentPhase.VALIDATION: validation_target,
        ExperimentPhase.HOLDOUT: holdout_target,
    }
    assigned: dict[ExperimentPhase, list[PreparedTask]] = {role: [] for role in targets}

    def group_order(group: tuple[PreparedTask, ...]) -> str:
        group_digest = canonical_digest(tuple(item.task.task_id for item in group))
        return hashlib.sha256(f"{config.seed}:{group_digest}".encode()).hexdigest()

    ordered = sorted(groups, key=group_order)
    for group in ordered:
        visible = any(item.snapshot.digest in visible_snapshot_digests for item in group)
        allowed = (ExperimentPhase.DISCOVERY,) if visible else tuple(targets)
        role = max(allowed, key=lambda item: (targets[item] - len(assigned[item]), -tuple(targets).index(item)))
        assigned[role].extend(group)
    if any(len(assigned[role]) < minimum_tasks for role in targets):
        return _task_error(
            "TASK_SPLIT_INSUFFICIENT",
            "grouped split cannot satisfy minimum paired evidence",
            counts={role.value: len(values) for role, values in assigned.items()},
            minimum_tasks=minimum_tasks,
        )
    return ok({role: tuple(sorted(values, key=lambda item: item.task.task_id)) for role, values in assigned.items()})


def _build_sets(assigned: Mapping[ExperimentPhase, tuple[PreparedTask, ...]], output_dir: Path, config: TaskSetConfig) -> Result:
    minimum_tasks = math.ceil(config.minimum_pairs / config.repetitions)
    values: list[PreparedTaskSet] = []
    fingerprints = []
    lookup: dict[str, PreparedTask] = {}
    manifest_root = output_dir / "inputs" / "task-sets"
    try:
        manifest_root.mkdir(parents=True, exist_ok=True)
        for role in (ExperimentPhase.DISCOVERY, ExperimentPhase.VALIDATION, ExperimentPhase.HOLDOUT):
            tasks = tuple(assigned.get(role, ()))
            if len(tasks) < minimum_tasks:
                return _task_error("TASK_SPLIT_INSUFFICIENT", "task set cannot satisfy minimum pairs", role=role.value, count=len(tasks))
            rows = tuple(
                TaskManifestRow(
                    item.manifest_row.task_id,
                    item.manifest_row.owner,
                    item.manifest_row.source_evidence_ref,
                    role.value,
                    item.manifest_row.project_snapshot_digest,
                    item.manifest_row.template_lineage,
                    item.manifest_row.sanitizer_version,
                    item.manifest_row.content,
                )
                for item in tasks
            )
            fingerprint = fingerprint_task_rows(rows, role)
            if not fingerprint.ok:
                return fingerprint
            path = manifest_root / f"{role.value}.jsonl"
            path.write_bytes(b"\n".join(canonical_json_bytes(asdict(row)) for row in rows) + b"\n")
            fingerprint_by_id = {entry.task_id: entry for entry in fingerprint.value.entries}
            prepared_with_keys = []
            for item in tasks:
                key = canonical_digest(fingerprint_by_id[item.task.task_id])
                updated = PreparedTask(item.task, item.snapshot, rows[len(prepared_with_keys)], key)
                prepared_with_keys.append(updated)
                lookup[key] = updated
            prepared_set = PreparedTaskSet(role, tuple(prepared_with_keys), path, fingerprint.value)
            values.append(prepared_set)
            fingerprints.append(fingerprint.value)
    except OSError as exc:
        return _task_error("TASK_SET_INVALID", "could not write prepared task manifests", path=manifest_root, cause=exc)
    isolated = validate_task_set_isolation(tuple(fingerprints), threshold=config.contamination_threshold)
    if not isolated.ok:
        return isolated
    return ok(PreparedTaskSets(values[0], values[1], values[2], lookup))


def _parse_verifier(value: Any, line: int) -> VerifierSpec | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _TaskInputError(line, "verifier must be an object")
    unknown = sorted(set(value) - _VERIFIER_FIELDS)
    if unknown:
        raise _TaskInputError(line, f"unknown verifier field: {unknown[0]}")
    argv = value.get("argv")
    if not isinstance(argv, list):
        raise _TaskInputError(line, "verifier argv must be an array")
    try:
        return VerifierSpec(tuple(argv), value.get("timeout_ms", 120_000), value.get("expected_exit_code", 0))
    except (TypeError, ValueError) as exc:
        raise _TaskInputError(line, str(exc)) from exc


def _required_text(value: Any, line: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _TaskInputError(line, f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, default: str, line: int, field: str) -> str:
    return default if value is None else _required_text(value, line, field)


def _text_sequence(value: Any, line: int, field: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) and item.strip() for item in value):
        raise _TaskInputError(line, f"{field} must contain non-empty strings")
    return tuple(item.strip() for item in value)


class _TaskInputError(ValueError):
    def __init__(self, line: int, message: str):
        super().__init__(message)
        self.line = line


def _task_error(code: str, message: str, *, cause: BaseException | None = None, **metadata: Any) -> Result:
    normalized = {key: str(value) if isinstance(value, Path) else value for key, value in metadata.items()}
    return err(
        make_loom_error(
            code,
            message,
            retryable=False,
            cause=None if cause is None else {"name": type(cause).__name__, "message": str(cause)},
            metadata=normalized,
        )
    )


__all__ = [
    "OptimizeTask",
    "PreparedTask",
    "PreparedTaskSet",
    "PreparedTaskSets",
    "PublishedTaskSets",
    "VerifierSpec",
    "WorkspaceSnapshot",
    "load_simple_tasks",
    "prepare_explicit_task_sets",
    "prepare_task_sets",
    "publish_prepared_task_sets",
]
