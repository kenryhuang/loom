"""Transactional SQLite campaign event store."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from loom.campaigns.artifacts import ArtifactStore
from loom.campaigns.contracts import ArtifactRef, CampaignEvent, CampaignSpec
from loom.campaigns.operations import BudgetReservation, CampaignOperation, CommittedOperation, Reconciliation, StoreBudgetUsage
from loom.campaigns.reducer import reduce_campaign
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, new_prefixed_id, utc_now
from loom.core import ActorAssertion, Result, StaticIdentityProvider, err, make_loom_error, ok, thaw_json

_STORE_SCHEMA_VERSION = 2


class SQLiteCampaignStore:
    def __init__(self, campaign_dir: str | os.PathLike[str], identity_provider: StaticIdentityProvider):
        self.campaign_dir = Path(campaign_dir)
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.campaign_dir / "campaign.sqlite"
        self.artifacts = ArtifactStore(self.campaign_dir / "artifacts")
        self.identity_provider = identity_provider
        self._initialize()

    async def create(self, spec: CampaignSpec, *, operation_id: str, actor: ActorAssertion) -> Result:
        spec_bytes = canonical_json_bytes(spec)
        spec_digest = canonical_digest(spec)
        spec_ref_result = self.artifacts.publish_bytes(
            spec_bytes,
            kind="campaign_spec",
            schema_version=spec.schema_version,
            suffix=".json",
        )
        if not spec_ref_result.ok:
            return spec_ref_result
        operation = CampaignOperation(
            operation_id=operation_id,
            campaign_id=spec.campaign_id,
            input_digest=spec_digest,
            event_type="campaign.created",
            actor=actor,
            payload={"spec_digest": spec_digest, "spec_ref": spec_ref_result.value},
            output_refs=(spec_ref_result.value,),
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT campaign_id FROM campaign_metadata WHERE singleton = 1").fetchone()
            if existing is not None:
                replay = self._replay_operation(connection, operation)
                connection.rollback()
                return replay or _store_error("CAMPAIGN_ALREADY_EXISTS", "Campaign store is already initialized")
            connection.execute(
                "INSERT INTO campaign_metadata(singleton, campaign_id, spec_digest, aggregate_version) VALUES (1, ?, ?, 0)",
                (spec.campaign_id, spec_digest),
            )
            for phase_budget in spec.budget.phases:
                connection.execute(
                    "INSERT INTO budget_limits(phase, candidate_experiments, task_side_runs) VALUES (?, ?, ?)",
                    (phase_budget.phase.value, phase_budget.max_candidate_experiments, phase_budget.max_task_side_runs),
                )
                connection.execute(
                    "INSERT INTO budget_usage(phase, consumed_candidate_experiments, reserved_candidate_experiments, "
                    "consumed_task_side_runs, reserved_task_side_runs) VALUES (?, 0, 0, 0, 0)",
                    (phase_budget.phase.value,),
                )
            connection.execute(
                "INSERT INTO campaign_budget(singleton, iterations, candidates, infrastructure_retry_task_side_runs, proposer_tokens, "
                "solver_tokens, maximum_cost, wall_time_seconds) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    spec.budget.iterations,
                    spec.budget.iterations * spec.budget.candidates_per_iteration,
                    spec.budget.infrastructure_retry_task_side_runs,
                    spec.budget.proposer_tokens,
                    spec.budget.solver_tokens,
                    format(spec.budget.maximum_cost, "f"),
                    spec.budget.wall_time_seconds,
                ),
            )
            connection.execute(
                "INSERT INTO global_budget_usage(singleton, consumed_iterations, reserved_iterations, consumed_candidates, reserved_candidates, "
                "consumed_infrastructure_retry_task_side_runs, "
                "reserved_infrastructure_retry_task_side_runs, consumed_proposer_tokens, reserved_proposer_tokens, "
                "consumed_solver_tokens, reserved_solver_tokens, consumed_cost, reserved_cost, consumed_wall_time_seconds, "
                "reserved_wall_time_seconds) VALUES (1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, '0', '0', 0, 0)"
            )
            result = self._commit_operation(connection, operation, expected_version=0)
            if not result.ok:
                connection.rollback()
                return result
            _atomic_write(self.campaign_dir / "campaign.json", spec_bytes)
            connection.commit()
            return result
        except (OSError, sqlite3.Error) as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def load(self, campaign_id: str) -> Result:
        events_result = self._read_events(campaign_id)
        if not events_result.ok:
            return events_result
        for event in events_result.value:
            for ref in _find_artifact_refs(event.payload):
                verified = self.artifacts.read_bytes(ref)
                if not verified.ok:
                    return verified
        return reduce_campaign(events_result.value)

    async def transact(self, operation: CampaignOperation, expected_version: int) -> Result:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay_operation(connection, operation)
            if replay is not None:
                connection.rollback()
                return replay
            result = self._commit_operation(connection, operation, expected_version)
            if not result.ok:
                connection.rollback()
                return result
            connection.commit()
            return result
        except sqlite3.Error as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def reconcile(self, lease_id: str, actor: ActorAssertion) -> Result:
        authorized = _authorize_any(
            self.identity_provider,
            actor,
            ("campaign_controller", "campaign_operator"),
            forbidden_roles=("campaign_finalizer", "governance_approver", "registry_operator"),
        )
        if not authorized.ok:
            return authorized
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease = _lease_row(connection, lease_id)
            if lease is None:
                connection.rollback()
                return _store_error("LEASE_NOT_FOUND", "Operation lease was not found")
            if lease[13] != "active":
                connection.rollback()
                return ok(Reconciliation(lease[0], lease[1], "already_reconciled"))
            if _parse_time(lease[12]) > datetime.now(UTC):
                connection.rollback()
                return ok(Reconciliation(lease[0], lease[1], "still_active"))
            _release_lease_budget(connection, lease, consume=False)
            connection.execute("UPDATE leases SET status = 'released' WHERE lease_id = ?", (lease_id,))
            connection.execute("UPDATE operations SET status = 'reconciled' WHERE operation_id = ?", (lease[1],))
            connection.commit()
            return ok(Reconciliation(lease[0], lease[1], "released"))
        except sqlite3.Error as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def complete_lease(
        self,
        lease_id: str,
        actor: ActorAssertion,
        actual: BudgetReservation | None = None,
    ) -> Result:
        authorized = self.identity_provider.authorize(
            actor,
            "campaign_controller",
            forbidden_roles=("campaign_finalizer", "governance_approver", "registry_operator"),
        )
        if not authorized.ok:
            return authorized
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease = _lease_row(connection, lease_id)
            if lease is None:
                connection.rollback()
                return _store_error("LEASE_NOT_FOUND", "Operation lease was not found")
            if lease[14] != json.dumps(asdict(actor), sort_keys=True):
                connection.rollback()
                return _store_error("AUTHORIZATION_FAILED", "Only the lease owner can complete this campaign operation")
            if lease[13] == "completed":
                connection.rollback()
                return ok(Reconciliation(lease[0], lease[1], "already_reconciled"))
            if lease[13] != "active":
                connection.rollback()
                return _store_error("OPERATION_CONFLICT", "Campaign operation lease was fenced by reconciliation")
            if actual is not None:
                if not _actual_within_lease(actual, lease):
                    connection.rollback()
                    return _store_error("BUDGET_ACCOUNTING_INVALID", "Actual operation usage exceeds its reserved budget")
                _release_lease_budget(connection, lease, consume=False)
                _consume_actual_budget(connection, actual)
            else:
                _release_lease_budget(connection, lease, consume=True)
            connection.execute("UPDATE leases SET status = 'completed' WHERE lease_id = ?", (lease_id,))
            connection.execute("UPDATE operations SET status = 'completed' WHERE operation_id = ?", (lease[1],))
            connection.commit()
            return ok(Reconciliation(lease[0], lease[1], "completed"))
        except sqlite3.Error as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def cancel_lease(self, lease_id: str, actor: ActorAssertion) -> Result:
        authorized = self.identity_provider.authorize(
            actor,
            "campaign_controller",
            forbidden_roles=("campaign_finalizer", "governance_approver", "registry_operator"),
        )
        if not authorized.ok:
            return authorized
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease = _lease_row(connection, lease_id)
            if lease is None:
                connection.rollback()
                return _store_error("LEASE_NOT_FOUND", "Operation lease was not found")
            if lease[14] != json.dumps(asdict(actor), sort_keys=True):
                connection.rollback()
                return _store_error("AUTHORIZATION_FAILED", "Only the lease owner can cancel this campaign operation")
            if lease[13] == "cancelled":
                connection.rollback()
                return ok(Reconciliation(lease[0], lease[1], "already_reconciled"))
            if lease[13] != "active":
                connection.rollback()
                return _store_error("OPERATION_CONFLICT", "Campaign operation lease was fenced by reconciliation")
            _release_lease_budget(connection, lease, consume=False)
            connection.execute("UPDATE leases SET status = 'cancelled' WHERE lease_id = ?", (lease_id,))
            connection.execute("UPDATE operations SET status = 'cancelled' WHERE operation_id = ?", (lease[1],))
            connection.commit()
            return ok(Reconciliation(lease[0], lease[1], "cancelled"))
        except sqlite3.Error as exc:
            connection.rollback()
            return _sqlite_error(exc)
        finally:
            connection.close()

    async def export(self, campaign_id: str) -> Result:
        projection = await self.load(campaign_id)
        if not projection.ok:
            return projection
        events = self._read_events(campaign_id)
        if not events.ok:
            return events
        payload = {
            "schema_version": "loom.campaign.export.v1",
            "campaign_id": campaign_id,
            "projection": projection.value.to_dict(),
            "events": [_event_to_dict(event) for event in events.value],
        }
        return self.artifacts.publish_bytes(
            canonical_json_bytes(payload),
            kind="campaign_export",
            schema_version="loom.campaign.export.v1",
            suffix=".json",
        )

    async def rebuild(self, campaign_id: str) -> Result:
        return await self.load(campaign_id)

    async def events(self, campaign_id: str) -> Result:
        """Return the verified append-only event stream for controller recovery."""
        return self._read_events(campaign_id)

    async def spec_ref(self, campaign_id: str) -> Result:
        events = self._read_events(campaign_id)
        if not events.ok:
            return events
        value = events.value[0].payload.get("spec_ref")
        if not isinstance(value, Mapping):
            return _store_error("CAMPAIGN_EVENT_INVALID", "Campaign creation event has no specification artifact")
        try:
            return ok(ArtifactRef(**value))
        except (TypeError, ValueError):
            return _store_error("CAMPAIGN_EVENT_INVALID", "Campaign specification artifact reference is invalid")

    async def budget_usage(self, campaign_id: str) -> Result:
        connection = self._connect()
        try:
            self._verify_campaign(connection, campaign_id)
            rows = connection.execute(
                "SELECT phase, consumed_candidate_experiments, reserved_candidate_experiments, consumed_task_side_runs, "
                "reserved_task_side_runs FROM budget_usage"
            ).fetchall()
            global_usage = connection.execute(
                "SELECT consumed_infrastructure_retry_task_side_runs, reserved_infrastructure_retry_task_side_runs, "
                "consumed_proposer_tokens, reserved_proposer_tokens, consumed_solver_tokens, reserved_solver_tokens, "
                "consumed_cost, reserved_cost, consumed_iterations, reserved_iterations, consumed_candidates, reserved_candidates, "
                "consumed_wall_time_seconds, reserved_wall_time_seconds FROM global_budget_usage WHERE singleton = 1"
            ).fetchone()
            return ok(
                StoreBudgetUsage(
                    {row[0]: row[1] for row in rows},
                    {row[0]: row[2] for row in rows},
                    {row[0]: row[3] for row in rows},
                    {row[0]: row[4] for row in rows},
                    global_usage[0],
                    global_usage[1],
                    global_usage[2],
                    global_usage[3],
                    global_usage[4],
                    global_usage[5],
                    _decimal_text(Decimal(global_usage[6])),
                    _decimal_text(Decimal(global_usage[7])),
                    global_usage[8],
                    global_usage[9],
                    global_usage[10],
                    global_usage[11],
                    global_usage[12],
                    global_usage[13],
                )
            )
        except sqlite3.Error as exc:
            return _sqlite_error(exc)
        finally:
            connection.close()

    def _commit_operation(self, connection: sqlite3.Connection, operation: CampaignOperation, expected_version: int) -> Result:
        authorized = self._authorize_operation(operation)
        if not authorized.ok:
            return authorized
        metadata = connection.execute("SELECT campaign_id, aggregate_version FROM campaign_metadata WHERE singleton = 1").fetchone()
        if metadata is None or metadata[0] != operation.campaign_id:
            return _store_error("CAMPAIGN_NOT_FOUND", "Campaign does not exist")
        if metadata[1] != expected_version:
            return err(
                make_loom_error(
                    "CONCURRENCY_CONFLICT",
                    "Campaign aggregate version changed",
                    retryable=True,
                    metadata={"expected_version": expected_version, "actual_version": metadata[1]},
                )
            )
        next_version = metadata[1] + 1
        previous = connection.execute("SELECT event_hash FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
        previous_hash = None if previous is None else previous[0]
        created_at = utc_now()
        event_id = new_prefixed_id("evt_")
        event_material = {
            "schema_version": "loom.campaign.event.v1",
            "event_id": event_id,
            "campaign_id": operation.campaign_id,
            "sequence": next_version,
            "aggregate_version": next_version,
            "event_type": operation.event_type,
            "created_at": created_at,
            "operation_id": operation.operation_id,
            "payload": operation.payload,
            "previous_hash": previous_hash,
        }
        event_hash = canonical_digest(event_material)
        candidate_event = CampaignEvent(
            "loom.campaign.event.v1",
            event_id,
            operation.campaign_id,
            next_version,
            next_version,
            operation.event_type,
            created_at,
            operation.operation_id,
            operation.payload,
            previous_hash,
            event_hash,
        )
        if next_version == 1:
            current_events = ()
        else:
            current = self._read_events(operation.campaign_id)
            if not current.ok:
                return current
            current_events = current.value
        transition = reduce_campaign((*current_events, candidate_event))
        if not transition.ok:
            return transition
        reservation_result = self._apply_reservation(connection, operation)
        if not reservation_result.ok:
            return reservation_result
        connection.execute(
            "INSERT INTO events(sequence, aggregate_version, event_id, event_type, created_at, operation_id, payload_json, "
            "previous_hash, event_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                next_version,
                next_version,
                event_id,
                operation.event_type,
                created_at,
                operation.operation_id,
                canonical_json_bytes(operation.payload).decode(),
                previous_hash,
                event_hash,
            ),
        )
        lease_id = reservation_result.value
        committed = CommittedOperation(
            operation.operation_id,
            operation.campaign_id,
            next_version,
            operation.event_type,
            event_hash,
            lease_id=lease_id,
            output_refs=operation.output_refs,
        )
        connection.execute(
            "INSERT INTO operations(operation_id, input_digest, status, result_json) VALUES (?, ?, ?, ?)",
            (
                operation.operation_id,
                operation.input_digest,
                "completed" if operation.complete else "started",
                json.dumps(_committed_to_dict(committed), sort_keys=True),
            ),
        )
        connection.execute("UPDATE campaign_metadata SET aggregate_version = ? WHERE singleton = 1", (next_version,))
        return ok(committed)

    def _authorize_operation(self, operation: CampaignOperation) -> Result:
        if operation.actor is None:
            return _store_error("AUTHENTICATION_FAILED", "Campaign operation requires an authenticated actor")
        allowed_roles, forbidden_roles = _campaign_event_roles(operation.event_type)
        if not allowed_roles:
            return _store_error("AUTHORIZATION_FAILED", "Campaign event type has no authorized writer")
        for role in allowed_roles:
            if role in operation.actor.roles:
                return self.identity_provider.authorize(operation.actor, role, forbidden_roles=forbidden_roles)
        return err(
            make_loom_error(
                "AUTHORIZATION_FAILED",
                "Actor cannot write this campaign event",
                retryable=False,
                metadata={"event_type": operation.event_type, "allowed_roles": allowed_roles},
            )
        )

    def _apply_reservation(self, connection: sqlite3.Connection, operation: CampaignOperation) -> Result:
        reservation = operation.reservation
        if reservation is None:
            return ok(None)
        phase = reservation.phase.value
        row = connection.execute(
            "SELECT l.candidate_experiments, l.task_side_runs, u.consumed_candidate_experiments, "
            "u.reserved_candidate_experiments, u.consumed_task_side_runs, u.reserved_task_side_runs "
            "FROM budget_limits l JOIN budget_usage u USING (phase) WHERE phase = ?",
            (phase,),
        ).fetchone()
        if row is None:
            return _store_error("BUDGET_EXCEEDED", "Campaign phase has no budget")
        next_candidates = row[2] + row[3] + reservation.candidate_experiments
        next_runs = row[4] + row[5] + reservation.task_side_runs
        if next_candidates > row[0] or next_runs > row[1]:
            return err(
                make_loom_error(
                    "BUDGET_EXCEEDED",
                    "Campaign phase budget is exhausted",
                    retryable=False,
                    metadata={"phase": phase, "candidate_experiments": next_candidates, "task_side_runs": next_runs},
                )
            )
        global_row = connection.execute(
            "SELECT b.iterations, b.candidates, b.infrastructure_retry_task_side_runs, b.proposer_tokens, b.solver_tokens, "
            "b.maximum_cost, b.wall_time_seconds, u.consumed_iterations, u.reserved_iterations, u.consumed_candidates, "
            "u.reserved_candidates, u.consumed_infrastructure_retry_task_side_runs, u.reserved_infrastructure_retry_task_side_runs, "
            "u.consumed_proposer_tokens, u.reserved_proposer_tokens, u.consumed_solver_tokens, u.reserved_solver_tokens, "
            "u.consumed_cost, u.reserved_cost, u.consumed_wall_time_seconds, u.reserved_wall_time_seconds "
            "FROM campaign_budget b JOIN global_budget_usage u USING (singleton) WHERE singleton = 1"
        ).fetchone()
        next_iterations = global_row[7] + global_row[8] + reservation.iterations
        next_global_candidates = global_row[9] + global_row[10] + reservation.candidates
        next_retry = global_row[11] + global_row[12] + reservation.infrastructure_retry_task_side_runs
        next_proposer = global_row[13] + global_row[14] + reservation.proposer_tokens
        next_solver = global_row[15] + global_row[16] + reservation.solver_tokens
        next_cost = Decimal(global_row[17]) + Decimal(global_row[18]) + Decimal(reservation.cost)
        next_wall = global_row[19] + global_row[20] + reservation.wall_time_seconds
        if (
            next_iterations > global_row[0]
            or next_global_candidates > global_row[1]
            or next_retry > global_row[2]
            or next_proposer > global_row[3]
            or next_solver > global_row[4]
            or next_cost > Decimal(global_row[5])
            or next_wall > global_row[6]
        ):
            return err(
                make_loom_error(
                    "BUDGET_EXCEEDED",
                    "Campaign global budget is exhausted",
                    retryable=False,
                    metadata={
                        "phase": phase,
                        "iterations": next_iterations,
                        "candidates": next_global_candidates,
                        "infrastructure_retry_task_side_runs": next_retry,
                        "proposer_tokens": next_proposer,
                        "solver_tokens": next_solver,
                        "cost": _decimal_text(next_cost),
                        "wall_time_seconds": next_wall,
                    },
                )
            )
        if operation.complete:
            connection.execute(
                "UPDATE budget_usage SET consumed_candidate_experiments = consumed_candidate_experiments + ?, "
                "consumed_task_side_runs = consumed_task_side_runs + ? WHERE phase = ?",
                (reservation.candidate_experiments, reservation.task_side_runs, phase),
            )
            connection.execute(
                "UPDATE global_budget_usage SET consumed_iterations = consumed_iterations + ?, consumed_candidates = consumed_candidates + ?, "
                "consumed_infrastructure_retry_task_side_runs = "
                "consumed_infrastructure_retry_task_side_runs + ?, consumed_proposer_tokens = consumed_proposer_tokens + ?, "
                "consumed_solver_tokens = consumed_solver_tokens + ?, consumed_cost = ?, consumed_wall_time_seconds = "
                "consumed_wall_time_seconds + ? WHERE singleton = 1",
                (
                    reservation.iterations,
                    reservation.candidates,
                    reservation.infrastructure_retry_task_side_runs,
                    reservation.proposer_tokens,
                    reservation.solver_tokens,
                    _decimal_text(Decimal(global_row[17]) + Decimal(reservation.cost)),
                    reservation.wall_time_seconds,
                ),
            )
            return ok(None)
        lease_id = new_prefixed_id("lease_")
        expires_at = (datetime.now(UTC) + timedelta(seconds=operation.lease_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        connection.execute(
            "UPDATE budget_usage SET reserved_candidate_experiments = reserved_candidate_experiments + ?, "
            "reserved_task_side_runs = reserved_task_side_runs + ? WHERE phase = ?",
            (reservation.candidate_experiments, reservation.task_side_runs, phase),
        )
        connection.execute(
            "UPDATE global_budget_usage SET reserved_iterations = reserved_iterations + ?, reserved_candidates = reserved_candidates + ?, "
            "reserved_infrastructure_retry_task_side_runs = "
            "reserved_infrastructure_retry_task_side_runs + ?, reserved_proposer_tokens = reserved_proposer_tokens + ?, "
            "reserved_solver_tokens = reserved_solver_tokens + ?, reserved_cost = ?, reserved_wall_time_seconds = "
            "reserved_wall_time_seconds + ? WHERE singleton = 1",
            (
                reservation.iterations,
                reservation.candidates,
                reservation.infrastructure_retry_task_side_runs,
                reservation.proposer_tokens,
                reservation.solver_tokens,
                _decimal_text(Decimal(global_row[18]) + Decimal(reservation.cost)),
                reservation.wall_time_seconds,
            ),
        )
        connection.execute(
            "INSERT INTO leases(lease_id, operation_id, phase, candidate_experiments, task_side_runs, "
            "infrastructure_retry_task_side_runs, proposer_tokens, solver_tokens, cost, iterations, candidates, wall_time_seconds, "
            "expires_at, status, actor_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                lease_id,
                operation.operation_id,
                phase,
                reservation.candidate_experiments,
                reservation.task_side_runs,
                reservation.infrastructure_retry_task_side_runs,
                reservation.proposer_tokens,
                reservation.solver_tokens,
                reservation.cost,
                reservation.iterations,
                reservation.candidates,
                reservation.wall_time_seconds,
                expires_at,
                json.dumps(asdict(operation.actor), sort_keys=True),
            ),
        )
        return ok(lease_id)

    def _replay_operation(self, connection: sqlite3.Connection, operation: CampaignOperation) -> Result | None:
        existing = connection.execute(
            "SELECT input_digest, result_json FROM operations WHERE operation_id = ?",
            (operation.operation_id,),
        ).fetchone()
        if existing is None:
            return None
        if existing[0] != operation.input_digest:
            return err(make_loom_error("OPERATION_CONFLICT", "Operation ID was reused with different input", retryable=False))
        return ok(_committed_from_dict(json.loads(existing[1])))

    def _read_events(self, campaign_id: str) -> Result:
        connection = self._connect()
        try:
            metadata = connection.execute("SELECT campaign_id, aggregate_version FROM campaign_metadata WHERE singleton = 1").fetchone()
            if metadata is None or metadata[0] != campaign_id:
                return _store_error("CAMPAIGN_NOT_FOUND", "Campaign does not exist")
            rows = connection.execute(
                "SELECT sequence, aggregate_version, event_id, event_type, created_at, operation_id, payload_json, "
                "previous_hash, event_hash FROM events ORDER BY sequence"
            ).fetchall()
            if len(rows) != metadata[1] or (rows and rows[-1][0] != metadata[1]):
                return _store_error("CAMPAIGN_EVENT_INVALID", "Campaign event log is truncated relative to committed version")
            events = tuple(
                CampaignEvent(
                    "loom.campaign.event.v1",
                    row[2],
                    campaign_id,
                    row[0],
                    row[1],
                    row[3],
                    row[4],
                    row[5],
                    json.loads(row[6]),
                    row[7],
                    row[8],
                )
                for row in rows
            )
            previous_hash = None
            for event in events:
                if event.previous_hash != previous_hash:
                    return _store_error("CAMPAIGN_EVENT_INVALID", "Campaign event hash chain is broken")
                material = _event_to_dict(event)
                material.pop("event_hash")
                if canonical_digest(material) != event.event_hash:
                    return _store_error("CAMPAIGN_EVENT_INVALID", "Campaign event digest is invalid")
                previous_hash = event.event_hash
            return ok(events)
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            return _sqlite_error(exc)
        finally:
            connection.close()

    def _verify_campaign(self, connection: sqlite3.Connection, campaign_id: str) -> None:
        row = connection.execute("SELECT campaign_id FROM campaign_metadata WHERE singleton = 1").fetchone()
        if row is None or row[0] != campaign_id:
            raise sqlite3.IntegrityError("Campaign not found")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info(version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS campaign_metadata(
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    campaign_id TEXT NOT NULL UNIQUE,
                    spec_digest TEXT NOT NULL,
                    aggregate_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    sequence INTEGER PRIMARY KEY,
                    aggregate_version INTEGER NOT NULL UNIQUE,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT,
                    event_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS operations(
                    operation_id TEXT PRIMARY KEY,
                    input_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_limits(
                    phase TEXT PRIMARY KEY,
                    candidate_experiments INTEGER NOT NULL,
                    task_side_runs INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_usage(
                    phase TEXT PRIMARY KEY REFERENCES budget_limits(phase),
                    consumed_candidate_experiments INTEGER NOT NULL,
                    reserved_candidate_experiments INTEGER NOT NULL,
                    consumed_task_side_runs INTEGER NOT NULL,
                    reserved_task_side_runs INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_budget(
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    iterations INTEGER NOT NULL,
                    candidates INTEGER NOT NULL,
                    infrastructure_retry_task_side_runs INTEGER NOT NULL,
                    proposer_tokens INTEGER NOT NULL,
                    solver_tokens INTEGER NOT NULL,
                    maximum_cost TEXT NOT NULL,
                    wall_time_seconds INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS global_budget_usage(
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    consumed_iterations INTEGER NOT NULL,
                    reserved_iterations INTEGER NOT NULL,
                    consumed_candidates INTEGER NOT NULL,
                    reserved_candidates INTEGER NOT NULL,
                    consumed_infrastructure_retry_task_side_runs INTEGER NOT NULL,
                    reserved_infrastructure_retry_task_side_runs INTEGER NOT NULL,
                    consumed_proposer_tokens INTEGER NOT NULL,
                    reserved_proposer_tokens INTEGER NOT NULL,
                    consumed_solver_tokens INTEGER NOT NULL,
                    reserved_solver_tokens INTEGER NOT NULL,
                    consumed_cost TEXT NOT NULL,
                    reserved_cost TEXT NOT NULL,
                    consumed_wall_time_seconds INTEGER NOT NULL,
                    reserved_wall_time_seconds INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases(
                    lease_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,
                    phase TEXT NOT NULL,
                    candidate_experiments INTEGER NOT NULL,
                    task_side_runs INTEGER NOT NULL,
                    infrastructure_retry_task_side_runs INTEGER NOT NULL,
                    proposer_tokens INTEGER NOT NULL,
                    solver_tokens INTEGER NOT NULL,
                    cost TEXT NOT NULL,
                    iterations INTEGER NOT NULL,
                    candidates INTEGER NOT NULL,
                    wall_time_seconds INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor_json TEXT NOT NULL
                );
                """
            )
            version = connection.execute("SELECT version FROM schema_info").fetchone()
            if version is None:
                connection.execute("INSERT INTO schema_info(version) VALUES (?)", (_STORE_SCHEMA_VERSION,))
            elif version[0] == 1:
                _ensure_column(connection, "leases", "actor_json", "TEXT NOT NULL DEFAULT '{}'")
                connection.execute("UPDATE schema_info SET version = ?", (_STORE_SCHEMA_VERSION,))
            elif version[0] != _STORE_SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported campaign store schema: {version[0]}")
        finally:
            connection.close()


def _event_to_dict(event: CampaignEvent) -> dict[str, Any]:
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "campaign_id": event.campaign_id,
        "sequence": event.sequence,
        "aggregate_version": event.aggregate_version,
        "event_type": event.event_type,
        "created_at": event.created_at,
        "operation_id": event.operation_id,
        "payload": thaw_json(event.payload),
        "previous_hash": event.previous_hash,
        "event_hash": event.event_hash,
    }


def _committed_to_dict(value: CommittedOperation) -> dict[str, Any]:
    return {
        "operation_id": value.operation_id,
        "campaign_id": value.campaign_id,
        "aggregate_version": value.aggregate_version,
        "event_type": value.event_type,
        "event_hash": value.event_hash,
        "lease_id": value.lease_id,
        "output_refs": [asdict(ref) for ref in value.output_refs],
    }


def _committed_from_dict(value: Mapping[str, Any]) -> CommittedOperation:
    return CommittedOperation(
        str(value["operation_id"]),
        str(value["campaign_id"]),
        int(value["aggregate_version"]),
        str(value["event_type"]),
        str(value["event_hash"]),
        value.get("lease_id"),
        tuple(ArtifactRef(**item) for item in value.get("output_refs", ())),
    )


def _find_artifact_refs(value: Any) -> Iterable[ArtifactRef]:
    if isinstance(value, Mapping):
        names = {field.name for field in fields(ArtifactRef)}
        if names.issubset(value):
            try:
                yield ArtifactRef(**{name: value[name] for name in names})
            except (TypeError, ValueError):
                return
        for item in value.values():
            yield from _find_artifact_refs(item)
    elif isinstance(value, tuple | list):
        for item in value:
            yield from _find_artifact_refs(item)


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _lease_row(connection: sqlite3.Connection, lease_id: str):
    return connection.execute(
        "SELECT lease_id, operation_id, phase, candidate_experiments, task_side_runs, infrastructure_retry_task_side_runs, "
        "proposer_tokens, solver_tokens, cost, iterations, candidates, wall_time_seconds, expires_at, status, actor_json "
        "FROM leases WHERE lease_id = ?",
        (lease_id,),
    ).fetchone()


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _authorize_any(provider, actor: ActorAssertion, roles: tuple[str, ...], *, forbidden_roles: tuple[str, ...]) -> Result:
    for role in roles:
        if role in actor.roles:
            return provider.authorize(actor, role, forbidden_roles=forbidden_roles)
    return _store_error("AUTHORIZATION_FAILED", "Actor cannot reconcile campaign leases")


def _release_lease_budget(connection: sqlite3.Connection, lease, *, consume: bool) -> None:
    if consume:
        connection.execute(
            "UPDATE budget_usage SET consumed_candidate_experiments = consumed_candidate_experiments + ?, "
            "reserved_candidate_experiments = reserved_candidate_experiments - ?, consumed_task_side_runs = "
            "consumed_task_side_runs + ?, reserved_task_side_runs = reserved_task_side_runs - ? WHERE phase = ?",
            (lease[3], lease[3], lease[4], lease[4], lease[2]),
        )
    else:
        connection.execute(
            "UPDATE budget_usage SET reserved_candidate_experiments = reserved_candidate_experiments - ?, "
            "reserved_task_side_runs = reserved_task_side_runs - ? WHERE phase = ?",
            (lease[3], lease[4], lease[2]),
        )
    usage = connection.execute(
        "SELECT consumed_iterations, reserved_iterations, consumed_candidates, reserved_candidates, "
        "consumed_infrastructure_retry_task_side_runs, reserved_infrastructure_retry_task_side_runs, "
        "consumed_proposer_tokens, reserved_proposer_tokens, consumed_solver_tokens, reserved_solver_tokens, "
        "consumed_cost, reserved_cost, consumed_wall_time_seconds, reserved_wall_time_seconds "
        "FROM global_budget_usage WHERE singleton = 1"
    ).fetchone()
    consumed_multiplier = 1 if consume else 0
    connection.execute(
        "UPDATE global_budget_usage SET consumed_iterations = ?, reserved_iterations = ?, consumed_candidates = ?, "
        "reserved_candidates = ?, consumed_infrastructure_retry_task_side_runs = ?, "
        "reserved_infrastructure_retry_task_side_runs = ?, consumed_proposer_tokens = ?, reserved_proposer_tokens = ?, "
        "consumed_solver_tokens = ?, reserved_solver_tokens = ?, consumed_cost = ?, reserved_cost = ?, "
        "consumed_wall_time_seconds = ?, reserved_wall_time_seconds = ? WHERE singleton = 1",
        (
            usage[0] + lease[9] * consumed_multiplier,
            usage[1] - lease[9],
            usage[2] + lease[10] * consumed_multiplier,
            usage[3] - lease[10],
            usage[4] + lease[5] * consumed_multiplier,
            usage[5] - lease[5],
            usage[6] + lease[6] * consumed_multiplier,
            usage[7] - lease[6],
            usage[8] + lease[7] * consumed_multiplier,
            usage[9] - lease[7],
            _decimal_text(Decimal(usage[10]) + Decimal(lease[8]) * consumed_multiplier),
            _decimal_text(Decimal(usage[11]) - Decimal(lease[8])),
            usage[12] + lease[11] * consumed_multiplier,
            usage[13] - lease[11],
        ),
    )


def _actual_within_lease(actual: BudgetReservation, lease) -> bool:
    return (
        actual.phase.value == lease[2]
        and actual.candidate_experiments <= lease[3]
        and actual.task_side_runs <= lease[4]
        and actual.infrastructure_retry_task_side_runs <= lease[5]
        and actual.proposer_tokens <= lease[6]
        and actual.solver_tokens <= lease[7]
        and Decimal(actual.cost) <= Decimal(lease[8])
        and actual.iterations <= lease[9]
        and actual.candidates <= lease[10]
        and actual.wall_time_seconds <= lease[11]
    )


def _consume_actual_budget(connection: sqlite3.Connection, actual: BudgetReservation) -> None:
    connection.execute(
        "UPDATE budget_usage SET consumed_candidate_experiments = consumed_candidate_experiments + ?, "
        "consumed_task_side_runs = consumed_task_side_runs + ? WHERE phase = ?",
        (actual.candidate_experiments, actual.task_side_runs, actual.phase.value),
    )
    usage = connection.execute("SELECT consumed_cost FROM global_budget_usage WHERE singleton = 1").fetchone()
    connection.execute(
        "UPDATE global_budget_usage SET consumed_iterations = consumed_iterations + ?, consumed_candidates = consumed_candidates + ?, "
        "consumed_infrastructure_retry_task_side_runs = consumed_infrastructure_retry_task_side_runs + ?, "
        "consumed_proposer_tokens = consumed_proposer_tokens + ?, consumed_solver_tokens = consumed_solver_tokens + ?, "
        "consumed_cost = ?, consumed_wall_time_seconds = consumed_wall_time_seconds + ? WHERE singleton = 1",
        (
            actual.iterations,
            actual.candidates,
            actual.infrastructure_retry_task_side_runs,
            actual.proposer_tokens,
            actual.solver_tokens,
            _decimal_text(Decimal(usage[0]) + Decimal(actual.cost)),
            actual.wall_time_seconds,
        ),
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _campaign_event_roles(event_type: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if event_type == "campaign.created" or event_type == "experience.imported":
        return ("campaign_creator",), ()
    if event_type in {"campaign.started", "campaign.paused", "campaign.resumed", "campaign.aborted"}:
        return ("campaign_operator",), ()
    if event_type == "candidate.created":
        return ("campaign_operator", "campaign_controller"), ("campaign_finalizer", "governance_approver", "registry_operator")
    if event_type in {
        "campaign.search_sealed",
        "campaign.finalists_selected",
        "campaign.finalized",
        "candidate.validation.passed",
        "candidate.validation.rejected",
        "candidate.holdout.passed",
        "candidate.holdout.rejected",
        "campaign.validation_evidence_consumed",
        "campaign.holdout_evidence_consumed",
    }:
        return ("campaign_finalizer",), ("campaign_controller", "governance_approver", "registry_operator")
    if event_type in {"candidate.awaiting_approval", "candidate.promoted", "candidate.expired", "candidate.rolled_back"}:
        return ("governance_automation", "registry_operator"), ("campaign_controller", "campaign_finalizer", "governance_approver")
    if event_type == "promotion.rejected":
        return ("governance_automation", "registry_operator"), ("campaign_controller", "campaign_finalizer", "governance_approver")
    if event_type.startswith(("candidate.validation", "experiment.", "frontier.", "history.", "campaign.iteration_")) or event_type == "campaign.frozen":
        return ("campaign_controller",), ("campaign_finalizer", "governance_approver", "registry_operator")
    return (), ()


def _store_error(code: str, message: str) -> Result:
    return err(make_loom_error(code, message, retryable=False))


def _sqlite_error(exc: BaseException) -> Result:
    return err(
        make_loom_error(
            "CAMPAIGN_STORE_FAILED",
            "Campaign store operation failed",
            retryable=isinstance(exc, sqlite3.OperationalError),
            cause={"name": type(exc).__name__, "message": str(exc)},
        )
    )


__all__ = ["SQLiteCampaignStore"]
