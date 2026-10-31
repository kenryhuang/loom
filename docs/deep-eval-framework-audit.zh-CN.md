# Deep Eval 框架事件与 incomplete 审视记录

日期：2026-10-31。范围：当前 `goal_graph_v1` 的 Graph、Memory、证据交付、
候选校验、质量投影与重试；旧 v2/v3/goal_loops 不在这次行为调整范围。

## 结论与实际样本

`incomplete` 同时承载了“分析工作尚未执行完”和“处理已结束但结果有质量缺口”。
应先看 `execution_state`、`result_state`、`pending_work` 和 `retryable_work`，
不能看到 incomplete 就重复 Resume 或增加预算。

只读查看本地 20 个 graph 评估任务时，主要问题为 memory.observation_required（24 次）、
memory.citation_limit（7 次）、memory.workflow_step_support（6 次）、schema.maxItems（4 次）、
memory.key_action_membership / memory.stages_pending（各 3 次）。这些是历史任务中的问题记录数，
不是独立模型实验，也不代表新实现的错误率。

对 `run_4ae1b6df57d548e48d5c97e809a741fe` 对应的
`evaluation_b100291734684397a4776678bb7f2a1e`，以固定 trace 和保存的候选进行只读复核：

| 原始隔离原因 | 数量 | 修复后本地校验 |
|---|---:|---|
| observation_required | 8 | 7 条运行时事实可通过；1 条“恢复成功”仍缺少实际结果 |
| citation_limit | 2 | 2 条 workflow 可通过，所有步骤证据均保留 |
| key_action_membership | 2 | 仍需重选合法 stage 动作或给出真实的 unknown |
| workflow_step_support | 2 | 仍缺少步骤执行结果，需修正、标 proposed 或显式撤回 |

共 14 条隔离候选，其中 9 条在新规则下通过确定性校验。此次复核没有修改数据库，
没有调用模型，也没有把历史任务状态改为 completed。通过来源校验不代表重新验证了其全部语义。

## 卡点和处理

| 环节 | 卡点 | 当前处理 |
|---|---|---|
| Graph 边界 | 宿主补 unknown 后，状态层仍把纯框架记录视作未解决语义判断 | 中央框架策略区分 boundary 与真实调用；boundary 保留记录，不计语义 unknown；纯 boundary 窗口无需模型 |
| Graph → Memory 归属 | 旧语义 stage 归属与 root 框架归属冲突，模型把 finish / 验证动作选作子 stage 关键动作 | 请求携带 root/semantic_review/stage_key_action 策略；按有效归属给出 stage 候选动作，校验拒绝框架 key action |
| Memory 事实 | 门禁、预算重置、运行终止、验证完成等框架状态被统一当作 intent | 维护明确事件白名单，赋予 observed_runtime_state，支持对应运行时事实 |
| Memory 恢复/工作流 | 用开始事件、模型结论、预算重置推断成功恢复或完整路线执行 | 继续要求工具结果，按实际交付来源限制 observed/proposed 契约；没有结果不能自动升级 |
| 工作流引用 | 每步最多 8 条合法引用，合并后却被 fact 的 8 条总上限拒绝 | canonical workflow 容量对齐 wire 契约，最多 8 + 10×8，不截断、不删除步骤证据 |
| 显式 Retry | 自动修复返回空数组后 rejected_records 被清空，只剩 quarantine 和错误摘要 | 每次修复从 quarantine 恢复候选和稳定身份；已接受记录不重写 |
| 有界分页 | 新读 page 被已有 delivery_span_ids 或后续 focus 排除，花了读取预算却没交付 | 将新 spans 加入 manifest 的交付集合；当前分页修复不再按旧候选缩小证据 |
| 可选错误候选 | 模型正确地认为不该保存并省略记录，但候选永久留在 quarantine | 增加显式 withdrawals，必须指明本窗口可选候选和原因；不把省略当成功 |
| 结构/必需判断 | 缺少子目标关系、stage 贡献，或真实动作 status=unknown | 保留质量缺口，提供明确反馈，定向重试；不因框架修复而掩盖 |
| 容量/提供方 | 请求字符、证据总量、调用/时间预算、超时、提供方错误 | 维持暂停/预算恢复逻辑；只在对应设置或提供方条件变化后恢复 |

## 框架事件的统一机制

策略集中在 `evaluation/framework.py`，被 extraction、Graph 输入、root 归属、
Memory scope、证据分类和状态统计复用。

| 证据/动作 | 可证明什么 | 不自动证明什么 |
|---|---|---|
| 工具真实输出 observed_output | 记录中的工具返回结果 | 所有任务要求已满足、该动作一定必要 |
| 白名单运行时状态 observed_runtime_state | 框架实际记录的门禁结果、预算变化、验证或运行状态 | 验证器结论必然正确、外部环境事实、完成声明真实、工具恢复成功 |
| 模型输出 model_claim | 模型表达的意图、结论或提交内容 | 已执行、已验证、已成功 |
| 历史记忆 historical_reference | 曾经记录或检索到的历史说法 | 当前环境的新观察 |
| 其他输入/边界 recorded_input_or_boundary | 输入、计划、检查开始或未分类的上下文 | 结果已经发生 |

运行时白名单采用明确事件名。未知事件保持上下文，不使用宽泛的“所有 acceptance/verification
事件都是观察”规则；开始事件、计划更新不升级。验证完成只能支持“框架记录了某验证状态”
这一事实，不能替代独立产物检查。

根任务专属的验证调用和 finish 仍是模型/工具调用，保留成本、来源和语义判断义务；
只有非调用 boundary 免除动作贡献判断。框架 root 归属不意味着必要性、有效性或验收成功。

## 修复和恢复不变量

- 原响应、引文、稳定候选 ID、已接受记录保留；改变的是明确版本的校验和调度规则。
- 旧窗口重试时，从固定来源恢复框架证据类别和当前有效归属，不读取当前 workspace 来补历史证据。
- 可选候选撤回独立留审计记录；不会发布为 memory，不删除已接受事实，不替代 required stage 判断。
- 真实 unknown、证据缺口、来源范围错误仍会使结果 partial；ready 不等于原任务通过验收。
- 本次规则在加载新代码后才用于后续处理。历史任务不会仅因文件更新而自动变为 completed。
- 服务直接加载工作区代码时，应避免让正在运行的评估跨越模块编辑过程。此次编辑中一次在途重试
  遇到短暂的重复 citations 参数导入错误；代码已修正，但该历史任务的 failed 状态尚未自动恢复。

恢复时先确保服务加载完整的新版本，再继续保存任务。对于 completed + partial 的任务使用
Retry failed items；对于因运行时/容量中断而 paused 的任务使用 Resume。已有有效记录继续保留。

## 验证范围与剩余限制

测试覆盖纯框架窗口不调用模型、旧 boundary unknown 完成状态恢复、真实动作 unknown 继续使结果
partial、框架动作归属、证据类别能力、十步 workflow 引用、空修复后的重试、分页交付、
显式候选撤回及禁止撤回必需贡献。另运行 evaluation 全套及 incremental graph 服务测试。

没有运行新的真实模型付费回合；模型是否遵守改进后的提示和 schema，仍应在重试样本上观察。
仅靠来源存在和字段规则无法完全验证事实语义、因果关系、恢复有效性或工作流优化收益。
自动修复仍受总调用预算和输入级次数限制；显式撤回解决的是可选错误提案的收敛，
不保证任意不完整 trace 都能变为 ready。
