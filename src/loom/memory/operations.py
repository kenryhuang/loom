"""Explicit version operations with optimistic concurrency and audit receipts."""

import json
from contextlib import nullcontext
from uuid import uuid4

from loom.memory.query import available
from loom.memory.repository import Repository, digest, now, timestamp
from loom.memory.store import MemoryError, serialized


class MemoryOperations(Repository):
    def execute(self, ns, operation, payload, *, command_id, actor="human", _db=None):
        with nullcontext(_db) if _db is not None else self.connect(write=True) as db:
            previous = db.execute("SELECT body FROM memory_events WHERE id=?", (command_id,)).fetchone()
            request = dict(operation=operation, payload=payload, actor=actor)
            if previous:
                body = json.loads(previous[0])
                if body.get("request") != request:
                    raise MemoryError("memory.command_conflict", "Command id already used with different input")
                return body["result"]
            item = self.checked(db, ns, payload["memory_id"], payload["expected_revision"])
            version = payload.get("version_id")
            if not version:
                version = db.execute("SELECT id FROM memory_item_versions WHERE memory_id=? ORDER BY revision DESC LIMIT 1", (item["id"],)).fetchone()[0]
            row = db.execute("SELECT * FROM memory_item_versions WHERE id=? AND memory_id=?", (version, item["id"])).fetchone()
            if not row:
                raise MemoryError("memory.not_found", "Version does not belong to this knowledge")
            before = dict(
                validities=[dict(r) for r in db.execute("SELECT * FROM memory_validity WHERE version_id=? AND recorded_until IS NULL", (version,))],
                redirects={item["id"]: item["canonical_redirect"]},
            )
            touched = [version]
            result = dict(memory_id=item["id"], version_id=version)
            if operation == "NOOP":
                result["revision"] = item["latest_revision"]
                self.event(db, ns, operation, dict(request=request, result=result, before=before), key=command_id)
                return result
            if operation == "ATTACH_SUPPORT":
                observations = payload.get("observation_ids", [])
                if not observations or len(observations) > 32:
                    raise ValueError("Supply 1–32 existing scoped evidence observations")
                bundle = "bundle:" + digest(command_id)
                for observation in set(observations):
                    evidence = db.execute(
                        """SELECT src.namespace_id,src.available FROM memory_observations o
                        JOIN memory_sources src ON src.id=o.source_id WHERE o.id=?""",
                        (observation,),
                    ).fetchone()
                    if not evidence or evidence[0] != ns or not evidence[1]:
                        raise ValueError("Support must reference available observations in the same namespace")
                    db.execute("INSERT OR IGNORE INTO memory_supports VALUES(?,?,?,?)", (version, observation, bundle, "supports"))
                self.refresh_support(db, [version])
                db.execute("UPDATE memory_items SET latest_revision=latest_revision+1 WHERE id=?", (item["id"],))
            elif operation == "CONFLICT":
                other = payload["other_version_id"]
                target = db.execute(
                    """SELECT i.namespace_id,i.claim_slot FROM memory_items i
                    JOIN memory_item_versions v ON v.memory_id=i.id WHERE v.id=?""",
                    (other,),
                ).fetchone()
                if not target or target["namespace_id"] != ns or not available(db, other):
                    raise ValueError("Conflict member must be available in the same namespace")
                result["conflict_id"] = self.conflict(db, ns, item["claim_slot"], [version, other])
                for vid in [version, other]:
                    db.execute(
                        "UPDATE memory_items SET latest_revision=latest_revision+1 WHERE id=(SELECT memory_id FROM memory_item_versions WHERE id=?)", (vid,)
                    )
                touched.append(other)
            elif operation == "SPLIT":
                records = payload.get("records", [])
                if not 2 <= len(records) <= 16:
                    raise ValueError("Split requires 2–16 atomic records with preserved evidence")
                original = json.loads(row["record"])
                source = json.loads(db.execute("SELECT source FROM memory_projection WHERE version_id=?", (version,)).fetchone()[0])
                result["children"] = []
                from loom.memory.repository import claim_slot

                for record in records:
                    record = {**original, **record}
                    if record.get("kind") != item["kind"] or record.get("scope") != original.get("scope"):
                        raise ValueError("Split cannot broaden or silently change scope/kind")
                    if not all(isinstance(record.get(k), str) and record[k] for k in ("title", "summary")):
                        raise ValueError("Split records require a title and summary")
                    conditions = (record.get("claim") or {}).get("conditions", {})
                    if any(conditions.get(k) != v for k, v in (original.get("claim") or {}).get("conditions", {}).items()):
                        raise ValueError("Split cannot drop original applicability conditions")
                    child = "mem:" + uuid4().hex
                    db.execute("INSERT INTO memory_items VALUES(?,?,?,?,0,NULL,?)", (child, ns, item["kind"], claim_slot(record), now()))
                    new_version = self.add_version(db, ns, child, record, source)
                    db.execute(
                        "INSERT INTO memory_supports SELECT ?,observation_id,bundle_id,polarity FROM memory_supports WHERE version_id=?", (new_version, version)
                    )
                    self.refresh_support(db, [new_version])
                    result["children"].append(dict(memory_id=child, version_id=new_version))
                    touched.append(new_version)
                self.validity(db, version, lifecycle="superseded")
                db.execute("UPDATE memory_items SET latest_revision=latest_revision+1 WHERE id=?", (item["id"],))
            elif operation in {"EXPIRE", "RETIRE", "RESTORE", "CONFIRM", "PIN", "TAG"}:
                if operation == "EXPIRE":
                    if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
                        raise ValueError("Expiry requires a verification reason")
                    # Staleness is not a human rejection and must not create a
                    # permanent denial tombstone for a potentially recurring fact.
                    self.validity(db, version, freshness="expired")
                elif operation == "RETIRE":
                    self.validity(db, version, lifecycle="retired")
                    db.execute("INSERT OR REPLACE INTO memory_tombstones VALUES(?,?,?)", (ns, row["signature"], payload.get("reason", "human rejection")))
                elif operation == "RESTORE":
                    db.execute("DELETE FROM memory_tombstones WHERE namespace_id=? AND signature=?", (ns, row["signature"]))
                    self.validity(db, version, lifecycle="active")
                elif operation == "CONFIRM":
                    db.execute("UPDATE memory_projection SET review_state='confirmed' WHERE version_id=?", (version,))
                elif operation == "PIN":
                    db.execute("UPDATE memory_projection SET pinned=? WHERE version_id=?", (bool(payload.get("value", True)), version))
                elif operation == "TAG":
                    tags = json.loads(db.execute("SELECT tags FROM memory_projection WHERE version_id=?", (version,)).fetchone()[0])
                    tag = payload.get("value")
                    if not isinstance(tag, str) or not tag.strip() or len(tag) > 4000:
                        raise ValueError("Tag must contain 1–4000 characters")
                    db.execute("UPDATE memory_projection SET tags=? WHERE version_id=?", (serialized(list(dict.fromkeys([*tags, tag]))), version))
                db.execute("UPDATE memory_items SET latest_revision=latest_revision+1 WHERE id=?", (item["id"],))
            elif operation in {"REVISE", "SUPERSEDE"}:
                record = payload["record"]
                if record.get("kind") != item["kind"] or not all(isinstance(record.get(k), str) for k in ("title", "summary", "scope")):
                    raise ValueError("Revision must preserve kind and provide title, summary and scope")
                effective = timestamp(payload.get("effective_at"))
                if operation == "SUPERSEDE" and not effective:
                    raise ValueError("Superseding a temporal fact requires an explicit effective_at")
                source = json.loads(db.execute("SELECT source FROM memory_projection WHERE version_id=?", (version,)).fetchone()[0])
                record = {**record, "basis": "human_asserted" if actor == "human" else record.get("basis", "inferred")}
                if effective:
                    record["valid_from"] = effective
                    old = before["validities"][0]
                    if old["valid_from"] and effective <= old["valid_from"]:
                        raise ValueError("Late observations cannot supersede a later effective version; resolve the historical interval explicitly")
                    self.validity(db, version, valid_from=old["valid_from"], valid_to=effective, replace_interval=True)
                else:
                    self.validity(db, version, lifecycle="superseded")
                new_version = self.add_version(db, ns, item["id"], record, source)
                # Explicit human statements are new evidence; never relabel old evidence observed.
                human_source = "ms:human:" + digest(command_id)
                human_obs = "mo:human:" + digest(command_id)
                db.execute(
                    "INSERT INTO memory_sources VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        human_source,
                        ns,
                        source["session_id"],
                        source["evaluation_id"],
                        source["record_id"],
                        "human:" + digest(command_id),
                        human_source,
                        1,
                        serialized(source),
                    ),
                )
                db.execute(
                    "INSERT INTO memory_observations VALUES(?,?,?,?,?,?)",
                    (human_obs, human_source, human_source, now(), serialized(dict(command_id=command_id, actor=actor)), serialized(record)),
                )
                db.execute("INSERT INTO memory_supports VALUES(?,?,?,?)", (new_version, human_obs, human_obs, "supports"))
                self.refresh_support(db, [new_version])
                db.execute("UPDATE memory_projection SET review_state='corrected' WHERE version_id=?", (new_version,))
                result["version_id"] = new_version
                touched.append(new_version)
            elif operation == "MERGE":
                target = self.checked(db, ns, payload["target_memory_id"], payload["target_revision"])
                if target["id"] == item["id"] or target["kind"] != item["kind"]:
                    raise ValueError("Merge requires distinct, same-kind knowledge with identical applicability slots")
                target_version = db.execute("SELECT id FROM memory_item_versions WHERE memory_id=? ORDER BY revision DESC LIMIT 1", (target["id"],)).fetchone()[
                    0
                ]
                target_content = json.loads(db.execute("SELECT record FROM memory_item_versions WHERE id=?", (target_version,)).fetchone()[0])
                original_content = json.loads(row["record"])
                scope_review = payload.get("scope_equivalence")
                same_scope = target_content.get("scope") == original_content.get("scope")
                if scope_review is not None:
                    # An explicit reviewed alias is bound to the exact scopes and
                    # optimistic revisions. Preserve both immutable versions;
                    # never normalize arbitrary scope text during ingestion.
                    if (
                        not isinstance(scope_review, dict)
                        or set(scope_review) != {"source", "target", "reason"}
                        or scope_review["source"] != original_content.get("scope")
                        or scope_review["target"] != target_content.get("scope")
                        or not isinstance(scope_review["reason"], str)
                        or not scope_review["reason"].strip()
                        or actor == "calibrated_consolidator"
                    ):
                        raise ValueError("Scope equivalence requires an explicit review of the exact source and target scopes")
                    same_scope = True
                if (
                    not same_scope
                    or target_content.get("claim", {}).get("conditions", {}) != original_content.get("claim", {}).get("conditions", {})
                    or bool(target_content.get("claim")) != bool(original_content.get("claim"))
                    or original_content.get("claim")
                    and target["claim_slot"] != item["claim_slot"]
                ):
                    raise ValueError("Merge cannot discard different applicability or claim slots")
                before["redirects"][target["id"]] = target["canonical_redirect"]
                before["target_supports"] = [dict(r) for r in db.execute("SELECT * FROM memory_supports WHERE version_id=?", (target_version,))]
                before["target_version"] = target_version
                db.execute(
                    "INSERT OR IGNORE INTO memory_supports SELECT ?,observation_id,bundle_id,polarity FROM memory_supports WHERE version_id=?",
                    (target_version, version),
                )
                db.execute("UPDATE memory_items SET canonical_redirect=?,latest_revision=latest_revision+1 WHERE id=?", (target["id"], item["id"]))
                db.execute("UPDATE memory_items SET latest_revision=latest_revision+1 WHERE id=?", (target["id"],))
                self.validity(db, version, lifecycle="merged")
                self.refresh_support(db, [target_version])
                result.update(memory_id=target["id"], version_id=target_version)
                touched.append(target_version)
            elif operation == "RESOLVE":
                group = db.execute("SELECT * FROM memory_conflicts WHERE id=? AND namespace_id=? AND state=?", (payload["conflict_id"], ns, "open")).fetchone()
                members = [r[0] for r in db.execute("SELECT version_id FROM memory_conflict_members WHERE conflict_id=?", (payload["conflict_id"],))]
                if not group or version not in members or not payload.get("reason"):
                    raise ValueError("Resolution requires an open conflict, a member and an evidence-based reason")
                before["validities"] = [
                    dict(r) for vid in members for r in db.execute("SELECT * FROM memory_validity WHERE version_id=? AND recorded_until IS NULL", (vid,))
                ]
                for vid in members:
                    self.validity(db, vid, lifecycle="active" if vid == version else "retired")
                    db.execute(
                        "UPDATE memory_items SET latest_revision=latest_revision+1 WHERE id=(SELECT memory_id FROM memory_item_versions WHERE id=?)", (vid,)
                    )
                db.execute("UPDATE memory_conflicts SET state='resolved',resolution=? WHERE id=?", (serialized(payload), group["id"]))
                touched = members
            elif operation == "DEPEND":
                dependency = payload["depends_on"]
                target = db.execute("SELECT namespace_id FROM memory_projection WHERE version_id=?", (dependency,)).fetchone()
                if not target or target[0] != ns or not available(db, dependency):
                    raise ValueError("Dependency must be available in the same namespace")
                reachable = db.execute(
                    """WITH RECURSIVE deps(id) AS (SELECT ? UNION
                    SELECT r.to_version FROM memory_relations r JOIN deps d ON r.from_version=d.id WHERE r.kind='depends_on')
                    SELECT 1 FROM deps WHERE id=?""",
                    (dependency, version),
                ).fetchone()
                if reachable:
                    raise ValueError("Memory evidence dependencies cannot form a cycle")
                db.execute(
                    "INSERT INTO memory_relations VALUES(?,?,?,?,?,?)", ("mr:" + uuid4().hex, ns, "depends_on", version, dependency, serialized(payload))
                )
                db.execute("UPDATE memory_items SET latest_revision=latest_revision+1 WHERE id=?", (item["id"],))
            else:
                raise ValueError("Unsupported memory operation")
            db.execute(
                "INSERT INTO memory_reviews VALUES(?,?,?,?,?,?,?)",
                ("review:" + digest(command_id), ns, result["version_id"], operation.lower(), actor, serialized(payload), now()),
            )
            result["revision"] = db.execute("SELECT latest_revision FROM memory_items WHERE id=?", (result["memory_id"],)).fetchone()[0]
            self.event(db, ns, operation, dict(request=request, result=result, before=before), key=command_id)
            self.bump(db, ns, touched)
            return result

    def undo_merge(self, ns, event_id, *, command_id):
        with self.connect(write=True) as db:
            replay = db.execute("SELECT body FROM memory_events WHERE id=?", (command_id,)).fetchone()
            if replay:
                return json.loads(replay[0])["result"]
            row = db.execute("SELECT body FROM memory_events WHERE id=? AND namespace_id=? AND operation='MERGE'", (event_id, ns)).fetchone()
            if not row:
                raise ValueError("Merge event not found")
            event = json.loads(row[0])
            source = event["request"]["payload"]["memory_id"]
            target = event["result"]["memory_id"]
            self.checked(db, ns, target, event["result"]["revision"])
            actual = db.execute("SELECT canonical_redirect,latest_revision FROM memory_items WHERE id=?", (source,)).fetchone()
            if actual[0] != target or actual[1] != event["request"]["payload"]["expected_revision"] + 1:
                raise ValueError("Merged knowledge changed; explicit reconciliation required")
            for identifier, redirect in event["before"]["redirects"].items():
                db.execute("UPDATE memory_items SET canonical_redirect=?,latest_revision=latest_revision+1 WHERE id=?", (redirect, identifier))
            touched = []
            for validity in event["before"]["validities"]:
                self.validity(db, validity["version_id"], lifecycle=validity["lifecycle"])
                touched.append(validity["version_id"])
            target_version = event["before"]["target_version"]
            db.execute("DELETE FROM memory_supports WHERE version_id=?", (target_version,))
            for support in event["before"]["target_supports"]:
                db.execute("INSERT INTO memory_supports VALUES(?,?,?,?)", tuple(support[k] for k in ("version_id", "observation_id", "bundle_id", "polarity")))
            self.refresh_support(db, [target_version])
            result = dict(restored_memory_id=source)
            self.event(db, ns, "UNDO_MERGE", dict(undoes=event_id, result=result), key=command_id)
            self.bump(db, ns, [*touched, target_version])
            return result
