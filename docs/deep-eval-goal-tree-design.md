# Deep Eval 与 Goal Tree：当前实现设计

日期：2026-10-20。状态：按当前源码整理的实现基线；结构、恢复和展示机制已有回归，
目标拆分与贡献判断的模型语义质量仍需校准。

本文是 Deep Eval 分析过程和 Goal Tree 展示方式的统一入口。
当前 `loom_py` 仓库的页面适配见第 14 节：以 `src/loom/web/assets/views/` 和
已有 `goal_graph_v1` 结果为实际入口；本文其他章节中的 `evolve/`、`assets/evaluation/`
路径及协议版本不因本次页面改造而自动成为当前仓库的实现。
[run_40b063 目标树核对](./deep-eval-run-40b063-goal-tree.md)保留完整逐调用人工样例；
本文解释系统如何生成、保存、校验和展示这种分析，以及当前实现的边界。
历史方案以文末链接为准；其中的旧页面、旧协议和旧测试次数不代表本文的当前行为。

阅读导航：

- 分析输入与增量协议：第 3–4 节。
- 目标、闭环、邻近归属与 Stage：第 5–6 节。
- 验收、成本、持久化与恢复：第 7–9 节。
- 页面及逐调用展示：第 10 节。
- 真实 run 核对与验证边界：第 11–12 节。

## 1. 目标与职责

Deep Eval 回答四个问题：

1. 本次 task 的目标是什么，实际行动追求了哪些 SubGoal，它们如何服务 Root？
2. 每个 SubGoal 经历哪些方法尝试和 Stage，逐个 model/tool action 做出了什么贡献？
3. 目标是否形成“目标—方法—行动—结果—反馈”的闭环，哪些仍停留在声明或未知状态？
4. 结论来自哪些原始记录，执行消耗和分析自身消耗各是多少？

默认结果是可展开的 `Root → SubGoal → Stage → Action` 树。树服务于按目标阅读，
原始时间位置、共享证据、因果关系和验收回执仍保留在结构化结果中。
分析不重放业务工具，不重新执行被分析 run，不修改其交付和验收结果。

三个职责分别是：

| 职责 | 来源 / 执行者 | 权限与限制 |
|---|---|---|
| 原子事实、调用配对、范围引用、成本 | 宿主确定性代码与原始 trace | 不依赖评审模型猜测 |
| SubGoal、方法、贡献、采用、闭环和依赖 | 分段评审模型，受宿主协议校验 | 必须有引用；允许 unknown；不是任务验收授权 |
| Root 验收结果 | 原 run 的 TaskLoop、verifier 与宿主回执 | Deep Eval 只解释记录，不能另行宣布 accepted |

`finish` 表示提交候选，实际验收由执行框架自动调度。答案验收工具在没有候选时
提示先提交；这些是被分析系统的运行规则，不是 Deep Eval 的补证操作。
参见[统一 Acceptance Review](./unified-acceptance-review.md)及
[任务验收规格](../product-specs/agent-acceptance.md)。

## 2. 实现入口与版本

| 层 | 当前标识 / 代码 | 职责 |
|---|---|---|
| 服务 | `loom.service.trace-analysis.v6`；[trace_analysis.py](../../src/loom/service/trace_analysis.py) | 固定来源、任务队列、缓存、取消、恢复、结果读取 |
| 默认引擎 / 结果 | `goal_loops/v1` / `loom.goal-loop-analysis.v1` | 与旧 `behavior_v3` 并存 |
| 输入特征 | `goal-features/v7`；[features.py](../../src/loom/evolve/goal_loops/features.py) | 本地特征、持久元数据、最近 SubGoal 与阶段前沿 |
| 模型增量协议 | `compact-delta/v4`；[delta.py](../../src/loom/evolve/goal_loops/delta.py) | 紧凑返回展开为规范 GraphPatch，宿主管理 occurrence IDs |
| 阶段投影 | `execution-overview/v2`；[overview.py](../../src/loom/evolve/goal_loops/overview.py) | 相邻执行片段、报告依赖图、旧结果分组 |
| 目标树投影 | `goal-tree/v3`；[tree.py](../../src/loom/evolve/goal_loops/tree.py) | 按目标组织、Root 属性、邻近归属、唯一调用统计 |
| 页面 | [goal-loops.mjs](../../src/loom/web/assets/evaluation/goal-loops.mjs)、[goal-tree.mjs](../../src/loom/web/assets/evaluation/goal-tree.mjs) | Skeleton / Acceptance / Report 与逐 Action 展开 |
| 分析过程区 | [process.mjs](../../src/loom/web/assets/evaluation/process.mjs)、[app.mjs](../../src/loom/web/assets/app.mjs) | History、实时进度、使用量、恢复控制 |

版本分别描述服务、分析协议和展示投影，不能用“页面已升级”推断已对旧数据重新评审。
本次文档整理不发起新评估，也不更改以上版本。

## 3. 从 run 到结果的完整流程

```text
选择 terminal run，固定 session cursor / source digest
  → 还原完整 observation 与来源缺口
  → EvidenceIndex：原子调用、证据卡、验收事实、原始范围索引
  → FeatureIndex：短特征、引用句柄；正文留在本地
  → 有界窗口 + 相关目标元数据 + 前序边界 + 补证信号
  → 评审模型返回 compact delta / GraphPatch
  → 规范化、引用/覆盖/目标/时间/DAG 校验
  → 原子提交 graph revision + checkpoint + 累计请求账本
  → 重复后续窗口；失败有界修复，缺口保留 unknown
  → 同源投影：Goal Tree / Overview / Acceptance / Costs / Markdown
  → 保存结果；页面轮询展示 partial 或完成结果
```

### 3.1 固定输入快照

服务只接受 `finished / stopped / failed` 的 run。发起时固定 session event cursor，
读取该 run 的事件信封，生成 `source_digest`，保存来源 artifact。详细 payload 被
拆成 `event_detail` artifact 时先还原；缺失 artifact 保留 compact preview 并记录缺口。

重建时排除 `llm_response_chunk` 等流式分片，以完整 request/response 作为事实来源，
避免把同一模型输出计算多次。保留 `service_seq`、源 event ID、run/loop ID；
只有 task/trial 等身份在记录中唯一时，才补齐缺少的身份字段。

因此有三种数量，不能混用：service 事件数、非流式 observation 数、分析 card 数。
页面进度使用 card 数，而不是把所有 service 事件都送给模型。

### 3.2 原子调用与证据卡

[evidence.py](../../src/loom/evolve/goal_loops/evidence.py)复用
[analysis_facts.py](../../src/loom/evolve/analysis_facts.py)配对调用。
原 event ID 按 run/loop 隔离，避免跨 loop 同名冲突。

| 对象 | 内容 | 用途 |
|---|---|---|
| Atom | 唯一调用 ID、model/tool、名称、请求/响应行、attempt、usage、duration、outcome | 执行调用数与成本的计量单位 |
| Card | `id/kind/name/role`、时间范围、atom IDs、packets、visibility、facts | 逐卡语义评审单位 |
| Evidence ref | source digest、record 行、字段路径、字符范围、service seq、event digest | 指向不可变来源片段 |
| Record index | 所有非流式记录的行、kind、card 映射、service seq、角色 | 保留不送入模型的上下文与原始入口 |
| Root / receipt / condition | 任务文本、配置、条件、候选和宿主回执 | 独立于模型判断的根目标与验收事实 |

一对 tool request/result 是一次 invocation、一个 atom；不是两次工具调用。
verification_result、task_decision 等没有调用 atom 的回执仍可形成 card。
`working_memory.*` 框架记录保留在完整 record index 的 context_management 类别，
不全部复制成业务目标卡。失败调用也保留，不能只扫描成功路径。

若请求和响应来自不同 clock/actor，无法可靠相减时 duration 为未知。
未记录 token、未记录响应和未记录耗时都有显式缺口，不用零代替。

### 3.3 引用格式与可见性

引用定位为：

```text
source_digest + line_number + field_path + [start, end)
                         ↕
              service_seq / event_digest
```

例如 `E139 · 0–248` 中，`E139` 是 service event 序号，`0–248` 是该字段的字符范围，
不是 trace 的第 0 到 248 行。`line_number` 是固定来源快照中的 observation 行号。
没有 service seq 的底层记录可按 source line 定位。字段路径可进入 JSON 编码的
arguments/result，原始来源不会为方便显示而被改写。

必须区分：

- **Captured**：工具结果已被宿主捕获。
- **Presented**：后续某次模型请求中存在该工具消息；可能只有 preview/ref。
- **Adopted**：后续行动或声明显示模型采用了信息，是需要证据的贡献判断。

跨 loop 连接工具消息时，只有该 run 中 tool_call_id 的生产者唯一才关联。
同内容呈现会去重，窗口只能看到其时间边界之前已发生的呈现。
捕获全文、显示了某片段、模型实际采用三者不互相等价。
相同调用、捕获前缀重叠、重复分页只是冗余候选；需结合新信息、失败重试和实际可见范围评审。

## 4. 有界增量分析

### 4.1 本地特征与持久分析上下文

FeatureIndex 不把源码、文档和完整命令输出逐批重新塞进模型输入：

- 工具卡提取 operation、路径、查询、offset/limit、shell 操作符、结果长度和错误信号。
- 源码结果提供有界 symbol inventory；命令结果可提供精确的 indexed、URI、错误等短信号。
- 模型公开 content 提取意图/转折语句；长文本引用保留精确来源范围。
- 本地保存完整来源，短特征携带可定向读取的 ref；不让模型凭关键词直接认定成功。
- 持久 `context_metadata` 保存目标状态、问题、资源线索、stage 产出和 previous_subgoal。

每次只发送与当前批次相关的 metadata view：状态视图上限取
`min(max_state_chars, 2800)` 字符，stage_frontier 上限为 `min(1100, max_chars / 2)`；
还可带最多两张前序边界卡，帮助解释紧接着发生的反馈和呈现。
边界卡不是本批待评审项。

当前语义输入主要使用记录的公开 `content`。只有 reasoning、没有 content 的模型卡
仍存在，但其语义特征可能为空；原始 reasoning 可经 Action 来源入口检查。
运行页的 reasoning 摘要回退不会自动把这些旧卡补成新的模型评审。

### 4.2 评审请求

请求包含 `root / state / cards / boundary_cards / supplemental_evidence`，
以及 `required_assessment_ids / batch_start / revision / review_control`。
格式修复另带 `repair / repair_details`，不重发整个历史图。

默认每批最多 10 卡；初始输入超限时先缩窗口。已经补证或进入修复的 pending 窗口
保留卡集合，压缩上下文和引用摘录，不能靠悄悄删除待评审卡满足预算。
截取后生成对应范围的新 ref，并标记未读内容。最小窗口仍放不下则保存 partial。

### 4.3 返回与图数据

存储层保留 `Goal → Attempt → Phase` 语义图；页面按 Goal 折叠成 Stage。

| 对象 | 主要字段与含义 |
|---|---|
| Goal | `id, parent_id, title, basis, root_relation, status, open_questions, closure, criterion_ids, evidence_refs` |
| Attempt | 某 Goal 的一种方法；`goal_id, title, evidence_refs` |
| Phase | 一次有时间位置的 occurrence；`attempt_id, card_ids, outcome, evidence_refs`，可附 `stage` |
| Stage annotation | `id, title, kind`；kind 为 research / investigation / execution / validation / delivery |
| Assessment | 每 card 的 `phase_ids, relation, expected, delta, adoption, redundancy, rationale, evidence_refs` |
| Edge | phase 间 `enables / feedback / verifies`；具体理由、可选传递产出 `output` 和引用 |
| Diagnostic | 有来源的过程问题与解释；status 为 inferred / supported / unknown |
| Patch history | revision、patch digest、原 patch，必要时记录宿主恢复来源 |

compact delta 允许局部 step label 和按目标组织的紧凑返回；宿主展开成稳定的
Attempt/Phase/Edge ID。已有 stage 作为依赖来源时解析到其最近的已提交 occurrence，
不能杜撰一个 phase ID。兼容规范 GraphPatch；root 回显被适配器忽略并留下 audit，
模型不能覆盖宿主 root。

每个 `required_assessment_ids` 必须恰好一个 assessment，不能跳过失败或未知 action。
后续证据改变过去的采用/贡献判断时，用 `assessment_updates`，每 patch 最多 8 张旧卡。
未被更新的历史判断保持不变，patch 历史保留修订依据。

### 4.4 宿主校验

[graph.py](../../src/loom/evolve/goal_loops/graph.py)在副本上完整校验，成功后一次提交：

- 本批覆盖无缺失、重复和越界卡；历史更新限量且只能指向已评审卡。
- ref 必须来自已交付的证据索引；有意义的贡献不能没有引用和 phase。
- Goal 必须有标题、服务父目标的解释、合法状态；父链最终到 root 且无环。
- 已存在 Goal 的 `title/parent_id` 不可改写；不同目标建立新 ID。
- 同父重复目标可以显式 merge，保留别名与旧目标；每 patch 最多 4 次 merge。
- Phase 引用真实 card，不借用尚未评审的未来 card；occurrence 和 relation 不原地改写。
- 因果边连接已知 phase，指向后发生的 occurrence，并通过 DAG 校验。
- `claimed/supported` 需要明确 closure 摘要；引用的 criterion 必须属于当时已记录的条件。

这些规则保证可追溯性和结构一致性，不是语义正确性证明。
要求 closure 涵盖目标、方法、结果和反馈，并在关闭时更新 open_questions，是模型协议；
宿主不会仅凭一段非空文字证明目标真实完成。closure 与问题并存时保留警示，不能自动清空问题。

### 4.5 补证、修复与缺口

模型可针对已知 ref 请求范围，或检索旧 Goal / Stage。每轮最多 4 个 read
（每个最多 4,000 字符）和 4 个目标查询；同一源范围不能靠更换 ref ID 重复读取。
先校验整组请求，再读取与计费。本地保留完整补证片段，当前特征模式向模型提供
与问题相关的短 signals 和引用，不能把读取字符数等同于传输了全文。

`review_control` 显式区分 review 与 conclude。达到补证轮数/字符限制，或需要把预算
留给后续未扫描卡时，关闭补证并要求就现有证据收尾。关闭补证不算格式修复。

无效返回最多再做一次格式修复；返回应是本批完整替换 patch。仍失败时：

1. 若仅是 assessment 覆盖问题，尝试 `host_reconciled`：相同重复去重、丢弃多余项，
   缺失/冲突项填 unknown，再执行全部图校验。
2. 其他结构问题或重校验不通过，`host_deferred` 整批保留 unknown，记录原因和问题。
3. 继续扫描后续窗口，不以一个局部格式错误阻塞整个 run。

长度截断、资源耗尽、超时等不能冒充有效 patch；保存 partial 和当前 checkpoint。
`complete` 只表示扫描结束，可能仍包含 deferred 或模型自己给出的 unknown。
恢复旧响应属于另一条路径：只解析已有完整响应，重新走全部校验，无新增模型调用；
若无可恢复判断，不能把恢复动作包装成已经解决语义缺口。

## 5. Root、SubGoal 与闭环

### 5.1 Root 的身份和属性

Root 使用记录的 task instruction，不能用模型概括替换真实目标。
任务文本被截取时展示提示和原文入口。任务要求、criteria 和 acceptance 配置归入
Root 的 **Task requirements 属性**；配置 card 仍计入 Root 覆盖，不显示成
“0 model / 0 call”的执行 Stage。

Root 同时展示 `Process` 与 `Host`：前者是分析图中的过程状态，后者来自 task_completed
或 task_decision。宿主 accepted 不把所有 SubGoal 自动改成 supported。

### 5.2 SubGoal 的划分

SubGoal 是实际追求的结果，不是工具名、源码符号、一次模型调用或一个分析窗口。
它可以来自原生 plan/goal 事件，也可以依据公开意图与实际动作推断。
每个 SubGoal 必须说明 `root_relation`，即怎样服务父目标；允许多层子目标。

使能目标和业务结果应分开。例如“确定外部文件入库的方法”支持“文档已入库”，
但理解 CLI 不等于入库完成。执行中重新尝试原目标，新增后续 occurrence；
不能把早先目标 ID 改成另一含义来承接完成证据。

状态包括 `open / claimed / supported / abandoned / unknown`。完成声明只是 claimed 的
依据之一；支持闭环需要结果和反馈证据。没有 open_questions 也不能反推闭环成立。

### 5.3 每个 Action 的贡献判断

| 字段 | 值 / 解释 |
|---|---|
| relation | direct / enabling / unrelated / unknown：对目标的关系 |
| expected | 当时预期带来什么进展 |
| delta（UI：Progress） | 结果实际增加了什么信息、状态或产物 |
| adoption | observed / inferred / unknown：后续是否采用 |
| redundancy | none / possible / supported / unknown：是否存在有证据的重复 |
| rationale / refs | 判断理由及来源 |

Expected / Progress / Adoption 是评审模型基于工具参数、结果、公开模型内容和后续证据
形成的判断，不是 tool_result 自带字段。宿主兜底的未知项另有来源说明。
每个 model_call 和 tool_call 保留独立判断；Stage 摘要不能替代逐调用解释。

### 5.4 未明确归属时继承邻近 SubGoal

[attribution.py](../../src/loom/evolve/goal_loops/attribution.py)提供可逆的逐卡投影：

1. verifier、verification_result、宿主回执和验收配置优先归 Root。
2. 保存的明确 `unrelated` 判断阻止继承。
3. 有明确 assessment phase_ids 时，按 phase → attempt → goal 取归属。
4. 无上述明确判断的 `finish / submit / submit_result` 归 Root。
5. 其余未明确归属卡默认服务最近的前一个已知 SubGoal，标记 `inferred_context`。
6. 没有前序 SubGoal，则仍留在 Root 的未归属展示中。

按 first_line / last_line / ID 的时间顺序处理；连续推定卡沿用最初已评审锚点，
不会把上一张推定卡变成新的评审证据。多个显式目标时，选择其中首个非 root 目标
更新后续上下文。当前启发式没有额外按并行 lane 隔离，不能把时间邻近升级为因果关系。

新请求带有 previous_subgoal，模型可以根据内容作更明确的归属。
旧结果投影只用已有判断，不凭命令关键词发明“完全不关联”的语义例外。
推定不改 graph、closure、reviewed_cards、usage 或执行成本 owner，也不补造依赖边。
后续明确 assessment 优先；归属只读当前 phase_ids，不能从旧 phase.card_ids 恢复已撤销关系。

## 6. Stage 与树投影

### 6.1 两种阶段组织，保留不同阅读目的

**Goal Tree** 在同一 Goal 内按 `stage.id` 汇集已评审动作，可跨分析窗口；
没有 stage annotation 时使用 overview 的展示分组。Attempt IDs 保留在 Stage，
不另占一层默认导航。跨 Goal 使用不同树节点，即便有共享 card。

**Overview** 只合并相邻 occurrence；后面返回同类活动仍是新的时间片段。
它保留供报告/导出使用，Skeleton 页面已移除 Stage overview / Event DAG 切换。
因此目标树可以在同一 Stage 下汇集非相邻行动，overview 仍保留先后片段，两者不矛盾。

树内先列子 Goal，再列当前 Goal 的 Stage；分组内部保留来源顺序。
树的位置表达服务关系，不表达全局时间顺序。原始 first/last line 始终保留。

### 6.2 Stage 的来源

| basis | 形成方式 | 展示含义 |
|---|---|---|
| assessed_stage | 模型明确给出目标内稳定 stage annotation | 已评审阶段 |
| operation_grouping | 无 annotation 的旧结果，按保存的操作参数确定性分组 | 展示分组，没有额外语义评审 |
| inferred_context | 无明确归属，继承上一个 SubGoal | `Continue: <SubGoal>`，贡献仍可能 unknown |
| recorded | 契约、verifier 回执、根裁决等原始事实 | 宿主记录 |

旧分组根据实际 shell 操作和参数区分 research / investigation / execution /
validation / delivery，避免用 echo 标签或管道 head 改变主操作类别。
不能解析或没有目标上下文时显示 Unresolved events。
没有模型贡献评审的 Continue 阶段可以有确定的归属和原始记录，但没有编造的工作总结。

### 6.3 共享 Action 与根级 Stage

一个 card 可以服务多个 Goal：首个为 primary，其余为 shared reference。
不复制实际 invocation；各分支统计基于唯一 atom，引用复用不是新增调用。
全局提交和整合可以直接成为 Root Stage，不必人为制造“提交子目标”。

Root 验收模型与回执固定展示在 Root。旧 graph 如把根回执归入子目标，
Action 详情保留原判断并提示其不能独立关闭该子目标；不改写旧付费 graph。

## 7. Acceptance 与 Root decision

新默认执行框架把整体回答审阅与逐条件审阅合成一个 `loom.acceptance`
（`acceptance-review/v1`）Acceptance Review；独立命令、差分检查等仍可并存。
历史 run 中 `loom.response` 和 `loom.acceptance_rubric` 继续分别显示
Response review / Condition review，不能把两个历史模型调用合成一次。

Root decision 是宿主汇总和交付阶段；其中多条记录并不代表多次 verification：

| 记录 | 职责 |
|---|---|
| verification_result | 某 verifier 对自身 subject 的检查结果，可包含多个 criterion |
| task_assessed | 当前冻结候选与适用验证证据的聚合 |
| task_decision | 宿主接受、阻塞、耗尽或错误决定 |
| task_outcome | 任务结果 |
| task_completed | 任务执行完成记录 |

Root decision 本身没有因为这些回执而增加 model/tool call。
TaskLoop 决策还受候选身份、必需检查、终止声明和预算等约束；Deep Eval 不重新执行这些规则。

验收投影按证据身份连接：

- task_assessed / task_decision 中的 verification ID 必须找到先前回执，
  且 candidate.subjects 对应 verifier 的非空 subject 必须匹配。
- task_completed 可通过其显式 verification IDs 关联先前回执；
  这是宿主完成引用，不宣称额外检查了一次 subject。
- 条件检查要求 acceptance contract digest 与 criterion ID 匹配。
  多份回执有相反布尔结果时显示 conflicting，不随意取最后一个绿色结果。
- 缺失/不匹配 ID 保留 gap，不按邻近时间补成验收关系。

overview 中的 Candidate 是从后续回执提取的 subject 快照，虚拟排在审阅前方便阅读，
不是伪造一条更早发生的事件。审阅模型与紧邻回执的分组仅用于导航，
不构成 subject 匹配。两次 review 时间相邻也不等于前一次输出是后一次输入。

## 8. 调用数、Duration 与两套成本

### 8.1 执行成本

执行 atoms 来自被分析 run。`costs.owners` 使用唯一归属：
单 phase 归该 phase；多 phase 归共同祖先的 shared bucket；无 phase 归 unassigned。
`costs.goals` 向祖先累计，不让共享证据重复计费。
该成本账本以保存的语义归属为准，邻近展示归属不会重新分配费用。

树的 `N model / M tool · Duration: …` 表示当前分支所涉及的唯一 atom：
Root 包含全部后代，SubGoal 包含自身及下级目标，Stage 包含其行动。
同一 atom 在一个分支内只计一次；兄弟分支可以共享引用，不能直接相加。

Duration 是已记录模型和工具调用耗时之和，**不是 Goal/Stage 的墙钟时间**。
并行与嵌套可能重叠：

| 情况 | 显示 |
|---|---|
| 没有调用 | `—` |
| 有调用但全部没有计时 | `Unknown (N calls)` |
| 部分有计时 | 已知时长 + `N unknown` |
| 真正的零耗时 | `0 ms` |

这些指标无需模型总结。Stage 详情另列模型名称。
Markdown 的阶段表包含 Models / Calls / Duration；当前树形 Markdown 行只列调用数，
网页树标题已展示 Duration，不能把二者描述为逐字段完全一致。

### 8.2 分析自身成本与预算

分析请求另有累计账本，不能和执行 tokens 混算。
[contracts.py](../../src/loom/evolve/goal_loops/contracts.py)默认值如下：

| 设置 | 默认值 |
|---|---:|
| max_calls（物理请求，含传输重试） | 24 |
| max_tokens（charged 上限） | 240,000 |
| max_seconds（累计分析时间） | 600 |
| max_call_seconds | 90 |
| max_input_tokens | 18,000 |
| max_output_tokens | 4,000 |
| max_cards | 10 |
| max_state_chars | 12,000，当前发送视图另限 2,800 |
| max_read_rounds（每窗口） | 2 |
| max_evidence_chars（全分析） | 24,000 |

这些是运行边界，不是质量目标或最优配置。
请求前持久化 input + max-output 预留，收到 usage 后按实际值结算。
异常、中断、取消、重启后 usage 仍未知的请求保留预留；已收费不能因没提交 patch 消失。
`charged_tokens` 包含这些未知预留，`known_tokens` 是已知用量；
都不是按价格换算的货币账单。

输入 admission 使用 UTF-8 大小加 framing 的保守估计，不是精确 tokenizer。
为剩余基础扫描预留请求和预计 token，可提前关闭非必要补证。
取消/超时后迟到响应不能继续修改已经停止的账本和 checkpoint。

## 9. 保存、恢复、缓存与免费投影

每次提交保存 graph、cursor、revision、refs、已读范围、pending、deferred、
context_metadata、evaluator trace 和 usage。checkpoint 与 partial 结果均存为 artifact。
请求预算预留也会触发保存，以覆盖“发出请求但尚未提交”的中断窗口。

完整服务结果通过 `analysis.goal_loops` 返回；处理中通过 `partial_analysis` 返回
当前 Goal Loop 投影。核心结果包括：

| 字段 | 内容 |
|---|---|
| schema_version / engine / status / source_digest / revision | 格式、来源和提交版本 |
| graph | 已保存 Goal/Attempt/Phase、assessments、edges、diagnostics、aliases、patches |
| evidence | cards、atoms、refs、record_index、任务输入、条件和回执 |
| coverage / review_gaps / errors | 扫描覆盖、宿主缺口与当前停止原因 |
| usage / analysis_context / context_metadata | 分析用量、输入组成测量与持久分析状态 |
| costs | 被分析执行的唯一 atom 成本和目标累计 |
| acceptance | 条件、候选、宿主回执及身份关联 |
| skeleton / overview / goal_tree | occurrence 图、时间阶段投影、按目标的展示树 |
| report_markdown / recovery | 同源报告，以及发生保存响应恢复时的数量与来源 |

服务作业状态和语义结果状态分别表示不同事情：

| 层 | 状态 / 含义 |
|---|---|
| Job | queued / running / completed / failed 等：后台分析作业生命周期 |
| Review result | factual_only / partial / cancelled / complete：扫描与评审状态 |
| Root Host | 原 run 的宿主决定，与上述作业是否完成无关 |

后台 completed 可以携带 partial review，必须同时检查 review_status 与 coverage。
恢复要求来源摘要、原模型/配置、引擎和结果格式兼容；只能增加总 calls、tokens、
seconds 和单次 input budget，不能降低，也不能随意改变其他语义设置。
已提交窗口不重跑；未提交请求继续累计既有成本。
增加预算不会自动重审所有已处理 unknown；兼容的保存响应恢复或显式历史更新是独立机制。

缓存身份包含 session/run/source、模型配置、mode、settings、各版本；
Efficient Eval 还固定所复用的 Deep Eval artifact。显式 New evaluation 生成新 attempt；
Resume 使用已有 checkpoint。Efficient Eval 只复用已保存分析或事实，不偷偷调用 judge。
服务当前最多允许 4 个活动分析作业。

读取旧结果时，`upgrade_projection` 在副本中重建 overview、goal_tree 和 report：
不覆盖已保存 paid graph、usage、costs 或原 artifact。重新打开就可使用新树布局，
但原模型漏掉/误分的业务目标不会被展示层补成“已评审结论”。
从保存响应恢复语义 patch 与纯展示升级要分开记录，前者也必须通过完整校验。

## 10. 页面布局与交互

### 10.1 三个独立 block

```text
操作设置
  run / engine / judge / budgets
  Analyze / Resume / Cancel / Refresh / Export JSON / Report / Trace

Analysis process
  状态、当前步骤、实时进度
  分析统计、成本与缺口提示
  Evaluator activity
  History 列表

分析结果
  Skeleton | Acceptance | Report
```

分析过程统计不再放在 Skeleton 顶部。History 选择同时决定过程区和结果区；
每项显示时间、模型、状态，已加载数据可显示 reviewed / requests / tokens，
否则显示基本耗时和 ID。切换 run/job 时不沿用另一作业的目标筛选。

### 10.2 实时进度与统计口径

页面在分析作业 queued/running 且当前位于分析页时，约每 1.2 秒轮询所选 job。
session/run/generation 防止切换页面后的旧响应覆盖当前选择。
保存的 partial graph 可逐步展开，不必等全分析结束。

| 指标 | 实际口径 |
|---|---|
| 进度条 | processed_cards / total_cards |
| Reviewed cards | 有效模型 assessment 数，排除宿主 deferred |
| Reviewed assignments / unknown | 两个计数：有 phase 的 assessments / relation=unknown 的 assessments，不是成功率 |
| Context-inferred cards | 展示归属继承前一 SubGoal 的 card 数，不增加 reviewed |
| Analysis requests | 累计物理请求 / 上限 |
| Charged analysis tokens | 已结算加未知预留 / 上限 |
| Unknown analysis usage | usage 未知的请求数 |
| Elapsed seconds | 当前累计分析时间 / 上限 |
| Execution tokens | 被分析 run 的已知 token 与未知模型调用数 |

模型可以给出有效的 unknown assessment；因此 reviewed 不等于目标完成。
processed 包括宿主兜底 unknown；因此 100% 扫描也不等于 100% 有效评审。
缺失 artifact、deferred、partial、错误、停止原因和免费恢复数量单独提示。
Evaluator activity 展示最近进度记录（服务最多保留 100 条），同作业刷新保留展开和滚动位置。
底层 evaluator trace 导出用于更详细的请求、修复和计费核对。

### 10.3 Skeleton：只保留 Goal Tree

Skeleton 默认只展示树和目标筛选，不再切换 Stage overview / Event DAG。
筛选支持 All goals、某个 SubGoal、Unreviewed / unassigned。
所选目标保留祖先和后代；共享 card 不会把无关兄弟分支带入结果。

Root、Goal 默认展开；Stage、Action 可逐级展开。
刷新按稳定节点 ID 保留展开和收起状态，Action 内容按需生成。
目标展示 Serves parent、Closure evidence、Remaining questions / feedback 和 Evidence。
阶段展示 basis、产出、模型名称和证据，标题展示调用数和 Duration。

`Unreviewed / unassigned` 树筛选当前按无 phase / 无归属集合工作，
与过程区排除 host_deferred 的 reviewed 统计不是完全相同的集合；
已有有效 unknown 且没有 phase 的卡也可进入该筛选。

### 10.4 Action 详情与去重

tool_call 的外层已经显示工具名和来源，展开后不再重复一遍工具标题和 enabling 标签。
保留 Expected / Progress / Adoption；Review evidence 单独折叠。
参数与结果按 **Tool arguments / Tool result** 标注，引用紧邻对应正文。
`E139 / E140` 通常分别指请求参数和返回结果，不代表两次调用。

后续模型请求中的工具消息放入默认折叠的 **Model-visible input**。
若显示片段与上方 packet 完全相同，仅显示说明和后续模型请求引用；
不同或截断片段仍保留。比较的是实际显示片段，不推断完整源文本相同。

model_call 保留 **This action’s original records**：从 atom.request_line /
response_line 和 record_index 找到自身请求/响应，可读取原始 payload
（包括已记录的 reasoning_content、tool_calls）。缺 content packet 时仍可核对。

邻近归属的 refs 另放入 **Goal attribution evidence · previous action**。
这些解释“为什么挂在这个目标下”，不是当前动作的原文。
例如多个 unknown model_call 共享 E1107/E1108 锚点，不能把它们都当作 E1107/E1108
那次调用；run_8075 的自身响应入口分别可定位到 E1427、E1609、E1727。

### 10.5 Acceptance 与 Report

Acceptance 展示条件及已记录检查、关联目标、candidate subjects、
verification IDs 和原始宿主回执。它提供身份链核对，不另行付费评审。

Report 从同一 graph/evidence/acceptance 生成 Markdown，包含目标树、阶段表、
依赖图、关系证据表、逐事件贡献、闭环、诊断、验收、成本和限制。
网页使用本地 Markdown 渲染和 HTML 消毒；其中 Mermaid 图块由同源 overview
渲染为 SVG，下载保留 Markdown。Skeleton 移除独立 DAG 视图不意味着导出删除依赖图。
报告正文仍包含 Coverage / Costs 等报告章节；移走的是页面独立分析统计面板。

JSON 保留结构化图、引用与投影；报告导出供阅读；trace 导出用于核对分析过程。
过程问题、重叠或未知范围用警示呈现；普通 Stage 不因布局完整就显示为“已最优”。

### 10.6 与运行页 Processing 的边界

运行页 Processing 展示被分析任务的实时 model/tool 记录；
Analysis process 展示评估器执行过程，二者不是同一条时间线。

Processing 的 Working 摘要优先取非空 content 的首行；content 为空时，从已记录的
`reasoning_content / reasoning` 中取最后一句，清理标题/列表标记并限制为 200 字符。
这是本地文本回退，不新增模型调用，也不声称总结了隐藏推理。
该规则不等于修改 Deep Eval 的 evidence packet 或自动补齐 unknown 判断。

## 11. run_40b063：人工样例与实现核对

来源及逐调用详表见[目标树核对](./deep-eval-run-40b063-goal-tree.md)和
[原始 skeleton 核对](./deep-eval-run-40b063-skeleton.md)。
固定 cursor 6180，source artifact
`47efc4bf84b1a61db085c1b1a6a2637d23df332696a98da731c84fe3d42888fa`；
502 条非流式 observation、62 张 card、22 次模型调用（20 worker + 2 verifier）、
33 次工具调用，另有 7 张非调用 card。

人工目标树摘要如下；这是核对基准，不是宣称保存的 paid graph 已自动达到该划分。

```text
Root：分析 QMD 功能，将指定 ARCHITECTURE.md 入库，测试搜索
  属性：Task requirements 与 3 个 required 条件
  G1：形成有来源依据的功能说明
    README / 项目结构调查；功能 finding
  G2：确定外部文件入库与检索的方法
    CLI 入口；collection/index/config；反复定位与可执行方法
  G3：指定文档已入库并可定位
    collection add；用后续目标 URI 补足文件级证据
  G4：搜索能返回目标文档内容
    首次 BM25；定向查询与 get
  Root Stage：整合与 finish
  Root Stage：Response review
  Root Stage：Condition review
  Root Stage：Root decision
```

| 主归属 | Worker model | Tool | 核对重点 |
|---|---:|---:|---|
| G1 功能说明 | 3 | 6 | 功能 finding 虽在 M14 期间发生，仍可服务 G1 |
| G2 方法调查 | 12 | 22 | 不把“我理解了”当成已验证的方法闭环 |
| G3 入库 | 1 | 1 | 索引了 118 文件；总数不能单独证明指定文件已入库 |
| G4 检索 | 2 | 2 | 后续目标 URI、定向 BM25 和 get 提供增量证据 |
| Root 整合提交 | 2 | 2 | finish 只提交候选 |
| 合计 | 20 | 33 | 跨目标复用证据不增加调用 |

E5702 / E6117 是两份 verifier 回执；E6117 内含三个 required 条件。
Root decision 的 E6118 / E6129 / E6153 / E6172 分别是聚合、决定、结果和完成，
不是四次 verification。该历史 run 继续保留两个 review；新默认 run 使用合并审阅。

已有免费重投影核对覆盖 62 卡和唯一 atoms，但旧业务图只有三个目标，大量方法调查
混入功能分析。展示升级不会凭人工样例补出第四个目标。
先前真实模型校准也留下 unknown 和错误归类；其具体请求、token 和版本边界保留在
原核对文档。本设计不把结构校验、扫描完成或 UI 更直观等同于模型质量达标。

另一个保存样例 run_8075 的三个未归类阶段共 25 卡，按邻近规则归入此前 g2：
它们仍是推定，103/134 有效评审和 31 张未审阅不变，Root 53 model / 71 tool 不变。
这验证的是归属投影与计量守恒，不是对这些动作重新完成了语义评审。

## 12. 验证依据、已知边界与维护规则

| 验证面 | 仓库入口 |
|---|---|
| 证据、预算、补证、修复、取消和 checkpoint | [test_goal_loops.py](../../tests/test_goal_loops.py) |
| 特征、metadata、范围及输入成本 | [test_goal_context.py](../../tests/test_goal_context.py) |
| 保存响应恢复与紧凑协议兼容 | [test_goal_delta_recovery.py](../../tests/test_goal_delta_recovery.py) |
| 相邻归属、根事件例外与语义不变 | [test_goal_attribution.py](../../tests/test_goal_attribution.py) |
| 目标树、共享引用、Root 属性和汇总 | [test_goal_tree.py](../../tests/test_goal_tree.py) |
| 阶段、依赖、Duration 与免费投影 | [test_goal_overview.py](../../tests/test_goal_overview.py) |
| 服务创建、读取、恢复与旧引擎兼容 | [test_session_evaluation.py](../../tests/test_session_evaluation.py) |
| 页面布局、树展开/过滤/证据/导出 | [web_goal_loops.cjs](../../tests/web_goal_loops.cjs) |

上述表格定位已有测试，不表示本次纯文档整理重新执行了这些测试。
本次只做源码交叉核对、文档链接和差异检查。机制测试和人工 trace 核对支持
可追溯性、守恒与展示行为；真实模型语义质量必须另行校准。
当前边界需要持续保留：

- 不能从引用合法推断结论正确，不能从 Root accepted 推断所有局部目标闭环或过程最优。
- 相邻归属是默认上下文解释，不是模型已评审，也不是因果或采用证明。
- content 缺失时可以查看原记录，但不会凭 reasoning 自动恢复旧语义判断。
- 阶段依赖缺失时保留缺口，不按时间顺序或人工理想路径补实线。
- 遗留坏拆分、重复调查、未决问题和多次尝试不可被展示折叠隐藏。
- 旧 checkpoint 的资源、既有成本和历史判断保持可追溯；展示升级不得发起隐式付费请求。

后续改动应同步更新协议版本、相关测试及本文对应章节。若改变计量或归属规则，
优先核对唯一 atom 守恒、共享分支、撤销归属和 unknown 数量；若改变布局，
核对历史选择、刷新保持、原始记录入口及无额外模型调用。

## 13. 相关文档

- [run_40b063 目标树与闭环核对](./deep-eval-run-40b063-goal-tree.md)：完整人工树与历史校准证据。
- [Deep Eval 目标闭环与执行成本分析](./deep-eval-goal-loops.md)：最初原则、整体模型与首版验证。
- [上下文与成本审计](./deep-eval-context-cost-audit.md)：从长正文输入转向特征与持久 metadata 的依据。
- [22 张缺口的协议诊断与恢复](./deep-eval-delta-recovery.md)：保存返回兼容与零新增请求恢复。
- [从事件图到执行 skeleton](./deep-eval-skeleton-presentation.md)：overview、报告与旧页面演进。
- [统一 Acceptance Review](./unified-acceptance-review.md)：被分析执行框架的语义验收职责。
- [本次文档整理计划](../exec-plans/completed/deep-eval-design-consolidation.md)：源码核对与链接检查记录。

## 14. 当前 loom_py 页面的适配（2026-10-20）

本次实现对应第 5–6、8.1、10 节的展示设计，实际输入为已保存的
`goal_graph_v1`：`goals / stages / record_assessments / extraction / cost_ledger`。
没有迁移评估器协议、重写 checkpoint、修改 token 策略或触发新模型调用。

实际入口：

- [goal-tree.mjs](../src/loom/web/assets/views/goal-tree.mjs)：只读树投影和目标筛选。
- [incremental-graph.mjs](../src/loom/web/assets/views/incremental-graph.mjs)：Skeleton /
  Acceptance / Report、展开状态、按需 Action 详情和来源入口。
- [goal-tree.test.mjs](../tests/web/goal-tree.test.mjs)、
  [incremental-graph.test.mjs](../tests/web/incremental-graph.test.mjs)：树语义及页面回归。

页面采用 Root → SubGoal → Stage → Action，不再以执行 DAG 为 Skeleton 主视图。
同一 Goal 内相同稳定 stage ID 的非相邻 occurrence 合并展示，保存的依赖仍可在
Stage 详情查看。Goal 默认展开，Stage/Action 默认折叠；刷新保留展开、收起、
筛选及所在滚动容器位置；切换评估作业或 run 重置局部状态。

筛选保留所选 Goal 的祖先和后代，不因共享 action 引入无关兄弟目标。首个明确目标
作为 primary，其余为 shared reference；分支按唯一 atom 计数和累加已知 duration。
零耗时、无调用、全部未知和部分未知分别展示，不将 duration 标为墙钟时间。

未明确归属的 action 可继承前一个明确 SubGoal，显示 Context inferred 和
Continue 阶段；不增加 reviewed 数、不改成本 owner、不补造成果依赖。显式
unrelated 阻止继承，Root 验收/宿主记录优先归 Root。验收配置归 Task requirements，
不制造一个零调用执行 Stage。旧数据中没有保存的 root_relation/closure 保留缺口。

Action 展示 Expected / Progress / Adoption、调用性能、Tool arguments / Tool result
元数据和原始记录入口。完整正文通过现有 evidence 阅读器按需读取；归属锚点和
动作自身 request/response 分开展示。已知完整呈现只显示复用说明及模型请求引用，
不在树内重复大段工具结果。

Acceptance 使用当前结果中的 conditions、verification 和宿主事件，提供记录字段、
verification ID/subject（存在时）和原始证据。当前结果未保存的候选身份匹配或
条件—目标关系不会由 UI 猜测补齐。Report 保留保存时的报告/依赖内容；其中原有
occurrence 编号与树内合并 Stage 的展示编号是不同投影。

验证：152 项前端测试通过；用独立本地样例在实际 Chrome 中检查桌面与窄屏布局，
服务静态资源 HTTP 200 且包含新模块。未重新运行付费分析，也未宣称旧模型漏判
的目标或贡献已由页面修复。
