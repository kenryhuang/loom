# Bailian Request Options Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace provider output `max_tokens` with `max_completion_tokens` and add safe, future-compatible Bailian request parameter passthrough.

**Architecture:** A focused `loom.llm.request_options` module owns recursive JSON validation, immutable copying, reserved-key enforcement, and request materialization. `OpenAIProvider` uses one body builder for streaming and non-streaming calls. Task configuration exposes `max_completion_tokens` plus nested `request_options`, backed by a recursive dependency-free YAML subset parser.

**Tech Stack:** Python 3.11 standard library and dataclasses, `tomllib`, Loom `Result`, pytest, Ruff.

## Global Constraints

- Provider output configuration must not accept or send `max_tokens`; use `max_completion_tokens` without an alias.
- Goal, run, token-accounting, and tool-selection budget fields named `max_tokens` remain unchanged.
- `request_options` accepts current and future JSON-compatible Bailian request parameters without an allowlist.
- `model`, `messages`, `stream`, `tools`, `tool_choice`, `temperature`, `max_completion_tokens`, and `max_tokens` are reserved.
- Do not add a model/parameter compatibility matrix, automatic capability defaults, or a YAML dependency.

---

### Task 1: Request option values and provider body construction

**Files:**
- Create: `src/loom/llm/request_options.py`
- Modify: `src/loom/llm/api.py:716-840`
- Modify: `src/loom/llm/api.py:880-915`
- Test: `tests/llm/test_llm.py`

**Interfaces:**
- Produces: `normalize_request_options(options: Mapping[str, Any] | None) -> Mapping[str, FrozenJsonValue]`.
- Produces: `materialize_request_options(options: Mapping[str, FrozenJsonValue]) -> dict[str, JsonValue]`.
- Produces: `OpenAIProvider.max_completion_tokens` and `OpenAIProvider.request_options`.

- [ ] **Step 1: Write failing request-body tests**

Write tests that exercise actual captured HTTP request bodies:

```python
def test_openai_provider_sends_completion_limit_and_request_options():
    async def scenario():
        calls = []

        async def http_client(url, request):
            calls.append((url, request))
            return {
                "status": 200,
                "ok": True,
                "json": {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}], "usage": {}},
            }

        options = {
            "enable_thinking": True,
            "reasoning_effort": "max",
            "search_options": {"forced_search": False},
            "stop": ["END"],
        }
        provider = create_openai_provider(
            api_key="key",
            model="glm-5.2",
            max_completion_tokens=4096,
            request_options=options,
            http_client=http_client,
        )
        options["search_options"]["forced_search"] = True
        options["stop"].append("MUTATED")

        assert (await provider.chat([{"role": "user", "content": "hello"}])).ok
        body = calls[0][1]["body"]
        assert body["max_completion_tokens"] == 4096
        assert "max_tokens" not in body
        assert body["enable_thinking"] is True
        assert body["reasoning_effort"] == "max"
        assert body["search_options"] == {"forced_search": False}
        assert body["stop"] == ["END"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reserved",
    ["model", "messages", "stream", "tools", "tool_choice", "temperature", "max_completion_tokens", "max_tokens"],
)
def test_openai_provider_rejects_reserved_request_options(reserved):
    with pytest.raises(ValueError, match="reserved"):
        create_openai_provider(api_key="key", model="test", request_options={reserved: "bad"})
```

Write the streaming test as:

```python
def test_openai_provider_stream_sends_completion_limit_and_request_options():
    async def scenario():
        calls = []

        async def http_client(url, request):
            calls.append((url, request))
            return {
                "status": 200,
                "ok": True,
                "chunks": ['data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n', "data: [DONE]\n\n"],
            }

        provider = create_openai_provider(
            api_key="key",
            model="qwen3.7-max",
            max_completion_tokens=8192,
            request_options={"enable_thinking": True, "thinking_budget": 2048, "tool_stream": True},
            http_client=http_client,
        )

        [event async for event in provider.stream_chat([LlmMessage("user", "hello")])]
        body = calls[0][1]["body"]
        assert body["stream"] is True
        assert body["max_completion_tokens"] == 8192
        assert body["enable_thinking"] is True
        assert body["thinking_budget"] == 2048
        assert body["tool_stream"] is True
        assert "max_tokens" not in body

    asyncio.run(scenario())
```

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/llm/test_llm.py -k "completion_limit or request_options" -q`.

Expected: failures because the provider does not accept either new field.

- [ ] **Step 3: Implement immutable JSON request options**

Create `src/loom/llm/request_options.py`:

```python
from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FrozenJsonValue: TypeAlias = JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]

RESERVED_REQUEST_OPTIONS = frozenset(
    {"model", "messages", "stream", "tools", "tool_choice", "temperature", "max_completion_tokens", "max_tokens"}
)


def normalize_request_options(options: Mapping[str, Any] | None) -> Mapping[str, FrozenJsonValue]:
    if options is None:
        return MappingProxyType({})
    if not isinstance(options, Mapping):
        raise TypeError("request_options must be a mapping")
    conflicts = RESERVED_REQUEST_OPTIONS.intersection(options)
    if conflicts:
        raise ValueError(f"request_options contains reserved fields: {', '.join(sorted(conflicts))}")
    frozen = {}
    for key, value in options.items():
        if not isinstance(key, str):
            raise TypeError("request_options keys must be strings")
        frozen[key] = _freeze_json(value, f"request_options.{key}")
    return MappingProxyType(frozen)


def materialize_request_options(options: Mapping[str, FrozenJsonValue]) -> dict[str, JsonValue]:
    return {key: _thaw_json(value) for key, value in options.items()}
```

Implement `_freeze_json` recursively: accept `None`, booleans, integers,
strings, finite floats, lists, and string-keyed mappings; convert lists to
tuples and mappings to `MappingProxyType`; reject every other value with its
full path. Implement `_thaw_json` to recursively return mutable dictionaries
and lists suitable for JSON serialization.

- [ ] **Step 4: Unify provider body construction**

Change `OpenAIProvider` to:

```python
@dataclass(frozen=True, slots=True)
class OpenAIProvider:
    api_key: str
    model: str
    temperature: float | None = None
    max_completion_tokens: int | None = None
    request_options: Mapping[str, Any] = field(default_factory=dict)
    base_url: str = "https://api.openai.com/v1"
    http_client: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_options", normalize_request_options(self.request_options))

    def _request_body(self, messages, tools, tool_choice, *, stream: bool) -> dict[str, Any]:
        body = {"model": self.model, "messages": [_to_openai_message(message) for message in messages]}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_completion_tokens is not None:
            body["max_completion_tokens"] = self.max_completion_tokens
        body.update(materialize_request_options(self.request_options))
        if stream:
            body["stream"] = True
        if tools:
            body["tools"] = tools
        if tools and tool_choice is not None:
            body["tool_choice"] = tool_choice
        return body
```

Use the helper from both `chat` and `stream_chat`. Rename the output argument
and add `request_options` in `create_env_openai_provider`. Update existing
provider test fixtures from `max_tokens=256` to
`max_completion_tokens=256`.

- [ ] **Step 5: Verify GREEN and commit**

Run `uv run pytest tests/llm/test_llm.py -q` and expect all tests to pass.

Commit:

```bash
git add src/loom/llm/request_options.py src/loom/llm/api.py tests/llm/test_llm.py
git commit -m "feat: support OpenAI request options"
```

### Task 2: Strict model configuration and nested YAML

**Files:**
- Modify: `src/loom/tasks/config.py:13-28`
- Modify: `src/loom/tasks/config.py:126-149`
- Modify: `src/loom/tasks/config.py:230-390`
- Test: `tests/tasks/test_task_config.py`

**Interfaces:**
- Consumes: Task 1 request option normalizer and provider fields.
- Produces: `ModelConfig.max_completion_tokens`, immutable `request_options`, strict known fields, and recursive YAML mappings/lists.

- [ ] **Step 1: Write failing config tests**

Write YAML coverage:

```python
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
    assert model.request_options["response_format"]["type"] == "json_object"
    assert tuple(model.request_options["stop"]) == ("END", "STOP")
```

Write TOML and rejection coverage:

```python
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
    assert model.request_options["reasoning_effort"] == "max"
    assert model.request_options["search_options"]["forced_search"] is False


@pytest.mark.parametrize(
    ("model_lines", "message"),
    [
        ("max_tokens: 8192", "max_completion_tokens"),
        ("temprature: 0.2", "unknown"),
        ("request_options: invalid", "mapping"),
    ],
)
def test_load_task_config_rejects_invalid_model_fields(tmp_path, model_lines, message):
    path = tmp_path / "config.yaml"
    path.write_text(f"models:\n  main:\n    model: qwen\n    {model_lines}\n", encoding="utf-8")

    result = load_task_config(path)

    assert not result.ok
    assert result.error.code == "VALIDATION_FAILED"
    assert message in result.error.message.lower()


def test_load_task_config_rejects_reserved_request_option(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[models.main]\nmodel = "qwen"\n[models.main.request_options]\nstream = false\n', encoding="utf-8")

    result = load_task_config(path)

    assert not result.ok
    assert "reserved" in result.error.message.lower()
```

In `test_create_provider_from_task_config_selects_named_model`, construct the
selected `ModelConfig` with `max_completion_tokens=4096` and
`request_options={"enable_thinking": True}`, then assert the resulting
provider exposes both values.

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/tasks/test_task_config.py -q`.

Expected: new parsing, validation, and propagation tests fail.

- [ ] **Step 3: Implement a recursive dependency-free YAML subset parser**

Replace the stateful shape-specific loop with tokenization plus recursive
mapping and sequence functions:

```python
@dataclass(frozen=True, slots=True)
class _YamlLine:
    number: int
    indent: int
    text: str


def _parse_yaml_config(text: str, path: Path) -> Result:
    tokenized = _tokenize_yaml(text, path)
    if not tokenized.ok:
        return tokenized
    try:
        value, next_index = _parse_yaml_mapping(tokenized.value, 0, 0)
    except _YamlParseError as exc:
        return _yaml_error(path, exc.line_number, exc.message)
    if next_index != len(tokenized.value):
        line = tokenized.value[next_index]
        return _yaml_error(path, line.number, "unexpected indentation")
    return ok(value)
```

`_tokenize_yaml` strips comments and blank lines, rejects tabs and odd
indentation, and records line numbers. `_parse_yaml_mapping` accepts
`key: scalar`, `key:` as `None`, or a nested collection at exactly `indent+2`.
`_parse_yaml_sequence` accepts scalar and nested collection items. Both return
`(value, next_index)`, reject duplicate keys and indentation jumps, and retain
line-aware errors. Preserve `_parse_yaml_scalar` and inline scalar lists. This
must continue parsing existing task/run/models shapes while allowing arbitrary
nested mappings and scalar lists in `request_options`.

- [ ] **Step 4: Implement strict `ModelConfig`**

Use:

```python
_MODEL_CONFIG_FIELDS = frozenset(
    {"provider", "model", "base_url", "api_key", "api_key_env", "temperature", "max_completion_tokens", "request_options"}
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
```

Before construction, return a dedicated `VALIDATION_FAILED` result for
`max_tokens`, report sorted unknown fields, and require `request_options` to be
a mapping. Pass the new fields through `create_provider_from_task_config`.

- [ ] **Step 5: Verify GREEN and commit**

Run `uv run pytest tests/tasks/test_task_config.py tests/llm/test_llm.py -q` and
expect all tests to pass.

Commit:

```bash
git add src/loom/tasks/config.py tests/tasks/test_task_config.py
git commit -m "feat: configure provider request options"
```

### Task 3: Migrate configuration, examples, and documentation

**Files:**
- Modify: `config.yaml`
- Modify: `README.md:100-130`
- Modify: `src/loom/examples/factories.py:220-250`
- Modify: `tests/integration/test_live_llm_smoke.py`

**Interfaces:**
- Consumes: Tasks 1-2 configuration names.
- Produces: No live provider output usage of `max_tokens`.

- [ ] **Step 1: Classify legacy-name matches**

Run:

```bash
rg -n "max_tokens" config.yaml README.md src/loom/llm src/loom/tasks src/loom/examples tests/llm tests/tasks tests/integration/test_live_llm_smoke.py
```

Migrate exactly the three provider output arguments in
`tests/integration/test_live_llm_smoke.py`, the two provider output arguments
in `src/loom/examples/factories.py`, the three model fields in `config.yaml`,
the README example, and the provider/config tests. Keep the goal budget at
`tests/integration/test_live_llm_smoke.py:169`, the example budget at
`src/loom/examples/factories.py:167`, token accounting, tool-selection limits,
and legacy rejection tests.

- [ ] **Step 2: Migrate executable configuration**

Change all three `config.yaml` models to:

```yaml
max_completion_tokens: 65536
```

Do not enable new capabilities in live models. Rename provider output keyword
arguments in examples and live tests, while preserving harness budgets.

- [ ] **Step 3: Document Bailian request options**

Use this README example:

```yaml
max_completion_tokens: 8192
request_options:
  enable_thinking: true
  thinking_budget: 2048
  tool_stream: true
  parallel_tool_calls: false
```

Explain that options are copied to the Chat request top level, Loom-owned keys
cannot be overridden, and Bailian validates model applicability. Link the
official OpenAI-compatible Chat API reference.

- [ ] **Step 4: Verify and commit migration**

Run:

```bash
uv run pytest tests/tasks/test_task_config.py tests/llm/test_llm.py tests/integration/test_live_llm_smoke.py -q
uv run ruff check src/loom/llm src/loom/tasks src/loom/examples tests/llm tests/tasks tests/integration/test_live_llm_smoke.py
```

Expected: tests pass or credential-dependent live tests skip; Ruff is clean.

Stage and commit the migrated files with:

```bash
git add config.yaml README.md src/loom/examples/factories.py tests/integration/test_live_llm_smoke.py
git commit -m "docs: migrate model completion configuration"
```

### Task 4: Full verification

**Files:**
- Verify: all Task 1-3 changes
- Test: complete `tests/` tree

**Interfaces:**
- Produces: Evidence that provider output migration is complete and harness budgets are intact.

- [ ] **Step 1: Run full tests and lint**

Run:

```bash
uv run pytest -q
uv run ruff check .
```

Expected: all tests pass, credential-gated tests may skip, and Ruff reports no
errors.

- [ ] **Step 2: Audit legacy names**

Run `rg -n "max_tokens" config.yaml README.md src tests`.

Expected: only harness budgets, token accounting, tool-selection limits, and
tests for legacy rejection remain.

- [ ] **Step 3: Review final state**

Run:

```bash
git diff --check
git status --short
git log -5 --oneline
```

Expected: no whitespace errors and all intended implementation commits are in
the log.
