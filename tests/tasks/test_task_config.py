from loom.tasks.config import ModelConfig, TaskRunnerConfig, create_provider_from_task_config, load_task_config


def test_provider_models_share_connection_but_keep_model_options(tmp_path):
    from loom.service.settings import yaml_text

    path = tmp_path / "config.yaml"
    path.write_text(yaml_text({
        "default_model": "dashscope/large", "evaluation_model": "dashscope/small", "verification_model": "dashscope/large",
        "providers": {
            "dashscope": {"base_url": "https://example.test/v1", "api_key_env": "SHARED_KEY", "models": {
                "large": { "temperature": 0, "context_window_tokens": 128000,
                         "max_completion_tokens": 8192, "request_options": {"enable_thinking": True}},
                "small": { "temperature": .5, "request_options": {"enable_thinking": False}},
            }},
            "local": {"base_url": "http://localhost:8080/v1", "api_key_env": "LOCAL_KEY", "models": {
                "large": {},
            }},
        },
    }))
    config = load_task_config(path).unwrap()
    main = create_provider_from_task_config(config, env={"SHARED_KEY": "test-key"}).unwrap()
    fast = create_provider_from_task_config(config, model_name="dashscope/small", env={"SHARED_KEY": "test-key"}).unwrap()
    assert main.base_url == fast.base_url == "https://example.test/v1"
    assert main.api_key == fast.api_key == "test-key"
    assert main.temperature == 0 and fast.temperature == .5
    assert main.request_options["enable_thinking"] is True
    assert fast.request_options["enable_thinking"] is False
    assert main.context_window_tokens == 128000
    assert main.max_completion_tokens == 8192
    assert config.models["local/large"].api_key_env == "LOCAL_KEY"


def test_provider_structure_errors_are_actionable(tmp_path):
    from loom.service.settings import yaml_text

    cases = [
        ({"providers": {}, "models": {}}, "not both"),
        ({"providers": []}, "providers must be a table"),
        ({"providers": {"one": {"models": []}}}, "providers.one.models"),
        ({"providers": {"one": {"base_url": 42, "models": {}}}}, "providers.one.base_url"),
        ({"providers": {"one": {"temperature": 0, "models": {}}}}, "model-specific fields"),
        ({"providers": {"one": {"models": {"main": {"base_url": "other"}}}}}, "belong on the provider"),
        ({"providers": {"one": {"models": {"main": {"model": 3}}}}}, "model ID must be a nonempty string"),
    ]
    path = tmp_path / "bad.yaml"
    for document, message in cases:
        path.write_text(yaml_text(document))
        result = load_task_config(path)
        assert not result.ok
        assert message in result.error.message


def test_provider_hierarchy_supports_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('default_model = "main"\n[providers.local]\nbase_url = "http://localhost/v1"\n'
                    'api_key_env = "LOCAL_KEY"\n[providers.local.models.main]\nmodel = "test"\n')
    assert load_task_config(path).unwrap().models["local/test"].base_url == "http://localhost/v1"


def test_load_task_config_reads_multiple_named_models(tmp_path):
    path = tmp_path / "loom-task.toml"
    path.write_text(
        """
default_model = "main"

[models.main]
provider = "openai"
model = "qwen-main"
base_url = "https://example.test/v1"
api_key_env = "MAIN_KEY"
temperature = 0.2
max_completion_tokens = 1234
context_window_tokens = 128000

[models.fast]
provider = "openai"
model = "qwen-fast"
base_url = "https://example.test/v1"
api_key = "inline-key"
""",
        encoding="utf-8",
    )

    loaded = load_task_config(path).unwrap()

    assert loaded.default_model == "main"
    assert loaded.models["main"].model == "qwen-main"
    assert loaded.models["main"].api_key_env == "MAIN_KEY"
    assert loaded.models["main"].temperature == 0.2
    assert loaded.models["main"].max_completion_tokens == 1234
    assert loaded.models["main"].context_window_tokens == 128000
    assert loaded.models["fast"].api_key == "inline-key"


def test_model_context_capacity_is_local_metadata_and_validated(tmp_path):
    from loom.llm import LlmMessage

    config = TaskRunnerConfig(default_model="main", models={"main": ModelConfig(
        model="custom", base_url="https://example.test/v1", api_key="key", context_window_tokens=128000,
    )})
    provider = create_provider_from_task_config(config).unwrap()
    assert provider.context_window_tokens == 128000
    assert "context_window_tokens" not in provider._request_body([LlmMessage("user", "hello")], None, None, stream=False)
    for value in [0, -1, 1023, True, "128000", 128000.5]:
        path = tmp_path / "bad.yaml"
        import json
        path.write_text('models:\n  main:\n    context_window_tokens: ' + json.dumps(value) + '\n')
        assert not load_task_config(path).ok


def test_load_task_config_reads_plan_mode_from_toml_and_yaml(tmp_path):
    toml_path = tmp_path / "config.toml"
    toml_path.write_text('[run]\nplan_mode = "force"\n', encoding="utf-8")
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("run:\n  plan_mode: force\n", encoding="utf-8")

    assert load_task_config(toml_path).unwrap().run.plan_mode == "force"
    assert load_task_config(yaml_path).unwrap().run.plan_mode == "force"


def test_load_task_config_rejects_invalid_plan_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[run]\nplan_mode = "sometimes"\n', encoding="utf-8")

    result = load_task_config(path)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert result.error.metadata["field"] == "plan_mode"


def test_load_task_config_reads_yaml_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
default_model: main
models:
  main:
    provider: openai
    model: qwen-main
    base_url: https://example.test/v1
    api_key_env: MAIN_KEY
    temperature: 0
    max_completion_tokens: 8192
""",
        encoding="utf-8",
    )

    loaded = load_task_config(path).unwrap()

    assert loaded.default_model == "main"
    assert loaded.models["main"].model == "qwen-main"
    assert loaded.models["main"].base_url == "https://example.test/v1"
    assert loaded.models["main"].api_key_env == "MAIN_KEY"
    assert loaded.models["main"].temperature == 0
    assert loaded.models["main"].max_completion_tokens == 8192


def test_load_task_config_reads_nested_bailian_options_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
default_model: main
models:
  main:
    provider: openai
    model: qwen3.7-max
    base_url: https://example.test/v1
    api_key: key
    max_completion_tokens: 8192
    request_options:
      enable_thinking: true
      thinking_budget: 2048
      response_format:
        type: json_object
      stop:
        - END
        - STOP
""",
        encoding="utf-8",
    )

    model = load_task_config(path).unwrap().models["main"]

    assert model.max_completion_tokens == 8192
    assert model.request_options["enable_thinking"] is True
    assert model.request_options["thinking_budget"] == 2048
    assert model.request_options["response_format"]["type"] == "json_object"
    assert tuple(model.request_options["stop"]) == ("END", "STOP")


def test_load_task_config_reads_yaml_lists_of_nested_mappings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
models:
  main:
    model: qwen3.7-max
    request_options:
      items:
        - name: first
          enabled: true
          details:
            rank: 1
        - name: second
          enabled: false
""",
        encoding="utf-8",
    )

    items = load_task_config(path).unwrap().models["main"].request_options["items"]

    assert items[0]["name"] == "first"
    assert items[0]["enabled"] is True
    assert items[0]["details"]["rank"] == 1
    assert items[1] == {"name": "second", "enabled": False}


def test_load_task_config_reads_request_options_from_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
default_model = "glm"

[models.glm]
provider = "openai"
model = "glm-5.2"
base_url = "https://example.test/v1"
api_key = "key"
max_completion_tokens = 4096

[models.glm.request_options]
enable_thinking = true
reasoning_effort = "max"

[models.glm.request_options.search_options]
forced_search = false
""",
        encoding="utf-8",
    )

    model = load_task_config(path).unwrap().models["glm"]

    assert model.max_completion_tokens == 4096
    assert model.request_options["enable_thinking"] is True
    assert model.request_options["reasoning_effort"] == "max"
    assert model.request_options["search_options"]["forced_search"] is False


def test_load_task_config_rejects_legacy_max_tokens(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
models:
  main:
    model: qwen3.7-max
    max_tokens: 8192
""",
        encoding="utf-8",
    )

    result = load_task_config(path)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert "max_completion_tokens" in result.error.message


def test_load_task_config_rejects_unknown_model_fields(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[models.main]\nmodel = "qwen"\ntemprature = 0.2\n', encoding="utf-8")

    result = load_task_config(path)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert result.error.metadata["fields"] == ("temprature",)


def test_load_task_config_rejects_non_mapping_request_options(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
models:
  main:
    model: qwen
    request_options: invalid
""",
        encoding="utf-8",
    )

    result = load_task_config(path)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert "mapping" in result.error.message


def test_load_task_config_rejects_null_request_options(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
models:
  main:
    model: qwen
    request_options: null
""",
        encoding="utf-8",
    )

    result = load_task_config(path)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert "mapping" in result.error.message


def test_load_task_config_rejects_reserved_request_option(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[models.main]\nmodel = "qwen"\n[models.main.request_options]\nstream = false\n', encoding="utf-8")

    result = load_task_config(path)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert "reserved" in result.error.message


def test_load_task_config_rejects_non_json_request_option_with_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[models.main]\nmodel = "qwen"\n[models.main.request_options]\ntop_p = nan\n', encoding="utf-8")

    result = load_task_config(path)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert "request_options.top_p" in result.error.message


def test_load_task_config_reads_task_and_run_defaults_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
default_model: main
task:
  objective: Audit this project briefly
  workspace: .
  profile: project_audit
  expected_outputs:
    - Brief markdown audit report.
run:
  tui: false
  stream: true
  trace_path_template: runs/{task_slug}-{timestamp}.jsonl
  max_steps: 3
  timeout_ms: 5000
models:
  main:
    provider: openai
    model: qwen-main
    base_url: https://example.test/v1
    api_key_env: MAIN_KEY
""",
        encoding="utf-8",
    )

    loaded = load_task_config(path).unwrap()

    assert loaded.task.objective == "Audit this project briefly"
    assert loaded.task.workspace == "."
    assert loaded.task.profile == "project_audit"
    assert loaded.task.expected_outputs == ("Brief markdown audit report.",)
    assert loaded.run.tui is False
    assert loaded.run.stream is True
    assert loaded.run.trace_path_template == "runs/{task_slug}-{timestamp}.jsonl"
    assert loaded.run.max_steps == 3
    assert loaded.run.timeout_ms == 5000


def test_create_provider_from_task_config_selects_named_model():
    config = TaskRunnerConfig(
        default_model="main",
        models={
            "main": ModelConfig(provider="openai", model="qwen-main", base_url="https://example.test/v1", api_key_env="MAIN_KEY"),
            "fast": ModelConfig(
                provider="openai",
                model="qwen-fast",
                base_url="https://example.test/v1",
                api_key="inline-key",
                max_completion_tokens=4096,
                request_options={"enable_thinking": True},
            ),
        },
    )

    provider = create_provider_from_task_config(config, model_name="fast").unwrap()

    assert provider.model == "qwen-fast"
    assert provider.base_url == "https://example.test/v1"
    assert provider.api_key == "inline-key"
    assert provider.max_completion_tokens == 4096
    assert provider.request_options["enable_thinking"] is True


def test_create_provider_from_task_config_uses_api_key_env():
    config = TaskRunnerConfig(
        default_model="main",
        models={
            "main": ModelConfig(provider="openai", model="qwen-main", base_url="https://example.test/v1", api_key_env="MAIN_KEY"),
        },
    )

    provider = create_provider_from_task_config(config, env={"MAIN_KEY": "env-key"}).unwrap()

    assert provider.model == "qwen-main"
    assert provider.api_key == "env-key"


def test_create_provider_from_task_config_reads_api_key_env_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MAIN_KEY", raising=False)
    (tmp_path / ".env").write_text("MAIN_KEY=dotenv-key\n", encoding="utf-8")
    config = TaskRunnerConfig(
        default_model="main",
        models={
            "main": ModelConfig(provider="openai", model="qwen-main", base_url="https://example.test/v1", api_key_env="MAIN_KEY"),
        },
    )

    provider = create_provider_from_task_config(config).unwrap()

    assert provider.api_key == "dotenv-key"


def test_create_provider_from_task_config_rejects_missing_model_name():
    config = TaskRunnerConfig(default_model="main", models={})

    result = create_provider_from_task_config(config, model_name="missing")

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"


def test_evaluation_model_is_independent_and_must_exist(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('default_model: main\nevaluation_model: judge\nmodels:\n  main:\n    model: solver\n  judge:\n    model: evaluator\n')
    config = load_task_config(path).unwrap()
    assert config.default_model == 'main'
    assert config.evaluation_model == 'judge'
    path.write_text('evaluation_model: missing\n')
    assert not load_task_config(path).ok


def test_model_id_keys_allow_namespaces_and_colons(tmp_path):
    from loom.service.settings import yaml_text
    path = tmp_path / 'config.yaml'
    path.write_text(yaml_text({'default_model': 'local/vendor/model:v1', 'providers': {
        'local': {'models': {'vendor/model:v1': {'temperature': .2}}},
    }}))
    config = load_task_config(path).unwrap()
    assert list(config.models) == ['local/vendor/model:v1']
    assert config.models['local/vendor/model:v1'].model == 'vendor/model:v1'


def test_legacy_profiles_migrate_without_losing_options_or_old_references(tmp_path):
    from loom.service.settings import yaml_text
    from loom.tasks.config import provider_config_document
    old = {'default_model': 'solver', 'evaluation_model': 'judge', 'meta_harness': {'solver_model': 'solver'},
           'providers': {'cloud': {'base_url': 'https://example.test/v1', 'api_key_env': 'KEY', 'models': {
               'solver': {'model': 'shared-v2', 'max_completion_tokens': 8192, 'request_options': {'enable_thinking': True}},
               'judge': {'model': 'shared-v2', 'max_completion_tokens': 2048, 'request_options': {'enable_thinking': False}},
           }}}}
    document = provider_config_document(old)
    assert provider_config_document(document) == document
    assert document['default_model'] == 'cloud/shared-v2'
    assert document['evaluation_model'] == 'cloud_judge/shared-v2'
    assert document['meta_harness']['solver_model'] == 'cloud/shared-v2'
    assert all('model' not in m for p in document['providers'].values() for m in p['models'].values())
    path = tmp_path / 'config.yaml'
    path.write_text(yaml_text(document))
    config = load_task_config(path).unwrap()
    assert set(config.models) == {'cloud/shared-v2', 'cloud_judge/shared-v2'}
    provider = create_provider_from_task_config(config, model_name='judge', env={'KEY': 'test'}).unwrap()
    assert provider.model == 'shared-v2'
    assert provider.max_completion_tokens == 2048
    assert provider.request_options['enable_thinking'] is False
    assert config.resolve_model_name('solver') == 'cloud/shared-v2'
