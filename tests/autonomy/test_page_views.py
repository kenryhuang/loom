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


@pytest.mark.parametrize('name', ['overview', 'profile', 'knowledge'])
def test_unrelated_tabs_never_read_session_bodies(agent_service, monkeypatch, name):
    service, facade, aid, _ = agent_service
    original = service.store.read

    @contextmanager
    def guarded():
        with original() as db:
            db.set_authorizer(lambda action, table, column, *_:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'sessions' and column == 'body' else sqlite3.SQLITE_OK)
            yield db

    monkeypatch.setattr(service.store, 'read', guarded)
    assert page(facade, aid, name)['pending_decisions'] == []


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
