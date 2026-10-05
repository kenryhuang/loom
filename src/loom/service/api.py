"""Authenticated local HTTP commands and durable, independent SSE readers."""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from loom.service.contracts import ServiceError, canonical


def installation_token(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as file:
        file.write(token + "\n")
        file.flush()
        os.fsync(file.fileno())
    return token


class ServiceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service, token, *, web_frontend=True):
        if not token or not all(ipaddress.ip_address(info[4][0]).is_loopback for info in socket.getaddrinfo(address[0], address[1])):
            raise ServiceError("Service requires a token and loopback address")
        self.service = service
        self.token = token
        if web_frontend is True:
            from loom.web.frontend import WebFrontend

            web_frontend = WebFrontend()
        self.web_frontend = web_frontend or None
        self.closing = threading.Event()
        super().__init__(address, SessionHandler)
        from loom.service.trajectory import SessionTrajectory

        self.trajectory = SessionTrajectory(service.store)

    def server_close(self):
        self.closing.set()
        if hasattr(self, "trajectory"):
            self.trajectory.close()
        super().server_close()


class SessionHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _json(self, status, value):
        body = canonical(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ServiceError("Transfer-Encoding is unsupported")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ServiceError("Invalid Content-Length") from exc
        if length > 1_048_576:
            # Drain small excess bodies so clients can read the JSON rejection.
            # Arbitrarily large uploads are rejected without allocating or consuming them.
            if length <= 2_097_152:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            self.close_connection = True
            raise ServiceError("Command body exceeds 1 MiB", 413)
        if length <= 0:
            raise ServiceError("JSON body required")
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ServiceError("Malformed JSON") from exc

    @staticmethod
    def _number(query, key, default, minimum=0, maximum=None):
        raw = query.get(key, [str(default)])
        try:
            if len(raw) != 1:
                raise ValueError
            value = int(raw[0])
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError
            return value
        except ValueError as exc:
            raise ServiceError(f"Invalid {key}") from exc

    def _dispatch(self, method):
        try:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if "token" in query:
                raise ServiceError("Credentials must use Authorization header")
            if method == "GET" and self.server.web_frontend:
                asset = self.server.web_frontend.asset(parsed.path)
                if asset:
                    body, content_type = asset
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header(
                        "Content-Security-Policy",
                        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                        "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                    )
                    self.end_headers()
                    self.wfile.write(body)
                    return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.token):
                self.close_connection = True
                raise ServiceError("Service credential required", 401, "UNAUTHORIZED")
            parts = parsed.path.strip("/").split("/")
            service = self.server.service
            if method == "GET" and parts == ["v1", "web", "catalog"] and self.server.web_frontend:
                self._json(200, self.server.web_frontend.catalog(service))
                return
            if parts == ["v1", "sessions"]:
                if method == "POST":
                    body = self._body()
                    if not isinstance(body, dict) or set(body) != {"command_id", "payload"}:
                        raise ServiceError("Creation requires command_id and payload")
                    self._json(202, service.create(body["command_id"], body["payload"]))
                else:
                    offset = self._number(query, "offset", 0)
                    limit = self._number(query, "limit", 50, 1, 200)
                    states = service.store.list_sessions()
                    summaries = [
                        {"session_id": s["session_id"], "title": s["title"], "task": s["task"], "event_cursor": s["event_cursor"]}
                        for s in states[offset : offset + limit]
                    ]
                    self._json(200, {"sessions": summaries, "next_offset": offset + limit if offset + limit < len(states) else None})
                return
            if len(parts) < 4 or parts[:2] != ["v1", "sessions"]:
                raise ServiceError("Route not found", 404)
            sid, route = parts[2:4]
            if method == "POST" and route == "commands" and len(parts) == 4:
                self._json(202, service.submit(sid, self._body()))
            elif route == "trajectory":
                analysis = self.server.trajectory
                if method == "POST" and len(parts) == 4:
                    if self._body() != {}:
                        raise ServiceError("Trajectory analysis accepts an empty object")
                    self._json(202, analysis.start(sid))
                elif method == "GET" and len(parts) == 5:
                    self._json(200, analysis.get(sid, parts[4]))
                elif method == "GET" and len(parts) == 6 and parts[5] == "round":
                    values = query.get("round_id", [])
                    if len(values) != 1 or not values[0]:
                        raise ServiceError("A single round_id is required")
                    self._json(200, analysis.round(sid, parts[4], values[0]))
                elif method == "GET" and len(parts) == 6 and parts[5] == "evidence":
                    field = query.get("field", [None])
                    if len(field) != 1 or field[0] == "":
                        raise ServiceError("Invalid evidence field")
                    self._json(200, analysis.evidence(sid, parts[4], self._number(query, "line", 0, 1), field[0],
                                                     self._number(query, "start", 0), self._number(query, "limit", 8000, 1, 32000)))
                else:
                    raise ServiceError("Route not found", 404)
            elif method == "GET" and route == "snapshot" and len(parts) == 4:
                self._json(200, service.snapshot(sid))
            elif method == "GET" and route == "processes" and len(parts) == 4:
                self._json(200, {"processes": service.store.processes(sid)})
            elif method == "GET" and route == "history" and len(parts) == 4:
                limit = self._number(query, "limit", 200, 1, 1000)
                after = self._number(query, "after", 0)
                view = query.get("view", ["raw"])
                run_id = query.get("run_id")
                if len(view) != 1 or view[0] not in {"raw", "activity"} or (run_id is not None and (len(run_id) != 1 or not run_id[0])):
                    raise ServiceError("Invalid history view or run_id")
                with service.store.transaction() as db:
                    state = service.store._load(db, sid)
                    before = self._number(query, "before", state["event_cursor"] + 1, 1)
                    clauses, values = ["session_id=?", "seq<?", "seq>?"], [sid, before, after]
                    if run_id:
                        clauses.append("json_extract(body,'$.run_id')=?")
                        values.append(run_id[0])
                    if view[0] == "activity":
                        clauses.append("json_extract(body,'$.type') NOT LIKE 'llm.%.delta'")
                    rows = db.execute(
                        "SELECT body FROM events WHERE " + " AND ".join(clauses) + " ORDER BY seq DESC LIMIT ?", (*values, limit + 1),
                    ).fetchall()
                events = [json.loads(row[0]) for row in reversed(rows[:limit])]
                self._json(200, {"events": events, "next_before": events[0]["seq"] if len(rows) > limit else None})
            elif method == "GET" and route == "events" and len(parts) == 4:
                cursor = self._number(query, "after", 0)
                if self.headers.get("Last-Event-ID"):
                    cursor = max(cursor, self._number({"last": [self.headers["Last-Event-ID"]]}, "last", 0))
                service.events(sid, cursor)  # Validate before sending streaming headers.
                self._sse(sid, cursor)
            elif method == "GET" and route == "artifacts" and len(parts) == 5:
                data = service.store.read_artifact(sid, parts[4])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                raise ServiceError("Route not found", 404)
        except ServiceError as exc:
            self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def _sse(self, sid, cursor):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
            while not self.server.closing.is_set():
                events = self.server.service.events(sid, cursor)
                for event in events:
                    self.wfile.write(f"id: {event['seq']}\nevent: {event['type']}\ndata: {canonical(event)}\n\n".encode())
                    self.wfile.flush()
                    cursor = event["seq"]
                if not events:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    with self.server.service.store.changed:
                        self.server.service.store.changed.wait(0.5)
