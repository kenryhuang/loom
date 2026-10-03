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
