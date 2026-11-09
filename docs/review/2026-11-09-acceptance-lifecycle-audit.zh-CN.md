# 验收生命周期审查：完成调用循环依赖与恢复缺口

日期：2026-11-09（Asia/Shanghai）。范围：Engineering partner 的 WP2、验收实现、统一规划编译与恢复链路。本文保留修改前的审查与复现；后续已按[简化验收协议](../design/2026-11-09-acceptance-contract.zh-CN.md)实施共享规则、恢复校验、合同修订与完整状态修复，部署情况见该文。

## 结论与归因

本次循环门是上一轮统一规划重构暴露并放大的回归：原有底层验证器没有禁止不可取证的控制工具，新规划入口又没有沿用旧验收规划提示词的禁令，最后把错误方法固定进工作合同。

- `31708a0` 已在旧 `acceptance.PLAN_PROMPT` 中加入不得要求成功 `finish/complete_node` 的说明，但没有对应的宿主校验。
- `c8e6bd5` 引入由 `PlanPlanner.PROMPT` 直接生成完整验收方案的 v2 入口，Worker 的 `prepare()` 直接加载 `compiled_plan`，不再调用旧验收规划器。新提示词没有迁入这条禁令。
- 编译器确实调用了共享 `AcceptanceController.validate_plan()`；问题在于该校验只确认工具存在，没有确认它能产生可用于该验收的执行凭证。
- 本地对普通独立 Session 也能复现同类死锁，证明底层缺陷不是 v2 才产生；v2 新入口和固定合同使它进入了这次实际运行。

## 运行证据与一项必要纠正

实际 Session：`sess_d13844de96944fd6bfe068d14005b27f`，WP2：`work_plan_4dd56c8e8dd4982d3deed330512c5cff`。

- 最新 run 的 seq 7898、10339、12654 均有 `tool.started`，`tool_id=finish`，随后有 `tool.completed`。
- 三次 `verification.completed`（seq 7902、10343、12658）都只有 `wp2-finish-report=failed`，原因相同：`finish: 0 successful recorded calls; required 1`。
- 三次 gate 都被拒绝，最终决策是 `pause / ACCEPTANCE_REPAIR_LIMIT`。
- 该 Session 没有独立语义验收模型请求；最新分配的 `model_calls=0`。

因此，“其他所有验收标准都通过”不符合记录。当前实现对确定性失败提前返回，WP2 的三个语义标准及宿主 `goal` 标准没有被执行，应标为“未评估”。WP1 曾通过验收，也不能代替 WP2 的独立验收。

## 发现

### F1 · P1：输出合同允许永远不能通过的控制工具计数

位置：`src/loom/tasks/acceptance_protocols.py:72`，`src/loom/tasks/acceptance.py:469`，`src/loom/tasks/assembly.py:502`、`:518`、`:564`。

`tool_call.check.tool_id` 的枚举包含所有 bindings，`validate_plan()` 只检查 ID 存在及次数合法。与此同时，`finish` 在验收之前不能被接受；更进一步，即使它被接受，`placement=control` 的工具也会直接返回，根本不会进入 `record_call()`。运行事件中的 `tool.completed` 与验收执行凭证不是同一个记录源。

这不是仅限 `finish` 的字符串黑名单问题：任何绕过执行凭证记录的控制工具都不能成为这种计数验收的对象。`complete_node` 等是否受影响取决于其实际绑定和记录路径，不能只依赖工具名称推断。

复现：当前校验器接受 `tool_call(finish, min_calls=1)`；调用一次被正常接受的控制 `finish` 后，验收调用索引仍没有成功 `finish`。普通 Session 的独立复现也因该标准而暂停，语义模型调用数为 0。

修复原则：由宿主声明每种工具是否支持可验证执行凭证，schema 和运行时校验共用这份能力。完成流程只能依赖完成请求之前已存在的事实。报告内容通过 completion candidate 验证，不能把“最终完成已成功”当作它自身的前置条件。

### F2 · P1：历史 Goal 状态污染当前 Stage 修订，实际自动恢复被挡住

位置：`src/loom/autonomy/plan_compiler.py:103`。

存在当前 Stage 时，校验检查全部 `existing_goals` 的状态，没有按当前 `stage_id` 过滤。迁移保留的历史调研 Goal 为 `superseded`，它不在允许集合中，因此任何当前 Stage 修订都被拒绝为 `Current stage cannot be revised in this state`。

使用实际持久输入离线回放：

| 输入 | 同一修订提案的结果 |
| --- | --- |
| 保留全部历史 Goal | 拒绝：Current stage cannot be revised in this state |
| 仅对当前 Stage 的 Goal 做该状态检查 | 通过 |

实际后继规划决策还把这一错误理解成“阶段不可修订”，返回 `review_stage blocked`。宿主将 Stage 保持 active、规划状态改为 waiting；该决策没有生成新的结构化 blocker，调度摘要后来显示 idle。需要同时保证域错误能定位历史记录、当前修订或执行状态，而不是让模型据错误消息猜测生命周期。

### F3 · P1：合同错误被反馈为交付缺口，修复被交给没有修改权限的 Worker

位置：`src/loom/tasks/acceptance_feedback.py:62`，`src/loom/tasks/acceptance.py:680`，`src/loom/tasks/recovery.py:61`。

`tool_call` 确定性失败没有结构化 gap，反馈默认转成 `deliverable_mismatch`。这次 `scope=['final discovery report']` 又被当作真实路径，建议变成“Restore or produce the deliverable at: final discovery report”。因此 Solver 重写报告、重读文件、再调 finish，直到修复额度耗尽。

v2 Worker 的 `revise_acceptance_plan` 被明确禁止；改变方法必须回到统一计划修订。这项限制本身合理，但当前反馈没有识别“不可能的合同”，也没有立即向 Coordinator 提交方法错误，而是要求 Worker 修复交付物。耗尽之后的 `assess()` 虽会给出 reassemble，F2 又挡住了这条路径。

修复原则：无效验收合同与实际交付不足使用不同的类型。前者立即归计划所有者重新编译；后者才交给 Worker 做有界执行修复。缓存 scope 不能直接当成补救动作目标。不得将拒绝的 finish 记成成功以绕过验收。

### F4 · P2：旧检查点只核对指纹，新增校验不会自动修复已发布坏合同

位置：`src/loom/tasks/acceptance.py:564`，`src/loom/tasks/acceptance_budget.py:64`。

已有 `data.plan` 时，`prepare()` 对 v2 比较固定合同与验证方案指纹，之后直接复用，不重新执行当前合同规则。Resume 更新预算、保留旧计划。即使现在只给新提案加禁令，已发布的 WP2 仍可能反复携带同一条坏标准。

这意味着修复必须覆盖新计划、已发布计划及恢复入口：给校验规则版本化，旧合同恢复时重新检查，发现不满足新不变量则提交明确的计划修订。有效读取记录和 candidate 应保留；指纹一致只证明合同没有变化，不证明合同可执行。

### F5 · P2：方法修订保留文字，但执行义务的保留仍依赖模型判断

位置：`src/loom/tasks/acceptance.py:688`，`src/loom/autonomy/plan_compiler.py:155`、`:217`。

普通 Session 的方法修订检查现有 criterion 的 ID、description、required 未改变，允许改变 verifier、check、scope 和 required_tools。实际复现中，把要求运行测试的 command 标准改为同名同描述的 semantic 标准，并去掉 shell 工具要求，宿主接受了修订。

系统会保留 `original_requirements` 并交给独立语义模型复核，因此本项不能表述为“修订后必然错误通过”；风险是用户明确要求实际执行、指定来源或截图时，强制证据义务仍没有宿主级保留规则。v2 重编译主要继承业务标准文字，同样不能单靠“文字仍在”证明具体证据义务没有弱化。

修复原则：把必须执行的命令、必须取得的视觉证据、不可替代的外部结果建模成不可削弱的证据义务；方法替换要提供对应关系，无法证明等价就保留未满足状态。普通语义标准仍可用模型判断，不需要把所有业务要求硬编码。

### F6 · P2：提前失败后的验收结果不完整，审计信息夸大已执行范围

位置：`src/loom/tasks/acceptance.py:874`、`:954`。

确定性标准失败后，不会执行语义标准，结果数组中也没有为这些标准写 `not_run/deferred`。gate 事件却固定声明 `assurance='checks_and_model_judgment'`，即使该轮模型调用数为 0。本次由此很容易把“未出现在失败列表”误读成“其余已通过”。

提前停止付费判断是合理的成本策略；需要完整保存每项标准的状态、未执行原因和实际验证方法，准确生成 assurance，不能将未评估标准算作通过。

## 建议修复顺序与验证边界

1. **统一合同有效性规则**：schema、Compiler、独立 Session、恢复检查点全部使用同一规则集；先禁止没有凭证路径或依赖自身完成的前置条件。
2. **修通合同错误恢复**：无效合同直接回 Coordinator；修订只检查当前 Stage；原子提交新版本后才替代旧方法。
3. **保留事实、重新验收**：保留原报告和有效读取证据，用可实现的方法检验报告内容与最终 Goal；不能直接完成 WP2，也不能只清空失败标准再沿用旧通过结论。
4. **补上证据义务与完整状态**：防止方法修订静默弱化具体证据义务；每项标准都必须有 passed/failed/blocked/not_run 之类的明确结果。

不需要再引入另一套验收规划器。应保留“完整合同编译 → 固定合同执行 → 事实与 candidate → 验收 → 提交完成”的单一顺序，补齐各边界的宿主不变量。

## 验证记录

- 现有验收、工具证据、反馈、方法修订、预算和协议测试：109 项通过。命令为 `.venv/bin/python -m pytest -q tests/tasks/test_acceptance.py tests/tasks/test_acceptance_tool_evidence.py tests/tasks/test_acceptance_feedback.py tests/tasks/test_acceptance_methods.py tests/tasks/test_acceptance_budget.py tests/tasks/test_acceptance_protocol.py`。
- 临时本地复现确认：控制 finish 不进入验收调用索引；普通 Session 可接受循环标准并暂停；command → semantic 丢弃工具义务的修订被接受；v2 Worker 无权本地改方法。
- 使用真实持久输入复现当前修订被历史 superseded Goal 拒绝，并验证仅对当前 Stage 检查即可通过。
- 运行数据使用只读连接检查；复现只在临时目录运行，未调用真实模型或改动线上任务。源码未修改。
