"""Local service entry point with a persistent private installation credential."""

import argparse
import signal
import threading
from pathlib import Path

from loom.service.api import ServiceHTTPServer, installation_token
from loom.service.controller import LoomService


def main(argv=None):
    parser = argparse.ArgumentParser(prog="loom serve")
    parser.add_argument("--data-dir", type=Path, default=Path(".loom/service"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-active-runs", type=int, default=2)
    args = parser.parse_args(argv)
    service = LoomService(args.data_dir, config_path=args.config, max_active_runs=args.max_active_runs)
    server = ServiceHTTPServer((args.host, args.port), service, installation_token(args.data_dir / "credential"))
    service.start()
    previous = {}

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, shutdown)
    print(f"Loom service: http://{args.host}:{server.server_port}; credential file: {(args.data_dir / 'credential').resolve()}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0
