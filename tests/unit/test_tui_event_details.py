from loom.tui.event_details import meaningful_fields, readable_event_details


def test_proposal_uses_descriptions_from_new_plan_with_scope_and_command():
    data = {"trace_id": "internal-trace", "at": "internal-time", "goal_digest": "internal-digest",
        "acceptance": {"reason": "Preparing plan", "plan": {"criteria": [{"description": "Old criterion"}]}},
        "proposal": {"criteria": [
            {"id": "planet", "description": "Examine default orbital parameters", "scope": ["src/data/planets.ts"], "verifier": "semantic", "check": {}},
            {"id": "test", "description": "Orbital tests pass", "verifier": "command", "check": {"input": {"argv": ["npm", "test"]}}},
        ], "unresolved_requirements": ["Real-world source needed"]}}
    detail = readable_event_details("acceptance.plan.proposed", data)
    for expected in ("Examine default orbital parameters", "src/data/planets.ts", "Review the result", "npm test", "Real-world source needed"):
        assert expected in detail
    for omitted in ("Old criterion", "internal-trace", "internal-time", "internal-digest"):
        assert omitted not in detail


def test_nested_event_content_and_errors_remain_visible_without_metadata():
    detail = readable_event_details("custom.completed", {"trace_id": "internal-trace", "summary": "Root cause identified", "result": {
        "findings": [{"description": "The request timeout was too short", "evidence": {"path": "config.yaml"}}],
        "error": {"message": "Request timed out", "remediation": {"description": "Increase the timeout"}},
    }})
    for expected in ("Root cause identified", "The request timeout was too short", "config.yaml", "Increase the timeout"):
        assert expected in detail
    assert "internal-trace" not in detail
    fields = meaningful_fields({"results": [{"description": "Finding"}] * 100, "metadata": {"description": "Internal only"}})
    assert len(fields) <= 41
    assert "Preview shortened" in fields[-1][1]
    assert "Internal only" not in str(fields)


def test_workflow_and_workspace_show_work_and_probe_limitations():
    workflow = readable_event_details("workflow.node.started", {"workflow": {"active": "step", "workflow": {"nodes": [
        {"id": "step", "objective": "Find the root cause", "status": "running"},
    ]}}})
    assert "Find the root cause" in workflow
    workspace = readable_event_details("workspace.probed", {"profile": {"workspace": "present", "files": ["package.json"],
        "configuration": [{"path": "package.json", "excerpt": '"test": "vitest"', "excerpt_sha256": "internal-digest"}], "limitations": ["Inventory bounded"]}})
    assert "vitest" in workspace and "Inventory bounded" in workspace
    assert "internal-digest" not in workspace
