import json
from types import SimpleNamespace

import pytest

from loom.observability.result_format import format_result_text, render_result_markdown
from loom.tasks.runner import _report_from_run_result

REPORT = "# 结果\n\n**已完成**\n\n| 检查 | 状态 |\n| --- | --- |\n| 测试 | 通过 |\n\n```python\nprint('ok')\n```"


@pytest.mark.parametrize(
    "value",
    [
        REPORT,
        {"report": REPORT, "sources": ["source"]},
        json.dumps({"content": REPORT}),
        json.dumps({"reasoning": "internal", "action": {"kind": "none", "input": {"report": REPORT}}}),
        json.dumps({"result": {"output": {"markdown": REPORT}}, "status": "completed"}),
        "```json\n" + json.dumps({"answer": REPORT}) + "\n```",
        json.dumps(json.dumps({"final_answer": REPORT})),
    ],
)
def test_json_report_bodies_are_decoded_as_markdown(value):
    assert format_result_text(value) == REPORT
    assert render_result_markdown(value) == REPORT


@pytest.mark.parametrize("value", [{"架构": "事件驱动", "适用": ["异步业务", "审计"]}, [{"id": 1}, {"id": 2}], {"result": "ok", "items": [1, 2]}, {}])
def test_ordinary_structured_json_preserves_all_fields(value):
    encoded = json.dumps(value, ensure_ascii=False)
    assert json.loads(format_result_text(encoded)) == value
    rendered = render_result_markdown(encoded)
    assert rendered.startswith("```json\n")
    assert "\n  " in rendered or value == {}
    assert json.loads(rendered.split("\n", 1)[1].rsplit("\n", 1)[0]) == value


def test_embedded_json_examples_and_malformed_json_are_not_reinterpreted():
    text = '# 说明\n\n例子：\n\n```json\n{"report": "example"}\n```'
    assert render_result_markdown(text) == text
    assert render_result_markdown('{"report": invalid}') == '{"report": invalid}'


def test_code_fences_inside_json_do_not_break_the_json_block():
    value = {"snippet": "```markdown\n# hello\n```"}
    rendered = render_result_markdown(value)
    assert rendered.startswith("````json\n") and rendered.endswith("\n````")


def test_complete_markdown_wrapper_is_rendered_as_a_document():
    value = json.dumps({"report": "```markdown\n" + REPORT + "\n```"})
    assert render_result_markdown(value) == REPORT


@pytest.mark.parametrize("value", [{"适用场景": ["异步业务"]}, {"action": {"kind": "none", "input": {"report": {"适用场景": ["异步业务"]}}}}])
def test_task_reports_preserve_json_instead_of_python_dict_representations(value):
    result = SimpleNamespace(output=value, context=SimpleNamespace(state=SimpleNamespace(decisions=(), observations=())))
    assert json.loads(_report_from_run_result(result)) == {"适用场景": ["异步业务"]}


def test_report_fallback_and_completion_ignore_previous_run_results():
    from dataclasses import replace

    from loom.core import Action, Decision, Observation
    from loom.tasks.request import TaskRequest
    from loom.tasks.runner import _task_done, make_task_context

    context = make_task_context(TaskRequest("New request"), plan_mode="off").unwrap()
    context = replace(context, state=replace(context.state,
        observations=(Observation("old", "finish", {"completed": True, "report": "Old reply"}, "now"),),
        decisions=(Decision("old", Action("old", "none", "Done", {"report": "Old decision"}), "done", (), 1, "now"),),
    ), metadata={"result_scope": {"run_id": context.run_id, "observation_start": 1, "decision_start": 1}})
    result = SimpleNamespace(output=None, context=context)
    assert "Old" not in _report_from_run_result(result)
    assert _task_done(context, None).unwrap() is False
    current = Observation("current", "finish", {"completed": True, "report": "Current reply"}, "now")
    context = replace(context, state=replace(context.state, observations=(*context.state.observations, current)))
    assert _report_from_run_result(SimpleNamespace(output=None, context=context)) == "Current reply"
    assert _task_done(context, None).unwrap() is True
