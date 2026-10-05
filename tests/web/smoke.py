"""Isolated, deterministic service for exercising the browser without an LLM.

Run with PYTHONPATH=src:. python tests/web/smoke.py, then open the printed URL.
The credential is for this temporary service only. Ctrl+C removes its data.
"""

import asyncio
import json
from tempfile import TemporaryDirectory

from loom.core import ok
from loom.llm.api import LlmResponse, LlmToolCall
from loom.service.api import ServiceHTTPServer
from loom.service.controller import LoomService
from loom.web.frontend import builtin_templates


class BrowserProvider:
    model = "browser-smoke"

    def __init__(self, state):
        self.objective = state["task"]["objective"]

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        if self.objective == "route-failure":
            return ok(LlmResponse('{"action":{"kind":"none","input":{"report":"No routing tool selected"}}}'))
        if self.objective.startswith("question") and not any(message.name == "request_input" for message in messages):
            return ok(LlmResponse("", (LlmToolCall("question", "request_input", '{"question":"Which audience?"}'),)))
        await asyncio.sleep(12 if self.objective.startswith("slow") else 0.05)
        report = "# 浏览器结果\n\n已完成 **测试任务**。\n\n- 中文与 emoji 😀\n- [Python](https://www.python.org/)\n\n```json\n{\"ok\": true}\n```"
        # Exercise result envelope extraction in the browser, not just Markdown.
        return ok(LlmResponse(json.dumps({"action": {"kind": "none", "input": {"report": json.dumps({"report": report})}}})))


def provider_factory(state):
    return BrowserProvider(state)


def main():
    with TemporaryDirectory(prefix="loom-web-smoke-") as directory:
        service = LoomService(directory, provider_factory=provider_factory).start()
        server = ServiceHTTPServer(("127.0.0.1", 0), service, "browser-smoke-token")
        spec = builtin_templates()[0]["task_spec"]
        spec["outputs"] = [{"kind": "report", "format": "markdown"}]
        service.create("demo", {"objective": "Summarize the browser test", "title": "Browser demo", "task_spec": spec})
        service.create("question", {"objective": "question about audience", "title": "Pending question", "task_spec": spec})
        service.create("failed-route", {"objective": "route-failure", "title": "Failed route", "workspace": directory})
        print(f"http://127.0.0.1:{server.server_port}/web/\nCredential: browser-smoke-token", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            service.close()


if __name__ == "__main__":
    main()
