# Context 审查工作集实现与验证

对应计划：[Context 审查工作集改造计划](../plan/2026-11-13-context-review-working-set.zh-CN.md)。

## 已实现

- `contexts/working_set.py` 保存来源索引、模型 findings/questions、来源消费状态、修订历史和重复读取统计。原文复用 acceptance 的 artifact；笔记是待核验主张，不能设置验收结论。
- `bounded_context` / `research_context` 每次 compact 调用前刷新 `context_working_set`，无论本轮是否真正触发压缩；窗口只包含预算内的投影。已分析材料退为引用，待分析材料保留状态与取回入口。
- `record_context_notes` 使用稳定 change_id、base_revision、增量更新和来源引用校验。工具返回的 journal receipt 保留工作集 snapshot，因此可以恢复“工具已经落盘、checkpoint 尚未更新”的窗口。
- 工作集快照存入既有 artifact，checkpoint 保存 revision 和引用。未变化时复用同一快照。外层 Context.state.scratch 仅保存统计和引用，不复制全量工作集。
- 普通 runner、JSON action、ManagedStep、ConversationContext 以及只读 DAG barrier/pipeline 路径接入。独立 lane 使用各自工作集；完成后显式合并并给笔记 ID 加 lane namespace。pipeline 主线复用主工作集，避免覆盖已交付的分支结果。
- `read_file` 增加 `offset_bytes` 和返回 `next_offset_bytes`，返回范围按 UTF-8 字符边界结束；完整文件 SHA、实际返回片段 SHA 和 byte_range 随结果记录。artifact 分页仍按字符计数。
- 验收根据 criterion→finding→evidence_id 优先选择已获得的原始证据，并保留原有独立完整目标检查、来源有效性检查、次数与预算规则。笔记不作为原文的替代品。

Context 不接管 workflow，不创建新的数据库表，也没有增加隐藏的摘要模型调用。笔记仍消耗正常 solver 工具轮次与现有任务预算。

## 启用

新任务的 task spec 可显式配置：

```json
{
  "context": {
    "plugin": "bounded_context",
    "working_set": {
      "max_projection_chars": 12000,
      "batch_chars": 24000,
      "preview_chars": 3000,
      "max_stalled_reads": 8
    }
  }
}
```

`working_set: {}` 使用默认参数；前三项默认值如上，`max_stalled_reads` 默认 **0（关闭额外停滞暂停）**。示例中的 8 是显式启用的诊断策略：连续相同来源版本/范围读取没有新覆盖或有效笔记进展时，协作暂停并保留状态。正常交叉核对不被硬禁止，明确 resume 后可以继续。

不配置 `working_set` 时维持旧策略。旧 context spec/checkpoint 不注入新字段，不绕过 config_digest/schema_version 校验；本次没有自动迁移或重跑现有 session，也没有重启服务。

若任务配置了工具 allowlist，必须允许 `record_context_notes` 和 `read_artifact`，否则启动时报告配置错误。独立分支仅获得自身工作集；审查策略与 `adaptive_dag.parallel.isolated_writes=true` 的组合明确拒绝，避免把隔离工作区证据与主工作区混用。

## 记录笔记

模型从每轮 `context_working_set` 读取当前 revision 和 source 的 id、range、unit；先保存问题和发现，后续按相同 ID 修订。引用必须指向宿主记录的来源，范围不能超出实际取得的片段。

- `kind=observed/inferred` 要有证据；`kind=unknown` 可以没有证据，但不能宣称检查已通过。
- 问题状态 supported 是模型的覆盖判断，仍由 acceptance 独立核验。
- 发现支持哪些 criterion 由 `criterion_ids` 声明；错误映射不能改变实际验收标准。
- 文件变化时旧证据及关联发现保留历史并标记过期；goal 变化时保留来源，但旧映射显示需要复核。
- 来源状态 pending / analyzed / irrelevant 分别表示待读分析、已有笔记引用、模型明确说明不相关，均不等于验收通过。

## 验证范围

新增 `tests/tasks/test_context_working_set.py` 覆盖：

- 7 次强制压缩及 snapshot/restore 后发现、待读范围和原文 artifact 保留。
- 大索引的有界投影、预览不递归嵌套、完整工具 exchange 不被拆散。
- 笔记事务性校验、幂等重试、引用越界/伪造/单位不匹配及修订冲突。
- 文件变化、goal 修订、明确 lane 交付及冲突版本。
- 工具 receipt 与 checkpoint 之间的恢复，以及 ManagedStep 暂停后下一模型请求看到原笔记。
- 普通 runner 与 JSON action 的逐轮笔记投影；显式停滞策略返回协作暂停。
- DAG barrier/pipeline 的分支隔离、显式结果合并与主线复用。
- 验收从旧来源引用取回真实原文，不能仅因模型笔记声称完成就通过。
- UTF-8 范围读取、artifact 字符分页、默认关闭及 allowlist/隔离写分支配置约束。

广泛回归中发现 `test_auto_continue_react_crosses_boundary_before_task_execution` 期望 `tool_choice="auto"`，实际为 `required`。已将 **HEAD 原始代码和测试导出到独立临时目录** 重跑同一用例，得到相同失败；本次不修改该既有工作流行为或测试断言。

最终验证：

- `python -m pytest -q tests/tasks tests/llm/test_managed_step.py tests/runtime/test_checkpoints.py tests/service/test_plugin_sessions.py tests/service/test_session_setup.py -k 'not test_auto_continue_react_crosses_boundary_before_task_execution'`：500 passed，1 deselected。
- 最后补充并复核冲突版本及读取版本绑定后，`python -m pytest -q tests/tasks/test_context_working_set.py tests/tasks/test_acceptance_tool_evidence.py`：38 passed。这与上面的回归有重叠，不能相加为测试总数。
- 本次修改的 Python 文件通过 Ruff；`git diff --check` 通过。

这些测试证明状态、边界、恢复和选材机制，不代表真实模型已达到某个 token/耗时下降比例。尚未对线上历史 session 或真实模型做效果 A/B；后续以相同模型、任务与预算比较重复读取率、报告覆盖质量和 token 用量。
