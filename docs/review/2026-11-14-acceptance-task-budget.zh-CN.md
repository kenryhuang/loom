# 分阶段验收后的任务总预算耗尽

2026-11-14，基于本机 service.sqlite 只读查询及不可变 checkpoint；没有改写在线任务额度或验收结论。

## 实际暂停原因

- `sess_7d88af2198504463917afad78fd663bf`，北京时间 11:16:12 暂停。checkpoint `a397eca07deeeb4a9c006845c6b759259158ad5cc6585309fb52035bff03349e`。
- 任务累计调用 200 次，`task.limits.max_llm_calls=200`，`max_tokens=null`；累计 token 6,529,060。直接阻塞的是任务调用数上限，不是 token 上限，也不是验收本轮预算。
- 7 项非 goal 标准都已通过。新 `verification_ledger` 已存在；本轮已保存 2 项工作，goal 第 1/4 页在模型调用前被拒绝。进度没有因压缩丢失。
- 普通 Resume 续期 active time / acceptance allocation，但保留任务累计调用数。因此到达 200 次后，继续 Resume 无法产生新模型调用。
- 另一条最新记录 `sess_6d90b49985204a7b8916bdcbb6832b4b`，北京时间 11:25:46，checkpoint `671d4e3c0df827c7dcb076253b1c66b3298d4749f7a838d21c0e859d2fcf9c00`：3 项非 goal 标准已通过，16 个工作项已提交，goal 第 2/4 页因观察数组没有准确覆盖请求 criterion 而协议失败。不能把这条暂停也归为 budget。

## 结构性缺口与修正

最终 goal 仍默认装入大量原始工具输出，再次选源、分页，重复了详细标准的审读。现改为：有详细判断凭证时，初验提供候选交付、完整目标/合同、当前逐项结论、独立判断凭证及来源目录引用。goal 仍独立判断完整性与跨项一致性；如果缺少决定性事实，则通过具体 path/tool/source 缺口补读原始材料。没有详细凭证时保留原始证据路径，特殊 policy 仍拥有自己的证据选择规则。

恢复旧 checkpoint 时，仅替换尚未完成的旧 goal 证据视图，归档旧视图；保留组件结果、已付费观察及凭证，不重新执行 solver 或确定性检查。

另外修复：

- 调用额度耗尽使用 `MODEL_CALL_BUDGET_EXCEEDED`，与 token 超限分开，给出实际使用量和上限。
- 外层预算准入发生在验收调用计数增加之前；被拒绝工作进入 `waiting_budget`，不消耗工作项 retry allowance。
- public/UI 单独展示任务累计调用预算，明确 Resume 保留使用量。
- 任务调用硬限额的恢复分类要求明确处理预算，不再归入可盲目 Resume 的 token/验收预算问题。

## 真实材料的规模检查

使用两个原 checkpoint 构造新 goal 输入（只读、无在线模型调用），含响应 schema 的估算输入分别约 15,110 和 20,910 token，均低于 24,000 目标；尚未计入运行时环境提示的少量开销。旧路径两者均计划 4 页。该检查证明编排规模降低，不代表业务验收通过。

任务已经消费的 200 次调用不能因优化或恢复被清零。继续实际验收需要明确调整任务总额度；本次未自动扩额或发起付费 Resume。

补充：新增显式 `set_model_call_budget` 服务命令（`payload.max_llm_calls` 为正整数），仅允许非运行状态修改。与 token 预算调整一样幂等提交，保留 run/checkpoint/累计使用量，不自行 Resume；客户端事件投影同步支持独立调用额度，避免覆盖 token 上限。

验证：主回归 186 passed；账本与服务验收组 24 passed（含重叠测试）；独立调用额度修改及原 token 额度恢复测试 2 passed。真实业务验证仍未发起。
