import json

from loom.service.contracts import canonical


def test_agent_recovery_avoids_context_messages_and_finished_plans(agent_service, monkeypatch):
    from contextlib import contextmanager

    from loom.autonomy.supervisor import AgentSupervisor
    from loom.service import session_storage
    from tests.autonomy.test_recovery import dispatched

    service, facade, aid, sid = dispatched(agent_service)
    with service.store.transaction() as db:
        # Large fields must remain untouched even while startup reconciles the
        # active work binding. Corrupt JSON makes accidental hydration fail.
        session_storage.put_field(db, sid, 'root', 'context', '{unread-context')
        session_storage.put_field(db, sid, 'root', 'run', '{}')
        session_storage.put_field(db, sid, 'run', 'id', canonical('recovery-run'))
        session_storage.put_field(db, sid, 'run', 'state', canonical('queued'))
        db.execute('INSERT INTO session_messages VALUES(?,?,?,?,?)', (sid, 999, 999, 'unused', '{unread-message'))
        for identifier, state, kind in (('old', 'completed', 'unified_plan'), ('resume', 'planning', 'unified_plan'),
                                         ('old-planner', 'planning', 'legacy_plan')):
            facade.store.put(db, 'agent_runs', dict(id=identifier, agent_id=aid, state=state, kind=kind))
    original = service.store.transaction
    reads = []

    @contextmanager
    def guarded():
        with original() as db:
            db.set_trace_callback(reads.append)
            yield db
            db.set_trace_callback(None)

    monkeypatch.setattr(service.store, 'transaction', guarded)
    recovered = AgentSupervisor(service, facade.store, service.background)
    recovered.close()
    assert not any('FROM session_messages' in q or "name='context'" in q for q in reads)
    with service.store.read() as db:
        assert json.loads(db.execute("SELECT body FROM agent_runs WHERE id='resume'").fetchone()[0])['state'] == 'recoverable'
        assert json.loads(db.execute("SELECT body FROM agent_runs WHERE id='old-planner'").fetchone()[0])['state'] == 'superseded'
        assert json.loads(db.execute("SELECT body FROM agent_runs WHERE id='old'").fetchone()[0])['state'] == 'completed'
        query_plan = db.execute("EXPLAIN QUERY PLAN SELECT body FROM agent_runs WHERE json_extract(body,'$.state')='planning'").fetchall()
        assert any('agent_runs_planning' in row[3] for row in query_plan)
        work = json.loads(db.execute('SELECT body FROM agent_work_packages WHERE id=(SELECT work_package_id FROM agent_sessions WHERE session_id=?)',
                                    (sid,)).fetchone()[0])
        assert work['state'] == 'dispatched'
        assert 'recovery-run' in work['run_ids']
        assert db.execute("SELECT session_id FROM agent_run_links WHERE run_id='recovery-run'").fetchone()[0] == sid
