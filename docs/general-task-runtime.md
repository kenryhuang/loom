# 通用任务与执行插件

一次性任务和持久 session 共用 `TaskAssembly`。通过 `--task-spec` 选择 session 环境、上下文管理、工具集合、执行后端和 workflow。配置支持 JSON、YAML、TOML；现有模型配置继续使用 `config.yaml` 或环境变量。

一次性任务传入 `--task-spec` 后，不再继承 `config.yaml` 的 `task` 项（objective、workspace、profile、expected_outputs），避免项目审计配置影响通用任务。模型和 `run` 配置继续生效，显式任务参数仍优先。`--task-spec` 只能传一次；启动时展示配置路径和工具集合。

## 运行示例

在仓库根目录运行无需代码目录的回答任务：

```bash
uv run loom task '解释事件驱动架构，给出适用场景' \
  --task-spec examples/task-specs/general.yaml
```

根据明确的 HTTP/HTTPS URL 调研并输出带来源的报告：

```bash
uv run loom task '读取 https://www.python.org/about/ 并用中文总结用途，注明来源' \
  --task-spec examples/task-specs/research.yaml
```

持久 session 使用同一个配置文件：

```bash
uv run loom serve --config config.yaml
# 在另一个终端创建 session；服务 URL 和 credential 使用既有 session CLI 配置。
uv run loom session create '读取 https://www.python.org/about/ 并总结，注明来源' \
  --task-spec examples/task-specs/research.yaml
```

编程任务可以继续不传 `--task-spec`，沿用 workspace 和 plan_mode。也可以使用 [coding.yaml](../examples/task-specs/coding.yaml)；该例的 `project` 目录是仓库根目录。目录 URI 相对配置文件解析；明确的插件配置中资源由 `session_environment.resources` 定义，`--workspace` 不会覆盖它。

## 内置插件

| 配置位置 | plugin / collection | 行为 |
| --- | --- | --- |
| `session_environment` | `session` | 可选目录资源，重连验证，共享读或独占写占用 |
| `context` | `bounded_context` | 字符窗口预算、工具交换配对、原文 artifact 与摘录摘要 |
| `context` | `research_context` | 增加来源记录和区分证据/推断的指令 |
| `execution_runtime` | `native_os` | 本机工具入口、operation 去重、结果查询、取消和状态恢复 |
| `tools.collections` | `filesystem`, `shell` | 现有文件和命令工具；需要目录资源 |
| `tools.collections` | `task_control` | 最终 `finish` 报告 |
| `tools.collections` | `web_research` | `fetch_url` 解压 gzip/deflate 并按响应字符集解码，原始响应最多保留 100 KB；模型优先接收可读正文页，HTML 原文保存在 source artifact |
| `tools.collections` | `document_outputs` | `create_report` 产生 Markdown 报告和来源引用 |
| `workflow` | `legacy_planning` | 兼容 `off / auto / force` planning |
| `workflow` | `dynamic` | 顺序节点、依赖检查、受约束修订、完成节点与 plan 投影 |

没有目录资源时默认只提供任务控制工具，不会隐式授予文件或 shell 工具。只读目录使用 `access: read`，默认声明 `shared_read` 并移除写工具和 shell；可写目录必须独占。

上下文参数为 `max_window_chars`、`max_history_steps`、`reserve_chars`、`recent_exchanges`、`summary_chars`。工具可见预算为 `tools.budget` 下的 `max_tools`、`max_tool_schema_tokens`、`max_composed_tools`、`max_ephemeral_tools`。必须保留的控制工具优先；预算无法容纳时明确报错。摘要是确定性摘录，完整移出内容仍可通过 `read_artifact` 读取。

配置 `outputs: [{kind: report, format: markdown, require_evidence_refs: true}]` 后，没有证据引用的最终答案不能完成任务。`fetch_url` 的 URL 和 `create_report` 的 sources 作为引用记录；这项检查保证引用存在，不判断来源内容是否证明每项结论。最终报告以 `{report, sources}` 保存为 artifact。持久 session 的 `output_artifacts` 可用 `loom session artifact SESSION_ID DIGEST --output report.json` 导出；一次性执行的结果和 trace 引用携带 `local_path`，指向本次临时 artifact 目录。

`research.yaml` 还设置 `require_verified_sources: true`：必须有成功来源工具返回的 source artifact，手写 sources 或模型记忆不满足此条件。没有目录资源的显式任务若包含“读取/抓取/访问”等指令和 HTTP URL，会要求成功读取这些 URL；没有来源工具时在装配阶段拒绝执行。重定向保留请求 URL 和最终 URL，来源证据随 checkpoint 保存，已提交的抓取结果在恢复时重用。这些检查保证抓取发生，不替代对摘要准确性的校验。

HTTP 401/403 等状态保留 URL 和状态码，网络超时报告为 `HTTP_TIMEOUT`，解码失败报告为 `HTTP_DECODE_FAILED`，不会把已知 HTTP 失败当成执行状态未知。抓取失败不能满足来源契约或将任务标为完成。

`fetch_url` 的模型输出每页最多 6,000 字符，HTML 的 `content` 不与可读 `text` 重复塞入上下文。`truncated` 表示实际响应超过抓取字节上限，`page.has_more` 表示 artifact 中还有已保存的正文；两者含义不同。后续内容按 `read_more` 参数调用 `read_artifact` 获取，不能通过重复抓取同一 URL 恢复被压缩的上下文。

`read_artifact` 接受 `digest`、`view`（默认 `auto`，优先读取正文；`json` 读取原始 artifact）、字符位置 `offset` 和页长 `limit`（1–6,000）。返回 `content`、`total_chars`、`has_more`、`next_offset` 和可直接使用的 `read_more` 参数。需要完整 HTML 时选择 `view: json` 并依次读取所有页。上下文压缩产生的 artifact 明确标为 conversation archive，避免将聊天归档误当成网页来源。

## 动态 workflow

没有初始 nodes 时创建一个 LLM 根节点。也可以配置确定性工具节点和后续 LLM 节点：

```yaml
tools:
  collections: [web_research, document_outputs, task_control]
workflow:
  plugin: dynamic
  nodes:
    - id: source
      objective: 读取资料
      executor_kind: tool
      executor_config:
        tool_id: fetch_url
        input:
          url: https://www.python.org/about/
    - id: summary
      objective: 根据资料输出有来源的报告
      dependencies: [source]
      executor_kind: llm
```

模型调用 `revise_workflow` 时提交当前 `base_revision`、修订原因及完整 nodes。运行中/已完成节点必须原样保留；新增节点只能 pending。内核拒绝环、未知依赖、不可用能力/资源/执行入口、过期 revision 和超量修订。`complete_node` 要求非空 evidence；`finish` 在其他节点尚未完成时拒绝结束。节点允许设置 `executor_config.allowed_tools` 限制其普通工具，但仍保留 workflow 控制入口。

有 `finish` 工具时，最后一个节点不能通过 `complete_node` 的自述直接结束任务，应提交最终报告。最终节点的直接回答、确定性执行和 `finish` 都接受同一输出契约检查，缺少来源证据时节点保持未完成。

节点完成记录的 provenance 最多保留 2,000 字符摘录，全文通过 artifact_refs 保留；避免大网页被反复写入后续节点的固定提示词。输出契约在最终节点校验，准备节点可以先完成，再由后续节点抓取来源。

持久 session 保存模型消息、工具游标及所有插件状态。重启后复用已提交的工具结果，输入等待继续使用原 request。不能确认执行效果时进入恢复核验，不会因 resume 盲目再次执行。

## 注册扩展

结构接口见 [task_plugins.py](../src/loom/runtime/task_plugins.py)，执行参数与 binding 见 [execution_contracts.py](../src/loom/runtime/execution_contracts.py)。插件必须提供匹配的 manifest 和可序列化快照；恢复会校验 plugin/version/schema/config digest。registry 只接受宿主代码注册的 factory，不从任务文件导入模块。

以下例子添加一个只读工具集合：

```python
from loom.core import Observation, ToolRef, now_iso, ok
from loom.tasks.assembly import default_plugin_registry
from loom.tasks.request import TaskRequest
from loom.tasks.runner import run_generic_task
from loom.tools.collections import ToolCollection

def make_registry():
    registry = default_plugin_registry()

    async def lookup(value, options):
        return ok(Observation("answer", "lookup", {"answer": "42"}, now_iso()))

    def factory(config, request):
        return ToolCollection(
            "catalog", (ToolRef("lookup", "Lookup a fact"),),
            {"lookup": lookup}, {"lookup": "read_only"},
        )

    registry.register("tools", "catalog", factory)
    return registry

async def run(provider):
    request = TaskRequest("Lookup a fact", task_spec={
        "tools": {"collections": ["catalog", "task_control"]},
        "workflow": {"plugin": "dynamic"},
    })
    return await run_generic_task(request, provider=provider, plugin_registry=make_registry())
```

自定义 binding 可以声明 `required_capabilities`、`resource_refs`、`effect_kind`、`supports_cancel`、`artifact_kind` 和 `evidence_fields`；未确认取消的实现不能声明可确认取消。持久服务用 `LoomService(..., plugin_registry_factory=make_registry)` 安装扩展，factory 必须是 worker 可导入的顶层函数，并在重启时继续提供相同版本。

## 当前边界

本阶段支持 native OS，不提供 sandbox 隔离。`docker`、`vm` 等未注册后端会拒绝装配，不会回退到本机。浏览器/数据库连接需宿主注册，内置调研工具只读取给定 URL，不提供搜索。workflow 顺序执行；注册的 child loop 目前只支持一次性执行，持久 child-loop continuation 和远端执行重附着留待后续实现。
