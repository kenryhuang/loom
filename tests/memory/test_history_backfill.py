import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from loom.llm.api import LlmResponse, TokenUsage

spec = importlib.util.spec_from_file_location("memory_history_maintenance", Path(__file__).parents[2] / "scripts/consolidate_memory_history.py")
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)


def test_paid_response_survives_budget_settlement_lock(tmp_path):
    class LockedLedger:
        def settle(self, identifier, actual):
            raise sqlite3.OperationalError("database is locked")

    receipt = tmp_path / "paid-response.json"
    provider = history.ReceiptBudgetProvider(SimpleNamespace(model="test"), LockedLedger(), {}, receipt=receipt, metadata={"tasks": ["job-a"]})
    response = LlmResponse(content='{"results":[]}', usage=TokenUsage(total_tokens=123))
    provider._settle("call-a", response)
    saved = json.loads(receipt.read_text())
    assert saved["reservation_id"] == "call-a"
    assert saved["response"]["usage"]["total_tokens"] == 123
    assert saved["response"]["content"] == response.content


def test_batch_rejects_missing_tasks_and_foreign_candidates():
    tasks = [{"target": "v-a", "candidates": ["v-b"]}]
    aliases = {"0": "v-a", "1": "v-b", "2": "v-foreign"}
    with pytest.raises(ValueError, match="Missing"):
        history.validate_result({"results": []}, tasks, aliases)
    result = {"results": [{"task": "0", "comparisons": [{"candidate": "2", "relation": "equivalent", "reason": "Wrong namespace"}]}]}
    with pytest.raises(ValueError, match="Unknown"):
        history.validate_result(result, tasks, aliases)


def test_compact_payload_preserves_conditions_and_distinct_evidence():
    nodes = {
        "a": {
            "record": {"title": "fact", "summary": "fact", "scope": "dev", "dependencies": {"evidence": "hash", "environment": "dev"}},
            "observations": [{"id": "o-a", "evidence_group_id": "root-a", "evaluation_id": "run-a", "observed_at": None}],
        },
        "b": {
            "record": {"title": "fact", "summary": "fact", "scope": "prod"},
            "observations": [{"id": "o-b", "evidence_group_id": "root-b", "evaluation_id": "run-b", "observed_at": "2026-01-01"}],
        },
    }
    payload, _ = history.payload_for([{"target": "a", "candidates": ["b"]}], nodes)
    a, b = payload["nodes"].values()
    assert a["record"]["dependencies"] == {"environment": "dev"}
    assert a["record"]["scope"] != b["record"]["scope"]
    assert a["evidence"][0]["root"] != b["evidence"][0]["root"]
    assert a["evidence"][0]["episode"] != b["evidence"][0]["episode"]
    assert a["evidence"][0]["observed_at"] is None
    assert b["evidence"][0]["observed_at"] == "2026-01-01"
