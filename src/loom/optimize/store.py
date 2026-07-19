"""Transactional orchestration store for one-command optimization."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loom.campaigns.serialization import canonical_json_bytes, utc_now
from loom.core import FrozenDict, Result, err, freeze_json, make_loom_error, ok, thaw_json
from loom.optimize.contracts import OptimizationLifecycle, OptimizationSpec, OptimizationStage, OptimizationState
from loom.optimize.events import OptimizationSnapshot


@dataclass(frozen=True, slots=True)
class StageLease:
    lease_id: str
    operation_id: str
    target_stage: OptimizationStage
    aggregate_version: int
    replayed: bool = False
    outputs: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        outputs = freeze_json(self.outputs)
        if not isinstance(outputs, FrozenDict):
            raise TypeError("stage outputs must be a mapping")
        object.__setattr__(self, "outputs", outputs)


@dataclass(frozen=True, slots=True)
class StageCompletion:
    operation_id: str
    target_stage: OptimizationStage
    aggregate_version: int
    outputs: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        outputs = freeze_json(self.outputs)
        if not isinstance(outputs, FrozenDict):
            raise TypeError("stage outputs must be a mapping")
        object.__setattr__(self, "outputs", outputs)


_NEXT_STAGES = {
    OptimizationStage.CREATED: frozenset({OptimizationStage.PREFLIGHT_COMPLETE}),
    OptimizationStage.PREFLIGHT_COMPLETE: frozenset({OptimizationStage.SEED_ANALYSIS_COMPLETE}),
    OptimizationStage.SEED_ANALYSIS_COMPLETE: frozenset({OptimizationStage.CAMPAIGN_INITIALIZED}),
    OptimizationStage.CAMPAIGN_INITIALIZED: frozenset({OptimizationStage.SEARCH_RUNNING}),
    OptimizationStage.SEARCH_RUNNING: frozenset({OptimizationStage.SEARCH_RUNNING, OptimizationStage.SEARCH_SEALED}),
    OptimizationStage.SEARCH_SEALED: frozenset({OptimizationStage.VALIDATION_COMPLETE}),
    OptimizationStage.VALIDATION_COMPLETE: frozenset({OptimizationStage.FINALISTS_SELECTED}),
    OptimizationStage.FINALISTS_SELECTED: frozenset({OptimizationStage.HOLDOUT_COMPLETE}),
    OptimizationStage.HOLDOUT_COMPLETE: frozenset({OptimizationStage.GOVERNANCE_COMPLETE}),
    OptimizationStage.GOVERNANCE_COMPLETE: frozenset(),
}
_TERMINAL = frozenset(
    {
        OptimizationLifecycle.PROMOTED,
        OptimizationLifecycle.REJECTED,
        OptimizationLifecycle.AWAITING_APPROVAL,
        OptimizationLifecycle.FAILED,
    }
)


class SQLiteOptimizationStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "optimization.sqlite"
        self._initialize()

    async def create(self, spec: OptimizationSpec) -> Result:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT optimization_id, spec_json FROM manifest WHERE singleton = 1").fetchone()
            spec_payload = _spec_payload(spec)
            if existing is not None:
                if existing[0] != spec.optimization_id or json.loads(existing[1]) != spec_payload:
                    connection.rollback()
                    return _store_error("OPTIMIZATION_CONFLICT", "optimization store already contains a different specification")
                connection.rollback()
                return await self.load(spec.optimization_id)
            connection.execute(
                "INSERT INTO manifest(singleton, optimization_id, optimization_key, spec_json) VALUES (1, ?, ?, ?)",
                (spec.optimization_id, spec.optimization_key, canonical_json_bytes(spec_payload).decode()),
            )
            self._append_event(
                connection,
                spec.optimization_id,
                "optimization.created",
                OptimizationStage.CREATED,
                OptimizationLifecycle.RUNNING,
                None,
                {"spec": spec_payload},
            )
            connection.commit()
            return ok(self._state_from_connection(connection, spec.optimization_id).unwrap())
        except sqlite3.Error as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def begin(
        self,
        optimization_id: str,
        operation_id: str,
        input_digest: str,
        target_stage: OptimizationStage,
        *,
        lease_seconds: int = 300,
    ) -> Result:
        if lease_seconds < 0:
            return _store_error("OPTIMIZATION_LEASE_INVALID", "lease duration cannot be negative")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute(
                "SELECT input_digest, status, lease_id, result_json, target_stage FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if operation is not None:
                if operation[0] != input_digest or operation[4] != target_stage.value:
                    connection.rollback()
                    return _store_error("OPERATION_CONFLICT", "operation ID was reused with different input")
                if operation[1] == "completed":
                    payload = json.loads(operation[3])
                    connection.rollback()
                    return ok(
                        StageLease(
                            operation[2],
                            operation_id,
                            target_stage,
                            int(payload["aggregate_version"]),
                            True,
                            payload["outputs"],
                        )
                    )
                if operation[1] == "started":
                    lease = connection.execute("SELECT expires_at, status FROM leases WHERE lease_id = ?", (operation[2],)).fetchone()
                    if lease is not None and lease[1] == "active" and _parse_time(lease[0]) > datetime.now(UTC):
                        connection.rollback()
                        return _store_error(
                            "OPTIMIZATION_OPERATION_IN_PROGRESS",
                            "stage operation already has an active lease",
                            operation_id=operation_id,
                        )
                    connection.rollback()
                    return _store_error("OPTIMIZATION_LEASE_EXPIRED", "operation lease must be reconciled before retry")
            state = self._state_from_connection(connection, optimization_id)
            if not state.ok:
                connection.rollback()
                return state
            if state.value.lifecycle is not OptimizationLifecycle.RUNNING:
                connection.rollback()
                return _store_error("OPTIMIZATION_TRANSITION_INVALID", "only a running optimization can start a stage")
            if target_stage not in _NEXT_STAGES[state.value.stage]:
                connection.rollback()
                return _store_error(
                    "OPTIMIZATION_TRANSITION_INVALID",
                    "optimization stage transition is invalid",
                    current=state.value.stage.value,
                    target=target_stage.value,
                )
            lease_id = _identifier("lease", {"operation_id": operation_id, "input_digest": input_digest, "at": utc_now()})
            expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            if operation is None:
                connection.execute(
                    "INSERT INTO operations(operation_id, input_digest, target_stage, status, lease_id, result_json) VALUES (?, ?, ?, 'started', ?, NULL)",
                    (operation_id, input_digest, target_stage.value, lease_id),
                )
            else:
                connection.execute(
                    "UPDATE operations SET status = 'started', lease_id = ?, result_json = NULL WHERE operation_id = ?",
                    (lease_id, operation_id),
                )
            connection.execute(
                "INSERT INTO leases(lease_id, operation_id, expires_at, status) VALUES (?, ?, ?, 'active')",
                (lease_id, operation_id, expires_at),
            )
            version = self._append_event(
                connection,
                optimization_id,
                "optimization.stage_started",
                state.value.stage,
                state.value.lifecycle,
                operation_id,
                {"target_stage": target_stage.value, "input_digest": input_digest, "lease_id": lease_id},
            )
            connection.commit()
            return ok(StageLease(lease_id, operation_id, target_stage, version))
        except (sqlite3.Error, ValueError) as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def complete(
        self,
        lease_id: str,
        outputs: Mapping[str, Any],
        *,
        lifecycle: OptimizationLifecycle | None = None,
    ) -> Result:
        try:
            output_value = json.loads(canonical_json_bytes(outputs))
        except (TypeError, ValueError) as exc:
            return _store_error("OPTIMIZATION_OUTPUT_INVALID", "stage output is not canonical JSON", cause=exc)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT l.operation_id, l.expires_at, l.status, o.target_stage, o.status "
                "FROM leases l JOIN operations o USING (operation_id) WHERE l.lease_id = ?",
                (lease_id,),
            ).fetchone()
            if lease is None or lease[2] != "active" or lease[4] != "started":
                connection.rollback()
                return _store_error("OPTIMIZATION_LEASE_INVALID", "stage lease is not active")
            if _parse_time(lease[1]) < datetime.now(UTC):
                connection.rollback()
                return _store_error("OPTIMIZATION_LEASE_EXPIRED", "stage lease has expired")
            optimization_id = self._optimization_id(connection)
            state = self._state_from_connection(connection, optimization_id).unwrap()
            target_stage = OptimizationStage(lease[3])
            next_lifecycle = lifecycle or state.lifecycle
            if lifecycle is not None and lifecycle not in _TERMINAL:
                connection.rollback()
                return _store_error("OPTIMIZATION_TRANSITION_INVALID", "completed lifecycle is not terminal")
            version = self._append_event(
                connection,
                optimization_id,
                "optimization.stage_completed",
                target_stage,
                next_lifecycle,
                lease[0],
                {"target_stage": target_stage.value, "outputs": output_value},
            )
            result_payload = {"aggregate_version": version, "outputs": output_value}
            connection.execute("UPDATE leases SET status = 'completed' WHERE lease_id = ?", (lease_id,))
            connection.execute(
                "UPDATE operations SET status = 'completed', result_json = ? WHERE operation_id = ?",
                (canonical_json_bytes(result_payload).decode(), lease[0]),
            )
            connection.commit()
            return ok(StageCompletion(lease[0], target_stage, version, output_value))
        except (sqlite3.Error, ValueError) as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def reconcile_expired(self, optimization_id: str) -> Result:
        connection = self._connect()
        expired: list[str] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC)
            rows = connection.execute("SELECT lease_id, operation_id, expires_at FROM leases WHERE status = 'active'").fetchall()
            for lease_id, operation_id, expires_at in rows:
                if _parse_time(expires_at) > now:
                    continue
                expired.append(lease_id)
                connection.execute("UPDATE leases SET status = 'expired' WHERE lease_id = ?", (lease_id,))
                connection.execute("UPDATE operations SET status = 'cancelled' WHERE operation_id = ?", (operation_id,))
                state = self._state_from_connection(connection, optimization_id).unwrap()
                self._append_event(
                    connection,
                    optimization_id,
                    "optimization.lease_expired",
                    state.stage,
                    state.lifecycle,
                    operation_id,
                    {"lease_id": lease_id},
                )
            connection.commit()
            return ok(tuple(expired))
        except (sqlite3.Error, ValueError) as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def cancel(self, lease_id: str, *, reason: str) -> Result:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT l.operation_id, l.status, o.status FROM leases l JOIN operations o USING (operation_id) WHERE l.lease_id = ?",
                (lease_id,),
            ).fetchone()
            if lease is None:
                connection.rollback()
                return _store_error("OPTIMIZATION_LEASE_INVALID", "stage lease was not found")
            if lease[1] == "cancelled" and lease[2] == "cancelled":
                connection.rollback()
                return ok(lease_id)
            if lease[1] != "active" or lease[2] != "started":
                connection.rollback()
                return _store_error("OPTIMIZATION_LEASE_INVALID", "only an active stage lease can be cancelled")
            optimization_id = self._optimization_id(connection)
            state = self._state_from_connection(connection, optimization_id).unwrap()
            connection.execute("UPDATE leases SET status = 'cancelled' WHERE lease_id = ?", (lease_id,))
            connection.execute("UPDATE operations SET status = 'cancelled' WHERE operation_id = ?", (lease[0],))
            self._append_event(
                connection,
                optimization_id,
                "optimization.stage_cancelled",
                state.stage,
                state.lifecycle,
                lease[0],
                {"lease_id": lease_id, "reason": reason},
            )
            connection.commit()
            return ok(lease_id)
        except (sqlite3.Error, ValueError) as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def pause(self, optimization_id: str, reason: str) -> Result:
        return await self._lifecycle_event(optimization_id, "optimization.paused", OptimizationLifecycle.PAUSED, {"reason": reason})

    async def resume(self, optimization_id: str) -> Result:
        state = await self.load(optimization_id)
        if not state.ok:
            return state
        if state.value.lifecycle is not OptimizationLifecycle.PAUSED:
            return _store_error("OPTIMIZATION_TRANSITION_INVALID", "only a paused optimization can resume")
        return await self._lifecycle_event(optimization_id, "optimization.resumed", OptimizationLifecycle.RUNNING, {})

    async def fail(self, optimization_id: str, failure: Mapping[str, Any]) -> Result:
        return await self._lifecycle_event(optimization_id, "optimization.failed", OptimizationLifecycle.FAILED, {"failure": failure})

    async def complete_approval(self, optimization_id: str, governance_output: Mapping[str, Any]) -> Result:
        try:
            output = json.loads(canonical_json_bytes(governance_output))
        except (TypeError, ValueError) as exc:
            return _store_error("OPTIMIZATION_OUTPUT_INVALID", "approval output is not canonical JSON", cause=exc)
        if output.get("disposition") != "promoted":
            return _store_error("OPTIMIZATION_TRANSITION_INVALID", "explicit approval must produce a promoted governance output")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state_from_connection(connection, optimization_id)
            if not state.ok:
                connection.rollback()
                return state
            if state.value.lifecycle is not OptimizationLifecycle.AWAITING_APPROVAL or state.value.stage is not OptimizationStage.GOVERNANCE_COMPLETE:
                connection.rollback()
                return _store_error("OPTIMIZATION_TRANSITION_INVALID", "optimization is not awaiting governance approval")
            operation = connection.execute(
                "SELECT operation_id FROM operations WHERE target_stage = ? AND status = 'completed' ORDER BY rowid DESC LIMIT 1",
                (OptimizationStage.GOVERNANCE_COMPLETE.value,),
            ).fetchone()
            if operation is None:
                connection.rollback()
                return _store_error("OPTIMIZATION_TRANSITION_INVALID", "governance operation is unavailable")
            outputs = {"stage.governance": output}
            version = self._append_event(
                connection,
                optimization_id,
                "optimization.approval_completed",
                OptimizationStage.GOVERNANCE_COMPLETE,
                OptimizationLifecycle.PROMOTED,
                operation[0],
                {"outputs": outputs},
            )
            connection.execute(
                "UPDATE operations SET result_json = ? WHERE operation_id = ?",
                (canonical_json_bytes({"aggregate_version": version, "outputs": outputs}).decode(), operation[0]),
            )
            connection.commit()
            return ok(self._state_from_connection(connection, optimization_id).unwrap())
        except (sqlite3.Error, ValueError) as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def _lifecycle_event(
        self,
        optimization_id: str,
        event_type: str,
        lifecycle: OptimizationLifecycle,
        payload: Mapping[str, Any],
    ) -> Result:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state_from_connection(connection, optimization_id)
            if not state.ok:
                connection.rollback()
                return state
            if event_type == "optimization.paused" and state.value.lifecycle is not OptimizationLifecycle.RUNNING:
                connection.rollback()
                return _store_error("OPTIMIZATION_TRANSITION_INVALID", "only a running optimization can pause")
            self._append_event(connection, optimization_id, event_type, state.value.stage, lifecycle, None, payload)
            connection.commit()
            return ok(self._state_from_connection(connection, optimization_id).unwrap())
        except sqlite3.Error as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def load(self, optimization_id: str) -> Result:
        connection = self._connect()
        try:
            return self._state_from_connection(connection, optimization_id)
        finally:
            connection.close()

    async def snapshot(
        self,
        optimization_id: str,
        *,
        campaign_id: str | None = None,
        budgets: Mapping[str, Any] | None = None,
    ) -> Result:
        connection = self._connect()
        try:
            state = self._state_from_connection(connection, optimization_id)
            if not state.ok:
                return state
            rows = connection.execute(
                "SELECT o.operation_id, o.target_stage, o.status, o.lease_id, l.status "
                "FROM operations o LEFT JOIN leases l ON l.lease_id = o.lease_id ORDER BY o.rowid"
            ).fetchall()
            operations = tuple(
                {
                    "operation_id": operation_id,
                    "target_stage": target_stage,
                    "status": status,
                    "lease_id": lease_id,
                    "lease_status": lease_status,
                }
                for operation_id, target_stage, status, lease_id, lease_status in rows
            )
            return ok(
                OptimizationSnapshot(
                    optimization_id,
                    campaign_id,
                    state.value.stage.value,
                    state.value.lifecycle.value,
                    state.value.aggregate_version,
                    operations,
                    {} if budgets is None else budgets,
                )
            )
        finally:
            connection.close()

    async def events(self, optimization_id: str) -> Result:
        connection = self._connect()
        try:
            manifest = connection.execute("SELECT optimization_id FROM manifest WHERE singleton = 1").fetchone()
            if manifest is None or manifest[0] != optimization_id:
                return _store_error("OPTIMIZATION_NOT_FOUND", "optimization was not found")
            rows = connection.execute(
                "SELECT sequence, aggregate_version, event_type, stage, lifecycle, operation_id, created_at, payload_json, previous_hash, event_hash "
                "FROM events ORDER BY sequence"
            ).fetchall()
            events = []
            previous = ""
            for row in rows:
                event = {
                    "sequence": row[0],
                    "aggregate_version": row[1],
                    "event_type": row[2],
                    "stage": row[3],
                    "lifecycle": row[4],
                    "operation_id": row[5],
                    "created_at": row[6],
                    "payload": json.loads(row[7]),
                    "previous_hash": row[8],
                    "event_hash": row[9],
                }
                expected = _event_hash({key: value for key, value in event.items() if key != "event_hash"})
                if row[8] != previous or row[9] != expected:
                    return _store_error("OPTIMIZATION_INTEGRITY_FAILED", "optimization event hash chain is invalid")
                previous = row[9]
                events.append(event)
            return ok(tuple(events))
        except (json.JSONDecodeError, sqlite3.Error) as exc:
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def export(self, optimization_id: str) -> Result:
        state = await self.load(optimization_id)
        if not state.ok:
            return state
        events = await self.events(optimization_id)
        if not events.ok:
            return events
        optimization_path = self.root / "optimization.json"
        events_path = self.root / "events.jsonl"
        try:
            _atomic_write(optimization_path, canonical_json_bytes(_state_payload(state.value)) + b"\n")
            _atomic_write(events_path, b"".join(canonical_json_bytes(event) + b"\n" for event in events.value))
            return ok({"optimization": optimization_path, "events": events_path})
        except OSError as exc:
            return _store_error("OPTIMIZATION_EXPORT_FAILED", "could not export optimization state", cause=exc)

    def _state_from_connection(self, connection: sqlite3.Connection, optimization_id: str) -> Result:
        manifest = connection.execute("SELECT optimization_id FROM manifest WHERE singleton = 1").fetchone()
        if manifest is None or manifest[0] != optimization_id:
            return _store_error("OPTIMIZATION_NOT_FOUND", "optimization was not found")
        rows = connection.execute("SELECT aggregate_version, event_type, stage, lifecycle, operation_id, payload_json FROM events ORDER BY sequence").fetchall()
        stage = OptimizationStage.CREATED
        lifecycle = OptimizationLifecycle.RUNNING
        completed: list[str] = []
        outputs: dict[str, Any] = {}
        failure = None
        version = 0
        for row in rows:
            version = row[0]
            event_type = row[1]
            if event_type == "optimization.stage_completed":
                stage = OptimizationStage(row[2])
                if row[4] is not None:
                    completed.append(row[4])
                payload = json.loads(row[5])
                outputs.update(payload.get("outputs", {}))
            elif event_type == "optimization.approval_completed":
                payload = json.loads(row[5])
                outputs.update(payload.get("outputs", {}))
            lifecycle = OptimizationLifecycle(row[3])
            if event_type == "optimization.failed":
                failure = json.loads(row[5]).get("failure")
        return ok(OptimizationState(optimization_id, lifecycle, stage, version, tuple(completed), outputs, failure))

    def _append_event(
        self,
        connection: sqlite3.Connection,
        optimization_id: str,
        event_type: str,
        stage: OptimizationStage,
        lifecycle: OptimizationLifecycle,
        operation_id: str | None,
        payload: Mapping[str, Any],
    ) -> int:
        version = self._aggregate_version(connection) + 1
        previous = connection.execute("SELECT event_hash FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
        previous_hash = "" if previous is None else previous[0]
        created_at = utc_now()
        payload_value = json.loads(canonical_json_bytes(payload))
        event_without_hash = {
            "sequence": version,
            "aggregate_version": version,
            "event_type": event_type,
            "stage": stage.value,
            "lifecycle": lifecycle.value,
            "operation_id": operation_id,
            "created_at": created_at,
            "payload": payload_value,
            "previous_hash": previous_hash,
        }
        event_hash = _event_hash(event_without_hash)
        connection.execute(
            "INSERT INTO events(sequence, aggregate_version, event_type, stage, lifecycle, operation_id, created_at, payload_json, previous_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version,
                version,
                event_type,
                stage.value,
                lifecycle.value,
                operation_id,
                created_at,
                canonical_json_bytes(payload_value).decode(),
                previous_hash,
                event_hash,
            ),
        )
        return version

    @staticmethod
    def _aggregate_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT COALESCE(MAX(aggregate_version), 0) FROM events").fetchone()
        return int(row[0])

    @staticmethod
    def _optimization_id(connection: sqlite3.Connection) -> str:
        row = connection.execute("SELECT optimization_id FROM manifest WHERE singleton = 1").fetchone()
        if row is None:
            raise ValueError("optimization manifest is missing")
        return str(row[0])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS manifest (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    optimization_id TEXT NOT NULL,
                    optimization_key TEXT NOT NULL,
                    spec_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY,
                    aggregate_version INTEGER NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    operation_id TEXT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    input_digest TEXT NOT NULL,
                    target_stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lease_id TEXT,
                    result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS leases (
                    lease_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES operations(operation_id)
                );
                """
            )
            connection.commit()
        finally:
            connection.close()


def _spec_payload(spec: OptimizationSpec) -> dict[str, Any]:
    return {
        "schema_version": spec.schema_version,
        "optimization_id": spec.optimization_id,
        "optimization_key": spec.optimization_key,
        "trace_path": str(spec.trace_path),
        "config_digest": spec.config_digest,
        "task_set_digests": dict(spec.task_set_digests),
        "model_digests": dict(spec.model_digests),
        "campaign_id": spec.campaign_id,
    }


def _state_payload(state: OptimizationState) -> dict[str, Any]:
    return {
        "optimization_id": state.optimization_id,
        "lifecycle": state.lifecycle.value,
        "stage": state.stage.value,
        "aggregate_version": state.aggregate_version,
        "completed_operations": state.completed_operations,
        "outputs": thaw_json(state.outputs),
        "failure": None if state.failure is None else thaw_json(state.failure),
    }


def _event_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _identifier(prefix: str, value: Any) -> str:
    return f"{prefix}_{hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:24]}"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _store_error(code: str, message: str, *, cause: BaseException | None = None, **metadata: Any) -> Result:
    return err(
        make_loom_error(
            code,
            message,
            retryable=False,
            cause=None if cause is None else {"name": type(cause).__name__, "message": str(cause)},
            metadata=metadata,
        )
    )


def _sqlite_error(exc: BaseException) -> Result:
    return err(
        make_loom_error(
            "OPTIMIZATION_STORE_FAILED",
            "optimization store operation failed",
            retryable=isinstance(exc, sqlite3.OperationalError),
            cause={"name": type(exc).__name__, "message": str(exc)},
        )
    )


__all__ = ["SQLiteOptimizationStore", "StageCompletion", "StageLease"]
