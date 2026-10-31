"""User-declared method versions: usable guidance, never fabricated validation."""

from loom.autonomy.store import digest
from loom.service.contracts import canonical, text


def import_method(db, aid, workspace, method_text, title):
    method_text = text(method_text, "method_text", max_length=8000)
    title = text(title, "title", max_length=200)
    identifier = "method_" + digest([aid, workspace, method_text, title])[:32]
    body = dict(
        id=identifier,
        skill_id=identifier,
        workspace=workspace or "",
        state="user_guidance",
        revision=1,
        method_text=method_text,
        title=title,
        evidence_state="human_asserted",
        adoption="user_specified",
        source={"kind": "human_import", "agent_id": aid},
        sources_available=True,
    )
    db.execute("INSERT OR IGNORE INTO skill_versions VALUES(?,?,?,?,?,?)", (identifier, identifier, workspace or "", "user_guidance", 1, canonical(body)))
    return body
