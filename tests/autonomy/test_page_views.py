import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from loom.service.api import ServiceHTTPServer
from loom.service.contracts import ServiceError
from tests.autonomy.helpers import role
from tests.autonomy.test_dispatch import prepare


def page(facade, aid, name):
    return facade.dispatch('GET', [aid, 'views', name], {})


def test_overview_reads_summaries_while_another_connection_holds_the_write_lock(agent_service):
    _, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    with sqlite3.connect(facade.store.sessions.path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        overview = page(facade, aid, 'overview')
        listing = facade.dispatch('GET', [], {})
        assert overview['view'] == 'overview'
        assert overview['work_packages'] == [dict(id=work['id'], state='pending')]
        assert 'execution' not in overview
        assert not overview['roadmaps'] and not overview['stage_reviews']
        assert listing['agents'][0]['id'] == aid


def test_memory_library_reads_review_revisions_without_acquiring_write_lock(agent_service):
    from tests.memory.helpers import seed

    service, facade, aid, path = agent_service
    seed(service.store, path)
    with sqlite3.connect(service.store.path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        result = facade.dispatch('GET', [aid, 'memory', 'library'], {})
    assert result['total'] == 1
    assert result['records'][0]['source']['annotation_revision'] == 0


def test_analysis_diagnostics_load_only_when_requested_and_do_not_expose_checkpoint_evidence(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work['id'])
    session = facade.snapshot(aid)['sessions'][0]['session_id']
    with service.store.transaction() as db:
        facade.store.put(db, 'agent_learning_cycles', dict(id='diagnostic-cycle', agent_id=aid, work_package_id=work['id'],
            goal_id=work['goal_id'], evaluation_id='diagnostic-job', state='incomplete', attempts=2, coverage='partial'))
        body = dict(id='diagnostic-job', state='incomplete', usage=dict(calls=4), settings=dict(max_calls=20),
            error_details=[dict(code='MISSING_EVIDENCE', reason='Need observed output')],
            checkpoint=dict(large_evidence='never-transmit-this-' * 100000,
                            units={'unit-one': dict(disposition='exhausted', issues=['Request timed out'], evidence='hidden')},
                            memory=dict(units={'ok': dict(disposition='accepted', evidence='also-hidden')})))
        db.execute('INSERT INTO semantic_jobs(id,session_id,cache_key,body) VALUES(?,?,?,?)',
                   (body['id'], session, 'diagnostic-cache', json.dumps(body)))
    overview = page(facade, aid, 'overview')
    work_page = page(facade, aid, 'work')
    assert overview['learning'] == [dict(id='diagnostic-cycle', state='incomplete')]
    assert 'Request timed out' not in json.dumps(work_page)
    assert 'Request timed out' not in json.dumps(facade.snapshot(aid))
    details = facade.dispatch('GET', [aid, 'learning', 'diagnostic-cycle', 'diagnostics'], {})
    assert details['usage']['calls'] == 4
    assert details['units'] == [dict(id='unit-one', disposition='exhausted', issues=['Request timed out'])]
    assert 'never-transmit-this' not in json.dumps(details)
    assert 'evidence' not in details['units'][0]
    assert work_page['execution']['runtime'][0]['task_state'] == 'queued'


@pytest.mark.parametrize('name', ['goals', 'work', 'profile', 'knowledge', 'learning'])
def test_tabs_preserve_required_data_and_skip_other_collections(agent_service, name):
    _, facade, aid, _ = agent_service
    prepare(facade, aid)
    data = page(facade, aid, name)
    assert data['view'] == name
    assert data['role']['workspace']
    if name in {'profile', 'knowledge'}:
        assert not data['work_packages'] and not data['goals'] and not data['learning']
    if name != 'work':
        assert 'execution' not in data


def test_page_routes_keep_agent_and_cycle_ownership_checks(agent_service):
    _, facade, aid, _ = agent_service
    for route in ([aid, 'views', 'unknown'], ['missing', 'views', 'overview'], [aid, 'learning', 'missing', 'diagnostics']):
        with pytest.raises(ServiceError):
            facade.dispatch('GET', route, {})


@pytest.mark.parametrize('name', ['overview', 'goals', 'work', 'profile', 'knowledge', 'learning'])
def test_tabs_never_reconstruct_session_bodies(agent_service, monkeypatch, name):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work['id'])
    sid = facade.store.snapshot(aid)['sessions'][0]['session_id']

    def large_session(state, emit, db):
        from loom.llm.api import TokenUsage
        from loom.runtime.checkpoints import encode

        state['context'] = {'large_context': 'not-for-the-agent-page' * 100000}
        state['run'] = dict(id='projection-run', state='queued', active_seconds=75,
                            counters=encode({'usage': TokenUsage(total_tokens=1234)}),
                            checkpoint={'large_checkpoint': 'also-not-for-the-page' * 100000})
        state['input_request'] = dict(state='pending', id='question', question='Choose direction')

    service.store.update(sid, large_session)
    original = service.store.read
    statements = []
    forbidden = []

    def authorize(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and (table == 'sessions' and column == 'body' or table == 'session_messages'):
            forbidden.append((table, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def guarded():
        with original() as db:
            db.set_authorizer(authorize)
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(service.store, 'read', guarded)
    result = page(facade, aid, name)
    assert not forbidden
    assert not any('SELECT value' in q and ("name='context'" in q or "name='checkpoint'" in q) for q in statements)
    assert 'large_context' not in json.dumps(result) and 'large_checkpoint' not in json.dumps(result)
    if name == 'learning':
        assert result['pending_decisions'][0]['request']['id'] == 'question'
    else:
        assert result['pending_decisions'] == []
    if name == 'goals':
        assert result['work_packages'][0]['execution_seconds'] == 75
        assert result['work_packages'][0]['actual_tokens'] == 1234
    if name == 'work':
        runtime = result['execution']['runtime'][0]
        assert runtime['session_id'] == sid and runtime['active_seconds'] == 75
        assert runtime['checkpoint_available'] is True
        assert any(b['code'] == 'SESSION_INPUT' for b in result['execution']['blockers'])


def test_page_summaries_omit_work_context_but_scoped_detail_keeps_it(agent_service):
    service, facade, aid, workspace = agent_service
    work = prepare(facade, aid)
    with service.store.transaction() as db:
        work['latest_context'] = {'evidence': 'large-private-work-context' * 10000}
        work['binding'] = {'rationale': 'only-needed-in-work-details'}
        facade.store.put(db, 'agent_work_packages', work)
    for view in ('overview', 'goals', 'work', 'learning'):
        data = page(facade, aid, view)
        assert 'large-private-work-context' not in json.dumps(data)
        assert 'only-needed-in-work-details' not in json.dumps(data)
    assert page(facade, aid, 'work')['work_packages'][0]['details_lazy'] is True
    assert set(page(facade, aid, 'learning')['work_packages'][0]) == {'id', 'state', 'objective'}
    detail = facade.dispatch('GET', [aid, 'work-packages', work['id']], {})
    assert detail['latest_context'] == work['latest_context']
    other = facade.store.create('other-agent', role(workspace))
    with pytest.raises(ServiceError):
        facade.dispatch('GET', [other['id'], 'work-packages', work['id']], {})


def test_pending_questions_only_load_for_learning(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work['id'])
    sid = facade.store.snapshot(aid)['sessions'][0]['session_id']
    service.store.update(sid, lambda s, e, db: s.update(input_request=dict(state='pending', id='q', question='Choose direction')))
    assert page(facade, aid, 'learning')['pending_decisions'][0]['request']['id'] == 'q'
    for view in ('overview', 'goals', 'work', 'profile', 'knowledge'):
        assert page(facade, aid, view)['pending_decisions'] == []


def test_goal_projection_keeps_route_history_links_and_boolean_types(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    with service.store.transaction() as db:
        facade.store.put(db, 'agent_roadmaps', dict(id='route-old', agent_id=aid, objective_id='objective', revision=1, baseline='Previous'))
        facade.store.put(db, 'agent_roadmaps', dict(id='route-new', agent_id=aid, objective_id='objective', revision=2, baseline='Current'))
        db.execute('INSERT INTO agent_stages VALUES(?,?,?)', ('stage', aid, json.dumps(dict(
            id='stage', agent_id=aid, objective_id='objective', roadmap_id='route-new', title='Delivery', state='active'))))
        work['superseded_contract'] = False
        facade.store.put(db, 'agent_work_packages', work)
    goals = page(facade, aid, 'goals')
    assert [r['objective_id'] for r in goals['roadmaps']] == ['objective', 'objective']
    assert goals['stages'][0]['roadmap_id'] == 'route-new'
    assert goals['work_packages'][0]['superseded_contract'] is False


def test_planning_activity_is_bounded_scoped_and_refreshes_without_agent_revision_changes(agent_service):
    service, facade, aid, workspace = agent_service
    other = facade.store.create('other-planning-agent', role(workspace))
    with service.store.transaction() as db:
        for index in range(5):
            facade.store.put(db, 'agent_runs', dict(id=f'plan-{index}', agent_id=aid, objective_id='quality',
                kind='unified_plan', phase='stage_review', state='planning', created_at=facade.store.now(),
                decision={'private_proposal': 'do-not-transmit' * 10000}, input_snapshot_ref={'private_input': 'hidden'}))
        for run_id, owner, objective in [('foreign-agent', other['id'], 'quality'), ('foreign-objective', aid, 'other')]:
            facade.store.put(db, 'agent_runs', dict(id=run_id, agent_id=owner, objective_id=objective,
                kind='stage_plan', state='planning'))
    before = page(facade, aid, 'goals')
    assert [r['id'] for r in before['planning_activity']] == ['plan-4', 'plan-3', 'plan-2']
    assert before['planning_activity'][0]['events'] == []
    assert before['planning_activity'][0]['phase'] == 'stage_review'
    with service.store.transaction() as db:
        for attempt in range(25):
            facade.store.event(db, aid, 'plan.protocol.requesting', dict(agent_run_id='plan-4', attempt=attempt + 1))
        facade.store.event(db, aid, 'plan.protocol.rejected', dict(agent_run_id='plan-4', attempt=25, code='truncated',
            issues=[dict(path='/', message='Output truncated')], proposal={'private': 'hidden'}))
        for _ in range(25):
            facade.store.event(db, aid, 'plan.stream.updated', dict(agent_run_id='plan-4', state='streaming'))
        run = facade.store.get(db, 'agent_runs', 'plan-4', aid)
        run['stream'] = dict(state='streaming', channels={'content': {'text': 'Draft so far', 'characters': 12}})
        facade.store.put(db, 'agent_runs', run)
        facade.store.event(db, other['id'], 'plan.protocol.requesting', dict(agent_run_id='plan-4', attempt=999))
        facade.store.event(db, aid, 'schedule.finished', dict(run_id='plan-4', reason='unrelated schedule'))
    after = page(facade, aid, 'goals')
    assert after['revision'] == before['revision']
    assert after['planning_activity'][0]['stream']['channels']['content']['text'] == 'Draft so far'
    events = after['planning_activity'][0]['events']
    assert len(events) == 20 and events[0]['attempt'] == 7
    assert [e['seq'] for e in events] == sorted(e['seq'] for e in events)
    assert events[-1]['message'] == 'Output truncated'
    assert events[-1]['code'] == 'truncated'
    assert all(e['attempt'] != 999 for e in events)
    assert 'private' not in json.dumps(after['planning_activity'])
    assert 'unrelated schedule' not in json.dumps(after['planning_activity'])
    assert 'planning_activity' not in page(facade, aid, 'overview')


def test_http_page_reads_do_not_wait_for_the_worker_admission_lock(agent_service):
    service, _, aid, _ = agent_service
    server = ServiceHTTPServer(('127.0.0.1', 0), service, 'page-test')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    try:
        # The handler runs in another thread. Holding this lock must not block
        # a read through the real HTTP entry point or lazily load background work.
        with service.lock:
            connection.request('GET', f'/v1/agents/{aid}/views/overview', headers={'Authorization': 'Bearer page-test'})
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read())['view'] == 'overview'
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_conditional_page_reads_revalidate_related_records_and_authentication(agent_service):
    service, facade, aid, _ = agent_service
    work = prepare(facade, aid)
    server = ServiceHTTPServer(('127.0.0.1', 0), service, 'page-test')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    path = f'/v1/agents/{aid}/views/overview'
    headers = {'Authorization': 'Bearer page-test'}
    try:
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        etag = response.getheader('ETag')
        before = json.loads(response.read())
        assert response.status == 200 and etag
        headers['If-None-Match'] = etag
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        assert response.status == 304 and response.read() == b''
        with service.store.transaction() as db:
            work['state'] = 'cancelled'
            facade.store.put(db, 'agent_work_packages', work)
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        after = json.loads(response.read())
        assert response.status == 200 and response.getheader('ETag') != etag
        assert after['revision'] == before['revision']  # Agent revision alone cannot version a view.
        connection.request('GET', path, headers={**headers, 'Authorization': 'Bearer wrong'})
        response = connection.getresponse()
        assert response.status == 401
        response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_historical_wait_decision_exposes_reason_without_full_proposal(agent_service):
    _, facade, aid, _ = agent_service
    with facade.store.sessions.transaction() as db:
        facade.store.put(db, 'agent_runs', dict(id='old-wait', agent_id=aid, objective_id='quality', kind='unified_plan',
            state='committed', decision=dict(action='wait', reason='No remaining delivery gap', private='large private proposal')))
    activity = page(facade, aid, 'goals')['planning_activity'][0]
    assert activity['outcome'] == dict(action='wait', reason='No remaining delivery gap')
    assert 'private' not in json.dumps(activity)
    assert 'goal_count' not in activity['outcome']  # Historical counts were not recorded.


def test_planning_activity_keeps_the_leased_run_and_projects_work_relationships(agent_service):
    service, facade, aid, _ = agent_service
    with service.store.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        agent['planner_lease'] = 'plan-0'
        facade.store.put(db, 'agents', agent)
        for index in range(5):
            facade.store.put(db, 'agent_runs', dict(id=f'plan-{index}', agent_id=aid, objective_id='quality',
                kind='work_plan', state='planning', goal_id=f'goal-{index}', work_package_ids=[f'work-{index}']))
    runs = page(facade, aid, 'goals')['planning_activity']
    assert [r['id'] for r in runs] == ['plan-0', 'plan-4', 'plan-3']
    assert [r['is_current'] for r in runs] == [True, False, False]
    assert runs[0]['goal_id'] == 'goal-0'
    assert runs[0]['work_package_ids'] == ['work-0']


def test_stage_planning_history_loads_old_linked_runs_without_other_stages_or_private_inputs(agent_service):
    from loom.autonomy.store import digest

    service, facade, aid, workspace = agent_service
    foreign = facade.store.create('foreign-history', role(workspace))
    with service.store.transaction() as db:
        facade.store.put(db, 'agent_stages', dict(id='old-stage', agent_id=aid, objective_id='quality', state='completed'))
        facade.store.put(db, 'agent_stages', dict(id='foreign-stage', agent_id=foreign['id'], objective_id='quality', state='completed'))
        facade.store.put(db, 'agent_roadmaps', dict(id='old-route', agent_id=aid, stage_id='old-stage', input_run_id='legacy-plan'))
        facade.store.put(db, 'agent_stage_reviews', dict(id='review_' + digest('legacy-review')[:32], agent_id=aid, stage_id='old-stage'))
        facade.store.put(db, 'agent_goals', dict(id='old-goal', agent_id=aid, stage_id='old-stage'))
        for rid, fields in [
            ('legacy-plan', {}), ('legacy-review', {'kind': 'stage_review'}),
            ('work-plan', {'kind': 'work_plan', 'goal_id': 'old-goal'}),
            ('review', {'stage_id': 'old-stage', 'phase': 'stage_review'}),
            ('published', {'outcome': {'stage_id': 'old-stage', 'action': 'propose_stage'}}),
            *[(f'unrelated-{i}', {'stage_id': 'new-stage'}) for i in range(4)],
        ]:
            facade.store.put(db, 'agent_runs', dict(id=rid, agent_id=aid, objective_id='quality',
                kind='unified_plan', state='committed', input_snapshot_ref={'private': 'hidden'},
                decision={'private_proposal': 'hidden'}, **{k: v for k, v in fields.items() if k != 'kind'})
                | ({'kind': fields['kind']} if 'kind' in fields else {}))
        facade.store.put(db, 'agent_runs', dict(id='foreign-run', agent_id=foreign['id'], objective_id='quality',
            kind='unified_plan', state='committed', stage_id='old-stage'))
    assert len(page(facade, aid, 'goals')['planning_activity']) == 3
    result = facade.dispatch('GET', [aid, 'stages', 'old-stage', 'planning-history'], {})
    assert result['stage_id'] == 'old-stage'
    assert [r['id'] for r in result['planning_activity']] == ['published', 'review', 'work-plan', 'legacy-review', 'legacy-plan']
    assert 'private' not in json.dumps(result)
    with pytest.raises(ServiceError):
        facade.dispatch('GET', [aid, 'stages', 'foreign-stage', 'planning-history'], {})
