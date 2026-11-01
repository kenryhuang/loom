# 在线 Task Spec 机制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agent 配置三层机制：初始化 LLM 推荐（人确认后生效）、每工作包派发时动态绑定 task_spec/plan_mode（失败回退 role 兜底盘）、阻塞按原因自动恢复（预算扩容 resume / 验收阻塞补工具 restart），恢复计数超限挂起等人工。

**Architecture:** 抽出 `recommend_setup` 推荐核心（session_setup.py，scope=session/agent_init/dispatch/repair）四处复用；dispatch 与 recover 改两阶段（LLM 在全局 service.lock 外，锁内重校验后提交）；恢复计数改决策级口径，超限置 recovery_exhausted 并对调度不可见；人工 resume/restart 经 SessionStore.submit 钩子重置计数。

**Tech Stack:** Python 3 (src/loom), pytest (tests/), 原生 ESM JS + node:test (src/loom/web/assets, tests/web)。

**Spec:** docs/superpowers/specs/2026-11-01-online-task-spec-design.zh-CN.md（含 builder check 六条决议，计划与之一致）

## Global Constraints

- LLM 推荐调用一律在全局 `service.lock` 之外执行；持锁期间只做状态校验与持久化。
- 推荐失败（超时/502/校验失败）必须回退兜底，绑定函数本身不抛异常。
- read_only 工作包不做动态绑定、不做验收修复（受限环境不可被扩权）。
- repair 只允许新增 collection，且新增 ⊆ {browser, filesystem, shell, web_research, document_outputs, task_control}；不允许 knowledge/external_tools。
- `POST /v1/session-setup/recommend` 行为不变（向后兼容）。
- plan_mode 合法值：auto / force / off。
- 恢复上限 `max_recovery_attempts` 代码默认值 3 改为 5（contracts.py scheduling_policy；存量已物化 role 不受影响）。
- 恢复计数口径：仅决策级结果计数（成功生成 intent、原因无法识别、repair 无新增、扩容触及上限）；安全前提失败不计数。
- commit message 末尾带 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

## Review Focus

- recommend 返回非法 JSON / 超纲 collection：dispatch 须静默回退且不阻塞派发（Task 3 测试钉住）。
- repair 提议删除现有 collection 或新增 knowledge/external_tools：校验拒绝并按决策级失败计数（Task 5 测试钉住）。
- exhausted 工作包存在时，同 agent 其他 goal 的 pending 包仍能派发（dispatch 单飞排除）（Task 4 测试钉住）。
- 人工在调度器 intent 未送达期间手动 resume：计数重置、pending intent 不失控重放（Task 6 测试钉住）。
- session 复用兼容性：候选 session 因 KB/外部工具派生出的 knowledge/external_tools collection 不应导致误判不兼容（比较前剔除派生 collection）（Task 3 测试钉住）。

---

### Task 1: recommend_setup 核心抽取与 scope 支持

**Files:**
- Modify: `src/loom/service/session_setup.py`（抽取核心；COLLECTIONS/capability_catalog 保留）
- Modify: `src/loom/web/frontend.py`（builtin_templates 移出，改为导入）
- Test: `tests/service/test_session_setup.py`

**Interfaces:**
- Produces（后续任务依赖）:
  - `recommend_setup(service, objective, *, model=None, scope="session", extra=None, provider_factory=None, timeout=60) -> dict`：返回校验后的 proposal（task_type, tool_collections, external_tools, knowledge_base_ids, plugins{context,workflow}, rationale, collection_reasons, 可选 plan_mode）。
  - `scope="dispatch"`：proposal 强制 `knowledge_base_ids == []`、`external_tools == []`；`plan_mode` 若模型返回则校验 ∈ {auto, force, off}，缺省补 "auto"。
  - `scope="repair"`：`extra={"unresolved_requirements": [str], "current_collections": [str]}`；校验：proposal.tool_collections ⊇ current_collections，且新增部分 ⊆ `REPAIR_ADDABLE`；违规抛 ValueError（调用方按推荐失败处理）。
  - `REPAIR_ADDABLE = frozenset({"browser", "filesystem", "shell", "web_research", "document_outputs", "task_control"})`（模块级导出）。
  - `builtin_templates()` 现居 session_setup.py（从 web/frontend.py 原样搬迁）；`web/frontend.py` 改为 `from loom.service.session_setup import capability_catalog, builtin_templates`。

- [ ] **Step 1: 写失败测试**

在 `tests/service/test_session_setup.py` 追加（该文件已有 recommend 测试与 fake provider 惯例，沿用之）：

```python
def test_dispatch_scope_strips_role_level_bindings_and_defaults_plan_mode(...):
    # fake provider 返回含 knowledge_base_ids=["kb_x"]、external_tools=["t1"]、无 plan_mode 的合法 JSON
    proposal = recommend_setup(service, "实现一个功能", model=None, scope="dispatch")
    assert proposal["knowledge_base_ids"] == [] and proposal["external_tools"] == []
    assert proposal["plan_mode"] == "auto"

def test_repair_scope_rejects_removal_and_role_level_additions(...):
    # extra={"unresolved_requirements": ["需要浏览器验收"], "current_collections": ["filesystem","shell","task_control"]}
    # fake provider 返回 tool_collections=["filesystem","task_control"]（减了 shell）→ ValueError
    # fake provider 返回新增 "knowledge" → ValueError
    # fake provider 返回新增 "browser" → 通过
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/service/test_session_setup.py -v -k "dispatch_scope or repair_scope"`
Expected: FAIL（recommend_setup 不存在 / scope 不支持）

- [ ] **Step 3: 实现**

`session_setup.py`：把 `SessionSetup.recommend` 的目录构建、provider 构建、prompt、LLM 调用、validate 抽为模块级 `recommend_setup(...)`；`SessionSetup.recommend(payload)` 变薄壳（保持现有返回结构：proposal + knowledge_bases + acceptance + model + elapsed_seconds + usage；slots 信号量留在壳或核心均可，确保 session 路径行为不变）。prompt 按 scope 追加段落：dispatch 说明"知识库与外部工具由 Agent 级绑定，不要选择；额外选择 plan_mode（auto/force/off），多阶段实现任务用 force，单步直答用 off，不确定用 auto"；repair 说明"只允许在 current_collections 基础上做最小新增以解除 unresolved_requirements，不得删除，不得选 knowledge/external_tools"。validate 增加 scope 分支与 plan_mode 校验。`builtin_templates` 从 frontend.py 迁入（frontend.py 改导入，/v1/web/catalog 行为不变）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/service/test_session_setup.py -v && pytest tests/web -v -k catalog 2>/dev/null; pytest tests/service -v -k "session_setup or frontend" `
Expected: 全 PASS（含既有用例不回退）

- [ ] **Step 5: Commit**

```bash
git add src/loom/service/session_setup.py src/loom/web/frontend.py tests/service/test_session_setup.py
git commit -m "feat: 抽取 recommend_setup 核心并支持 dispatch/repair scope"
```

---

### Task 2: role.plan_mode 字段与派发兜底

**Files:**
- Modify: `src/loom/autonomy/contracts.py`（ROLE_FIELDS + role_contract 校验）
- Modify: `src/loom/autonomy/dispatch.py:14`（task_payload 的 plan_mode）
- Test: `tests/autonomy/test_foundation.py`（role 合同测试所在文件；若 plan_mode 测试更合适放他处，跟随现有 role 字段测试位置）

**Interfaces:**
- Consumes: 无（独立于 Task 1）。
- Produces: role 支持可选 `plan_mode`（∈ {auto, force, off}，缺省 "auto"）；`task_payload` 兜底 plan_mode 取 `role.get("plan_mode") or "auto"`。

- [ ] **Step 1: 写失败测试**

```python
def test_role_accepts_and_defaults_plan_mode(agent_service):
    # role 不含 plan_mode → 创建后 role["plan_mode"] == "auto"
    # role["plan_mode"]="force" → 保留；role["plan_mode"]="bogus" → ServiceError

def test_task_payload_uses_role_plan_mode_as_fallback(agent_service):
    # 构造 agent role plan_mode="force" → task_payload(agent, work)["plan_mode"] == "force"
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/autonomy/test_foundation.py -v -k plan_mode` → FAIL

- [ ] **Step 3: 实现** — contracts.py：ROLE_FIELDS 加 "plan_mode"；role_contract 校验（缺省补 "auto"，非法值 ServiceError）。dispatch.py task_payload：`plan_mode=role.get("plan_mode") or "auto"`。

- [ ] **Step 4: 运行确认通过** — `pytest tests/autonomy/test_foundation.py tests/autonomy/test_dispatch.py -v` → PASS

- [ ] **Step 5: Commit** — `git commit -m "feat: role 支持 plan_mode 兜底配置"`

---

### Task 3: dispatch 两阶段动态绑定

**Files:**
- Create: `src/loom/autonomy/binding.py`
- Modify: `src/loom/autonomy/dispatch.py`（dispatch 两阶段重构 + 复用兼容性 + binding 记录）
- Modify: `src/loom/service/controller.py`（LoomService.__init__ 增加 `setup_recommender=None` 存为属性）
- Test: `tests/autonomy/test_binding.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `recommend_setup`；Task 2 的 plan_mode 兜底。
- Produces:
  - `bind_for_dispatch(service, agent, work) -> dict | None`：work.read_only → None；否则调 `service.setup_recommender(objective_text, model=role["model"], scope="dispatch")`（service.setup_recommender 为 None 时用 `recommend_setup(service, ...)`），成功返回 `{"task_spec": spec, "plan_mode": plan_mode, "record": {"source": "recommended", "rationale": str, "at": ...}}`；任何异常返回 `{"task_spec": role["task_spec"], "plan_mode": role.get("plan_mode") or "auto", "record": {"source": "fallback", "reason": str(exc), "at": ...}}`。**本函数不抛异常。**task_spec 构造：模板 = `builtin_templates()` 中 proposal.task_type 对应项的 task_spec 深拷贝，`tools.collections = proposal["tool_collections"]`，`context.plugin`/`workflow.plugin` 按 proposal.plugins 覆盖。
  - `compatible_config(candidate_task, payload) -> bool`：比较 collections 集合（双方剔除 knowledge/external_tools 两个派生项）、context/workflow 插件 id、plan_mode。
  - `service.setup_recommender`：可注入 callable `(objective, *, model, scope, extra=None) -> proposal`。

- [ ] **Step 1: 写失败测试**（tests/autonomy/test_binding.py，fixture 用 tests/autonomy/conftest.py 的 agent_service，LoomService 增加 setup_recommender 注入）

```python
def test_dispatch_applies_recommended_binding(agent_service):
    # setup_recommender 返回 {task_type:"general", tool_collections:["filesystem","shell","task_control"],
    #   plugins:{"context":"bounded_context","workflow":"legacy_planning"}, plan_mode:"force", rationale:"...", ...}
    # dispatch 后：session task_spec tools.collections 含 shell、plan_mode=="force"；
    # work["binding"]["source"]=="recommended"；agent 事件含 schedule.binding.recommended

def test_dispatch_falls_back_to_role_on_recommend_failure(agent_service):
    # setup_recommender 抛 ServiceError；role.task_spec 显式给定 collections=["filesystem","task_control"]
    # session 仍被创建，spec 用 role 的；work["binding"]["source"]=="fallback"；事件 schedule.binding.fallback

def test_read_only_work_skips_recommend(agent_service):
    # read_only 工作包（参照 dispatch.py:20-25 的生成路径）；setup_recommender 若被调用则抛 AssertionError
    # dispatch 后 spec 仍为受限 collections

def test_reuse_rejects_candidate_with_mismatched_collections(agent_service):
    # 先造一个 idle session（collections 无 browser）；本轮 binding 含 browser
    # dispatch 不复用该 session，新建一个
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/autonomy/test_binding.py -v` → FAIL（模块不存在）

- [ ] **Step 3: 实现**

dispatch.py 重构：`dispatch(aid, wid)` 先用短事务读 agent/work 快照 → 锁外 `bind_for_dispatch` → 进入现有 `with self.service.lock` 事务：重校验（agent state/work state/generation——bind() 闭包已有 generation 校验，保持）；候选过滤的 `incompatible_configuration` 分支改用 `compatible_config(candidate_task, payload)`（payload 在进锁前由 `task_payload(agent, work, binding=binding)` 构建；binding=None 即 read_only 时走现有限制覆盖且 compatible_config 沿用旧比较逻辑）；`task_payload` 增加可选参数 `binding=None`，有 binding 时覆盖 task_spec/plan_mode。`bind()` 闭包内写 `work["binding"] = binding["record"]` 并按 source 记 `schedule.binding.recommended/fallback` 事件（store.event(db, aid, ...)）。

controller.py：`LoomService.__init__` 增加 `setup_recommender=None` 参数并 `self.setup_recommender = setup_recommender`（测试注入点；生产 None → binding.py 内部回退到 recommend_setup）。

- [ ] **Step 4: 运行确认通过** — `pytest tests/autonomy/test_binding.py tests/autonomy/test_dispatch.py -v` → PASS（既有 dispatch 测试不回退：生产路径 setup_recommender=None 且无 config_path 时 recommend_setup 抛错→fallback，行为同现状）

- [ ] **Step 5: Commit** — `git commit -m "feat: 派发时按任务描述动态绑定 task_spec/plan_mode，失败回退 role 兜底"`

---

### Task 4: 恢复计数口径与 exhausted 挂起

**Files:**
- Modify: `src/loom/autonomy/scheduling.py`（recover 决策级计数 + exhaustion；_cycle blocked 扫描排除）
- Modify: `src/loom/autonomy/contracts.py:79`（scheduling_policy 默认 max_recovery_attempts 3→5）
- Modify: `src/loom/autonomy/dispatch.py`（单飞排除扫描加 blocked_reason != "recovery_exhausted"）
- Test: `tests/autonomy/test_recovery_exhaustion.py`（新建；复用 tests/autonomy/test_queue_scheduling.py 的 paused_work）

**Interfaces:**
- Consumes: 无新外部接口。
- Produces: `work["blocked_reason"] == "recovery_exhausted"` 为挂起标记；事件 `schedule.recovery.exhausted`（payload 含 work_package_id, attempts）。

- [ ] **Step 1: 写失败测试**

```python
def test_unrecoverable_reason_counts_each_attempt(agent_service):
    # paused_work(reason="某些无法识别的原因")；每次 scheduler.advance(aid, manual=True) 后 attempts +1；
    # agent scheduler 进入 intervention

def test_premise_failure_does_not_count(agent_service):
    # paused_work(control={"kind":"paused","command_id":"human-1","reason":"pause"})（人工暂停前提不满足）
    # advance 后 attempts 不变

def test_exhaustion_suspends_and_stops_retrying(agent_service):
    # role scheduling_policy.max_recovery_attempts=2（测试用显式小值）；
    # 两次 advance（不可恢复原因）→ attempts==2；第三次 advance → blocked_reason=="recovery_exhausted"、
    # 事件 schedule.recovery.exhausted；第四次 advance：attempts 不变、不再产生 exhausted 事件

def test_exhausted_work_does_not_block_other_dispatch(agent_service):
    # 一个 exhausted 包 + 另一个 goal 的 pending 包；advance 后 pending 包被派发
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/autonomy/test_recovery_exhaustion.py -v` → FAIL

- [ ] **Step 3: 实现**

scheduling.py recover()：前提链不动；在 `policy = scheduling_policy(...)` 之后、决策之前插入：无 pending intent 且 `attempts >= max` → 置 blocked_reason="recovery_exhausted"、put、event、raise。决策级失败点（parent_block 上限、扩容上限不足、"无法自动恢复"、repair 无新增——Task 5 加入）在 raise 前 `work["recovery"] = dict(attempts=attempts+1, pending=None)` + put。成功路径保持现有 intent 创建（已 +1）。_cycle 的 blocked 扫描（scheduling.py:103）加 `and w.get("blocked_reason") != "recovery_exhausted"`。dispatch.py 单飞扫描（dispatch.py:64）加同条件。contracts.py 默认值 3→5。

- [ ] **Step 4: 运行确认通过** — `pytest tests/autonomy/test_recovery_exhaustion.py tests/autonomy/test_queue_scheduling.py tests/autonomy/test_objective_budget_recovery.py -v` → PASS（既有恢复用例不回退；注意既有用例 role 若显式存了 max_recovery_attempts=3 则行为不变）

- [ ] **Step 5: Commit** — `git commit -m "feat: 恢复决策级计数与超限挂起，默认上限 5 次"`

---

### Task 5: 验收阻塞修复分支（repair recommend + restart_task）

**Files:**
- Modify: `src/loom/autonomy/scheduling.py`（recover 新增验收分支，两阶段）
- Modify: `src/loom/autonomy/binding.py`（repair 辅助）
- Test: `tests/autonomy/test_acceptance_recovery.py`（新建）

**Interfaces:**
- Consumes: Task 1 `recommend_setup`(scope="repair")/`REPAIR_ADDABLE`；Task 3 `service.setup_recommender`；Task 4 计数口径。
- Produces:
  - `repair_collections(service, *, objective, model, unresolved, current_collections) -> list[str]`：调 recommender（scope="repair"），返回**新增** collection 列表；recommend 抛错则向上抛（调用方按决策级失败计数）；返回空列表表示无可补。
  - `merge_repair_collections(task, additions) -> dict`：深拷贝 task["task_spec"]，collections 并入 additions，经 `normalize_task_spec` + `TaskAssembly` 校验（workspace/plan_mode 取 task 的；registry 用 service.plugin_registry_factory() 或默认），失败抛 ServiceError。
  - recover() 行为：acceptance blocked 分支生成的 intent 形如 `dict(id=new_id("schedule_resume"), run_id=run["id"], reason=reason, kind="restart", task_spec=merged)`；提交 `restart_task` 命令。

- [ ] **Step 1: 写失败测试**

```python
def test_acceptance_blocked_work_restarts_with_repaired_spec(agent_service):
    # paused_work 变体：state["acceptance"]={"state":"blocked","plan":{"unresolved_requirements":["UI 视觉验证缺 browser capture"]}}
    # setup_recommender(scope="repair") 返回 tool_collections=现有+["browser"]
    # advance → session 收到 restart_task 命令（id 为 schedule_resume_* 前缀），payload.task.task_spec 含 browser；
    # work.recovery.attempts==1；事件 schedule.recovery.requested 带 kind="restart"

def test_repair_without_additions_counts_and_reports_unrecoverable(agent_service):
    # repair 返回 tool_collections == 现有集合 → advance 后 attempts+1，scheduler intervention，无 restart_task 命令

def test_read_only_work_is_not_repaired(agent_service):
    # read_only 工作包 + acceptance blocked → recommender 不被调用；attempts+1；intervention
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/autonomy/test_acceptance_recovery.py -v` → FAIL

- [ ] **Step 3: 实现**

scheduling.py recover() 两阶段化：锁内前提链 + 分支判定（记录 branch 与 repair 输入：objective/acceptance 文本、`state["acceptance"]["plan"]["unresolved_requirements"]`、`state["task"]["task_spec"]["tools"]["collections"]`、`state["task"]["model"]`）；验收分支条件 `(state.get("acceptance") or {}).get("state") == "blocked"` 且 work 非 read_only。锁外执行 `repair_collections`；无新增或异常 → 重新进锁：attempts+1 put 后 raise。有新增：重新进锁重校验（task 仍 paused、run id 未变、acceptance 仍 blocked、agent generation 未变），`merge_repair_collections` 合并校验，建 intent（kind="restart"），attempts+1 put，记 `schedule.recovery.requested`（含 kind 与新增 collections），更新 work/goal/agent_sessions 簿记（与 resume 路径相同），锁外 `service.submit(sid, dict(command_id=intent["id"], type="restart_task", payload={"task": {"task_spec": merged}, "reason": reason}))`。pending intent 重放路径按 intent["kind"] 选择 resume 或 restart_task 提交。现有"无法自动恢复"raise 保持兜底。

- [ ] **Step 4: 运行确认通过** — `pytest tests/autonomy/test_acceptance_recovery.py tests/autonomy/test_queue_scheduling.py -v` → PASS

- [ ] **Step 5: Commit** — `git commit -m "feat: 验收阻塞自动补全工具集并 restart 重新下发"`

---

### Task 6: 人工介入重置恢复计数

**Files:**
- Modify: `src/loom/service/store.py`（submit 钩子）
- Test: `tests/autonomy/test_recovery_exhaustion.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 recovery 结构。
- Produces: `SessionStore.submit` 中 kind ∈ {resume, restart_task} 且 `command_id` 不以 "schedule_resume" 开头、session 经 `agent_sessions` 关联到工作包时，将该包 `recovery` 重置为 `{"attempts": 0, "pending": None}` 并清除 `blocked_reason=="recovery_exhausted"`（state 保持 blocked，由调度器后续正常恢复）。

- [ ] **Step 1: 写失败测试**

```python
def test_human_resume_resets_recovery_counter(agent_service):
    # work.recovery={"attempts":2,"pending":None}，blocked_reason="recovery_exhausted"
    # 人工 store.submit(sid, dict(command_id="manual-1", type="resume", payload={}))
    # → work.recovery.attempts==0，blocked_reason 不再是 recovery_exhausted

def test_scheduler_resume_does_not_reset_counter(agent_service):
    # command_id="schedule_resume_xxx" → 计数不变
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/autonomy/test_recovery_exhaustion.py -v -k reset` → FAIL

- [ ] **Step 3: 实现** — store.py submit 的应用段（参照 store.py:249 已有的 agent_sessions 跨表写先例）：命令成功应用后，若 kind ∈ {resume, restart_task} 且 cid 不以 "schedule_resume" 开头：`SELECT agent_id, work_package_id FROM agent_sessions WHERE session_id=?`（表不存在则跳过），有关联则读 agent_work_packages 行，recovery 有 attempts 或 blocked_reason 为 recovery_exhausted 时重置并写回。

- [ ] **Step 4: 运行确认通过** — `pytest tests/autonomy/test_recovery_exhaustion.py tests/service/test_store.py -v` → PASS

- [ ] **Step 5: Commit** — `git commit -m "feat: 人工 resume/restart 重置工作包恢复计数"`

---

### Task 7: agent 初始化推荐端点

**Files:**
- Modify: `src/loom/service/autonomy.py`（dispatch() 在 `aid = parts[0]` 之前加特例路由）
- Test: `tests/autonomy/test_api.py`（追加；沿用该文件的 facade.dispatch 调用惯例）

**Interfaces:**
- Consumes: Task 1 recommend_setup(scope="agent_init")；Task 3 service.setup_recommender。
- Produces: `POST /v1/agents/setup/recommend`，body `strict(body, {"description", "model"})`（description 为 role 描述/目标文本，model 可选）；返回 proposal + 映射好的 `{task_spec, plan_mode, knowledge_base_ids, external_tool_ids, rationale, collection_reasons}`（task_spec 按 Task 3 的模板+覆盖规则构造，供表单直接回填）。该端点不写库。

- [ ] **Step 1: 写失败测试**

```python
def test_agent_setup_recommend_returns_mappable_proposal(agent_service):
    # 注入 setup_recommender 返回完整 proposal；调 facade.dispatch("POST", ["setup","recommend"], None, {"description":"...","model":None})
    # 返回含 task_spec（collections 与插件已合并）、plan_mode、knowledge_base_ids、rationale

def test_agent_setup_recommend_failure_surfaces_502(agent_service):
    # recommender 抛错 → ServiceError 502（表单提示手动配置）
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/autonomy/test_api.py -v -k setup_recommend` → FAIL

- [ ] **Step 3: 实现** — autonomy.py dispatch()：`if parts == ["setup", "recommend"] and method == "POST"` 分支置于 `aid = parts[0]` 之前；校验 body；调 recommender/recommend_setup(scope="agent_init")；映射为回填结构返回。recommender 缺失且服务无模型配置时返回 422（引导手动配置）。

- [ ] **Step 4: 运行确认通过** — `pytest tests/autonomy/test_api.py -v` → PASS

- [ ] **Step 5: Commit** — `git commit -m "feat: Agent 初始化配置推荐端点"`

---

### Task 8: Web UI（创建表单推荐 + 执行配置编辑 + 状态展示）

**Files:**
- Modify: `src/loom/web/assets/views/agent-setup.mjs`（"智能推荐"按钮 + 执行配置区块：collections 多选 + plan_mode 选择；创建与编辑共用）
- Modify: `src/loom/web/assets/views/agent-execution.mjs`（工作包详情补 binding 来源与 recovery_exhausted 文案；recovery.attempts 展示已存在于 :77）
- Test: `tests/web/agent-setup.test.mjs`（沿用现有 node:test 惯例）

**Interfaces:**
- Consumes: Task 7 端点；Task 3 work["binding"]；Task 4 blocked_reason=="recovery_exhausted"。
- Produces: 表单 role 增加 `task_spec`（collections/context/workflow 插件）与 `plan_mode` 字段的读写；推荐结果（含 rationale/collection_reasons）展示后一键回填；编辑已有 agent 时同区块可手工改（保存走现有 role/revisions）。

- [ ] **Step 1: 写失败测试** — agent-setup.test.mjs：模拟 api（recommend 返回 proposal），点击"智能推荐"→ 表单执行配置区块回填 collections/plan_mode；手工勾选后 save 的 role 含对应 task_spec/plan_mode。

- [ ] **Step 2: 运行确认失败** — `node --test tests/web/agent-setup.test.mjs` → FAIL

- [ ] **Step 3: 实现** — agent-setup.mjs 增加执行配置区块（默认折叠，无 task_spec 时显示"跟随系统默认"）；推荐按钮调 POST /v1/agents/setup/recommend（description = role 文本+目标），结果展示理由并可回填；agent-execution.mjs 工作包详情加一行 `绑定：推荐/兜底` 与 exhausted 的中文文案（"已挂起，等待人工恢复"）。

- [ ] **Step 4: 运行确认通过** — `node --test tests/web/agent-setup.test.mjs tests/web/agents.test.mjs` → PASS

- [ ] **Step 5: Commit** — `git commit -m "feat: Agent 表单智能推荐与执行配置编辑"`

---

## 收尾

- [ ] 全量回归：`pytest tests/autonomy tests/service tests/tasks -x -q` + `node --test tests/web/`（既有 4 个失败用例与本次无关，基线已核实：test_large_research_result_compacts…、test_service_reads_source_pages…、test_long_history_has_a_bounded_prompt…、test_resume_renews_time_allowance…）
- [ ] 用真实 agent（agent_63761f68…）验证：对其 paused 的验收阻塞 session 做一次手动调度，观察 repair → restart_task 全链路（需要服务重启加载新代码后）
