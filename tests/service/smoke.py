"""Importable fake-model daemon for process-level protocol smoke tests."""

import argparse
import json
import signal
import threading

from loom.service.api import ServiceHTTPServer, installation_token
from loom.service.controller import LoomService
from tests.service.fakes import provider_factory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    service = LoomService(args.data_dir, provider_factory=provider_factory).start()
    server = ServiceHTTPServer(("127.0.0.1", 0), service, installation_token(service.store.directory / "credential"))

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}"}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
