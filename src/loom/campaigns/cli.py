"""Phase-zero campaign command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from loom.campaigns.config import load_campaign_spec, load_resolved_campaign_spec
from loom.campaigns.contracts import ArtifactRef
from loom.campaigns.controller import CampaignController
from loom.campaigns.history import import_experience
from loom.campaigns.operations import CampaignOperation
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, new_prefixed_id
from loom.campaigns.store import SQLiteCampaignStore
from loom.core import ActorAssertion, Result, StaticIdentityProvider, err, make_loom_error, ok, thaw_json


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    result = asyncio.run(_execute(args))
    if not result.ok:
        payload = {
            "ok": False,
            "error": {
                "code": result.error.code,
                "message": result.error.message,
                "retryable": result.error.retryable,
                "metadata": None if result.error.metadata is None else thaw_json(result.error.metadata),
            },
        }
        print(json.dumps(payload, sort_keys=True) if args.json else f"{result.error.code}: {result.error.message}")
        return 1
    payload = json.loads(canonical_json_bytes(result.value))
    print(json.dumps(payload, sort_keys=True) if args.json else _human(payload))
    return 0


async def _execute(args) -> Any:
    identity = load_identity(args.identity, configured_trust_store())
    if not identity.ok:
        return identity
    actor, provider = identity.value
    store = SQLiteCampaignStore(args.campaign_dir, provider)
    if args.command in {"create", "derive"}:
        derived_from = None
        ancestor_holdout = None
        if args.command == "derive":
            parent_manifest_path = Path(args.from_campaign_dir) / "campaign.json"
            try:
                parent = json.loads(parent_manifest_path.read_text(encoding="utf-8"))
                derived_from = str(parent["campaign_id"])
                ancestor_holdout = str(parent["holdout_set"]["fingerprint_digest"])
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                from loom.core import err, make_loom_error

                return err(
                    make_loom_error(
                        "VALIDATION_FAILED",
                        "Parent campaign manifest is invalid",
                        retryable=False,
                        cause={"name": type(exc).__name__, "message": str(exc)},
                    )
                )
        resolved = load_campaign_spec(
            args.config,
            store.artifacts,
            derived_from=derived_from,
            ancestor_holdout_digest=ancestor_holdout,
        )
        if not resolved.ok:
            return resolved
        created = await store.create(resolved.value, operation_id=args.operation_id or new_prefixed_id("op_"), actor=actor)
        if not created.ok:
            return created
        from loom.core import ok

        return ok(
            {
                "campaign_id": resolved.value.campaign_id,
                "operation_id": created.value.operation_id,
                "aggregate_version": created.value.aggregate_version,
                "event_type": created.value.event_type,
            }
        )
    projection = await store.load(args.campaign_id)
    if not projection.ok:
        return projection
    if args.command == "status":
        from loom.core import ok

        return ok(projection.value.to_dict())
    if args.command == "frontier":
        spec_ref = await store.spec_ref(args.campaign_id)
        if not spec_ref.ok:
            return spec_ref
        loaded_spec = load_resolved_campaign_spec(Path(args.campaign_dir) / "campaign.json", store.artifacts, spec_ref.value)
        if not loaded_spec.ok:
            return loaded_spec
        controller = CampaignController(store, loaded_spec.value, controller_actor=actor, finalizer_actor=actor)
        restored = await controller.restore()
        if not restored.ok:
            return restored
        return ok(
            {
                "schema_version": "loom.campaign.frontier-cli.v1",
                "campaign_id": args.campaign_id,
                "frontier_digest": controller.frontier_digest(),
                "latest_frontier_ref": projection.value.latest_frontier_ref,
                "candidates": [candidate_id for candidate_id, state in sorted(projection.value.candidates.items()) if state.lifecycle.value == "evaluated"],
            }
        )
    if args.command == "inspect":
        candidate = projection.value.candidates.get(args.candidate_id)
        if candidate is None:
            return err(make_loom_error("VALIDATION_FAILED", "Candidate was not found", retryable=False))
        return ok(projection.value.to_dict()["candidates"][args.candidate_id])
    if args.command in {"seal-search", "select-finalists", "finalize"}:
        spec_ref = await store.spec_ref(args.campaign_id)
        if not spec_ref.ok:
            return spec_ref
        loaded_spec = load_resolved_campaign_spec(Path(args.campaign_dir) / "campaign.json", store.artifacts, spec_ref.value)
        if not loaded_spec.ok:
            return loaded_spec
        controller = CampaignController(store, loaded_spec.value, controller_actor=actor, finalizer_actor=actor)
        if args.command == "seal-search":
            result = await controller.seal_search(
                args.operation_id or new_prefixed_id("op_"),
                args.expect_frontier_digest,
            )
        elif args.command == "select-finalists":
            ref = _artifact_ref_argument(args.validation_results_ref)
            if not ref.ok:
                return ref
            result = await controller.select_finalists(ref.value, dry_run=args.dry_run)
        else:
            ref = _artifact_ref_argument(args.holdout_results_ref)
            if not ref.ok:
                return ref
            result = await controller.finalize(
                args.expect_finalist_digest,
                ref.value,
                operation_id=args.operation_id or new_prefixed_id("op_"),
            )
        if not result.ok:
            return result
        return ok({"schema_version": "loom.campaign-command.v1", "campaign_id": args.campaign_id, "result": result.value})
    if args.command == "import-experience":
        return await import_experience(
            store,
            args.campaign_id,
            args.artifact_path,
            operation_id=args.operation_id or new_prefixed_id("op_"),
            actor=actor,
        )
    event_type = {
        "run": "campaign.started",
        "pause": "campaign.paused",
        "resume": "campaign.resumed",
        "abort": "campaign.aborted",
    }[args.command]
    operation_id = args.operation_id or new_prefixed_id("op_")
    operation = CampaignOperation(
        operation_id,
        args.campaign_id,
        canonical_digest({"command": args.command, "campaign_id": args.campaign_id}),
        event_type,
        actor=actor,
    )
    result = await store.transact(operation, projection.value.aggregate_version)
    if not result.ok:
        return result
    from loom.core import ok

    return ok(
        {
            "operation_id": result.value.operation_id,
            "campaign_id": result.value.campaign_id,
            "aggregate_version": result.value.aggregate_version,
            "event_type": result.value.event_type,
            "event_hash": result.value.event_hash,
        }
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loom campaign")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "derive"):
        child = subparsers.add_parser(command)
        child.add_argument("campaign_dir")
        child.add_argument("--config", required=True)
        child.add_argument("--operation-id")
        child.add_argument("--identity", required=True)
        child.add_argument("--json", action="store_true")
        if command == "derive":
            child.add_argument("--from-campaign-dir", required=True)
    for command in ("status", "frontier", "run", "pause", "resume", "abort"):
        child = subparsers.add_parser(command)
        child.add_argument("campaign_dir")
        child.add_argument("--campaign-id", required=True)
        child.add_argument("--identity", required=True)
        child.add_argument("--json", action="store_true")
        if command != "status":
            child.add_argument("--operation-id")
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("campaign_dir")
    inspect.add_argument("candidate_id")
    inspect.add_argument("--campaign-id", required=True)
    inspect.add_argument("--identity", required=True)
    inspect.add_argument("--json", action="store_true")
    imported = subparsers.add_parser("import-experience")
    imported.add_argument("campaign_dir")
    imported.add_argument("artifact_path")
    imported.add_argument("--campaign-id", required=True)
    imported.add_argument("--operation-id")
    imported.add_argument("--identity", required=True)
    imported.add_argument("--json", action="store_true")
    seal = subparsers.add_parser("seal-search")
    seal.add_argument("campaign_dir")
    seal.add_argument("--campaign-id", required=True)
    seal.add_argument("--expect-frontier-digest", required=True)
    seal.add_argument("--operation-id")
    finalists = subparsers.add_parser("select-finalists")
    finalists.add_argument("campaign_dir")
    finalists.add_argument("--campaign-id", required=True)
    finalists.add_argument("--validation-results-ref", required=True)
    finalists.add_argument("--dry-run", action="store_true")
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("campaign_dir")
    finalize.add_argument("--campaign-id", required=True)
    finalize.add_argument("--expect-finalist-digest", required=True)
    finalize.add_argument("--holdout-results-ref", required=True)
    finalize.add_argument("--operation-id")
    for child in (seal, finalists, finalize):
        child.add_argument("--identity", required=True)
        child.add_argument("--json", action="store_true")
    return parser


def _artifact_ref_argument(value: str) -> Result:
    try:
        source = Path(value)
        raw = source.read_text(encoding="utf-8") if source.is_file() else value
        payload = json.loads(raw)
        return ok(ArtifactRef(**payload))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Artifact reference argument is invalid",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        )


def _human(payload: Any) -> str:
    if isinstance(payload, dict):
        return "\n".join(f"{key}: {value}" for key, value in payload.items())
    return str(payload)


def load_identity(path: str | Path, trust_store_path: str | Path):
    identity_path = Path(path)
    trust_path = Path(trust_store_path)
    try:
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
        actor = ActorAssertion(**payload["actor"])
        trust = json.loads(trust_path.read_text(encoding="utf-8"))
        trusted = tuple(ActorAssertion(**item) for item in trust["trusted_assertions"])
        provider = StaticIdentityProvider(trusted, revoked_signatures=tuple(trust.get("revoked_signatures", ())))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "AUTHENTICATION_FAILED",
                "Campaign CLI identity file is invalid",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
            )
        )
    authenticated = provider.authenticate(actor)
    return ok((actor, provider)) if authenticated.ok else authenticated


def configured_trust_store() -> Path:
    """Return the deployment-owned trust root; it is never accepted as a command argument."""
    return Path(os.environ.get("LOOM_TRUST_STORE", "/etc/loom/trust.json"))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["configured_trust_store", "load_identity", "main"]
