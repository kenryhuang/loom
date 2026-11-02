from loom.autonomy.acceptance import contract_key
from loom.autonomy.migrations import migrate
from loom.autonomy.results import record_session_state
from loom.service.contracts import canonical


def test_migration_clears_only_successful_work_blockers_and_keeps_evidence(agent_service):
    _, facade, aid, _ = agent_service
    with facade.store.sessions.transaction() as db:
        db.execute("DELETE FROM autonomy_migrations WHERE version=4")
        for state in ("succeeded", "failed", "blocked"):
            facade.store.put(
                db,
                "agent_work_packages",
                dict(id=state, agent_id=aid, state=state, blocked_reason="failed", reason="Verify the implementation", run_ids=["original-run"]),
            )
        migrate(db)
        good = facade.store.get(db, "agent_work_packages", "succeeded", aid)
        assert good["blocked_reason"] is None and good["legacy_blocked_reason"] == "failed"
        assert good["reason"] == "Verify the implementation" and good["run_ids"] == ["original-run"]
        for state in ("failed", "blocked"):
            assert facade.store.get(db, "agent_work_packages", state, aid)["blocked_reason"] == "failed"
        migrate(db)
        assert facade.store.get(db, "agent_work_packages", "succeeded", aid) == good


def test_successful_terminal_result_clears_stale_blocker(agent_service):
    _, facade, aid, _ = agent_service
    with facade.store.sessions.transaction() as db:
        facade.store.put(db, "agent_work_packages", dict(id="work", agent_id=aid, goal_id="goal", state="blocked",
                                                       blocked_reason="paused", acceptance=["Verified delivery"]))
        work = facade.store.get(db, "agent_work_packages", "work", aid)
        event = dict(seq=1, type="acceptance.gate.passed", run_id="run")
        db.execute("INSERT INTO events VALUES(?,?,?)", ("session", 1, canonical(event)))
        state = dict(
            session_id="session",
            event_cursor=1,
            autonomy=dict(agent_id=aid, goal_id="goal", work_package_id="work", acceptance_contract=contract_key(work["acceptance_criteria"])),
            task=dict(state="idle"),
            run=dict(id="run", state="completed", reason=""),
        )
        record_session_state(db, state)
        work = facade.store.get(db, "agent_work_packages", "work", aid)
        assert work["state"] == "succeeded" and work["blocked_reason"] is None
