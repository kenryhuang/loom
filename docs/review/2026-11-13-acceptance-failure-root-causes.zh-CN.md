# 最近 acceptance 失败：根因与修复

日期：2026-11-13（Asia/Shanghai）。检查本机 `runs/service/service.sqlite` 的只读快照、事件与 checkpoint artifact；未改写验收结果、调用记录或下游工作区文件。

## 证据与分类

| Session | 观察到的阻塞 | 根因与处理 |
| --- | --- | --- |
| `sess_7d88af2198504463917afad78fd663bf` | 22:27:29 的事件 29794：`ACCEPTANCE_INPUT_BUDGET` / `max_evidence_pages`；测试和类型检查的宿主命令均退出 0，语义判定尚未执行 | 验收按调用次数重复装入相同文件全文；命令判定又带入无关 shell 历史。修复证据选择与去重，保留独立语义门。 |
| 同上，修复上线后的真实复验 | 23:27 的事件 30677：测试执行 61,061ms 后退出 124，却标成 failed 并触发业务修复 | 合同给 vitest 900 秒、tsc 600 秒，但默认 `command_timeout_seconds=60` 被误当显式上限。修复默认值/显式上限的优先级，将实际时限计入分配；超时、取消和无法启动归为基础设施阻塞，正常非零退出仍是业务失败。 |
| `sess_b099fa9421e84cd98e308b9dba7b6d02` | `ACCEPTANCE_TIMEOUT`，单次 verify 180 秒；旧请求约 174,230 字符 / 67,072 估算 token | 旧验收请求过大。现有逐项批次和分页提供输入上限，本次去重进一步减少重复材料；不通过延长所有请求超时掩盖问题。超时仍保留为基础设施阻塞。 |
| `sess_30b2cba036f04143a3115b3320cea2a5` | `Active time budget exceeded` 被归为 `ACCEPTANCE_BLOCKED` | 外层执行时间耗尽，不是模型判定任务不合格。修复控制信号的原因码传递，保持暂停，不消费新的模型请求、不生成业务修复缺口。 |
| `sess_6d90b49985204a7b8916bdcbb6832b4b` | `smoke-test-pass` 因没有 shell 实际执行凭证而阻塞；后续 run 另有 `database is locked` 失败 | 当前任务明确“只读分析、不写报告”，工具只有 filesystem/task_control，但旧 auto profile 把 `reports/` 的 `repo` 子串当作项目审计，额外注入 smoke-test 输出合同。新任务使用 general，显式 project_audit 保留；已有错误合同明确要求 Revise/owner revision，不在 Resume 中改变 context。数据库锁失败是独立执行错误，不伪装为验收结论。 |

最后一项的 task objective 与 session title 不同；分析以保存的 task/context 为准，不能只看标题。其 checkpoint `context.metadata.profile=project_audit`，goal criteria 为自动模板的“project purpose, smoke test result …”，并非该只读工作包声明的验收项。

## 目标 session 的可复现规模

原始 checkpoint：`3417d9c5a6390a62ddc8ff84743fa07ce2fe5cb9900dc3b631c29ed105528a9f`。

- 238 条工具调用：138 read_file、49 shell_execute、46 list_directory、3 edit_file、2 write_file。
- read_file 有 136 次成功调用，但只有 36 份不同的输入/内容/版本记录，涉及 15 个路径。
- 三项语义标准各自装入约 **1,357,688 字符 / 493,841 估算 token**，直接超过 16 页上限。
- 修复后每项为约 **342,710 字符 / 125,294 估算 token，9 页**；136 次执行仍保留，100 次重复内容只共享展示，不减少实际调用计数。
- 测试判定从约 299,432 字符降到 37,072；类型检查从约 270,040 降到 7,680。采用本次独立检查的原始输出，显式证据缺口仍能展开历史命令。
- 全流程离线回放使用真实证据和模拟判断：38 次请求（32 页观察、6 次判断），所有请求满足 24,000 token 批次目标，最大 payload 估算为 23,258 token，所有单项均在 16 页内。**这是编排/预算验证，不是业务验收通过。**

## 修复边界

1. 仅合并内容、输入、文件版本和路径一致的成功本地读取，保留等价调用 ID。不同版本、不同读取范围、失败、shell 命令及外部读取均不合并；原始 journal、artifact 和 tool_call 次数不变。
2. command 检查自带宿主刚执行的凭证和完整输出，不因同名 executor 附带所有历史命令。其他已声明工具和显式补验证据仍保留。
3. 页数不足时报告具体标准、证据数量、估算输入量与批次/页数限额，避免展示上一轮过时的 input_budget。
4. auto profile 不再通过 repo/project/report 等关键词追加业务要求；显式 profile 仍有效，任务原文中的测试要求仍须被验收。
5. 恢复旧的自动审计合同保留 context、计划、证据及预算，返回明确 `ACCEPTANCE_CONTRACT_INVALID`。通过 Revise 或计划所有者修订重建，不能擅自删除用户明确要求的测试。
6. 暂停、执行预算中断保留其原因码；超时、分页阻塞和业务未通过不混为同一种错误。
7. 存在 pending_verification 时，Resume 保留已有页/批次及旧反馈状态，不把更早候选的 applied feedback 重新置为 pending。目标 checkpoint 的旧修复反馈属于 candidate `52c8b1f…`，当前待验收 candidate 为 `e77ada7e…`；旧逻辑会重放过期修复，并可能把当前命令的 pending 判定转成 UNKNOWN_GAP。新增三类基础设施阻塞的恢复回归。
8. 命令超时使用检查声明值；未声明才回退到默认值。用户显式配置的命令上限和总验收上限保持有效。超时保留原始输出/执行凭证，以 `ACCEPTANCE_COMMAND_TIMEOUT` 暂停，不生成代码修复反馈、不增加 failed_attempts。取消、启动失败分别保留明确原因；内部检查抛出的 `AcceptanceBlocked` 不再被通用 ValueError 捕获抹掉原因码。

## 验证

回归覆盖重复读取超页、136 次调用的原始计数、checkpoint 恢复、版本/范围/外部操作隔离、命令证据及显式补验、独立 goal 门、旧合同阻塞、显式审计合同、暂停原因码，以及真实进程超时、声明时限/显式上限、启动失败和取消。最终完整任务/验收/服务/统一规划回归 **279 passed / 260.10s**。Ruff 与 diff 检查通过。

上线期间先暂停目标 session，重载服务后使用正常 Resume 恢复原 `run_93b693bd527f48119f1e4c6d51e9e132`；没有 Revise/reset context 或手工写入 passed。第二轮恢复命令为 `acceptance-deadline-fix-20261113-7d88af`。事件 30843 确认原测试以 900 秒时限重跑；最终业务验收状态以该 run 的后续独立判断为准。

23:36:27 的事件 30846 返回实际测试结果：执行 105,125ms，宿主未超时；79 个测试文件中 78 个通过、1 个失败，387 个测试中 386 个通过、1 个失败。失败为 `tests/components/practice-workspace.test.tsx:206` 的组件导航渲染测试，触发 Vitest 自身的 5,000ms 超时。solver 命令尾部 echo 使 shell 退出码为 0，但 stdout 明确 `EXIT=1`；不能将该命令退出 0 当成测试通过。合同中的独立测试命令及输出语义检查仍保留。此时 session 仍 running，业务验收未通过；这是下游测试结果，不再是 60 秒宿主截断或证据页数不足。

总预算中断原因码的末尾补充通过预算专项 **35 passed**。这些用例与完整回归重叠，不另行累加。
