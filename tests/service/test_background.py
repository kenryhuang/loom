from loom.service.api import ServiceHTTPServer
from loom.service.controller import LoomService


def test_background_shared_without_http_lifecycle(tmp_path):
    service = LoomService(tmp_path)
    try:
        background = service.background
        first = ServiceHTTPServer(("127.0.0.1", 0), service, "test")
        first.server_close()
        second = ServiceHTTPServer(("127.0.0.1", 0), service, "test")
        try:
            assert first.semantic is second.semantic is background.semantic
            assert not background.semantic.closed.is_set()
            background.configure(None)
        finally:
            second.server_close()
    finally:
        service.close()
    assert background.closed
    service.close()
