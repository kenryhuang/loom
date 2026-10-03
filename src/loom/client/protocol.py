"""HTTP/SSE client; reconnects are driven by durable session cursors."""

from __future__ import annotations

import json
import uuid
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from loom.service.contracts import ServiceError, canonical


class SessionClient:
    def __init__(self, url, token, *, timeout=10):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ServiceError("Invalid service URL")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _open(self, path, value=None, headers=None):
        request = Request(
            self.url + path,
            data=canonical(value).encode() if value is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json", **(headers or {})},
        )
        try:
            return urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            try:
                error = json.loads(exc.read())["error"]
            except (ValueError, KeyError):
                error = {"message": exc.reason, "code": "HTTP_ERROR"}
            raise ServiceError(error["message"], exc.code, error["code"]) from exc

    def _json(self, path, value=None):
        with self._open(path, value) as response:
            return json.load(response)

    @staticmethod
    def _path(sid, route):
        return f"/v1/sessions/{quote(sid, safe='')}/{route}"

    def create(self, payload, *, command_id=None):
        return self._json("/v1/sessions", {"command_id": command_id or uuid.uuid4().hex, "payload": payload})

    def list_sessions(self, *, offset=0, limit=50):
        return self._json("/v1/sessions?" + urlencode({"offset": offset, "limit": limit}))["sessions"]

    def command(self, sid, kind, payload=None, *, command_id=None, expected_task_revision=None):
        value = {"command_id": command_id or uuid.uuid4().hex, "type": kind, "payload": payload or {}}
        if expected_task_revision is not None:
            value["expected_task_revision"] = expected_task_revision
        try:
            return self._json(self._path(sid, "commands"), value)
        except ServiceError as exc:
            if kind == "set_token_budget" and exc.status == 400 and str(exc) == "Unknown command type":
                raise ServiceError("Backend does not support token budget changes. Restart loom serve with the same --data-dir.", exc.status, exc.code) from exc
            raise

    def snapshot(self, sid):
        return self._json(self._path(sid, "snapshot"))

    def history(self, sid, *, before=None, limit=200):
        query = {"limit": limit}
        if before is not None:
            query["before"] = before
        return self._json(self._path(sid, "history") + "?" + urlencode(query))

    def artifact(self, sid, digest):
        with self._open(self._path(sid, "artifacts/" + quote(digest, safe=""))) as response:
            return response.read()

    def events(self, sid, after=0, *, last_event_id=None, stop=None):
        headers = {"Last-Event-ID": str(last_event_id)} if last_event_id is not None else {}
        with self._open(self._path(sid, "events") + "?" + urlencode({"after": after}), headers=headers) as response:
            data = []
            for raw in response:
                if stop and stop.is_set():
                    return
                line = raw.decode().rstrip("\r\n")
                if not line:
                    if data:
                        yield json.loads("\n".join(data))
                        data.clear()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
