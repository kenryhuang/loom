"""Task-set provenance fingerprint and contamination CLI."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from loom.campaigns.contracts import ExperimentPhase
from loom.campaigns.serialization import canonical_json_bytes
from loom.campaigns.task_sets import fingerprint_task_rows, load_task_manifest, validate_task_set_isolation
from loom.core import thaw_json


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sources = [args.manifest, *getattr(args, "against", [])]
    fingerprinted = []
    for index, source in enumerate(sources):
        loaded = load_task_manifest(source)
        if not loaded.ok:
            return _print_error(loaded, args.json)
        role = ExperimentPhase(args.role) if args.command == "fingerprint" else (ExperimentPhase.DISCOVERY if index == 0 else ExperimentPhase.VALIDATION)
        result = fingerprint_task_rows(loaded.value, role)
        if not result.ok:
            return _print_error(result, args.json)
        fingerprinted.append(result.value)
    result = fingerprinted[0]
    if args.command == "validate":
        for comparison in fingerprinted[1:]:
            checked = validate_task_set_isolation((fingerprinted[0], comparison), threshold=args.threshold)
            if not checked.ok:
                return _print_error(checked, args.json)
    payload = (
        json.loads(canonical_json_bytes(result))
        if args.command == "fingerprint"
        else {
            "schema_version": "loom.task-set-validation.v1",
            "valid": True,
            "fingerprint_digests": [item.fingerprint_digest for item in fingerprinted],
        }
    )
    print(json.dumps(payload, sort_keys=True) if args.json else _human(payload))
    return 0


def _print_error(result, as_json: bool) -> int:
    payload = {
        "ok": False,
        "error": {
            "code": result.error.code,
            "message": result.error.message,
            "metadata": None if result.error.metadata is None else thaw_json(result.error.metadata),
        },
    }
    print(json.dumps(payload, sort_keys=True) if as_json else f"{result.error.code}: {result.error.message}")
    return 1


def _human(payload) -> str:
    return "\n".join(f"{key}: {value}" for key, value in payload.items())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loom task-set")
    children = parser.add_subparsers(dest="command", required=True)
    fingerprint = children.add_parser("fingerprint")
    fingerprint.add_argument("manifest")
    fingerprint.add_argument("--role", choices=("discovery", "validation", "holdout"), default="discovery")
    validate = children.add_parser("validate")
    validate.add_argument("manifest")
    validate.add_argument("--against", action="append", required=True)
    validate.add_argument("--threshold", type=float, default=0.8)
    for child in (fingerprint, validate):
        child.add_argument("--json", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
