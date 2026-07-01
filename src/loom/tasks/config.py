"""Configuration loading for generic Loom task runs."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loom.core import Result, err, make_loom_error, ok
from loom.llm import create_openai_provider


@dataclass(frozen=True, slots=True)
class ModelConfig:
    provider: str = "openai"
    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    api_key_env: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TaskRunnerConfig:
    default_model: str | None = None
    models: Mapping[str, ModelConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", dict(self.models))


def load_task_config(path: str | os.PathLike[str]) -> Result:
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Could not read task config",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(config_path)},
            )
        )

    if config_path.suffix.lower() in {".yaml", ".yml"}:
        parsed_yaml = _parse_yaml_config(text, config_path)
        if not parsed_yaml.ok:
            return parsed_yaml
        payload = parsed_yaml.value
    else:
        try:
            payload = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            return err(
                make_loom_error(
                    "VALIDATION_FAILED",
                    "Task config TOML is malformed",
                    retryable=False,
                    cause={"message": str(exc)},
                    metadata={"path": str(config_path)},
                )
            )

    return _parse_task_config(payload, config_path)


def create_provider_from_task_config(
    config: TaskRunnerConfig,
    *,
    model_name: str | None = None,
    env: Mapping[str, str] | None = None,
    http_client: Any = None,
) -> Result:
    selected_name = model_name or config.default_model
    if not selected_name:
        return err(make_loom_error("VALIDATION_FAILED", "No model selected and config has no default_model", retryable=False))

    model = config.models.get(selected_name)
    if model is None:
        return err(make_loom_error("VALIDATION_FAILED", "Task model is not configured", retryable=False, metadata={"model": selected_name}))
    if model.provider != "openai":
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Unsupported task model provider",
                retryable=False,
                metadata={"model": selected_name, "provider": model.provider},
            )
        )

    api_key = model.api_key or _env_value(model.api_key_env, env)
    if not api_key:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Task model API key is missing",
                retryable=False,
                metadata={"model": selected_name, "api_key_env": model.api_key_env or ""},
            )
        )
    if not model.model:
        return err(make_loom_error("VALIDATION_FAILED", "Task model name is missing", retryable=False, metadata={"model": selected_name}))
    if not model.base_url:
        return err(make_loom_error("VALIDATION_FAILED", "Task model base_url is missing", retryable=False, metadata={"model": selected_name}))

    return ok(
        create_openai_provider(
            api_key=api_key,
            model=model.model,
            base_url=model.base_url,
            temperature=model.temperature,
            max_tokens=model.max_tokens,
            http_client=http_client,
        )
    )


def _parse_task_config(payload: Mapping[str, Any], path: Path) -> Result:
    models_payload = payload.get("models", {})
    if not isinstance(models_payload, Mapping):
        return err(make_loom_error("VALIDATION_FAILED", "Task config models must be a table", retryable=False, metadata={"path": str(path)}))

    models: dict[str, ModelConfig] = {}
    for name, value in models_payload.items():
        if not isinstance(value, Mapping):
            return err(
                make_loom_error(
                    "VALIDATION_FAILED",
                    "Task model config must be a table",
                    retryable=False,
                    metadata={"path": str(path), "model": str(name)},
                )
            )
        parsed = _parse_model_config(value, path, str(name))
        if not parsed.ok:
            return parsed
        models[str(name)] = parsed.value

    default_model = payload.get("default_model")
    if default_model is not None and not isinstance(default_model, str):
        return err(make_loom_error("VALIDATION_FAILED", "default_model must be a string", retryable=False, metadata={"path": str(path)}))

    return ok(TaskRunnerConfig(default_model=default_model, models=models))


def _parse_model_config(payload: Mapping[str, Any], path: Path, name: str) -> Result:
    try:
        return ok(
            ModelConfig(
                provider=str(payload.get("provider", "openai")),
                model=str(payload.get("model", "")),
                base_url=str(payload.get("base_url", "")),
                api_key=_optional_str(payload.get("api_key")),
                api_key_env=_optional_str(payload.get("api_key_env")),
                temperature=_optional_float(payload.get("temperature")),
                max_tokens=_optional_int(payload.get("max_tokens")),
            )
        )
    except (TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Task model config contains invalid values",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(path), "model": name},
            )
        )


def _parse_yaml_config(text: str, path: Path) -> Result:
    payload: dict[str, Any] = {}
    models: dict[str, dict[str, Any]] = {}
    in_models = False
    current_model: str | None = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_yaml_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2 != 0:
            return _yaml_error(path, line_number, "indentation must use two-space levels")

        parsed = _split_yaml_mapping_line(line.strip(), path, line_number)
        if not parsed.ok:
            return parsed
        key, raw_value = parsed.value

        if indent == 0:
            if key == "models":
                if raw_value is not None:
                    return _yaml_error(path, line_number, "models must be a mapping")
                in_models = True
                current_model = None
                payload["models"] = models
                continue
            payload[key] = _parse_yaml_scalar(raw_value)
            in_models = False
            current_model = None
            continue

        if indent == 2 and in_models:
            if raw_value is not None:
                return _yaml_error(path, line_number, "model entries must be mappings")
            current_model = key
            models[current_model] = {}
            continue

        if indent == 4 and in_models and current_model is not None:
            models[current_model][key] = _parse_yaml_scalar(raw_value)
            continue

        return _yaml_error(path, line_number, "unsupported YAML shape")

    return ok(payload)


def _split_yaml_mapping_line(line: str, path: Path, line_number: int) -> Result:
    if ":" not in line:
        return _yaml_error(path, line_number, "expected key: value")
    key, value = line.split(":", 1)
    key = key.strip()
    if not key:
        return _yaml_error(path, line_number, "mapping key is required")
    value = value.strip()
    return ok((key, None if value == "" else value))


def _strip_yaml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            continue
        if char == "#" and quote is None:
            return line[:index]
    return line


def _parse_yaml_scalar(value: str | None) -> Any:
    if value is None:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"null", "none", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _looks_like_int(value):
        return int(value)
    if _looks_like_float(value):
        return float(value)
    return value


def _looks_like_int(value: str) -> bool:
    digits = value[1:] if value.startswith(("+", "-")) else value
    return bool(digits) and digits.isdigit()


def _looks_like_float(value: str) -> bool:
    if "." not in value:
        return False
    try:
        float(value)
    except ValueError:
        return False
    return True


def _yaml_error(path: Path, line_number: int, message: str) -> Result:
    return err(
        make_loom_error(
            "VALIDATION_FAILED",
            "Task config YAML is malformed",
            retryable=False,
            cause={"line": line_number, "message": message},
            metadata={"path": str(path)},
        )
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("boolean is not an integer")
    return int(value)


def _env_value(name: str | None, env: Mapping[str, str] | None) -> str | None:
    if not name:
        return None
    if env is not None:
        return env.get(name)
    return os.environ.get(name) or _read_dotenv(Path(os.environ.get("LOOM_ENV_FILE", ".env"))).get(name)


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("'\"")
        if name:
            values[name] = value
    return values


__all__ = [
    "ModelConfig",
    "TaskRunnerConfig",
    "create_provider_from_task_config",
    "load_task_config",
]
