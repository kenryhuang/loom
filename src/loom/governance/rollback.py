"""Manual version-bound rollback orchestration."""

from __future__ import annotations

from loom.governance.policy import GovernanceOperation


class RollbackController:
    def __init__(self, store):
        self.store = store

    async def rollback(self, surface_id: str, *, expected_active_version: int, actor, reason: str, operation_id: str):
        lease = await self.store.acquire_lease(
            GovernanceOperation(operation_id, surface_id, actor, "rollback"),
            expected_active_version,
        )
        if not lease.ok:
            return lease
        return await self.store.rollback(lease.value, reason=reason)


__all__ = ["RollbackController"]
