# Local Bailian Model Options Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Configure model-appropriate Bailian reasoning, tool streaming, sequential tool calls, and stream usage reporting in the operator-local `config.yaml`.

**Architecture:** The ignored local configuration uses the existing `ModelConfig.request_options` passthrough. Each model receives only the options approved for its profile; validation uses Loom's real dependency-free YAML loader without making a paid model request.

**Tech Stack:** YAML, `loom.tasks.config.load_task_config`, pytest.

## Global Constraints

- Do not set a fixed `thinking_budget`.
- Do not enable search or code interpreter.
- Keep every `max_completion_tokens` value at `65536`.
- Qwen and GLM enable `tool_stream`; Kimi does not.
- GLM and Kimi use `reasoning_effort: max`; Qwen uses `enable_thinking: true`.
- All profiles request stream usage and disable parallel tool calls.
- `config.yaml` remains ignored and is not added to Git.

---

### Task 1: Configure and validate the three local model profiles

**Files:**
- Modify: `config.yaml:17-38`
- Verify: `tests/tasks/test_task_config.py`

**Interfaces:**
- Consumes: `load_task_config("config.yaml") -> Result` and existing `ModelConfig.request_options`.
- Produces: `main`, `glm`, and `kimi` profiles with immutable nested request options.

- [ ] **Step 1: Verify the approved options are currently absent**

Run:

```bash
uv run python -c 'from loom.tasks.config import load_task_config; c=load_task_config("config.yaml").unwrap(); assert c.models["main"].request_options["enable_thinking"] is True'
```

Expected: FAIL with `KeyError: 'enable_thinking'` because the local profiles
currently have no `request_options`.

- [ ] **Step 2: Add the exact local request options**

The model portion of `config.yaml` must become:

```yaml
models:
  main:
    provider: openai
    model: qwen3.7-max
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: LOOM_LLM_API_KEY
    temperature: 0
    max_completion_tokens: 65536
    request_options:
      enable_thinking: true
      tool_stream: true
      parallel_tool_calls: false
      stream_options:
        include_usage: true
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
      stream_options:
        include_usage: true
  kimi:
    provider: openai
    model: kimi-k3
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: LOOM_LLM_API_KEY
    temperature: 0
    max_completion_tokens: 65536
    request_options:
      reasoning_effort: max
      parallel_tool_calls: false
      stream_options:
        include_usage: true
```

- [ ] **Step 3: Validate all parsed values through Loom**

Run:

```bash
uv run python -c 'from loom.tasks.config import load_task_config; c=load_task_config("config.yaml").unwrap(); q=c.models["main"].request_options; g=c.models["glm"].request_options; k=c.models["kimi"].request_options; assert q["enable_thinking"] is True and q["tool_stream"] is True and q["stream_options"]["include_usage"] is True; assert g["enable_thinking"] is True and g["reasoning_effort"] == "max" and g["tool_stream"] is True and g["stream_options"]["include_usage"] is True; assert k["reasoning_effort"] == "max" and "tool_stream" not in k and k["stream_options"]["include_usage"] is True; assert all(m.max_completion_tokens == 65536 for m in c.models.values())'
```

Expected: exit code 0 with no output.

- [ ] **Step 4: Run configuration regression tests**

Run:

```bash
uv run pytest tests/tasks/test_task_config.py -q
```

Expected: all task configuration tests pass.

- [ ] **Step 5: Verify the ignored local file did not dirty Git**

Run:

```bash
git check-ignore -v config.yaml
git status --short
```

Expected: `.gitignore` identifies `config.yaml`; Git reports no change for the
local file.
