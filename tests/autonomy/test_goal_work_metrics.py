from loom.autonomy.views import add_work_metrics
from loom.llm.api import TokenUsage
from loom.runtime.checkpoints import encode
from loom.service.store import SessionStore


def test_work_metrics_use_session_details_even_without_run_links(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create('metrics', {'objective': 'Read evidence'})['session_id']
    run = {'active_seconds': 75, 'counters': encode({'usage': TokenUsage(total_tokens=1234)})}
    store.update(sid, lambda s, e, db: s.update(run=run))
    works = [{'id': 'w', 'session_id': sid}, {'id': 'pending'}, {'id': 'outside', 'session_id': 'other'}]
    with store.transaction() as db:
        db.execute('CREATE TABLE agent_sessions(agent_id, session_id)')
        db.execute('INSERT INTO agent_sessions VALUES(?,?)', ('a', sid))
        add_work_metrics(db, 'a', works)
    assert works[0]['execution_seconds'] == 75
    assert works[0]['actual_tokens'] == 1234
    assert 'execution_seconds' not in works[1]
    assert 'execution_seconds' not in works[2]
