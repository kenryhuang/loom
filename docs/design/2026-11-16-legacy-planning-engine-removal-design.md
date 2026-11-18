# 删除 legacy 规划引擎（planning engine 1）设计

日期：2026-11-16
状态：已确认范围，待实施计划

## 意图与背景

Loom 的自主 Agent 长期并存两套规划引擎：

- **legacy（engine 1）**：`StageCoordinator`（stage 规划）+ `AgentPlanner`/`supervisor._plan`（按 goal 拆 work package）两段式。
- **engine-2（统一规划）**：`ObjectiveCoordinator` + `PlanPlanner` + `plan_compiler`，Stage → Goals → Work Packages 一次编译发布，契约完整。

并存的代价已经在真实运行中暴露：研发搭档（`agent_63761f68f8e04e37a6663d696b811d4b`）仍是 legacy，最近一次发布产生两个 `stage_id=NULL` 的"孤儿" work package（挂在无 stage 归属的 discovery goal 上，UI roadmap 中不可见），且 run 结果硬编码 `goal_count=0` 文案误导。engine-2 从结构上不会产生这类数据。

**已确认决策（用户）**：研发搭档迁移到 engine-2；删除 legacy 引擎代码；await_input 等新功能只做在 engine-2 上。

## 目标

1. 研发搭档安全迁移到 engine-2，迁移后 engine-2 能正常完成一个规划周期。
2. 删除 legacy 规划的执行路径与全部 `planning_engine` 行为分支，代码库只剩统一规划路径。
3. 历史数据（legacy 产出的 stage/goal/work/run）完整保留、只读展示不崩。
4. 存量数据库中未迁移的 legacy agent 仍可通过已有 migrate 命令迁移（迁移工具本身保留）。

## 非目标

- 不改变 engine-2 的规划协议、编译器、调度与验收语义。
- 不做 await_input 规划中提问（独立项目，在其后进行）。
- 不删除任何历史行（runs、stages、goals、work packages、events）。

## 反目标（做了即失败）

- 迁移过程丢失在途执行结果或已完成交付证据。
- 删除后残留 `planning_engine` 行为分支等死代码。
- UI 无法渲染历史 legacy 记录（如 `kind="work_plan"` 的旧 run、无 stage_id 的旧 work package）。
- 未迁移的 legacy agent 在代码删除后产生报错循环（应安静停止规划 + 提示迁移）。

## 约束与既有基建

- **迁移工具已存在且可用**：`plan_commands.migrate()`（含 `migration_report()` 预览）做完整状态改写——取消非终态 work package（标记 `superseded_contract` + `migration_reason`）、supersede 非终态 goal/stage、objective 状态重置为 `ready`（清空 stage_id/roadmap_id/discovery_id/last_input/blocker）、agent 置 `planning_engine=2`，并先落一份 sqlite 备份。有在途执行（running/queued/pausing/paused/awaiting_input 的 session）时 409 拒绝。
- **研发搭档当前有一个 running discovery 包**（`work_1ae7a55f…`，只读调查）。迁移必须先等它完成或手动停止；pending 的 `work_65248b91…` 由 migrate 自动取消。
- 新 agent 默认已是 engine-2（`service/controller.py:91`：未注入 legacy planner callable 时默认 2）；config.yaml 无覆盖。
- `stage_planner.py` 是共享模块：`SCHEMA`/`validate`/`fit_payload`/`obj` 被 `plan_compiler` 与 engine-2 复用，只有 legacy 专属分支（如 `stage_planner.py:220` 的非 engine-2 证据规则、legacy 用 PROMPT/类）可删。

## 已定的实现决策

1. **顺序：先迁移、后删代码**。先迁移研发搭档并观察 engine-2 正常规划至少一个周期，再落地删除 PR。删除 PR 本身不迁移任何存量数据。
2. **保留 migrate 命令与 UI 迁移提示**作为存量数据迁移工具（它只做状态改写，不依赖 legacy 规划器）。代码删除后，未迁移的 legacy agent：`QueueScheduler` 不再为其调用任何规划器（安静跳过），UI 迁移提示（agents.mjs 已有）继续显示。
3. **`planning_engine` 字段保留为数据属性**（历史快照、views 输出兼容），但删除全部行为分支；`AgentStore` 不再接受 engine 参数，新 agent 恒为 engine-2；`service/controller.py` 删除 `agent_planner`/`agent_stage_planner`/`agent_planning_engine` 构造参数。
4. **删除即删除，不做兼容垫片**：legacy 专属的 run kind（`work_plan`/`stage_plan`/`stage_review`）不再产生，但 UI 渲染历史记录的分支保留。
5. 服务重启恢复逻辑（supervisor 构造里的 run 状态修复）保留对 legacy kind 的识别（`recoverable` 判断），避免旧库启动报错。

## 删除清单（代码面，实施时逐行核实）

| 位置 | 内容 |
|---|---|
| `autonomy/supervisor.py` | `_plan` 整个方法；`AgentPlanner`/`StagePlanner`/`StageCoordinator` 的构造与字段；相应 import |
| `autonomy/planner.py` | 整文件（legacy AgentPlanner） |
| `autonomy/stages.py` | `StageCoordinator` 的 `advance`/`_advance`/`discovery`/`commit`/`repair` 及 legacy-only 辅助；保留共享的 `planning_state`/`evidence`/`signature`/`role_signature`/`TERMINAL`/`state_id`/`runnable`（以实际引用为准） |
| `autonomy/stage_planner.py` | legacy `StagePlanner` 类与 legacy-only 校验分支；保留共享 schema/validate/fit_payload |
| `autonomy/goals.py` | `rank` 与 legacy goal 调度分支（37/49/190 行一带） |
| `autonomy/habits.py` / `triggers.py` | legacy 引擎触发的调度分支 |
| `autonomy/scheduling.py` | 引擎分流（109-111 行一带）与 legacy 分支调用 |
| `autonomy/configuration.py` / `acceptance.py` / `stage_commands.py` / `stage_goals.py` / `service/autonomy.py` / `autonomy/store.py` | 各 `planning_engine` 行为分支 |
| `service/controller.py` | `agent_planner`/`agent_stage_planner`/`agent_planning_engine` 参数与默认逻辑 |
| `web/assets/views/agent-roadmap.mjs` | `planning_engine===2` 三元分支（284/293/311 行一带） |
| `web/assets/views/agents.mjs` | 迁移提示**保留**（决策 2）；如还有 legacy-only 展示分支则删除 |
| `tests/` | legacy 路径专属测试删除；migrate/共享行为测试保留并改到无分支世界 |

## 运营步骤（研发搭档迁移）

1. 等 `work_1ae7a55f…`（只读摘录报告）完成，或在 UI 停止它。
2. Agent 页面 → "Review planning migration" 预览 → "Apply planning migration" 应用（自动备份 sqlite）。
3. 观察下一个调度周期：engine-2 应基于既有证据提案新 stage（上次拒绝原因与 discovery 结论都在 answers/证据里）。
4. 确认正常后再合并删除 PR。

## 验收标准

- `uv run pytest` 全绿；`tests/web` 除 2 个既有失败（role 预算配置相关，与本次无关）外全绿。
- `grep -rn planning_engine src/` 只剩数据兼容读取（views 快照输出、migrate、历史渲染），无行为分支。
- 研发搭档迁移后完成至少一次 engine-2 规划周期（run kind=`unified_plan`，结果 committed）。
- 旧库（含 legacy 历史数据）启动服务无报错，Agent 页面正常渲染历史。

## 留给实施者的判断

- `stages.py`/`goals.py` 中每个函数"共享 vs legacy-only"的最终界定（以 engine-2 引用为准）。
- 测试套件中哪些用例改写为 engine-2 等价物、哪些直接删除。
- `migration_reason`/历史字段是否纳入 views 快照输出（默认不动）。

## Builder 检查决议（2026-11-16）

1. **未迁移 legacy agent 的护栏**（文档修订）：删除 `scheduling.py` 引擎分流后，在原位置保留**唯一**显式护栏——`planning_engine != 2` 的 agent 不进入任何规划器，`finish("waiting", 迁移提示)` 并记录事件。验收标准相应修订为：`grep planning_engine` 仅允许这一个有文档说明的迁移护栏 + 数据兼容读取（views 快照、migrate、历史渲染）。
2. **在线迁移由 Claude 执行**：监控研发搭档 running 的 discovery 包完成 → 调 migration API → 观察一个 engine-2 规划周期正常 → 才开始删除。删除 PR 以该观察结果为门槛。
3. **代码基线**：删除工作基于当前工作树（含已提交的 UI 归位 commit）。
4. **历史 legacy 数据的写接口**（如对无 stage 的旧 goal 调 retry_goal）：统一返回干净的 409（按 stage 缺失判断，不需要引擎分支）；文档只承诺历史展示不崩，写拒绝不算回归。
