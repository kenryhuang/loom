"""Explicit cross-workspace method candidates, never cross-workspace raw memory."""

from copy import deepcopy
from pathlib import Path

from loom.core import now_iso
from loom.skills.contracts import canonical, digest


def promote(store, version_id, *, revision, target_workspace, conditions, environment):
    if not isinstance(target_workspace, (str, Path)):
        raise ValueError("Promotion requires an explicit target workspace")
    target = str(Path(target_workspace).expanduser().resolve())
    if (
        not Path(target).is_dir()
        or not isinstance(conditions, list)
        or not 1 <= len(conditions) <= 8
        or any(not isinstance(c, str) or not c.strip() or len(c) > 1600 for c in conditions)
    ):
        raise ValueError("Promotion requires an existing target workspace and parameterized applicability conditions")
    keys = {
        "environment_digest",
        "solver_digest",
        "tools_digest",
        "permissions_digest",
        "evaluator_digest",
        "knowledge_digest",
        "memory_digest",
        "budget_digest",
    }
    if not isinstance(environment, dict) or set(environment) != keys or any(not isinstance(v, str) or len(v) != 64 for v in environment.values()):
        raise ValueError("Promotion requires a frozen target environment")
    with store.transaction() as db:
        source = store.version(version_id, db=db)
        if source["revision"] != revision or source["state"] not in {"active", "trial", "candidate"}:
            raise ValueError("Source version changed or is unavailable")
        if not store.dependencies_valid(version_id, db=db) or target == source["workspace"]:
            raise ValueError("Promotion requires valid sources and a distinct target workspace")
        identifier = "skillv:" + digest([version_id, revision, target, conditions, environment])[:32]
        body = deepcopy(source)
        body.update(
            id=identifier,
            skill_id="skill:" + digest([source["skill_id"], target])[:24],
            workspace=target,
            state="candidate",
            revision=1,
            evidence_state="proposed",
            validation={"status": "not_executed"},
            created_at=now_iso(),
            parent_version_id=version_id,
            promotion=dict(
                source_version_id=version_id,
                source_revision=revision,
                source_workspace=source["workspace"],
                target_workspace=target,
                conditions=conditions,
                environment=environment,
            ),
        )
        body["contract"]["preconditions"] = [*body["contract"]["preconditions"], *conditions]
        for claim in body["contract"].get("claims", []):
            # Target users receive the method and lineage, not original excerpts.
            claim.pop("evidence_refs", None)
        body["limitations"] = [
            *body.get("limitations", []),
            "Target workspace requires independent executable validation; source success is not transferable proof.",
        ]
        db.execute("INSERT OR IGNORE INTO skill_versions VALUES(?,?,?,'candidate',1,?)", (identifier, body["skill_id"], target, canonical(body)))
        db.execute("INSERT OR IGNORE INTO skill_version_dependencies VALUES(?,?,?)", (identifier, version_id, revision))
        # Existing case validation stays authoritative; no target case/memory copies.
        db.execute(
            "INSERT OR IGNORE INTO skill_dependencies SELECT ?,case_id,case_revision FROM skill_dependencies WHERE version_id=?", (identifier, version_id)
        )
    return store.version(identifier)
