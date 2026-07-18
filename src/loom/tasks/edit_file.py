"""Exact, atomic text editing for Loom task workspaces."""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
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
class FileEditResult:
    path: str
    replacements: int
    bytes_written: int
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
            return _validation_error(
                "Edit contains unknown fields",
                edit_index=index,
                fields=sorted(unknown),
            )
        old_text = item.get("old_text")
        new_text = item.get("new_text")
        occurrence = item.get("occurrence")
        if not isinstance(old_text, str) or not old_text:
            return _validation_error("old_text must be a non-empty string", edit_index=index)
        if not isinstance(new_text, str):
            return _validation_error("new_text must be a string", edit_index=index)
        if occurrence is not None and (isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 1):
            return _validation_error(
                "occurrence must be an integer greater than or equal to 1",
                edit_index=index,
            )
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
        invalid = _validate_text_edit(edit, path=path, edit_index=index)
        if invalid is not None:
            return invalid
        positions = _non_overlapping_positions(content, edit.old_text)
        if not positions:
            return _validation_error(
                "old_text was not found",
                path=path,
                edit_index=index,
                occurrences=0,
            )
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
    return ok(
        EditPlan(
            updated,
            len(matched),
            content.count("\n", 0, first_offset) + 1,
            diff,
            truncated,
        )
    )


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
        TextEdit(
            _normalize_newlines(edit.old_text),
            _normalize_newlines(edit.new_text),
            edit.occurrence,
        )
        for edit in edits
    )
    planned = plan_text_edits(
        normalized,
        normalized_edits,
        path=display_path,
        max_diff_bytes=max_diff_bytes,
    )
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
            with suppress(FileNotFoundError):
                os.unlink(temporary_path)

    plan = planned.value
    return ok(
        FileEditResult(
            display_path,
            plan.replacements,
            len(encoded),
            plan.first_changed_line,
            plan.diff,
            plan.diff_truncated,
        )
    )


def _validate_text_edit(edit: TextEdit, *, path: str, edit_index: int) -> Result | None:
    if not isinstance(edit.old_text, str) or not edit.old_text:
        return _validation_error("old_text must be a non-empty string", path=path, edit_index=edit_index)
    if not isinstance(edit.new_text, str):
        return _validation_error("new_text must be a string", path=path, edit_index=edit_index)
    if edit.occurrence is not None and (isinstance(edit.occurrence, bool) or not isinstance(edit.occurrence, int) or edit.occurrence < 1):
        return _validation_error(
            "occurrence must be an integer greater than or equal to 1",
            path=path,
            edit_index=edit_index,
        )
    return None


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


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _detect_newline(value: str) -> str:
    positions = [(value.find(token), token) for token in ("\r\n", "\n", "\r") if token in value]
    return min(positions, default=(0, "\n"), key=lambda item: item[0])[1]


def _restore_newlines(value: str, newline: str) -> str:
    return value if newline == "\n" else value.replace("\n", newline)


def _validation_error(message: str, **metadata: Any) -> Result:
    return err(make_loom_error("VALIDATION_FAILED", message, retryable=False, metadata=metadata))


def _tool_error(message: str, **metadata: Any) -> Result:
    return err(make_loom_error("TOOL_FAILED", message, retryable=False, metadata=metadata))
