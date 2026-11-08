"""Indexed, scoped, read-only knowledge queries and exact historical references."""

import base64
import json

from loom.memory.repository import Repository, digest, namespace_id, now, timestamp
from loom.memory.schema import exists
from loom.memory.store import POLICY, SUMMARY_CHARS, MemoryError, serialized, tokens

JOIN = """FROM memory_projection p JOIN memory_items i ON i.id=p.memory_id
 JOIN memory_item_versions v ON v.id=p.version_id
 JOIN memory_validity t ON t.version_id=v.id AND t.recorded_until IS NULL"""


LIST_JOIN = """FROM memory_projection p JOIN memory_items i INDEXED BY memory_items_visibility ON i.id=p.memory_id
 JOIN memory_validity t INDEXED BY memory_validity_eligibility ON t.version_id=p.version_id AND t.recorded_until IS NULL"""


def available(db, version, *, at=None, visiting=()):
    if version in visiting:
        return False
    at = timestamp(at) if at else now()
    row = db.execute(
        """SELECT p.support_count,t.* FROM memory_projection p JOIN memory_validity t ON t.version_id=p.version_id
        JOIN memory_items i ON i.id=p.memory_id WHERE p.version_id=? AND t.recorded_until IS NULL
        AND i.canonical_redirect IS NULL""",
        (version,),
    ).fetchone()
    if not row or not row["support_count"] or row["lifecycle"] != "active" or row["freshness"] == "expired":
        return False
    if row["valid_from"] and row["valid_from"] > at or row["valid_to"] and row["valid_to"] <= at:
        return False
    if row["revalidate_after"] and row["revalidate_after"] <= at:
        return False
    dependencies = db.execute("SELECT to_version FROM memory_relations WHERE from_version=? AND kind='depends_on'", (version,))
    return all(available(db, r[0], at=at, visiting=(*visiting, version)) for r in dependencies)


class MemoryQueryService:
    def __init__(self, database, *, workspace=None, session_id="", environment=None):
        self.repo = Repository(database)
        self.ns = namespace_id(workspace, session_id)
        self.environment = environment or {}

    def state(self, db):
        if not exists(db):
            return None
        return db.execute("SELECT * FROM memory_namespaces WHERE id=?", (self.ns,)).fetchone()

    def enabled(self, *, runtime=False):
        with self.repo.connect() as db:
            state = self.state(db)
            return bool(state and state["mode"] in ({"runtime_v2"} if runtime else {"runtime_v2", "library_v2"}))

    def epoch(self):
        with self.repo.connect() as db:
            state = self.state(db)
            if not state:
                return None
            # Time alone can change eligibility; no global cache invalidation required.
            at = now()
            boundary = db.execute(
                """SELECT MIN(boundary) FROM (
                SELECT t.valid_from AS boundary """
                + JOIN
                + """ WHERE p.namespace_id=? UNION ALL
                SELECT t.valid_to """
                + JOIN
                + """ WHERE p.namespace_id=? UNION ALL
                SELECT t.revalidate_after """
                + JOIN
                + """ WHERE p.namespace_id=?) WHERE boundary>?""",
                (self.ns, self.ns, self.ns, at),
            ).fetchone()[0]
            passed = db.execute(
                "SELECT COUNT(*) "
                + JOIN
                + """ WHERE p.namespace_id=? AND
                (t.valid_from<=? OR t.valid_to<=? OR t.revalidate_after<=?)""",
                (self.ns, at, at, at),
            ).fetchone()[0]
            return digest([state["epoch"], state["mode"], boundary, passed, self.environment])

    def _filter(self, *, kind="", review="", query="", lifecycle="current", runtime=False, at=None, dependencies=True):
        at = timestamp(at) if at else now()
        where = ["p.namespace_id=?", "i.canonical_redirect IS NULL"]
        args = [self.ns]
        if lifecycle == "current" or runtime:
            where += [
                "t.lifecycle IN ('active','disputed')",
                "p.support_count>0",
                "t.freshness!='expired'",
                "(t.valid_from IS NULL OR t.valid_from<=?)",
                "(t.valid_to IS NULL OR t.valid_to>?)",
            ]
            args += [at, at]
        elif lifecycle != "all":
            if lifecycle not in {"active", "disputed", "retired", "superseded", "merged"}:
                raise MemoryError("memory.query_invalid", "Invalid lifecycle")
            where += ["t.lifecycle=?"]
            args += [lifecycle]
        if dependencies and (lifecycle == "current" or runtime):
            where.append("""NOT EXISTS (WITH RECURSIVE deps(id) AS (
                SELECT to_version FROM memory_relations WHERE from_version=p.version_id AND kind='depends_on'
                UNION SELECT r.to_version FROM memory_relations r JOIN deps d ON r.from_version=d.id WHERE r.kind='depends_on')
                SELECT 1 FROM deps d LEFT JOIN memory_projection dp ON dp.version_id=d.id
                LEFT JOIN memory_validity dt ON dt.version_id=d.id AND dt.recorded_until IS NULL
                WHERE dp.version_id IS NULL OR dp.support_count=0 OR dt.lifecycle!='active' OR dt.freshness='expired'
                OR dt.valid_from>? OR dt.valid_to<=? OR dt.revalidate_after<=?)""")
            args += [at, at, at]
        if runtime:
            where += [
                "COALESCE(json_extract(v.record,'$.runtime_blocked'),0)=0",
                "(t.revalidate_after IS NULL OR t.revalidate_after>?)",
                """NOT EXISTS(SELECT 1 FROM json_each(v.conditions) c
                         WHERE json_type(?, '$.' || c.key) IS NULL OR json_extract(?, '$.' || c.key)!=c.value)""",
            ]
            args += [at, serialized(self.environment), serialized(self.environment)]
        if kind:
            where.append("p.kind=?")
            args.append(kind)
        if review:
            where.append("p.review_state='unreviewed'" if review == "unreviewed" else "p.review_state!='unreviewed'")
        if query.strip():
            terms = sorted(tokens(query))[:128]
            if terms:
                where.append("p.version_id IN (SELECT version_id FROM memory_fts WHERE memory_fts MATCH ?)")
                args.append(" OR ".join('"' + term.replace('"', '""') + '"' for term in terms))
            else:
                where.append("0")
        return " AND ".join(where), args

    @staticmethod
    def row(row, *, detail=False):
        record = json.loads(row["record"])
        source = json.loads(row["source"])
        source.update(memory_id=row["memory_id"], version_id=row["version_id"])
        record["pinned"] = bool(row["pinned"])
        record["tags"] = json.loads(row["tags"])
        fields = (
            record
            if detail
            else {
                k: record[k]
                for k in (
                    "kind",
                    "title",
                    "summary",
                    "scope",
                    "confidence",
                    "basis",
                    "validation",
                    "human_review",
                    "pinned",
                    "tags",
                    "preconditions",
                    "revalidate_when",
                )
                if k in record
            }
        )
        return dict(
            **fields,
            source=source,
            memory_id=row["memory_id"],
            version_id=row["version_id"],
            revision=row["latest_revision"],
            content_revision=row["content_revision"],
            lifecycle=row["lifecycle"],
            freshness=("needs_revalidation" if row["revalidate_after"] and row["revalidate_after"] <= now() else row["freshness"]),
            review_state=row["review_state"],
            support_count=row["support_count"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            conditions=json.loads(row["conditions"]),
            applicability="conditional" if json.loads(row["conditions"]) else "historical",
        )

    def browse(self, *, query="", kind="", review="", offset=0, limit=20, cursor=None, lifecycle="current"):
        with self.repo.connect() as db:
            dependencies = bool(db.execute("SELECT 1 FROM memory_relations WHERE namespace_id=? AND kind='depends_on' LIMIT 1", (self.ns,)).fetchone())
            where, args = self._filter(query=query, kind=kind, review=review, lifecycle=lifecycle, dependencies=dependencies)
            state = self.state(db)
            epoch = state["epoch"]
            count_join = LIST_JOIN.replace("memory_projection p ", "memory_projection p INDEXED BY memory_projection_filter ")
            total = db.execute("SELECT COUNT(*) " + count_join + " WHERE " + where, args).fetchone()[0]
            if cursor:
                try:
                    value = json.loads(base64.urlsafe_b64decode(cursor))
                    if value["epoch"] != epoch or value["filters"] != digest([self.ns, query, kind, review, lifecycle]):
                        raise ValueError("Changed query or knowledge epoch")
                    where += " AND (p.created_at,p.version_id)<(?,?)"
                    args += [value["created_at"], value["version_id"]]
                    offset = 0
                except (ValueError, KeyError, TypeError) as exc:
                    raise MemoryError("memory.cursor_stale", "Reload the first page: knowledge or filters changed") from exc
            rows = db.execute(
                "SELECT p.*,v.record,v.conditions,v.revision AS content_revision,i.latest_revision,"
                "t.lifecycle,t.freshness,t.valid_from,t.valid_to,t.revalidate_after "
                + JOIN
                + " WHERE p.version_id IN (SELECT p.version_id "
                + LIST_JOIN
                + " WHERE "
                + where
                + " ORDER BY p.created_at DESC,p.version_id DESC LIMIT ? OFFSET ?) ORDER BY p.created_at DESC,p.version_id DESC",
                [*args, limit + 1, offset],
            ).fetchall()
            selected = rows[:limit]
            following = None
            if len(rows) > limit:
                last = selected[-1]
                following = base64.urlsafe_b64encode(
                    serialized(
                        dict(
                            epoch=epoch, filters=digest([self.ns, query, kind, review, lifecycle]), created_at=last["created_at"], version_id=last["version_id"]
                        )
                    ).encode()
                ).decode()
            return dict(
                records=[self.row(r) for r in selected],
                total=total,
                next_cursor=following,
                next_offset=offset + limit if following else None,
                truncated=False,
                issues=[],
                epoch=epoch,
                storage="memory_v2",
            )

    def search(self, query, *, limit=6):
        where, args = self._filter(query=query, runtime=True)
        with self.repo.connect() as db:
            state = self.state(db)
            rows = db.execute(
                "SELECT p.*,v.record,v.conditions,v.revision AS content_revision,i.latest_revision,"
                "t.lifecycle,t.freshness,t.valid_from,t.valid_to,t.revalidate_after "
                + JOIN
                + " WHERE "
                + where
                + " ORDER BY p.pinned DESC,p.created_at DESC LIMIT 200",
                args,
            ).fetchall()
            terms = tokens(query)
            rows = sorted(rows, key=lambda r: (r["pinned"], len(terms & tokens(r["title"])) * 2 + len(terms & tokens(r["summary"]))), reverse=True)
            selected, conflicts, seen = [], [], set()
            for row in rows:
                if row["lifecycle"] == "disputed":
                    groups = db.execute(
                        """SELECT c.id FROM memory_conflicts c JOIN memory_conflict_members m ON m.conflict_id=c.id
                        WHERE m.version_id=? AND c.state='open' """,
                        (row["version_id"],),
                    ).fetchall()
                    for (group,) in groups:
                        if group not in seen and len(conflicts) < 3:
                            conflicts.append(dict(conflict_id=group, instruction="Conflicting evidence; revalidate before using this claim."))
                            seen.add(group)
                    continue
                if not available(db, row["version_id"]):
                    continue
                item = self.row(row)
                if len(selected) < limit and len(serialized([*selected, item])) <= SUMMARY_CHARS:
                    selected.append(item)
            value = dict(
                instruction=POLICY,
                records=selected,
                conflicts=conflicts,
                available_records=len(rows),
                truncated=len(rows) > len(selected),
                issues=[],
                namespace_id=self.ns,
                epoch=state["epoch"],
                storage="memory_v2",
            )
            return packet(value)

    def detail(self, memory_id):
        with self.repo.connect() as db:
            rows = db.execute(
                "SELECT p.*,v.record,v.conditions,v.revision AS content_revision,i.latest_revision,"
                "t.lifecycle,t.freshness,t.valid_from,t.valid_to,t.revalidate_after "
                + JOIN
                + " WHERE p.namespace_id=? AND p.memory_id=? ORDER BY v.revision DESC",
                (self.ns, memory_id),
            ).fetchall()
            if not rows:
                raise MemoryError("memory.not_found", "Knowledge not found in this namespace")
            versions = [self.row(r, detail=True) for r in rows]
            for version in versions:
                vid = version["version_id"]
                version["supports"] = [
                    dict(r)
                    for r in db.execute(
                        """SELECT o.id,o.evidence_group_id,o.observed_at,o.anchor,src.body AS source,
                    src.available,s.bundle_id,s.polarity FROM memory_supports s JOIN memory_observations o ON o.id=s.observation_id
                    JOIN memory_sources src ON src.id=o.source_id WHERE s.version_id=?""",
                        (vid,),
                    )
                ]
                version["conflicts"] = [
                    dict(r)
                    for r in db.execute(
                        """SELECT c.* FROM memory_conflicts c
                    JOIN memory_conflict_members m ON m.conflict_id=c.id WHERE m.version_id=?""",
                        (vid,),
                    )
                ]
                version["reviews"] = [json.loads(r[0]) for r in db.execute("SELECT body FROM memory_reviews WHERE version_id=?", (vid,))]
            return dict(memory_id=memory_id, versions=versions)

    def history(self, memory_id, *, valid_at=None, known_at=None):
        clauses = ["i.namespace_id=?", "i.id=?"]
        args = [self.ns, memory_id]
        if known_at:
            known_at = timestamp(known_at)
            clauses += ["t.recorded_at<=?", "(t.recorded_until IS NULL OR t.recorded_until>?)"]
            args += [known_at, known_at]
        if valid_at:
            valid_at = timestamp(valid_at)
            clauses += ["(t.valid_from IS NULL OR t.valid_from<=?)", "(t.valid_to IS NULL OR t.valid_to>?)"]
            args += [valid_at, valid_at]
        with self.repo.connect() as db:
            rows = db.execute(
                "SELECT v.id AS version_id,v.revision,v.record,t.* FROM memory_items i "
                "JOIN memory_item_versions v ON v.memory_id=i.id JOIN memory_validity t ON t.version_id=v.id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY v.revision,t.recorded_at",
                args,
            ).fetchall()
            return dict(
                memory_id=memory_id,
                interpretations=[{**dict(r), "record": json.loads(r["record"])} for r in rows],
                historical=True,
                instruction="Historical interpretations; current support availability is checked separately.",
            )

    def read_version(self, memory_id, version_id, *, offset=0, limit=6000):
        detail = self.detail(memory_id)
        version = next((v for v in detail["versions"] if v["version_id"] == version_id), None)
        if not version:
            raise MemoryError("memory.not_found", "Version does not belong to the scoped knowledge")
        text = serialized(version)
        end = min(len(text), offset + limit)
        return dict(
            instruction=POLICY,
            source=version["source"],
            text=text[offset:end],
            total_chars=len(text),
            next_offset=end if end < len(text) else None,
            memory_id=memory_id,
            version_id=version_id,
            lifecycle=version["lifecycle"],
            historical=True,
        )

    def original(self, evaluation_id, record_id, *, offset=0, limit=6000):
        with self.repo.connect() as db:
            row = db.execute(
                """SELECT o.body,src.body AS source,src.available,o.id FROM memory_sources src
                JOIN memory_observations o ON o.source_id=src.id WHERE src.namespace_id=? AND src.evaluation_id=? AND src.record_id=?
                AND src.revision NOT LIKE 'human:%'
                ORDER BY src.available DESC,o.rowid DESC LIMIT 1""",
                (self.ns, evaluation_id, record_id),
            ).fetchone()
            if not row:
                raise MemoryError("memory.not_found", "Historical record not found in this namespace")
            mapping = [
                dict(r)
                for r in db.execute(
                    """SELECT p.memory_id,p.version_id FROM memory_supports s
                JOIN memory_projection p ON p.version_id=s.version_id WHERE s.observation_id=?""",
                    (row["id"],),
                )
            ]
            text = row["body"]
            end = min(len(text), offset + limit)
            return dict(
                instruction=POLICY,
                source=json.loads(row["source"]),
                text=text[offset:end],
                total_chars=len(text),
                next_offset=end if end < len(text) else None,
                historical=True,
                source_available=bool(row["available"]),
                canonical=mapping,
            )


def packet(value):
    result = {k: v for k, v in value.items() if k not in {"packet_id", "packet_digest"}}
    checksum = digest(result)
    return {**result, "packet_id": "mp:" + checksum[:32], "packet_digest": checksum}


def denial_overlay(database, workspace, session_id, rows):
    """A rollback to legacy reads must not undo newer human withdrawals."""
    from loom.memory.repository import signature

    repo = Repository(database)
    with repo.connect() as db:
        if not exists(db):
            return rows
        ns = namespace_id(workspace, session_id)
        denied = {r[0] for r in db.execute("SELECT signature FROM memory_tombstones WHERE namespace_id=?", (ns,))}
    return [row for row in rows if signature(row["record"]) not in denied]
