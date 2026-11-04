import http.client
import json
import sqlite3
import threading

import pytest

from loom.service.api import ServiceHTTPServer
from loom.service.contracts import ServiceError
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
