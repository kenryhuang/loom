# 规划中提问（await_input 工具）设计

日期：2026-11-16
状态：需求已确认，待实施计划

## 意图与背景

engine-2 规划器（`PlanPlanner`）目前是**单次结构化调用**：模型拿到完整 payload 一次性返回决策 JSON。模型需要人决策时只能用 `request_input` 动作**结束整个 run**；人回答后由**新 run** 携带累积答案重新规划——模型每轮都从头重读全部上下文，丢掉问到一半的思路。

目标：规划中途模型可以调用 `await_input` 工具**一次性问完所有问题**，人回答后**恢复同一段对话**继续规划。决策权在规划过程中回到人手里，而不是只能在两轮规划之间。

## 已确认的需求决策（用户）

- 只做 engine-2 规划器（legacy 引擎另行删除）。
- 同一时刻尽可能问所有问题：一次 `await_input` 携带问题列表，人批量作答。
- 回答后恢复同一段对话：模型的消息线程（含其 tool_call）持久化，答案作为 tool result 追加，从断点继续。

## 目标

1. 规划循环支持工具调用：模型可在任意规划轮次调用 `await_input(questions=[...])`。
2. 调用即挂起：对话线程持久化为 run 的 artifact；释放 `planner_lease`；objective 状态置 `awaiting_input`，问题在 Planning 块展示（复用归位后的答题区，扩展为批量）。
3. 作答后恢复原对话继续，直至模型产出通过现有校验的决策 JSON（schema/编译/纠错/预算记账全部沿用）。
4. 服务重启不丢挂起状态：重启后问题仍在，作答仍可恢复对话。

## 非目标

- 不给规划器加除 await_input 外的其他工具（读文件等仍靠 payload 预置证据）。
- 不改执行侧（session worker）的 `request_input` 机制。
- 不做挂起超时自动取消（初版）。

## 反目标

- 问答案例破坏现有单次规划路径：模型不提问时行为与今天完全一致。
- 挂起期间阻塞其他 objective 的规划（planner_lease 必须释放）。
- 重复提问已回答的问题（答案同时写入 `state["answers"]`，后续 run 可见）。
- 对话线程丢失导致"恢复"退化成重新规划（那是现状，不是本功能）。

## 设计要点

### 规划循环改造（`plan_planner.py`）

- 引入工具循环：每轮发送消息 + 工具定义（`await_input`）；模型返回 tool_calls 且含 await_input → 进入挂起流程；模型返回无 tool_calls 的内容 → 按现有 `request_structured` 协议解析为决策（含 invalid_json 纠错）。
- 决策提交方式保持"最终消息即决策 JSON"，不引入 submit_plan 工具——最大限度复用现有校验/纠错/编译路径。
- LLM 层的 tool_calls 基建（`llm/api.py` 的 `LlmToolCall`/`tool_choice`）已存在，执行侧在用；规划侧的 `send`/`planning_response` 需要支持工具透传。

### 挂起（`advance` 内）

- run 记录新增：`conversation_ref`（消息线程 artifact）、`pending_questions`（问题列表）。
- 对话线程、attempt/correction 计数随 run 持久化。
- 释放 `planner_lease`；objective state → `awaiting_input`，`questions` 列表存入 planning state（兼容现有单问题 `question` 字段的渲染）。
- 事件：`plan.input.requested`（携带 run_id 与问题数）。

### 恢复（`plan_commands` answer 路径）

- answer 命令携带 `{question_id → answer}` 映射（批量）；写入 `state["answers"]`（保持既有语义），并把答案作为 await_input 的 tool result 追加到持久化的对话线程。
- planning state 记录 `resume_run_id`；`advance()` 检测到它时不重新计算 rid/不重新起 run，而是加载该 run 的对话继续工具循环（lease 重新获取，沿用现有 generation/revision 校验）。
- 校验失败（期间输入已变化，如 source 刷新）→ 该 run 置 superseded，按现有"输入已变"语义走新 run——不强行恢复。

### 问题与答案模型

- 问题：`{id, text, options[]?}`；答案始终允许自由文本（点选 option 即填充）。
- 一次 await_input 最多 N=5 个问题（schema maxItems，防滥用）；同一 run 允许多次 await_input（回答可能引出新问题），但总次数计入预算。
- 既有 `request_input` 终止动作保留作为兼容出口（模型选择结束规划等人答）。

### UI（`agent-roadmap.mjs`，建筑在工作流 1 之上）

- Planning 块答题区扩展为批量：问题列表 + 每题 options + 一个提交按钮。
- 规划 run 卡片展示"提问 → 回答"事件对（事件流里已有 protocol 事件，新增 input.requested/answered）。
- awaiting_input 自动展开 Planning 块（工作流 1 已实现）。

### 服务重启

- supervisor 启动恢复：`agent_runs` 中挂起态（awaiting_input 的 planning run）**不**标记 recoverable/superseded，原样保留；objective 状态已是 awaiting_input，等待作答。

## 约束

- 预算：恢复/提问轮次都计入同一 run 的预算记账（BudgetLedger category=stage_planning），不新设科目。
- 幂等：answer 命令带 command_id 与 expected_revision（沿用现有模式）；重复提交返回 replay。
- 并发：同一 objective 同时最多一个挂起的 await_input；lease 释放期间其他 objective 可正常规划。

## 验收标准

- 单测：await_input 调用→挂起→作答→同对话恢复→产出合法决策的完整链路；输入变化时 run superseded；lease 在挂起期间释放。
- UI 测试：批量问题渲染与提交；恢复后 run 卡片显示问答事件对。
- 现有 autonomy/web 测试套件全绿（除 2 个既有失败）。
- 真实冒烟：对一个 engine-2 agent 触发规划，模型提问（可用测试 hook 强制），回答后规划在同一 run 内完成。

## 留给实施者的判断

- 工具循环与 `request_structured` 纠错循环的具体编织方式（每轮独立纠错 vs 外层统一）。
- 对话 artifact 的裁剪策略（超长线程的截断点）。
- 问题 schema 的字段命名与 events 的 payload 形状。

## Builder 检查决议（2026-11-16）

1. **非答题路径退出 awaiting_input 时挂起 run 的处置**：按"对话连续性优先"分路径处理——
   - `pause`/`resume` 与服务重启：**保留**挂起的对话与问题（暂停是"稍后再答"，不是"输入已变"）；
   - `replan`/`revise`/stage cancel/role 变更：**supersede** 该 run 并丢弃对话（与既有"输入已变"语义一致），清理 `pending_questions`/`resume_run_id` 链接。
2. **批量作答允许部分回答**：提交时不要求每题必填；未答问题在 tool result 中显式标记 `skipped`，模型可据此自行决策或选择 wait——避免人为解锁规划而编造答案。全部跳过也允许（等价于"都你自己定"）。
3. Builder 核实确认的关键落点（无需再查）：run 增加挂起态值使 `supervisor.py:54` 的重启恢复查询不受影响；不支持工具的 provider 优雅降级为现有无工具路径（发 protocol 事件便于观测）；resume 校验沿用 generation/revision/输入指纹；测试用 `tests/autonomy/test_unified_planning.py:255` 的脚本化 provider 模式。
