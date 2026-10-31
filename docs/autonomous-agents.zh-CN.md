# 自主 Agent：运行与人工介入

P0～P3 提供显式目标和周期职责的执行、学习闭环。一个 Agent 保存长期 Role，AgentRun 负责拆分和派发，SessionRun 使用现有执行内核完成工作。任务验收、Deep eval 和人工偏好是不同事实，分别保存和展示。

## 启动

```bash
uv run loom serve --data-dir .loom/service --config /absolute/path/to/task-config.json --enable-agents
```

沿用已有模型配置和服务凭据。打开服务输出的 `/web/` 地址，连接后进入 **Agents**。省略 `--enable-agents` 时可查看和编辑 Agent 历史，但不会启动自主编排、派发、学习或已排队的自主执行。普通 Session 和手动 Analysis 仍然可用。

主机和服务进程需要持续运行。关闭浏览器不会停止任务；机器休眠期间不会执行。服务持有目录级进程锁，同一数据目录只能有一个执行 supervisor。

## 首次配置

1. 创建 Role：填写角色原文、长期目标、工作目录、来源位置、默认方法、模型、检查间隔和预算。新 Agent 默认未启用。
2. 在「目标」添加本次交付目标与可检查的验收条件。长期目标提供方向，显式 Goal 提供本次完成合同。
3. 启用 Agent。编排最多拆分三个顺序工作包；末个工作包保留原 Goal 的全部验收条件。
4. 在「执行」查看 Session、验收结果和 Session 选择依据；在「学习」查看固定快照评估、已接受记忆及阻塞原因。

周期职责必须显式配置间隔及验收条件。每个职责同时保留一个未解决的周期目标；停机错过的窗口合并到最新窗口，不补跑全部历史窗口。单纯设置「检查间隔」不会凭空创建目标。没有就绪目标时记录 `no_action`，不调用规划模型。

API 中的 Role 示例（替换为真实目录）：

```json
{
  "command_id": "create-research-agent-1",
  "role": {
    "name": "研发搭档",
    "role_text": "维护设计与实现的一致性。中文输出，结论附依据。",
    "workspace": "/absolute/path/to/project",
    "long_term_objectives": [{
      "id": "design-quality", "desired_outcome": "检查设计与实现的差异", "mode": "ongoing",
      "schedule": {"every_seconds": 86400, "acceptance": ["报告有来源引用的差异；无法核实的内容标明未知"]}
    }],
    "source_routes": [{"id": "implementation", "topic": "实现事实", "paths": ["src", "tests"]}],
    "methods": ["先检查实现，再建议修改设计"],
    "timezone": "Asia/Shanghai", "interval_seconds": 300,
    "budget": {"daily_tokens": 1000000, "goal_tokens": 100000000, "work_tokens": 100000,
               "learning_tokens": 250000, "max_calls": 2000, "output_tokens": 8192}
  }
}
```

`POST /v1/agents` 创建；`POST /v1/agents/{id}/goals` 接收 `command_id` 和 `goal: {objective, acceptance}`。所有请求沿用 `Authorization: Bearer <service credential>`。Role 的结构化字段由用户提供并通过 Host 校验；系统保存原文、显式字段与默认派生字段，不假称已从任意自然语言可靠抽取完整权限。

## Session 复用与合同

只考虑同 Agent、兼容目录/模型/知识库/工具、空闲且没有人工输入或恢复待办的 Session，再按目标词汇相关性选择。选择依据保存在 routes。新工作包创建独立运行和验收关联，历史上下文可供参考，不能继承上次的验收通过结论。

每个 Agent 默认只推进一个活动工作包。自主和人工执行共用现有 workspace 读写仲裁；人工排队优先，自主等待超过五分钟后提升优先级。副作用未知仍使用现有 recovery 流程，不能通过 Agent 重派发跳过对账。

「接管 Session」阻止后续自主路由和 Agent 控制命令干预该 Session；当前执行仍保留原工作包与预算身份，执行控制在 Sessions 中操作。「归还」恢复后续路由资格。已结束的 Agent 工作之后发送独立人工消息，会开启普通人工运行，不重开旧工作包。

暂停先持久化意图，再通过控制 outbox 通知 Session，界面显示是否仍在等待停止。暂停或取消 Goal 不改变已发生的工具事实。Role 修订保留旧版本；执行环境仍固定于原合同，新的明确指导在下一合法模型请求边界更新，来源权限撤销在新工具操作准入时检查。待派发工作遇到 Role 版本变化会阻塞，需修订 Goal 或创建后继目标。

## 自动学习与人工标注

每个绑定执行的 `completed`、`failed`、`stopped` 终态与学习待办在同一事务中提交。暂停、等待输入和恢复不触发终态评估。学习固定终态事件游标，先完成 factual analysis，再针对原 run 执行 `goal_graph_v1 + extract_memory`；即使 Session 已开始下一轮，也不把后来的事件加入旧快照。

评估保留已有 Deep eval 检查点。状态与质量分开：`completed + partial` 表示处理完成但仍有缺口；无可分析证据为 `not_applicable`。学习额度不足时，LearningCoordinator 根据结构化预算拒绝事件自动申请当天额度并续跑同一 evaluation，不重跑源任务，也不自动重试已结算的质量缺口。达到自动扩容上限或每日总量／调用次数上限后，延后至所配置时区的下一日。Goal 累计预算、配置冲突、评估自身资源上限等需要人工处理后重试。API 重试可带 `limits: {max_calls, max_seconds, max_tokens}`，仅允许现有评估协议支持的资源额度变更。

记忆仍限制在同 workspace；没有 workspace 时只允许同 Session。人工可认可、拒绝、更正、限定范围、打标签、置顶、停用和恢复。标注为覆盖层，保留原始提取和 trace；更正标为 `human_asserted`，认可不会将推断改成观察。相同来源再次提取不能绕过拒绝。摘要注入、memory_search/read 和页面共用有效视图；源删除后失效。

「学习」统一组织待人工决策、已学知识与审核、人工原则与方法，以及学习记录与追溯。「工作方式」保留角色定位与角色配置入口。已学知识默认列出当前 workspace 中的有效事实、经验和方法，可按类型、审核状态和关键词筛选并分页；没有 workspace 时必须选择一个归属当前 Agent 的 Session。每条内容可展开原始引用、适用条件、方法步骤与验证状态，不再仅展示与角色描述匹配的少量检索结果。

未审核内容保留评估给出的 `confidence`。人工确认或更正后，覆盖层增加 `human_review`、`review_state`、`effective_confidence=high` 与 `confidence_basis=human_review`，检索优先参考相关的人工审核内容；原评估置信度、引用与原始记录保持不变。这些字段同时进入 context、memory_search/read 和页面。添加标签、限定范围或置顶本身不等于确认内容有效。人工审核提高的是历史参考的可信度，不表示当前任务已通过验收，也不会将推断或提案变成已观察的执行结果。

「人工原则与方法」支持仅当前任务或长期指导、停用/恢复、手工方法文本与工作目录内 UTF-8 文档导入。文档按当时内容形成不可变版本，后续文件修改不会悄悄改变已导入方法。方法复用现有 Skill 版本存储，采用状态为 `user_guidance`，不伪造可执行验证或 active Skill。

执行中的决策入口复用 `request_input(question, options)`：当重要选择缺少用户偏好或约束时，执行器提交具体问题及方案，学习页展示所属 Session 的待答问题。用户可选择建议项并补充说明，点击提交后通过原有 `answer_input` 命令恢复当前任务；问题 ID 和任务版本用于拒绝过期回答。回答默认作用于当前任务，不自动升级为长期原则。涉及工具副作用不确定的 recovery 问题仍在 Session 页面核实处理。Analysis 中的替代方案、改进建议属于事后复盘，可以作为人工原则的材料；当前不会自动将这些历史建议转成阻塞执行的待答问题。

实际生效的 ContextSnapshot 可在「学习」查看。运行中的指导不会改写已经发送的模型请求或尚未完成的工具交换，而是在 `before_llm` 替换管理块并记录 `context.updated`。记忆检索只表示看到了参考；模型通过 `agent_record_use` 关联真实已完成动作时，记录模型声明的采用，仍不声称因果改善。

## 预算与恢复

「学习与 Deep Eval 预算」Block 可以设置初始学习额度及 `learning_policy`：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `budget.learning_tokens` | 250,000 | 每天初始学习子额度 |
| `learning_policy.auto_expand` | `true` | 允许宿主自动扩容 |
| `learning_policy.max_learning_tokens` | 初始额度的 4 倍，上限为每日总额 | 当天学习额度上限 |
| `learning_policy.max_expansions` | 2 | 同 Agent 每天最多扩容次数 |
| `learning_policy.max_resumes` | 3 | 每个 evaluation 每天最多自动预算续跑次数 |

每次扩容最多翻倍，仍受每日总额、Goal 累计额度和调用次数约束。只有允许的新额度足以容纳当前请求才提交扩容；未知用量的请求继续按预留额度计费。扩容记录保存在当天 allocation，不修改 Role 合同；跨日重新从初始额度开始。已有 Role 未提供策略时采用上述默认值；保存配置后按显式值执行。

额度、续跑意图和审计事件原子保存，Deep Eval 按意图 ID 去重，重启或回执丢失不会重复提交。恢复沿用快照、evaluation ID、检查点和已发布 memory；后续调用仍逐次经过 BudgetLedger 准入。如果续跑后再次预算失败且没有新的已完成工作，则停止自动续跑并显示 `LEARNING_NO_PROGRESS`；达到当天续跑次数上限显示 `LEARNING_RETRY_LIMIT`。暂停、取消及角色配置变更会在准入时重新检查。

概览显示当天实际学习额度和扩容历史；事件流记录 `learning.budget.expanded`、`learning.resume.scheduled` 与学习状态变更。旧版因学习额度延期的任务会在加载新策略后重新评估一次，识别到受支持的宿主预算错误后自动恢复。

概览的「调整预算」或「工作方式 → 修订角色与预算」可设置每日总量、单工作包、单目标累计额度及每日调用次数。新 Agent 的工作包默认额度为每日总量的 1/10（向下取整，至少 1 token），每日调用次数默认 2000；显式填写的额度优先，已有配置不会被默认值覆盖。选择「每日总量的 1/10」时，保存为按当前每日额度计算出的具体数值；以后调整每日额度可再次选择按比例计算。

单目标、每日总量和工作包额度同时约束请求。单纯提高工作包额度不会提高单目标累计上限。仅修改预算不会使待派发工作包因 Role 版本变化而失效；已创建 Session 的 token 上限仍需暂停后通过 Session 的预算设置同步调整。

单目标累计额度默认 **1 亿 tokens**，显式配置的旧值保留。一个 Goal 可拆成多个工作包：Goal 额度统计该目标的规划、所有工作包的执行与验收，以及学习；工作包额度只统计该工作包的执行与验收，不包括学习。重试或复用 Session 不会清零这些累计用量，跨日也不会重置。额度是同时检查的上限，不是给每个工作包预先分配的余额。例如 Goal 为 1 亿、每个工作包为 1000 万时，多个工作包共同使用 Goal 的额度，还需为规划和学习留出空间。

预算拒绝信息会逐项列出超限范围（`daily` / `goal` / `work` / `learning` 或每日调用次数）、配置上限 `limit`、已占用 `used`、实际结算 `actual`、未结算或未知用量预留 `reserved_or_unknown`、本次申请 `requested`、预计合计 `projected`、可用余额 `remaining`、超出量 `exceeded_by`，并注明目标/工作包 ID 或日期窗口。`requested` 是发送前的保守预留，不是已经消耗的 token；被拒绝的调用不会记账。Goal 与工作包是累计限制，不会在次日重置。

规划、执行、验收和自动评估的每次模型请求都先预留 token，再根据真实 usage 结算。无 usage 或请求结果不明保留未知用量预留，不能算作零。每日额度按配置时区分窗，Goal/工作包累计消耗跨日保留；学习使用独立日子额度。没有可靠价格时只展示 token、调用数与未知项，不显示虚构费用。

估计采用请求字节数和输出上限，偏保守。已有 TaskSpec 和 evaluator 的时间/步数限制仍然生效。未知用量可能需要增加合同预算，系统不会自动释放未知预留或推定外部请求没有发生。

验收的单次输出和时间限制独立于 Goal 总量。Qwen 3.8 Max 开启思考且未显式配置 `thinking_budget` / `reasoning_effort` 时，验收请求默认将思考额度设为有效输出上限的一半，为最终判定留出空间；例如输出上限 8192 时，思考额度为 4096。显式模型配置保留，执行模型配置不变。验收超时会记录阶段、模型、实际请求时限与输出上限；暂停后 Resume 可续用现有检查点并更新验收分配。

同一任务在失败后 Resume，即使生成新的 run ID，也会保留最后检查点中的已观察结果、工具证据与执行快照；新任务或显式重新开始仍使用新的执行状态。验收阻塞后 Resume，如果原计划条目已全部结束，会保留历史条目并增加一个进行中的验收修复步骤，允许补读文件、补充证据和修复交付物；最终仍需通过原验收条件。Session 的运行失败和暂停原因显示在 Processing 内，长原因展开查看。

command ID 支持相同输入重放、不同输入冲突；人工修订携带 expected revision。Session 派发与所有权绑定、工作包、receipt 和 outbox 原子提交。后台在重启时对账终态并恢复待办。学习积压达到 20 项时暂缓派发，处理失败、取消或完成学习后可恢复推进。

## API 入口

| 路径（相对 `/v1/agents/{id}`） | 用途 |
| --- | --- |
| `commands`、`role/revisions`、`role-revisions` | 控制与 Role 版本 |
| `goals`、`goals/{goal}/commands` | 目标及修订、暂停、取消 |
| `runs`、`work-packages`、`routes`、`sessions` | 编排和执行关联 |
| `sessions/{session}/commands` | bind / takeover / release |
| `learning`、`learning/{cycle}/commands` | 学习进度、retry / cancel |
| `memory`、`memory/library`、`memory/annotations` | 运行时检索、分页审核知识库、标注与标注历史 |
| `guidance`、`profile`、`profile/commands`、`methods/import` | 明确偏好、方法版本与采用 |
| `context`、`context/{snapshot}`、`artifacts/{digest}` | 实际输入及归属校验 |
| `events?after={cursor}` | 顺序事件追赶，每次最多 200 条 |

历史列表支持 `offset` / `limit`（最多 200），返回 `next_offset`。页面当前按快照轮询，服务事件游标可供其他客户端增量追赶。

`memory/library` 支持 `query`、`kind`（facts / lessons / workflows）、`review`（unreviewed / reviewed）、`offset` / `limit`（默认 20，最多 50）；无 workspace 时传 `session_id`。扫描仍限制为最近 500 条范围内的有效来源，并通过 `truncated` 标识。Agent 快照中的 `pending_decisions` 提供实际待答问题及任务版本，回答沿用 `/v1/sessions/{session}/commands`。

## 验证与当前范围

无需外部模型的完整演示：

```bash
PYTHONPATH=src:. uv run python tests/autonomy/smoke.py
uv run pytest -q tests/autonomy tests/service/test_background.py
npm --prefix tests/web test
```

smoke 使用真实 HTTP、SQLite、执行 worker 和 Deep eval 管线，模型判断由可控 fixture 提供；它验证编排、身份、恢复和注入，不证明真实模型判断质量。`--serve` 可保留临时演示用于浏览器检查，Ctrl-C 关闭并验证重启恢复。

基础执行环境仍为本机 native_os；持续学习、多 Agent 协作与隔离并发的支持范围和显式开关见下文。

## P4～P5：持续迭代与协作扩展

目标推进支持两种模式：原有 `iteration` 由观测和来源事件派生目标；新增 `stage_planning` 从长期目标生成近期阶段、拆分执行并在验收后推进下一阶段。后者默认关闭，详见下文和[设计第 23 节](superpowers/specs/2026-10-25-autonomous-agent-design.zh-CN.md#23-长期目标驱动的阶段规划与自动推进)。

长期目标可配置 `iteration: {acceptance: [...], max_depth: 2, review_seconds: 86400}`。
新的人工观测或绑定来源变化会生成有来源的 GoalProposal；相同对象和证据版本去重。
`met/gap/unknown` 保留原始来源。持续目标到期复核，有限目标不会仅因时间经过重新打开。
目标选择综合优先级、截止时间、依赖影响与等待时间，Goals 页面显示选择或阻塞原因。
失败 Goal 不改写终态；`goals/{id}/retry` 必须提供 `change_kind`（input/method/environment）、
`change` 和 `expected_revision`，产生有血缘、指数退避且次数受限的新 attempt。

偏好通过 `preferences/feedback` 提交。`kind=explicit` 是明确长期指导；推断候选必须引用
该 Agent 所有 Session 的真实 `producer=human` 消息，不按消息 role 推断用户身份。
独立 Session/episode 才增加支持数，重复反馈或 evaluation 不增加支持。
Supervisor 有界扫描真实人工消息中的明确未来指导（如“以后”“今后”），只生成待审候选；不会将普通单次任务自动升级为长期偏好。
`preferences/commands` 支持 activate/trial/once/lock/unlock/retire/restore，带 entry_id 和 expected_revision。
试用需要两个独立反馈且无未解决反例，七天后复核；锁定条目不会被推断覆盖，停用条目不被扫描复活。
Profile 页面可查看共享范围、冲突、到期时间并执行采用/试用/停用。
采用只改变指导，不增加外部写权限。`habits` 可将已采用偏好绑定到已有允许 iteration 的职责，
至少每小时一次；Host 将工作包编译为只有文件读取和任务控制的独立环境。

Role 的 `owner_id` 默认 `local`。同 owner 可共享偏好，优先级是当前任务 > Agent 局部例外 > owner。
经验 memory 的 workspace/Session 范围不变。多个 Agent 不自动共享 Session；显式
`sessions/{id}/commands` 的 transfer 需要源和目标 revision，且 Session 必须空闲、无预留、目录兼容。
暂停、取消和预算仍分别归属对应 Agent。调度按上次服务时间轮转；目录父子路径存在写冲突时不能并行。

来源路由可设置 `refresh_seconds`、`objective_id`、`fallback_route_ids`。
`sources/refresh` 立即检查已有绑定，定时检查由 Supervisor 驱动。保存文档摘要、版本、成功版本与覆盖缺口。
现有 sqlite_fts 本地文件索引可增量刷新并传播删除；不会复制新知识库。
本地源文件改变或删除后，旧索引段落先停止返回。需要最新事实的 Goal 可声明 `required_source_ids`，
规划和派发均检查当前版本或显式替代来源；其他无依赖工作仍可继续。
带 embedding/模型调用但没有逐调用 Agent 计量适配的后端不会自动刷新；绑定外部工具在使用时核验，
不会自动创建连接或权限。历史成功版本保留供解释，不能作为当前事实的唯一依据。

`external-events` 接收绑定来源的通用事件：source_id、event_id、object_id、整数 version、occurred_at、
cursor、origin、causation、depth、type（changed/deleted/revoked）、summary 和 command_id。
持久 inbox 处理重复、乱序、撤销和重连重放；第一适配器是现有来源版本扫描。
自身执行期间/终态带来的来源变化保守抑制，不自动回环生成同类目标。该策略可能合并并发外部变化，
需要明确人工观测时可调用 `observations`；不会假称能识别任意外部系统的写入来源。

方法反馈关联 context 中的方法/记忆、工作包、模型声明、真实工具调用和验收结果。
`methods/feedback` 区分明确未采用、失败、采用和 unknown；没有采用记录本身不证明未采用。
Role 的 `method_learning=true` 开启终态学习后的有界 Skill 归纳；默认关闭。
也可通过 `methods/evolve` 显式启动，所有模型调用进入 Agent BudgetLedger。沿用冻结案例、反例与验证发布门槛。
Host 在请求发送前拒绝预算时进入 deferred，不计为已发送或未知费用；额度配置或本地日期窗口变化后最多自动续跑三次，保留已有归纳检查点。
跨项目推广使用 `methods/promote`，指定源版本/revision、目标 Agent、conditions 和冻结 environment digests。
它创建目标 workspace 的新 candidate，不共享原始 trace/memory，不继承已验证状态；原版本停用使依赖候选失效。
真正启用仍要求现有 SkillPairedValidation 的独立 family/holdout、真实执行、目标环境和验收证据。
反馈报告不把一次成功、引用次数或重复家族声称为因果改善。

### 从长期目标自动规划阶段（P4.5）

让服务加载新代码后刷新页面；已有服务进程需要沿用原数据目录、模型配置和启动参数重启。功能不会自动开启旧 Agent。

以“研发搭档”为例：

1. 在 **工作方式 → 修订角色与预算 → 自主阶段规划** 选择自动推进，设置长期目标累计额度、阶段约束与完成条件。也可在 **目标 → 阶段路线图** 对某个长期目标单独开启；该入口默认累计额度为 1000 万 tokens，包含可归属的历史调用。
2. 保留长期目标“从太阳系逐步扩展到银河系、整个宇宙”，目标类型使用持续维护；约束可填写“先形成设计与细粒度实施计划，再修改实现；每步完成后验证”。有限目标应提供最终完成条件，缺失时会出现具体待答问题。
3. Agent 首先复用新鲜的实际交付证据；没有证据或基线已过期时，生成只读调查 Goal。调查失败会保留原失败，不反复创建同一调查。
4. 阶段规划器生成具体近期交付、验收与子 Goal，现有规划器继续拆成工作包执行。模型可提出暂定远期方向，但只有近期阶段会激活；下一阶段由完成验收后的复盘触发，无需手工添加短期 Goal，也无需等待普通六小时检查周期。
5. 在目标页查看基线、当前阶段、关联 Goal、未来概要、复盘和累计用量。可以暂停/恢复、说明原因后跳过/取消、修订近期交付与验收，或根据新信息重新规划。修订有在途执行的阶段前，先完成或取消现有工作；不会原地替换正在执行的合同。

目标页展示当前/最近阶段与上一阶段的目标名称、状态、工作包和 Session 入口，点击目标名称跳到完整目标合同；默认展开当前与上一阶段，保留重试前的历史 attempt。执行页保留全部工作包与 Session 关联，包括没有阶段归属的历史工作。概览只展示运行摘要与调度时间，不重复展示阶段执行详情。

自动推进沿用当前权限和任务执行环境。阶段规划不会自行增加外部工具、发布权限或预算。与授权或交付范围有关的重要缺失选择显示为阶段待答问题；问题和版本由 Agent 保存，回答不会创建伪造的 Session。

Role 中每个长期目标可以配置：

```json
{
  "stage_planning": {
    "mode": "autonomous",
    "objective_tokens": 10000000,
    "horizon_stages": 3,
    "max_goals_per_stage": 3,
    "max_new_stages_per_day": 3,
    "max_replans_without_progress": 2,
    "review_seconds": 86400,
    "constraints": ["先完成设计与实施计划，再修改代码"],
    "completion_criteria": ["交付具有实际执行和验收证据"]
  }
}
```

这是长期目标对象内的配置片段，Role 修订需提交完整 Role。已有 `iteration`、周期职责、来源事件与习惯在阶段模式下汇入规划输入，不再各自复制同一职责的 Goal；未开启阶段模式的目标保留原行为。已有人工目标先完成或解除阻塞，再规划新的阶段。

默认每阶段最多 3 个 Goal、每个 Goal 最多 3 个顺序工作包。最后一个 Goal 保留全部阶段验收，工作包最后一步继续保留 Goal 的完整验收。阶段完成还必须经过逐项证据复盘；仅运行 completed 或 Deep eval completed 不等于阶段验收通过。

阶段规划和复盘的结构、证据引用与依赖语义校验共用有界纠错流程：首次响应不合规时，将具体错误与原提案交给模型修正一次；每次调用仍先检查并计入预算。当前阶段在远期依赖中固定使用 `current`，其他依赖只能引用更早的远期阶段 key；Goal 依赖只能引用本阶段更早的 Goal key。不能用标题、交付键或数据库 ID 替代这些 key，也不允许重复、自依赖和前向依赖。拒绝的提案保存为 artifact，`stage.protocol.*` 事件记录关联规划 run、尝试次数与校验错误；只有完整通过校验的提案才能提交。纠错耗尽后阻塞，预算不足则延后，网络错误和模型拒绝不会作为格式错误重试。修复协议或补充有效输入后，可通过目标的“重新规划”入口恢复，历史失败和已消耗预算保留。

失败复盘可以选择有明确方法变化的有限后继 attempt，保留原失败，第一次退避 60 秒，后续指数增加。相同修复不重复提交；达到无进展上限需要新输入。来源被撤销或必要来源改变时，停止后续派发并显示需要复核的合同，修订或重新规划后才继续。终态合同不能改写，跳过不算验收通过。

Deep eval incomplete/partial 或学习预算 deferred 会单列为学习缺口；业务验收通过且没有必要学习依赖时可以进入下一阶段。达到既有 20 项学习积压门槛时仍会暂停新阶段，明确显示积压原因。方法归纳与 Deep eval 共用学习子额度，长期目标累计额度还覆盖规划、执行、验收和学习；换阶段、重试和跨日均不清零累计用量。

API 路径相对 `/v1/agents/{id}`：

| 路径 | 内容与版本 |
| --- | --- |
| `GET objectives/{objective}/roadmap` | 当前路线图与 planning 状态，首次未规划时也返回可操作的初始状态 |
| `GET stages`、`GET stages/{stage}` | 阶段、Goal 关联及复盘记录 |
| `POST objectives/{objective}/planning/commands` | enable/disable 使用 Agent revision；pause/resume/replan/answer 使用 planning revision |
| `POST stages/{stage}/commands` | pause/resume/skip/cancel/revise 使用 stage revision |

命令均包含 `command_id`、`type` 和 `expected_revision`。重新规划、跳过、取消和修订需要 `reason`；修订的 `payload` 为 `{expected_outcome, acceptance}`。回答需要 `question_id`、`question_revision` 和 `answer`。重放相同命令不会重复执行，过期回答或 revision 冲突会拒绝。enable/disable 与 Role 的 stage_planning.mode 是同一配置。

阶段路线图以不可变版本保存。模型有效响应先持久化，再原子提交阶段与 Goal；重启优先复用已记录响应。已发送但未记录响应的调用保持未知费用，等待带原因的重规划，不自动重发。文件来源与模型语义质量仍需实际核实；结构/依赖/范围校验及文本相似度检查不能证明模型选出的阶段就是最优路线。

### 执行扩展的开关与限制

仍默认使用原只读 barrier。显式 TaskSpec 可配置：

```json
{"workflow":{"plugin":"adaptive_dag","parallel":{"mode":"pipeline","isolated_writes":true,"max_lanes":2}}}
```

该片段需与实际 session_environment、tools 和 nodes 合并。`pipeline` 是有界滑动窗口：
一个节点完成后，其依赖节点可先于其他慢分支启动。`execution_hint=main_candidate` 允许明确独立的只读
主线节点参加同一调度器并复用 main context；一般主线工具和最终验收仍在稳定边界串行执行。
`isolated_write=true` 的节点只支持 read_file/write_file，由 Host 操作独立副本；禁止 shell、网络、连接器和进程。
节点可指定服务已配置的 `model`，恢复必须匹配原模型配置摘要，费用仍归所属 Agent/Goal/WorkPackage。

写副本最多 2000 个文件、单文件 2 MB、总计 20 MB，不支持符号链接、删除、Git 操作和外部副作用。
副本在运行期不直接影响主目录；集成队列核验基线、逐文件持久 journal，并使旧验收失效。
重复集成幂等，中途退出会核对 before/after 摘要；主线基线变化或多个写分支相冲突时暂停，保留副本，
需要人工检查后取消旧合同并以新基线重建，首版不自动三方合并。
分支存储必须位于源 workspace 外；服务数据目录若位于源 workspace 内，将拒绝写分支。
CLI 的临时副本会随检查点记录路径；临时目录被系统清理后不能假装恢复成功。
native_os 并未因此成为完整 OS 沙箱。设计与恢复协议见
[执行扩展子设计](superpowers/specs/2026-10-30-autonomous-agent-execution-extensions.zh-CN.md)。
