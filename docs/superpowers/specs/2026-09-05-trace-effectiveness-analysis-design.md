# Trace 精确分析：五维诊断设计

状态：设计草案，待评审；尚未修改运行时代码或分析器。

目标：让 `loom.evaluation.analyze` 解释每次决策是否获得必要信息、工具是否产生有效结果、任务如何推进、资源花在哪里、完成声明如何被验证，并为后续优化提供可回查、可证伪的依据。

## 1. 研究依据和边界

依据仓库保存的 [Harness Engineering for Self-Improvement](../../2026-07-04-harness.html)，重点参考 Context Engineering、Self-Improving Harness、Future Challenges。这里使用本地文章的论述，不将其中引用论文的实验结果视作本次独立复现的事实。

对本设计的约束：

- ACE / MCE：分析 context 的增量、保留、丢弃及管理机制，不能只判断当前 prompt 的长度或主题相关性。
- Meta-Harness / AHE：原始轨迹可按需访问；摘要是导航入口，不能成为唯一证据。逐任务分析应能向上聚合，向下回查。
- Self-Harness：相同的终止失败可能来自不同机制；同时保存成功行为，避免优化破坏已有能力。
- AHE：问题需要指向具体组件；优化假设应带可验证的预期收益和回归风险。
- Chain-of-Evidence 与文章对弱 verifier 的讨论：区分声明、检查和外部证据；模型自评不替代验证。
- 负结果也是信息进展；探索尚未产出最终答案时不能自动判为浪费。

本轮范围是离线分析器。输出诊断与验证框架建议；不执行被分析 trace 中的命令，不修改 agent harness，不自动接受优化提案，不把本次分析兼作优化后的效果验证。

## 2. 现状核对

已直接读取 `evaluation/analyze.py`、`judge.py`、`assessments.py`、`token_usage.py`、`bundle.py`，以及 `trace_analysis`、`llm/api.py`、`runtime/engine.py`、`tasks/runner.py` 的事件生产和消费路径。

复现记录见 [真实样本分析](../../analysis/2026-09-05-trace-effectiveness-sample-review.md)。关键差距：

| 现状 | 对精确分析的影响 |
| --- | --- |
| `graph.py` 对 LLM bucket key 做字符串排序 | `llm-10` 出现在 `llm-2` 前面，时间顺序不可信 |
| Round evidence 只从 `response.tool_calls` 关联工具 | JSON action 路径执行的工具可能全部从 Round 证据中丢失 |
| 工具结果可能是 `Observation.value` 包装 | 当前浅层摘录可能截到时间、ID，而非 stdout、文件内容或错误 |
| Round 只看末尾 3 条消息和固定短摘录 | 目标、约束、工具 schema、关键断言或结果尾部可能不可见 |
| Step 主要汇总 Round 自评 | 缺少跨 Step 的任务状态、声明—验证关系 |
| 规则维度默认 1.0 | 缺少问题检测被表现为质量满分，证据缺失没有独立语义 |
| token 主要汇总 total | 缺少输入/输出拆分、上下文来源、预算完整性和收益关联 |
| `done()` 的停止条件及 `run.completed.outcome=pass` | 不能等同于任务验收通过 |

例如 `runtime.engine.done()` 可以因 `goal.budget.max_steps` 达到上限返回 true；`run()` 随后记录 pass。generic task 的 `_task_done()` 也可依据非 parseFallback 的 decision 结束。`SuccessCriterion` 支持 evaluator，但该运行路径没有标准的逐 criterion 验证证据事件；generic task 默认生成的 criteria 不指定 evaluator。

## 3. 方案选择

| 方案 | 收益 | 局限 |
| --- | --- | --- |
| 只替换 judge 的维度和 prompt | 改动少 | 关联、顺序、摘录缺失继续污染结论 |
| 将整条 trace 交给一个模型自由分析 | 原型简单，内容较全 | 长 trace 成本高；引用、覆盖和判断一致性难验证 |
| 事实重建 + 五维证据分析 + task 级综合 | 证据可审计，支持长轨迹和局部诊断 | 需要明确轨迹、证据和诊断契约 |

选择第三种。执行结构统计继续作为事实和数据质量信息；新报告不生成六维平均分或五维统一总分。

## 4. 分析流水线

```text
JSONL + 可选显式 task/criteria/历史产物
  -> 校验、索引、证据可用性清单
  -> 按记录顺序重建 Run / Loop / Step / Round / Tool 关联
  -> TaskContract + ContextSnapshot/Delta + RoundTransition
  -> ToolUse + TokenLedger + VerificationEvidence
  -> 五维诊断，必要时展开原始证据
  -> Run 级一致性审查与覆盖检查
  -> diagnosis / verification coverage / preserved behaviors / report
```

事实抽取由确定性代码负责。语义判断由 LLM 在结构化证据上完成。无 judge 模式产出事实、覆盖和可确定的问题；未执行的语义分析标记 `not_evaluated`，不能默认通过。

每个产物记录分析器版本、源文件 SHA-256、模型与 prompt 版本、配置和证据截取预算。每个维度区分“源记录是否完整”与“本次分析实际读取是否充分”。

## 5. 精确重建的要求

### 5.1 顺序、身份和关联

- 同一 JSONL 的物理记录序号是采集顺序；显式因果 ID 优先确定依赖。时间戳仅辅助，不能将并行或时钟偏差强行解释为因果关系。
- 身份键包含 run、loop、trace、step 和调用 ID。另设按出现顺序生成的展示序号，不假设原始 `step_number` 连续，也不按 UUID 或调用 ID 字符串推断先后。
- LLM 失败、未结束工具、孤立事件、重复记录和冲突 ID 必须显式保留。相同内容重复执行不等于重复日志，不按 payload hash 简单去重。
- 工具关联先用显式顶层/metadata 父调用 ID，再用 native tool call ID。旧 trace 的已知 JSON action ID 编码可作为唯一匹配的兼容规则，记录 `link_basis=legacy_id_convention`。有歧义时保持 unresolved。
- JSON action、native tool call、工具触发的 Step 边界均须覆盖。没有 native `tool_calls` 不等于没有工具行为。
- 按工具结果 schema 解包 `output.value`，同时保留原始字段路径。工具 runtime 成功、进程退出码、领域结果、观察结果进入 context 是不同事实。

### 5.2 TaskContract 与信息可得时间

从用户显式提供的任务、run metadata、context snapshot、首次请求中的目标/约束及后续任务修订提取 TaskContract。记录每条要求的来源与生效时点，不用任务结束后的新信息改写最初要求。

结构化显式要求与从自然语言推断的验收条件分开。来源冲突或任务不完整时保留歧义；final answer 不是任务定义的权威来源。

每轮同时提供两种视图：

- 决策时视图：当时真正发送给模型的消息、工具 schema 和可用证据。
- 回溯结果视图：后来出现的工具结果、产物和验证事实。

后见信息可以证明后来发生了什么，不能伪装成 agent 当时已知的信息。

## 6. 五个分析维度

### 6.1 Context 有效性

主问题：相对于本轮要做的决策，当前 context 和本轮新增信息是否足够、相关、可靠，并且实际进入了模型请求？

输入：完整请求消息与工具 schema、上一轮/上一步的请求、工具原始结果及注入版本、任务约束、context policy 或快照（若有）。

输出：

- 每轮新增、移除、保留、重复、替换的 context 单元及来源；跨 Step 重组单独标记。
- 当前决策的信息需求，以及每项需求对应的可见证据、缺口或冲突。
- 工具产生的信息是否被截断、摘要化或遗漏；比较原始结果与实际请求内容。
- 关键信息在压缩后丢失、旧结论过期、无关背景持续携带等具体实例。
- 使用证据：后续参数、结论或行动与信息的关联；“未见显式引用”不自动等于没用。

充分性必须相对于当前动作：执行探索性搜索并不要求提前知道搜索答案。避免按最终答案反向要求每轮拥有全部信息。

消息差分先使用稳定 ID/内容指纹和结构对齐；纯文本相似仅生成待审线索。缺少来源标签时可以确认文本变化，不能断言某条被移除内容必然来自某个内部 memory 策略。

### 6.2 Tool 有效性

主问题：这次调用是否满足了当时的一个明确需求，结果是否可靠、可消费，并被后续推理或状态变化利用？

沿链路分析：需求 -> 可用工具及 schema -> 选择与参数 -> 执行结果 -> 注入 context -> 后续使用/产物变化。

分别记录选择适配、参数有效、执行状态、结果信息质量、下游使用和替代方案。区分输入问题、环境失败、工具实现问题、结果表示问题和使用问题。

重复调用只有在需求、参数、环境/产物版本及已知结果相同，而且未增加信息时，才有证据支持“可能冗余”。重试若验证恢复、排除假设或读取变化后的文件，可以有效。

写入类工具可通过产物变化贡献进展，不要求必须有后续 LLM 回复。读取类工具可能在后续 Step 被使用，需要跨边界追踪。并行工具按调用 ID 关联结果。

没有历史产物或副作用证据时，工具的成功返回不证明目标文件内容、数据库状态或外部操作符合要求。

### 6.3 Loop 进展

主问题：每个 Round、Step 相对于任务和当前子目标改变了什么，是否值得继续这一条路径？

`RoundTransition` 记录决策前状态、意图/问题、动作、观察、决策后状态，以及证据支持的变化：

- 获得新事实、排除假设、降低关键不确定性。
- 修改或交付产物。
- 完成验证、发现回归、解决阻塞。
- 作出必要澄清或有依据地放弃某路径。
- 停滞、循环、回退、偏离任务或无证据完成声明。

进展既可以是正向产物，也可以是有效负结果。计划更新和模型“完成”声明分别作为计划/声明记录，不能直接变成外部进展。

Run 级维护任务/子目标进展账本和连续停滞区间；Step 汇总引用原始变化记录，不把多个 Round 的同一个贡献重复计数。没有可靠 task contract 时不计算“完成百分比”。

第一轮/终止轮、跨 Step 的延迟工具使用、取消、委派和未完成子任务要有明确状态；父子关联缺失时不虚构整个任务树。

### 6.4 Token efficiency

主问题：为取得哪些信息、产物或验证结果花了多少 token，哪些开销有可验证的压缩或避免机会？

事实账本按实际调用记录 prompt/completion/total、模型和可获得的辅助调用 usage。区分任务执行成本与 analyzer 自身成本；流式完成与普通完成不能重复计费。

context 来源拆分包括约束/目标、工具 schema、历史模型输出、工具反馈和摘要。来源级 token 若没有 tokenizer，只报字符/字节数；若估算，必须标明估算方法，不当作 provider 实测值。

当前 `TokenUsage` 只有 prompt/completion/total，不能伪造 cached/reasoning token 或美元成本。未记录 usage 的调用标记 unknown；部分 round 缺 usage 时不能以已有调用之和冒充完整总量。工具选择等辅助调用单独核对，禁止与 Step 汇总重复累加。

报告重点是成本热点、重复携带来源、无新增信息的区间成本，以及对应的具体优化假设。没有任务难度可比性和验证结果时，不跨任务比较 token 排名；高输入占比、重复 prompt 或高总量本身不构成浪费判定。

优化假设示例：对重复传递的长 README 使用任务相关摘录并保留回读索引；预测减少后续输入 token，同时必须验证目标/约束未丢失、答案有证据、smoke 和测试结论不退化。未经对照实验，不声称节省比例或因果收益。

### 6.5 Verify gate

主问题有两个：本任务需要怎样的验证框架；实际轨迹是否提供了足够有效且适用于最终状态的验证？

以 task criterion 为单位建立映射：

```text
criterion + 来源
  -> 为什么该检查可以验证它（oracle / 断言）
  -> 检查定义与执行参数
  -> 被检查对象/版本/环境
  -> 执行结果和失败信号
  -> 最后一次相关改动后的有效性
  -> 最终声明及其证据
```

分别输出原轨迹中已观察到的验证和分析器建议的验证框架。建议的测试没有被运行，不能出现在 passed 栏。

`criterion.status` 使用 `supported / contradicted / unverified / not_applicable`；另记录 `evidence_coverage`、判定依据以及 oracle 的适用范围。缺少验证事件不等于未执行验证：shell 输出、测试源码和历史产物也可能提供证据。

检查要求覆盖、真实断言、错误传播、版本新鲜度和独立性。区分命令退出 0、测试框架通过、要求被覆盖和整个 task 被验证。只打印 PASSED、只创建了文件或模型自己打分都不能独立证明目标完成。

成功 smoke 可以支持一个有限的 E2E 路径，不能自动扩展为全部产品功能正确。测试前后发生相关改动而没有重测时，旧结果标记 stale；缺少版本绑定时显式说明无法证明最终版本仍适用。

对于文档、研究等没有可执行 oracle 的任务，给出来源核验、事实/主张覆盖或人工审阅的框架，并报告验证局限。这里的 task verification 与 `governance/gates.py` 的优化提案晋级 gate 是两个对象。

## 7. 证据访问与结论契约

### 7.1 引用能解析，原文能展开

EvidenceRef 至少保存源文件摘要、记录序号、原始 event hash（若有）、事件/调用身份、字段路径和内容范围。摘录是显示字段，不能作为唯一存储。

无原始 hash 时，以源文件摘要和记录序号定位，不伪造 producer hash。原始 hash 的校验结果独立记录。字段不存在、范围越界、事件归属不符或来源被改写，均产生明确证据错误。

分析器只读取 trace 及显式登记的历史产物。trace 提到的路径不意味着该路径当前内容就是当时内容；不自动用当前工作区文件补历史证据，不执行 trace 中的指令。

先提供轨迹索引、task contract 和相关证据；模型可通过受限只读接口请求事件字段/范围。预算不足时报告未覆盖部分并降为 unknown，不继续用固定 48/160 字符截断作确定结论。源数据已截断、过滤或未保存则不能靠展开恢复。

### 7.2 每条诊断的内容

`Diagnosis` 包含：

- `dimension`、作用域和相关 criterion/子目标。
- `observation`：直接观察到的事实及精确引用。
- `interpretation`：该事实如何影响当前需求、进展或验证。
- `epistemic_status`：observed / inferred / hypothesis / unknown。
- `evidence_coverage`：源证据完整性、已审范围、未解决缺口。
- `supporting_refs` 与 `counterevidence_refs`。
- `mechanism` 和可定位的组件；归因不充分时允许 unknown 或多个候选。
- `consequence`：实际发生的影响与预期影响分别描述。
- `improvement_hypothesis`：候选变更、预测信号、验证方式。
- `preserve`：应维持的成功行为及回归风险。

模型置信度可保留为自报辅助字段，但不能当作校准概率，也不能经乘权成为“优化价值”。单条 trace 只能定位实例或提出模式假设，不能证明跨任务高频问题。

例如“README 不含 smoke 说明”需要完整搜索覆盖；只读前缀时，诊断应为“最终否定结论超出已读范围”，而非断言 README 确实有或没有该内容。

## 8. 报告与兼容性

新报告按以下顺序组织：task/验收要求 -> 证据覆盖 -> Round 时间线 -> 五维诊断 -> 验证矩阵 -> 成功行为与优化假设。

拟新增产物：`trajectory.jsonl`、`context-deltas.jsonl`、`token-ledger.jsonl`、`diagnoses.jsonl`、`verification-coverage.jsonl`、`preserved-behaviors.jsonl`；manifest 明确声明分析版本、源文件、覆盖和产物引用。

新语义使用独立 v2 schema；保留现有 v1 bundle 的读取，不把 v2 diagnosis 写成旧 `EvaluationFinding` 的同名字段，也不将 unknown 映射成 pass。旧 evolution 消费者遇到 v2 必须给出明确版本不支持信息，不能静默生成优化提案。接通 v2 到优化模块属于后续工作。

默认报告以五维诊断为主；旧规则分数不成为新分析的依据。历史 v1 报告和导入路径保持可读。具体迁移由入口显式版本选项与 schema 检查约束，不能静默改变 v1 文件契约。

## 9. 实施拆分与验收

### A. 事实层

在 `trace_analysis` 实现稳定事件顺序、完整身份、工具关联、结果解包、可解析 EvidenceRef 和 coverage。

验收：37 轮样本无 `1,10,...,2` 重排；8 轮样本的 7 次 JSON 工具调用全部可关联，注明旧 ID 兼容依据；真实结果字段不被 Observation 包装遮挡。

### B. 轨迹与账本

实现 TaskContract、ContextDelta、RoundTransition、ToolUse、TokenLedger 和 VerificationEvidence。确定性事实与语义待判断项明确分离。

验收：实际消息增删可回查；跨 Step 使用不被误判；token 不重复计算且缺失显示 unknown；预算停止不被视为验证通过。

### C. 五维分析与产物

替换固定摘要评分路径，加入按需证据访问、五维诊断、Run 综合和报告、版本化 CLI/bundle。

验收：样本的 README 越界结论被识别；smoke 与 pytest 的有效行为被保留；不得凭总 token 高而认定浪费；每条结论的引用可解析。

### D. 精度验证

使用小型人工标注轨迹集覆盖：完整/截断 context、无效工具成功返回、有效负结果、合理重试、重复读取、并行、跨 Step 消费、假 PASSED、真实断言、测试后改动、任务变更、缺 usage、未知父调用和无 hash。

确定性测试验证顺序、关联、差分、计量和引用。语义评审使用固定输入、人工依据和对照扰动，分别记录误报、漏报、无依据归因、证据覆盖与重复运行一致性；不能用模型 JSON 格式正确代替分析质量验收。

反事实扰动至少包括：删除必要结果 -> 应转为 unknown；将测试 exit_code 改成失败 -> 不得仍通过；把无变化重试换成有新结果的重试 -> 应认可进展；把用户约束从前缀移除 -> 不得继续声称当时可见。

首版发布门槛：确定性契约测试通过；人工标注样例中所有主张必须有可解析引用或明确的 unknown，不能出现无证据的 task verified；分别公布诊断的人工核对结果，不以一个平均分掩盖失败。

## 10. 本轮完成情况

已完成研究阅读、事件生产/消费核对、两个真实 trace 的定向复现以及八轮样本的五维人工分析。此文是可评审设计，不声称分析器已经具备上述能力；没有重新执行样本中的测试，没有调用真实 LLM judge，没有修改优化器或 runtime。
