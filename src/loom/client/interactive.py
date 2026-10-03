"""Default interactive entry: a fresh waiting session or an existing task."""

import argparse
import os
from pathlib import Path
from urllib.error import URLError

from loom.client.budgets import parse_token_budget
from loom.client.protocol import SessionClient
from loom.service.contracts import ServiceError


def resume_session(client, sid):
    snapshot = client.snapshot(sid)
    state = snapshot["task"]["state"]
    request = snapshot.get("input_request")
    if (request and request["state"] == "pending") or snapshot.get("workspace_blocked"):
        return
    command = "reopen_task" if state == "completed" else "resume" if state in {"paused", "failed", "recovering"} else None
    if not command:
        return
    try:
        client.command(sid, command, expected_task_revision=snapshot["task"]["revision"])
    except ServiceError as exc:
        # A second frontend may already have resumed or reopened this task.
        if exc.status != 409:
            raise
        current = client.snapshot(sid)
        pending = current.get("input_request")
        if (pending and pending["state"] == "pending") or current.get("workspace_blocked"):
            return
        if current["task"]["state"] == state:
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="loom",
        description="Open a new session and enter a task, or resume an existing session.",
        epilog="Other commands: serve, session, task, campaign, candidate, experiment, task-set, governance, optimize.",
    )
    parser.add_argument("--resume", metavar="SESSION_ID")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace for a new session (default: current directory)")
    parser.add_argument("--model", help="Server-owned model name for a new session")
    parser.add_argument("--plan-mode", choices=("auto", "force", "off"), default="auto")
    parser.add_argument("--token-budget", type=parse_token_budget, metavar="TOKENS", help="Token budget, e.g. 10M or 500K (new-session default: 10M)")
    parser.add_argument("--url", default=os.environ.get("LOOM_SERVICE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token-file", type=Path, default=Path(os.environ.get("LOOM_SERVICE_TOKEN_FILE", ".loom/service/credential")))
    args = parser.parse_args(argv)
    try:
        try:
            from loom.client.tui import SessionTuiApp
        except ImportError as exc:
            raise ServiceError("Install loom[tui] or run uv run --extra tui loom to use the interactive client") from exc
        token = os.environ.get("LOOM_SERVICE_TOKEN") or args.token_file.read_text().strip()
        client = SessionClient(args.url, token)
        if args.resume is not None:
            sid = args.resume
            if args.token_budget is not None:
                client.command(sid, "set_token_budget", {"max_tokens": args.token_budget})
            resume_session(client, sid)
        else:
            payload = {"workspace": str(args.workspace.resolve()), "plan_mode": args.plan_mode}
            if args.model is not None:
                payload["model"] = args.model
            if args.token_budget is not None:
                payload["limits"] = {"max_tokens": args.token_budget}
            sid = client.create(payload)["session_id"]
        SessionTuiApp(client, sid).run()
        return 0
    except KeyboardInterrupt:
        return 0
    except (ServiceError, OSError, URLError) as exc:
        print(f"Session command failed: {exc}")
        return 1
