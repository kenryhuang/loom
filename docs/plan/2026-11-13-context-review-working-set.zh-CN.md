# 审查任务的 Context 工作集改造计划

状态：已实现可显式启用的首版，并完成确定性回归；真实模型效果对比尚未执行。范围：在现有 context 插件、artifact、checkpoint、工具调用和 acceptance 边界内解决重复读取与审查进度丢失。实现与启用说明见 [验证记录](../review/2026-11-13-context-review-working-set.zh-CN.md)。

## 1. 结论与现状

将 `bounded_context` 从“维护一段有界聊天记录”扩展为“维护有界消息窗口及可恢复的任务工作集”。审查任务使用工作集保存来源索引、阶段性发现和待解决问题；聊天记录只是这些状态的模型视图。

不新增调度器、审查工作流、长期记忆库或数据库表。Context 不决定任务验收通过，不自行修改 workflow 计划，也不执行文件工具。默认不开启新策略，先在显式配置的只读审查任务上验证。

代码依据：

- `contexts/bounded.py`：compact 保存完整 conversation artifact，但摘要由消息摘录拼接后取最后 `summary_chars` 字符，默认 2000；默认保留两个近期 exchange。它不是结构化审查状态。
- `tasks/acceptance_evidence.py`：已经保存工具调用索引及 artifact，并按来源有效性选择验收证据。应复用原始记录，不能再建一份独立的证据事实库。
- `tasks/tools.py`：read_file 已返回完整文件摘要、返回片段摘要、byte_range 与 truncated，可直接支持来源版本和覆盖范围。
- `runtime/task_plugins.py`：ContextManager 已有 project / compact / ingest / snapshot / restore。
- `tasks/assembly.py`：ingest 在外层 step 返回后才执行。仅实现这个方法无法覆盖一个 step 内的长时间 ReAct 循环。
- `llm/managed_step.py`：每次模型调用前已有 compact 安全点及 checkpoint；工具结果已有 journal 和 replay 路径。

案例 `sess_30b2cba036f04143a3115b3320cea2a5` 实际执行约 60 分钟，读取 41 个不同文件共 184 次，列举 29 个不同目录共 107 次，发生 7 次 compact。工具次数校验已经通过，最终验收被总时间预算中断。改造要解决的是证据利用与审查连续性，不能把该案例误诊为原始证据已经被删除。

## 2. 工作集的数据组织

工作集归 context 插件管理，由 snapshot/restore 持久化。完整状态可保存为现有 artifact，checkpoint 保存版本、artifact 引用、消费游标及有界增量。外层 ingest 只向 Context.state.scratch 投影摘要与引用，不复制一份全量可变状态，也不自动晋升为长期 KnowledgeLayer facts。

| 内容 | 记录字段 | 谁负责真实性 |
| --- | --- | --- |
| 来源索引 source | evidence_id、工具调用 ID、规范路径、版本摘要、实际读取范围、truncated、artifact 引用、结果状态 | 宿主从已持久化工具结果提取 |
| 审查发现 finding | 稳定 ID、业务问题/criterion 引用、结论、证据引用及定位、推断/观察标记、限制、被替代版本 | 模型提交；宿主验证结构和引用，语义仍须验收 |
| 待办问题 question | 稳定 ID、需要查明什么、已有 findings、缺失来源/片段、当前状态、下一步 | 模型维护；不覆盖 workflow 的执行计划 |
| 消费进度 | 哪些来源已形成发现、明确无关、仍待分析，重复读取计数、最近新增证据/发现修订 | 宿主统计，模型解释内容用途 |

必须区分三个事实：“工具执行成功”“模型已形成分析”“业务要求已被证据充分支持”。来源已读不能自动完成问题，finding 已保存也不能自动通过 acceptance。

来源身份使用 workspace/资源边界、规范路径、内容版本与读取范围，不只使用文件名。目录列表单独记录分页覆盖，列出文件不表示读过文件。

文件内容变更时，保留旧来源与旧发现的历史关系，标记其当前有效性待复核；只复核受影响的发现。goal/criterion 修订后保留来源，但重新确认业务问题映射。跨任务不隐式复用工作集。

## 3. Context 的每轮投影

每次模型请求前重建一个带预算的 `context_working_set` 命名消息，替换上一次同名投影，不不断追加。投影包括：

1. 当前业务问题及其与现有 workflow 活动项的映射。
2. 与该问题相关的持久化 findings，包括证据引用和限制。
3. 本轮待分析的新来源片段。
4. 已读来源的紧凑索引和明确的缺口。
5. 当前动作建议：分析本批材料、定向补证或汇总报告。

总体目标、身份与工具约束仍由原 project 生成；workflow 继续拥有计划和活动节点。工作集消息中的来源文本、模型笔记都是数据，不能提升成 system 指令。

为工作集索引、相关 findings、待分析片段、近期完整 exchange 和回复/schema 预留分别设置预算，全部计入现有 max_window_chars 与模型容量约束。完整工作集不常驻 prompt；超出预算时按当前问题选择，提供省略计数和可读取 artifact。不能把“全量来源索引永远 pinned”变成新的膨胀点。

compact 后从工作集重建该消息，不能再把它当普通历史截取尾部。完整工具 exchange 的 ID、调用参数和结果对应关系仍需保持；可以对结果原文做可追溯投影，不能制造孤立 tool message。

## 4. 行为循环：获取、消化、补证、汇总

第一版不增加隐藏的摘要模型调用。由当前 solver 在正常工具循环中形成审查笔记，调用开销和耗时继续计入任务现有预算。

增加一个可选的 context 控制工具，例如 `record_context_notes`。它通过现有工具注册、授权、journal、事件和 checkpoint 路径接入，assembly 只负责转发到 context 的工作集更新方法。不开启策略时不暴露该工具；只读任务允许写 session 内部状态，不写被审查仓库。

该工具按稳定 ID 和 base_revision 增量 upsert findings/questions，显式引用 evidence_id 与定位。省略旧条目不删除；修订保留 provenance。可记录“未确定”“此材料与问题无关”，不能为了释放上下文强迫模型作出结论。

行为顺序：

1. **获取**：原始工具结果先保存为 artifact/receipt；context 登记来源，再把有界内容交给模型。超过投影上限的部分必须显式标为未展示，不能假定模型已读。
2. **消化**：待分析材料达到批次预算或准备切换问题时，投影要求先提交 findings/unknowns/limitations。批次按材料量衡量，不简单按读了几个文件衡量。
3. **替换**：确认笔记和引用已保存后，将较旧原文换成 findings 与来源索引。未形成分析的材料保留 pending 状态；即使紧急 compact 移除原文，也必须保留其待分析身份和精确取回入口。
4. **补证**：从明确问题选择缺失片段。恢复旧内容优先读取 artifact；判断当前文件状态或获取此前没有读取的范围时才重新读取 workspace。
5. **汇总**：依据已存 findings 编写最终报告；未解决问题带边界说明进入报告，仍由 acceptance 判断是否满足任务要求。

首版采用明确的上下文引导与可观测反馈，不因为达到读取次数阈值而硬禁用文件工具。否则可能把正常交叉核对变成死锁。若连续若干轮既没有新增来源覆盖、也没有有效笔记进展，则通过已有预算/暂停机制报告“重复采集且未消化”，不得伪造完成或无限自动重试。具体阈值通过案例实验配置，不作为新的隐式任务预算。

## 5. 最小接口改动

在现有 ContextManager 协议外提供可选能力，旧插件缺省不执行：

```python
observe_tool(receipt)                  # 消费宿主确认的来源，按稳定调用 ID 幂等
update_notes(change, evidence_lookup)  # 工具提交；验证引用、版本和 revision
prepare_window(messages, budget, focus) # 每次 LLM 请求前投影工作集，不调用 LLM
```

名称在实现时统一；关键是语义边界：

- `observe_tool` 在原始 artifact/receipt 已持久化之后执行，且在投影缩减之前保有完整引用。不能在原始证据尚未写入时只保存摘录。
- 工作集更新纳入下一安全点 checkpoint。崩溃发生在“结果已记账、context 未更新”之间时，用同一 receipt 重放更新，不重新运行工具；重复通知不得生成重复来源或 findings。
- `prepare_window` 和 compact 在同一个模型请求安全点协作，不让刷新消息覆盖当前 exchange。
- 外层 `ingest` 用于把稳定引用投影回 Context，不能成为唯一摄入入口。
- `snapshot/restore` 包含工作集游标、revision、pending 来源与笔记引用，恢复后无需依赖旧 prompt 重建事实。

接入路径包括 ManagedStep、普通 `llm/api.py` 执行循环、ConversationContext 包装器和 DAG lane。ConversationContext 必须在 workflow 合并消息后刷新工作集，避免复用 main_messages 时丢失新投影。DAG lane 使用独立工作集与明确的结果引用合并，不共享可变活动问题。第一阶段可仅启用主执行路径；未支持的组合必须显式拒绝新策略，不能静默退化为不保留 findings。

文件读取的当前接口只能读取文件前缀。已有 artifact 能取回“曾经读到的内容”，不能取回从未读取的文件尾部。定向补证阶段应向 read_file 增加兼容的范围参数，默认行为不变，沿用 byte_range 和 hash 元数据；分页单位、UTF-8 边界及返回片段摘要必须有明确定义，不能把 artifact 字符 offset 当文件字节 offset。

## 6. 与验收的关系

验收仍使用现有 acceptance_calls、artifact、版本检查与预算。工作集只增加“criterion → finding → evidence_id → 来源范围”的选材线索。

- 模型 finding 是待核验主张，不能替代原始工具证据，也不能自行设定 passed。
- 验收可按映射获取相关原文，同时保留独立检查完整目标、遗漏与反证的能力。
- 某项支持不足时先查询已有证据和被省略片段；仅当来源不存在、范围不足或已过期时才反馈需要重新采集。
- 对 evidence unavailable / stale / truncated / not yet analyzed 使用不同状态，避免统一反馈“请重新读取文件”。
- 保留原有验收契约；将“至少读取 N 次”改成覆盖度指标属于后续独立调整，不在本次顺带更改。

## 7. 实施顺序

| 阶段 | 交付 | 主要文件 |
| --- | --- | --- |
| P1：来源与恢复 | 工作集数据结构、receipt 适配、幂等摄入、来源索引投影、checkpoint 恢复 | 新增 contexts/working_set.py；contexts/bounded.py；tasks/assembly.py；runtime/task_plugins.py |
| P2：持久化分析 | record_context_notes、引用校验、pending/consumed 状态、每轮投影与批次消化 | context 工具适配；llm/managed_step.py；llm/api.py；contexts/conversation.py |
| P3：定向取证与验收 | read_file 范围读取、来源失效、criterion 映射选材、细分证据缺口 | tasks/tools.py 及其 schema；tasks/acceptance_evidence.py；验收上下文构建 |
| P4：兼容与启用 | DAG lane 隔离、旧 checkpoint 兼容、受控案例对比、显式配置启用 | tasks/dag_lanes.py；插件配置与恢复；测试与诊断 |

P1 只能防止忘记“读过”，P2 才能防止忘记“得出了什么”；两者完成前不能宣称已解决问题。各阶段提交独立可检查的改动，先不自动重跑真实历史任务。

配置仍放在 context spec 中，例如新增可选 working_set 配置。旧的整数配置校验需按字段扩展；缺省保持原行为。当前插件恢复严格匹配 config_digest 与 schema_version，因此不得往旧 spec 自动注入新默认字段。旧 snapshot 缺工作集时按关闭策略恢复；开启新策略需要显式新配置或受测迁移，不能仅修改 digest 绕过兼容检查。

## 8. 验证与成功标准

必须先构建可重复的只读审查 fixture，包含跨文件接线、长文件截断、无关材料、冲突证据和多次强制 compact。用相同任务、预算、模型配置进行新旧行为对比，真实 session 轨迹只用于离线分析与测试材料提取，不能把历史模型输出当作新策略的实际表现。

确定性检查：

- 连续 compact 后，已读路径、范围、版本、findings、unknowns、pending 来源及引用仍可恢复。
- 保存一条业务发现后恢复进程，下一模型请求仍能看到相关发现，且没有自动重读已完成工具。
- receipt 重放、重复笔记提交、修订冲突、崩溃窗口不造成重复证据和状态丢失。
- 来源未读取、引用越界、旧版本冲突、目录仅列出文件等情况不能被记录为已证实内容。
- 超大工作集只投影有界部分，所有消息和 tool schema 总体满足现有上下文限制，工具 exchange 完整。
- 默认关闭时维持现有行为；主循环、普通运行、ConversationContext、DAG 路径分别验证或明确禁用组合。
- 验收引用原始证据，不能因模型笔记声称完整就自动通过。

效果指标：相同版本与范围的重复读取率、pending 材料积压、compact 后恢复读取次数、每项发现对应的证据可核验率、总 tokens、耗时及报告覆盖质量。重复读取需区分有意复核与无进展重读，不能只以读得少判好坏。

验收目标：受控案例在多次 compact/恢复后不需要整轮重扫；报告不把已读且已有发现的来源说成未读；覆盖和准确性不低于基线，同时重复输入与无效采集显著下降。具体性能阈值在取得基线后固定，不能预先虚构收益数字。
