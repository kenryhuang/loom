"""Trusted report contract and evidence-backed acceptance for diagnosis runs."""

import json

from loom.diagnostics.evidence import EvidenceView

REPORT_CONTRACT = """Return finish(report=<JSON string>) with this schema:
{"summary":"conclusion; distinguish diagnosis from fixing the target",
 "status":"identified|inconclusive",
 "findings":[{"title":"symptom", "layer":"framework|configuration|assembly|external|unknown",
 "confidence":"confirmed|hypothesis|unknown", "cause":"direct blocker and likely root cause",
 "evidence_ids":["exact id returned by a diagnosis tool"],
 "recommendation":"where/what to change and expected effect, or how to obtain missing evidence",
 "validation_status":"not_run|passed|failed|timeout",
 "validation":"executed checks with receipt citations, results and remaining proposed steps", "risk":"possible impact"}],
 "causal_chain":[{"from":"trigger", "to":"observable effect", "relation":"observed|inferred", "evidence_ids":["id"]}],
 "coverage":{"checked":["areas inspected"],"missing":["unavailable evidence or unresolved alternatives"]}}
At least one finding and evidence reference are required, even for an inconclusive report.
Only cite complete returned page IDs. Limit citations to 12 distinct pages, report to 24000 characters.
Historical evidence and current source/configuration may differ; only claim tests ran when a run_validation receipt supports that statement.
"""


def validate_report(candidate, ledger):
    if not isinstance(candidate, str) or len(candidate) > 24000:
        raise ValueError("Diagnosis report must be a JSON string of at most 24000 characters")
    try:
        report = json.loads(candidate)
    except ValueError as exc:
        raise ValueError("Diagnosis report must be a JSON object, without markdown fences") from exc
    if not isinstance(report, dict):
        raise ValueError("Diagnosis report must be an object")

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    if not text(report.get("summary")) or report.get("status") not in {"identified", "inconclusive"}:
        raise ValueError("Diagnosis requires a summary and identified/inconclusive status")
    findings = report.get("findings")
    if not isinstance(findings, list) or not 1 <= len(findings) <= 12:
        raise ValueError("Diagnosis requires 1 to 12 findings")
    cited = set()

    def citations(row):
        ids = row.get("evidence_ids")
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(i, str) or i not in ledger["entries"] or not ledger["entries"][i]["content"].strip() for i in ids)
        ):
            raise ValueError("Every finding and causal link must cite evidence actually read in this diagnosis")
        cited.update(ids)

    for row in findings:
        if not isinstance(row, dict) or any(not text(row.get(k)) for k in ("title", "cause", "recommendation", "validation", "risk")):
            raise ValueError("Findings require title, cause, recommendation, validation and risk")
        if row.get("layer") not in {"framework", "configuration", "assembly", "external", "unknown"} or row.get("confidence") not in {
            "confirmed",
            "hypothesis",
            "unknown",
        }:
            raise ValueError("Invalid finding layer or confidence")
        citations(row)
        validation = row.get("validation_status", "not_run")
        if validation not in {"not_run", "passed", "failed", "timeout"}:
            raise ValueError("Invalid validation_status")
        if validation != "not_run" and not any(ledger.get("validations", {}).get(ref, {}).get("status") == validation for ref in row["evidence_ids"]):
            raise ValueError("Executed validation requires a cited run_validation receipt with the matching outcome")
    if report["status"] == "identified" and not any(r["confidence"] == "confirmed" for r in findings):
        raise ValueError("Identified diagnosis requires a confirmed finding")
    chain = report.get("causal_chain", [])
    if not isinstance(chain, list) or len(chain) > 20:
        raise ValueError("Invalid causal chain")
    for row in chain:
        if not isinstance(row, dict) or not text(row.get("from")) or not text(row.get("to")) or row.get("relation") not in {"observed", "inferred"}:
            raise ValueError("Causal links require endpoints and observed/inferred relation")
        citations(row)
    coverage = report.get("coverage")
    if (
        not isinstance(coverage, dict)
        or any(not isinstance(coverage.get(k), list) or any(not text(v) for v in coverage[k]) for k in ("checked", "missing"))
        or not coverage["checked"]
    ):
        raise ValueError("Coverage requires checked and missing string arrays")
    if report["status"] == "inconclusive" and not coverage["missing"]:
        raise ValueError("Inconclusive diagnosis must explain missing evidence")
    if len(cited) > 12:
        raise ValueError("Cite at most 12 distinct evidence pages")
    return report


def markdown(report):
    lines = ["# Session diagnosis", "", report["summary"], "", f"Conclusion: {report['status']}", ""]
    for row in report["findings"]:
        lines += [
            f"## {row['title']}",
            "",
            f"{row['layer']} · {row['confidence']}",
            "",
            row["cause"],
            "",
            "Evidence: " + ", ".join(row["evidence_ids"]),
            "",
            "Recommendation: " + row["recommendation"],
            "",
            "Validation: " + row["validation"],
            "",
            "Risk: " + row["risk"],
            "",
        ]
    lines += ["## Coverage", "", "Checked: " + "; ".join(report["coverage"]["checked"]), "", "Missing: " + "; ".join(report["coverage"]["missing"])]
    return "\n".join(lines)


class DiagnosisAcceptance:
    def __init__(self, directory):
        self.view = EvidenceView(directory)

    def plan(self):
        return {
            "criteria": [
                {
                    "id": "diagnosis-evidence",
                    "description": "The diagnosis distinguishes facts from hypotheses, connects findings to the cited evidence, "
                    "separates current code/config from historical execution, and provides actionable recommendations "
                    "with validation and coverage gaps. Executed tests must cite actual run_validation receipts, "
                    "including their outcome and tested patch. Never imply diagnostic changes fixed the live service. "
                    "An honest inconclusive report is acceptable when evidence cannot establish a root cause.",
                    "verifier": "semantic",
                    "required": True,
                    "scope": [],
                    "check": {},
                }
            ],
            "unresolved_requirements": [],
        }

    def validate(self, candidate):
        return validate_report(candidate, self.view.ledger())

    def evidence(self, candidate):
        report = self.validate(candidate)
        ids = {i for row in [*report["findings"], *report.get("causal_chain", [])] for i in row["evidence_ids"]}
        entries = self.view.ledger()["entries"]
        return [entries[i] for i in sorted(ids)]
