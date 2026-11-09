"""Host invariants shared by proposal schemas, verification and restore.

A completion request submits a candidate. Only earlier execution facts can be
preconditions; control transitions never supply execution receipts.
"""

RULES_VERSION = 2


class InvalidAcceptanceContract(ValueError):
    code = "ACCEPTANCE_CONTRACT_INVALID"

    def __init__(self, message, criterion_id=None):
        self.criterion_id = criterion_id
        super().__init__(message)


def evidence_tool_ids(assembly):
    return [name for name, binding in assembly.bindings.items() if binding.placement != "control"]


def require_evidence_tool(assembly, name, criterion_id):
    if name not in evidence_tool_ids(assembly):
        raise InvalidAcceptanceContract(
            f"Criterion {criterion_id}: {name} cannot supply an execution receipt. "
            "Completion/control calls cannot be acceptance prerequisites; verify the submitted candidate and prior execution facts.", criterion_id)


def evidence_check(criterion):
    """Compare evidence meaning, including wire defaults, not JSON spelling."""
    check = dict(criterion["check"])
    if criterion["verifier"] == "tool_call":
        check.setdefault("min_calls", 1)
    elif criterion["verifier"] == "artifact":
        check.setdefault("nonempty", True)
        check.setdefault("contains", [])
    return check


def preserve_evidence_requirements(previous, proposed):
    """Solver method edits may strengthen evidence, never discard a witness.

    Replacing an execution/visual/source assertion requires a new owner-issued
    contract. Descriptions alone are not evidence requirements.
    """
    by_id = {c["id"]: c for c in proposed["criteria"]}
    for old in previous["criteria"]:
        new = by_id.get(old["id"])
        if not new:
            continue  # Existing identity/description validation reports removal.
        if not set(old.get("required_tools", [])) <= set(new.get("required_tools", [])):
            raise InvalidAcceptanceContract(f"Criterion {old['id']}: required evidence tools cannot be removed", old["id"])
        if old["verifier"] != "semantic" and (new["verifier"] != old["verifier"] or evidence_check(new) != evidence_check(old)):
            raise InvalidAcceptanceContract(
                f"Criterion {old['id']}: changing a concrete evidence requirement needs a new task/plan contract", old["id"])
