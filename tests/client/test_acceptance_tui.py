import json
from copy import deepcopy

import pytest

from loom.client.tui import SessionTuiApp
from loom.tui.acceptance import AcceptancePanel
from loom.tui.compact import CompactEventFeedWidget, CompactLoomTuiApp
from loom.tui.tui_collector import TuiEvent, TuiEventCollector
from tests.client.test_tui import FakeClient


def acceptance(state="verifying"):
    return {"state": state, "revision": 2, "reason": "Checking deliverable", "plan": {
        "criteria": [{"id": "doc", "description": "Analysis saved in workspace"}, {"id": "goal", "description": "Full goal satisfied"}],
        "unresolved_requirements": []}, "results": [{"criterion_id": "doc", "status": "passed"}]}


@pytest.mark.asyncio
async def test_session_acceptance_restores_without_history_updates_and_clears_on_switch():
    client = FakeClient()
    client.states["one"].update(run={"id": "r"}, acceptance_run_id="r", acceptance=acceptance(), acceptance_current_check="Checking goal coverage")
    app = SessionTuiApp(client, "one")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        panel = app.query_one(AcceptancePanel)
        assert "Verifying · 1/2 checks passed" in panel.render().plain
        assert "Checking goal coverage" in panel.render().plain
        assert "[pending] Full goal satisfied" in panel.render().plain
        result = acceptance("needs_repair")
        result["results"].append({"criterion_id": "goal", "status": "failed", "reason": "Missing root cause evidence"})
        assert app.apply_session_event({"session_id": "one", "run_id": "r", "seq": 1, "type": "acceptance.gate.blocked",
            "payload": {"acceptance": result}}, app.subscription_generation)
        assert "Needs repair" in panel.render().plain
        assert "Missing root cause evidence" in panel.render().plain
        assert "Checking goal coverage" not in panel.render().plain
        await app.select_session("two")
        assert "Missing root cause" not in panel.render().plain
        assert "Automatic" in panel.render().plain


@pytest.mark.asyncio
async def test_one_shot_acceptance_is_visible_outside_folded_process_and_resets_per_run():
    app = CompactLoomTuiApp(TuiEventCollector())
    async with app.run_test(size=(100, 30)) as pilot:
        app._handle_event(TuiEvent(0, "acceptance.plan.accepted", {"acceptance": acceptance("not_ready")}, run_id="r"))
        app._handle_event(TuiEvent(0, "verification.started", {"acceptance": acceptance(), "description": "Run targeted tests"}, run_id="r"))
        await pilot.pause()
        panel = app.query_one(AcceptancePanel)
        assert "Run targeted tests" in panel.render().plain
        assert panel.region.height > 0
        assert "[passed] Analysis saved" in panel.render().plain
        app._handle_event(TuiEvent(0, "run.started", {}, run_id="next"))
        assert "Run targeted tests" not in panel.render().plain


@pytest.mark.asyncio
async def test_old_run_acceptance_and_markup_are_not_projected_as_current():
    app = CompactLoomTuiApp(TuiEventCollector())
    async with app.run_test(size=(100, 30)):
        panel = app.query_one(AcceptancePanel)
        value = deepcopy(acceptance("blocked"))
        value["reason"] = "[red]Untrusted text[/red]"
        panel.restore({"run": {"id": "r"}, "acceptance_run_id": "r", "acceptance": value})
        assert "[red]Untrusted text[/red]" in panel.render().plain
        panel.restore({"run": {"id": "new"}, "acceptance_run_id": "r", "acceptance": value})
        assert "Untrusted text" not in panel.render().plain


@pytest.mark.asyncio
async def test_acceptance_feed_groups_checks_by_round_and_expands_failures():
    app = CompactLoomTuiApp(TuiEventCollector())
    async with app.run_test(size=(120, 40)) as pilot:
        feed = app.query_one(CompactEventFeedWidget)
        value = {**acceptance(), "attempts": 1, "goal_digest": "goal"}
        for kind in ("acceptance.verifying", "verification.started", "verification.completed"):
            app._handle_event(TuiEvent(0, kind, {"acceptance": deepcopy(value)}, run_id="r"))
        assert len(feed.acceptance_items) == 1
        item = next(iter(feed.acceptance_items.values()))
        assert not item.is_expanded
        value["results"].append({"criterion_id": "goal", "status": "passed", "reason": "Goal verified", "evidence_ids": ["candidate"]})
        value["state"] = "passed"
        app._handle_event(TuiEvent(0, "acceptance.gate.passed", {"acceptance": deepcopy(value)}, run_id="r"))
        assert len(item.group.records) == 1
        assert "Acceptance passed · 2/2" in item.event.data["summary"]
        item.set_expanded(True)
        await pilot.pause()
        assert "Goal verified" in item._details.render().plain
        assert "Evidence: candidate" in item._details.render().plain

        value.update(attempts=2, state="needs_repair")
        value["results"][-1].update(status="failed", reason="Missing proof")
        app._handle_event(TuiEvent(0, "verification.completed", {"acceptance": deepcopy(value)}, run_id="r"))
        app._handle_event(TuiEvent(0, "acceptance.gate.blocked", {"acceptance": deepcopy(value)}, run_id="r"))
        assert len(feed.acceptance_items) == 2
        failed = list(feed.acceptance_items.values())[-1]
        assert failed.is_expanded and failed.group.is_expanded
        assert "1/2 checks passed" in failed.event.data["summary"]
        assert "Missing proof" in failed._details.render().plain
        app._handle_event(TuiEvent(0, "acceptance.gate.blocked", {"acceptance": deepcopy(value)}, run_id="next"))
        assert len(feed.acceptance_items) == 3


@pytest.mark.asyncio
async def test_proposed_plan_feed_displays_descriptions_without_raw_envelope():
    app = CompactLoomTuiApp(TuiEventCollector())
    async with app.run_test(size=(120, 40)) as pilot:
        app._handle_event(TuiEvent(0, "acceptance.plan.proposed", {
            "trace_id": "internal-trace", "proposal": {"criteria": [{"id": "planet", "description": "Examine planetary orbital parameters",
                "scope": ["src/data/planets.ts"], "verifier": "semantic", "check": {}}]},
            "acceptance": acceptance("not_ready"),
        }, run_id="r"))
        feed = app.query_one(CompactEventFeedWidget)
        item = feed.process_groups["r"].records[0]
        assert "Proposed acceptance plan" in item.event.data["summary"]
        item.set_expanded(True)
        await pilot.pause()
        assert "Examine planetary orbital parameters" in item._details.render().plain
        assert "src/data/planets.ts" in item._details.render().plain
        assert "internal-trace" not in item._details.render().plain


@pytest.mark.asyncio
async def test_session_verification_artifact_loads_content_without_event_type():
    class ArtifactClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.loaded = []

        def artifact(self, sid, digest):
            self.loaded.append((sid, digest))
            return json.dumps({"goal": {"objective": "Check planetary orbital speeds"},
                "plan": {"criteria": [{"description": "Compare orbital speeds with real values", "scope": ["src/data/planets.ts"], "verifier": "semantic"}]},
                "binding_before": {"candidate": "internal-fingerprint"},
                "evidence": [{"id": "candidate", "content": "Orbital analysis"}, {"id": "tool:one", "tool": "shell_execute",
                    "input": {"command": "node speeds.js"}, "output": json.dumps({"stdout": "Earth: 29.78 km/s", "exit_code": 0})}]})

    client = ArtifactClient()
    app = SessionTuiApp(client, "one")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.apply_session_event({"session_id": "one", "run_id": "r", "seq": 1, "type": "artifact.created",
            "payload": {"artifact": {"kind": "verification_evidence", "sha256": "proof"}}}, app.subscription_generation)
        feed = app.query_one(CompactEventFeedWidget)
        item = feed.process_groups["r"].records[0]
        assert not client.loaded
        item.set_expanded(True)
        await pilot.pause()
        assert client.loaded == [("one", "proof")]
        assert item.loaded_artifact == "proof" and not item.detail_error
        detail = item._details.render().plain
        for text in ("Check planetary orbital speeds", "Compare orbital speeds", "src/data/planets.ts", "Orbital analysis", "Earth: 29.78 km/s"):
            assert text in detail
        assert "internal-fingerprint" not in detail
