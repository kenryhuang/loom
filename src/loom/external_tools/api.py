"""Bearer-authenticated registration, discovery and enable/disable endpoints."""

from loom.service.contracts import ServiceError


def dispatch(handler, method, parts):
    store = handler.server.service.store.external_tools
    tail = parts[2:]
    if not tail:
        if method == "GET":
            return 200, {"connections": store.list(), "tools": store.catalog()}
        if method == "POST":
            return 201, store.save(handler._body())
    if len(tail) == 1:
        if method == "GET":
            return 200, store.get(tail[0])
        if method == "POST":
            return 200, store.save(handler._body(), tail[0])
    if len(tail) == 2 and tail[1] == "discover" and method == "POST":
        if handler._body() != {}:
            raise ServiceError("Discover expects an empty object")
        return 200, store.discover(tail[0])
    raise ServiceError("External tools route not found", 404)
