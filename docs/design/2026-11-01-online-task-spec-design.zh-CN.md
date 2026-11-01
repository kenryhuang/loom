# 在线 Task Spec 机制设计

日期：2026-11-01
状态：已与人对齐，待计划

## 意图与动机

当前 Agent 的工具/模式配置是静态的：role 里存一份固定 `task_spec`，派发时原样使用，`plan_mode` 写死 `"auto"`。两个真实的卡死案例暴露了问题：

- `sess_b5eca2ec…`：目标 token 预算耗尽。实际已被现有调度器自动扩容 + resume 救活（`schedule_resume_57605ed5…` 命令），但用户不知道是自动的。
- `sess_a87fd1cd…`：验收阶段需要 UI 视觉验证（solar/galaxy 模式切换截图），但任务没有绑定 browser collection → `acceptance.state="blocked"`，调度器每 15 分钟尝试恢复都落入"无法自动恢复"，无限空转等人工。

目标：把 Agent 配置从"静态固定"变成三层机制——初始化推荐、每任务动态绑定、阻塞自动恢复；恢复有次数上限，超限真正挂起等人工。

## 目标

1. **Agent 初始化推荐**：创建 Agent 时可调 recommend（LLM 从能力目录圈定 task_type、tool collections、context/workflow 插件、知识库、外部工具），结果回填创建表单，**人确认后才写入 role**。
2. **Spawn 动态绑定**：每个工作包派发时，按其任务描述动态绑定 task type / tool collections / plan mode / context 模式等（走 task assembly 校验）；recommend 失败时回退到 role 兜底盘。role 配置可事后随时手工改，下一次派发生效。
3. **阻塞按原因恢复**：预算问题 → 自动扩容 + 从暂停点 resume（已存在，不动）；验收阻塞 → 判断是否可补全工具，补全后经 `restart_task`（revise + restart）重新下发。
4. **恢复计数与挂起**：每次恢复尝试计数一次；超过 5 次真正挂起，调度不再尝试，等人工恢复；人工介入后计数清零。

## 非目标

- 每任务粒度的知识库选择（知识库保持 role 级绑定；init recommend 会选 KB，dispatch 动态绑定不动 KB）。
- 人工接管（taken_over）session 的自动恢复——现有安全前提全部保留。
- 恢复策略的开放式 LLM 决策（recommend 只从目录选配置，不做自由修复）。
- CLI/TUI 的新交互（现有命令已支持 restart_task/resume，不在本次范围）。

## 反目标（做到这些也算失败）

- 动态绑定覆盖 read_only 工作包的受限环境（受限覆盖优先）。
- recommend 失败阻塞派发（必须静默回退到兜底盘并记事件）。
- 恢复绕过现有安全前提（人工暂停不覆盖、未确认副作用不恢复、session 归属变更不碰、role 签名变化需复核）。
- 验收恢复通过解析英文 reason 字符串做判断（必须用结构化信号）。

## 约束

- 复用 `SessionSetup.recommend` 的目录构建与校验逻辑；LLM 调用单次、有界、带超时。
- 调度循环内的 LLM 调用只阻塞该 Agent 的调度锁（QueueScheduler 已有 per-agent lock），不能卡死整个 supervisor。
- session 的 task_spec 只能经 `restart_task` 命令修改（resume 不改配置）；restart 保留工作区文件，但新开 run 重新执行。
- `POST /v1/session-setup/recommend` 保持向后兼容。

## 现状事实（已在代码与数据库核实）

- `QueueScheduler._cycle()`（src/loom/autonomy/scheduling.py）每 tick 检测 blocked/failed/running/dispatched 工作包 → `recover()`。
- `recover()` 已有：安全前提检查链、预算分支（`execution_block` 解析 → `expand_objective` + `allocate` → resume）、恢复计数 `work.recovery.attempts`（仅成功提交 resume 时 +1）、上限 `scheduling_policy.max_recovery_attempts`（代码默认 3，范围 0–10）。
- 未知原因落入 `raise ServiceError("无法自动恢复：…")` → intervention，且**失败不计数**，每 tick 无限重试（a87f 从 01:11 起每 15 分钟空转）。
- 验收阻塞的结构化信号：`session["acceptance"]["state"] == "blocked"`，`session["acceptance"]["plan"]["unresolved_requirements"]` 为缺口描述列表。
- `restart_task`（src/loom/service/store.py）接受 `payload.task` 的 EDITABLE_TASK_FIELDS（含 task_spec），task 状态为 idle/paused/failed/completed 时可用；旧 run 记 `run.stopped("Restarted from scratch")`，任务重新排队。
- `SessionSetup.recommend`（src/loom/service/session_setup.py）：输入 objective+model，输出 task_type/tool_collections/plugins{context,workflow}/knowledge_base_ids/external_tools/rationale；模板 task_spec 内嵌 context.plugin 与 workflow.plugin。
- 派发入口 `Dispatcher.dispatch` / `task_payload`（src/loom/autonomy/dispatch.py）：session 复用兼容性检查目前比对 workspace/model/KB/外部工具/role_revision，**不比 task_spec**；read_only 工作包强制受限 collections。
- `validate_create`（src/loom/service/contracts.py）：plan_mode ∈ {auto, force, off}；task_spec 经 `normalize_task_spec` + `TaskAssembly` 校验。
- 存量 agent 的 role 已显式物化 `scheduling_policy`（如 max_recovery_attempts=3），改代码默认值不影响存量。

## 设计（已与人确认的部分）

### A. 抽出可复用的推荐核心

从 `session_setup.py` 抽出不依赖 web frontend 的核心：

```
recommend_setup(service, objective, *, model, scope, extra=None) -> proposal
```

- `scope ∈ {"session", "agent_init", "dispatch", "repair"}`，控制 prompt 侧重与返回字段。
- 返回在现有 schema 上**新增可选 `plan_mode`**（auto/force/off，仅 dispatch scope 使用；缺省按 "auto"）。
- `dispatch` scope：不选知识库与外部工具（role 级绑定），校验时强制为空。
- `repair` scope：`extra` 带 `unresolved_requirements` 与当前 collections；约束**只增不减**，且新增只允许无需额外绑定的 collection（browser/filesystem/shell/web_research/document_outputs/task_control；**不允许** knowledge/external_tools——它们需要 role 级绑定）；只产出 collections 合并结果，不切换 context/workflow 插件、不返回 plan_mode。
- `builtin_templates()` 从 web/frontend.py 移入本模块（web 层改为从这里导入，消除 autonomy→web 的反向依赖）。
- 现有 session-setup 端点内部改调该核心，行为不变。

### B. Agent 初始化推荐（需求 1）

- 新端点 `POST /v1/agents/setup/recommend`（在 autonomy dispatch 的 `parts[0]=aid` 之前作为特例处理）：输入 role 描述等，scope="agent_init"，返回推荐配置 + 各项理由。
- Web 创建表单加"智能推荐"按钮 → 调端点 → 回填表单 → 人确认后随 role 一起创建。
- role 新增**可选** `plan_mode` 字段（默认 "auto"），属兜底盘，可经 role revisions 修改。

### C. Spawn 动态绑定（需求 2）

**两阶段结构（关键约束）**：`Dispatcher.dispatch()` 与 `recover()` 目前全程持有**全局** `service.lock`（所有 session/agent 共用）。LLM 推荐必须在**锁外**完成，只持有 per-agent 调度锁；进入锁后重新校验状态再提交。bind() 闭包已有 generation 校验，过期绑定会在提交时被拒绝、下一 tick 重算。

1. 阶段一（锁外）：读取 agent/work 快照；read_only 工作包跳过，保留现有受限覆盖；否则调 `recommend_setup(scope="dispatch", objective=工作包文本, model=role.model)`，有界超时。
2. 成功：payload.task_spec = 选中模板 spec + collections 覆盖 + context/workflow 插件覆盖；payload.plan_mode = proposal.plan_mode；`work["binding"] = {source:"recommended", proposal摘要, at}`。
3. 任何失败（超时/502/校验失败）：回退 role.task_spec + role.plan_mode（缺省 "auto"），`work["binding"] = {source:"fallback", reason}`，记事件。**绑定函数本身不抛异常**。
4. 阶段二（锁内）：重校验 work 仍为 pending、agent generation 未变；KB / 外部工具始终取 role 的，动态绑定不动。
5. **session 复用兼容性检查增加**：候选 session 的 task_spec（collections 集合、context/workflow 插件，剔除 knowledge/external_tools 两个派生 collection）与 plan_mode 必须与本次绑定一致，否则不复用、新建 session。

### D. 阻塞恢复（需求 3）

- 预算分支：不动（含 acceptance 预算耗尽的 resume 续分配路径）。
- 新增验收阻塞分支，置于 `recover()` 的"无法自动恢复"抛出之前（**两阶段**：前提检查与决策在锁内，repair recommend 在锁外，提交前重新进锁校验）：
  1. 条件：`session["acceptance"]["state"] == "blocked"`（结构化判断，acceptance 在 session state 中是 dict）。
  2. read_only 工作包：拒绝修复（不破坏受限环境），按决策级失败计数并转人工。
  3. 锁外调 `recommend_setup(scope="repair", objective, model=session task.model, extra={unresolved_requirements, current_collections})`。
  4. 有新增：合并进 session 当前 task_spec（只加 collections）→ **先经 assembly 校验**（normalize_task_spec + TaskAssembly，与 validate_create 相同路径；校验不过则按决策级失败处理，不产生 intent）→ 提交 `restart_task`（`payload.task.task_spec` = 合并结果，附 reason），复用 `schedule_resume_*` intent 幂等机制（intent 增加 kind="restart" 与合并后 spec；restart_task 的 `_restart_configuration` 会再过一次 validate_create）。
  5. 无新增：抛不可自动恢复（决策级失败，计数）。
  6. restart 后新 run 重新做验收规划（此时具备 browser 等能力）。
- restart 分支不受 `recover()` 中 checkpoint 前提限制（restart 不依赖 checkpoint 续跑）；其余安全前提（人工控制、副作用、归属、role 签名）全部保留。

### E. 恢复计数与挂起（需求 4）

- **计数口径**：只有"做出恢复决策"才计数——成功生成恢复 intent（resume 或 restart）、原因无法识别、repair 无新增、扩容触及上限，这些都 +1 并**先持久化再抛出/提交**（修复现在失败不计数导致无限空转的问题）。安全前提不满足（worker 未退出、来源暂不可用、人工暂停、有未确认副作用等）**不计数**，保持现有的等待语义。
- 上限检查位置：安全前提全部通过之后、决策之前。当 `attempts >= max_recovery_attempts`：`work.blocked_reason = "recovery_exhausted"`（state 保持 blocked），记 `schedule.recovery.exhausted` 事件一次，抛 intervention。
- **挂起后的队列语义**：exhausted 包对 `_cycle` 的 blocked 扫描不可见（不再每 tick 重试），且不再阻塞同 agent 其他工作包的派发（dispatch 的单飞排除扫描同步排除它）；其余工作正常流转。
- 上限默认 3 改为 **5**（仅影响新建 agent；存量 role 已物化旧值，需要时经 role revision 调整）。
- **人工介入重置**：`SessionStore.submit` 中，对关联了工作包的 session 收到 `resume`/`restart_task` 且 command_id **不带 `schedule_resume` 前缀**（人工/UI 命令用 crypto.randomUUID，调度器 intent 用该前缀）时，将该工作包 `recovery` 重置为 `{attempts:0, pending:null}`。修订/替换工作合同会产生新工作包（旧的被 supersede），天然从零计数，无需额外钩子。

### F. 事件与审计

- `schedule.binding.recommended` / `schedule.binding.fallback`（含理由）。
- `schedule.recovery.requested` 扩展 `kind: "resume" | "restart"`。
- `schedule.recovery.exhausted`。
- 工作包持久化 `binding`（本次动态绑定来源与摘要）供界面展示。

### G. UI 变更（web/assets）

- Agent 创建表单："智能推荐"按钮 + 推荐结果回填（含各项理由展示）。
- Agent 编辑表单：补充 task_spec（collections）与 plan_mode 编辑（当前仅支持 KB/外部工具）。
- 工作包/调度视图：展示 recovery.attempts、recovery_exhausted 状态、binding 来源。

## 审查决议（builder check，2026-11-01）

1. **全局锁**：dispatch/recover 全程持有全局 service.lock，LLM 调用一律放锁外（两阶段），进锁后重校验——见 C/D。
2. **计数口径**：仅决策级结果计数，前提不满足不计数——见 E。
3. **read_only 修复**：拒绝，不破坏受限环境——见 D.2。
4. **挂起队列语义**：exhausted 包对恢复扫描与派发单飞检查都不可见，不拖累其余工作——见 E。
5. **重置钩子**：SessionStore.submit 按 command_id 前缀识别调度器 intent，人工 resume/restart 重置计数；合同 revise/replace 产生新包天然清零——见 E。
6. **repair 范围**：只增 tool collections（不含 knowledge/external_tools），不碰插件与 plan_mode——见 A/D。

## 我自行决定的细节（可否决）

- recommend 用模型：dispatch 用 role.model；repair 用 session task.model；agent_init 用服务默认模型。
- role.task_spec 保留在 `role_signature` 内：手工改兜底配置仍 bump role_revision，已固定的待派发包走现有 "role_changed; revise or replace work contract" 流程，新派发用新配置——正好符合"下一次调度生效"。
- 动态绑定不触碰知识库与外部工具（role 级）。
- 恢复计数挂在工作包上（与现状一致），不按 session 或 goal。

## 留给实现者的

- 具体函数签名与文件布局（推荐核心放 session_setup.py 还是新模块）。
- recommend prompt 的精确措辞（在现有 prompt 基础上按 scope 扩展）。
- restart 恢复分支与现有 pending intent 幂等机制的精确衔接（intent.run_id 校验对 restart 的适配）。
- UI 具体交互与排版。
- 测试组织（TDD：先写失败测试）。
