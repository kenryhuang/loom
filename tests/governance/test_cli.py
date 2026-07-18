from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from loom.campaigns.artifacts import ArtifactStore
from loom.governance.cli import main
from loom.governance.policy import ActorAssertion, StaticIdentityProvider
from loom.governance.registry import SQLiteGovernanceStore

from .conftest import governance_admin


def _actor(role: str) -> ActorAssertion:
    return ActorAssertion(
        role,
        (role,),
        "2026-07-18T00:00:00.000000Z",
        "2999-01-01T00:00:00.000000Z",
        f"signed-{role}",
    )


def _identity(path: Path, actor: ActorAssertion) -> Path:
    assertion = {
        "subject": actor.subject,
        "roles": list(actor.roles),
        "issued_at": actor.issued_at,
        "expires_at": actor.expires_at,
        "signature": actor.signature,
    }
    path.write_text(json.dumps({"actor": assertion}), encoding="utf-8")
    trust_path = path.with_name("trust.json")
    trust = json.loads(trust_path.read_text(encoding="utf-8")) if trust_path.exists() else {"trusted_assertions": []}
    trust["trusted_assertions"] = [
        item for item in trust["trusted_assertions"] if (item["subject"], item["signature"]) != (assertion["subject"], assertion["signature"])
    ] + [assertion]
    trust_path.write_text(json.dumps(trust), encoding="utf-8")
    os.environ["LOOM_TRUST_STORE"] = str(trust_path)
    return path


def test_governance_cli_lists_verified_active_versions_and_records_review(tmp_path: Path, capsys):
    governance_dir = tmp_path / "governance"
    artifact_root = tmp_path / "artifacts"
    artifacts = ArtifactStore(artifact_root)
    baseline = artifacts.publish_bytes(b"baseline", kind="baseline", schema_version="baseline.v1").unwrap()
    operator = _actor("registry_operator")
    admin = governance_admin()

    async def setup():
        store = SQLiteGovernanceStore(governance_dir, StaticIdentityProvider((operator, admin)), artifacts)
        return await store.initialize_surface("context_policy", baseline, admin)

    assert asyncio.run(setup()).ok
    operator_identity = _identity(tmp_path / "operator.json", operator)
    common = [
        "--governance-dir",
        str(governance_dir),
        "--artifact-root",
        str(artifact_root),
        "--identity",
        str(operator_identity),
        "--json",
    ]
    assert main(["active", "--surface", "context_policy", *common]) == 0
    active = json.loads(capsys.readouterr().out)
    assert active[0]["artifact"]["sha256"] == baseline.sha256

    approver = _actor("governance_approver")
    approver_identity = _identity(tmp_path / "approver.json", approver)
    review_common = [
        "--governance-dir",
        str(governance_dir),
        "--artifact-root",
        str(artifact_root),
        "--identity",
        str(approver_identity),
        "--json",
    ]
    digests = [
        "--candidate-digest",
        "c" * 64,
        "--baseline-digest",
        baseline.sha256,
        "--policy-digest",
        "p" * 64,
        "--risk-rules-digest",
        "r" * 64,
        "--gate-digest",
        "g" * 64,
        "--expires-at",
        "2999-01-01T00:00:00.000000Z",
    ]
    assert main(["approve", "cand_test", *digests, *review_common]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["decision"] == "approve"
    assert main(["review", "cand_test", *review_common]) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert reviews[0]["approval"]["candidate_digest"] == "c" * 64
