"""Persistent connection catalog and immutable session tool bindings."""

import asyncio
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from loom.core import now_iso
from loom.service.contracts import ServiceError, canonical, new_id, text


def schema_check(schema, value=None, *, validate=False):
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError, ValidationError
    except ImportError as exc:
        raise ServiceError('Install external tools support: uv pip install "loom[external-tools]"', 422) from exc

    def local_refs(node):
        if isinstance(node, dict):
            if "$ref" in node and not str(node["$ref"]).startswith("#"):
                raise ServiceError("Tool schemas must use local references", 422)
            for child in node.values():
                local_refs(child)
        elif isinstance(node, list):
            for child in node:
                local_refs(child)

    local_refs(schema)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ServiceError("input_schema must describe a JSON object", 422)
    try:
        Draft202012Validator.check_schema(schema)
        if validate:
            Draft202012Validator(schema).validate(value)
    except (SchemaError, ValidationError) as exc:
        raise ServiceError(f"Invalid tool schema or arguments: {exc.message[:300]}", 422) from exc


def normalize(value):
    if not isinstance(value, dict) or set(value) - {"name", "description", "kind", "config", "enabled"}:
        raise ServiceError("Expected name, description, kind, config and optional enabled")
    kind = value.get("kind")
    if kind not in {"mcp", "cli"}:
        raise ServiceError("kind must be mcp or cli")
    config = value.get("config")
    if not isinstance(config, dict):
        raise ServiceError("config must be an object")
    config = dict(config)
    allowed = {"command", "args", "cwd", "env", "timeout_seconds", "max_output_chars"}
    allowed |= {"transport", "url", "headers_env", "trust_env"} if kind == "mcp" else {"input_mode", "input_schema", "effect_kind"}
    if set(config) - allowed:
        raise ServiceError("Unknown connection configuration fields")
    for key, default, maximum in [("timeout_seconds", 60, 300), ("max_output_chars", 32000, 128000)]:
        value_number = config.setdefault(key, default)
        if type(value_number) is not int or not 1 <= value_number <= maximum:
            raise ServiceError(f"{key} must be between 1 and {maximum}")
    for key in ("env", "headers_env"):
        mapping = config.get(key, {})
        if not isinstance(mapping, dict) or any(
            not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", k) or not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v)
            for k, v in mapping.items()
        ):
            raise ServiceError(f"{key} maps names to environment-variable names, not secret values")
    transport = config.get("transport", "stdio")
    if kind == "mcp":
        config["transport"] = transport
        if type(config.get("trust_env", False)) is not bool:
            raise ServiceError("trust_env must be a boolean")
        if transport not in {"stdio", "streamable_http", "sse"}:
            raise ServiceError("MCP transport must be stdio, streamable_http or sse")
    if kind == "cli" or transport == "stdio":
        text(config.get("command"), "command", max_length=4096)
        if not isinstance(config.get("args", []), list) or any(not isinstance(a, str) or "\0" in a for a in config.get("args", [])):
            raise ServiceError("args must be an array of strings")
    else:
        parsed = urlsplit(text(config.get("url"), "url", max_length=4096))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ServiceError("MCP URL must be HTTP(S) without embedded credentials or fragment")
    if config.get("cwd"):
        directory = Path(text(config["cwd"], "cwd")).expanduser().resolve()
        if not directory.is_dir():
            raise ServiceError("cwd must be an existing directory")
        config["cwd"] = str(directory)
    if kind == "cli":
        if config.setdefault("input_mode", "json_stdin") not in {"json_stdin", "arguments"}:
            raise ServiceError("input_mode must be json_stdin or arguments")
        if config.setdefault("effect_kind", "side_effecting") not in {"read_only", "side_effecting"}:
            raise ServiceError("effect_kind must be read_only or side_effecting")
        schema_check(config.setdefault("input_schema", {"type": "object", "properties": {}, "additionalProperties": False}))
    enabled = value.get("enabled", True)
    if type(enabled) is not bool:
        raise ServiceError("enabled must be a boolean")
    return dict(
        name=text(value.get("name"), "name", max_length=120),
        description=text(value.get("description"), "description", max_length=3000),
        kind=kind,
        config=config,
        enabled=enabled,
    )


class ExternalToolStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "external_tools.sqlite"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def list(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM connections ORDER BY rowid DESC")]

    def get(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT body FROM connections WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ServiceError("External connection not found", 404)
        return json.loads(row[0])

    def save(self, value, identifier=None):
        body = normalize(value)
        old = self.get(identifier) if identifier else None
        body.update(id=identifier or new_id("external"), updated_at=now_iso(), tools=[])
        if body["kind"] == "cli":
            body["tools"] = self.tools(
                body,
                [
                    {
                        "name": body["name"],
                        "description": body["description"],
                        "inputSchema": body["config"]["input_schema"],
                        "annotations": {"readOnlyHint": body["config"]["effect_kind"] == "read_only"},
                    }
                ],
            )
        elif old and old["kind"] == body["kind"] and old["config"] == body["config"]:
            body["tools"] = self.tools(
                body,
                [
                    {
                        "name": tool["name"],
                        "description": tool["description"],
                        "inputSchema": tool["input_schema"],
                        "annotations": {"readOnlyHint": tool["effect_kind"] == "read_only"},
                    }
                    for tool in old["tools"]
                ],
            )
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO connections VALUES (?,?)", (body["id"], canonical(body)))
        return body

    def tools(self, body, rows):
        tools, names = [], set()
        for row in rows:
            name = text(row.get("name"), "tool name", max_length=200)
            if name in names:
                raise ServiceError("MCP returned duplicate tool names", 502)
            names.add(name)
            schema = row.get("inputSchema", {"type": "object", "properties": {}})
            schema_check(schema)
            identity = hashlib.sha256((body["id"] + ":" + name).encode()).hexdigest()[:24]
            definition = dict(
                id=f"ext_{identity}",
                name=name,
                description=(row.get("description") or name)[:12000],
                input_schema=schema,
                effect_kind="read_only" if (row.get("annotations") or {}).get("readOnlyHint") is True else "side_effecting",
                connector_id=body["id"],
                connector_name=body["name"],
                kind=body["kind"],
            )
            definition["version"] = hashlib.sha256(canonical([body["config"], definition]).encode()).hexdigest()
            tools.append(definition)
        return tools

    def discover(self, identifier):
        from loom.external_tools.transport import discover

        body = self.get(identifier)
        if body["kind"] == "cli":
            return body
        try:
            rows = asyncio.run(discover(body["config"]))
        except ServiceError:
            raise
        except Exception as exc:
            from loom.external_tools.errors import external_error

            raise external_error(exc, "MCP discovery") from exc
        body["tools"] = self.tools(body, rows)
        body["updated_at"] = now_iso()
        with self.connect() as db:
            current = json.loads(db.execute("SELECT body FROM connections WHERE id=?", (identifier,)).fetchone()[0])
            if any(current[key] != body[key] for key in ("config", "kind", "name", "description")):
                raise ServiceError("Connection changed during discovery; retry", 409)
            body["enabled"] = current["enabled"]
            db.execute("UPDATE connections SET body=? WHERE id=?", (canonical(body), identifier))
        return body

    def catalog(self):
        return [tool for body in self.list() if body["enabled"] for tool in body["tools"]]

    def resolve(self, ids):
        if not isinstance(ids, list) or len(ids) > 64 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise ServiceError("external_tool_ids must be a unique list of at most 64 IDs")
        catalog = {tool["id"]: tool for tool in self.catalog()}
        if any(i not in catalog for i in ids):
            raise ServiceError("External tool is unknown, disabled or not discovered", 422)
        return [catalog[i] for i in ids]

    def connection_for(self, binding):
        current = self.resolve([binding["id"]])[0]
        if current != binding:
            raise ServiceError("External tool changed; rebind tools between tasks before running", 409)
        return self.get(binding["connector_id"])
