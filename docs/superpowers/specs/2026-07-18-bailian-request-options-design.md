# Bailian Request Options Design

## Status

Approved for implementation on 2026-07-18. Amended after implementation to
define the operator's local model-profile options.

## Context

Loom's OpenAI-compatible provider currently exposes `max_tokens` as a model
configuration field and sends it to `/chat/completions`. Alibaba Cloud Model
Studio marks that parameter as deprecated for new integrations and recommends
`max_completion_tokens`, which limits the complete model output, including
reasoning content. Bailian also exposes model-specific parameters such as
`enable_thinking`, `thinking_budget`, `reasoning_effort`, `tool_stream`, and
`parallel_tool_calls`.

Mirroring every Bailian parameter as a Loom dataclass field would make Loom's
configuration surface large and quickly outdated. Loom instead needs a small,
stable set of provider-neutral fields plus a controlled extension point for
provider request parameters.

Official API reference:

- <https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions>

## Goals

- Replace the model-output setting `max_tokens` with
  `max_completion_tokens` without a compatibility alias.
- Let task model configurations send current and future Bailian Chat API
  parameters without a Loom release for every new parameter.
- Keep Loom-owned request fields authoritative.
- Give YAML and TOML configurations equivalent nested parameter support.
- Preserve Loom's zero-runtime-dependency package design.

## Non-goals

- Rename Loom's separate goal, run, or tool-selection token budgets. Those
  fields measure harness consumption and continue to use `max_tokens`.
- Maintain a model-to-parameter compatibility matrix inside Loom.
- Persist complete reasoning chains in Loom context or replay them in later
  requests.
- Automatically enable thinking, search, code interpreter, or any other
  provider capability in the provider implementation. Operators may enable
  supported capabilities explicitly in a model profile.
- Add PyYAML or another runtime dependency.

## Configuration Contract

`ModelConfig` keeps the provider-neutral connection fields and adds an
explicit complete-output limit and an extension mapping:

```python
@dataclass(frozen=True, slots=True)
class ModelConfig:
    provider: str = "openai"
    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    api_key_env: str | None = None
    temperature: float | None = None
    max_completion_tokens: int | None = None
    request_options: Mapping[str, JsonValue] = field(default_factory=dict)
```

Example:

```yaml
models:
  glm:
    provider: openai
    model: glm-5.2
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: LOOM_LLM_API_KEY
    temperature: 0
    max_completion_tokens: 65536
    request_options:
      enable_thinking: true
      reasoning_effort: max
      tool_stream: true
      parallel_tool_calls: false
```

The repository ignores `config.yaml`; it is an operator-local configuration,
not a checked-in project artifact. The provider never supplies implicit
request options. The README demonstrates representative Bailian thinking and
tool options, while the operator may opt into them explicitly per model.

### Local model profiles

The approved local configuration enables only model-appropriate reasoning,
tool streaming, sequential tool calls, and usage reporting:

```yaml
models:
  main:
    model: qwen3.7-max
    request_options:
      enable_thinking: true
      tool_stream: true
      parallel_tool_calls: false
      stream_options:
        include_usage: true

  glm:
    model: glm-5.2
    request_options:
      enable_thinking: true
      reasoning_effort: max
      tool_stream: true
      parallel_tool_calls: false
      stream_options:
        include_usage: true

  kimi:
    model: kimi-k3
    request_options:
      reasoning_effort: max
      parallel_tool_calls: false
      stream_options:
        include_usage: true
```

No fixed `thinking_budget` is configured. `max_completion_tokens` remains the
complete-output ceiling, and each model retains its provider-defined reasoning
budget behavior. Search and code interpreter remain disabled. The local file
is validated through `load_task_config`; no paid model call is required to
verify the configuration structure and provider construction.

### Breaking migration

`max_tokens` is no longer accepted by `ModelConfig`, `OpenAIProvider`,
`create_env_openai_provider`, or task model configuration files. A task config
containing `models.<name>.max_tokens` fails with `VALIDATION_FAILED` and tells
the operator to use `max_completion_tokens`.

This removal applies only to provider output limits. Domain fields such as
`GoalBudget.max_tokens` and `ToolSelectionConfig.max_tokens` remain unchanged.

### Model field ownership

The allowed keys directly under each model are:

- `provider`
- `model`
- `base_url`
- `api_key`
- `api_key_env`
- `temperature`
- `max_completion_tokens`
- `request_options`

Any other top-level model key fails validation. Provider-specific parameters
belong under `request_options`; rejecting unknown top-level fields prevents
typos from being silently ignored.

## Request Options

`request_options` is a string-keyed mapping whose values must be JSON-compatible:
null, boolean, number, string, list, or nested string-keyed mapping. Loom makes
an owned copy at its configuration boundary so callers cannot mutate provider
behavior after construction.

Loom forwards these values to the top level of both streaming and non-streaming
`/chat/completions` request bodies. It intentionally does not validate whether
a particular model supports an option. Bailian remains the authority for model
capabilities and returns the provider error when a combination is invalid.

The following keys are reserved and rejected inside `request_options`:

- `model`
- `messages`
- `stream`
- `tools`
- `tool_choice`
- `temperature`
- `max_completion_tokens`
- `max_tokens`

The first five are constructed dynamically by Loom. The next two have explicit
configuration fields. The legacy `max_tokens` name is forbidden everywhere in
the provider configuration surface.

Other fields, including `stream_options`, remain available to callers. Example
Bailian options include:

- reasoning: `enable_thinking`, `thinking`, `preserve_thinking`,
  `thinking_budget`, `reasoning_effort`;
- generation: `top_p`, `top_k`, `repetition_penalty`, `presence_penalty`,
  `seed`, `stop`, `response_format`;
- tools and services: `tool_stream`, `parallel_tool_calls`, `enable_search`,
  `search_options`, `enable_code_interpreter`.

This list documents examples, not an allowlist.

## Request Construction

`OpenAIProvider.chat` and `OpenAIProvider.stream_chat` delegate to one private
request-body builder. The builder performs these operations in order:

1. Create `model` and normalized `messages`.
2. Add configured `temperature` and `max_completion_tokens` when present.
3. Merge validated `request_options`.
4. Add the runtime `stream` flag for streaming calls.
5. Add runtime-selected `tools` and `tool_choice` when applicable.

Reserved-key validation makes the merge order unambiguous. A shared builder
ensures that thinking and other provider options behave identically in both
execution modes.

## YAML and TOML Parsing

TOML already represents nested mappings through tables. The dependency-free
YAML subset parser is extended to handle arbitrary nested mappings and lists
under `request_options`, including both block lists and existing inline scalar
lists. It continues to reject unsupported or malformed YAML with a line-aware
`VALIDATION_FAILED` result.

The parser extension is scoped to configuration data required by Loom; it is
not intended to become a complete YAML implementation.

## Error Handling

Configuration errors fail before any external request:

- legacy `max_tokens`: explain the replacement field;
- unknown model field: report the model name and offending fields;
- non-mapping `request_options`: report that a mapping is required;
- reserved request option: report all conflicting keys;
- non-JSON-compatible value or non-string nested key: report the invalid path.

Provider capability errors remain normal `LLM_FAILED` results using the
existing HTTP status and error-body mapping.

## Documentation and Migration

The implementation updates:

- all live provider factories and examples from `max_tokens` to
  `max_completion_tokens`;
- the operator-local, ignored `config.yaml` model output limits and explicitly
  selected request options;
- the README task configuration example and parameter explanation.

Historical design and implementation documents are not rewritten. References
to harness token budgets also remain `max_tokens` by design.

## Verification

Automated tests cover:

- YAML and TOML parsing of nested request mappings and lists;
- rejection of `max_tokens`, unknown model fields, invalid request mappings,
  non-JSON values, and reserved-key conflicts;
- provider construction from named task models;
- exact non-streaming and streaming request bodies;
- presence of `max_completion_tokens` and absence of `max_tokens`;
- representative Bailian reasoning and tool parameters without an allowlist.

The final verification runs the focused task/provider tests, the complete test
suite, and Ruff. A repository search confirms that remaining `max_tokens`
occurrences belong only to harness budgets, tests for legacy rejection, or
historical documents.
