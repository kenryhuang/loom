"""Transactional canonical knowledge, immutable evidence and reversible changes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from loom.memory.store import MemoryError, canonical_path, serialized, tokens


def digest(value):
    return hashlib.sha256(serialized(value).encode()).hexdigest()


def now():
    return datetime.now(UTC).isoformat()


def timestamp(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Memory timestamps require a timezone")
    return parsed.astimezone(UTC).isoformat()


def namespace_id(workspace=None, session_id=""):
    scope = canonical_path(workspace)
    return "mns:" + digest(["workspace", scope] if scope else ["session", session_id])[:32]


# Provenance and presentation do not define the identity of knowledge.
PROVENANCE = {
    "id",
    "unit_id",
    "run_id",
    "source_digest",
    "graph_revision",
    "evidence_refs",
    "citations",
    "goal_ids",
    "stage_ids",
    "action_ids",
    "normalizations",
    "publication_state",
    "human_review",
    "human_confirmed",
    "pinned",
    "tags",
    "confidence",
    "observed_at",
    "valid_from",
    "valid_to",
    "revalidate_after",
    "candidate_id",
    "provenance",
    "provenance_status",
    "slice_action_ids",
    "source_record_ids",
    "history",
}


def signature(record):
    claim = record.get("claim")
    if claim:
        return digest(dict(kind=record["kind"], scope=record["scope"], claim=claim, basis=record.get("basis", record.get("validation"))))
    return digest({k: v for k, v in record.items() if k not in PROVENANCE})


def claim_slot(record):
    claim = record.get("claim")
    if not claim:
        return signature(record)  # Do not guess an entity/condition from a similar title.
    if not isinstance(claim, dict) or set(claim) - {"subject", "predicate", "object", "conditions", "cardinality"}:
        raise ValueError("Invalid atomic claim")
    if not all(isinstance(claim.get(k), str) and claim[k] for k in ("subject", "predicate")):
        raise ValueError("Atomic claims require a subject and predicate")
    if not isinstance(claim.get("object"), dict) or set(claim["object"]) != {"type", "value"}:
        raise ValueError("Atomic claims require a typed object")
    if not isinstance(claim.get("conditions", {}), dict) or claim.get("cardinality", "single") not in {"single", "set"}:
        raise ValueError("Invalid conditions or cardinality")
    object_value = claim["object"]
    expected = {"string": str, "integer": int, "number": (int, float), "boolean": bool}.get(object_value["type"])
    if (
        expected is None
        or not isinstance(object_value["value"], expected)
        or object_value["type"] in {"integer", "number"}
        and isinstance(object_value["value"], bool)
    ):
        raise ValueError("Claim value does not match its declared type")
    allowed = {"environment", "platform", "branch", "version", "config_path", "dependency_fingerprint", "human_scope_requires_validation"}
    if set(claim.get("conditions", {})) - allowed or any(not isinstance(v, str) for v in claim.get("conditions", {}).values()):
        raise ValueError("Unknown applicability conditions must remain unstructured and require validation")
    return digest([claim["subject"], claim["predicate"], record["scope"], claim.get("conditions", {}), claim.get("cardinality", "single")])


class Repository:
    def __init__(self, database):
        self.path = Path(database).resolve()

    @contextmanager
    def connect(self, *, write=False):
        uri = str(self.path) if write else self.path.as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=not write, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def namespace(db, workspace=None, session_id="", *, mode="runtime_v2"):
        if not workspace and not session_id:
            raise ValueError("A memory namespace needs a workspace or session")
        identifier = namespace_id(workspace, session_id)
        db.execute(
            "INSERT OR IGNORE INTO memory_namespaces(id,scope_kind,scope_key,mode) VALUES(?,?,?,?)",
            (identifier, "workspace" if workspace else "session", canonical_path(workspace) or session_id, mode),
        )
        return identifier

    @staticmethod
    def event(db, ns, operation, body, *, key=None):
        identifier = key or "me:" + uuid4().hex
        db.execute("INSERT INTO memory_events VALUES(?,?,?,?,?)", (identifier, ns, operation, serialized(body), now()))
        return identifier

    @staticmethod
    def bump(db, ns, versions=()):
        db.execute("UPDATE memory_namespaces SET epoch=epoch+1 WHERE id=?", (ns,))
        epoch = db.execute("SELECT epoch FROM memory_namespaces WHERE id=?", (ns,)).fetchone()[0]
        for version in set(versions):
            db.execute(
                "INSERT INTO memory_outbox(namespace_id,version_id,epoch,body) VALUES(?,?,?,?)",
                (ns, version, epoch, serialized(dict(version_id=version, namespace_id=ns, epoch=epoch))),
            )
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_versions'").fetchone():
            db.execute("UPDATE memory_versions SET value=value+1 WHERE id='effective'")

    @staticmethod
    def validity(db, version, *, lifecycle=None, freshness=None, valid_from=None, valid_to=None, replace_interval=False):
        old = db.execute("SELECT * FROM memory_validity WHERE version_id=? AND recorded_until IS NULL", (version,)).fetchone()
        at = now()
        db.execute("UPDATE memory_validity SET recorded_until=? WHERE id=?", (at, old["id"]))
        db.execute(
            """INSERT INTO memory_validity(version_id,valid_from,valid_to,recorded_at,lifecycle,freshness,revalidate_after,
                   environment_fingerprint) VALUES(?,?,?,?,?,?,?,?)""",
            (
                version,
                timestamp(valid_from) if replace_interval else old["valid_from"],
                timestamp(valid_to) if replace_interval else old["valid_to"],
                at,
                lifecycle or old["lifecycle"],
                freshness or old["freshness"],
                old["revalidate_after"],
                old["environment_fingerprint"],
            ),
        )

    def add_version(self, db, ns, memory, record, source, *, lifecycle="active"):
        claim_slot(record)
        if len(serialized(record)) > 64000:
            raise ValueError("Knowledge content exceeds the 64000 character limit")
        revision = db.execute("SELECT latest_revision FROM memory_items WHERE id=?", (memory,)).fetchone()[0] + 1
        identifier = "mv:" + uuid4().hex
        at = now()
        conditions = (record.get("claim") or {}).get("conditions", {})
        db.execute(
            "INSERT INTO memory_item_versions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                memory,
                revision,
                digest(record),
                signature(record),
                serialized(record),
                serialized(conditions),
                record.get("basis", record.get("validation", "proposed")),
                at,
            ),
        )
        db.execute("UPDATE memory_items SET latest_revision=? WHERE id=?", (revision, memory))
        start, end = timestamp(record.get("valid_from")), timestamp(record.get("valid_to"))
        if start and end and start >= end:
            raise ValueError("Invalid memory validity interval")
        db.execute(
            """INSERT INTO memory_validity(version_id,valid_from,valid_to,recorded_at,lifecycle,freshness,revalidate_after)
                   VALUES(?,?,?,?,?,?,?)""",
            (identifier, start, end, at, lifecycle, "unknown", timestamp(record.get("revalidate_after"))),
        )
        review = ("corrected" if record.get("basis") == "human_asserted" else "confirmed") if record.get("human_review") else "unreviewed"
        db.execute(
            "INSERT INTO memory_projection VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                memory,
                ns,
                record["kind"],
                record["title"],
                record["summary"],
                record["scope"],
                review,
                bool(record.get("pinned")),
                serialized(source),
                0,
                at,
                serialized(record.get("tags", [])),
            ),
        )
        searchable = " ".join(str(record.get(k, "")) for k in ("title", "summary", "scope", "trigger", "preconditions", "steps", "tags"))
        db.execute("INSERT INTO memory_fts VALUES(?,?)", (identifier, " ".join(sorted(tokens(searchable)))))
        return identifier

    @staticmethod
    def refresh_support(db, versions):
        for version in set(versions):
            # AND within a bundle; OR between complete bundles. Same root never adds votes.
            count = db.execute(
                """SELECT COUNT(DISTINCT o.evidence_group_id) FROM memory_supports s
                JOIN memory_observations o ON o.id=s.observation_id JOIN memory_sources src ON src.id=o.source_id
                WHERE s.version_id=? AND s.polarity='supports' AND src.available=1
                AND NOT EXISTS(SELECT 1 FROM memory_supports x JOIN memory_observations xo ON xo.id=x.observation_id
                    JOIN memory_sources xs ON xs.id=xo.source_id WHERE x.version_id=s.version_id AND x.bundle_id=s.bundle_id
                    AND x.polarity='supports' AND xs.available=0)""",
                (version,),
            ).fetchone()[0]
            db.execute("UPDATE memory_projection SET support_count=? WHERE version_id=?", (count, version))
            # Representative provenance must stay readable after losing a redundant source.
            source = db.execute(
                """SELECT src.body FROM memory_supports s JOIN memory_observations o ON o.id=s.observation_id
                JOIN memory_sources src ON src.id=o.source_id WHERE s.version_id=? AND src.available=1
                AND s.polarity='supports' ORDER BY src.id LIMIT 1""",
                (version,),
            ).fetchone()
            if source:
                db.execute("UPDATE memory_projection SET source=? WHERE version_id=?", (source[0], version))

    def ingest(self, db, ns, record, source, *, denied=False):
        """Exact consolidation only; semantic judgments enter the proposal queue."""
        sig, slot = signature(record), claim_slot(record)
        source_id = "ms:" + digest([ns, source["evaluation_id"], source["record_id"], source["content_digest"]])
        obs = "mo:" + digest([source_id, record.get("evidence_refs")])
        existing = db.execute("SELECT id FROM memory_sources WHERE id=?", (source_id,)).fetchone()
        if existing:
            return None
        db.execute(
            "INSERT INTO memory_sources VALUES(?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                ns,
                source["session_id"],
                source["evaluation_id"],
                source["record_id"],
                source["content_digest"],
                source["source_digest"],
                1,
                serialized(source),
            ),
        )
        db.execute(
            "INSERT INTO memory_observations VALUES(?,?,?,?,?,?)",
            (obs, source_id, source["source_digest"], timestamp(record.get("observed_at")), serialized(record.get("evidence_refs", [])), serialized(record)),
        )
        if denied:
            db.execute("INSERT OR IGNORE INTO memory_tombstones VALUES(?,?,?)", (ns, sig, "legacy human rejection"))
        denied = denied or bool(db.execute("SELECT 1 FROM memory_tombstones WHERE namespace_id=? AND signature=?", (ns, sig)).fetchone())
        same = db.execute(
            """SELECT v.id,v.memory_id FROM memory_item_versions v JOIN memory_items i ON i.id=v.memory_id
            JOIN memory_validity t ON t.version_id=v.id AND t.recorded_until IS NULL
            WHERE i.namespace_id=? AND v.signature=? AND i.canonical_redirect IS NULL
            AND t.lifecycle IN ('active','disputed','retired') ORDER BY v.created_at DESC LIMIT 1""",
            (ns, sig),
        ).fetchone()
        if not same:
            # A known paraphrase remains an alias after MERGE. Only reuse the
            # target content version that received its evidence, never an unrelated later revision.
            aliases = db.execute(
                """SELECT v.id,i.canonical_redirect FROM memory_item_versions v
                JOIN memory_items i ON i.id=v.memory_id WHERE i.namespace_id=? AND v.signature=?
                AND i.canonical_redirect IS NOT NULL""",
                (ns, sig),
            ).fetchall()
            for alias in aliases:
                target, seen = alias["canonical_redirect"], set()
                while target and target not in seen:
                    seen.add(target)
                    redirect = db.execute("SELECT canonical_redirect FROM memory_items WHERE id=?", (target,)).fetchone()
                    if not redirect or not redirect[0]:
                        break
                    target = redirect[0]
                same = db.execute(
                    """SELECT v.id,v.memory_id FROM memory_item_versions v
                    JOIN memory_validity t ON t.version_id=v.id AND t.recorded_until IS NULL
                    WHERE v.memory_id=? AND t.lifecycle IN ('active','disputed','retired') AND EXISTS(
                    SELECT 1 FROM memory_supports a JOIN memory_supports b ON b.observation_id=a.observation_id
                    WHERE a.version_id=? AND b.version_id=v.id) ORDER BY v.revision DESC LIMIT 1""",
                    (target, alias["id"]),
                ).fetchone()
                if same:
                    break
        if same:
            version, memory = same
            db.execute("UPDATE memory_items SET latest_revision=latest_revision+1 WHERE id=?", (memory,))
            operation = "ATTACH_SUPPORT"
        else:
            prior = db.execute(
                """SELECT v.id,v.memory_id,v.record,i.claim_slot FROM memory_sources src
                JOIN memory_observations o ON o.source_id=src.id JOIN memory_supports s ON s.observation_id=o.id
                JOIN memory_item_versions v ON v.id=s.version_id JOIN memory_items i ON i.id=v.memory_id
                JOIN memory_projection p ON p.version_id=v.id JOIN memory_validity t ON t.version_id=v.id
                WHERE src.evaluation_id=? AND src.record_id=? AND src.id!=? AND i.namespace_id=? AND i.kind=?
                AND i.canonical_redirect IS NULL AND p.support_count=0 AND t.recorded_until IS NULL AND t.lifecycle='active'
                ORDER BY v.revision DESC LIMIT 1""",
                (source["evaluation_id"], source["record_id"], source_id, ns, record["kind"]),
            ).fetchone()
            old_record = json.loads(prior["record"]) if prior else {}
            same_identity = (
                prior
                and old_record.get("scope") == record["scope"]
                and (prior["claim_slot"] == slot if record.get("claim") else old_record.get("title") == record["title"])
            )
            if same_identity and not denied:
                memory = prior["memory_id"]
                self.validity(db, prior["id"], lifecycle="superseded")
                operation = "REVISE"
            else:
                memory = "mem:" + uuid4().hex
                db.execute("INSERT INTO memory_items VALUES(?,?,?,?,0,NULL,?)", (memory, ns, record["kind"], slot, now()))
                operation = "CREATE"
            version = self.add_version(db, ns, memory, record, source, lifecycle="retired" if denied else "active")
        if denied:
            self.validity(db, version, lifecycle="retired")
        db.execute("INSERT OR IGNORE INTO memory_supports VALUES(?,?,?,?)", (version, obs, obs, "supports"))
        self.refresh_support(db, [version])
        claim = record.get("claim")
        if claim:
            entity_id = "entity:" + digest([ns, claim["subject"]])[:32]
            db.execute("INSERT OR IGNORE INTO memory_entities VALUES(?,?,?,?)", (entity_id, ns, "subject", claim["subject"]))
            db.execute("INSERT OR IGNORE INTO memory_entity_aliases VALUES(?,?,?,?)", (ns, claim["subject"], entity_id, obs))
        if claim and claim.get("cardinality", "single") == "single" and not same and not denied:
            others = db.execute(
                """SELECT v.id,v.record,t.valid_from,t.valid_to FROM memory_items i
                JOIN memory_item_versions v ON v.memory_id=i.id JOIN memory_validity t ON t.version_id=v.id
                WHERE i.namespace_id=? AND i.claim_slot=? AND v.id!=? AND i.canonical_redirect IS NULL
                AND t.recorded_until IS NULL AND t.lifecycle IN ('active','disputed')""",
                (ns, slot, version),
            ).fetchall()
            start, end = timestamp(record.get("valid_from")), timestamp(record.get("valid_to"))
            overlapping = [
                r["id"]
                for r in others
                if not (start and r["valid_to"] and start >= r["valid_to"] or end and r["valid_from"] and end <= r["valid_from"])
                and json.loads(r["record"]).get("claim", {}).get("object") != claim["object"]
            ]
            if overlapping:
                self.conflict(db, ns, slot, [*overlapping, version])
        self.event(db, ns, operation, dict(memory_id=memory, version_id=version, observation_id=obs))
        self.bump(db, ns, [version])
        return version

    def conflict(self, db, ns, slot, versions):
        identifier = "mc:" + digest(sorted(versions))
        db.execute("INSERT OR IGNORE INTO memory_conflicts VALUES(?,?,?,?,NULL,?)", (identifier, ns, slot, "open", now()))
        for version in versions:
            db.execute("INSERT OR IGNORE INTO memory_conflict_members VALUES(?,?)", (identifier, version))
            self.validity(db, version, lifecycle="disputed")
        self.bump(db, ns, versions)
        return identifier

    def invalidate(self, db, *, evaluation_id=None, session_id=None):
        field, value = ("evaluation_id", evaluation_id) if evaluation_id is not None else ("session_id", session_id)
        affected = db.execute(
            f"""SELECT DISTINCT s.version_id,p.namespace_id FROM memory_sources src
            JOIN memory_observations o ON o.source_id=src.id JOIN memory_supports s ON s.observation_id=o.id
            JOIN memory_projection p ON p.version_id=s.version_id WHERE src.{field}=? AND src.available=1""",
            (value,),
        ).fetchall()
        db.execute(f"UPDATE memory_sources SET available=0 WHERE {field}=?", (value,))
        self.refresh_support(db, [r[0] for r in affected])
        for memory in {db.execute("SELECT memory_id FROM memory_item_versions WHERE id=?", (r[0],)).fetchone()[0] for r in affected}:
            db.execute("UPDATE memory_items SET latest_revision=latest_revision+1 WHERE id=?", (memory,))
        for ns in {r[1] for r in affected}:
            self.event(db, ns, "SOURCE_UNAVAILABLE", {field: value})
            self.bump(db, ns, [r[0] for r in affected if r[1] == ns])

    def checked(self, db, ns, memory, revision):
        row = db.execute("SELECT * FROM memory_items WHERE id=? AND namespace_id=? AND canonical_redirect IS NULL", (memory, ns)).fetchone()
        if not row:
            raise MemoryError("memory.not_found", "Knowledge not found in this namespace")
        if type(revision) is not int or row["latest_revision"] != revision:
            raise MemoryError("memory.revision_conflict", "Knowledge changed; reload before updating")
        return row
