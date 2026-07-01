from loom.tasks.config import ModelConfig, TaskRunnerConfig, create_provider_from_task_config, load_task_config


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
max_tokens = 1234

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
    assert loaded.models["main"].max_tokens == 1234
    assert loaded.models["fast"].api_key == "inline-key"


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
    max_tokens: 8192
""",
        encoding="utf-8",
    )

    loaded = load_task_config(path).unwrap()

    assert loaded.default_model == "main"
    assert loaded.models["main"].model == "qwen-main"
    assert loaded.models["main"].base_url == "https://example.test/v1"
    assert loaded.models["main"].api_key_env == "MAIN_KEY"
    assert loaded.models["main"].temperature == 0
    assert loaded.models["main"].max_tokens == 8192


def test_create_provider_from_task_config_selects_named_model():
    config = TaskRunnerConfig(
        default_model="main",
        models={
            "main": ModelConfig(provider="openai", model="qwen-main", base_url="https://example.test/v1", api_key_env="MAIN_KEY"),
            "fast": ModelConfig(provider="openai", model="qwen-fast", base_url="https://example.test/v1", api_key="inline-key"),
        },
    )

    provider = create_provider_from_task_config(config, model_name="fast").unwrap()

    assert provider.model == "qwen-fast"
    assert provider.base_url == "https://example.test/v1"
    assert provider.api_key == "inline-key"


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
