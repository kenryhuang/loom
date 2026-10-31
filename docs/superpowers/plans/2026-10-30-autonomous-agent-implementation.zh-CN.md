# 自主 Agent 分阶段实施计划

日期：2026-10-30。状态：P0～P3 代码及自动化闭环已实现，验收证据见第 10 节；手工浏览器视觉验收待可用浏览器连接。P4/P5 尚未执行。

设计依据：[自主 Agent：数字分身与持续工作设计](../specs/2026-10-25-autonomous-agent-design.zh-CN.md)，重点对应第 14～22 节。方法版本与验证复用[跨任务 Skill 设计](../specs/2026-10-26-cross-task-skill-induction-design.zh-CN.md)及[当前实现说明](../../cross-task-skills.zh-CN.md)。

目标：用户配置包含长期目标、工作目录和方法的 Role 后，Agent 能周期编排子任务、复用相关 Session，在每个执行 run 结束后自动 Deep eval，并将适用 memory、人工纠正和方法指导用于后续规划与执行。

## 1. 执行约定与里程碑

本计划基于 `193d52b` 和编写时工作区。工作区已有 Session、Deep eval、Skill 和前端的未提交改动；执行时先核对它们，不覆盖、重建或假定这些改动已经合入。新增文件路径是拟议落点；既有模块路径已核对，若执行前发生变化，先更新对应任务的文件清单。

路径约定：任务文件清单中的 `autonomy/`、`service/`、`memory/`、`skills/`、`knowledge/`、`llm/`、`tasks/`、`workflows/`、`session_environments/`、`web/` 均相对 `src/loom/`；`tests/`、`docs/` 和命令中的路径相对仓库根目录。

每个任务按「核对前置条件 → 建立关键行为/故障复现 → 实现 → 运行相关验证 → 记录证据」执行。一个任务可形成一个独立评审变更；通过后填写实际变更引用、测试结果及残余限制，再勾选。阶段内部可增量集成，但未达到里程碑门槛前不宣称完整功能可用。

| 工程阶段 | 内容 | 依赖 | 对应产品里程碑 |
| --- | --- | --- | --- |
| P0 | 基线、测试支架、后台服务生命周期 | 无 | M1 的前置工作 |
| P1 | 领域合同、存储、预算、基础接口 | P0 | M1 的持久基础 |
| P2 | 周期编排、派发、Session 复用与控制 | P1 | M1 的执行链路 |
| P3 | 自动学习、人工指导、context 与页面闭环 | P2；部分模块依赖 P1 即可开始 | **M1：可验证的执行与学习闭环**，设计阶段一 |
| P4 | 目标自主迭代、偏好学习、知识刷新、方法反馈 | M1 | **M2：能持续学习的数字分身**，设计阶段二 |
| P5 | 多角色、外部事件、跨项目方法和执行能力扩展 | M2；执行能力扩展另有前置门槛 | **M3：更强的协作与演进**，设计阶段三 |

执行顺序：`P0.1 → P0.2 → P1.1～P1.4 → P2.1～P2.4 → P3.1～P3.5 → M1 验收 → P4.1～P4.4 → M2 验收 → P5.1～P5.5`。每项任务下列出更精确的依赖；这是依赖关系说明，不要求启动多个执行 agent。

M1 必须包含「Role → AgentRun → SessionRun → Deep eval → memory → 下一次执行 → 人工纠正生效」，不能将自动学习留到 M2。M1 先支持一个主要 Agent、一个 workspace、显式目标和配置的周期职责；多目标自主发现和推断偏好在 M2 完善。

## 2. 跨阶段约束与验证方式

- `agent_run_id` 表示编排轮次，现有执行 `run_id` 保持 SessionRun 含义。Session 复用不能混淆工作包合同、验收或预算。
- 所有 completed/failed/stopped 的 Agent 执行 run 都产生持久学习待办；预算不足延后，不抽样丢弃。暂停、等待输入和恢复状态不当作终态。
- 业务事务与 outbox、receipt、消费游标配套；外部副作用结果不确定时仍进入现有恢复流程。租约超时不证明旧进程已停止。
- 保留同 workspace、无 workspace 时同 Session 的 memory 范围。人工标注是覆盖层，不能修改原 trace；方法采用策略与验证状态分开。
- 模型与工具在事务外运行；规划、执行、验收、学习、索引和方法实验都要有调用前预算准入。无法计量的费用显示 unknown，不能按零处理。
- 首期沿用现有 native_os 和共享读/独占写规则。可写分支、不同 subagent 模型与非 barrier 并发到 P5 单独实现，M1/M2 不提前开放。

自动验证优先采用临时 service directory、fake clock、可控 solver/evaluator provider 和可注入故障点，覆盖真实 SQLite、TaskAssembly、worker 与 HTTP 链路；不依赖付费模型稳定输出。模型判断质量另用明确标注的试用样本记录，不能拿假 provider 测试证明语义效果。

实施落点调整：原计划的小模块合并为 `autonomy/contracts.py`、`store.py`、`supervisor.py`、`dispatch.py`、`context.py` 和 `service/autonomy.py`；测试相应合并到 `test_foundation.py`、`test_recovery.py`、`test_context.py`、`test_learning.py`、`test_execution_flow.py` 与 `test_end_to_end.py`。下文文件清单保留规划时的逻辑划分，第 10 节给出实际落点与统一可运行命令。

任务中的命令从仓库根目录执行。`uv run pytest ...` 使用项目环境；首次准备前端依赖运行 `npm --prefix tests/web ci`，后续使用 `npm --prefix tests/web test`。尚未创建的测试文件在对应实现任务中新增后再运行。本次计划编写不执行这些实现测试。

## 3. P0：基线与后台服务支架

### P0.1 核对集成基线并建立可重复的测试环境

依赖：无。文件：新增 `tests/autonomy/{__init__,helpers}.py`；复用 `tests/service/fakes.py`、`tests/memory/helpers.py`、`tests/skills/helpers.py`；实施记录写回本文第 10 节。

- [x] 记录实际 HEAD、已有工作区改动及 Skill 可用接口；确认目标分支包含所需跨任务 Skill 版本能力，缺失时明确集成依赖，不另造方法库。
- [x] 运行一次相关基线：`uv run pytest -q tests/service tests/memory tests/skills tests/llm/test_managed_step.py` 和 `npm --prefix tests/web test`，记录原有失败及复现方式。
- [x] 建立可注入时间、规划返回、执行结果、评估窗口及故障位置的支架；使用独立临时目录，记录模型调用次数与实际工具副作用计数。
- [x] 固定演示 fixture：两个相近工作包、一次失败教训、一条人工长期指导、一个不兼容 workspace；后续阶段共用相同场景。

完成标准：无需外部服务即可确定性驱动成功、失败、恢复、时间推进和用户介入；已有失败不被当成新功能的成功证据或随手屏蔽。

### P0.2 统一后台组件生命周期

依赖：P0.1。新增 `src/loom/service/background.py`、`tests/service/test_background.py`；修改 `service/controller.py`、`service/api.py`；核对 `service/trajectory.py`、`service/semantic.py`、`service/skills.py` 的 start/close 行为。

- [x] 将 trajectory、semantic、skills 的组合和所有权提取到服务级后台组件；HTTP handler 只调用共享实例，避免创建两个 evaluation worker 池。
- [x] 明确初始化与关闭顺序、模型配置更新、懒加载边界和异常清理；避免 controller/HTTP/background 相互循环依赖。
- [x] 为后续 AgentSupervisor/LearningCoordinator 预留受控注册入口，后台启动不依赖浏览器访问 Analysis。
- [x] 验证无 HTTP 请求也能驱动后台任务、重复 start/close 无副作用、关闭时不丢已提交检查点；保留现有 API 行为。

验证：`uv run pytest -q tests/service/test_background.py tests/service/test_api.py tests/service/test_semantic.py tests/service/test_trajectory.py tests/skills/test_api.py`。

完成标准：一个 service directory 只有一组后台组件，组件可独立测试，HTTP 层没有隐含的学习启动副作用。

## 4. P1：领域合同、持久化与预算

### P1.1 定义 Role、长期目标和编排合同

依赖：P0.1。新增 `src/loom/autonomy/{__init__,contracts,role}.py`、`tests/autonomy/test_foundation.py`；核对现有 `service/contracts.py`。

- [x] 定义 AgentRoleRevision、LongTermObjective、AgentRun、Goal、WorkPackage、SessionRouteDecision、LearningCycle、GuidanceEvent 和 ContextSnapshot 的字段、状态与 schema version。
- [x] 实现 Role 原文与结构化结果的校验；明确字段标记 explicit/derived，source routes 指向真实绑定，目录规范化，缺失资源产生依赖缺口。
- [x] 固定状态迁移、Goal 依赖无环、不可变终态、当前任务与长期指导的 scope，以及 command ID/expected revision 校验。
- [x] 给出设计第 15 节 Role 样例的可解析 fixture 和结构化 API payload；不能用规划模型的自由文本直接作为 Host 状态更新。

验证：`uv run pytest -q tests/autonomy/test_foundation.py`，覆盖冲突 scope、无效绑定、依赖环、非法状态迁移和明确指导的来源。

完成标准：同一合同可被 planner、存储、API、worker descriptor 使用；不改变原 TaskSpec 的职责或允许任意未知字段。

### P1.2 存储迁移、事务、事件和 artifact 归属

依赖：P1.1。新增 `autonomy/{store,migrations,artifacts}.py`、`tests/autonomy/test_foundation.py`；修改 `service/store.py` 的初始化接入。

- [x] 按设计第 10.1/21.1 节增加版本化迁移，先落地 M1 需要的表、唯一约束与索引；迁移记录有版本，重复启动不重复改写。
- [x] AgentStore 复用 SessionStore 的事务入口；需要原子更新 Session 与 Agent 的操作显式接收同一 `db`，不得在事务中再开独立 SQLite 写事务。
- [x] 实现 Role/Goal revision、AgentRun 租约、dispatch/learning outbox、Session 任务槽和消费游标；所有命令支持同 ID 同输入重放、不同输入冲突。
- [x] 为无 Session 的编排输入/报告/context 增加 Agent artifact 所有权与读取校验，复用内容寻址存储，不伪造 Session ID。
- [x] 为迁移和事务提交前后加入故障测试，旧 Session 数据仍可读；新表缺失时只读 memory/CLI 保持兼容。

验证：`uv run pytest -q tests/autonomy/test_foundation.py tests/service/test_store.py tests/memory/test_store.py`。

完成标准：重启后合同、待办和事件可恢复，原 Session/trace/evaluation 正文未被复制成第二份事实源。

### P1.3 统一预算准入与用量结算

依赖：P1.2、P0.2。新增 `autonomy/budget.py`、`tests/autonomy/test_foundation.py tests/autonomy/test_recovery.py`；按调用链修改 `service/controller.py`、`service/semantic.py`、`llm/managed_step.py`、`tasks/assembly.py`，复用现有 usage 事实。

- [x] 定义 Agent 日额度、Goal 总额度、WorkPackage/学习子额度；以稳定 call ID 预留、结算、保留未知成本，按配置时区保存窗口 ID。
- [x] 接入 solver 的模型请求与验收、planner/evaluator 请求；内部重试和分支请求使用真实调用身份，不能仅在任务结束后统计。
- [x] 结算与恢复幂等；父级汇总和子请求不能重复收费。跨日未完成 reservation 不清零，为验收和学习留出配置额度。
- [x] 定义知识刷新与方法实验共用的 budget adapter，未接入路径禁止自动启动；价格未知使用 token/调用数/时间限制而非假现金精度。

验证：`uv run pytest -q tests/autonomy/test_foundation.py tests/autonomy/test_recovery.py tests/tasks/test_acceptance_budget.py tests/service/test_semantic.py tests/llm/test_managed_step.py`；覆盖调用前崩溃、响应丢失、重复结算和跨日恢复。

完成标准：自动调用无绕过预算的入口，预算不足形成可恢复阻塞；P2/P3 接入时仍须验证各自真实调用经过本适配器。

### P1.4 基础服务 API 与 Agent 控制

依赖：P1.1～P1.3。新增 `service/autonomy.py`、`tests/autonomy/test_api.py`；修改 `service/api.py`，新增 `web/assets/agents-api.mjs` 与 `tests/web/agents.test.mjs`。

- [x] 实现 Agent 创建/查询、Role revision、Goal 提交/修订、enable/pause/resume/archive、wake、事件游标和 artifact 读取；状态控制先持久化，再通知后台。
- [x] 复用现有认证和错误 envelope；校验 ID 归属、revision、body 上限与分页，不在 HTTP 线程调用模型。
- [x] 返回控制状态与活动状态、缺失配置和下次唤醒；尚未接入的能力明确不可用，不返回伪成功。
- [x] 实现前端 API 封装及冲突处理，支持 snapshot 后按游标追赶，不在此阶段提前制作完整页面。

验证：`uv run pytest -q tests/autonomy/test_api.py tests/service/test_api.py`；`node --test tests/web/agents.test.mjs`。

完成标准：通过 API 能创建停用状态的 Agent、提交合同并查看版本，未知字段和跨范围读取被拒绝。

## 5. P2：AgentRun 编排与 Session 执行

### P2.1 持久唤醒和有界编排

依赖：P1、P0.2。新增 `autonomy/{supervisor,triggers,planner,goals}.py`、`tests/autonomy/test_learning.py tests/autonomy/test_recovery.py`；接入 `service/background.py`。

- [x] 实现定时/事件唤醒去重、时区和迟到合并、租约恢复、generation 检查；同 Agent 仅一个有效规划提交者。
- [x] 先确定性归并用户目标和执行结果，必要时调用 planner；M1 支持明确目标拆分和配置的周期职责，限制提案数/深度。
- [x] 生成带来源、贡献、验收和停止条件的 PlanDecision；Host 校验后原子提交合同、预算 reservation、派发 outbox 和消费游标。
- [x] 无新信息保存 no_action；规划期间暂停、取消 Goal 或改 Role 使旧提案 superseded，已发生调用用量不丢失。

验证：`uv run pytest -q tests/autonomy/test_learning.py tests/autonomy/test_recovery.py tests/autonomy/test_foundation.py tests/autonomy/test_recovery.py`，用 fake clock 验证连续多个空周期不调用模型。

完成标准：AgentRun 在编排提交后结束，不等待 Session；规划不直接执行项目工具或改写任务验收。

### P2.2 工作包编译、SessionRouter 与可靠派发

依赖：P2.1。新增 `autonomy/{compiler,routing,dispatch}.py`、`tests/autonomy/test_dispatch.py`；修改 `service/session_setup.py`、`service/contracts.py`、`service/controller.py`、`service/store.py`。

- [x] 复用现有能力校验与 TaskAssembly，按 Role 的显式能力配置编译工作包；M1 不额外调用 SessionSetup 推荐模型。保存配置依据和可信 `producer=agent_planner`。
- [x] 路由先检查 workspace/环境/能力兼容、接管、未决人工输入和恢复状态，再比较同工作包、Goal、对象及相关调查背景；保存候选和排除原因。
- [x] 用任务槽、Session revision、输入游标和稳定 dispatch command ID 防竞态；发送前再次检查，未知 receipt 先对账再决定是否重路由。
- [x] 新子任务保存独立合同、run 和 episode 关联；续作遵守现有恢复规则。新 Session 使用有来源的交接摘要，复用 Session 不继承旧验收结论。

验证：`uv run pytest -q tests/autonomy/test_dispatch.py tests/service/test_session_setup.py tests/service/test_controller.py tests/service/test_session_turns.py`。

完成标准：两个相近工作包复用 S1 并产生可区分的 R1/R2；不兼容任务新建 Session；崩溃恢复和人工接管不会重复执行。

### P2.3 结果汇总、人工控制与资源仲裁

依赖：P2.2。新增 `autonomy/results.py`、`tests/autonomy/test_recovery.py`；修改 `service/controller.py`、`service/scheduler.py`，复用现有 acceptance 与 operation journal。

- [x] 从已提交验收、产物和失败事实更新 WorkPackage/Goal；run completed 不直接推导 Goal completed，必要验收未知则阻塞。
- [x] 实现 pause_pending、归档前暂停、Goal 取消、Session 接管/释放和合同修订；旧规划或旧派发不能复活被取消工作。
- [x] 自主/人工执行共用 workspace 仲裁；人工优先并有等待老化策略，每 Agent 默认最多一个写工作包。
- [x] failed 工作包保留终态，后继工作包记录血缘；uncertain operation 交回已有恢复机制，不因租约过期绕过恢复。

验证：`uv run pytest -q tests/autonomy/test_recovery.py tests/service/test_controller.py tests/service/test_process_cleanup.py tests/service/test_acceptance.py`。

完成标准：暂停可观测且最终收敛，人工接管不会抢走别的工作包；工具成功但验收失败不会被误报完成。

### P2.4 执行链路集成与故障验收

依赖：P2.1～P2.3。新增 `tests/autonomy/test_execution_flow.py`，扩展 `tests/autonomy/helpers.py`。

- [x] 用真实 service/worker + fake provider 完成 Role、Goal、两轮 AgentRun、同 Session 两个任务及验收汇总。
- [x] 在派发提交后丢失回执、终态后服务重启和规划中 Goal 取消注入故障，验证 receipt 与结果游标；工具副作用和进程清理复用既有 Session 恢复测试，未新增逐位置进程终止矩阵。
- [x] 验证关闭前端不影响执行；机器停机后的时间事件按策略合并，预算与人工暂停状态保持。
- [x] 记录执行链路已通、学习尚待 P3 的状态；自动执行仍作为内部功能开关启用，不将本阶段标为 M1 完成。

验证：`uv run pytest -q tests/autonomy/test_execution_flow.py tests/service/test_workers.py tests/service/test_worker_transport.py`。

完成标准：执行恢复可靠，运行到结果的关联完整，且不以跳过验收或人工接管冲突达成测试。

## 6. P3：自动学习、人工指导与首个可用闭环

### P3.1 终态学习事件与固定快照 Deep eval

依赖：P2.3、P1.3、P0.2。新增 `autonomy/learning.py`、`tests/autonomy/test_learning.py`；修改 `service/{controller,store,trajectory,semantic}.py` 与 `tests/service/test_trajectory.py`。

- [x] 在每个归属于 Agent 的 SessionRun 终态事务内写学习事件，建立 `(session_id, run_id, terminal_event_cursor)` 唯一性及启动对账；非终态不触发。
- [x] 扩展受信任的固定 `source_cursor` 快照入口，先完成 factual analysis，再对指定 run 启动 `goal_graph_v1 + extract_memory: true`。
- [x] 固定分析器/提示/模型配置/settings 身份，保存 analysis/evaluation receipt；恢复沿用原检查点，兼容人工 evaluation 不重复创建。
- [x] 分开 state、phase、coverage 和有效记忆数；失败重试、预算 deferred、partial 发布、暂停、排队老化和积压背压均可观察。
- [x] 支持 learning_dependency；不依赖复盘的后续任务使用当前已发布 memory，显式依赖不能被超时伪装成满足。

验证：`uv run pytest -q tests/autonomy/test_learning.py tests/service/test_trajectory.py tests/service/test_semantic.py tests/evaluation/test_trace_memory.py`；重点覆盖 R2 已开始时才评估 R1、失败窗口恢复及终态事件重复到达。

完成标准：每个有分析证据的终态 run 可追溯到固定 evaluation；无证据标记 not_applicable，已提交分析阶段不重复调用模型。

### P3.2 人工记忆标注与统一有效视图

依赖：P1.2、P3.1。新增 `memory/{annotations,effective}.py`、`tests/autonomy/test_context.py`；修改 `memory/store.py`、`service/autonomy.py`。

- [x] 实现 confirm/reject/correct/scope/tag/pin/retire/restore、expected revision、原记录 digest 绑定与来源血缘抑制。
- [x] 原始 evaluation 不改写；correct 保存 human_asserted 覆盖，confirm 不将 inferred 升级成 observed；扩大项目范围不通过普通标注完成。
- [x] 规划检索、自动注入、memory_search/read 与 UI 共用有效视图；旧数据库缺少标注表时保留兼容只读行为。
- [x] 源删除、内容变更、撤销/恢复递增相应范围版本；同来源重复提取不能绕过拒绝，历史证据仍可标明失效原因。

验证：`uv run pytest -q tests/autonomy/test_context.py tests/memory/test_store.py tests/autonomy/test_api.py tests/service/test_evaluation_deletion.py`。

完成标准：通过摘要和工具得到一致的更正/撤销结果，标注不会越过 workspace 或丢失原始证据。

### P3.3 显式偏好、手工方法与 ContextSnapshot

依赖：P3.2、P1.1；复用已有 Skill 版本能力。新增 `autonomy/{profile,guidance,context}.py`、`skills/imports.py`、`tests/autonomy/test_context.py`、`tests/autonomy/test_recovery.py`；修改 `skills/{contracts,store,runtime}.py`、`memory/runtime.py`、`tasks/assembly.py`、`llm/managed_step.py`。

- [x] 接入当前任务/长期指导、Role 方法文档绑定和不可变导入版本；来源保留 human/imported，scope 明确，用户锁定的条目不被推断覆盖。
- [x] 方法采用策略与证据状态分开；用户指定的方法可作为指导使用，但不能伪造 Skill active/已验证状态，不新建第二套方法身份。
- [x] 为 planner 和 executor 生成不同的有界 ContextSnapshot，保留合同、验收、版本、相关性理由、裁剪与缺口；无 workspace 仍遵循同 Session 记忆范围。
- [x] 查询键加入 guidance/effective memory/method 版本；dirty 通知与边界版本核验结合，在 before_llm 替换受管理块并记录实际生效事件。
- [x] 保留已发出 LLM/tool 交换；指导改变合同走修订流程，权限撤销走准入。记录 retrieved/selected/action/outcome 的不同语义。

验证：`uv run pytest -q tests/autonomy/test_context.py tests/autonomy/test_recovery.py tests/skills/test_runtime_validation.py tests/memory/test_runtime.py tests/llm/test_managed_step.py tests/tasks/test_task_runner.py`。

完成标准：同一查询下人工纠正能使缓存失效；运行中的方法在下一合法边界生效，回放能确认实际使用的版本。

### P3.4 Agents 页面与完整 API

依赖：P2、P3.1～P3.3、P1.4。新增 `web/assets/views/agents.mjs`、`web/assets/views/agent-detail.mjs`、`tests/web/agents.test.mjs`；修改 `web/assets/app.mjs`、`web/assets/index.html`、`web/assets/styles.css`、`web/assets/agents-api.mjs` 及 `service/autonomy.py`。

- [x] 完成设计第 10.2/21.2 节 API：编排历史、工作包/Session、学习重试、有效 memory、标注、方法导入、guidance 和实际 context 查询。
- [x] 新增 Agents 一级入口和创建流程，至少覆盖 Overview、Goals、Work、Profile、Knowledge、Learning 六个页签的 M1 内容；推断偏好和自动知识刷新尚未开放的部分明确标注。
- [x] 显示暂停中/等待/学习延迟/部分提取/指导待生效；展示 Session 复用原因、缺失依赖、下次检查与预算，不能把 enabled 当在线。
- [x] 通过现有 Sessions/Analysis 页面查看执行和证据；支持接管、仅本次/长期指导、标注纠正、跳转实际使用快照。
- [x] 处理切换 Agent 的过期请求、空列表、版本冲突和错误恢复；M1 页面轮询快照，事件游标追赶由 API 提供；操作后以服务器提交状态更新 UI。

验证：`uv run pytest -q tests/autonomy/test_api.py tests/service/test_api.py`；`npm --prefix tests/web test`。**待外部条件：人工浏览器检查**创建、查看、暂停和纠正流程。当前 CUA 返回无可用浏览器；HTTP smoke 和 DOM 自动化已通过，不替代视觉验收。

完成标准：用户不需要直接编辑 SQLite 或 Python 调用即可走完核心操作；页面只展示真实状态与已生效版本。

### P3.5 M1 纵向验收、恢复演练与使用文档

依赖：P3.1～P3.4、P2.4。新增 `tests/autonomy/test_end_to_end.py`、`tests/autonomy/smoke.py`、`docs/autonomous-agents.zh-CN.md`；修改 `README.md`、`README.zh-CN.md` 及设计的实际实现状态说明。

- [x] 重现设计第 22.1 节：W1/S1/R1 → 自动 L1 → W2 复用 S1/R2 并注入记忆 → 运行中长期指导 → 人工范围标注 → 自动 L2 → Goal 验收完成。
- [x] 增加反例链路：失败经验进入后续动作；记忆仅被检索但未使用不记为改善；学习预算不足延迟，源任务不被重跑。
- [x] 用真实 HTTP 和 worker 运行 smoke，在终态提交、评估发布、指导待生效窗口重启；记录 outbox、receipt、原始证据及调用次数。
- [x] 验证关闭 Agents 功能开关后旧 Session、CLI memory 与手动 Analysis 仍正常；只停止新自动工作，保留恢复和数据查询能力。
- [x] 更新用户文档，说明常开主机要求、Role/Session/AgentRun 区别、预算、学习积压、人工介入和当前限制；填写 M1 验收证据。

验证：`uv run pytest -q tests/autonomy tests/memory tests/skills tests/service tests/llm/test_managed_step.py`；`PYTHONPATH=src:. uv run python tests/autonomy/smoke.py`；`npm --prefix tests/web test`，再执行第 9 节里程碑检查。

**M1 放行条件：P0～P3 完成，所有关键恢复/归属/预算/人工纠正场景有通过证据；不能只以 UI 可打开或生成过一次 memory 作为完成标准。**

## 7. P4：持续目标迭代与学习质量

### P4.1 多目标调度、自主提案与有限重试

依赖：M1。扩展 `autonomy/{goals,planner,triggers,supervisor}.py`；新增 `tests/autonomy/test_goal_iteration.py`。

- [ ] 实现长期目标指标观测、差距、截止时间/阻塞影响/等待时长排序，有限目标完成和持续目标复查；保留 unknown 及来源。
- [ ] 在职责内根据新证据创建 GoalProposal，按对象/交付物/窗口去重；只细化近期就绪工作，校验依赖无环与范围。
- [ ] 同类失败按指纹退避；新 attempt 说明输入、方法或环境变化。自身报告/知识更新默认不再触发同类任务，限制派生深度。
- [ ] UI 展示为什么现在做、未选择原因和解除条件；验证用户目标修订不被旧自主计划覆盖。

验证：`uv run pytest -q tests/autonomy/test_goal_iteration.py tests/autonomy/test_planner.py tests/autonomy/test_control.py`。

完成标准：多轮周期能围绕长期目标产生有界改善，无新信息时等待，不靠制造任务维持运行。

### P4.2 从反馈学习偏好与习惯

依赖：M1、P4.1。新增 `autonomy/habits.py`，扩展 `profile.py`、`learning.py`；新增 `tests/autonomy/test_profile_learning.py`。

- [ ] 基于可信 producer、用户明确采纳/纠正和独立 episode 提取候选，排除 Agent 自述、默认 user role 派发和重复 evaluation。
- [ ] 实现 candidate/active/disputed/retired、用户锁定、scope 覆盖、反例、到期复核和低影响可逆试用；明确长期指导仍立即生效。
- [ ] 习惯候选在已有职责和授权范围内转为有界例行检查；频率观察不能生成新外部写入权限。
- [ ] Profile 页面展示变化、来源、试用效果、仅这次/以后/停用操作；拒绝来源后不得被扫描自动恢复。

验证：`uv run pytest -q tests/autonomy/test_profile_learning.py tests/autonomy/test_context.py`；扩展 `tests/web/agents.test.mjs` 并运行前端测试。

完成标准：区分「用户希望这样做」与「某方法执行成功」；至少覆盖一次性要求、长期纠正、冲突与锁定四类情形。

### P4.3 来源路由、增量刷新与事实过期

依赖：M1、P4.1。新增 `autonomy/sources.py`、`tests/autonomy/test_sources.py`；接入 `knowledge/{store,jobs,local_sources,web_sources}.py` 与已有外部工具绑定。

- [ ] M1 的静态来源绑定增加刷新周期、成功游标、文档版本、删除/失效传播与替代来源策略；先支持现有知识库和已绑定工具。
- [ ] 同步部分失败保留上个成功版本及覆盖说明；关键当前事实使用前重新核验，历史材料不被当作当前唯一证据。
- [ ] 将刷新和 embedding 的调用准入/计量接入统一预算；计量接口未接通的后端不自动刷新。
- [ ] 来源变化更新 context 版本并产生有因果链的有限事件；验证失效引用停止检索，其他独立工作仍可继续。

验证：`uv run pytest -q tests/autonomy/test_sources.py tests/service/test_knowledge.py tests/knowledge/test_store.py tests/knowledge/test_import_jobs.py`。

完成标准：Role 的「去哪取数据」能执行、刷新和解释失效，不复制出没有版本关系的新知识库。

### P4.4 方法使用回流、候选演进与 M2 验收

依赖：P4.1～P4.3。扩展 `autonomy/learning.py`、`skills/{cases,lineage,induction,runtime,validation}.py`；新增 `tests/autonomy/test_method_feedback.py`、`tests/autonomy/test_continuous_learning.py`。

- [ ] 贯通 memory/method → work package → decision → 真实 action → acceptance/user feedback，分别记录未采用、失败与 unknown。
- [ ] 新经验只创建带条件和反例的后继方法候选；复用已有 Skill 归纳、试用及验证发布能力，受统一预算准入。
- [ ] 效果报告包含纠正、重复失败、返工与成本；一次成功、引用次数或跨任务重复家族不宣称因果改善。
- [ ] 完成多周期集成验收：来源更新 → 新 Goal → 执行 → 评估 → 方法候选/偏好纠正 → 后续采用；验证来源删除与方法停用传播。

验证：`uv run pytest -q tests/autonomy/test_method_feedback.py tests/autonomy/test_continuous_learning.py tests/skills tests/evaluation/test_trace_memory.py`，执行第 9 节里程碑检查。

**M2 放行条件：M1 不回归，多目标迭代、来源刷新、偏好学习和方法使用回流均有完整证据；收益评价明确样本范围和未知项。**

## 8. P5：多角色与执行能力扩展

本阶段包含可独立交付的扩展。P5.1～P5.3 不依赖新并发内核；P5.4 是 P5.5 的强制前置。后两项涉及原设计刻意保留的执行后端缺口，必须先形成子设计再实施，不能通过改一个并发上限完成。

### P5.1 多角色共享个人画像

依赖：M2。扩展 `autonomy/{contracts,profile,context,routing,budget}.py`；新增 `tests/autonomy/test_multi_agent.py`，扩展 Agents UI。

- [ ] 明确 owner 级画像、Agent 覆盖和当前任务指导的优先级，支持共享条目版本与局部例外；UI 显示影响哪些角色。
- [ ] 支持多个 Agent 的公平预算/资源仲裁，显式 Session 转交或绑定；禁止仅凭同 owner 自动复用其他 Agent 的 Session。
- [ ] 验证共享偏好更新、局部锁定、跨 Agent 取消/暂停与 workspace 互斥不串扰。

验证：`uv run pytest -q tests/autonomy/test_multi_agent.py tests/autonomy/test_context.py tests/autonomy/test_control.py`。

完成标准：多个角色共享人的偏好，同时保留职责、合同、经验 scope 和预算边界。

### P5.2 外部事件适配

依赖：M2。新增 `autonomy/events.py`、`tests/autonomy/test_external_events.py`；扩展来源绑定和 service API。

- [ ] 先实现通用事件 envelope：来源身份、事件 ID、对象版本、发生时间、游标、origin/causation；使用现有绑定来源提供第一个适配器。
- [ ] 实现重复/乱序/迟到/撤销处理、断线重连和有限派生；新连接器所需凭据通过现有连接管理获取。
- [ ] 验证同一外部变化只产生一个有效目标，Agent 自己的写入不会无限触发同类任务。

验证：`uv run pytest -q tests/autonomy/test_external_events.py tests/autonomy/test_goal_iteration.py tests/autonomy/test_sources.py`。

完成标准：事件驱动与定时驱动共用准入、去重和预算，不引入第二套目标编排器。

### P5.3 跨项目方法推广与代表性对照

依赖：M2、P5.1。扩展 `skills/{contracts,lineage,validation,execution}.py`；新增 `tests/skills/test_promotion.py`。

- [ ] 为推广创建新方法版本，保存源范围、目标范围、参数化条件与依赖；原 workspace memory 查询规则保持不变。
- [ ] 复用冻结案例、独立 family/holdout 和真实执行验证，分别验证目标项目的环境与必要验收；不满足条件保留候选。
- [ ] 保存推广/撤销记录与配对成本；用户指定试用仍与已验证状态分开，不把历史原始数据自动共享到其他项目。

验证：`uv run pytest -q tests/skills/test_promotion.py tests/skills/test_runtime_validation.py tests/autonomy/test_method_feedback.py`。

完成标准：可检查新版本在什么项目、什么条件下适用，源撤销会传播到依赖版本。

### P5.4 写分支与模型分工的实现前置及隔离能力

依赖：M2。新增子设计 `docs/superpowers/specs/2026-10-30-autonomous-agent-execution-extensions.zh-CN.md`；拟修改 `session_environments/session.py`、`tasks/dag_lanes.py`、`workflows/adaptive_dag.py`；新增 `tests/autonomy/test_execution_isolation.py`。

- [ ] 子设计明确目录父子冲突、工作区快照/分支隔离、写入归属、集成/冲突策略、外部副作用分类、进程退出及恢复协议；列出不支持场景。
- [ ] 先实现父子路径资源冲突检测，再实现隔离目录中的写分支；分支产物通过显式主线集成及验收，不直接写共享主目录。
- [ ] 为 subagent 增加可验证模型配置与预算归属，恢复保持原配置版本；模型不存在或环境不兼容时拒绝派发。
- [ ] 故障测试覆盖分支残留进程、集成失败、基线改变、预算耗尽和重复集成；现有只读 barrier 模式保持默认。

验证：`uv run pytest -q tests/autonomy/test_execution_isolation.py tests/tasks/test_adaptive_dag.py tests/service/test_process_cleanup.py`；新增资源/集成专项测试的精确清单随子设计补入。

完成标准：隔离和恢复协议先通过故障验证，再允许可写分支；无法隔离的外部写操作保持不支持，不声称 native_os 已变成完整 OS 沙箱。

### P5.5 非 barrier 并发与 M3 验收

依赖：P5.4；完整 M3 还需 P5.1～P5.3。新增并发调度子计划，修改 `tasks/dag_lanes.py`、`workflows/adaptive_dag.py` 和对应执行协议；新增 `tests/autonomy/test_concurrent_lanes.py`。

- [ ] 定义主线与分支并发时的输入版本、共享上下文合并、事件顺序、验收归属、取消与预算传播；不能复用仅为 barrier 假设设计的状态更新。
- [ ] 分阶段开启独立只读并发、隔离写分支、集成队列；输入变化使过期分支结果待复核，不覆盖主线新状态。
- [ ] 用确定性调度测试交错执行、主线/分支失败、重启、部分取消和合并冲突，验收中统计所有分支成本。
- [ ] 与原 barrier 模式比较代表性任务的验收、交接成本和故障恢复结果；通过后才对支持的任务类型启用。

验证：`uv run pytest -q tests/autonomy/test_concurrent_lanes.py tests/tasks/test_adaptive_dag.py tests/service/test_workers.py tests/skills/test_runtime_validation.py`，执行第 9 节里程碑检查。

**M3 放行条件：各扩展声明明确支持范围；写隔离和并发恢复有独立证据，原有执行/学习闭环及 CLI 兼容性不回归。**

## 9. 验收矩阵、发布与回退

| 必须证明的行为 | 主要任务与测试 |
| --- | --- |
| Role 的目标/目录/来源可追溯，缺信息只阻塞依赖任务 | P1.1 `test_contracts`、P4.3 `test_sources` |
| 唤醒重复、旧 revision、派发崩溃不会重复任务 | P1.2 `test_store`、P2.1 `test_planner`、P2.2 `test_dispatch` |
| Session 复用保留上下文且合同独立，人工接管安全 | P2.2 `test_dispatch`、P2.3 `test_control` |
| 每个终态自动评估，固定游标，不污染 R1/R2 | P3.1 `test_learning`、现有 `test_trajectory` |
| 预算不足延迟、未知成本保留、partial 可恢复 | P1.3 `test_budget`、P3.1 `test_learning` |
| 人工更正/删除在摘要和工具中一致生效 | P3.2 `test_annotations`、P3.3 `test_context` |
| 运行中指导在合法边界生效，不改已发出协议 | P3.3 `test_context`、现有 `test_managed_step` |
| 仅本次/长期偏好/方法有效性相互区分 | P3.3 `test_imports`、P4.2 `test_profile_learning` |
| 无变化不调用模型，自身事件不会循环生任务 | P2.1 `test_planner`、P4.1 `test_goal_iteration` |
| 来源失效与方法撤销传播，成功不伪装因果改善 | P4.3 `test_sources`、P4.4 `test_method_feedback` |
| 多角色、跨项目与并发保持范围/资源/恢复边界 | P5.1～P5.5 对应专项测试 |

每个里程碑完成相关测试后，执行一次更广回归；无新改动或未解决失败时不反复运行全量检查：

```bash
uv run pytest -q
npm --prefix tests/web test
uv run ruff check src tests
uv run ruff format --check src tests
git diff --check
```

基线已有失败或格式差异必须列出并与本次影响区分，不能为通过门槛删除测试或格式化无关文件。新增行为的必要验收失败仍阻止该里程碑完成。真实模型试用另外记录配置、样本、用量和结果，不自动启用仓库的付费 live 测试。

发布采用默认关闭的自主调度开关，M1 通过后再对配置完整的 Agent 启用；每个 P5 扩展有独立能力开关。运行期回退先停止新规划/派发/学习调用，在安全边界暂停现有工作，保留 outbox、预算和恢复状态；不通过清空队列或重新派发实现回退。

数据库迁移前在服务一致性边界备份并演练恢复；优先使用向前兼容的新增表。回退二进制前确认 schema 兼容，无法兼容时恢复匹配的备份与版本，并对备份之后可能发生的外部副作用做对账，不能当作未执行重放。

## 10. 执行记录

实际基线最初为 `193d52b`，包含既有 Skill、Session 与文档改动。实施期间其他操作将这些改动提交为 `0b8bc75`，本次保留全部已有内容并基于它继续；没有创建提交，也没有修改另行出现的 `scripts/`。本次变更引用为当前工作区文件。

| 任务/里程碑 | 状态 | 实际变更引用 | 验证证据 | 限制/后续事项 |
| --- | --- | --- | --- | --- |
| P0.1～P0.2 | 已实现 | `tests/autonomy/helpers.py`、`service/background.py` | 后台共享生命周期、fixture、基线记录 | HTTP 不再拥有评估池 |
| P1.1～P1.4 | 已实现 | `autonomy/contracts.py`、`migrations.py`、`store.py`、`budget.py`、`service/autonomy.py` | 合同范围、命令重放、revision、未知用量、跨日额度、认证/归属 | Role 结构化输入由用户确认，未实现自然语言抽取器 |
| P2.1～P2.4 | 已实现 | `autonomy/triggers.py`、`planner.py`、`supervisor.py`、`dispatch.py`、`results.py`、`control.py` | 周期合并、无目标不调用模型、派发回执丢失、取消竞争、接管、暂停恢复、资源老化、两次独立 run | 编译使用显式 Role 配置及现有校验，不增加能力推荐调用 |
| P3.1～P3.3 | 已实现 | `autonomy/learning.py`、`context.py`、`memory/annotations.py`、`skills/imports.py` | 固定终态游标、失败经验、预算延后续同一评估、来源删除、标注恢复、方法不可变版本、运行中边界生效 | fake provider 证明协议和数据链路，不证明真实模型语义质量 |
| P3.4～P3.5 / M1 | 自动化闭环已实现；手工视觉待验证 | `web/assets/views/agents.mjs`、`tests/autonomy/smoke.py`、`docs/autonomous-agents.zh-CN.md` | 真实 HTTP/worker smoke、29 项新增 Python 验收、225 项 Web 测试 | 当前没有连接的浏览器；仓库既有测试/格式问题另列 |
| P4 / M2 | 未开始 | — | — | 自主发现、推断偏好、知识刷新、方法效果学习 |
| P5 / M3 | 未开始 | — | — | 多角色与隔离并发尚未开放 |

### 验证命令与结果

- 初始相关基线：`uv run pytest -q tests/service tests/memory tests/skills tests/llm/test_managed_step.py`，281 passed、2 skipped、4 failed；Web 基线 221 passed。
- 扩大相关回归：`uv run pytest -q tests/autonomy tests/memory tests/skills tests/service tests/llm/test_managed_step.py tests/evaluation/test_trace_memory.py tests/tasks/test_acceptance_budget.py`，340 passed、2 skipped、同样 4 failed。
- 最终新增验收：`uv run pytest -q tests/autonomy tests/service/test_background.py`，29 passed；最后 API/标注/控制小范围复验 15 passed。
- memory/Skill 边界复验：新增测试加 `tests/memory/test_runtime.py tests/memory/test_store.py tests/skills/test_runtime_validation.py`，45 passed（后续增加的四个控制/调度反例另外验证）。
- HTTP smoke：`PYTHONPATH=src:. uv run python tests/autonomy/smoke.py`；实际执行带 `--serve` 并正常退出，1 Session / 2 run / 2 learning cycles / 4 accepted memories，暂停后重启、关闭自主开关的历史恢复通过。验收 fixture 未报告 usage 时保留 unknown reservation。
- Web：`npm --prefix tests/web test`，225 passed。覆盖 API ID、六页签、状态、过期请求、创建表单与 HTML 字面文本处理。
- 全仓回归：`LOOM_RUN_LIVE_LLM=0 LOOM_VISUAL_LIVE=0 uv run pytest -q`，2050 passed、9 skipped、7 failed。包结构测试的子模块白名单已补充 `autonomy` 及原有的 `memory` / `skills` / `host_environment`；保留顶层 API 禁止直接导出类等断言，包结构与 API/基础测试复验 7 passed。剩余六项为已复现的基线失败，未再重复整仓运行；未启用付费 live 测试。
- 新增模块和测试 `ruff check`、`ruff format --check` 通过，`git diff --check` 通过。全仓 lint 仍有三个原有问题：`evaluation/analyze.py:426`、`optimize/runtime.py:556` 超长行，`tests/evaluation/test_behavior.py:446` lambda 赋值。全仓格式检查有 236 个既有风格文件需格式化，与独立基线相同；本次未批量改写无关代码。

相关基线和扩大回归共同的四项失败：

1. `test_plugin_sessions.py::test_large_research_result_compacts_and_retains_session_artifact`。
2. `test_plugin_sessions.py::test_service_reads_source_pages_to_the_end_without_refetching`。
3. `test_session_turns.py::test_long_history_has_a_bounded_prompt_and_keeps_the_current_request`。
4. `test_store.py::test_resume_renews_time_allowance_durably_without_resetting_usage_or_checkpoint`（既有 `acceptance_budget_request` 字段与断言不一致）。

全仓另有两项既有失败，已将 `0b8bc75` 导出至独立临时目录，用原有代码单独复现（2 failed），不依赖本次工作区改动：

5. `tests/integration/test_optimize_end_to_end.py::test_pause_after_completed_pair_resumes_without_repeating_paid_trial_sides`。
6. `tests/integration/test_optimize_end_to_end.py::test_optimize_resume_reuses_published_validation_experiment`。

两项均为原有 acceptance 模型调用计数与优化测试预期不一致；本次未修改优化路径或删除断言。

### 实施取舍与后续放行

- 无新增模型费用的 Role 结构化校验与能力编译替代额外的自然语言抽取/SessionSetup 推荐；原文、字段来源、固定环境及显式绑定保留。周期职责用固定秒数和创建时刻窗口，预算用 IANA 时区；复杂本地日历规则属于后续工作。
- AgentContext 在每个合法边界核验并生成内容寻址快照；memory 使用有效版本缓存键。没有持久化第二份 trace 或 evaluation 事实源。
- 当前按快照轮询 UI；事件 API 支持有序游标。方法导入复用 Skill 版本表，明确指导与验证状态分开；检索和模型声明采用均不能记为改善。
- 手工浏览器验收尚未完成，不能将 DOM 测试记录为视觉检查。真实模型试用也尚未执行。当前默认关闭功能开关，适合使用显式配置开展受控试用；完成视觉检查和既有失败处理后再决定产品发布。

下一项实现阶段是 **P4**；在继续扩大能力前先完成上述 M1 放行核对。
