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

- **Deep eval 经验记忆**：从 trace 提取 stage 对目标的贡献、环境与配置事实、失败经验和更有效的工作流。
  每条记录保留 goal / stage / action 与原始证据引用，区分观察、推断和待验证建议；支持预算恢复。
  使用方法与适用范围见 [Deep eval Memory](docs/deep-eval-memory.md)；
  [分析管线与校验机制](docs/deep-eval-validation.zh-CN.md)说明模块边界、具体错误和有限重试规则。
- **持久 Session**：保存对话、执行 checkpoint、工具结果、artifact 和事件历史，
  支持界面重连与服务重启后的恢复。一轮任务完成后，新消息会在同一 Session 中开启下一轮。
- **Web 与 TUI 共用后端**：提供模型流式输出、工具详情、任务控制、历史记录和 token 用量。
- **工具与工作流可配置**：按需选择文件、命令、URL 阅读、报告输出和知识检索工具。
  任务配置分别定义 workspace、上下文管理、执行后端与 workflow。
- **执行主机环境感知**：执行与验收模型的系统提示词包含 OS、架构、实际 shell 和容器信息，
  提示模型使用对应平台的命令、路径与工具参数；恢复 Session 时刷新环境信息。
- **Web / Canvas / WebGL 视觉验收**：通过可选浏览器工具操作页面、等待渲染并采集多帧截图，
  由独立多模态模型结合浏览器断言与运行时事实判断结果，保留原图和验收证据。
- **计划与上下文管理**：支持动态工作流及 Plan & Execute；
  压缩上下文时，完整原文仍保存在 artifact 中。
- **报告同时保存为文档和 artifact**：保留来源引用；绑定可写 workspace 后，
  `create_report` 会将分析文档写入目录，页面展示保存路径。
- **本地知识库**：支持关键词、向量混合、YakDB 和 LightRAG。
  LightRAG 支持网站同步、图谱浏览、索引进度展示和按已完成文档续跑。
- **外部工具**：注册 MCP 服务或 CLI 命令，手工选择或通过 LLM 推荐选择具体工具，
  接入同一个 Task Loop，保留执行控制、调用记录和结果。
- **可追踪的执行过程**：查看模型与工具活动、原始证据，分析 trace 和 Session 轨迹；
  高级评估及 Meta-Harness 命令支持实验和受治理约束的优化。可选的 [行为评估 v3](docs/behavior-evaluation-v3.zh-CN.md) 将目标验收与意图、计划、推进、推理效率及恢复能力分开评估。

当前通用任务执行后端使用本机 OS。项目面向 macOS/Linux 和单个本地用户；
Docker/VM 任务执行后端尚未实现。环境探测可识别当前进程的容器标记及 Docker CLI，
不代表 Loom 会自动创建容器或将任务隔离到 Docker 中。

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

使用 Web / Canvas / WebGL 视觉功能时，安装可选浏览器依赖和 Chromium：

```bash
uv sync --extra browser --inexact
PLAYWRIGHT_SKIP_BROWSER_GC=1 .venv/bin/python -m playwright install chromium
```

`--inexact` 保留环境中已安装的其他可选依赖。浏览器在安装阶段下载，任务运行时不自动安装。

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
   **Create session**。需要实际页面操作或视觉验收时，选择 **Browser & visual evidence**；
   本地应用预览同时需要 workspace、Files 和 Commands。
4. 在对话区查看进展，展开 **Process** 检查模型请求、工具参数、结果和错误。
   Workflow、预算和输出面板展示当前任务状态。
5. 发送补充要求或回答待处理问题，通过 **Pause**、**Resume**、**Stop** 控制执行。

Coding 和文件/命令工具需要服务所在机器上的现有目录。
Research 可以不绑定 workspace；如果希望分析文档落盘，请绑定可写目录，
结果会同时保留为 artifact。内置调研工具读取给定的 HTTP(S) URL，
当前没有内置搜索引擎；需要执行页面 JavaScript 或检查 Canvas/WebGL 时，使用可选浏览器集合。

任务控制中，**Complete task** 关闭任务，**Reopen task** 恢复消息输入，但不会自动执行。
需要从头运行时点击 **Restart task**：沿用当前目标，重新绑定当前注册的外部工具，创建新 run，
重建计划和验收，重置本轮预算用量；旧对话和结果作为有长度限制的背景提供给模型，旧 checkpoint 和验收结果不沿用，工作区文件不回滚。
在 **Session info → Details → Edit task → Save & restart** 中可修改目标、模型、执行预算、
context/workflow plugins、工具集合、知识库和外部工具。已有 Session 的 workspace 只读展示，
更换目录需要新建 Session。配置验证与保存、重启一次完成，验证失败不会改变原任务。
编辑后的目标作为新请求；直接 Restart 时，暂停期间尚未执行的追加要求会合并到新一轮目标。
旧 worker 退出后，可从 idle、paused、failed、completed 直接重跑。
TUI 也提供这些按钮，CLI 使用 `loom session restart SESSION_ID`。

在 **Evaluation / Evolve → Session diagnosis** 中选择 run 和模型，按需填写问题，
点击 **Diagnose session**。独立 Loom 循环读取脱敏的源码、trace、配置与装配快照，
默认 **Worktree verification** 会在 `.worktrees/` 创建独立分支与工作区，包含当前未提交代码，
允许编写复现测试、修改代码并在操作系统沙箱中离线验证。默认 **Auto-delete worktree**
在诊断结束后删除临时分支和工作区，保留报告、测试记录和补丁；也可选择 **Keep for review**。
通过 **Review patch → Merge reviewed changes** 显式应用补丁，冲突时拒绝覆盖；
不自动暂存、提交或重启服务。**Read-only investigation** 保留原有只读调查方式。
执行验证需要 macOS `sandbox-exec` 或 Linux `bubblewrap`，无无限制执行的降级路径。
预算耗尽后在 **Budgets** 增加对应总预算，再 **Resume diagnosis**，复用已有证据。
详见[设计与实现说明](docs/session-self-diagnosis.zh-CN.md)。

**Knowledge bases** 页面管理文档导入、embedding profile、网站来源和图谱。
将知识库显式绑定到 Session 后，Agent 才能检索其中的资料。
LightRAG 的 **Sync activity** 展示当前页面或文档、执行阶段、已保存文档数量、
请求用量和近期活动；失败或取消后可通过 **Resume sync** 从保存进度继续。

**Trajectory** 打开 Session 执行分析，**Outputs** 展示已保存文档路径和报告 artifact。

**External tools** 页面注册 MCP（stdio / Streamable HTTP / SSE）和 CLI 连接。
发现 MCP 工具后，可在 **New session** 中手工勾选，或通过 **Recommend with LLM**
推荐；已有 Session 可在任务之间调整绑定。安装、配置和示例见
[外部工具指南](docs/external-tools.zh-CN.md)。
详细说明见 [Web 指南](docs/web-frontend.md) 和 [Session 创建指南](docs/session-setup.md)。

关闭浏览器标签页不会停止后端任务。凭证只保存在当前标签页内存，
刷新后需要重新连接。Web 页面随 Python 包发布，不需要 Node 或单独的前端服务。

### Web / Canvas / WebGL 视觉验收

浏览器集合提供 `browser_open`、`browser_act`、`browser_observe`、`browser_capture` 和
`browser_close`。执行 Agent 可以观察页面；正式验收在独立浏览器中重放场景，
将实际截图交给独立的图像模型。配置方法见下方 `visual_verification_model` 示例，
也可在 **Settings → Models** 设置 Input modalities，并在 **General** 选择 Visual acceptance model。

本地预览使用当前候选源码的有界副本和独立端口，场景通过 `preview.argv` 的 `{port}` 占位符
启动实际应用。外部页面需要显式绑定 URL resource；正式验收不直接复用版本不明的已有 dev server。
Loom 自身的 Web 界面不需要 Node，但被验收的应用仍需具备自己的运行依赖。

截图前最多等待 **12 秒**，检查文档与资源加载、可见 Canvas 初始化和实际渲染调用；
可识别的 React Three Fiber 场景还会等待空 root 挂载内容，减少只截到 UI 或纹理加载黑屏的情况。
未指定采样时刻且要求 Canvas 或检测到 WebGL 时，就绪等待后默认延时 1.5 秒采第一张，
再间隔 1 秒采第二张。就绪不等于验收通过，超时仍保留实际画面和诊断。

在 **Processing / 验收记录 → View screenshot** 查看原图和判断区域。
证据还包含就绪耗时、超时阶段、页面异常、失败资源请求、HTTP 错误及可用的场景事实。
确定性浏览器断言失败不能被模型覆盖；精确对象数量需要断言或独立运行时证据，不能只靠截图猜测。
视角或画面不足时最多补采一次，仍不足则保留具体阻塞原因。

**Input budget** 表示单次模型上下文容量，**Acceptance budget** 表示验收调用、耗时和失败候选额度。
视觉任务会按条件数调整默认验收额度，任务显式配置的上限仍有效。验收阻塞或额度耗尽后，
明确点击 **Resume** 会保留历史记录并开启新的有界验收额度，重新采证；任务总用量和原始要求保留。
单纯重启服务不会补充额度，暂停或环境阻塞也不算作候选修复失败。

首期覆盖 Web 页面、Canvas 和 WebGL。安装配置、场景协议、证据边界和恢复流程详见
[视觉观察与验收指南](docs/visual-observation-and-acceptance.zh-CN.md)。

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
default_model: dashscope/your-model-id
# 可选：为评估选择独立配置的模型别名。
# evaluation_model: dashscope/your-fast-model-id
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

`models` 下的节点名就是实际 API 模型 ID，无需再配置 `model` 字段。
所有模型选择和角色引用均使用 `provider/model`，例如 `dashscope/your-model-id`。
`api_key_env` 从后端环境变量或 `.env` 中读取 key。

使用视觉验收时，取消 `visual_verification_model` 行的注释，并使用该模型实际可用的供应商 endpoint。
示例中的 Qwen3.8-Omni-Flash 采用 JSON Mode，结果由 Loom 本地校验。
`verification_model` 负责普通验收，`visual_verification_model` 负责图像判断，两者可分别选择。

同一个 provider 仅共用 `base_url` 和 `api_key_env`，其他参数放在各自模型下。不同 provider 可以使用相同模型 ID。同一 API 模型需要不同参数配置时，使用独立的 provider 配置组（例如 `dashscope` 和 `dashscope_judge`）。Settings → Models 可填写供应商连接及其模型参数。旧版配置仍可读取，通过 Settings 保存时转换为新结构；顶层 `models` 和 `providers` 不能混用。迁移保留的 `model_aliases` 仅用于兼容旧 session、知识库及评估记录的引用，不会出现在选择列表中。

每个模型默认使用 OpenAI 兼容 Chat API 适配器（`provider: openai`）。
`max_completion_tokens` 限制模型输出，包含 reasoning 内容。

验收输入预算默认使用**验证模型上下文的 60%**，按多语言 token 估算并预留输出空间，不再默认使用固定字符上限。已知模型容量自动识别；自定义模型或部署可在 `config.yaml` 的 `providers.<供应商>.models.<模型ID>` 下设置 `context_window_tokens`（例如 `context_window_tokens: 128000`）。未知模型使用明确标注的 32,768 token 回退值。该字段仅供 Loom 使用，不发送给模型 API。任务显式设置的 `acceptance.max_prompt_chars` 仍作为额外上限；省略或设为 `null` 即使用自动预算。输入超限时会显示估算用量和容量，不会静默截断证据。输入估算与服务商报告的实际 token 用量分开记录。
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

### 执行主机环境与 shell

执行模型与验收模型的系统提示词会加入当前 worker 的 OS、版本、CPU 架构、
实际执行 shell 及参数、登录 shell、容器检测结果和 Docker CLI 可用性。
这些信息描述工具实际运行的位置；容器内报告的是容器的 OS，浏览器客户端所在机器不决定命令格式。
恢复旧 Session 时会刷新环境信息。

`shell_execute` 默认通过 `/bin/bash -o pipefail -c` 执行，不跟随登录 shell 自动切换；
例如 macOS 登录 shell 为 zsh，工具仍可能使用 Bash。需要调整时，在**任务配置**中指定：

```yaml
execution_runtime:
  plugin: native_os
  shell: ["/bin/bash", "-o", "pipefail", "-c"]
```

模型会收到适用的命令提示，例如 macOS 默认 BSD 工具与 GNU 参数的差异。
`process_execute` 直接接收字面量 argv，不执行 shell 展开。容器探测结合 Docker/Podman 标记和
cgroup；未检测到标记不保证位于裸机。Docker CLI 存在与当前是否位于容器内分别记录，
daemon 连通性标为未探测，使用 Docker 前仍需实际检查。

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
  --workspace /absolute/path/to/project --config config.yaml --model dashscope/your-model-id --tui

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
- [外部工具：MCP 与 CLI](docs/external-tools.zh-CN.md)
- [Web / Canvas / WebGL 视觉观察与验收](docs/visual-observation-and-acceptance.zh-CN.md)
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
