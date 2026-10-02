# Trace 五维分析样本核对

日期：2026-09-05。方法：只读原始 JSONL、核对当前源码，并直接调用现有 graph / rules / evidence-pack 函数复现其行为。以下是人工诊断样例，不是新分析器的自动运行结果。

## 样本与证据身份

- 样本 A：[smoke-kimi-20260718-173448-193048.jsonl](../../runs/smoke-kimi-20260718-173448-193048.jsonl)。SHA-256：`63bb4989c97138a9c7ef4281486442c1bac20cd45005232350cc1ae7db80a4ec`。
- 样本 B：[yakdb-incremental-index-latest.jsonl](../../runs/yakdb-incremental-index-latest.jsonl)。SHA-256：`326e6a00dacb9db65e094444b26fc461b8cf567346cfd78d81ca32a9b1ed3b98`。

行号均为原始 JSONL 物理行号；字段路径以下均相对于原始记录的 `payload`。A 做五维人工分析；B 只用于执行顺序和 Step 身份核对，不声称已审完 B 的任务效果。

## 1. 当前分析器可复现的盲点

对 A 调用 `load_normalized_events`、`build_episode_graph`、`assess_steps`、`build_round_evidence_packs` 得到：

- 1 Run、1 Step、8 LLM rounds、7 tool calls。
- 规则总分 `0.9166666666666666`，唯一 finding 是 `high_token_usage`。
- 8 个 Round evidence pack 的 `tool_calls` 长度与 `tool_call_count` 全部为 0。

原因：该历史 trace 通过模型 content 中的 JSON action 发起工具；`judge._tool_call_ids_from_round()` 只读取 native `response.tool_calls`。工具调用记录及其 ID 实际存在，不能据空 evidence pack 判断未使用工具。样本 A 的 metadata 没有直接父 `llm_call_id`，但 ID 如 `...-llm-1-json-tool-1` 包含旧生产器的关联约定；恢复时必须标明依据。

对 B 重建得到：1 Run、8 Steps、37 rounds、37 tools。Step number 是 `[0, 2, 4, 6, 24, 29, 34, 43]`，并非连续序数。

其中一个 Step 在 graph 中的请求行号顺序是 `55,118,125,132,139,146,153,160,167,62,69,76,83,90,97,104,111`，对应 `llm-1,llm-10,...,llm-17,llm-2,...,llm-9`。这是 bucket key 字符串排序造成的分析器重排，不能当作 agent 实际执行顺序。

## 2. A 的任务与时间线

第 3 行 `messages[0].content` 记录的目标：简要检查项目，构造最小 E2E smoke test 并运行。约束还要求先检查结构/文档、执行相关验证、依据证据给出建议，并通过 finish 提交报告；结构化 criterion 只写了包含目的、smoke 结果和改进方向的 Markdown 报告。

这里已存在任务契约覆盖问题：report criterion 没有完整表达“构造并运行”的要求。agent 选择复用已有 smoke script，应显式说明其适用性，而不能由 report 产生自动证明任务全部完成。现有脚本足以满足何种验收范围，应在报告中限定。

| Round | 请求/回复行 | 动作与结果行 | 可观察的进展 |
| --- | --- | --- | --- |
| 1 | 3 / 6 | 7–8：目录列表 | 找到 README、项目配置、已有 smoke script 和测试目录 |
| 2 | 9 / 12 | 13–14：README 前 8,000 字节 | 获得项目定位和 embedded 使用线索；文档覆盖不完整 |
| 3 | 15 / 18 | 19–20：smoke_test.py 全文 | 找到可复用的有限 E2E 验证路径及断言 |
| 4 | 21 / 24 | 25–26：pyproject.toml | 获得依赖、运行和测试配置信息 |
| 5 | 27 / 30 | 31–32：执行 smoke | 退出 0，产生 index/read/search/glob 的实际结果 |
| 6 | 33 / 36 | 37–38：列出测试/包目录 | 发现可运行的测试集合；目录名只证明存在性，不证明覆盖质量 |
| 7 | 39 / 42 | 43–44：执行 pytest | 输出 120 passed、11 warnings，进程退出 0 |
| 8 | 45 / 48 | 最终回复内 custom finish action | 汇总报告；没有对应 `tool.started: finish`，须区分 finish 声明和工具执行 |

## 3. Context 有效性

正向证据：Round 3、4、5 分别利用前面读取的 README、smoke script 和配置决定下一步，形成可回查的信息—行动链。请求消息数由 2 增至 16，内容字符数由 2,502 增至 28,162，完整请求保留在 trace 中。

具体问题：第 14 行 `output.value.truncated=true`、`bytes_read=8000`；第 48 行报告却断言 README 未提及 smoke script。当前所有读取中没有 README 后续分段或完整搜索。

- 已观察事实：README 读取不完整；报告包含针对整份 README 的否定性判断。
- 支持的诊断：结论超出当前证据覆盖范围。
- 未证明的结论：README 实际是否提及该脚本；不能由这条 trace 判定。
- 候选机制：截断/覆盖状态没有约束最终表述。无法仅凭单次运行区分 prompt 问题、agent 未遵守已有信息或其他认知因素。
- 优化假设：要求否定性文档结论附完整搜索范围，否则降为“已读部分未见”；用包含尾部反例的样本验证。

不能仅因 README 长而建议删去全部文档；它为选择 embedded smoke 路径提供了有效信息。

## 4. Tool 有效性

7 次工具调用均有配对记录；shell 结果含 exit_code、stdout、stderr、timed_out，read_file 结果含 content、bytes_read、truncated。它们在 `output.value` 内。

有效行为：先读已有 smoke script，再运行，避免未经检查直接重建；测试结果进入后续请求和最终报告。没有证据表明这些读取是无效工具调用。

值得优化验证的机会：Round 2、3 的 reasoning 多次提到可一起读 pyproject，但实际分轮读取。可以测试合并独立读取是否减少一次往返；目前 trace 没有证明可用工具组合一定支持相同语义和结果，因此这是候选实验，不是确认的浪费。

分析器自身缺陷：Round judge 没有拿到这些 JSON action 的关联工具；旧结果摘录也没有专门解包 `Observation.value`。先修复证据供给，再评估 agent 的 tool effectiveness。

## 5. Loop 进展

本样本的八轮分别贡献项目发现、用途理解、测试复用、运行配置、smoke 结果、测试发现、回归验证、报告交付。没有明确证据支持“连续空转”。

第 44 行的 warnings 是有效负面信息；第 48 行将未 await 的 coroutine 警告带入改进建议。这一行为应保留。未读取 watch_service 实现，故“会泄漏任务”等具体根因只能作为风险推测，不能作为已验证故障。

进展盲点集中于最后的要求核对：已有脚本的复用是否完整满足用户意图、所有报告声明是否被已读证据支持、finish 是否按所要求的执行路径发生。模型输出可解析和 runtime pass 不能代替这些检查。

## 6. Token efficiency

将第 6、12、18、24、30、36、42、48 行 `response.usage` 按调用求和：

| 项目 | 实测值 |
| --- | ---: |
| Prompt tokens | 46,503 |
| Completion tokens | 2,727 |
| Total tokens | 49,230 |
| Prompt 占比 | 94.46% |
| 首轮 / 末轮 prompt tokens | 1,196 / 9,049 |

输入量随累计历史增加，说明优化应检查历史内容的携带成本。以上占比不能证明 94.46% 是浪费；没有 cached token、来源级 tokenizer 计数或对照实验，不给出可节省 token 数。

可测试假设：对已消费的 README/工具输出保存任务相关事实与可回读引用，减少后续重复输入。验证指标是实际输入 token 变化、逐项验收结论和证据准确性；保留真实 smoke/pytest 执行及 warning 报告。

## 7. Verify gate

| 验收对象 | 当前证据 | 结论及范围 |
| --- | --- | --- |
| 项目定位 | README 已读段、配置、目录列表 | 支持限定范围的简要介绍；目录存在不能证明模块实现 |
| 有效 E2E 路径 | 第 20 行 smoke 源码和第 32 行执行结果 | 有 read/search/glob 断言，支持该次 embedded 单文件路径；不能证明所有功能 |
| 测试集合执行结果 | 第 44 行 stdout/exit_code | 支持本次命令 120 passed、11 warnings；没有逐测试内容，不推断功能全覆盖 |
| 构造 smoke test | 无写入调用；读取并复用了现有脚本 | 复用已说明；是否满足“构造”要求需明确验收语义，不能伪称本次创建 |
| 文档缺口声明 | README 仅部分读取 | unverified，需完整搜索或限定表述 |
| 改进建议 | warnings、smoke 输出、pytest 配置 | 部分有直接证据；对排序质量、警告根因等判断仍需定向验证 |
| finish 执行 | 第 48 行 custom action 文本，无 finish 工具事件 | 可确认模型提交报告的意图/内容，不能声称 finish 工具已执行 |

没有读入历史 commit/diff 或验证对象摘要，不能证明测试与最终版本的不可变绑定。本样本也未记录写入动作，不应因此凭空报告“测试后发生修改”。

建议的验证框架：显式列出任务要求；每条最终声明绑定读取/命令结果和覆盖范围；允许复用现有测试但记录理由；区分 runtime 正常结束、report 已生成和要求已验证。上述建议尚未实施或运行。

## 8. 对新分析器的精度要求

本样本理想的输出应同时做到：

1. 发现 README 越界否定结论，定位第 14、48 行；不编造 README 未读部分。
2. 恢复全部 7 次 JSON action 工具关联，注明旧 ID 规则；准确读取 `output.value`。
3. 认可 smoke/pytest 的有限验证价值及 warnings 的后续利用。
4. 给出 49,230 token 的可核对拆分，避免由总量直接判浪费。
5. 区分复用测试、创建测试、报告生成和 finish 工具执行。
6. 对样本 B 恢复真实记录顺序，保留原始非连续 Step number。

这组检查比“分数是否提高”更适合作为精确分析第一阶段的验收依据。
