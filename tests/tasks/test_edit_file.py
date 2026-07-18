import asyncio

import loom.tasks.edit_file as edit_file_module
from loom.tasks.edit_file import (
    TextEdit,
    apply_file_edits,
    parse_text_edits,
    plan_text_edits,
)
from loom.tasks.request import TaskRequest
from loom.tasks.tools import make_task_tools


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


def test_plan_text_edits_allows_deletion():
    result = plan_text_edits("keep\nremove\n", (TextEdit("remove\n", ""),), path="sample.txt")

    assert result.ok
    assert result.value.content == "keep\n"


def test_plan_text_edits_rejects_occurrence_out_of_range():
    result = plan_text_edits(
        "same\nsame\n",
        (TextEdit("same", "changed", occurrence=3),),
        path="sample.txt",
    )

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
    result = plan_text_edits(
        "console.log(‘hello’);\n",
        (TextEdit("console.log('hello');", "changed"),),
        path="sample.txt",
    )

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"


def test_apply_file_edits_preserves_crlf_bom_and_permissions(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_bytes("\ufefffirst\r\nsecond\r\n".encode())
    path.chmod(0o640)

    result = apply_file_edits(path, (TextEdit("second\n", "SECOND\n"),), display_path="sample.txt")

    assert result.ok
    assert path.read_bytes() == "\ufefffirst\r\nSECOND\r\n".encode()
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
                "edits": [
                    {
                        "old_text": "same",
                        "new_text": "changed",
                        "occurrence": 2,
                    }
                ],
            }
        )
    )

    assert result.ok
    assert result.value.source == "edit_file"
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
        handler(
            {
                "path": "../outside.txt",
                "edits": [{"old_text": "hello", "new_text": "world"}],
            }
        )
    )

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert outside.read_text(encoding="utf-8") == "hello\n"


def test_apply_file_edits_cleans_temporary_file_after_replace_failure(tmp_path, monkeypatch):
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
