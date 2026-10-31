"""Versioned metadata for existing bound sources; no unmetered network/model work."""

import json
import os
from datetime import timedelta
from pathlib import Path

from loom.autonomy.events import ingest
from loom.autonomy.store import digest
from loom.service.contracts import ServiceError


def bounded_files(root):
    from loom.knowledge.local_sources import EXCLUDE

    if root.is_file():
        return [root]
    pending, found, count = [root], [], 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.name in EXCLUDE or entry.is_symlink():
                    continue
                count += 1
                if count > 2000:
                    raise ValueError("Source exceeds 2000 entries; narrow route")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    found.append(Path(entry.path))
    return sorted(found)


def inspect_route(store, role, route):
    documents, issues = [], []
    root = role["working_directory"]
    for name in route.get("paths", []):
        path = (Path(root) / name).resolve()
        if not path.is_relative_to(root):
            issues.append(dict(source=name, error="Path escaped working directory"))
            continue
        if not path.exists():
            documents.append(dict(id=name, state="deleted"))
            continue
        paths = bounded_files(path)
        total = 0
        for item in paths:
            if item.is_symlink() or not item.is_file():
                continue
            try:
                if item.stat().st_size > 2_000_000:
                    raise ValueError("File exceeds 2 MB")
                total += item.stat().st_size
                if total > 20_000_000:
                    issues.append(dict(source=name, error="Source exceeds 20 MB; narrow route"))
                    break
                documents.append(dict(id=str(item.relative_to(root)), digest=digest(item.read_bytes().hex()), state="current"))
            except (OSError, ValueError) as exc:
                issues.append(dict(source=str(item), error=str(exc)))
    for kid in route.get("knowledge_base_ids", []):
        try:
            base = store.sessions.knowledge.get(kid)
            if base["engine"] == "sqlite_fts":
                # Reuse registered path/document identities. FTS has no remote,
                # embedding or model calls; metered backends remain deferred.
                from loom.knowledge.local_sources import run

                knowledge = store.sessions.knowledge
                with knowledge.connect() as db:
                    local = [dict(r) for r in db.execute("SELECT * FROM local_sources WHERE kb_id=?", (kid,))]
                for source in local:
                    path = Path(source["path"])
                    if not root or not path.resolve().is_relative_to(root):
                        continue
                    if not path.exists():
                        knowledge.remove_document(kid, source["document_id"])
                    else:
                        run(
                            knowledge,
                            kid,
                            dict(files=[str(path)], completed={str(path): source["digest"]}),
                            lambda **_kw: None,
                            lambda: False,
                            lambda _value: None,
                        )
                base = knowledge.get(kid)
            documents.extend(dict(id=f"{kid}:{d['id']}", digest=d.get("digest"), state="current") for d in base["documents"])
            if base["engine"] != "sqlite_fts":
                issues.append(dict(source=kid, error="Automatic indexing deferred: backend has no per-call Agent metering adapter", metadata_available=True))
        except ServiceError as exc:
            documents.append(dict(id=kid, state="deleted" if exc.status == 404 else "unavailable", reason=str(exc)))
    for tid in route.get("external_tool_ids", []):
        # Credentials and execution remain in the existing connector manager.
        try:
            store.sessions.external_tools.resolve([tid])
            documents.append(dict(id=tid, state="requires_revalidation", reason="Read through bound tool at use time"))
        except ServiceError as exc:
            documents.append(dict(id=tid, state="unavailable", reason=str(exc)))
    return documents, issues


def refresh(store, aid, *, force=False):
    agent = store.snapshot(aid)
    if agent["state"] != "enabled":
        return []
    results = []
    for route in agent["role"]["source_routes"]:
        if "refresh_seconds" not in route and not force:
            continue
        identifier = "source_" + digest([aid, route["id"]])[:32]
        with store.sessions.transaction() as db:
            saved = db.execute("SELECT body FROM agent_sources WHERE id=?", (identifier,)).fetchone()
            previous = json.loads(saved[0]) if saved else {}
        if not force and previous.get("next_refresh_at", "") > store.now():
            continue
        try:
            documents, issues = inspect_route(store, agent["role"], route)
        except (OSError, ValueError, ServiceError) as exc:
            documents, issues = [], [dict(source=route["id"], error=str(exc))]
        work_states = {w["id"]: w["state"] for w in agent["work_packages"]}
        own_change = (
            bool(previous) and previous.get("work_states", {}) != work_states or any(w["state"] in {"running", "dispatched"} for w in agent["work_packages"])
        )
        fingerprint = digest(documents)
        changed = previous.get("digest") != fingerprint
        state = "partial" if issues else "current"
        if any(d["state"] != "current" for d in documents) or not documents:
            state = "unavailable" if not any(d["state"] == "current" for d in documents) else "partial"
        row = dict(
            id=identifier,
            agent_id=aid,
            route_id=route["id"],
            revision=previous.get("revision", 0) + int(changed),
            state=state,
            digest=fingerprint,
            documents=documents,
            issues=issues,
            checked_at=store.now(),
            next_refresh_at=(store.clock() + timedelta(seconds=route.get("refresh_seconds", 86400))).isoformat(),
            expires_at=(store.clock() + timedelta(seconds=route.get("refresh_seconds", 86400))).isoformat(),
            last_success=previous.get("last_success"),
            role_revision=agent["role_revision"],
        )
        row["work_states"] = work_states
        if state == "current":
            row["last_success"] = dict(at=store.now(), digest=fingerprint, documents=documents)
        with store.sessions.transaction() as db:
            latest = store.get(db, "agents", aid)
            if latest["role_revision"] != agent["role_revision"] or latest["state"] != "enabled":
                continue
            store.put(db, "agent_sources", row)
            if changed:
                latest.update(guidance_version=latest["guidance_version"] + 1, generation=latest["generation"] + 1, next_wake_at=store.now())
                store.put(db, "agents", latest)
        if changed:
            ingest(
                store,
                aid,
                dict(
                    command_id=f"refresh:{identifier}:{row['revision']}",
                    source_id=route["id"],
                    event_id=f"{identifier}:{row['revision']}",
                    object_id=route["id"],
                    version=row["revision"],
                    occurred_at=store.now(),
                    cursor=str(row["revision"]),
                    origin=aid if own_change else "bound_source",
                    depth=0,
                    type="changed",
                    summary=f"Bound source version changed; coverage={state}",
                ),
                producer="bound_source",
            )
        results.append(row)
    return results


def view(store, db, agent):
    rows = store.rows(db, "agent_sources", agent["id"])
    bound = {r["id"]: r for r in agent["role"]["source_routes"]}
    result = []
    for row in rows:
        if row["route_id"] not in bound:
            continue
        value = {k: v for k, v in row.items() if k not in {"documents", "last_success"}}
        value["revalidate_before_use"] = row["state"] != "current" or row["expires_at"] <= store.now() or row["role_revision"] != agent["role_revision"]
        value["fallback_route_ids"] = [r for r in bound[row["route_id"]].get("fallback_route_ids", []) if r in bound]
        value["invalid_document_ids"] = [d["id"] for d in row["documents"] if d["state"] != "current"]
        result.append(value)
    return result
