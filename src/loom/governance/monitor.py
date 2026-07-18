"""Version-bound post-promotion monitoring."""

from __future__ import annotations


class MonitoringService:
    def __init__(self, store, actor):
        self.store = store
        self.actor = actor

    async def record(self, monitor_id: str, observed_version: int, *, quality_delta: float, resources=None):
        return await self.store.record_monitor_sample(monitor_id, observed_version, quality_delta, self.actor, resources)


__all__ = ["MonitoringService"]
