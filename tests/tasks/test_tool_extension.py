import asyncio
from copy import deepcopy

import pytest

from loom.tasks.assembly import TaskAssembly, normalize_task_spec
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context
from loom.tasks.tool_extension import extension


def spec(root):
    return normalize_task_spec({"session_environment": {"plugin": "session", "resources": [
        {"id": "workspace", "kind": "directory", "uri": str(root), "access": "read-write"}]},
        "tools": {"collections": ["filesystem", "task_control"]}})


def test_extension_preserves_workflow_evidence_and_acceptance_without_replaying_tools(tmp_path):
    original = spec(tmp_path)
    extended = deepcopy(original)
    extended["tools"]["collections"].append("shell")
    old = TaskAssembly(TaskRequest("Read then validate", task_spec=original))
    new = TaskAssembly(TaskRequest("Read then validate", task_spec=extended))
    try:
        snapshot = old.snapshot()
        snapshot["acceptance_calls"] = [{"id": "tool:original-run:read", "tool_id": "read_file"}]
        snapshot["evidence"] = ["existing-evidence"]
        snapshot["acceptance"].update(model_calls=2, candidate="Retained candidate")
        new.restore(snapshot, tool_extension=extension(original, extended))
        restored = new.snapshot()
        old.restore(snapshot)
        baseline = old.snapshot()
        for key in ("context", "acceptance_calls", "evidence", "acceptance"):
            assert restored[key] == baseline[key]
        # The workflow retains its plan and progress, while refreshing visible tool refs.
        for key, value in baseline['workflow']['state'].items():
            if key != 'normal_refs':
                assert restored['workflow']['state'][key] == value
        assert "shell_execute" in new.bindings
        with pytest.raises(ValueError, match="checkpoint configuration"):
            new.restore(snapshot, tool_extension={**extension(original, extended), "source_digest": "wrong"})
    finally:
        asyncio.run(old.close())
        asyncio.run(new.close())


@pytest.mark.parametrize("change", ["workspace", "workflow", "remove", "budget"])
def test_tool_extension_cannot_change_existing_contract_or_permissions(tmp_path, change):
    before = spec(tmp_path)
    after = deepcopy(before)
    after["tools"]["collections"].append("shell")
    if change == "workspace":
        after["session_environment"]["resources"][0]["access"] = "read-only"
    elif change == "workflow":
        after["workflow"]["mode"] = "off"
    elif change == "remove":
        after["tools"]["collections"].remove("filesystem")
    else:
        after["tools"]["budget"] = {"max_tools": 1}
    with pytest.raises(ValueError, match="only append"):
        extension(before, after)


def test_visibility_budget_keeps_work_and_acceptance_required_tools(tmp_path):
    config = spec(tmp_path)
    config['workflow']['mode'] = 'off'
    config["tools"].update(collections=["filesystem", "shell", "task_control"],
                           budget={"max_tools": 1, "max_tool_schema_tokens": 1})
    request = TaskRequest("Read and validate", task_spec=config, metadata={"autonomy": {"required_tools": ["shell_execute"]}})
    assembly = TaskAssembly(request)
    try:
        assembly.acceptance.data["plan"] = {"criteria": [{"verifier": "tool_call", "check": {"tool_id": "read_file"}}]}
        context = make_task_context(request, assembly=assembly).unwrap()
        visible = {ref.id for ref in assembly.resolve_tools(context)}
        assert {"finish", "shell_execute", "read_file"} <= visible
        assert not {"write_file", "edit_file", "process_execute"} & visible
    finally:
        asyncio.run(assembly.close())


def test_new_capability_block_replaces_stale_completion_decision(tmp_path):
    from loom.tasks.acceptance import AcceptanceBlocked

    assembly = TaskAssembly(TaskRequest('Validate', task_spec=spec(tmp_path)))
    try:
        assembly.acceptance.data.update(decision={'reason_code': 'ACCEPTANCE_REPAIR_REQUIRED'}, plan={
            'criteria': [{'id': 'check', 'description': 'Execute validation', 'verifier': 'semantic',
                          'required_tools': ['shell_execute']}]})
        with pytest.raises(AcceptanceBlocked):
            assembly.acceptance.require_capabilities()
        public = assembly.acceptance.public()
        assert public['decision'] is None
        assert public['cause_code'] == 'ACCEPTANCE_CAPABILITY_UNAVAILABLE'
    finally:
        asyncio.run(assembly.close())
