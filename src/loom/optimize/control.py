"""Cooperative, durable control requests for optimization runs."""

from __future__ import annotations

from dataclasses import dataclass

from loom.core import Result, err, make_loom_error, ok


@dataclass(slots=True)
class OptimizeRunControl:
    _pause_reason: str | None = None
    _cancel_reason: str | None = None

    def request_pause(self, reason: str = "user requested pause") -> None:
        self._pause_reason = reason.strip() or "user requested pause"

    def request_cancel(self, reason: str = "user cancelled active work") -> None:
        self._cancel_reason = reason.strip() or "user cancelled active work"

    @property
    def pause_requested(self) -> bool:
        return self._pause_reason is not None

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_reason is not None

    async def checkpoint(self, store, optimization_id: str) -> Result:
        reason = self._cancel_reason or self._pause_reason
        if reason is None:
            return ok(None)
        paused = await store.pause(optimization_id, reason)
        if not paused.ok:
            return paused
        return err(
            make_loom_error(
                "OPTIMIZATION_PAUSED",
                "Optimization paused at a durable checkpoint",
                retryable=True,
                metadata={"reason": reason, "cancel_requested": self._cancel_reason is not None},
            )
        )


__all__ = ["OptimizeRunControl"]
