# Loom

[English documentation](README.md)

Loom 是一个运行在本地的 AI Agent 任务执行框架，用于编程、资料调研和通用任务。你提供目标、模型和可用工具，Loom 完成计划、执行、验收，并从每次运行中学习。持久服务让你在 Web 和终端中跟踪同一个任务；断开界面后任务继续运行，重新连接即可查看已保存的状态。

## 主要特点

- **自主 Agent（可选启用）**：设定长期目标后，Loom 规划路线与阶段、通过执行队列派发工作包、在预算与执行失败时有界恢复，并复用相关 Session；独立的分析队列不阻塞交付地复盘每个终态 run，人工纠偏与方法指导作为记忆保存。使用 `loom serve --enable-agents` 启用，见[完整运行与调度设计](docs/design/autonomous-agents.zh-CN.md)。
- **执行路径驱动的自我迭代**：Deep Eval 分析每个终态 run，提取经过校验的记忆——阶段贡献、环境事实、失败教训和更有效的工作流——同 workspace 的后续 Session 自动检索复用。Trace 与轨迹分析把目标验收与执行质量分开评估，Meta-Harness 将积累的证据转为受治理约束的优化实验。见 [Deep eval Memory](docs/design/deep-eval-memory.md)、[Session 轨迹分析](docs/design/session-trajectory-analysis.md) 与[Meta-Harness](docs/design/meta-harness.md)。
- **独立验收**：按任务和 workspace 生成验收条件，执行真实命令检查并进行独立语义审阅；可选视觉条件在真实浏览器中重放场景，由图像模型判断实际截图。失败候选进入有界修复，保留具体阻塞原因。详见[验收设计](docs/design/2026-10-10-runtime-acceptance-gate-design.zh-CN.md)。
- **持久 Session（Web 与 TUI 共用后端）**：对话、checkpoint、工具结果、artifact 和事件在界面断开、服务重启后保留；已完成的 Session 可继续追加轮次。
- **模型、工具与工作流可配置**：OpenAI 兼容模型配置；文件、命令、URL、MCP、CLI 工具支持 LLM 推荐选择；动态工作流或 Plan & Execute。
- **本地知识库与带来源的报告**：关键词、向量混合、YakDB、LightRAG 检索，支持可续跑网站同步；报告保留来源引用并可落盘到 workspace。见[知识库指南](docs/design/knowledge-bases.md)。
- **失败诊断**：只读或在隔离 worktree 中调查已保存的 run，编写复现测试，审查后再合并补丁。见[设计与实现说明](docs/design/session-self-diagnosis.zh-CN.md)。

当前通用任务执行后端使用本机 OS，面向 macOS/Linux 和单个本地用户；Docker/VM 任务执行后端尚未实现。

## 快速开始

需要 Python 3.11+、`uv`，以及可用的 OpenAI 兼容模型 API。下面的 `uv` 命令都在 Loom 仓库目录执行。

### 1. 安装

```bash
uv sync
```

可选依赖：LightRAG 使用 `--extra knowledge-graph`；页面操作与视觉验收使用 `--extra browser --inexact`，并执行 `PLAYWRIGHT_SKIP_BROWSER_GC=1 .venv/bin/python -m playwright install chromium`。

### 2. 配置模型

创建本地 `.env`（已被 Git 忽略），替换为供应商提供的实际值：

```dotenv
LOOM_LLM_MODEL=your-model-id
LOOM_LLM_BASE_URL=https://your-provider.example/v1
LOOM_LLM_API_KEY=your-api-key
```

多模型和供应商参数见下方「配置方法」。

### 3. 启动服务

```bash
uv run loom serve --data-dir .loom/service
```

保持这个进程运行。默认监听 `127.0.0.1:8765`，同时提供 Web 页面，连接凭证保存在 `.loom/service/credential`。重启时继续使用原 data directory，才能保留 Session 和知识库。

### 4. 打开 Web 或 TUI

**Web**：访问 <http://127.0.0.1:8765/web/>，在连接窗口选择 credential 文件。**TUI**：在 Loom 仓库目录打开另一个终端：

```bash
uv run loom --workspace /absolute/path/to/project
```

新 Session 会立即保存，并等待任务输入。

## Web 使用方法

1. 使用服务启动时打印的 credential 连接。
2. 打开新建 Session 页面，填写任务目标，选择 **General**、**Research** 或**Coding**；**Recommend with LLM** 可推荐工具、模型和知识库。需要页面操作或视觉验收时，选择 **Browser & visual evidence** 并绑定 workspace。
3. 在对话区查看进展，展开 **Process** 检查模型请求、工具参数、结果和错误。
4. 发送补充要求或回答待处理问题，通过 **Pause**、**Resume**、**Stop** 控制执行。

**Complete task** 关闭任务；**Restart task** 沿用当前目标创建新 run——重建计划和验收、重置本轮预算，旧对话作为有界背景保留，旧 checkpoint 不沿用，工作区文件不回滚。**Session info → Edit task → Save & restart** 可一次性修改目标、模型、执行预算、plugins、工具集合、知识库和外部工具。CLI 等价命令：`loom session restart SESSION_ID`。

**Evaluation / Evolve → Session diagnosis** 只读或在隔离 worktree 中调查失败的 Session，并在冲突检查后合并审查过的补丁。**Knowledge bases** 页面管理导入、embedding、网站同步和图谱；**External tools** 页面注册 MCP 与 CLI 连接；**Trajectory** 打开执行分析；**Outputs** 展示已保存报告。详见 [Web 指南](docs/design/web-frontend.md) 和 [Session 创建指南](docs/design/session-setup.md)。

关闭浏览器标签页不会停止后端任务；凭证只保存在当前标签页内存，刷新后需重新连接。Web 页面随 Python 包发布，不需要 Node。

### 视觉验收

安装浏览器集合后，验收条件可以在独立浏览器中重放候选：场景等待真实渲染、执行交互步骤、采集多帧截图，由独立的图像模型结合确定性断言判断画面。在配置文件中声明支持图像输入的 `visual_verification_model`（示例见下方），或在 **Settings → Models** 中选择。安装、场景协议与证据边界见[视觉观察与验收指南](docs/design/visual-observation-and-acceptance.zh-CN.md)。

## 自主 Agent

```bash
uv run loom serve --data-dir .loom/service --enable-agents
```

Agent 保存角色与长期目标。调度器规划路线和阶段，将工作包派发到有界执行队列，在配置限额内恢复耗尽的预算；无法恢复时带着具体原因和推进方案暂停。终态 run 进入独立的分析队列进行 Deep Eval 与记忆提取。运行方式、恢复与人工介入见[自主 Agent 运行与调度设计](docs/design/autonomous-agents.zh-CN.md)。

## TUI 与 Session CLI 使用方法

```bash
# 新建 Session；默认 workspace 为当前目录
uv run loom

# 使用服务 config.yaml 中定义的模型别名
uv run loom --workspace /absolute/path/to/project --model dashscope/your-model-id

# 重新连接，并恢复符合条件的暂停/失败任务
uv run loom --resume SESSION_ID

# 列出 Session，或只连接而不请求恢复执行
uv run loom session list
uv run loom session connect SESSION_ID
```

TUI 展示 Session 列表、计划、流式执行、可展开的详情和最终结果。`Ctrl+C` 或 `Ctrl+Q` 断开前端连接，后端任务继续运行。也可以直接使用命令：

```bash
uv run loom session create '分析项目并修复失败的测试' --workspace /absolute/path/to/project
uv run loom session message SESSION_ID '优先检查 parser 测试'
uv run loom session pause SESSION_ID
uv run loom session resume SESSION_ID
uv run loom session stop SESSION_ID
uv run loom session snapshot SESSION_ID
uv run loom session history SESSION_ID --limit 100
uv run loom session artifact SESSION_ID DIGEST --output report.json
```

| 操作              | 行为                                   |
| ----------------- | -------------------------------------- |
| Pause / Resume    | 保存当前 run，在 checkpoint 边界继续。 |
| Stop              | 结束当前 run；后续恢复会开启新的 run。 |
| Complete / Reopen | 显式关闭或重新打开任务。               |
| Disconnect        | 仅断开界面连接，后端继续执行。         |

工具中断后，如果无法确认副作用是否发生，Loom 会要求先核验再继续。新 Session 默认 token 预算为 **10M**；在 TUI 中用 `/budget` 查看或修改，或使用 `uv run loom session budget SESSION_ID [20M]`。修改预算前先暂停活跃任务；Resume 会重新授予活跃时间窗口，并保留已消耗的 token、step 和模型调用次数。

## 配置方法

### 模型配置：`config.yaml`

需要多个模型或供应商参数时，创建配置文件：

```yaml
default_model: dashscope/your-model-id
# 可选：为评估选择独立配置的模型别名。
# evaluation_model: dashscope/your-fast-model-id
# 可选：仅覆盖 Deep Eval（含 Memory）及 CLI judge 的参数，需所选模型支持。
# evaluation_request_options:
#   enable_thinking: true
#   reasoning_effort: low
# 可选：运行时验收模型，默认使用任务模型。
# verification_model: dashscope/your-fast-model-id
# 可选：独立的视觉验收模型，必须声明支持 image 输入。
# visual_verification_model: dashscope/qwen3.8-omni-flash

providers:
  dashscope:
    base_url: https://your-provider.example/v1
    api_key_env: LOOM_LLM_API_KEY
    models:
      your-model-id:
        temperature: 0
        max_completion_tokens: 8192
        context_window_tokens: 128000
        request_options:
          enable_thinking: true
      your-fast-model-id:
        request_options:
          enable_thinking: false
      qwen3.8-omni-flash:
        input_modalities: [text, image]
        context_window_tokens: 1000000
        max_completion_tokens: 4096
        request_options:
          modalities: [text]
          reasoning_effort: none
          response_format:
            type: json_object
```

启动后端时显式指定该文件：

```bash
uv run loom serve --data-dir .loom/service --config config.yaml
```

`models` 下的节点名就是实际 API 模型 ID，所有模型引用使用 `provider/model` 形式；`api_key_env` 从后端环境变量或 `.env` 中读取 key。同一 provider 仅共用 `base_url` 和 `api_key_env`；同一 API 模型需要不同参数时，使用独立的 provider 配置组。旧版顶层 `models` 配置仍可读取，通过 **Settings → Models** 保存时转换为新结构。

`request_options` 将供应商参数（如 thinking 选项）传入流式和非流式请求。验收输入预算默认自动使用验证模型上下文的 60%；按模型设置 `context_window_tokens` 可覆盖明确标注的回退值。未传 `--config` 时，服务使用 `LOOM_LLM_*` 环境配置，并支持 `OPENAI_*` 作为后备值。

### 服务与客户端配置

模型 key 和模型配置属于**服务进程**。从不同目录启动时，使用绝对路径：

```bash
LOOM_ENV_FILE=/absolute/path/to/.env uv run --project /absolute/path/to/loom loom serve \
  --data-dir /absolute/path/to/service-data \
  --config /absolute/path/to/config.yaml
```

| 配置                      | 默认值 / 用途                                                 |
| ------------------------- | ------------------------------------------------------------- |
| `--data-dir`              | `.loom/service`，保存 Session 和知识库。                      |
| `--port`                  | `8765`，本地 HTTP API 和 Web 共用端口。                       |
| `--max-active-runs`       | `2`，Session worker 并发数；冲突的 workspace 写任务串行执行。 |
| `--enable-agents`         | 默认关闭；启用自主 Agent 调度。                               |
| `--no-web`                | 关闭内置 Web 页面，保留 API。                                 |
| `LOOM_ENV_FILE`           | `.env`，后端读取模型/embedding 密钥的文件。                   |
| `LOOM_SERVICE_URL`        | `http://127.0.0.1:8765`，客户端连接地址。                     |
| `LOOM_SERVICE_TOKEN_FILE` | `.loom/service/credential`，客户端凭证文件。                  |

服务使用其他 data directory 时，客户端指向对应的 credential（`export LOOM_SERVICE_TOKEN_FILE=/path/to/credential`），或使用 `--url URL --token-file PATH` 参数。连接凭证与模型 API key 是两种不同的凭证。

### 工具和工作流配置

模型配置决定**调用哪个模型**；任务配置决定**使用哪些工具、资源、上下文、工作流和输出要求**。Web 模板提供默认值，高级配置通过 `--task-spec` 指定，参考 [General](examples/task-specs/general.yaml)、[Research](examples/task-specs/research.yaml)、[Coding](examples/task-specs/coding.yaml) 示例。详见[通用任务与插件指南](docs/design/general-task-runtime.md)。

### 执行主机环境与 shell

执行与验收提示词包含 worker 实际的 OS、架构、shell 与容器标记，恢复 Session 时刷新。执行 shell 默认为 `/bin/bash -o pipefail -c`，可在任务配置中修改：

```yaml
execution_runtime:
  plugin: native_os
  shell: ["/bin/bash", "-o", "pipefail", "-c"]
```

### 知识库与 embedding 配置

| 引擎             | 配置与用途                                                           |
| ---------------- | -------------------------------------------------------------------- |
| `sqlite_fts`     | 默认关键词检索，不需要 embedding 服务。                              |
| `sqlite_hybrid`  | 关键词 + 向量检索，需要 embedding profile。                          |
| `yakdb_local`    | 单独安装 YakDB，支持本地 PDF/Office/文本解析和检索。                 |
| `lightrag_local` | 安装 `knowledge-graph` extra，需要配置索引模型和 embedding profile。 |

在 Web 中添加 embedding profile（完整 embeddings URL、模型 ID、可选 API-key 变量），用所选引擎创建知识库并绑定到 Session。限制、超时与恢复方式见[知识库指南](docs/design/knowledge-bases.md)。

## 一次性任务

不启动持久服务，也可以直接运行任务：

```bash
uv run loom task '分析项目并提出改进建议' \
  --workspace /absolute/path/to/project --config config.yaml --model dashscope/your-model-id --tui
```

与 `loom serve` 不同，`loom task` 会自动发现 `./config.yaml`，并可通过 `--memory-store` 复用服务数据目录中已接受的 Deep eval memory。参数、`run` 默认配置块与 trace 输出见[通用任务与插件指南](docs/design/general-task-runtime.md)。

## 进一步阅读

- [文档索引](docs/index.md)
- [Web 前端](docs/design/web-frontend.md)与 [TUI 展示](src/loom/tui/README.md)
- [Session 创建与配置推荐](docs/design/session-setup.md)
- [外部工具：MCP 与 CLI](docs/design/external-tools.zh-CN.md)
- [运行时验收：实现与配置](docs/design/2026-10-10-runtime-acceptance-gate-design.zh-CN.md)
- [Trace 效果分析](docs/design/trace-effectiveness-analysis.md)
- [Meta-Harness：优化、实验与治理](docs/design/meta-harness.md)

## 开发与验证

```bash
uv run python -m pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv build

# Web 测试；仅开发时需要 Node
npm --prefix tests/web ci
npm --prefix tests/web test
```

真实模型 smoke test 默认跳过，需要显式开启：

```bash
LOOM_RUN_LIVE_LLM=1 uv run python -m pytest tests/integration/test_live_llm_smoke.py -q
```

代码采用 `src/loom/` 包布局，Web 前端资源包含在 Python 发布包中。
