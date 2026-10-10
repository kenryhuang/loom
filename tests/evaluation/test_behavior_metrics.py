from loom.evaluation.behavior_metrics import endpoint_metrics


def node(identifier, kind, line):
    return {"id": identifier, "kind": kind, "evidence_refs": [{"line_number": line}]}


def edge(a, kind, b, line):
    return {"from": a, "kind": kind, "to": b, "evidence_refs": [{"line_number": line}]}


def facts():
    return {
        "episodes": [{"id": "episode", "start_line": 1, "end_line": 10}],
        "event_episode_ids": {str(i): "episode" for i in range(1, 11)},
        "model_calls": [{"id": str(i), "episode_id": "episode", "request_ref": {"line_number": i}} for i in (3, 5, 10)],
        "token_ledger": [{"round_id": str(i), "total_tokens": 15} for i in (3, 5, 10)],
        "tool_uses": [],
    }


def test_adoption_lag_starts_at_actual_visibility_not_evidence_existence():
    data = facts()
    graph = {"nodes": [node("evidence", "evidence", 2), node("decision", "decision", 10)], "edges": [edge("decision", "uses", "evidence", 10)]}
    assert endpoint_metrics(data, graph)["evidence_adoption_lag"]["status"] == "unknown"
    data["tool_uses"] = [
        {"raw_output_ref": {"line_number": 2}, "injections": [{"ref": {"line_number": 8}, "raw_output_preserved": True, "source_identity": "tool_call_id"}]}
    ]
    metric = endpoint_metrics(data, graph)["evidence_adoption_lag"]
    assert metric["start_line"] == 8 and metric["end_line"] == 10
    assert metric["known_tokens"] == 15


def test_root_cause_claim_and_supporting_experiment_are_different_endpoints():
    graph = {
        "nodes": [node("problem", "problem", 1), node("cause", "hypothesis", 3), node("test", "evidence", 5), node("validation", "evidence", 10)],
        "edges": [edge("cause", "resolves", "problem", 3), edge("validation", "verifies", "cause", 10)],
    }
    assert endpoint_metrics(facts(), graph)["cost_to_supported_cause"]["status"] == "unknown"
    graph["edges"].append(edge("test", "supports", "cause", 5))
    metrics = endpoint_metrics(facts(), graph)
    assert metrics["cost_to_supported_cause"]["end_line"] == 5
    assert metrics["cause_to_verification"]["start_line"] == 5
    assert metrics["cause_to_verification"]["end_line"] == 10


def test_later_success_does_not_imply_causal_recovery():
    graph = {"nodes": [node("failure", "problem", 1), node("unrelated", "evidence", 10)], "edges": []}
    assert endpoint_metrics(facts(), graph)["causal_recovery_cost"]["status"] == "unknown"
