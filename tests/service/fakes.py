import asyncio
import json

from loom.core import ok
from loom.llm.api import LlmResponse, LlmToolCall


class FakeProvider:
    model = "test-model"

    def __init__(self, state):
        self.objective = state["task"]["objective"]

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        results = [m for m in messages if m.role == "tool"]
        if "uncertain" in self.objective and not results:
            command = ["python3", "-c", "import os,signal; open('marker','a').write('once'); os.kill(os.getppid(),signal.SIGKILL)"]
            return ok(LlmResponse("", (LlmToolCall("side-effect", "shell_execute", json.dumps({"command": command})),)))
        if "question" in self.objective and not results:
            return ok(LlmResponse("", (LlmToolCall("ask", "request_input", '{"question":"Which module?"}'),)))
        if "slow" in self.objective and not results:
            return ok(
                LlmResponse(
                    "", (LlmToolCall("shell", "shell_execute", json.dumps({"command": ["python3", "-c", "import time; time.sleep(.5); print('ok')"]})),)
                )
            )
        if "crash" in self.objective and not results:
            import os

            os._exit(7)
        await asyncio.sleep(0.03)
        return ok(LlmResponse(json.dumps({"reasoning": "verified", "action": {"kind": "none", "description": "Complete", "input": {"report": "done"}}})))


def provider_factory(state):
    return FakeProvider(state)
