import asyncio
import contextlib
import json
import shlex
import sys
import time

import pytest

from loom.core import thaw_json
from loom.execution.native import NativeToolExecutionRuntime
from loom.runtime.execution_contracts import Invocation
from loom.tasks.request import TaskRequest
from loom.tasks.tools import make_task_tools


def test_shell_command_does_not_block_other_coroutines(tmp_path):
    async def scenario():
        tools = make_task_tools(TaskRequest("Work", workspace=tmp_path))
        started = time.monotonic()
        task = asyncio.create_task(tools["process_execute"]({"argv": [sys.executable, "-c", "import time; time.sleep(.3)"]}, {}))
        await asyncio.sleep(0.025)
        assert not task.done()
        assert time.monotonic() - started < 0.2
        result = await task
        assert result.value.value["exit_code"] == 0

    asyncio.run(scenario())


def test_shell_cancellation_reaps_child_and_bounds_output(tmp_path):
    async def scenario():
        tool = make_task_tools(TaskRequest("Work", workspace=tmp_path))["process_execute"]
        task = asyncio.create_task(tool({"argv": [sys.executable, "-c", "import time; time.sleep(30)"]}, {}))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        output = await tool({"argv": [sys.executable, "-c", "print('x' * 10000)"]}, {"max_output_bytes": 1000})
        assert len(output.value.value["stdout"].encode()) <= 1000
        assert output.value.value["output_truncated"]

    asyncio.run(scenario())


def test_timeout_reaps_descendant_that_ignores_termination(tmp_path):
    async def scenario():
        tool = make_task_tools(TaskRequest("Work", workspace=tmp_path))["process_execute"]
        code = (
            "import subprocess,time; subprocess.Popen(['"
            + sys.executable
            + "','-c',\"import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(2); "
            "open('leaked','w').write('bad')\"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(30)"
        )
        result = await tool({"argv": [sys.executable, "-c", code], "timeout_seconds": 1}, {})
        assert result.value.value["timed_out"]
        await asyncio.sleep(1.5)
        assert not (tmp_path / "leaked").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["shell_execute", "process_execute"])
def test_managed_process_registration_precedes_side_effect(tmp_path, name):
    async def scenario():
        tool = make_task_tools(TaskRequest("Work", workspace=tmp_path))[name]

        def registered(_pid):
            time.sleep(0.2)
            assert not (tmp_path / "marker").exists()

        argv = [sys.executable, "-c", "open('marker','w').write('ok')"]
        payload = {"argv": argv} if name == "process_execute" else {"command": shlex.join(argv)}
        result = await tool(payload, {"process_started": registered})
        assert result.ok and result.value.value["exit_code"] == 0
        assert (tmp_path / "marker").read_text() == "ok"

    asyncio.run(scenario())


def test_shell_operators_and_pipefail(tmp_path):
    async def scenario():
        tool = make_task_tools(TaskRequest("Work", workspace=tmp_path))["shell_execute"]
        (tmp_path / "space dir").mkdir()
        result = (await tool({"command": "cd 'space dir' && printf '%s' 'hello world' | tr a-z A-Z > result && cat result"})).unwrap()
        assert result.value["stdout"] == "HELLO WORLD"
        assert (tmp_path / "space dir" / "result").read_text() == "HELLO WORLD"
        failed = (await tool({"command": "false | cat && touch should-not-exist"})).unwrap()
        assert failed.value["status"] == "nonzero_exit" and not failed.value["ok"]
        assert not (tmp_path / "should-not-exist").exists()

    asyncio.run(scenario())


def test_process_arguments_are_literal_and_input_errors_are_actionable(tmp_path):
    async def scenario():
        tools = make_task_tools(TaskRequest("Work", workspace=tmp_path))
        argv = [sys.executable, "-c", "import sys,json; print(json.dumps(sys.argv[1:]))", "space dir", "&&", "$HOME", "*.py"]
        result = (await tools["process_execute"]({"argv": argv})).unwrap()
        assert json.loads(result.value["stdout"]) == argv[3:]
        for wrong in (["touch", "marker"], json.dumps(["touch", "marker"])):
            rejected = await tools["shell_execute"]({"command": wrong})
            assert not rejected.ok and "process_execute" in rejected.error.message
        for wrong in ("git status", [], [5], [""]):
            rejected = await tools["process_execute"]({"argv": wrong})
            assert not rejected.ok and "argv" in rejected.error.message
        assert not (tmp_path / "marker").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("managed", [False, True])
def test_process_launch_failure_and_search_no_match(tmp_path, managed):
    async def scenario():
        tool = make_task_tools(TaskRequest("Work", workspace=tmp_path))["process_execute"]
        options = {"process_started": lambda _pid: None} if managed else {}
        failed = (await tool({"argv": ["/no/such/executable"]}, options)).unwrap()
        assert failed.value["status"] == "launch_failed" and not failed.value["ok"]
        (tmp_path / "sample").write_text("hello\n")
        empty = (await tool({"argv": ["grep", "absent", "sample"]}, options)).unwrap()
        assert empty.value["exit_code"] == 1
        assert empty.value["status"] == "no_match" and empty.value["ok"]
        invalid = (await tool({"argv": ["grep", "[", "sample"]}, options)).unwrap()
        assert invalid.value["status"] == "nonzero_exit" and not invalid.value["ok"]

    asyncio.run(scenario())


def test_runtime_selects_shell_and_keeps_tool_semantics(tmp_path):
    async def scenario():
        handlers = make_task_tools(TaskRequest("Work", workspace=tmp_path))
        runtime = NativeToolExecutionRuntime({"shell": ["/bin/sh", "-c"]}, entrypoints=handlers)
        invocation = Invocation("operation", "attempt", "shell_execute", "shell_execute", {"command": "false | cat"})
        # Unlike bash+pipefail, this explicitly configured POSIX shell reports the final pipeline command.
        result = await runtime.execute(invocation)
        assert thaw_json(result.result.value.value)["exit_code"] == 0
        await runtime.close()

    asyncio.run(scenario())
