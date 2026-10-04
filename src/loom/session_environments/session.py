"""Default session environment, independent from tool execution backends."""

from __future__ import annotations

from pathlib import Path

from loom.core import ResourceRef
from loom.runtime.execution_contracts import ResourceClaim
from loom.runtime.plugin_contracts import PluginManifest, json_value


class SessionEnvironment:
    manifest = PluginManifest("session", "session_environment")

    def __init__(self, config, *, connections=None):
        self.config = json_value(config)
        if set(config) - {"resources", "connections"}:
            raise ValueError("Unknown session environment configuration")
        self._resources = []
        self._claims = []
        ids = set()
        for item in config.get("resources", []):
            if set(item) - {"id", "kind", "uri", "access", "access_mode"}:
                raise ValueError("Unknown resource binding field")
            rid, kind, uri = item["id"], item["kind"], item["uri"]
            if not isinstance(kind, str) or not kind or not isinstance(uri, str) or not uri:
                raise ValueError("Resource kind and URI must be nonempty strings")
            if not isinstance(rid, str) or not rid or rid in ids:
                raise ValueError("Resource IDs must be nonempty and unique")
            ids.add(rid)
            if kind == "directory":
                path = Path(uri).expanduser().resolve()
                if not path.is_dir():
                    raise ValueError(f"Directory resource does not exist: {path}")
                uri = str(path)
            access = item.get("access", "read-write")
            if access not in {"read", "read-only", "read-write"}:
                raise ValueError("Unsupported resource access")
            self._resources.append(ResourceRef(rid, kind, uri, access))
            mode = item.get("access_mode", "shared_read" if access in {"read", "read-only"} else "exclusive_write")
            if kind == "directory" and access == "read-write" and mode != "exclusive_write":
                raise ValueError("Writable directories require exclusive resource claims")
            self._claims.append(ResourceClaim(f"{kind}:{uri}", mode))
        self.connection_refs = []
        for item in config.get("connections", []):
            if set(item) != {"id", "config_ref"} or item["id"] in ids:
                raise ValueError("Invalid session connection reference")
            if connections is None or item["config_ref"] not in connections:
                raise ValueError(f"Unavailable connection configuration: {item['config_ref']}")
            ids.add(item["id"])
            self.connection_refs.append(dict(item))

    def open(self):
        return self

    def resources(self):
        return tuple(self._resources)

    def claims(self):
        return tuple(self._claims)

    def snapshot(self):
        return self.manifest.state(self.config, {"resources": [{"id": r.id, "uri": r.uri} for r in self._resources]})

    def restore(self, state):
        value = self.manifest.restore(self.config, state)
        if value != {"resources": [{"id": r.id, "uri": r.uri} for r in self._resources]}:
            raise ValueError("Session resource identity changed")

    def reconnect(self):
        return all(r.kind != "directory" or Path(r.uri).is_dir() for r in self._resources)

    async def close(self):
        pass
