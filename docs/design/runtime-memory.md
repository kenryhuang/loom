# Deep eval memory 在执行中的复用

## 目标

让 Web session 与独立 `loom task` 通过 TaskAssembly 复用 Deep eval 已接受的事实、失败经验和工作流。共享检索、范围约束、上下文注入与审计逻辑，不要求 CLI 连接服务，也不再调用模型做二次提炼。

## 数据与范围

Deep eval checkpoint 的 `memory.records` 是唯一数据源。只读取已经提交的 facts / lessons / workflows；contributions、待修复响应及 rejected_records 不进入执行上下文。允许不完整 evaluation 中已经接受的记录，明确展示 evaluation 状态。SQLite 使用只读连接，不创建数据库或修改 evaluation；删除 evaluation/session 后，下次检索不再返回其记录。

新 evaluation 保存创建时的 workspace 范围。旧 evaluation 使用所属 session 的只读 workspace；不推断跨项目适用性。有 workspace 时要求规范化路径完全一致，无 workspace 时要求 session ID 相同。模型不能指定其他 workspace、数据库或 session 来扩大范围。第一期不做跨项目通用经验推荐，不自动关联知识库。

## 检索

独立模块 `loom.memory` 负责只读数据源、确定性排序和运行时适配。查询在同一范围内匹配标题、摘要、适用条件及任务描述；短续答同时保留当前中断轮的前序任务描述；支持英文词与中文二元词。相同 session 优先，相关度、验证状态、时间和稳定 ID 决定排序。重复内容去重，冲突内容保留，不能自动将最新记录视为真相。

每次最多读取 500 条最近候选记录，最多返回 6 条，摘要总长度不超过 6000 字符。返回截断标记；无关键词命中时不注入无关记录。全文按页读取，单页最多 6000 字符，始终返回来源和分页信息。限制是确定性资源边界，不通过反复模型校验修复。

## 执行集成

服务在 worker descriptor 中传入受信任的 memory 数据库路径。独立 CLI 使用 `--memory-store /path/to/service-data` 显式选择本地来源；未指定时不检索。TaskAssembly 根据实际绑定的 workspace 创建作用域。

每次 worker 启动后的第一个新模型请求边界和目标变化时，检索少量摘要，写入 context metadata，并以用户级参考数据进入模型上下文。普通后续 step 复用这份有界摘要，不重复扫描数据库。新增只读 `memory_search` 与 `memory_read` 工具，供模型按需查询和读取证据。工具属于 assembly 的辅助工具，不改变已有插件配置与 checkpoint manifest。同一范围没有可用记录时不暴露额外工具；有工具预算时 memory 工具可被裁剪，不挤占必需执行工具。

恢复已有 LLM/tool checkpoint 时，先完成保存的协议交换；在下一个 `before_llm` 请求边界更新 memory，避免重写已发出的请求或工具决策。因此删除或新增记录会在下次检索生效，已经保存的 prompt/trace 不回写删除。

## 语义与证据

所有 memory 都是历史参考数据，不能修改任务约束，不能充当当前环境探测或验收通过的证明。保留 observed/inferred、observed/proposed、confidence、scope、revalidate_when/preconditions。环境、配置、能力使用前必须重新确认；proposed 工作流仅是待验证建议。摘要、全文和证据引用保留 session/run/evaluation/record ID、source digest、评估创建时间、内容摘要校验值。工作流不会自动改写实际 workflow 插件。再次 Deep eval 时，memory_search/read 输出标为 historical_reference，不能仅凭检索成功再提炼为 observed 事实。

## 可观测性与错误

`memory.retrieved` trace 记录查询、匹配来源、内容摘要校验值、截断状态及本次运行 ID，表示“提供给模型的参考”，不宣称模型已采用或带来收益。按需检索/读取通过已有 tool trace 留下返回内容。数据源不可用、格式异常分别以 `memory.store_unavailable`、`memory.record_invalid` 明确报告；初始检索失败记录 `memory.unavailable`，主任务仍可执行。工具失败返回结构化错误，不静默伪装为空结果。

## 验证与边界

测试覆盖范围隔离、中文检索、未接受记录排除、不完整 evaluation、去重与预算、删除失效、来源保留、数据库异常、CLI 参数、真实 TaskAssembly loop 的提示词与 trace，以及新旧 checkpoint 恢复路径。使用确定性假 provider 跑端到端 smoke，避免付费模型的不稳定输出。

第一期不做向量检索、自动推广全局经验、自动修改源记录或“使用效果”因果判断。此后可以用 trace 中的来源 ID 关联下次 Deep eval，评估 memory 的实际帮助。

## 实现与验证结果（2026-10-24）

- `src/loom/memory/store.py`：只读数据源、实际 workspace 绑定、确定性检索、去重和分页。
- `src/loom/memory/runtime.py`：上下文适配、只读工具和 trace 事件。
- TaskAssembly、ManagedStep、service worker descriptor 与 CLI 已接通；旧 plugin checkpoint 不需要迁移。
- 运行 `pytest tests/memory tests/llm/test_managed_step.py tests/tasks/test_task_cli.py tests/tasks/test_task_runner.py tests/service/test_semantic.py -q`：100 passed。
- 运行 `pytest tests/evaluation/test_trace_memory.py -q`：24 passed。
- CLI smoke 经过参数解析、TaskAssembly、假 provider、真实工具检索/分页、完成与 JSONL trace；session smoke 使用真实 worker 进程，覆盖 legacy/dynamic 与暂停后新 memory 的加载。没有请求付费模型。
- 现有 Solaris 数据只读检索返回 6 条记录、无格式问题，约 0.1 秒；这是检索连通性验证，不是经验质量或任务成功率评测。
- 扩展回归中的三项已有失败在独立导出的原始 HEAD 上同样复现：`test_long_history_has_a_bounded_prompt_and_keeps_the_current_request`、`test_large_research_result_compacts_and_retains_session_artifact`、`test_service_reads_source_pages_to_the_end_without_refetching`，涉及原有小窗口压缩及 source 分页；本次未修改这些行为。
