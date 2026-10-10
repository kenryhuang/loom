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
from loom.llm.request_options import normalize_request_options

_MODEL_CONFIG_FIELDS = frozenset(
    {
        "provider",
        "model",
        "base_url",
        "api_key",
        "api_key_env",
        "temperature",
        "max_completion_tokens",
        "request_options",
    }
)


@dataclass(frozen=True, slots=True)
class ModelConfig:
    provider: str = "openai"
    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    api_key_env: str | None = None
    temperature: float | None = None
    max_completion_tokens: int | None = None
    request_options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_options", normalize_request_options(self.request_options))


@dataclass(frozen=True, slots=True)
class TaskDefaults:
    objective: str | None = None
    workspace: str | None = None
    profile: str | None = None
    expected_outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_outputs", tuple(self.expected_outputs))


@dataclass(frozen=True, slots=True)
class RunDefaults:
    tui: bool | None = None
    stream: bool | None = None
    trace_path: str | None = None
    trace_path_template: str | None = None
    max_steps: int | None = None
    timeout_ms: int | None = None
    plan_mode: str | None = None


@dataclass(frozen=True, slots=True)
class TaskRunnerConfig:
    default_model: str | None = None
    task: TaskDefaults = field(default_factory=TaskDefaults)
    run: RunDefaults = field(default_factory=RunDefaults)
    models: Mapping[str, ModelConfig] = field(default_factory=dict)
    evaluation_model: str | None = None

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
            max_completion_tokens=model.max_completion_tokens,
            request_options=model.request_options,
            http_client=http_client,
        )
    )


def _parse_task_config(payload: Mapping[str, Any], path: Path) -> Result:
    task = _parse_task_defaults(payload.get("task", {}), path)
    if not task.ok:
        return task
    run = _parse_run_defaults(payload.get("run", {}), path)
    if not run.ok:
        return run

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

    evaluation_model = payload.get("evaluation_model")
    if evaluation_model is not None and (not isinstance(evaluation_model, str) or evaluation_model not in models):
        return err(make_loom_error("VALIDATION_FAILED", "evaluation_model must name a configured model", retryable=False))
    return ok(TaskRunnerConfig(default_model=default_model, task=task.value, run=run.value, models=models, evaluation_model=evaluation_model))


def _parse_task_defaults(payload: Any, path: Path) -> Result:
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        return err(make_loom_error("VALIDATION_FAILED", "Task config task must be a table", retryable=False, metadata={"path": str(path)}))
    try:
        return ok(
            TaskDefaults(
                objective=_optional_str(payload.get("objective")),
                workspace=_optional_str(payload.get("workspace")),
                profile=_optional_str(payload.get("profile")),
                expected_outputs=_optional_str_tuple(payload.get("expected_outputs")),
            )
        )
    except (TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Task config task contains invalid values",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(path)},
            )
        )


def _parse_run_defaults(payload: Any, path: Path) -> Result:
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        return err(make_loom_error("VALIDATION_FAILED", "Task config run must be a table", retryable=False, metadata={"path": str(path)}))
    plan_mode = payload.get("plan_mode")
    if plan_mode is not None and (not isinstance(plan_mode, str) or plan_mode not in {"auto", "force", "off"}):
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Task config run.plan_mode must be auto, force, or off",
                retryable=False,
                metadata={"path": str(path), "field": "plan_mode"},
            )
        )
    try:
        return ok(
            RunDefaults(
                tui=_optional_bool(payload.get("tui")),
                stream=_optional_bool(payload.get("stream")),
                trace_path=_optional_str(payload.get("trace_path")),
                trace_path_template=_optional_str(payload.get("trace_path_template")),
                max_steps=_optional_int(payload.get("max_steps")),
                timeout_ms=_optional_int(payload.get("timeout_ms")),
                plan_mode=plan_mode,
            )
        )
    except (TypeError, ValueError) as exc:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Task config run contains invalid values",
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(path)},
            )
        )


def _parse_model_config(payload: Mapping[str, Any], path: Path, name: str) -> Result:
    if "max_tokens" in payload:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Task model config field max_tokens is not supported; use max_completion_tokens",
                retryable=False,
                metadata={"path": str(path), "model": name, "field": "max_tokens"},
            )
        )

    unknown = sorted(set(payload) - _MODEL_CONFIG_FIELDS)
    if unknown:
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Task model config contains unknown fields",
                retryable=False,
                metadata={"path": str(path), "model": name, "fields": unknown},
            )
        )

    request_options = payload.get("request_options", {})
    if not isinstance(request_options, Mapping):
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                "Task model config request_options must be a mapping",
                retryable=False,
                metadata={"path": str(path), "model": name},
            )
        )

    try:
        return ok(
            ModelConfig(
                provider=str(payload.get("provider", "openai")),
                model=str(payload.get("model", "")),
                base_url=str(payload.get("base_url", "")),
                api_key=_optional_str(payload.get("api_key")),
                api_key_env=_optional_str(payload.get("api_key_env")),
                temperature=_optional_float(payload.get("temperature")),
                max_completion_tokens=_optional_int(payload.get("max_completion_tokens")),
                request_options=request_options,
            )
        )
    except (TypeError, ValueError) as exc:
        message = "Task model config contains invalid values"
        if "request_options" in str(exc):
            message = f"Task model request_options is invalid: {exc}"
        return err(
            make_loom_error(
                "VALIDATION_FAILED",
                message,
                retryable=False,
                cause={"name": type(exc).__name__, "message": str(exc)},
                metadata={"path": str(path), "model": name},
            )
        )


@dataclass(frozen=True, slots=True)
class _YamlLine:
    number: int
    indent: int
    text: str


class _YamlParseError(ValueError):
    def __init__(self, line_number: int, message: str):
        super().__init__(message)
        self.line_number = line_number
        self.message = message


def _parse_yaml_config(text: str, path: Path) -> Result:
    tokenized = _tokenize_yaml(text, path)
    if not tokenized.ok:
        return tokenized
    lines = tokenized.value
    if not lines:
        return ok({})
    if lines[0].indent != 0:
        return _yaml_error(path, lines[0].number, "root mapping must start at indentation zero")

    try:
        value, next_index = _parse_yaml_mapping(lines, 0, 0)
    except _YamlParseError as exc:
        return _yaml_error(path, exc.line_number, exc.message)
    if next_index != len(lines):
        line = lines[next_index]
        return _yaml_error(path, line.number, "unexpected indentation")
    return ok(value)


def parse_yaml_document(text: str, path: str | os.PathLike[str]) -> Result:
    """Parse Loom's supported deterministic YAML subset into JSON-like values."""
    return _parse_yaml_config(text, Path(path))


def _tokenize_yaml(text: str, path: Path) -> Result:
    lines: list[_YamlLine] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_yaml_comment(raw_line).rstrip()
        if not line.strip():
            continue
        leading_length = len(line) - len(line.lstrip())
        leading = line[:leading_length]
        if "\t" in leading:
            return _yaml_error(path, line_number, "indentation must use spaces")
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2 != 0:
            return _yaml_error(path, line_number, "indentation must use two-space levels")
        lines.append(_YamlLine(line_number, indent, line[indent:]))
    return ok(lines)


def _parse_yaml_mapping(lines: list[_YamlLine], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise _YamlParseError(line.number, "unexpected indentation")
        if line.text == "-" or line.text.startswith("- "):
            raise _YamlParseError(line.number, "expected key: value")

        key, raw_value = _split_yaml_mapping_line(line.text, line.number)
        if key in result:
            raise _YamlParseError(line.number, f"duplicate mapping key: {key}")
        index += 1
        if raw_value is not None:
            result[key] = _parse_yaml_scalar(raw_value)
            continue
        if index < len(lines) and lines[index].indent > indent:
            nested = lines[index]
            if nested.indent != indent + 2:
                raise _YamlParseError(nested.number, "nested values must use one two-space indentation level")
            if nested.text == "-" or nested.text.startswith("- "):
                result[key], index = _parse_yaml_sequence(lines, index, indent + 2)
            else:
                result[key], index = _parse_yaml_mapping(lines, index, indent + 2)
            continue
        result[key] = None
    return result, index


def _parse_yaml_sequence(lines: list[_YamlLine], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise _YamlParseError(line.number, "unexpected indentation")
        if line.text != "-" and not line.text.startswith("- "):
            break

        raw_value = line.text[1:].strip()
        index += 1
        if raw_value:
            if _looks_like_yaml_mapping_entry(raw_value):
                value, index = _parse_yaml_sequence_mapping(lines, index, indent, line.number, raw_value)
                result.append(value)
                continue
            result.append(_parse_yaml_scalar(raw_value))
            continue
        if index < len(lines) and lines[index].indent > indent:
            nested = lines[index]
            if nested.indent != indent + 2:
                raise _YamlParseError(nested.number, "nested values must use one two-space indentation level")
            if nested.text == "-" or nested.text.startswith("- "):
                value, index = _parse_yaml_sequence(lines, index, indent + 2)
            else:
                value, index = _parse_yaml_mapping(lines, index, indent + 2)
            result.append(value)
            continue
        result.append(None)
    return result, index


def _parse_yaml_sequence_mapping(
    lines: list[_YamlLine],
    index: int,
    sequence_indent: int,
    first_line_number: int,
    first_entry: str,
) -> tuple[dict[str, Any], int]:
    mapping_indent = sequence_indent + 2
    result: dict[str, Any] = {}
    key, raw_value = _split_yaml_mapping_line(first_entry, first_line_number)
    result[key], index = _parse_yaml_mapping_value(lines, index, mapping_indent, raw_value)

    while index < len(lines):
        line = lines[index]
        if line.indent < mapping_indent:
            break
        if line.indent > mapping_indent:
            raise _YamlParseError(line.number, "unexpected indentation")
        if line.text == "-" or line.text.startswith("- "):
            break

        key, raw_value = _split_yaml_mapping_line(line.text, line.number)
        if key in result:
            raise _YamlParseError(line.number, f"duplicate mapping key: {key}")
        index += 1
        result[key], index = _parse_yaml_mapping_value(lines, index, mapping_indent, raw_value)
    return result, index


def _parse_yaml_mapping_value(lines: list[_YamlLine], index: int, parent_indent: int, raw_value: str | None) -> tuple[Any, int]:
    if raw_value is not None:
        return _parse_yaml_scalar(raw_value), index
    if index >= len(lines) or lines[index].indent <= parent_indent:
        return None, index

    nested = lines[index]
    if nested.indent != parent_indent + 2:
        raise _YamlParseError(nested.number, "nested values must use one two-space indentation level")
    if nested.text == "-" or nested.text.startswith("- "):
        return _parse_yaml_sequence(lines, index, parent_indent + 2)
    return _parse_yaml_mapping(lines, index, parent_indent + 2)


def _looks_like_yaml_mapping_entry(value: str) -> bool:
    if value.startswith(("'", '"')):
        return False
    colon = value.find(":")
    return colon > 0 and (colon == len(value) - 1 or value[colon + 1].isspace())


def _split_yaml_mapping_line(line: str, line_number: int) -> tuple[str, str | None]:
    if ":" not in line:
        raise _YamlParseError(line_number, "expected key: value")
    key, value = line.split(":", 1)
    key = key.strip()
    if not key:
        raise _YamlParseError(line_number, "mapping key is required")
    value = value.strip()
    return key, None if value == "" else value


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
    if value.startswith("[") and value.endswith("]"):
        return _parse_yaml_inline_list(value)
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


def _parse_yaml_inline_list(value: str) -> list[Any]:
    inner = value[1:-1].strip()
    if not inner:
        return []
    return [_parse_yaml_scalar(item.strip()) for item in inner.split(",")]


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


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise TypeError("value is not a boolean")


def _optional_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise TypeError("expected a list of strings")
    if isinstance(value, tuple | list):
        return tuple(str(item) for item in value)
    raise TypeError("expected a list of strings")


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
    "RunDefaults",
    "TaskDefaults",
    "TaskRunnerConfig",
    "create_provider_from_task_config",
    "load_task_config",
    "parse_yaml_document",
]
