# 按阻塞原因恢复任务

恢复分为评估、准备、提交和重新执行。`tasks/recovery.py` 读取错误码、验收 gap、预算数据和计划 blocker，生成带 source_key 的结构化报告。错误说明只展示，不决定恢复动作。

## 原因与动作

| 原因 | 动作 |
| --- | --- |
| 单纯验收调用次数/时间额度耗尽 | 续跑 checkpoint，按显式恢复请求更新本轮验收额度 |
| 每日 token 总额耗尽 | 等待 Agent 时区下一个自然日；账本按新窗口计数，累计历史保留，不消耗执行重试次数 |
| 模型连接中断、请求超时或临时服务错误，且结构化错误标记 retryable | 保留 task/tools/workflow/plan/acceptance，从 checkpoint 续跑；沿用既有自动恢复次数上限与调度间隔 |
| TLS 证书校验或非临时协议错误 | 不自动重试，保留错误码与嵌套异常类型，修正配置后再恢复 |
| 历史长期目标/Goal/工作包/学习子额度拒绝 | 不再扩容子额度；重新检查当日总额，移除 Session 累计 token 上限后续跑 |
| 工具不可用 | 检查实际 TaskAssembly bindings，补齐集合，并再次验证工具真实可用 |
| 验收输入/输出容量不足 | 在有界配置内调整验收预算，重组后重新验收 |
| 验收合同疑似扩大原要求 | 独立合同复核，记录 supported/overreach/ambiguous 与原任务的逐字引用 |
| 计划 blocked 或恢复工作流不兼容 | 重新生成 workflow/plan/context，保留原要求和历史证据 |
| 外部依赖、未知执行结果、人工控制 | 保留阻塞，等待对应条件解决 |

恢复前的模型复核与文件复制在锁外进行。提交前在锁内校验 Agent generation、Goal revision、来源状态、Session 所有权、run、task revision、完整 source_key、人工控制及未确认副作用。重启命令固定 command_id 幂等；过期且尚未提交的意图撤销，已接受命令保留可重放记录。

## 模型请求失败

模型请求异常按 Python 异常类型分类，流式与非流式共用规则。Worker 的 run.failed 保存完整错误码、retryable、cause、metadata 与 trace_id。历史 run 若只剩错误码和文字，只允许从同一 run 最后一次 llm.failed 事件补回结构化字段；出现更新的请求、完成响应或 run.started 时，不得借用旧失败。恢复报告记录证据事件序号，不扫描报错文字。缺少有效 checkpoint/上下文，或存在人工暂停、待答问题、未确认副作用时，仍保留原恢复门槛。

## 每日唯一 token 配额

规划、阶段规划、执行、验收、学习及方法学习共用 `budget.daily_tokens`。Agent 时区的每天零点切换自然日窗口，当日用量自动从零开始。历史调用、实际消耗、未知回复和预留记录均不删除；跨日迟到的结算仍更新调用发起当天的记录。事务内先预留后发送，防止并发调用分别占用完整每日额度。

长期目标、Goal、工作包、学习及每日调用次数的旧配额字段仅为已有 Role 的读取兼容保留，不参与准入；不再自动扩容这些子额度。每次调用的输出长度与运行时间、步骤、无进展等执行边界仍独立生效。新派发及恢复的 Agent Session 使用 `limits.max_tokens=null`，旧 Session 在版本 6 迁移时转为同一策略，独立人工 Session 的原限制保留。

每日预算拒绝在流式和非流式调用中保留同样的错误码与 scopes；流式异常使用 `LlmProviderError` 携带完整 `LoomError`。旧 run 若只记录了 `LLM_FAILED`，仅在宿主拒绝记录与当前 Agent/Goal/work/run 全部绑定一致、且拒绝后没有新的执行调用获准时，才可按预算原因恢复。没有宿主记录时不解析报错文字。学习等待每日额度不会耗尽重试机会；新窗口可自动恢复同一评估 checkpoint。


## 工具与只读验证

内置 `bounded_context` 和 `research_context` 的请求窗口直接使用运行时传入的 `limits.max_window_chars`。内置模板不再设置独立的 24,000 字符窗口；旧 checkpoint 中的插件 `max_window_chars` 仅保留用于配置身份兼容，不再作为额外上限。工具定义和回复预留仍在同一请求窗口内计入，固定上下文确实无法容纳时继续停止。

Work 和验收条目可声明 `required_tools`。执行前检查实际工具绑定；缺少工具会生成 `ACCEPTANCE_CAPABILITY_UNAVAILABLE`，无需先执行计划再被验收拒绝。集合名称存在不代表其中的工具通过了资源权限过滤。

只读工作区需要执行 shell/process 验证时，恢复会复制完整当前工作树，包括未提交内容和依赖，到服务数据目录下的独立验证目录。复制前后内容指纹必须一致；指向源树外的符号链接、链接式 Git worktree 和特殊文件不自动复制。TaskAssembly 将执行资源绑定到副本；原始约束、源路径、源指纹和执行路径记录在 recovery_context。普通 Session restart 仍不允许更换工作区，只有匹配宿主已提交重组意图的命令可切换到这个验证副本。

这是独立工作副本，不是操作系统沙箱。任务说明要求命令在副本运行；验收时宿主再次校验原树指纹，将 source-preservation 作为证据。如果原树已变化，则停止并要求复核基线，不把旧结果作为当前验证结论。同时核对副本内 Git 跟踪及未忽略文件的输入清单，防止通过修改被测源码取得通过。被 Git 忽略的构建生成物允许写入副本；原树内容必须保持不变。独立目录和收据保留用于审计，不自动删除。

验收命令输入和 scope 中的绝对源路径同步映射到副本；审计仍保存映射前合同。已准备的重启命令同时绑定 command_id、source_run_id 和完整 task 摘要，不允许把复核结果附到另一份任务描述上。

## 验收合同复核

`reassembly.py` 的合同复核不评价任务是否通过，只评价原条件是否被正确解释。每个条件必须被覆盖并引用原始要求。只有 overreach 可修正派生条件，supported 条件不能修改；宿主 full-goal 条件始终存在。ambiguous 停止自动恢复。

复核是有证据出处的模型判断，不是对任意自然语言的形式化证明。复核记录、旧合同和修正后的合同均持久保存。重组后加载修正合同并重新获取执行证据，不继承之前的 passed 状态。“本次不修改文件”与“工作区起初干净”分开判断。

## 计划与显式重试

计划项支持 `blocked`，附带 TOOLS_UNAVAILABLE/TOKEN_BUDGET/ACCEPTANCE_CONTRACT/EXTERNAL_DEPENDENCY/EXECUTION_UNKNOWN 及 tool_ids。遇到 blocked，执行停止并进入恢复。新 skipped 项必须带 not_applicable 或 superseded；缺工具、缺预算和缺证据应使用 blocked。直接回答和 finish 都不能绕过未完成计划。

执行队列提供“重新评估恢复”。Agent recover 命令要求 work_package_id、reason、expected_revision、command_id；它最多补足一次新的恢复机会，不清零累计 attempts、预算账本和历史，也不会越过人工暂停/待答问题/未知副作用。老数据不通过匹配报错文字猜原因；缺结构化信息时走有界兼容复核，无法生成可验证配置则保留人工阻塞。

## 服务进程恢复

启动脚本遇到丢失或陈旧的 PID 文件时，通过 lsof 查找当前用户的监听进程，并要求其同时持有该数据目录的 supervisor.lock、进程命令属于 loom serve，才恢复 PID 文件。无法确认归属时仍拒绝接管；占用相同端口本身不是终止进程的依据。
