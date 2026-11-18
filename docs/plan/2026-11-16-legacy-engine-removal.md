# Legacy 规划引擎删除 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 legacy 规划引擎（engine 1）的全部执行路径与行为分支，所有 agent 统一走 engine-2 统一规划，保留 migrate 工具与唯一迁移护栏。

**Architecture:** 先把测试套件整体迁移到 engine-2 基座（legacy 代码变为无测试引用），再落调度护栏，最后删代码、清接线、清 UI 分支。每个任务结束测试套件保持全绿。

**Tech Stack:** Python 3.11（src/loom），vanilla-JS 前端（src/loom/web/assets），pytest + node --test（tests/web）。

**Spec:** [docs/design/2026-11-16-legacy-planning-engine-removal-design.md](../design/2026-11-16-legacy-planning-engine-removal-design.md)（含 Builder 检查决议一节，同为依据）

## Global Constraints

- 每个任务结束：`uv run pytest tests/autonomy -x -q`（或相应子集）与 `cd tests/web && node --test *.test.mjs` 保持既有通过基线（web 套件 2 个既有失败：role 预算配置相关，与本次无关，不算回归）。
- 历史数据只读保留：不删除任何 runs/stages/goals/work_packages/events 历史行；UI 对 `kind="work_plan"` 旧 run、无 `stage_id` 旧 work package 的渲染分支保留。
- `planning_engine` 字段保留为数据属性（views 快照、migrate）；删除后唯一允许的行为分支是 Task 4 的迁移护栏。
- 提交信息遵循仓库近期风格（中英文均可），结尾带 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。
- 工程师可自行决定处：stages.py/goals.py 中每个函数"共享 vs legacy-only"的界定（以 engine-2 实际引用为准）；测试用例"改写 vs 删除"的取舍（规则见 Task 2）。

## Review Focus

1. **旧库启动恢复**：supervisor 构造函数对 `kind in {"stage_plan","stage_review","unified_plan"}` 的 planning run 标 recoverable、其余标 superseded——删除后旧 kind 必须仍可识别（不得 NameError/KeyError）。→ Task 4 步骤加回归测试。
2. **历史 legacy 数据写操作**：`plan_commands.retry_goal` 对无 `stage_id` 的历史 goal 当前会 500（`stage["revision"]` 空指针）；期望干净 409。→ Task 5 加测试钉住。
3. **未迁移 legacy agent 零活动**：不得产生 planning run、不得 dispatch、scheduler 落 waiting 且带迁移提示。→ Task 4 测试。
4. **migrate 删除后仍可用**：对 store 直接构造的 legacy agent（不经 service 引擎参数）执行 migrate 成功。→ Task 5 测试。
5. **dispatch/execution 恢复语义不变**：works 的 dispatch/recover/budget 行为与引擎无关，测试迁移后必须原样通过（不是放松断言过关）。→ Task 2 规则约束。

---

### Task 1: engine-2 测试基座

**Files:**
- Modify: `tests/autonomy/helpers.py`
- Test: `tests/autonomy/test_unified_planning.py`（既有，验证基座可用）

**Interfaces:**
- Produces: `make_unified_service(tmp_path, proposal_fn)` 工厂（返回 `(service, facade, aid)`，内部用 `LoomService(..., agent_plan_provider=proposal_fn)` + autonomous stage_planning + enable，模式照抄 `test_unified_planning.py:36-47` 的 `unified` fixture）；`compiled_work(aid, *, goal_id, stage_id, ...)` 快捷构造函数（生成 engine-2 形状的 work package dict：`engine_version=2`、`verification_plan`、`compiled_payload` 经 `plan_compiler`/`AcceptanceController` 真实校验后产生——不允许手工拼裸 dict 绕过校验）。

- [ ] **Step 1:** 在 helpers.py 增加上述两个工厂；`make_unified_service` 的签名与 fixture 语义以 `test_unified_planning.py` 的 `unified` fixture 为准（含 `notes.txt` 证据文件的可选参数）。
- [ ] **Step 2:** 把 `test_unified_planning.py` 的 `unified` fixture 改为调用 `make_unified_service`，断言不变。
- [ ] **Step 3:** Run: `uv run pytest tests/autonomy/test_unified_planning.py -q` — 全绿。
- [ ] **Step 4:** Commit: `test: 抽出 engine-2 统一规划测试基座`

### Task 2: 测试套件迁移到 engine-2（最大任务，按文件簇分步）

**Files:**
- Modify: `tests/autonomy/conftest.py`（`agent_service` fixture 移除 `agent_planner=single_package`，改走 engine-2）
- Modify/Delete: `tests/autonomy/` 下各测试文件
- Test: 全套件

**Interfaces:**
- Consumes: Task 1 的 `make_unified_service`/`compiled_work`。
- Produces: 全套件在无 legacy 引用下转绿；已知 legacy-only 文件的处置记录（提交信息中列出）。

**取舍规则（设计文档明确留给实施者，规则在此钉死）：**
- 用例验证的是**已删除行为**（`AgentPlanner.propose`、`StageCoordinator.advance/_advance/discovery/commit/repair`、`StagePlanner.propose`、`goals.rank`、legacy work_plan run、legacy 触发物化）→ **删除该用例/文件**。
- 用例验证的是**存活行为**（dispatch、recovery、budget、acceptance、sources、learning、API）只是经 legacy fixture 搭场景 → **改写 setup**：engine-2 下用 `make_unified_service` + 脚本化 proposal 让规划器真实产出 compiled works，或直接 `compiled_work(...)` 落库。
- 拿不准归属的文件，优先看断言针对的是规划产物形状（legacy）还是执行/调度行为（存活）。

**已知 legacy-only（删除候选）**：`test_planner.py`（整文件）；`test_stage_planner.py` 中 `StagePlanner.propose` 用例（保留 `validate`/`fit_payload`/`acceptance_support` 用例）；`test_stage_progression.py`、`test_structured_contracts.py`、`test_stage_end_to_end.py` 中经 `StageCoordinator` 驱动规划的用例。
**已知需改写 setup（存活行为）**：`test_dispatch.py`、`test_queue_scheduling.py`、`test_recovery*.py`、`test_budget*.py`、`test_end_to_end.py`、`test_execution_flow.py`、`test_configuration.py`、`test_binding.py`、`test_stage_goals.py`、`test_stage_api.py`、`test_stage_contracts.py`、`test_stage_overlap.py` 等。

- [ ] **Step 1:** 改 conftest 的 `agent_service`：移除 `agent_planner`，等价 engine-2 默认。
- [ ] **Step 2:** 按文件簇循环：选一个簇 → `uv run pytest <簇> -q` → 红灯文件按取舍规则处理 → 簇内全绿才进下一簇。簇顺序：dispatch/recovery → budget/learning → stage 系列 → api/e2e → 其余。
- [ ] **Step 3:** Run: `uv run pytest tests/autonomy -q` — 全绿（允许删除用例后的总数下降，提交信息记录删除/改写清单）。
- [ ] **Step 4:** Commit: `test: 测试套件迁移到 engine-2 基座`

### Task 3: UI 引擎分支清理（提前做，解锁 Task 4 的护栏文案）

**Files:**
- Modify: `src/loom/web/assets/views/agent-roadmap.mjs`（284/293/311 行一带的三元）
- Keep: `src/loom/web/assets/views/agents.mjs:445-462`（迁移提示保留）
- Test: `tests/web/agents.test.mjs`

**Interfaces:**
- Produces: roadmap 不再读 `d.planning_engine` 做展示分支；migration 提示继续工作（其 fixture 显式构造 `planning_engine:1`）。

- [ ] **Step 1:** 先改 `tests/web/agents.test.mjs`：`planning_engine===2` 分支断言改为无条件断言；legacy 文案断言删除；migration 用例的 fixture 显式保留 `planning_engine:1`。Run: `node --test agents.test.mjs` — 红。
- [ ] **Step 2:** 删 agent-roadmap.mjs 三处三元，保留 engine-2 文案。
- [ ] **Step 3:** Run: `cd tests/web && node --test *.test.mjs` — 除 2 个既有失败外全绿。
- [ ] **Step 4:** Commit: `refactor: roadmap 视图移除引擎分支`

### Task 4: 调度统一 + 迁移护栏 + 删除 legacy 规划路径

**Files:**
- Modify: `src/loom/autonomy/scheduling.py`（`_cycle`，约 107-210 行）
- Modify: `src/loom/autonomy/supervisor.py`（删 `_plan`、`AgentPlanner`/`StagePlanner`/`StageCoordinator` 构造与 import；保留启动恢复对旧 run kind 的识别）
- Delete: `src/loom/autonomy/planner.py`
- Modify: `src/loom/autonomy/stages.py`（删 `StageCoordinator` 的 legacy 规划方法；保留 `planning_state`/`evidence`/`signature`/`role_signature`/`TERMINAL`/`state_id`/`runnable` 以实际引用为准）
- Modify: `src/loom/autonomy/stage_planner.py`（删 `StagePlanner` 类与 legacy-only 校验分支如 220 行；保留 SCHEMA/validate/fit_payload/obj/TEXT/TEXTS）
- Modify: `src/loom/autonomy/goals.py`（删 `rank`、`materialize` 及 legacy 分支）、`habits.py`、`triggers.py`（删 legacy 物化路径）
- Test: `tests/autonomy/test_queue_scheduling.py`（护栏测试归属此文件）

**Interfaces:**
- Produces: 护栏行为——`planning_engine != 2` 的 agent：`scheduler.finish(aid, "waiting", "Legacy planning was removed; run planning migration to resume.", blockers=[make(Code.MODEL_PROTOCOL? 否——用既有合适 code 或新增 LEGACY_REMOVED)])` 后 return，不产生 run/dispatch；发事件 `plan.legacy_removed` 一次（以 agent.generation 去重）。

- [ ] **Step 1:** 写失败测试（test_queue_scheduling.py）：store 直造 `planning_engine` 缺失的 agent → `scheduler.advance` → scheduler.state=="waiting"、reason 含 migration 提示、无 `agent_runs` 新行、无 outbox dispatch；重复 advance 不重复发事件。engine-2 agent 正常路径不受影响（既有 unified 测试覆盖）。
- [ ] **Step 2:** Run 确认红。
- [ ] **Step 3:** 改 `_cycle`：引擎判断处改为护栏——非 2 → finish+return；2 → `unified.cycle`。删除 legacy 分支全部代码（reconcile/materialize/_plan/dispatch_one 的 legacy 调用）。删 supervisor._plan 与相关构造、planner.py、StageCoordinator legacy 方法、StagePlanner、goals.rank 等。
- [ ] **Step 4:** supervisor 启动恢复回归测试（旧库含 `kind="work_plan"` 的 planning run → 启动后标 superseded 不报错；`stage_plan`/`stage_review`/`unified_plan` 标 recoverable）。先写测试确认当前行为，删除后仍绿。
- [ ] **Step 5:** Run: `uv run pytest tests/autonomy -q` 全绿 + `grep -rn "StageCoordinator\|AgentPlanner\|StagePlanner\b" src/loom --include="*.py" | grep -v __pycache__` 为空。
- [ ] **Step 6:** Commit: `feat!: 删除 legacy 规划引擎执行路径，未迁移 agent 进入迁移护栏`

### Task 5: 配置与接线清理

**Files:**
- Modify: `src/loom/service/controller.py`（删 `agent_planner`/`agent_stage_planner`/`agent_planning_engine` 参数，约 49-92 行）
- Modify: `src/loom/autonomy/store.py`（删 `planning_engine` 构造参数；snapshot 字段保留为常量 2 或从 agent 读——以 views 兼容为准）
- Modify: `configuration.py:72/135`、`acceptance.py:69`、`stage_commands.py:69/183`、`stage_goals.py:40`、`service/autonomy.py:15/188/537`、`habits.py:28/39`、`scheduling.py:109`（若 Task 4 后仍在）——分支全部按"恒为 2"化简
- Test: `tests/autonomy/test_api.py`（migrate 测试）、`tests/autonomy/test_stage_goals.py`（retry 409）

**Interfaces:**
- Produces: `LoomService(...)` 不再接受引擎相关参数；`migrate` 命令照常工作；`retry_goal` 对无 stage 的历史 goal 返回 409。

- [ ] **Step 1:** 失败测试 ×2：①store 直造 legacy agent → migrate 命令成功（不依赖 service 引擎参数）；②对无 `stage_id` 的 completed 历史 goal 调 retry_goal → 409 而非 500。
- [ ] **Step 2:** Run 确认红（②当前应为 500/异常）。
- [ ] **Step 3:** 实施删除与化简；`retry_goal` 加 stage 缺失 → 409 的守卫。
- [ ] **Step 4:** Run: `uv run pytest tests/autonomy -q` 全绿 + `grep -rn "planning_engine" src/loom --include="*.py" | grep -v __pycache__` 只剩：scheduling 护栏、views 快照、migrate、store 数据写入。
- [ ] **Step 5:** Commit: `refactor: 移除引擎配置接线，统一规划引擎常量`

### Task 6: 终验

- [ ] **Step 1:** `uv run pytest -q`（全量）绿；`cd tests/web && node --test *.test.mjs` 除 2 个既有失败外绿。
- [ ] **Step 2:** 旧库冒烟：用 `runs/service/before-plan-v2-97a11f4b1a98a5dd.sqlite`（迁移前备份）拷贝到临时目录起服务 → Agent 页面可渲染、legacy agent 落迁移护栏、migrate 预览接口正常。手工验证并记录结果。
- [ ] **Step 3:** 验收 grep 结果贴入提交信息；Commit: `chore: legacy 引擎删除终验`。

## Self-Review 记录

- **Spec 覆盖**：设计文档目标 1（迁移运营）已在会话中完成并观察 committed；目标 2/3/4 由 Task 2-6 覆盖；Builder 决议 1（护栏）→Task 4，决议 3（基线）→已提交，决议 4（写接口 409）→Task 5。
- **步骤扫描**：Task 2 的逐文件取舍是设计文档明确留给实施者的判断，计划给出规则+验证循环而非逐文件步骤。
- **类型一致**：`make_unified_service`/`compiled_work` 在 Task 1 定义、Task 2 消费，签名一致。
- **比例**：删除型计划，验证命令与取舍规则为主体，无大段代码转写。
