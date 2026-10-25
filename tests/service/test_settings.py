import json

import pytest

from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from loom.service.settings import Settings
from loom.tasks.config import load_task_config, parse_yaml_document


def test_settings_removes_retired_task_defaults_and_cannot_reintroduce_them(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('models: {}\ntask:\n  objective: Old audit\n  workspace: ~/workspace/yakDB\n')
    service = LoomService(tmp_path / 'data', config_path=path)
    try:
        settings = Settings(service)
        value = settings.get()
        assert 'task' not in value['config']
        saved = settings.save({k: value[k] for k in ('revision', 'config')})
        assert 'task' not in parse_yaml_document(path.read_text(), path).unwrap()
        assert load_task_config(path).ok
        before = path.read_bytes()
        saved['config']['task'] = {'objective': 'Old audit'}
        with pytest.raises(ServiceError, match='Top-level task defaults are no longer supported'):
            settings.save({k: saved[k] for k in ('revision', 'config')})
        assert path.read_bytes() == before
    finally:
        service.close()


def test_settings_atomic_roundtrip_secrets_and_revision(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('default_model: main\nmodels:\n  main:\n    model: test\n    api_key: private-value\n')
    service = LoomService(tmp_path / 'data', config_path=path)
    try:
        settings = Settings(service)
        value = settings.get()
        assert 'private-value' not in json.dumps(value)
        value['config']['providers']['provider_1']['models']['test']['temperature'] = .3
        value['config']['metadata'] = {'description': 'Explain "quoted" text\nwith another line'}
        value['config']['embedding_profiles'] = [{'id': 'emb_test', 'name': 'Test', 'model': 'embed',
                                                 'endpoint': 'http://localhost:1234/embeddings', 'api_key_env': 'EMBED_KEY'}]
        saved = settings.save({k: value[k] for k in ('revision', 'config')})
        assert saved['revision'] != value['revision']
        assert 'models' not in saved['config']
        assert 'models' not in parse_yaml_document(path.read_text(), path).unwrap()
        config = load_task_config(path).unwrap()
        assert config.models['provider_1/test'].api_key == 'private-value'
        assert parse_yaml_document(path.read_text(), path).unwrap()['metadata'] == value['config']['metadata']
        assert service.store.knowledge.profiles()[0]['id'] == 'emb_test'
        with pytest.raises(ServiceError, match='changed'):
            settings.save({k: value[k] for k in ('revision', 'config')})
        saved['config']['providers']['provider_1']['models']['test']['unknown_parameter'] = True
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


def test_settings_rejects_inconsistent_evaluation_budgets_before_writing(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('models:\n  main:\n    model: test\n')
    service = LoomService(tmp_path / 'data', config_path=path)
    try:
        settings = Settings(service)
        value = settings.get()
        value['config']['meta_harness'] = {
            'proposer_model': 'main', 'solver_model': 'main', 'judge_model': 'main',
            'search': {'iterations': 3, 'candidates_per_iteration': 4}, 'budgets': {'max_candidates': 2},
        }
        before = path.read_bytes()
        with pytest.raises(ServiceError, match='meta_harness.budgets.max_candidates'):
            settings.save({k: value[k] for k in ('revision', 'config')})
        assert path.read_bytes() == before
        value['config']['meta_harness']['budgets']['max_candidates'] = 12
        settings.save({k: value[k] for k in ('revision', 'config')})
        assert parse_yaml_document(path.read_text(), path).unwrap()['meta_harness']['budgets']['max_candidates'] == 12
    finally:
        service.close()


def test_masked_secrets_in_extension_lists_restore_the_correct_entry():
    from loom.service.settings import mask, restore

    value = {'connections': [{'name': 'one', 'api_key': 'first-secret'}, {'name': 'two', 'api_key': 'second-secret'}]}
    assert restore(mask(value), value) == value


def test_provider_settings_save_applies_shared_fields_to_all_models(tmp_path):
    from loom.service.settings import yaml_text

    path = tmp_path / 'config.yaml'
    path.write_text(yaml_text({'default_model': 'main', 'providers': {
        'cloud': {'base_url': 'https://old.example/v1', 'api_key_env': 'OLD_KEY', 'models': {
            'main': {'model': 'large', 'api_key': 'private-value', 'temperature': 0},
            'fast': {'model': 'small', 'temperature': .8},
        }},
    }}))
    service = LoomService(tmp_path / 'data', config_path=path)
    try:
        settings = Settings(service)
        value = settings.get()
        group = value['config']['providers']['cloud']
        assert group['models']['large']['api_key'] == '••••••••'
        group.update(base_url='https://new.example/v1', api_key_env='NEW_KEY')
        settings.save({k: value[k] for k in ('revision', 'config')})
        config = load_task_config(path).unwrap()
        assert config.models['cloud/large'].api_key == 'private-value'
        assert config.models['cloud/large'].temperature == 0
        assert config.models['cloud/small'].temperature == .8
        assert all(m.base_url == 'https://new.example/v1' and m.api_key_env == 'NEW_KEY' for m in config.models.values())
    finally:
        service.close()
