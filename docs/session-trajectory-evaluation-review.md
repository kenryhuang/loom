# Session trajectory：评估能力审查与接入建议

审查时间：2026-10-05。以下为改造前的审查快照；审查阶段没有运行付费 LLM judge，也没有生成或应用 mutation。

后续已接入独立深度评估任务、批次检查点、v2 改进假设适配与页面诊断视图，详见 [当前实现说明](session-trajectory-analysis.md)。下文“未接入”等描述保留为改造依据。

## 当前页面实际运行什么

`service/trajectory.py:analyze_session_trace` 调用 `build_episode_graph` 和 `evaluation/trajectory.py:build_fact_analysis`。
真实计算了 task contracts、工具因果关联、上下文增删、结果注入、provider token ledger、验证证据。
但 service 没有调用 `judge_effectiveness`、`assess_steps`、`LlmStepScorer` 或 evolution proposal/shadow 流程。
页面的 `semantic_status=not_evaluated` 和 `task_completion=unverified` 是显式状态，不是评估结果。

## 四类评价的含义

| 实现 | 实际机制 | 使用边界 |
| --- | --- | --- |
| evaluation v1 `assess_steps` | 六维规则分数，默认 1，按不完整事件、失败、固定 token 阈值等扣分；六维算术平均 | 运行与记录健康度，不是任务价值 |
| evaluation v1 round/step judge | LLM 对逐轮和逐步 evidence pack 给维度分数、归因和 findings | 旧版语义评价，输入摘要存在截断限制 |
| evaluation v2 `judge_effectiveness` | 五维诊断、逐轮状态变化、验收证据、保留行为和可验证改进假设 | 适合作为 session 深度分析主路径；明确不输出总分 |
| evolution `LlmStepScorer` | 八维 0–1 分数、overall、confidence、attribution、proposed_fixes | 主观评审分数；不是校准过的质量概率 |

v1 的“没有发现规则问题”会得到高分；例如同一步工具结果后没有 LLM completion 会扣分，但最终写入本身可能已达到目的。
evolution 的 severity 取 overall 和各维度中最大的不足，再按可修改 surface 聚合；默认重复两次形成 signal。
旧 v1 bundle 入口另外支持单个高影响/error finding。
proposal gate 默认 confidence >= 0.7、风险不高于 medium、要求可逆；这些条件不构成效果证明。
当前 proposal 主要是模板化的 add_rule / clarify_schema / adjust_context_policy 等建议描述，且生成时风险统一 low、可逆统一 true。
`DefaultEvolutionEvaluator` 的公式是 passes - 2*failures - 2*timeouts - 0.5*gaps；不能用于比较不同长度轨迹的任务质量。
shadow helper 可以运行 before/after，但 analyzer 并未将它接成自动验证、应用、回滚的闭环。
`evaluation/experiments.py` 定义了成对试验、区间和用量契约，本身不是实验执行器。

## v2 已具备但未接入的关键能力

- context_effectiveness：当前决策需要什么信息，实际请求是否包含；关键约束是否丢失、过时或被截断。
- tool_effectiveness：需要 → schema → 参数 → 运行/业务结果 → 注入 → 后续利用。
- loop_progress：信息增加、排除假设、产物变化、验证、解除阻塞、规划、停滞或未知。
- token_efficiency：实测成本与上述进展的关系；重复上下文不直接判定浪费。
- verify_gate：原始验收条件 → 实际 oracle → 执行/源码证据 → 覆盖范围与最终版本。

`round_analyses` 已有 pre_state、intent、action、observed_change、post_state、progress_kind 和五维 effective/ineffective/mixed/unknown。
`Diagnosis` 已有支持证据、反证、机制、后果、改进假设、保留行为，以及 observed/inferred/hypothesis/unknown。
judge 按批读取证据，核验实际呈现的字符范围。未读到的引用会降为 unknown/unverified；会区分当时可见信息与事后信息。
默认每批 8 轮，最多 6 次追加取证，80,000 字符为整个评估的证据预算，单次 prompt 上限 100,000 字符。
244 轮初始需要 31 批，另有综合阶段和取证请求；prompt 拆批可能增加批数，不能按“一次模型调用”设计界面和预算。

## 接入前需要解决的缺口

1. **v2 到 evolution 的契约缺失。** evolution 使用的 `load_evaluation_bundle` 只接受 `loom.evaluation.bundle.v1`，v2 bundle 会被拒绝。应增加 v2 diagnosis adapter，保留 source hash、完整 EvidencePointer、反证、coverage、版本；不能伪造 v1 分数和事件 hash。
2. **最终产物与验证未绑定。** `build_verification_evidence` 将 command execution 的 artifact_binding 设为 unknown；`validate_verification` 要求 confirmed 才能支持执行验收。因此当前普通执行证据无法走到 supported。需要记录文件/产物 digest、测试输入与环境版本、测试 oracle、后续修改，不应放宽检查来制造通过率。
3. **shell 修改识别不完整。** 当前写入检测依赖工具名 write/edit/patch 等；shell 内修改不一定被识别。后续写入列表也不能精确代表相同文件的修改关系。
4. **恢复与跨作用域证据。** 工具关联已支持同 trace 的唯一 managed call 跨 loop 恢复；但注入匹配、上下文比较和 round review 仍有同 loop 限制。需要显式恢复/依赖关系，避免合法证据无法归因，也避免全局放宽作用域串线。
5. **任务修订需整理。** facts 保留不同来源及 supersedes；页面指标应按 run、需求版本与生效范围计算，不能直接把所有 criteria 加总成最终验收分母。
6. **预算和持久进度。** 当前 judge 的证据/prompt 限额不能替代总 token、总调用次数和时长限制。服务需持久化批次、取消状态、错误、覆盖率和 evaluator 用量，重启可继续，避免整条轨迹反复重评。

## 建议页面指标

| 指标 | 计算或展示 | 限制 |
| --- | --- | --- |
| 评估覆盖率 | 已审查轮数/总轮数；各维度 known/unknown；证据读取缺口 | 与质量分开显示 |
| 需求验证覆盖 | 当前有效 required criteria 中 supported/contradicted/unverified | 同时展示原始要求、oracle、版本绑定 |
| 进展分布 | 各 progress_kind 的轮数、耗时和实测 token | 规划和排除假设不自动扣分 |
| 停滞区间 | 连续 stalled 的片段、总成本、结束原因 | 需要逐轮语义判断；unknown 单列 |
| 恢复代价 | 错误到有证据的 blocker_resolution 的调用数、token、活动耗时 | 未恢复单列；暂停等待不计为模型低效 |
| 工具价值链 | 运行成功、结果注入、实际利用、任务影响分别展示 | 未注入不等于无用，写入可独立产生价值 |
| 重复尝试 | 相同工具/参数重试及结果变化，语义分析重试理由 | 缺少环境变化证据时不能直接叫无效重复 |
| 上下文携带 | retained/added/removed 字符、来源分布、关键约束缺失 | 字符不是 token；保留不是浪费 |
| 成本归因 | prompt/completion、run/阶段/进展类别、疑似低价值片段成本 | 未测量单列；不虚构可节省 token |
| 改进机会 | 机制、证据与反证、受影响轮次、改动面、预测、验证方案、需保留行为 | 节省与质量提升均待实验验证 |

不建议将五个维度简单平均成一个“总质量分”。若需要 score，可单独展示明确分母的指标比例，或标注模型/rubric/覆盖率的主观评分。
unknown 不应按 0 或 1 填补。跨版本比较应固定任务集、环境、模型、预算与评价器，报告 paired delta、有效配对数和区间。

## yakDB 已有快照：事实示例

源 session `sess_d45881073114497daf629cf82342096b`，cursor 74363；以下来自已保存的事实分析，不是 LLM 判定。

- 244 轮模型调用：241 轮有完整用量，3 轮未知。
- 已知 prompt 6,222,460、completion 258,186、total 6,480,646 tokens；总量仍有缺口。
- 输入消息字符中位数 53,143，最大 149,193；同 loop 前后轮完全相同的 retained 单元字符占所有请求消息字符 82.16%。这不是浪费率，也不是 token 占比。
- 233 次工具调用，按服务失败判定 43 次 failed；需要进一步区分工具/命令失败、正常负结果与有效探索。
- 201 次工具结果有可匹配的后续注入；其余不能直接认定被忽略，匹配范围及副作用都影响解释。
- 201 条 command execution 记录中 158 passed、43 failed，全部 artifact_binding unknown。命令成功数不是测试通过数。
- 12 条任务定义、17 条 criteria 包含来源与修订，不能直接作为最终完成率的分母。

## 推荐实现顺序

1. 在当前 snapshot 下增加独立 semantic job，复用 `judge_effectiveness`，选择 judge 模型和预算。事实视图立即可用，深度分析明确启动；持久化批次进度和 evaluator 费用，不写入原任务 event stream。
2. 缓存键包含 source_sha256、analyzer/prompt 版本、模型与配置、分析范围及预算；结果补充 round_analyses、diagnoses、verification、preserved_behaviors、coverage、usage。
3. Trajectory 顶部展示结论与覆盖率；加入五维分布、关键停滞/恢复片段、验收矩阵、成本归因与证据导航。原始调用列表保留作为事实明细。
4. 增加 v2 diagnosis → improvement hypothesis 的 adapter；按同机制和可修改 surface 聚合，保留单次严重问题。记录 proposed/tested/accepted/rejected 状态。
5. 再接配对实验与产物版本证据，证明改动确有帮助并守住回归行为；proposal gate 通过不等于实验通过或自动部署。
