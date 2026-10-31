import json
import sqlite3

import pytest

from loom.memory.store import SUMMARY_CHARS, MemoryError, MemoryStore, serialized
from loom.service.store import SessionStore
from tests.memory.helpers import fact, seed


def test_review_library_browses_beyond_runtime_search_and_keeps_scope(tmp_path):
    store = SessionStore(tmp_path / 'data')
    workspace = tmp_path / 'project'
    records = [{**fact(f'fact-{index:02}'), 'title': f'Fact {index}'} for index in range(25)]
    records.append({**fact('method'), 'kind': 'workflows', 'steps': ['Inspect identifiers'], 'validation': 'proposed'})
    seed(store, workspace, records=records)
    seed(store, tmp_path / 'other', evaluation='private')
    memory = MemoryStore(store.path, workspace=workspace)
    page = memory.browse(kind='facts', review='unreviewed', limit=20)
    assert page['total'] == 25 and len(page['records']) == 20 and page['next_offset'] == 20
    following = memory.browse(kind='facts', offset=20)
    assert len(following['records']) == 5 and following['next_offset'] is None
    assert not {r['id'] for r in page['records']} & {r['id'] for r in following['records']}
    method = memory.browse(kind='workflows')['records'][0]
    assert method['steps'] == ['Inspect identifiers'] and method['citations']
    assert not memory.browse(query='unrelated-payments')['records']
    assert not memory.browse(review='reviewed')['records']
    assert all(r['source']['evaluation_id'] == 'eval-memory' for r in memory.browse(limit=50)['records'])
    for arguments in ({'limit': 500}, {'offset': -1}, {'review': 'approved'}, {'kind': 'unknown'}):
        with pytest.raises(MemoryError):
            memory.browse(**arguments)


def test_quarantined_stale_and_legacy_unverified_records_never_enter_runtime_search(tmp_path):
    store = SessionStore(tmp_path / 'data')
    good = {**fact('published'), 'publication_state': 'published'}
    rejected = [{**fact(state), 'publication_state': state} for state in ('quarantined', 'stale', 'candidate', 'legacy')]
    seed(store, tmp_path, records=[good, *rejected])
    memory = MemoryStore(store.path, workspace=tmp_path)
    rows = memory.search('planets')['records']
    assert len(rows) == 1 and rows[0]['source']['record_id'] == 'published'
    for state in ('quarantined', 'stale', 'candidate', 'legacy'):
        with pytest.raises(MemoryError):
            memory.read('eval-memory', state)


def test_scope_citations_partial_chinese_and_delete(tmp_path):
    store = SessionStore(tmp_path / 'data')
    workspace = tmp_path / 'solar'
    sid = seed(store, workspace)
    seed(store, tmp_path / 'other', evaluation='private')
    memory = MemoryStore(store.path, workspace=workspace)
    result = memory.search('修复行星渲染')
    assert len(result['records']) == 1
    source = result['records'][0]['source']
    assert source['evaluation_state'] == 'incomplete'
    assert source['session_id'] == sid
    assert source['run_id'] == 'historical-run'
    full = memory.read('eval-memory', 'memory:planet', limit=100)
    assert full['next_offset'] == 100
    rest = memory.read('eval-memory', 'memory:planet', offset=100)
    assert json.loads(full['text'] + rest['text'])['citations'][0]['quote'] == 'duplicate planet IDs'
    assert rest['next_offset'] is None
    assert not memory.search('unrelated payments')['records']
    with pytest.raises(MemoryError, match='outside'):
        memory.read('private', 'memory:planet')
    with store.transaction() as db:
        db.execute('DELETE FROM semantic_jobs WHERE id=?', ('eval-memory',))
    assert not memory.search('planets')['records']
    with pytest.raises(MemoryError):
        memory.read('eval-memory', 'memory:planet')


def test_workspace_canonical_legacy_no_workspace_and_deleted_session(tmp_path):
    store = SessionStore(tmp_path / 'data')
    workspace = tmp_path / 'solar'
    workspace.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(workspace, target_is_directory=True)
    sid = seed(store, workspace, frozen_scope=False)
    assert MemoryStore(store.path, workspace=alias).search('planets')['records']
    assert not MemoryStore(store.path).search('planets')['records']
    # Workspace-less tasks cannot read project memories, even with the same session ID.
    assert not MemoryStore(store.path, session_id=sid).search('planets')['records']
    no_workspace = seed(store, None, evaluation='general')
    assert MemoryStore(store.path, session_id=no_workspace).search('planets')['records']
    assert not MemoryStore(store.path, session_id='another').search('planets')['records']
    with store.transaction() as db:
        body = json.loads(db.execute('SELECT body FROM sessions WHERE id=?', (sid,)).fetchone()[0])
        body['deleted_at'] = 'now'
        db.execute('UPDATE sessions SET body=? WHERE id=?', (json.dumps(body), sid))
    assert not MemoryStore(store.path, workspace=workspace).search('planets')['records']


def test_frozen_scope_cannot_follow_later_session_mutation(tmp_path):
    store = SessionStore(tmp_path / 'data')
    sid = seed(store, tmp_path / 'old')
    store.update(sid, lambda state, emit, db: state['task'].update(workspace=str(tmp_path / 'new')))
    assert not MemoryStore(store.path, workspace=tmp_path / 'new').search('planets')['records']
    assert MemoryStore(store.path, workspace=tmp_path / 'old').search('planets')['records']


def test_dedup_conflicting_status_budget_and_malformed_record(tmp_path):
    store = SessionStore(tmp_path / 'data')
    records = [fact(), fact('duplicate')]
    inferred = fact('inferred')
    inferred['basis'] = 'inferred'
    records.append(inferred)
    invalid = fact('bad')
    invalid['source_digest'] = 'wrong-source'
    records.append(invalid)
    seed(store, tmp_path, records=records)
    result = MemoryStore(store.path, workspace=tmp_path).search('planets')
    assert len(result['records']) == 2
    assert {r['basis'] for r in result['records']} == {'observed', 'inferred'}
    assert result['issues'][0]['code'] == 'memory.record_invalid'
    many = []
    for i in range(510):
        row = fact('many-' + str(i))
        row['summary'] = 'planet rendering ' + str(i) + 'x' * 1000
        many.append(row)
    seed(store, tmp_path, records=many, evaluation='many')
    result = MemoryStore(store.path, workspace=tmp_path).search('planets')
    assert result['truncated'] and len(result['records']) <= 6
    assert len(serialized(result['records'])) <= SUMMARY_CHARS


def test_store_failures_are_explicit_and_never_create_database(tmp_path):
    path = tmp_path / 'absent.sqlite'
    with pytest.raises(MemoryError) as caught:
        MemoryStore(path, workspace=tmp_path).search('planets')
    assert caught.value.code == 'memory.store_unavailable'
    assert not path.exists()
    with sqlite3.connect(path):
        pass
    with pytest.raises(MemoryError, match='sessions table missing'):
        MemoryStore(path).search('planets')
    store = SessionStore(tmp_path / 'empty')
    assert not MemoryStore(store.path, workspace=tmp_path).search('planets')['records']
    with pytest.raises(MemoryError) as caught:
        MemoryStore(store.path).read('e', 'r', limit=100000)
    assert caught.value.code == 'memory.query_invalid'


def test_actual_execution_binding_controls_legacy_scope(tmp_path):
    store = SessionStore(tmp_path / 'data')
    sid = seed(store, tmp_path, frozen_scope=False)
    actual = tmp_path / 'actual'
    actual.mkdir()
    store.update(sid, lambda state, emit, db: state['task'].update(task_spec={
        'session_environment': {'resources': [{'id': 'code', 'kind': 'directory', 'uri': str(actual)}]},
        'execution_runtime': {'workspace_resource': 'code'},
    }))
    assert not MemoryStore(store.path, workspace=tmp_path).search('planets')['records']
    assert MemoryStore(store.path, workspace=actual).search('planets')['records']


def test_lessons_and_proposed_workflows_keep_status_contributions_are_not_reused(tmp_path):
    store = SessionStore(tmp_path / 'data')
    lesson = fact('lesson')
    lesson.update(kind='lessons', validation='observed_recovery', trigger='Duplicate planets',
                  failed_approach='Count draw calls', failure_reason='Draw calls include nonplanet geometry',
                  better_approach='Inspect planet identifiers')
    workflow = fact('workflow')
    workflow.update(kind='workflows', validation='proposed', preconditions='Planet data is available',
                    steps=['Inspect identifiers', 'Check rendered bodies'], avoid='Guessing from draw calls',
                    expected_benefit='Narrow the investigation', preserve='Visual acceptance')
    contribution = fact('contribution')
    contribution.update(kind='contributions', rating='key')
    seed(store, tmp_path, records=[lesson, workflow, contribution])
    memory = MemoryStore(store.path, workspace=tmp_path)
    result = memory.search('duplicate planets')
    assert {r['kind'] for r in result['records']} == {'lessons', 'workflows'}
    proposed = next(r for r in result['records'] if r['kind'] == 'workflows')
    assert proposed['validation'] == 'proposed' and proposed['preconditions'] == 'Planet data is available'
    full = json.loads(memory.read('eval-memory', 'workflow')['text'])
    assert full['steps'] == workflow['steps'] and full['preserve'] == 'Visual acceptance'
