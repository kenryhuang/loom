"""Local service entry point with a persistent private installation credential."""

import argparse
import signal
import threading
from pathlib import Path

from loom.service.api import ServiceHTTPServer, installation_token
from loom.service.controller import LoomService
from loom.tasks.config import discover_default_config_path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="loom serve")
    parser.add_argument("--data-dir", type=Path, default=Path(".loom/service"))
    parser.add_argument("--config", type=Path, help="Model configuration; defaults to ./config.yaml or ./config.yml when present.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-active-runs", type=int, default=2)
    parser.add_argument("--no-web", action="store_true", help="Disable the bundled browser frontend.")
    parser.add_argument("--enable-agents", action="store_true", help="Enable background autonomous Agent orchestration and learning.")
    parser.add_argument("--no-auth", action="store_true", help="Skip the credential check (local development only; the service stays loopback-only).")
    args = parser.parse_args(argv)
    config_path = args.config or discover_default_config_path()
    service = LoomService(args.data_dir, config_path=config_path, max_active_runs=args.max_active_runs, autonomy_enabled=args.enable_agents)
    server = ServiceHTTPServer(
        (args.host, args.port), service, installation_token(args.data_dir / "credential"), web_frontend=not args.no_web, require_auth=not args.no_auth
    )
    service.start()
    previous = {}

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, shutdown)
    print(f"Loom service: http://{args.host}:{server.server_port}; credential file: {(args.data_dir / 'credential').resolve()}", flush=True)
    if args.no_auth:
        print("WARNING: --no-auth disables the credential check; intended for local development only.", flush=True)
    if not args.no_web:
        print(f"Web frontend: http://{args.host}:{server.server_port}/web/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0
