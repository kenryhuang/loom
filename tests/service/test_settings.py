import json

import pytest

from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from loom.service.settings import Settings
from loom.tasks.config import load_task_config, parse_yaml_document


def test_settings_atomic_roundtrip_secrets_and_revision(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('default_model: main\nmodels:\n  main:\n    model: test\n    api_key: private-value\n')
    service = LoomService(tmp_path / 'data', config_path=path)
    try:
        settings = Settings(service)
        value = settings.get()
        assert 'private-value' not in json.dumps(value)
        value['config']['models']['main']['temperature'] = .3
        value['config']['task'] = {'objective': 'Explain "quoted" text\nwith another line'}
        value['config']['embedding_profiles'] = [{'id': 'emb_test', 'name': 'Test', 'model': 'embed',
                                                 'endpoint': 'http://localhost:1234/embeddings', 'api_key_env': 'EMBED_KEY'}]
        saved = settings.save({k: value[k] for k in ('revision', 'config')})
        assert saved['revision'] != value['revision']
        config = load_task_config(path).unwrap()
        assert config.models['main'].api_key == 'private-value'
        assert config.task.objective == value['config']['task']['objective']
        assert service.store.knowledge.profiles()[0]['id'] == 'emb_test'
        with pytest.raises(ServiceError, match='changed'):
            settings.save({k: value[k] for k in ('revision', 'config')})
        saved['config']['models']['main']['unknown_parameter'] = True
        before = path.read_bytes()
        with pytest.raises(ServiceError):
            settings.save({k: saved[k] for k in ('revision', 'config')})
        assert path.read_bytes() == before
    finally:
        service.close()


def test_embedding_profile_registration_writes_yaml(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('models: {}\n')
    service = LoomService(tmp_path / 'data', config_path=path)
    try:
        profile = service.store.knowledge.create_profile({'name': 'Local', 'model': 'embed', 'endpoint': 'http://localhost:1234/embeddings'})
        saved = parse_yaml_document(path.read_text(), path).unwrap()
        assert saved['embedding_profiles'] == [profile]
        assert service.store.knowledge.profiles() == [profile]
    finally:
        service.close()
