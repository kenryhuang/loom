"""Offline M1 smoke through real HTTP, SQLite and spawned execution workers.

PYTHONPATH=src:. uv run python tests/autonomy/smoke.py [--serve]
--serve retains the temporary demo until Ctrl-C for browser inspection.
"""

import argparse
import http.client
import json
import tempfile
import threading
import time
from pathlib import Path

from loom.service.api import ServiceHTTPServer
from loom.service.controller import LoomService
from tests.autonomy.helpers import evaluation_provider, goals, role, single_package, solver_provider
from tests.autonomy.test_execution_flow import until


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="loom-agent-smoke-") as directory:
        path = Path(directory)
        (path / "notes.txt").write_text("Session fixture evidence: use current workspace evidence.")
        service = LoomService(
            path / "data",
            autonomy_enabled=True,
            provider_factory=solver_provider,
            evaluation_provider_factory=evaluation_provider,
            agent_planner=single_package,
        )
        token = "offline-agent-smoke"
        server = ServiceHTTPServer(("127.0.0.1", 0), service, token)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        service.start()

        def request(route, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            try:
                connection.request(
                    "POST" if body is not None else "GET",
                    "/v1/agents" + route,
                    body=json.dumps(body) if body is not None else None,
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                result = json.loads(response.read())
                assert response.status == 200, result
                return result
            finally:
                connection.close()

        try:
            agent = request("", dict(command_id="create", role=role(path)))
            aid = agent["id"]
            request("/" + aid + "/goals", dict(command_id="g1", goal=goals()[0]))
            request("/" + aid + "/commands", dict(command_id="enable", type="enable", expected_revision=1))
            first = until(lambda: request("/" + aid), lambda s: s["learning"] and s["learning"][0]["state"] == "completed")
            memory = request("/" + aid + "/memory?query=Session")["records"][0]
            request(
                "/" + aid + "/memory/annotations",
                dict(
                    command_id="scope",
                    kind="scope",
                    value="Only this smoke workspace",
                    expected_revision=0,
                    evaluation_id=memory["source"]["evaluation_id"],
                    record_id=memory["source"]["record_id"],
                    content_digest=memory["source"]["content_digest"],
                ),
            )
            request("/" + aid + "/guidance", dict(command_id="guide", text="Distinguish implementation from proposals", scope="persistent"))
            request("/" + aid + "/goals", dict(command_id="g2", goal=goals()[1]))
            second = until(lambda: request("/" + aid), lambda s: len(s["learning"]) == 2 and all(c["state"] == "completed" for c in s["learning"]))
            assert len(second["sessions"]) == 1
            assert all(g["state"] == "completed" for g in second["goals"])
            assert first["work_packages"][0]["run_ids"] != second["work_packages"][1]["run_ids"]
            # Stop scheduling, then close/reopen HTTP; background ownership is unchanged.
            paused = request("/" + aid + "/commands", dict(command_id="pause", type="pause", expected_revision=second["revision"]))
            print(
                json.dumps(
                    dict(
                        result="passed",
                        agent_id=aid,
                        sessions=1,
                        runs=2,
                        learning_cycles=2,
                        accepted_memories=sum(c["memory_result"]["accepted"] for c in second["learning"]),
                        state=paused["state"],
                        budget=second["budget"],
                    ),
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if args.serve:
                print(f"Browser demo: http://127.0.0.1:{server.server_port}/web/#/agents ; token: {token}", flush=True)
                print(f"Workspace: {path}", flush=True)
                while True:
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
            service.close()
        restored = LoomService(path / "data")
        try:
            saved = restored.background.autonomy.snapshot(aid)
            assert saved["state"] == "paused" and len(saved["learning"]) == 2
            assert restored.background.autonomy.supervisor.thread is None
            print("Restart with autonomous scheduling disabled: passed", flush=True)
        finally:
            restored.close()


if __name__ == "__main__":
    main()
