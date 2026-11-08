"""Rebuildable batch indexes. External summaries never cross the authority gate.

Adapters implement publish(upserts, deletes, generation) and search(query, limit).
An index binding is scoped to one namespace and one model fingerprint. Publishing
is explicit: the default read/write path never starts an embedding or graph call.
"""

import json

from loom.memory.query import available
from loom.memory.repository import Repository, digest, now
from loom.memory.store import serialized


class MemoryIndexProjection:
    def __init__(self, database, namespace, backend, fingerprint, adapter):
        self.repo = Repository(database)
        self.namespace, self.backend, self.fingerprint, self.adapter = namespace, backend, fingerprint, adapter

    def sync(self, *, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("Index batches must contain 1–10000 events")
        with self.repo.connect() as db:
            state = db.execute(
                "SELECT * FROM memory_index_state WHERE namespace_id=? AND backend=? AND fingerprint=?", (self.namespace, self.backend, self.fingerprint)
            ).fetchone()
            watermark = state["last_outbox"] if state else 0
            events = db.execute("SELECT * FROM memory_outbox WHERE namespace_id=? AND id>? ORDER BY id LIMIT ?", (self.namespace, watermark, limit)).fetchall()
            if not events:
                return dict(updated=0, generation=state["generation"] if state else None)
            upserts, deletes = [], []
            for version in {r["version_id"] for r in events}:
                row = db.execute(
                    """SELECT v.*,p.namespace_id FROM memory_item_versions v
                    JOIN memory_projection p ON p.version_id=v.id WHERE v.id=? AND p.namespace_id=?""",
                    (version, self.namespace),
                ).fetchone()
                if not row or not available(db, version):
                    deletes.append(version)
                else:
                    upserts.append(
                        dict(id=version, memory_id=row["memory_id"], namespace_id=self.namespace, content_digest=row["content_digest"], text=row["record"])
                    )
        generation = digest([self.namespace, self.backend, self.fingerprint, events[-1]["id"], upserts, deletes])
        try:
            self.adapter.publish(upserts, deletes, generation)
        except Exception as exc:
            with self.repo.connect(write=True) as db:
                db.execute(
                    """INSERT INTO memory_index_state(namespace_id,backend,fingerprint,error) VALUES(?,?,?,?)
                    ON CONFLICT(namespace_id,backend,fingerprint) DO UPDATE SET error=excluded.error""",
                    (self.namespace, self.backend, self.fingerprint, str(exc)),
                )
            raise
        with self.repo.connect(write=True) as db:
            previous = db.execute(
                "SELECT last_outbox FROM memory_index_state WHERE namespace_id=? AND backend=? AND fingerprint=?",
                (self.namespace, self.backend, self.fingerprint),
            ).fetchone()
            if previous and previous[0] > watermark:
                raise ValueError("Another publisher advanced this index; retry from its watermark")
            for version in deletes:
                db.execute(
                    "DELETE FROM memory_index_entries WHERE namespace_id=? AND backend=? AND fingerprint=? AND version_id=?",
                    (self.namespace, self.backend, self.fingerprint, version),
                )
            for entry in upserts:
                db.execute(
                    "INSERT OR REPLACE INTO memory_index_entries VALUES(?,?,?,?,?,?)",
                    (self.namespace, self.backend, self.fingerprint, entry["id"], entry["content_digest"], generation),
                )
            db.execute(
                "INSERT OR REPLACE INTO memory_index_state VALUES(?,?,?,?,?,?,?)",
                (self.namespace, self.backend, self.fingerprint, generation, events[-1]["epoch"], None, events[-1]["id"]),
            )
        return dict(updated=len(upserts), deleted=len(deletes), generation=generation)

    def search(self, query, *, limit=6, environment=None):
        hits = self.adapter.search(query, limit)
        results, seen = [], set()
        with self.repo.connect() as db:
            for hit in hits:
                version = hit.get("version_id", hit.get("document_id"))
                row = db.execute(
                    """SELECT v.*,p.namespace_id FROM memory_item_versions v JOIN memory_projection p ON p.version_id=v.id
                    JOIN memory_index_entries x ON x.version_id=v.id AND x.namespace_id=p.namespace_id AND x.content_digest=v.content_digest
                    WHERE v.id=? AND p.namespace_id=? AND x.backend=? AND x.fingerprint=?""",
                    (version, self.namespace, self.backend, self.fingerprint),
                ).fetchone()
                if not row or not available(db, version) or row["memory_id"] in seen:
                    continue
                conditions = json.loads(row["conditions"])
                if any((environment or {}).get(key) != value for key, value in conditions.items()):
                    continue
                record = json.loads(row["record"])
                if record.get("runtime_blocked"):
                    continue
                seen.add(row["memory_id"])
                # Never forward hit.text, graph_entities, relationships or generated summaries.
                results.append(dict(memory_id=row["memory_id"], version_id=version, content_digest=row["content_digest"], record=record))
                if len(results) == limit:
                    break
        return results


class LightRAGMemoryAdapter:
    """Explicit optional adapter for a dedicated LightRAG knowledge base."""

    def __init__(self, knowledge_store, base_id):
        self.store, self.base_id = knowledge_store, base_id

    def publish(self, upserts, deletes, generation):
        from loom.knowledge.lightrag_local import publish

        documents = [dict(id=r["id"], name=r["id"] + ".json", digest=digest(r["text"]), content=r["text"], created_at=now()) for r in upserts]
        return publish(self.store, self.base_id, documents, deletes, checkpoint_key="memory_" + generation[:24])

    def search(self, query, limit):
        from loom.knowledge.lightrag_local import search

        return search(self.store, self.base_id, query, limit)


def document_evidence(knowledge_store, base_id, document_id, *, page_number=1):
    """YakDB/existing document readers supply immutable page evidence, not canonical truth."""
    value = knowledge_store.read(base_id, document_id, page_number=page_number, limit=6000)
    return dict(
        kind="document_page",
        source_ref=dict(knowledge_base_id=base_id, document_id=document_id, page_number=page_number),
        revision=digest(value),
        payload=serialized(value),
    )
