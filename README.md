# Loom Python

Python implementation of Loom, built as a sibling project to the TypeScript `loom`
implementation.

## Development Environment

This project uses `uv` for a reproducible local environment.

```bash
uv sync
```

That creates `.venv` and installs the project with its development tools from
`uv.lock`.

## Verify

```bash
uv run pytest
```

Developer checks:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
```

## Build

```bash
uv build
```

## Package Layout

The project uses a `src` layout. Subpackage `__init__.py` files are export
shims only; implementation lives in named modules such as `core/models.py`,
`runtime/engine.py`, `llm/api.py`, and `observability/traces.py`.

## LLM Configuration

LLM examples use the existing OpenAI-compatible provider. If no `api_key` is
passed, Loom reads local settings from the project `.env` file.

The minimal `.env` shape is:

```bash
LOOM_LLM_MODEL=qwen3.6-max-preview
LOOM_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LOOM_LLM_API_KEY=...
```

The `.env` file is local-only and ignored by git. The same values can also be
passed with process environment variables. Generic `OPENAI_MODEL`,
`OPENAI_BASE_URL`, and `OPENAI_API_KEY` are supported as fallbacks.

Live LLM smoke tests are skipped by default. To run one real OpenAI-compatible
LLM call through the full Loom runtime/tool/trace chain:

```bash
LOOM_RUN_LIVE_LLM=1 uv run pytest tests/integration/test_live_llm_smoke.py -q
```

Optional knobs:

```bash
LOOM_LIVE_ENV_FILE=.env
LOOM_LIVE_MAX_TOKENS=512
LOOM_LIVE_TEMPERATURE=0
```
