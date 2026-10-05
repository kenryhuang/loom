import asyncio
import json
import re

from loom.core import ok
from loom.llm.api import LlmResponse, LlmToolCall, TokenUsage


class FakeProvider:
    model = "test-model"

    def __init__(self, state):
        self.objective = state["task"]["objective"]

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        results = [m for m in messages if m.role == "tool"]
        names = {t["function"]["name"] for t in tools or ()}
        if "submit_plan" in names:
            return ok(LlmResponse("", (LlmToolCall("submit", "submit_plan", '{"explanation":"Review","items":[{"content":"Review module"}]}'),)))
        if "update_plan" in names and "planned" in self.objective:
            if re.search(r"^- \[x\]", "\n".join(m.content for m in messages), re.MULTILINE):
                return ok(LlmResponse("", (LlmToolCall("finish", "finish", '{"report":"Verified module"}'),)))
            item_id = re.search(r"^- \[[^]]*\] (\S+):", "\n".join(m.content for m in messages), re.MULTILINE).group(1)
            args = {"explanation": "Verified", "items": [{"id": item_id, "content": "Review module", "status": "completed"}]}
            return ok(LlmResponse("", (LlmToolCall("update", "update_plan", json.dumps(args)),)))
        if tools and {"enter_plan", "continue_react"}.issubset({t["function"]["name"] for t in tools}):
            return ok(LlmResponse("", (LlmToolCall("route", "continue_react", '{"reason":"Direct maintenance"}'),)))
        if "slow-model" in self.objective:
            await asyncio.sleep(30)
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
        usage = TokenUsage(6, 4, 10) if "token-budget" in self.objective else TokenUsage()
        return ok(
            LlmResponse(json.dumps({"reasoning": "verified", "action": {"kind": "none", "description": "Complete", "input": {"report": "done"}}}), usage=usage)
        )


def provider_factory(state):
    return FakeProvider(state)


class MissingRouteProvider:
    model = "missing-route-test"

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        return ok(LlmResponse('{"action":{"kind":"none","input":{"report":"Old task finished"}}}', usage=TokenUsage(2, 1, 3)))


def routing_failure_provider_factory(state):
    return FakeProvider(state) if state["run"].get("failure") else MissingRouteProvider()
