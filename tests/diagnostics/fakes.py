import json
from pathlib import Path

from loom.core import ok
from loom.llm import LlmResponse
from loom.llm.api import LlmToolCall, TokenUsage
from tests.acceptance_fakes import FixtureVerifier


def report(ref):
    return {
        "summary": "Insufficient historical evidence; no files changed.",
        "status": "inconclusive",
        "findings": [
            {
                "title": "Missing historical configuration",
                "layer": "configuration",
                "confidence": "unknown",
                "cause": "The current snapshot cannot establish historical model limits.",
                "evidence_ids": [ref],
                "recommendation": "Compare the recorded model request with the configured input budget.",
                "validation": "Not executed: reproduce with a bounded input.",
                "risk": "Larger inputs increase cost.",
            }
        ],
        "causal_chain": [],
        "coverage": {"checked": ["Snapshot overview"], "missing": ["Historical configuration"]},
    }


class Provider:
    model = "diagnosis-fixture"
    verification_provider = FixtureVerifier()

    def __init__(self, state):
        resources = state["task"]["task_spec"]["session_environment"]["resources"]
        self.directory = Path(next(r["uri"] for r in resources if r["kind"] == "diagnosis_snapshot"))

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        ledger = json.loads((self.directory / "ledger.json").read_text())
        if not ledger["entries"]:
            call = LlmToolCall("overview", "diagnosis_overview", "{}")
        else:
            call = LlmToolCall("report", "finish", json.dumps({"report": json.dumps(report(next(iter(ledger["entries"]))))}))
        return ok(LlmResponse("", (call,), usage=TokenUsage(10, 5, 15)))


def factory(state):
    return Provider(state)


class VerificationProvider(Provider):
    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        from loom.diagnostics.workspace import DiagnosisWorkspace

        workspace = DiagnosisWorkspace(self.directory)
        ledger = json.loads((self.directory / "ledger.json").read_text())
        if not ledger["entries"]:
            name, args = "diagnosis_overview", {}
        elif not (workspace.path / "test_regression.py").exists():
            name, args = (
                "write_file",
                {
                    "path": "test_regression.py",
                    "content": "import unittest\nfrom sample import value\nclass Test(unittest.TestCase):\n"
                    " def test_value(self):\n  self.assertEqual(value, 3)\n",
                },
            )
        elif not workspace.meta["checks"] or len(workspace.meta["checks"]) == 1 and (workspace.path / "sample.py").read_text() == "value = 3\n":
            name, args = "run_validation", {"argv": ["python", "-m", "unittest", "test_regression"], "timeout_seconds": 10}
        elif (workspace.path / "sample.py").read_text() == "value = 2\n":
            name, args = "edit_file", {"path": "sample.py", "old_text": "value = 2", "new_text": "value = 3"}
        elif not any(key.startswith("workspace_diff:") for key in ledger["entries"]):
            name, args = "workspace_diff", {}
        else:
            result = report(next(iter(ledger["entries"])))
            result["summary"] = "Reproduced and verified in diagnostic worktree only."
            finding = result["findings"][0]
            finding["evidence_ids"] = [key for key in ledger["entries"] if key.startswith("run_validation:")]
            finding["validation"] = "Regression failed before the change and passed afterwards."
            finding["validation_status"] = "passed"
            name, args = "finish", {"report": json.dumps(result)}
        return ok(LlmResponse("", (LlmToolCall(name + str(ledger["tool_calls"]), name, json.dumps(args)),), usage=TokenUsage(10, 5, 15)))


def verification_factory(state):
    return VerificationProvider(state)
