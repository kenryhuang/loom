# Loom

[English documentation](README.md)

Loom 是一个运行在本地的 AI Agent 任务执行框架，用于编程、资料调研和通用任务。
你提供目标、模型和可用工具，Loom 通过模型与工具循环推进任务，维护计划或工作流，
并保存执行过程和结果。

它可以帮助你分析代码库、修复问题、阅读资料并生成分析文档，也可以建立本地知识库，
供后续任务检索。持久服务让你在 Web 和终端中查看同一个任务；断开界面后，
任务仍可继续运行，重新连接时可以查看已保存的状态。

运行时验收会根据任务和 workspace 生成条件，执行实际检查及独立语义审阅；失败时有限修复或暂停，并展示验证证据。详见[实现范围与配置](docs/superpowers/specs/2026-10-10-runtime-acceptance-gate-design.zh-CN.md)。

## 主要特点

- **持久 Session**：保存对话、执行 checkpoint、工具结果、artifact 和事件历史，
  支持界面重连与服务重启后的恢复。一轮任务完成后，新消息会在同一 Session 中开启下一轮。
- **Web 与 TUI 共用后端**：提供模型流式输出、工具详情、任务控制、历史记录和 token 用量。
- **工具与工作流可配置**：按需选择文件、命令、URL 阅读、报告输出和知识检索工具。
  任务配置分别定义 workspace、上下文管理、执行后端与 workflow。
- **计划与上下文管理**：支持动态工作流及 Plan & Execute；
  压缩上下文时，完整原文仍保存在 artifact 中。
- **报告同时保存为文档和 artifact**：保留来源引用；绑定可写 workspace 后，
  `create_report` 会将分析文档写入目录，页面展示保存路径。
- **本地知识库**：支持关键词、向量混合、YakDB 和 LightRAG。
  LightRAG 支持网站同步、图谱浏览、索引进度展示和按已完成文档续跑。
- **可追踪的执行过程**：查看模型与工具活动、原始证据，分析 trace 和 Session 轨迹；
  高级评估及 Meta-Harness 命令支持实验和受治理约束的优化。可选的 [行为评估 v3](docs/behavior-evaluation-v3.zh-CN.md) 将目标验收与意图、计划、推进、推理效率及恢复能力分开评估。

当前通用任务执行后端使用本机 OS。项目面向 macOS/Linux 和单个本地用户；
Docker/VM 任务执行后端尚未实现。

## 快速开始

需要 Python 3.11+、`uv`，以及可用的 OpenAI 兼容模型 API。
下面的 `uv` 命令都在 Loom 仓库目录执行。

### 1. 安装

```bash
uv sync
```

这会在 `.venv` 中安装项目、开发工具和 TUI 依赖。
使用 LightRAG 时，额外安装图谱依赖：

```bash
uv sync --extra knowledge-graph
```

### 2. 配置模型

创建本地 `.env`，将模型、地址和 key 替换为供应商提供的实际值：

```dotenv
LOOM_LLM_MODEL=your-model-id
LOOM_LLM_BASE_URL=https://your-provider.example/v1
LOOM_LLM_API_KEY=your-api-key
```

`.env` 已被 Git 忽略。进程环境变量优先于文件中的同名配置。
多模型和供应商参数见下方「配置方法」。

### 3. 启动服务

```bash
uv run loom serve --data-dir .loom/service
```

保持这个进程运行。默认监听 `127.0.0.1:8765`，同时提供 Web 页面，
连接凭证保存在 `.loom/service/credential`。
重启时继续使用原 data directory，才能保留 Session 和知识库。

### 4. 打开 Web 或 TUI

**Web**：访问 <http://127.0.0.1:8765/web/>，在连接窗口选择 credential 文件，
或粘贴文件内容。

**TUI**：在 Loom 仓库目录打开另一个终端：

```bash
uv run loom --workspace /absolute/path/to/project
```

新 Session 会立即保存，并等待任务输入。输入目标后按 Enter 开始执行。

## Web 使用方法

1. 使用服务启动时打印的 credential 连接。
2. 打开新建 Session 页面，填写任务目标。
3. 选择 **General**、**Research** 或 **Coding**，检查工具、模型、workspace
   和知识库绑定。也可以点击 **Recommend with LLM** 获取建议，确认后点击
   **Create session**。
4. 在对话区查看进展，展开 **Process** 检查模型请求、工具参数、结果和错误。
   Workflow、预算和输出面板展示当前任务状态。
5. 发送补充要求或回答待处理问题，通过 **Pause**、**Resume**、**Stop** 控制执行。

Coding 和文件/命令工具需要服务所在机器上的现有目录。
Research 可以不绑定 workspace；如果希望分析文档落盘，请绑定可写目录，
结果会同时保留为 artifact。内置调研工具读取给定的 HTTP(S) URL，
当前没有内置搜索引擎，也不执行 JavaScript 浏览器。

**Knowledge bases** 页面管理文档导入、embedding profile、网站来源和图谱。
将知识库显式绑定到 Session 后，Agent 才能检索其中的资料。
LightRAG 的 **Sync activity** 展示当前页面或文档、执行阶段、已保存文档数量、
请求用量和近期活动；失败或取消后可通过 **Resume sync** 从保存进度继续。

**Trajectory** 打开 Session 执行分析，**Outputs** 展示已保存文档路径和报告 artifact。
详细说明见 [Web 指南](docs/web-frontend.md) 和 [Session 创建指南](docs/session-setup.md)。

关闭浏览器标签页不会停止后端任务。凭证只保存在当前标签页内存，
刷新后需要重新连接。Web 页面随 Python 包发布，不需要 Node 或单独的前端服务。

## TUI 与 Session CLI 使用方法

```bash
# 新建 Session；默认 workspace 为当前目录
uv run loom

# 使用服务 config.yaml 中定义的模型别名
uv run loom --workspace /absolute/path/to/project --model main

# 重新连接，并恢复符合条件的暂停/失败任务
uv run loom --resume SESSION_ID

# 列出 Session，或只连接而不请求恢复执行
uv run loom session list
uv run loom session connect SESSION_ID
```

TUI 展示 Session 列表、计划、流式执行、可展开的详情和最终结果。
Enter 提交消息或回答问题，History 和 Detail 获取历史事件及大型 artifact。
`Ctrl+C` 或 `Ctrl+Q` 断开前端连接，后端任务继续运行。

也可以直接使用命令：

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

工具中断后，如果无法确认副作用是否发生，Loom 会要求先核验再继续；
已提交的工具结果会被复用。

新 Session 默认 token 预算为 **10M**。创建时可指定 `--token-budget 500K`
或 `--token-budget 20M`，TUI 中通过 `/budget` 查看、`/budget 20M` 修改，
也可以使用：

```bash
uv run loom session budget SESSION_ID
uv run loom session budget SESSION_ID 20M
```

修改预算前先暂停活跃任务。预算统计当前 run 的输入和输出 token，
与单次模型响应的输出上限不同。Resume 会重新授予活跃时间窗口，默认 30 分钟，
但保留当前 run 已消耗的 token、step 和模型调用次数。

## 配置方法

### 模型配置：`config.yaml`

需要多个模型或供应商参数时，创建配置文件：

```yaml
default_model: main
# 可选：为评估选择独立配置的模型别名。
# evaluation_model: judge
# 可选：运行时验收模型，默认使用任务模型。
# verification_model: judge

models:
  main:
    provider: openai
    model: your-model-id
    base_url: https://your-provider.example/v1
    api_key_env: LOOM_LLM_API_KEY
    temperature: 0
    max_completion_tokens: 8192

  fast:
    provider: openai
    model: your-fast-model-id
    base_url: https://your-provider.example/v1
    api_key_env: LOOM_LLM_API_KEY
```

启动后端时显式指定该文件：

```bash
uv run loom serve --data-dir .loom/service --config config.yaml
```

`main`、`fast` 是 Loom 内部别名；`model` 是供应商的实际模型 ID。
Web/TUI 中选择的是服务端配置的别名。
`api_key_env` 从后端环境变量或 `.env` 中读取 key。

`provider: openai` 表示使用 OpenAI 兼容 Chat API 适配器。
`max_completion_tokens` 限制模型输出，包含 reasoning 内容。
`request_options` 将供应商参数传入流式和非流式请求，例如模型支持的 thinking 参数。
参数必须适用于该供应商和模型，不能覆盖 Loom 管理的 `messages`、
`tools`、`stream` 等字段。

服务没有传入 `--config` 时，使用 `LOOM_LLM_*` 环境配置，
也支持 `OPENAI_MODEL`、`OPENAI_BASE_URL`、`OPENAI_API_KEY` 作为后备值。
`loom serve` 不会自动加载目录中现有的 `config.yaml`。

### 服务与客户端配置

模型 key 和模型配置属于**服务进程**。
从不同目录启动时，可以指定绝对路径：

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
| `--no-web`                | 关闭内置 Web 页面，保留 API。                                 |
| `LOOM_ENV_FILE`           | `.env`，后端读取模型/embedding 密钥的文件。                   |
| `LOOM_SERVICE_URL`        | `http://127.0.0.1:8765`，客户端连接地址。                     |
| `LOOM_SERVICE_TOKEN_FILE` | `.loom/service/credential`，客户端凭证文件。                  |

如果服务使用不同 data directory，客户端应使用启动时打印的 credential 路径：

```bash
export LOOM_SERVICE_URL=http://127.0.0.1:8765
export LOOM_SERVICE_TOKEN_FILE=/absolute/path/to/service-data/credential
uv run loom
```

也可以使用 `loom --url URL --token-file PATH`，
或 `loom session --url URL --token-file PATH list`。
服务连接凭证与模型 API key 是两种不同的凭证。

### 工具和工作流配置

模型配置决定**调用哪个模型**；任务配置决定**使用哪些工具、资源、
上下文、工作流和输出要求**。Web 模板提供默认值，
高级配置通过 `--task-spec` 指定：

```bash
uv run loom session create '读取 https://www.python.org/about/ 并写出带来源的总结' \
  --task-spec examples/task-specs/research.yaml
```

参考 [通用任务与插件指南](docs/general-task-runtime.md)，以及
[General](examples/task-specs/general.yaml)、
[Research](examples/task-specs/research.yaml)、
[Coding](examples/task-specs/coding.yaml) 示例。
显式任务配置中的目录 URI 相对配置文件解析，
`--workspace` 不会覆盖这些资源绑定。

对于 legacy planning workflow，`--plan-mode auto` 让模型按需进入计划，
`force` 要求先提交计划，`off` 使用不含计划工具的 ReAct 路径。
General/Research 模板使用 dynamic workflow。

### 知识库与 embedding 配置

| 引擎             | 配置与用途                                                           |
| ---------------- | -------------------------------------------------------------------- |
| `sqlite_fts`     | 默认关键词检索，不需要 embedding 服务。                              |
| `sqlite_hybrid`  | 关键词 + 向量检索，需要 embedding profile。                          |
| `yakdb_local`    | 单独安装 YakDB，支持本地 PDF/Office/文本解析和检索。                 |
| `lightrag_local` | 安装 `knowledge-graph` extra，需要配置索引模型和 embedding profile。 |

在 Web 中添加 embedding profile，填写**完整 embeddings URL**
（例如 `http://localhost:11434/v1/embeddings`）、已安装的模型 ID，
以及可选的 API-key 环境变量名。随后使用所选引擎创建知识库，并绑定到 Session。

LightRAG 网站索引包含 LLM 实体/关系抽取和 embedding；
大型文档可能需要多个 chunk 和多次模型调用。
数据存储在本地，选定的模型/embedding 服务会接收请求所需内容。
更换绑定的模型或 embedding 身份时，需要新建知识库。
限制、超时与恢复方式见 [知识库指南](docs/knowledge-bases.md)。

## 一次性任务

不启动持久服务，也可以直接运行任务：

```bash
uv run loom task '分析项目并提出改进建议' \
  --workspace /absolute/path/to/project --config config.yaml --model main --tui

uv run loom task '读取 https://www.python.org/about/ 并总结，注明来源' \
  --task-spec examples/task-specs/research.yaml --config config.yaml
```

与 `loom serve` 不同，`loom task` 会自动发现 `./config.yaml` 或
`./config.yml`，命令行参数覆盖配置默认值。
可选的 `task`、`run` 块用于一次性执行：

```yaml
task:
  workspace: .
  expected_outputs:
    - A Markdown analysis report.

run:
  tui: true
  stream: true
  plan_mode: auto
  trace_path_template: runs/{task_slug}-{model}-{timestamp}.jsonl
  max_steps: 100
  timeout_ms: 120000
```

`timeout_ms` 是每个 step 的超时。这两个块不配置持久 Session 的执行限制。
传入 `--task-spec` 后，一次性任务不继承 `task` 块，
模型和 `run` 配置仍然生效。
默认 JSONL trace 写入 `runs/`；一次性任务依赖启动它的进程存活。

## 进一步阅读

- [Web 前端](docs/web-frontend.md)与 [TUI 展示](src/loom/tui/README.md)
- [Session 创建与配置推荐](docs/session-setup.md)
- [任务插件、工作流和报告输出](docs/general-task-runtime.md)
- [知识库与 LightRAG 同步](docs/knowledge-bases.md)
- [Session 轨迹分析](docs/session-trajectory-analysis.md)
- [Trace 效果分析](docs/trace-effectiveness-analysis.md)
- [Meta-Harness：优化、实验与治理](docs/meta-harness.md)
- [工具执行与进度](docs/tool-execution-and-progress-review.md)

## 开发与验证

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv build

# Web 测试；仅开发时需要 Node
npm --prefix tests/web ci
npm --prefix tests/web test
```

真实模型 smoke test 默认跳过，需要显式开启：

```bash
LOOM_RUN_LIVE_LLM=1 uv run pytest tests/integration/test_live_llm_smoke.py -q
```

代码采用 `src/loom/` 包布局，Web 前端资源包含在 Python 发布包中。
