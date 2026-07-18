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
