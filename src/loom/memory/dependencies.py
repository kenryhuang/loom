"""Pin procedural skills to the exact knowledge versions used at induction."""

import json

from loom.memory.schema import exists


def bind_skill(db, skill_version_id, case):
    if not exists(db):
        return
    ref = case.get("provenance", {})
    if not ref.get("record_id"):
        return
    # A correction must not silently rebind an old Skill to its replacement fact.
    db.execute(
        """INSERT OR IGNORE INTO memory_skill_dependencies
        SELECT DISTINCT ?,s.version_id FROM memory_sources src JOIN memory_observations o ON o.source_id=src.id
        JOIN memory_supports s ON s.observation_id=o.id
        WHERE src.evaluation_id=? AND src.record_id=? AND src.revision=?""",
        (skill_version_id, ref["evaluation_id"], ref["record_id"], ref.get("record_digest")),
    )


def migrate_skills(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='skill_dependencies'").fetchone():
        return
    for row in db.execute("""SELECT d.version_id,c.body FROM skill_dependencies d JOIN skill_cases c ON c.id=d.case_id
        WHERE NOT EXISTS(SELECT 1 FROM memory_skill_dependencies m WHERE m.skill_version_id=d.version_id)""").fetchall():
        bind_skill(db, row[0], json.loads(row[1]))
