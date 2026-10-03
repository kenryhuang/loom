"""Scriptable session commands and optional interactive TUI connection."""

import argparse
import json
import os
from pathlib import Path
from urllib.error import URLError

from loom.client.budgets import parse_token_budget, snapshot_token_budget
from loom.client.protocol import SessionClient
from loom.service.contracts import ServiceError


def main(argv=None):
    parser = argparse.ArgumentParser(prog="loom session")
    parser.add_argument("--url", default=os.environ.get("LOOM_SERVICE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token-file", type=Path, default=Path(os.environ.get("LOOM_SERVICE_TOKEN_FILE", ".loom/service/credential")))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    create = commands.add_parser("create")
    create.add_argument("objective")
    create.add_argument("--workspace", type=Path, default=Path.cwd())
    create.add_argument("--title")
    create.add_argument("--model")
    create.add_argument("--plan-mode", choices=("auto", "force", "off"), default="auto")
    create.add_argument("--command-id")
    create.add_argument("--token-budget", type=parse_token_budget, metavar="TOKENS")
    budget = commands.add_parser("budget", help="Show or change a paused session's token budget")
    budget.add_argument("session_id")
    budget.add_argument("tokens", nargs="?", type=parse_token_budget, help="New budget, e.g. 10M or 500K")
    budget.add_argument("--command-id")
    for kind in ("message", "answer", "supersede", "pause", "resume", "stop", "complete", "reopen", "snapshot", "history", "events", "artifact", "connect"):
        sub = commands.add_parser(kind)
        sub.add_argument("session_id", nargs="?" if kind == "connect" else None)
        if kind in {"message", "answer", "supersede"}:
            sub.add_argument("content")
        if kind in {"answer", "supersede"}:
            sub.add_argument("--request-id", required=True)
        if kind in {"message", "answer", "supersede", "pause", "resume", "stop", "complete", "reopen"}:
            sub.add_argument("--command-id")
        if kind == "history":
            sub.add_argument("--before", type=int)
            sub.add_argument("--limit", type=int, default=200)
        if kind == "events":
            sub.add_argument("--after", type=int, default=0)
        if kind == "artifact":
            sub.add_argument("digest")
            sub.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        token = os.environ.get("LOOM_SERVICE_TOKEN") or args.token_file.read_text().strip()
        client = SessionClient(args.url, token)
        if args.command == "list":
            result = client.list_sessions()
        elif args.command == "create":
            payload = {"objective": args.objective, "workspace": str(args.workspace.resolve()), "plan_mode": args.plan_mode}
            payload.update({k: getattr(args, k) for k in ("title", "model") if getattr(args, k) is not None})
            if args.token_budget is not None:
                payload["limits"] = {"max_tokens": args.token_budget}
            result = client.create(payload, command_id=args.command_id)
        elif args.command == "budget":
            if args.tokens is not None:
                client.command(args.session_id, "set_token_budget", {"max_tokens": args.tokens}, command_id=args.command_id)
            result = snapshot_token_budget(client.snapshot(args.session_id))
        elif args.command == "connect":
            try:
                from loom.client.tui import SessionTuiApp
            except ImportError as exc:
                raise ServiceError("Install loom[tui] to use the interactive client") from exc
            SessionTuiApp(client, args.session_id).run()
            return 0
        elif args.command == "snapshot":
            result = client.snapshot(args.session_id)
        elif args.command == "history":
            result = client.history(args.session_id, before=args.before, limit=args.limit)
        elif args.command == "events":
            for event in client.events(args.session_id, args.after):
                print(json.dumps(event, ensure_ascii=False), flush=True)
            return 0
        elif args.command == "artifact":
            data = client.artifact(args.session_id, args.digest)
            if args.output:
                args.output.write_bytes(data)
            else:
                print(data.decode())
            return 0
        else:
            kinds = {
                "message": "submit_message",
                "answer": "answer_input",
                "supersede": "supersede_input",
                "stop": "stop_run",
                "complete": "complete_task",
                "reopen": "reopen_task",
            }
            payload = {}
            if args.command in {"message", "supersede", "answer"}:
                payload["answer" if args.command == "answer" else "content"] = args.content
            if args.command in {"answer", "supersede"}:
                payload["request_id"] = args.request_id
            result = client.command(args.session_id, kinds.get(args.command, args.command), payload, command_id=args.command_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 0
    except (ServiceError, OSError, URLError) as exc:
        print(f"Session command failed: {exc}")
        return 1
