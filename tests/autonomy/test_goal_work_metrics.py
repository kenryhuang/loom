import json
import sqlite3

from loom.autonomy.views import add_work_metrics
from loom.llm.api import TokenUsage
from loom.runtime.checkpoints import encode


def test_work_metrics_use_session_details_even_without_run_links():
    db = sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE agent_sessions(agent_id, session_id); CREATE TABLE sessions(id, body);')
    db.execute('INSERT INTO agent_sessions VALUES(?,?)', ('a', 's'))
    run = {'active_seconds': 75, 'counters': encode({'usage': TokenUsage(total_tokens=1234)})}
    db.execute('INSERT INTO sessions VALUES(?,?)', ('s', json.dumps({'run': run})))
    works = [{'id': 'w', 'session_id': 's'}, {'id': 'pending'}, {'id': 'outside', 'session_id': 'other'}]
    add_work_metrics(db, 'a', works)
    assert works[0]['execution_seconds'] == 75
    assert works[0]['actual_tokens'] == 1234
    assert 'execution_seconds' not in works[1]
    assert 'execution_seconds' not in works[2]
