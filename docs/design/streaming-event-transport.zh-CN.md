# Streaming 事件的异步批量持久化

## 问题与测量

原来的 service worker 每产生一个流式 delta，都会同步执行 `Pipe.send/recv`，等待 supervisor 处理事件、
更新累计文本、保存完整 session 快照并提交 SQLite 事务后，才消费下一段流。

对 `sess_332a31e7c9124c5caf0a587dd6486cf4` 的 Action 28 做本地离线回放：

| 路径 | 耗时 |
| --- | ---: |
| SSE 解析与组装 | 0.01 秒 |
| 直接事件处理与落库 | 10.3 秒 |
| 原 worker RPC + supervisor 调度 + 持久化 | 40.7 秒 |
| 新异步批量路径，包括末尾持久化确认 | 0.85 秒 |

数据为 921 个 delta，初始 session 快照约 600 KB。改造后 16 次 RPC（15 批 delta 和 1 个流结束事件），
回读确认全部 921 个 delta 已保存。数据来自只读生产记录，回放仅写入临时数据库，未调用模型或改写生产记录。
这是无网络、无生产 UI 订阅的快速回放，不能从原模型调用的 94.36 秒中直接减去这些数字，
也不能据此承诺真实模型调用降到 0.85 秒；真实批量大小取决于流的到达节奏和持久化速度。

## 执行协议

```text
模型流消费协程
    │ enqueue delta（容量不足时异步等待）
    ▼
有界 outbox → 唯一 RPC 线程 → event_batch → 单个数据库事务
    │                                         │
    │                      逐条保存原始事件、更新 offset
    │                      每批仅保存一次完整 session 快照
    ◄────────────────────── 持久化确认 ─────────┘

完成／失败／工具执行／checkpoint／控制请求
    → 进入相同 FIFO 队列 → 等待此前批次及本请求确认
```

- 仅 `llm.content.delta`、`llm.reasoning.delta`、`llm.reasoning_context.delta`、
  `llm.tool_call.arguments.delta` 可以提前返回“已入队”。其他事件仍等待持久化确认。
- 单独线程独占 Pipe 的发送和接收，避免异步 delta 与同步工具 RPC 争抢响应。
- 默认 outbox 上限 256 条、512 KiB；计入正在发送但尚未确认的数据。达到上限时异步等待容量，事件循环继续运行。
- 默认聚批窗口 150ms，最多 64 条／64 KiB；遇到后续关键请求立即结束当前批次。
- 单个超大事件不进入异步 delta 批处理。超大关键请求只在队列空时单独接纳，保留大 checkpoint／artifact 的兼容能力。
- 不拼接、删除或压缩原始 delta：每条保留源时间、通道、调用 ID、原始字段及原有 offset 语义，仍可完整回放。
- `event_batch` 复用原有 epoch 校验和 record ID 幂等机制；整个批次在同一事务中提交或回滚。
- 完成、失败、工具执行、控制和 checkpoint 请求共用 FIFO。关键请求返回意味着排在它之前的 delta 已提交。
- 持久化／RPC 失败会拒绝后续请求并唤醒等待者，不允许在丢失先前写入确认后继续执行工具。
- 正常退出会排空队列。worker 被强制终止时，尚未确认的预览 delta 尾部可能丢失；任务通过原有 checkpoint／operation journal 恢复，
  未提交的模型调用不能因此变成完成。这里有意将“预览已入队”与“关键状态已持久化”区分开。

## 范围与限制

本次把同步等待移出流消费协程，并把逐 delta 的完整快照写入降为逐批写入。
SQLite 仍使用原有持久化事务，未关闭同步保障。未改变任务工具调用顺序、验收、暂停／恢复协议。

入站 HTTP SSE 队列、浏览器 SSE 和 session 快照的数据结构未在本次重构。
当前 model duration 仍是客户端观测耗时，不等同于服务端纯 decode 时间。下面的分层计时用于定位本地积压，不能分离服务端计算与公网传输。

关键实现：`src/loom/service/worker_transport.py`、`workers.py`、`controller.py`。
新增测试覆盖：非阻塞入队、背压、关键请求顺序、失败传播、批次幂等与 offset、epoch fencing、退出排空，
以及真实 spawn worker 的完整流式任务。

更新服务代码后需重启服务，新的 worker 才会使用此协议。历史报告中的已记录调用耗时不会被追溯修改。

## 验证结果

共 246 项 service／LLM 相关测试通过，2 项跳过。新增 9 项测试包含真实 spawn worker 的流式完成和 SQLite 错误回传；
Ruff、diff 空白检查通过。另有 1 项现有分页读取测试 `test_service_reads_source_pages_to_the_end_without_refetching`
因 `read_more` 缺失失败；在临时源码副本中恢复旧 `workers.py`／`controller.py` 后同样复现，未在本次扩大修改范围。

## 自动验收证据归档的展示与复用

每次成功工具调用没有自带 artifact 时，`acceptance_evidence.record_call` 保存输入和完整输出为
`acceptance_tool_evidence`，供验收、上下文压缩后的证据回读和 journal replay 使用。
这类内部归档保留在 trace 中，但 Web／TUI 的 compact process 不再显示独立的 Artifact saved 行。
报告、来源归档和 verification evidence 等其余 artifact 类型仍按原逻辑展示。

服务 worker 对这一个内部 kind 增加最多 128 条的 LRU 引用缓存：按完整输入／输出的规范化 JSON 哈希匹配，
仅在 RPC 已确认成功落库后加入。同 worker 再次提交相同内容时复用引用，省去重复 RPC、事务和 artifact.created 事件；
工具执行及调用索引仍逐次记录，输入或输出改变则重新归档。失败、不同 kind 和用户报告不复用此缓存。
worker 重启后缓存重新建立，原来的持久化归档及 journal replay 规则不变。

历史样本：`sess_54adad54dcaf486380b435e757dbed56` 的 247 次验收归档中只有 60 个不同内容哈希，
说明文件系统已有的内容去重仍留下了重复 RPC 和事件。另用 `sess_332a31e7c9124c5caf0a587dd6486cf4` 的
10 份首次归档在临时数据库回放，含 worker RPC／调度／持久化共 0.47 秒，平均约 47ms。
该数字不是生产现场耗时；Artifact saved 事件本身没有独立的精确 duration，不能从单个时间戳推断落盘时长。

## 真实流速复核与分层计时（2026-10-18）

`sess_9e01d073cca0480289460f4be607ebee` 已使用异步 outbox，但仍执行了约 628 秒。
14 次模型调用累计 596.76 秒，输出 26,118 tokens；其中 12 次任务流式调用产生 7,017 条 delta。
本轮模型是 `qwen3.8-max`，此前基准 session 是 `glm-5.2`，不可将跨模型 TPS 差异视为优化收益。

本次实际 attempt 有 6,431 条 worker RPC 记录（包含其他关键请求）。流平均每约 75–80ms 到达一个 delta，
原 25ms 窗口很难合批。将窗口调为 150ms，并添加每 75ms 发送一条 delta 的持续流测试；
保留原容量、顺序和关键事件立即结束聚批的规则。预览更新会增加最多一个聚批窗口的等待，
批量落库仍在后台，不让模型消费主动等待该窗口。

使用该 session 的 36 条 delta 和约 324 KB 快照，在临时数据库按每 75ms 一条回放：
25ms 窗口产生 37 次 RPC，150ms 窗口产生 15 次 RPC；两次都保存了全部 36 条事件。
含输入间隔的总耗时分别为 2.83 秒和 2.81 秒。该测试验证减少写入次数，不能证明模型生成会加快。

新的 `llm.completed.response.stream_metrics` 保存：

| 字段 | 含义 |
| --- | --- |
| `http_first_chunk_ms` | HTTP 读取开始到网络线程读到第一行（可以是 SSE 元数据，不等于 TTFT） |
| `http_receive_ms` | HTTP 读取开始到网络线程读到最后一行；包含连接、服务端与网络等待 |
| `received_bytes` / `received_chunks` | 收到的字节／行数，不等于 token／delta 数 |
| `receive_queue_max_lag_ms` | 行到达网络线程后，到异步消费者取出该行的最大延迟；包括事件循环排队 |
| `receive_queue_end_lag_ms` | 最后取出的行／结束标记的消费延迟 |
| `event_emit_ms` | 本次请求等待事件发射器返回的累计时间（含重试尝试） |
| `delta_emit_ms` / `delta_events` | delta 发射累计时间／数量；包含序列化、入队与背压等待 |
| `completion_emit_ms` | 流结束事件等待此前事件持久化的时间，包括该结束事件自身 |

入站指标在网络线程记录收包时刻，不能由消费者取数据的时刻代替。
普通流式 `model duration` 在发射结束事件前截止，`completion_emit_ms` 在其后；
其他指标存在重叠，不能相加为总耗时。重试时 HTTP 指标属于最终响应尝试，emit 指标累计所有尝试。
自定义 provider 没有 HTTP 指标时显示缺失，不填零。历史调用没有这些指标，不进行推算。
Execution flow 的 Working 行展开可查看 HTTP 接收、最大消费延迟、delta 发射等待、最终持久化等待。

## 新任务复核：本地写入不再串行拖慢流消费

对比相同目标的 `sess_0c49d485a3a347f18b1e12de860e673f` 与
`sess_dd86d9bdf4564ee999674ac843db8e94`（只读事件和 event_detail artifact）：

| 指标 | 旧 dd86 | 新 0c49 |
| --- | ---: | ---: |
| 请求及原始 SSE 记录的模型 | qwen3.8-max | glm-5.2 |
| 实际运行跨度，扣除暂停 | 约310秒 | 约178秒 |
| 模型调用次数，含验收 | 9 | 14 |
| 模型调用累计耗时 | 约298秒 | 147.22秒 |
| 输入／输出 tokens | 83,762／10,407 | 155,039／11,538 |
| 流式输出 tokens | 9,027 | 9,897 |
| 流式调用累计耗时 | 约267秒 | 127.45秒 |
| 流式有效 TPS，扣除 TTFT 后加权 | 约35.3 | 92.5 |
| delta 数量 | 2,551 | 5,099 |
| 最新 attempt 的 worker RPC 记录 | 3,598 | 1,369 |

旧任务有一次暂停；不能把约227秒的暂停时间算成推理或 RPC 耗时。
旧模型耗时和 TTFT 由秒级事件时间推算，新值来自单调时钟；新旧工具选择、调用轮次和运行日期也不同。
以上并非同模型的受控 A/B 实验，无法给出异步 RPC 对总提速的精确贡献率。

新任务的12次流式调用：HTTP 接收累计124.85秒，delta 发射累计等待2.00秒，
所有流事件发射累计等待9.30秒（其中结束事件持久化等待2.96秒），最大消费积压473ms。
这些指标重叠，不能直接相加。它们说明本次没有长时间的本地 SSE 消费积压；
耗时主要沿 HTTP 流的到达时间展开，HTTP 时间本身仍无法区分服务端计算和网络等待。

两次任务的 `complete_node` 都被同一条件拒绝：默认只有一个节点，该节点同时也是最后一个节点，
框架要求使用 `finish` 提交报告，但之前的工具描述和节点约束仍引导模型先调用 `complete_node`。
新任务生成这次无效动作的模型调用用了28.51秒，旧任务约96秒；这些不是工具执行耗时。
修复后按当前节点暴露完成控制：中间节点使用 `complete_node`，有报告提交能力的最终节点使用 `finish`。
节点工具限制保留完成控制，恢复 checkpoint 时重新投影工具和约束；已有的报告与验收守卫继续执行。
无 `finish` 能力的独立 workflow 仍可用 `complete_node` 完成最后一个节点。
