# Loom Task `edit_file` Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a workspace-bounded `edit_file` tool that atomically applies one or more exact text replacements, including 1-based occurrence selection for repeated strings.

**Architecture:** A new `loom.tasks.edit_file` module owns parsing, exact match planning, overlap detection, newline/BOM preservation, bounded unified diff generation, and atomic same-directory replacement. `loom.tasks.tools` adapts it to a Loom `Observation`, while `loom.tasks.runner` publishes the model-facing ToolRef/schema.

**Tech Stack:** Python 3.11+, standard-library `dataclasses`, `difflib`, `os`, `stat`, `tempfile`, Loom `Result`/`LoomError`, pytest, Ruff.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-18-task-edit-file-design.md` exactly.
- Match text exactly after newline normalization only; do not add fuzzy matching.
- `occurrence` is optional and 1-based over non-overlapping left-to-right matches.
- Every edit in a call matches the original file; overlapping targets fail the complete batch.
- Edit existing regular UTF-8 files only; `write_file` remains the creation/full-replacement tool.
- Keep paths inside the configured task workspace through `_resolve_workspace_path`.
- Preserve UTF-8 BOM, LF/CRLF convention, and permission bits.
- Use only Python's standard library; add no dependencies.
- Use `apply_patch` for source/test edits and TDD RED → GREEN for every task.

---

### Task 1: Exact Text Edit Planner

**Files:**
- Create: `src/loom/tasks/edit_file.py`
- Create: `tests/tasks/test_edit_file.py`

**Interfaces:**
- Produces: `TextEdit(old_text: str, new_text: str, occurrence: int | None)`.
- Produces: `EditPlan(content: str, replacements: int, first_changed_line: int, diff: str, diff_truncated: bool)`.
- Produces: `parse_text_edits(value: Any) -> Result`.
- Produces: `plan_text_edits(content: str, edits: tuple[TextEdit, ...], *, path: str, max_diff_bytes: int = 20_000) -> Result`.
- Later tasks consume all four interfaces without changing their names or field meanings.

- [x] **Step 1: Write failing planner tests**

Create `tests/tasks/test_edit_file.py` with these initial tests:

```python
from loom.tasks.edit_file import TextEdit, parse_text_edits, plan_text_edits


def test_plan_text_edits_replaces_unique_text_and_returns_diff():
    result = plan_text_edits(
        "alpha\nbeta\ngamma\n",
        (TextEdit("beta", "BETA"),),
        path="sample.txt",
    )

    assert result.ok
    assert result.value.content == "alpha\nBETA\ngamma\n"
    assert result.value.replacements == 1
    assert result.value.first_changed_line == 2
    assert "-beta" in result.value.diff
    assert "+BETA" in result.value.diff
    assert not result.value.diff_truncated


def test_plan_text_edits_uses_one_based_occurrence_for_repeated_text():
    result = plan_text_edits(
        "same\nsame\nsame\n",
        (TextEdit("same", "changed", occurrence=2),),
        path="sample.txt",
    )

    assert result.ok
    assert result.value.content == "same\nchanged\nsame\n"


def test_plan_text_edits_applies_disjoint_edits_against_original_content():
    result = plan_text_edits(
        "foo\nbar\nbaz\n",
        (
            TextEdit("foo\n", "foo bar\n"),
            TextEdit("bar\n", "BAR\n"),
        ),
        path="sample.txt",
    )

    assert result.ok
    assert result.value.content == "foo bar\nBAR\nbaz\n"


def test_plan_text_edits_rejects_ambiguous_match_without_occurrence():
    result = plan_text_edits("same same", (TextEdit("same", "changed"),), path="sample.txt")

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert result.error.metadata["occurrences"] == 2


def test_plan_text_edits_rejects_overlap_without_changing_input():
    original = "one\ntwo\nthree\n"
    result = plan_text_edits(
        original,
        (
            TextEdit("one\ntwo\n", "ONE\nTWO\n"),
            TextEdit("two\nthree\n", "TWO\nTHREE\n"),
        ),
        path="sample.txt",
    )

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert original == "one\ntwo\nthree\n"


def test_parse_text_edits_rejects_invalid_entries():
    invalid_values = (
        [],
        [{"old_text": "", "new_text": "x"}],
        [{"old_text": "x", "new_text": "y", "occurrence": 0}],
        [{"old_text": "x", "new_text": "y", "occurrence": True}],
        [{"old_text": "x", "new_text": "y", "extra": "no"}],
    )

    for value in invalid_values:
        result = parse_text_edits(value)
        assert not result.ok
        assert result.error.code == "VALIDATION_FAILED"
```

- [x] **Step 2: Run the planner tests and verify RED**

Run:

```bash
uv run pytest tests/tasks/test_edit_file.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'loom.tasks.edit_file'`.

- [x] **Step 3: Implement the minimal exact planner**

Create `src/loom/tasks/edit_file.py` with these immutable contracts and helpers:

```python
"""Exact, atomic text editing for Loom task workspaces."""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loom.core import Result, err, make_loom_error, ok, thaw_json

DEFAULT_MAX_DIFF_BYTES = 20_000


@dataclass(frozen=True, slots=True)
class TextEdit:
    old_text: str
    new_text: str
    occurrence: int | None = None


@dataclass(frozen=True, slots=True)
class EditPlan:
    content: str
    replacements: int
    first_changed_line: int
    diff: str
    diff_truncated: bool


@dataclass(frozen=True, slots=True)
class _MatchedEdit:
    edit_index: int
    start: int
    end: int
    new_text: str


def parse_text_edits(value: Any) -> Result:
    raw = thaw_json(value)
    if not isinstance(raw, (list, tuple)) or not raw:
        return _validation_error("edits must contain at least one replacement")
    parsed: list[TextEdit] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            return _validation_error("Each edit must be an object", edit_index=index)
        unknown = set(item) - {"old_text", "new_text", "occurrence"}
        if unknown:
            return _validation_error("Edit contains unknown fields", edit_index=index, fields=sorted(unknown))
        old_text = item.get("old_text")
        new_text = item.get("new_text")
        occurrence = item.get("occurrence")
        if not isinstance(old_text, str) or not old_text:
            return _validation_error("old_text must be a non-empty string", edit_index=index)
        if not isinstance(new_text, str):
            return _validation_error("new_text must be a string", edit_index=index)
        if occurrence is not None and (isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 1):
            return _validation_error("occurrence must be an integer greater than or equal to 1", edit_index=index)
        parsed.append(TextEdit(old_text, new_text, occurrence))
    return ok(tuple(parsed))


def plan_text_edits(
    content: str,
    edits: tuple[TextEdit, ...],
    *,
    path: str,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> Result:
    if not edits:
        return _validation_error("edits must contain at least one replacement", path=path)
    matched: list[_MatchedEdit] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit.old_text, str) or not edit.old_text:
            return _validation_error("old_text must be a non-empty string", path=path, edit_index=index)
        if not isinstance(edit.new_text, str):
            return _validation_error("new_text must be a string", path=path, edit_index=index)
        if edit.occurrence is not None and (
            isinstance(edit.occurrence, bool) or not isinstance(edit.occurrence, int) or edit.occurrence < 1
        ):
            return _validation_error(
                "occurrence must be an integer greater than or equal to 1",
                path=path,
                edit_index=index,
            )
        positions = _non_overlapping_positions(content, edit.old_text)
        if not positions:
            return _validation_error("old_text was not found", path=path, edit_index=index, occurrences=0)
        if edit.occurrence is None:
            if len(positions) != 1:
                return _validation_error(
                    "old_text must be unique when occurrence is omitted",
                    path=path,
                    edit_index=index,
                    occurrences=len(positions),
                )
            selected = positions[0]
        elif edit.occurrence > len(positions):
            return _validation_error(
                "occurrence is out of range",
                path=path,
                edit_index=index,
                occurrence=edit.occurrence,
                occurrences=len(positions),
            )
        else:
            selected = positions[edit.occurrence - 1]
        matched.append(_MatchedEdit(index, selected, selected + len(edit.old_text), edit.new_text))

    matched.sort(key=lambda item: item.start)
    for previous, current in zip(matched, matched[1:], strict=False):
        if previous.end > current.start:
            return _validation_error(
                "Edit targets overlap",
                path=path,
                edit_index=current.edit_index,
                previous_edit_index=previous.edit_index,
            )

    updated = content
    for item in reversed(matched):
        updated = updated[: item.start] + item.new_text + updated[item.end :]
    if updated == content:
        return _validation_error("The edit batch would not change the file", path=path)

    first_offset = matched[0].start
    diff = "".join(
        difflib.unified_diff(
            content.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            n=3,
        )
    )
    diff, truncated = _truncate_utf8(diff, max_diff_bytes)
    return ok(EditPlan(updated, len(matched), content.count("\n", 0, first_offset) + 1, diff, truncated))


def _non_overlapping_positions(content: str, needle: str) -> tuple[int, ...]:
    positions: list[int] = []
    start = 0
    while True:
        position = content.find(needle, start)
        if position < 0:
            return tuple(positions)
        positions.append(position)
        start = position + len(needle)


def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _validation_error(message: str, **metadata: Any) -> Result:
    return err(make_loom_error("VALIDATION_FAILED", message, retryable=False, metadata=metadata))
```

- [x] **Step 4: Run the planner tests and verify GREEN**

Run:

```bash
uv run pytest tests/tasks/test_edit_file.py -q
```

Expected: all planner tests pass.

- [x] **Step 5: Add edge-case planner tests and keep GREEN**

Extend `tests/tasks/test_edit_file.py` with:

```python
def test_plan_text_edits_allows_deletion():
    result = plan_text_edits("keep\nremove\n", (TextEdit("remove\n", ""),), path="sample.txt")

    assert result.ok
    assert result.value.content == "keep\n"


def test_plan_text_edits_rejects_occurrence_out_of_range():
    result = plan_text_edits("same\nsame\n", (TextEdit("same", "changed", occurrence=3),), path="sample.txt")

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert result.error.metadata["occurrences"] == 2


def test_plan_text_edits_rejects_duplicate_target():
    result = plan_text_edits(
        "target\n",
        (TextEdit("target", "one"), TextEdit("target", "two")),
        path="sample.txt",
    )

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"


def test_plan_text_edits_does_not_match_text_introduced_by_earlier_edit():
    result = plan_text_edits(
        "foo\n",
        (TextEdit("foo", "bar"), TextEdit("bar", "baz")),
        path="sample.txt",
    )

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"


def test_plan_text_edits_truncates_diff_at_utf8_boundary():
    result = plan_text_edits(
        "汉" * 100 + "\n",
        (TextEdit("汉", "字", occurrence=50),),
        path="sample.txt",
        max_diff_bytes=80,
    )

    assert result.ok
    assert result.value.diff_truncated
    assert len(result.value.diff.encode("utf-8")) <= 80


def test_plan_text_edits_does_not_fuzzy_match_unicode_punctuation():
    result = plan_text_edits("console.log(‘hello’);\n", (TextEdit("console.log('hello');", "changed"),), path="sample.txt")

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
```

Run:

```bash
uv run pytest tests/tasks/test_edit_file.py -q
```

Expected: all tests pass; no test permits fuzzy matching.

- [x] **Step 6: Commit Task 1**

```bash
git add src/loom/tasks/edit_file.py tests/tasks/test_edit_file.py
git commit -m "feat: add exact text edit planner"
```

---

### Task 2: Atomic File Editing and Task Handler

**Files:**
- Modify: `src/loom/tasks/edit_file.py`
- Modify: `src/loom/tasks/tools.py:12-126`
- Modify: `tests/tasks/test_edit_file.py`

**Interfaces:**
- Consumes: `TextEdit`, `parse_text_edits`, and `plan_text_edits` from Task 1.
- Produces: `FileEditResult(path: str, replacements: int, bytes_written: int, first_changed_line: int, diff: str, diff_truncated: bool)`.
- Produces: `apply_file_edits(path: Path, edits: tuple[TextEdit, ...], *, display_path: str, max_diff_bytes: int = 20_000) -> Result`.
- Produces: `make_task_tools(request)["edit_file"]`, returning an `Observation` with the result fields.

- [x] **Step 1: Write failing filesystem and handler tests**

Add tests that:

```python
import asyncio

from loom.tasks.edit_file import TextEdit, apply_file_edits
from loom.tasks.request import TaskRequest
from loom.tasks.tools import make_task_tools


def test_apply_file_edits_preserves_crlf_bom_and_permissions(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_bytes("\ufefffirst\r\nsecond\r\n".encode("utf-8"))
    path.chmod(0o640)

    result = apply_file_edits(path, (TextEdit("second\n", "SECOND\n"),), display_path="sample.txt")

    assert result.ok
    assert path.read_bytes() == "\ufefffirst\r\nSECOND\r\n".encode("utf-8")
    assert path.stat().st_mode & 0o777 == 0o640


def test_apply_file_edits_failure_leaves_file_unchanged(tmp_path):
    path = tmp_path / "sample.txt"
    original = b"alpha\nbeta\n"
    path.write_bytes(original)

    result = apply_file_edits(
        path,
        (TextEdit("alpha", "ALPHA"), TextEdit("missing", "MISSING")),
        display_path="sample.txt",
    )

    assert not result.ok
    assert path.read_bytes() == original


def test_edit_file_handler_edits_workspace_file_and_returns_observation(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    handler = make_task_tools(TaskRequest("Edit sample", workspace=tmp_path))["edit_file"]

    result = asyncio.run(
        handler(
            {
                "path": "sample.txt",
                "edits": [{"old_text": "same", "new_text": "changed", "occurrence": 2}],
            }
        )
    )

    assert result.ok
    assert result.value.tool_id == "edit_file"
    assert result.value.value["path"] == "sample.txt"
    assert result.value.value["replacements"] == 1
    assert path.read_text(encoding="utf-8") == "same\nchanged\n"


def test_edit_file_handler_rejects_unknown_top_level_field(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("hello\n", encoding="utf-8")
    handler = make_task_tools(TaskRequest("Edit sample", workspace=tmp_path))["edit_file"]

    result = asyncio.run(
        handler(
            {
                "path": "sample.txt",
                "edits": [{"old_text": "hello", "new_text": "world"}],
                "extra": True,
            }
        )
    )

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert path.read_text(encoding="utf-8") == "hello\n"


def test_apply_file_edits_rejects_missing_directory_and_invalid_utf8(tmp_path):
    missing = apply_file_edits(tmp_path / "missing.txt", (TextEdit("a", "b"),), display_path="missing.txt")
    directory = apply_file_edits(tmp_path, (TextEdit("a", "b"),), display_path=".")
    binary_path = tmp_path / "binary.dat"
    binary_path.write_bytes(b"\xff\xfe")
    binary = apply_file_edits(binary_path, (TextEdit("a", "b"),), display_path="binary.dat")

    assert not missing.ok and missing.error.code == "TOOL_FAILED"
    assert not directory.ok and directory.error.code == "TOOL_FAILED"
    assert not binary.ok and binary.error.code == "TOOL_FAILED"


def test_edit_file_handler_rejects_workspace_traversal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("hello\n", encoding="utf-8")
    handler = make_task_tools(TaskRequest("Edit sample", workspace=workspace))["edit_file"]

    result = asyncio.run(
        handler({"path": "../outside.txt", "edits": [{"old_text": "hello", "new_text": "world"}]})
    )

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert outside.read_text(encoding="utf-8") == "hello\n"


def test_apply_file_edits_cleans_temporary_file_after_replace_failure(tmp_path, monkeypatch):
    from loom.tasks import edit_file as edit_file_module

    path = tmp_path / "sample.txt"
    original = b"hello\n"
    path.write_bytes(original)

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr(edit_file_module.os, "replace", fail_replace)
    result = apply_file_edits(path, (TextEdit("hello", "world"),), display_path="sample.txt")

    assert not result.ok
    assert result.error.code == "TOOL_FAILED"
    assert path.read_bytes() == original
    assert not tuple(tmp_path.glob(".sample.txt.*.tmp"))
```

- [x] **Step 2: Run filesystem/handler tests and verify RED**

Run:

```bash
uv run pytest tests/tasks/test_edit_file.py -q
```

Expected: fails because `apply_file_edits` and `make_task_tools(...)["edit_file"]` do not exist.

- [x] **Step 3: Implement atomic file application**

Extend `src/loom/tasks/edit_file.py` with:

```python
import os
import stat
import tempfile
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileEditResult:
    path: str
    replacements: int
    bytes_written: int
    first_changed_line: int
    diff: str
    diff_truncated: bool


def apply_file_edits(
    path: Path,
    edits: tuple[TextEdit, ...],
    *,
    display_path: str,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> Result:
    if not path.exists() or not path.is_file():
        return _tool_error("Edit target must be an existing regular file", path=display_path)
    try:
        raw = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return _tool_error(f"Failed to read edit target: {exc}", path=display_path)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _tool_error(f"Edit target is not valid UTF-8: {exc}", path=display_path)

    bom = "\ufeff" if decoded.startswith("\ufeff") else ""
    text = decoded[len(bom) :]
    newline = _detect_newline(text)
    normalized = _normalize_newlines(text)
    normalized_edits = tuple(
        TextEdit(_normalize_newlines(edit.old_text), _normalize_newlines(edit.new_text), edit.occurrence)
        for edit in edits
    )
    planned = plan_text_edits(normalized, normalized_edits, path=display_path, max_diff_bytes=max_diff_bytes)
    if not planned.ok:
        return planned
    final_text = bom + _restore_newlines(planned.value.content, newline)
    encoded = final_text.encode("utf-8")

    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        return _tool_error(f"Failed to replace edit target: {exc}", path=display_path)
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass

    plan = planned.value
    return ok(FileEditResult(display_path, plan.replacements, len(encoded), plan.first_changed_line, plan.diff, plan.diff_truncated))


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _detect_newline(value: str) -> str:
    positions = [(value.find(token), token) for token in ("\r\n", "\n", "\r") if token in value]
    return min(positions, default=(0, "\n"), key=lambda item: item[0])[1]


def _restore_newlines(value: str, newline: str) -> str:
    return value if newline == "\n" else value.replace("\n", newline)


def _tool_error(message: str, **metadata: Any) -> Result:
    return err(make_loom_error("TOOL_FAILED", message, retryable=False, metadata=metadata))
```

- [x] **Step 4: Register the task handler**

Modify `src/loom/tasks/tools.py` to import `apply_file_edits` and
`parse_text_edits`, validate top-level keys, resolve the workspace path, call
the file editor, and return this complete handler:

```python
async def edit_file(input_value: Any, _options: Mapping[str, Any] | None = None) -> Result:
    data = _tool_input(input_value)
    unknown = set(data) - {"path", "edits"}
    if unknown:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "edit_file input contains unknown fields",
                retryable=False,
                metadata={"fields": sorted(unknown)},
            )
        )
    resolved = _resolve_workspace_path(root, data.get("path"))
    if not resolved.ok:
        return resolved
    edits = parse_text_edits(data.get("edits"))
    if not edits.ok:
        return edits
    display_path = _relative_to_root(root, resolved.value)
    edited = apply_file_edits(resolved.value, edits.value, display_path=display_path)
    if not edited.ok:
        return edited
    value = edited.value
    return ok(
        Observation(
            new_trace_id(),
            "edit_file",
            {
                "path": value.path,
                "replacements": value.replacements,
                "bytes_written": value.bytes_written,
                "first_changed_line": value.first_changed_line,
                "diff": value.diff,
                "diff_truncated": value.diff_truncated,
            },
            now_iso(),
        )
    )
```

Add `"edit_file": edit_file` to the returned handler mapping.

- [x] **Step 5: Run Task 2 tests and verify GREEN**

Run:

```bash
uv run pytest tests/tasks/test_edit_file.py -q
```

Expected: all planner, filesystem, and handler tests pass.

- [x] **Step 6: Commit Task 2**

```bash
git add src/loom/tasks/edit_file.py src/loom/tasks/tools.py tests/tasks/test_edit_file.py
git commit -m "feat: add edit_file task handler"
```

---

### Task 3: ToolRef, Runtime Trace, and Smoke Integration

**Files:**
- Modify: `src/loom/tasks/runner.py:199-259`
- Modify: `src/loom/examples/real_project_smoke.py`
- Modify: `tests/tasks/test_task_runner.py`
- Modify: `tests/integration/test_real_project_smoke.py:148-225`
- Test: `tests/tasks/test_edit_file.py`

**Interfaces:**
- Consumes: the `edit_file` handler from Task 2.
- Produces: a ToolRef named `edit_file` with the exact schema from the design.
- Preserves: existing `read_file`, `write_file`, `shell_execute`, and `finish` contracts.

- [x] **Step 1: Write failing ToolRef and runtime tests**

Update `test_make_task_context_maps_request_to_loom_layers` to require
`edit_file`, then add:

```python
def test_make_task_context_exposes_exact_edit_file_schema(tmp_path):
    context = make_task_context(TaskRequest("Edit this project", workspace=tmp_path)).unwrap()
    tool = next(item for item in context.affordances.tools if item.id == "edit_file")
    schema = tool.input_schema

    assert schema["required"] == ["path", "edits"]
    assert schema["additionalProperties"] is False
    item_schema = schema["properties"]["edits"]["items"]
    assert item_schema["required"] == ["old_text", "new_text"]
    assert item_schema["properties"]["occurrence"]["minimum"] == 1
    assert item_schema["additionalProperties"] is False
```

Add this provider and test:

```python
class FakeEditTaskProvider:
    model = "fake-edit-task-model"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        self.calls += 1
        if self.calls == 1:
            return _response(
                content="",
                tool_calls=(
                    LlmToolCall(
                        "call-edit",
                        "edit_file",
                        json.dumps(
                            {
                                "path": "sample.txt",
                                "edits": [{"old_text": "same", "new_text": "changed", "occurrence": 2}],
                            }
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
        if self.calls == 2:
            return _response(
                content="",
                tool_calls=(LlmToolCall("call-finish", "finish", json.dumps({"report": "Edited sample."})),),
                finish_reason="tool_calls",
            )
        return _response(
            content=json.dumps(
                {
                    "reasoning": "The requested occurrence was edited and reported.",
                    "action": {"kind": "none", "description": "task complete", "target": None, "input": {}},
                    "alternatives": [],
                    "confidence": 0.9,
                }
            )
        )


def test_run_generic_task_executes_edit_file_and_traces_observation(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    trace_path = tmp_path / "runs" / "edit-task.jsonl"

    result = asyncio.run(
        run_generic_task(
            TaskRequest("Edit the second repeated string", workspace=tmp_path),
            provider=FakeEditTaskProvider(),
            options=TaskRunOptions(trace_path=trace_path),
        )
    )

    assert result.ok
    assert target.read_text(encoding="utf-8") == "same\nchanged\n"
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    completed = [record for record in records if record.get("eventType") == "tool.completed"]
    edit_record = next(record for record in completed if record["payload"]["tool_id"] == "edit_file")
    assert edit_record["payload"]["input"]["edits"][0]["occurrence"] == 2
    assert edit_record["payload"]["output"]["value"]["replacements"] == 1
```

- [x] **Step 2: Run integration tests and verify RED**

Run:

```bash
uv run pytest tests/tasks/test_task_runner.py tests/integration/test_real_project_smoke.py -q
```

Expected: failures show the ToolRef and expected built-in tool sequence do not
yet include `edit_file`.

- [x] **Step 3: Add the ToolRef and update smoke expectations**

Insert this ToolRef between `read_file` and `write_file` in
`src/loom/tasks/runner.py`:

```python
ToolRef(
    "edit_file",
    "Make precise exact-text replacements in an existing UTF-8 workspace file. "
    "Use occurrence to select repeated text; omitted occurrence requires a unique match. "
    "All edits match the original file. Use write_file for new files or intentional full replacement.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative path to an existing UTF-8 file."},
            "edits": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string"},
                        "occurrence": {"type": "integer", "minimum": 1},
                    },
                    "required": ["old_text", "new_text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["path", "edits"],
        "additionalProperties": False,
    },
),
```

Update exact built-in tool tuples in real-project smoke tests to:

```python
("read_file", "edit_file", "write_file", "shell_execute", "finish")
```

Register the corresponding real-project smoke handler using the shared exact
edit parser and atomic file application so its ToolRef and runtime registry
remain aligned.

- [x] **Step 4: Run task and smoke tests and verify GREEN**

Run:

```bash
uv run pytest tests/tasks tests/integration/test_real_project_smoke.py -q
```

Expected: all tests pass.

- [x] **Step 5: Run formatting, lint, and full regression tests**

Run:

```bash
uv run ruff format src/loom/tasks/edit_file.py src/loom/tasks/tools.py src/loom/tasks/runner.py src/loom/examples/real_project_smoke.py tests/tasks/test_edit_file.py tests/tasks/test_task_runner.py tests/integration/test_real_project_smoke.py
uv run ruff check src tests
uv run ruff format --check src/loom/tasks/edit_file.py src/loom/tasks/tools.py src/loom/tasks/runner.py src/loom/examples/real_project_smoke.py tests/tasks/test_edit_file.py tests/tasks/test_task_runner.py tests/integration/test_real_project_smoke.py
uv run pytest -q
```

Expected: Ruff reports no errors for the changed files and the full pytest suite passes with only the repository's expected skips.

- [x] **Step 6: Commit Task 3**

```bash
git add src/loom/tasks/runner.py src/loom/examples/real_project_smoke.py tests/tasks/test_task_runner.py tests/integration/test_real_project_smoke.py tests/tasks/test_edit_file.py
git commit -m "feat: expose edit_file in task runs"
```

## Review Remediation

- [x] Compute `first_changed_line` from the actual before/after common prefix,
  including multiline replacements and earlier no-op targets.
- [x] Reject non-string and malformed workspace paths as `VALIDATION_FAILED`
  instead of coercing them or allowing path-library exceptions to escape.
- [x] Include the safe workspace-relative path in edit-entry validation errors.
- [x] Re-run task/smoke, full pytest, Ruff lint, and changed-file format checks.

## Final Verification

- [x] Confirm `git status --short` is clean.
- [x] Confirm `git log -5 --oneline` contains the plan, three feature commits,
  and the review-fix commit.
- [x] Run `uv run pytest -q` and record the exact pass/skip counts.
- [x] Run `uv run ruff check src tests`.
- [x] Run changed-file `uv run ruff format --check ...`.
- [x] Compare the final diff against every acceptance criterion in
  `docs/superpowers/specs/2026-07-18-task-edit-file-design.md`.
