"""Authenticated manual candidate creation and import commands."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from loom.campaigns.cli import configured_trust_store, load_identity
from loom.campaigns.config import load_resolved_campaign_spec
from loom.campaigns.contracts import ArtifactRef, ExperimentPhase
from loom.campaigns.controller import CampaignController
from loom.campaigns.operations import BudgetReservation, CampaignOperation
from loom.campaigns.serialization import canonical_digest, canonical_json_bytes, new_prefixed_id, utc_now
from loom.campaigns.store import SQLiteCampaignStore
from loom.core import Result, err, make_loom_error, ok, thaw_json

_MAX_IMPORT_BYTES = 4 * 1024 * 1024


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = asyncio.run(_execute(args))
    if result.ok:
        payload = result.value
        print(json.dumps(payload, sort_keys=True) if args.json else "\n".join(f"{key}: {value}" for key, value in payload.items()))
        return 0
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


async def _execute(args) -> Result:
    identity = load_identity(args.identity, configured_trust_store())
    if not identity.ok:
        return identity
    actor, provider = identity.value
    store = SQLiteCampaignStore(args.campaign_dir, provider)
    projection = await store.load(args.campaign_id)
    if not projection.ok:
        return projection
    if args.command == "validate":
        spec_ref = await store.spec_ref(args.campaign_id)
        if not spec_ref.ok:
            return spec_ref
        spec = load_resolved_campaign_spec(Path(args.campaign_dir) / "campaign.json", store.artifacts, spec_ref.value)
        if not spec.ok:
            return spec
        refs = tuple(_artifact_ref_argument(value) for value in (args.candidate_ref, args.validation_ref))
        if any(not ref.ok for ref in refs):
            return next(ref for ref in refs if not ref.ok)
        experiment = None if args.experiment_ref is None else _artifact_ref_argument(args.experiment_ref)
        if experiment is not None and not experiment.ok:
            return experiment
        controller = CampaignController(store, spec.value, controller_actor=actor, finalizer_actor=actor)
        recorded = await controller.record_candidate(
            args.candidate_id,
            candidate_ref=refs[0].value,
            validation_ref=refs[1].value,
            experiment_ref=None if experiment is None else experiment.value,
        )
        if not recorded.ok:
            return recorded
        return ok(
            {
                "schema_version": "loom.candidate-validation-cli.v1",
                "campaign_id": args.campaign_id,
                "candidate_id": args.candidate_id,
                "aggregate_version": recorded.value.aggregate_version,
            }
        )
    if args.command == "create":
        materialized = _manual_candidate_payload(args)
    else:
        materialized = _imported_candidate_payload(Path(args.candidate_dir), args.campaign_id, store.artifacts)
    if not materialized.ok:
        return materialized
    payload, candidate_id = materialized.value
    published = store.artifacts.publish_bytes(
        canonical_json_bytes(payload),
        kind="candidate_bundle",
        schema_version="loom.candidate-bundle.v1",
        suffix=".json",
    )
    if not published.ok:
        return published
    operation = CampaignOperation(
        args.operation_id or new_prefixed_id("op_"),
        args.campaign_id,
        canonical_digest(payload),
        "candidate.created",
        actor=actor,
        payload={"candidate_id": candidate_id, "candidate_ref": published.value, "source": "manual"},
        output_refs=(published.value,),
        reservation=BudgetReservation(ExperimentPhase.DISCOVERY, candidates=1),
    )
    committed = await store.transact(operation, projection.value.aggregate_version)
    if not committed.ok:
        return committed
    return ok(
        {
            "schema_version": "loom.candidate-cli.v1",
            "campaign_id": args.campaign_id,
            "candidate_id": candidate_id,
            "operation_id": committed.value.operation_id,
            "aggregate_version": committed.value.aggregate_version,
            "artifact_ref": thaw_json(json.loads(canonical_json_bytes(published.value))),
        }
    )


def _manual_candidate_payload(args) -> Result:
    try:
        patch = json.loads(Path(args.patch).read_text(encoding="utf-8"))
        hypothesis = Path(args.hypothesis).read_text(encoding="utf-8")
        if not isinstance(patch, Mapping | list) or not hypothesis.strip():
            raise ValueError("patch must be JSON and hypothesis must be non-empty")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return _candidate_error("Manual candidate inputs are invalid", exc)
    seed = {"campaign_id": args.campaign_id, "patch": patch, "hypothesis": hypothesis}
    candidate_id = args.candidate_id or new_prefixed_id("cand_")
    return ok(
        (
            {
                "schema_version": "loom.candidate-bundle.v1",
                "candidate_id": candidate_id,
                "campaign_id": args.campaign_id,
                "created_at": utc_now(),
                "kind": "declarative_patch",
                "source": "manual",
                "hypothesis": hypothesis,
                "patch": patch,
                "input_digest": canonical_digest(seed),
            },
            candidate_id,
        )
    )


def _imported_candidate_payload(root: Path, campaign_id: str, artifacts) -> Result:
    try:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("candidate import path must be a directory")
        manifest_path = resolved_root / "candidate.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            raise ValueError("candidate.json must be an object")
        candidate_id = str(manifest.get("candidate_id") or new_prefixed_id("cand_"))
        if not candidate_id.startswith("cand_"):
            raise ValueError("candidate_id must use the cand_ prefix")
        declared_campaign = manifest.get("campaign_id")
        if declared_campaign is not None and declared_campaign != campaign_id:
            raise ValueError("candidate campaign_id does not match the destination campaign")
        files = []
        total = 0
        for path in sorted(resolved_root.rglob("*")):
            if path.is_symlink():
                raise ValueError("candidate imports cannot contain symlinks")
            if not path.is_file():
                continue
            relative = path.relative_to(resolved_root).as_posix()
            content = path.read_bytes()
            total += len(content)
            if total > _MAX_IMPORT_BYTES:
                raise ValueError("candidate import exceeds its byte limit")
            published = artifacts.publish_bytes(
                content,
                kind="candidate_source",
                schema_version="loom.candidate-source.v1",
                suffix=path.suffix,
            )
            if not published.ok:
                return published
            files.append({"path": relative, "artifact_ref": published.value})
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return _candidate_error("Imported candidate directory is invalid", exc)
    return ok(
        (
            {
                "schema_version": "loom.candidate-bundle.v1",
                "candidate_id": candidate_id,
                "campaign_id": campaign_id,
                "created_at": utc_now(),
                "kind": str(manifest.get("kind", "declarative_patch")),
                "source": "manual_import",
                "manifest": manifest,
                "files": files,
            },
            candidate_id,
        )
    )


def _candidate_error(message: str, cause: BaseException) -> Result:
    return err(
        make_loom_error(
            "CANDIDATE_MANIFEST_INVALID",
            message,
            retryable=False,
            cause={"name": type(cause).__name__, "message": str(cause)},
        )
    )


def _artifact_ref_argument(value: str) -> Result:
    try:
        path = Path(value)
        raw = path.read_text(encoding="utf-8") if path.is_file() else value
        return ok(ArtifactRef(**json.loads(raw)))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return _candidate_error("Candidate evidence reference is invalid", exc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loom candidate")
    children = parser.add_subparsers(dest="command", required=True)
    create = children.add_parser("create")
    create.add_argument("campaign_id")
    create.add_argument("--campaign-dir", required=True)
    create.add_argument("--patch", required=True)
    create.add_argument("--hypothesis", required=True)
    create.add_argument("--candidate-id")
    imported = children.add_parser("import")
    imported.add_argument("campaign_id")
    imported.add_argument("candidate_dir")
    imported.add_argument("--campaign-dir", required=True)
    validate = children.add_parser("validate")
    validate.add_argument("campaign_id")
    validate.add_argument("candidate_id")
    validate.add_argument("--campaign-dir", required=True)
    validate.add_argument("--candidate-ref", required=True)
    validate.add_argument("--validation-ref", required=True)
    validate.add_argument("--experiment-ref")
    for child in (create, imported, validate):
        child.add_argument("--operation-id")
        child.add_argument("--identity", required=True)
        child.add_argument("--json", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
