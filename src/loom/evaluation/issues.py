"""Stable, JSON-serializable failures shared by the incremental graph and memory.

Rules raise RuleViolation; boundaries attach request/record coordinates. Human
messages are presentation only, never inputs to retry or recovery decisions.
Do not include raw model values or evidence in issue details.
"""

from jsonschema import Draft202012Validator

VERSION = "evaluation-issue.1"


def issue(code, reason, *, category="semantic", phase="validate", path=(), **context):
    return dict(version=VERSION, code=code, category=category, phase=phase,
                path=list(path), reason=reason, **context)


class RuleViolation(ValueError):
    def __init__(self, code, reason, **context):
        self.issue = issue(code, reason, **context)
        super().__init__(reason)


class EvaluationFailure(ValueError):
    """A stopped phase, with its precise causes available to the API and report."""

    def __init__(self, message, issues):
        self.issues = issues
        primary = sorted(issues, key=lambda e: e["category"] == "coverage")
        super().__init__(message + (" " + "; ".join(describe(e) for e in primary[:2]) if issues else ""))


def stopped(code, reason, *, category, stage, unit_id=None, **context):
    return EvaluationFailure("Evaluation stopped.", [issue(code, reason, category=category, phase="orchestrate",
                                                         stage=stage, unit_id=unit_id, **context)])


def failure_state(issues):
    categories = {e["category"] for e in issues}
    if "cancelled" in categories:
        return "cancelled"
    if "budget" in categories:
        return "budget_exhausted"
    if categories <= {"format", "semantic", "coverage"}:
        return "incomplete"
    return "failed"


def describe(error):
    location = [error.get("unit_id"), error.get("record_type")]
    if error.get("record_index") is not None:
        location.append(f"record {error['record_index']}")
    if error.get("path"):
        location.append("/".join(map(str, error["path"])))
    return f"[{error['code']}] " + ": ".join(str(v) for v in location if v) + ": " + error["reason"]


def protocol_issue(error, **context):
    # ProtocolError contains safe parser diagnostics, not rejected JSON values.
    return issue("response." + error.code, str(error)[:450], category="format" if error.retryable else "provider",
                 phase="decode", **context)


def schema_issue(error, **context):
    """Never echo jsonschema.message: it can contain secrets or huge instances."""
    path = list(error.absolute_path)
    constraint = error.validator
    details = dict(constraint=constraint)
    if constraint in {"maxLength", "minLength", "maxItems", "minItems"}:
        details.update(limit=error.validator_value, actual_count=len(error.instance))
        unit = "characters" if "Length" in constraint else "items"
        bound = "at most" if constraint.startswith("max") else "at least"
        reason = f"Field {'.'.join(map(str, path)) or '$'}: {bound} {error.validator_value} {unit}; received {len(error.instance)}."
        if constraint == "maxLength":
            reason += " Shorten the field."
    elif constraint == "required":
        details["missing_fields"] = sorted(set(error.validator_value) - set(error.instance))
        reason = "Missing required fields: " + ", ".join(details["missing_fields"])
    elif constraint == "type":
        details["expected_type"] = error.validator_value
        reason = f"Expected {error.validator_value}; received {type(error.instance).__name__}"
    elif constraint == "enum":
        details["allowed_values"] = error.validator_value
        reason = "Value must be one of: " + ", ".join(map(str, error.validator_value))
    else:
        reason = f"Field {'.'.join(map(str, path)) or '$'} violates {constraint}"
    return issue("schema." + constraint, reason, category="format", path=path, details=details, **context)


def validate_schema(value, schema):
    # Sorting makes the selected failure independent of JSON object key order.
    errors = sorted(Draft202012Validator(schema).iter_errors(value),
                    key=lambda e: (tuple(map(str, e.absolute_path)), str(e.validator), tuple(map(str, e.absolute_schema_path))))
    if errors:
        error = schema_issue(errors[0])
        raise RuleViolation(error.pop("code"), error.pop("reason"), **{k: v for k, v in error.items() if k != "version"})
