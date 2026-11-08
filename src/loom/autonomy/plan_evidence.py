"""One citation namespace for planning facts; execution receipts stay distinct."""

from loom.autonomy.acceptance import support
from loom.autonomy.store import digest

PROTOCOL_VERSION = 4


def prepare(payload):
    """Register host observations without presenting them as passed deliveries.

    Pointers refer to facts in the same persisted planning input. They cannot be
    used as completion evidence: only delivery receipts have verified criteria.
    """
    rows = list(payload["evidence"])
    if snapshot := payload.get("workspace_snapshot"):
        rows.append(dict(id="workspace:" + digest(snapshot), kind="workspace_observation", passed=False, verified_criteria=[],
                         snapshot_path="/workspace_snapshot",
                         limitation="Bounded directory/configuration observation only; no command execution or delivery verification."))
    facts = payload.get("source_status") or {}
    for field, kind in (("sources", "source_status"), ("observations", "reported_observation")):
        for index, fact in enumerate(facts.get(field, [])):
            rows.append(dict(id=kind + ":" + digest(fact), kind=kind, passed=False, verified_criteria=[],
                             snapshot_path=f"/source_status/{field}/{index}", limitation="Planning reference, not a passed execution receipt."))
    payload["evidence"] = rows
    payload["acceptance_support"] = support(payload)
    return payload


def reference_schema(payload):
    ids = [row["id"] for row in payload["evidence"]]
    value = dict(type="array", maxItems=30, items=dict(type="string"),
                 description="Exact IDs from evidence[].id in this input. File paths, source route IDs, criterion IDs and field names are not citations. "
                             "Use [] when no registered evidence supports the decision; proposing discovery requires no prior delivery.")
    if ids:
        value["items"]["enum"] = ids
    else:
        value["maxItems"] = 0
    return value
