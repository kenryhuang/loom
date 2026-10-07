"""Versioned, explicitly registered execution plugins (distinct from observers)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


def json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        value = {key: json_value(item) for key, item in value.items()}
    elif isinstance(value, list | tuple):
        value = [json_value(item) for item in value]
    return json.loads(json.dumps(value, allow_nan=False, sort_keys=True))


def config_digest(value: Mapping) -> str:
    return hashlib.sha256(json.dumps(json_value(value), allow_nan=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    kind: str
    version: str = "1"
    state_version: int = 1
    capabilities: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()

    def state(self, config: Mapping, value: Mapping) -> dict:
        return json_value(
            {"plugin_id": self.plugin_id, "version": self.version, "schema_version": self.state_version, "config_digest": config_digest(config), "state": value}
        )

    def restore(self, config: Mapping, snapshot: Mapping) -> dict:
        expected = self.state(config, {})
        if any(snapshot.get(key) != expected[key] for key in ("plugin_id", "version", "schema_version", "config_digest")):
            raise ValueError(f"Incompatible {self.kind} plugin checkpoint: {self.plugin_id}")
        if not isinstance(snapshot.get("state"), Mapping):
            raise ValueError("Plugin checkpoint state must be an object")
        return json_value(snapshot["state"])


class ExecutionPlugin(Protocol):
    manifest: PluginManifest

    def snapshot(self) -> dict: ...

    def restore(self, state: Mapping) -> None: ...


class PluginRegistry:
    """Factories are registered by trusted code, never imported from session data."""

    def __init__(self):
        self._factories: dict[tuple[str, str, str], Callable] = {}

    def register(self, kind: str, plugin_id: str, factory: Callable, *, version="1") -> None:
        key = (kind, plugin_id, version)
        if key in self._factories:
            raise ValueError(f"Duplicate plugin: {kind}/{plugin_id}")
        self._factories[key] = factory

    def catalog(self, kind: str) -> list[dict[str, str]]:
        """Describe registered identities without instantiating or executing plugins."""
        return [{"id": plugin_id, "version": version} for registered_kind, plugin_id, version in sorted(self._factories) if registered_kind == kind]

    def create(self, kind: str, spec: Mapping, **services):
        spec = json_value(dict(spec))
        plugin_id = spec.pop("plugin", None)
        version = spec.pop("version", "1")
        factory = self._factories.get((kind, plugin_id, version))
        if factory is None:
            raise ValueError(f"Unavailable {kind} plugin: {plugin_id}; no execution fallback is allowed")
        plugin = factory(spec, **services)
        if plugin.manifest.kind != kind or plugin.manifest.plugin_id != plugin_id or plugin.manifest.version != version:
            raise ValueError("Plugin manifest does not match its registered identity")
        return plugin
