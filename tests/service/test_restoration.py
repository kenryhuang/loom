import asyncio
from dataclasses import replace

import pytest

from loom.core import Observation
from loom.runtime.checkpoints import decode, encode
from loom.service.contracts import ServiceError
from loom.service.restoration import prepare_restore, validate_restore
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context


def test_failed_run_restore_manifest_keeps_one_committed_state_and_source_identity(tmp_path):
    assembly = TaskAssembly(TaskRequest("Read sources", workspace=tmp_path), plan_mode="off")
    try:
        context = make_task_context(assembly.request, assembly=assembly, plan_mode="off").unwrap()
        observation = Observation("old-call", "read_file", {"content": "fact"}, "2026-10-31")
        snapshot = assembly.snapshot()
        snapshot["acceptance_calls"] = [{"id": "tool:old-run:old-call", "artifact": {"sha256": "original"}}]
        checkpoint = {"context": context, "observations": [observation], "plugin_states": snapshot}
        state = {"run": {"id": "old-run", "state": "failed", "task_episode_id": "episode", "counters": {"used": 42}},
                 "messages": [], "input_cursor": 0, "context": encode(context), "autonomy": {"work_package_id": "work"}}
        intent = prepare_restore(state, checkpoint)
        restored = decode(state["context"])
        assert intent.mode == "same_task"
        assert intent.manifest["journal_run_id"] == "old-run"
        assert intent.manifest["counters"] == {"used": 42}
        assert restored.state.observations == (observation,)
        assert restored.state.scratch["execution_plugins"]["acceptance_calls"][0]["id"] == "tool:old-run:old-call"
        state["run"] = {"id": "new-run", "task_episode_id": "episode", "restore_intent": intent.snapshot()}
        validate_restore(state, restored)
        with pytest.raises(ServiceError, match="snapshot"):
            validate_restore(state, replace(restored, state=replace(restored.state, scratch={})))
        state["autonomy"] = {"work_package_id": "different"}
        with pytest.raises(ServiceError, match="work package"):
            validate_restore(state, restored)
    finally:
        asyncio.run(assembly.close())


@pytest.mark.parametrize("restarting,old_state", [(True, "failed"), (False, "completed"), (False, "stopped")])
def test_new_task_or_restart_does_not_import_prior_acceptance(restarting, old_state):
    state = {"run": {"id": "old", "state": old_state, "task_episode_id": "episode"},
             "messages": [{"role": "user", "seq": 2}], "input_cursor": 1, "context": None}
    intent = prepare_restore(state, {"plugin_states": {"acceptance": {"state": "passed"}}}, restarting=restarting)
    assert intent.mode in {"new_task", "restart"}
    assert intent.manifest is None and intent.task_episode_id is None
