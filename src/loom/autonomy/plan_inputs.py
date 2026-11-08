"""Resolve compiled output references against recorded execution receipts.

The initial contract supports verified run results (including artifact refs),
not arbitrary string interpolation into commands or paths.
"""

from loom.autonomy.acceptance import recorded_verdict
from loom.service.contracts import ServiceError


def resolve(store, db, aid, work):
    bindings = []
    for ref in work.get("input_refs", []):
        producer = store.get(db, "agent_work_packages", ref["work_package_id"], aid)
        if (ref["kind"] != "verified_result" or producer.get("contract_digest") != ref["contract_digest"]
                or producer["state"] != "succeeded" or producer.get("superseded_contract") or not recorded_verdict(db, producer)):
            raise ServiceError("A required predecessor result has no matching verified receipt", 409, code="DEPENDENCY_WAIT")
        bindings.append(dict(**ref, session_id=producer["session_id"], run_id=producer["terminal_run_id"],
                             receipt=producer["acceptance_result"], outcome=producer["outcome"]))
    return bindings
