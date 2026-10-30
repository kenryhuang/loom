from urllib.parse import quote

import pytest

from loom.client.protocol import SessionClient
from loom.service.contracts import ServiceError
from tests.service.test_api import api, new  # noqa: F401
from tests.skills.helpers import Judge, seed


def test_skill_http_auth_scopes_job_version_and_encoded_ids(api):  # noqa: F811
    service, server, client, path = api
    # Real HTTP and service facade, deterministic model; original pointer resolution is independently tested.
    sources = server.skills.catalog
    from tests.skills.helpers import Sources
    fake = Sources(service.store)
    server.semantic.provider_factory = lambda model: Judge()
    with service.store.transaction() as db:
        db.execute('DELETE FROM semantic_jobs')
    for i in range(3):
        seed(service.store, fake, path, i)
    sources.trajectory = fake
    # Initial catalog is reconstructed on first request.
    with service.store.transaction() as db:
        sid = db.execute('SELECT id FROM sessions ORDER BY id LIMIT 1').fetchone()[0]
    query = '?session_id=' + sid
    with pytest.raises(ServiceError) as denied:
        SessionClient(client.url, 'wrong')._json('/v1/skills' + query)
    assert denied.value.status == 401
    listing = client._json('/v1/skills' + query)
    assert listing['catalog']['families'] == 3
    started = client._json('/v1/skills/jobs' + query, dict(query='HTTP connection', model='test'))
    server.skills.futures[started['id']].result(timeout=10)
    job = client._json('/v1/skills/jobs/' + quote(started['id'], safe='') + query)
    assert job['state'] == 'completed'
    path = '/v1/skills/versions/' + quote(job['version_id'], safe='')
    version = client._json(path + query)
    assert version['evidence_state'] == 'proposed'
    package = client._json(path + '/package' + query)
    assert set(package['files']) == {'SKILL.md', 'contract.json', 'evidence.json', 'validation.json', 'examples/README.md'}
    with pytest.raises(ServiceError):
        client._json(path + '/state' + query, dict(state='active', revision=version['revision']))
    assert client._json(path + '/trial' + query, dict(revision=version['revision']))['state'] == 'trial'
    with pytest.raises(ServiceError):
        client._json(path + '/state' + query, [])
