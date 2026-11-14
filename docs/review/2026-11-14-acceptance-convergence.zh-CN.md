# Acceptance 无法在运行预算内收敛：第二轮根因与修复

日期：2026-11-14（Asia/Shanghai）。基于本机 service.sqlite、事件及不可变 checkpoint；保留任务要求，不手工写入验收通过。

## 这次实际暂停

- `sess_6d90b49985204a7b8916bdcbb6832b4b` 于 01:00:56 暂停。当前已是新任务/合同，不是上一轮记录的 auto profile 问题。第一项 `seven-dimension-analysis` 装入 82 项证据、约 543,181 估算 token，超过 16 页。调用记录为 21 read_file、49 shell_execute、12 list_directory；大量 shell 输出也在读取文件，单纯合并相同 read_file 无法解决。
- `sess_7d88af2198504463917afad78fd663bf` 于 00:33:32 因 ACTIVE_TIME_BUDGET_EXCEEDED 暂停。验收已发起 26 次模型请求，耗时约 2,352 秒。同一组读取材料按不同语义标准重复分页。该模型配置允许 65,536 输出 token，短页观察也得到同样额度，自动 reasoning 分配过大。
- 原 Resume 回到 solver，报告措辞变化也会改变 candidate fingerprint，丢失本可继续使用的分页进度。已支付的判断只保存在 reviews，恢复时还需要重走旧分组才能取回。

原始 checkpoint：6d90 = `645d9a72a179dacc6b73c62f0791280da1fb2bdf64356c803c2d839b6bb0d125`；7d88 = `6a8ada31b46681e4f72519e416f9150d10964dbf49b19ef984eac0926f56a698`。

## 修复

1. 大视图先向独立验收模型提供完整来源目录和有界预览，选择支持/反驳当前要求的原始凭证，再加载所选原文。目录不省略旧来源，选择结果不具有 passed 权限。最终判断明确看到遗漏来源和数量，可针对路径或工具补证据；显式路径补验固定保留对应材料。原 journal/artifact 不删除，源内容不因选择而截断。
2. 证据高度重叠的语义标准共享选择和分页审读，仍分别输出每项结论，goal 仍是独立门。命令检查继续使用本次执行凭证。
3. `select_evidence` / `verify_page` 默认输出上限 4096，`verify` 为 8192，包括协议修复请求；可用 `page_max_output_tokens` / `review_max_output_tokens` 显式配置，供应商或任务的更小上限仍有效。Qwen 未显式指定 thinking budget 时为回答保留空间。solver 的模型设置保持原样。
4. 选择、页观察和完成的判断逐步持久化。恢复时检验原 artifact，保留完成项和 goal 所需凭证；缺证据视图的未完成项从已知缺口继续。未变更的候选在 Resume 时直接进入验收，不再次让 solver 改写报告。不可缓存的命令仍重新执行。
5. 保留每批 24,000 token 目标、16 页、单请求超时及用户总运行预算。没有把这些上限整体放大，也没有将遗漏视为通过。

## 验证

- 完整任务/验收/服务/统一规划/ManagedStep 回归：341 passed（181.57 秒）。
- 最后补充的不可缓存命令 Resume、选择与批次回归：38 passed（20.52 秒；与完整集重叠，不累加）。验证恢复不新增 solver 请求、不可缓存命令重新执行，以及未知证据 ID 无法产生通过结果。
- 真实 checkpoint 的只读输入规模校验：保留全部来源目录后，选择请求约 16,318 / 18,716 估算 token，均低于 24,000 目标。此校验使用模拟选择，仅验证输入编排，不代表业务验收结论。
- Ruff / diff 检查通过。两个 session 均通过正常 Resume 恢复原 run；线上结果在下方继续记录。

## 同轮发现的服务端阻塞

- native sample 显示服务线程持续执行 SQLite `jsonExtractFunc`，worker 在模型已返回后仍等待服务。临时 Python 调用栈进一步定位到 `LearningCoordinator._advance_cycles -> learning_queue.reconcile`。
- 旧查询在写事务内 join 每个 Agent 的全部 run 与全部事件，用 JSON 的 type/state 聚合终止位置。真实库 `EXPLAIN QUERY PLAN` 选择 `events_type(session_id=?)`，每个 run 再解析并过滤整个 session 的事件；已入队的历史 run 也重复扫描。这会阻塞其他写事务，造成多个验收一起停等，并消耗 active time。
- 修复为跳过已有 queue_order 的已入队 run；只有旧格式迁移才查全量。开始位置读取 `events_run` 表达式索引；终止位置通过 `events_type` 限定三个生命周期事件类型。当前 run 的 id/state 直接读取两个字段，不构造包含上下文的兼容 session JSON。
- 真实库只读测量：两名 Agent 的全部 195/33 个历史 run 分别 2.107/1.228 秒；正常待补查均 1 个，分别 0.007/0.015 秒。查询结果保留原始 rowid 排序和终止 cursor，不改写任何验收结论。
- 增加回归测试：2000 条带大 payload 的 token 流与多 run 混合，替换 SQLite JSON 解析函数，任何读取 token 流的行为立即失败。终止游标、顺序、幂等、尚未原子提交的失败事件保护均通过（10 passed）。

## 其他收敛修正

- worker 的 read_artifact / poll_control / check_control / status 使用只读事务；校验 session 归属、epoch/attempt 和运行状态，不把读取结果重复存入 worker_records，不重写上下文。预算检查包含未落盘时间，下一次写入仍完整计费，重复查询不重复收费。
- 同次验收使用有界不可变来源缓存，避免重叠批次重复 RPC；文件新鲜度和命令缓存判定照常执行。
- 页观察字数改为提示目标，provider 输出预算仍为硬限制；合法但略超目标的文字不再无谓重试。合并观察超输入预算时，分层归纳并保留所有原始凭证引用，最终判断仍独立执行。
