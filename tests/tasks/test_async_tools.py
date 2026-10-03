import asyncio
import contextlib
import sys
import time

from loom.tasks.request import TaskRequest
from loom.tasks.tools import make_task_tools


def test_shell_command_does_not_block_other_coroutines(tmp_path):
    async def scenario():
        tools = make_task_tools(TaskRequest("Work", workspace=tmp_path))
        started = time.monotonic()
        task = asyncio.create_task(tools["shell_execute"]({"command": [sys.executable, "-c", "import time; time.sleep(.3)"]}, {}))
        await asyncio.sleep(0.025)
        assert not task.done()
        assert time.monotonic() - started < 0.2
        result = await task
        assert result.value.value["exit_code"] == 0

    asyncio.run(scenario())


def test_shell_cancellation_reaps_child_and_bounds_output(tmp_path):
    async def scenario():
        tool = make_task_tools(TaskRequest("Work", workspace=tmp_path))["shell_execute"]
        task = asyncio.create_task(tool({"command": [sys.executable, "-c", "import time; time.sleep(30)"]}, {}))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        output = await tool({"command": [sys.executable, "-c", "print('x' * 10000)"]}, {"max_output_bytes": 1000})
        assert len(output.value.value["stdout"].encode()) <= 1000
        assert output.value.value["output_truncated"]

    asyncio.run(scenario())


def test_timeout_reaps_descendant_that_ignores_termination(tmp_path):
    async def scenario():
        tool = make_task_tools(TaskRequest("Work", workspace=tmp_path))["shell_execute"]
        code = (
            "import subprocess,time; subprocess.Popen(['"
            + sys.executable
            + "','-c',\"import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(2); "
            "open('leaked','w').write('bad')\"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(30)"
        )
        result = await tool({"command": [sys.executable, "-c", code], "timeout_seconds": 1}, {})
        assert result.value.value["timed_out"]
        await asyncio.sleep(1.5)
        assert not (tmp_path / "leaked").exists()

    asyncio.run(scenario())


def test_managed_process_registration_precedes_side_effect(tmp_path):
    async def scenario():
        tool = make_task_tools(TaskRequest("Work", workspace=tmp_path))["shell_execute"]

        def registered(_pid):
            time.sleep(0.2)
            assert not (tmp_path / "marker").exists()

        result = await tool({"command": [sys.executable, "-c", "open('marker','w').write('ok')"]}, {"process_started": registered})
        assert result.ok and result.value.value["exit_code"] == 0
        assert (tmp_path / "marker").read_text() == "ok"

    asyncio.run(scenario())
