"""Unified Loom command dispatcher."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loom")
    parser.add_argument("command", choices=("campaign", "candidate", "experiment", "task-set", "governance", "optimize", "task"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "task":
        from loom.tasks.cli import main as command_main
    elif args.command == "campaign":
        from loom.campaigns.cli import main as command_main
    elif args.command == "candidate":
        from loom.campaigns.candidate_cli import main as command_main
    elif args.command == "experiment":
        from loom.campaigns.experiment_cli import main as command_main
    elif args.command == "task-set":
        from loom.campaigns.task_set_cli import main as command_main
    elif args.command == "governance":
        from loom.governance.cli import main as command_main
    else:
        from loom.optimize.cli import main as command_main
    return command_main(args.arguments)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
