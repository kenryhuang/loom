# Agent 页面加载性能诊断

诊断时间：2026-11-08（Asia/Shanghai）。代码版本：`8b35ab1`。

后续实施与测试见[优化与复测报告](2026-11-08-agent-loading-optimization.zh-CN.md)。以下保留优化前的诊断记录。

结论：**主要瓶颈是后台调度和页面读取的全量加载逻辑，被大 JSON 快照的存储粒度放大；前端轮询、过大的接口响应和整页重绘进一步增加成本。当前没有证据表明需要更换 SQLite。**

## 测量范围

- 对本机正在运行的 `127.0.0.1:8765` 服务发送只读 GET 请求，每个主要 Agent 接口顺序采样 4 次。
- 使用 Chromium headless 打开真实页面、选择 Agent、切换六个标签，采集 Resource Timing、同步 render 耗时和 long task。
- 在独立 Python 进程中，用 `mode=ro`、`PRAGMA query_only=ON` 连接相同数据库，调用现有读取代码并做 cProfile；绕过会迁移数据库或启动后台任务的构造函数。预算读取也替换为只读连接。
- 对运行服务执行 3 秒原生栈采样，检查 CPU 与 JSON 解析、GIL 等待的迹象。
- 未修改业务代码、数据库结构、Agent 配置或任务状态，未停止服务。独立进程测量使用同一个实时数据库，不是停掉后台后的严格对照实验。

样本包含 1 个 Agent、39 个 Session（36 个未删除）、24 个 Agent Session 关联、35 个 Goal、79 个 Work Package、107 个 Learning Cycle、5,403 条预算调用记录。数据库文件约 2.41 GB；全部 Session JSON 为 18.02 MB，未删除 Session JSON 为 17.31 MB，Agent 关联 Session JSON 为 12.32 MB。数据库文件总大小本身不能证明查询慢。

## 实测结果

接口时间单位为 ms，响应大小使用十进制 KB/MB。4 次样本只适合定位问题，不代表稳定的 P95。

| 接口 | 4 次完整 HTTP 耗时 | 响应大小 | 同读取逻辑在独立进程中的耗时（3 次范围） |
| --- | --- | --- | --- |
| Agent 列表 | 20 / 9 / 15 / 148 | 18.4 KB | 未测 |
| Overview | 1013 / 579 / 338 / 947 | 45.5 KB | 59–63 |
| Goals | 566 / 368 / 726 / 596 | 1.04 MB | 68–69 |
| Work | 715 / 754 / 787 / 371 | 1.14 MB | 67–75 |
| Profile | 272 / 467 / 237 / 62 | 25.5 KB | 30–36 |
| Knowledge | 185 / 86 / 228 / 58 | 28.8 KB | 28–30 |
| Learning | 236 / 540 / 443 / 467 | 824 KB | 38–40 |

独立进程结果不包含 HTTP、最终响应序列化和服务进程内部线程竞争，不能直接当作优化后的响应时间。但差距说明仅看 SQL 或前端 render 都不足以解释线上延迟。

浏览器一次完整采样：

- Agent 列表可用约 1.22 秒；选择 Agent 后 Overview 可用约 455 ms。
- 初始化 `/v1/sessions` 请求约 572 ms，TTFB 约 571 ms，之后才开始 `/v1/agents`。
- Work 请求约 704 ms，TTFB 约 702 ms；同步 render 约 55.5 ms，生成 5,844 个后代节点。
- Goals 同步 render 约 10 ms，Learning 约 9 ms，Profile 约 1 ms，Knowledge 约 2.7 ms。
- 观察到 183 ms 和 58 ms 的 long task；未对它们做完整 JS 栈归因，不能把 183 ms 全归于 render。
- 页面无 JavaScript 错误；当前 Goals 请求未触发逐 Session snapshot 的补充请求，因此不能把这次加载慢归因于前端 N+1。

本机回环网络下，大部分请求时间耗在首字节之前，主要是服务端等待/处理，不是数据下载。大响应在远程访问时仍会额外增加传输成本。

## 1. 首要瓶颈：后台调度反复解析全部 Session

代码位置：

- [`Service._schedule()` / `_tick()`](../../src/loom/service/controller.py)：循环每轮完成后等待 20 ms；每轮调用 `list_sessions()`，每秒清理分支还会额外调用一次。
- [`SessionStore.list_sessions()`](../../src/loom/service/store.py)：读取全部未删除 Session 的 `body`，并对每个完整快照执行 `json.loads()`。
- [`ready_sessions()`](../../src/loom/service/scheduler.py)：拿到完整对象之后才筛选 queued Session。

实测单次 `list_sessions()`：

| 测量 | 4 次结果（ms） |
| --- | --- |
| 墙钟时间 | 325 / 458 / 436 / 436 |
| 进程 CPU 时间 | 323 / 452 / 432 / 433 |

cProfile 另一次采样总计约 475 ms，其中 36 次 JSON 解码占约 367 ms，约 77%。测量时 Session 状态为 33 个 idle、2 个 paused、1 个 running，**没有 queued Session，全量解析仍然发生**。

服务进程采样时 CPU 为 96.9%，原生栈中可见大量 JSON 解码及 GIL 等待。结合后台高频全量加载代码和独立进程耗时，强烈支持后台 JSON 解析挤占请求处理资源的判断。原生采样未提供 Python 函数级完整归因，不能声称全部 CPU 都由这一条路径产生。

这里的 20 ms 是每轮结束后的等待时间，不能据此声称实际每秒扫描 50 次；扫描本身已达数百毫秒。

只读验证实验：将调度读取改为 SQL 投影调度相关字段，包括状态、资源声明、工作目录、autonomy、时间和 workspace blocker，同样返回 36 行：

- JSON 输出约 24.8 KB。
- 耗时 39–46 ms，远低于完整读取的 325–458 ms。
- 实验只验证减少读取和解码的数据量有明显收益，没有修改生产调度器，也不构成调度语义等价性测试。

这说明无需换数据库即可先显著减少开销。进一步把调度状态放入独立摘要列/表，才能避免 SQL `json_extract()` 仍扫描大 JSON。

## 2. 首屏加载被无关的 Session 全量列表阻塞

[`SessionController.connect()`](../../src/loom/web/assets/controller.mjs) 等待 catalog 和全部 Session 列表；[`login()`](../../src/loom/web/assets/app.mjs) 等 connect 完成后才按 `#/agents` 路由打开 Agent 页面。

`GET /v1/sessions` 调用 `list_sessions_by_activity()`，先读取、排序、解析所有完整 Session，再在 Python 中分页，最后只返回 title/task/event_cursor 等摘要。独立进程测得该函数耗时约 597–610 ms，CPU 时间约 579–592 ms。

因此直接打开 Agent 页面也要支付完整 Session 列表的成本。应让路由决定初始化依赖，Agent 页面先加载 Agent 所需数据，Session 列表延迟加载；同时让 Session 列表在 SQL 中分页并读取摘要。

## 3. 存储问题在于冷热字段混在大 JSON 中

当前 SQLite 已使用 WAL，`SessionStore.read()` 使用独立连接和 deferred read transaction，不获取写路径的 Python RLock。没有证据把主因归为 SQLite 写锁串行阻塞所有页面读取。

但 [`AutonomyService.snapshot()`](../../src/loom/service/autonomy.py) 对所有标签都查询 pending decisions，必须读取关联 Session 的 JSON，判断 `deleted_at` 和 `input_request.state`。

针对相同 24 条关联记录：

- 仅查询 Session ID：0.02–0.08 ms。
- 增加 pending/deleted JSON 条件：30–45 ms，即使结果为 0 行。
- 查询计划为扫描小型 `agent_sessions`，再按 Session 主键查找；不是对全部 Session 做无索引关联。

[`add_work_metrics()`](../../src/loom/autonomy/views.py) 为 Goals 读取全部关联 Session 的整个 `run`，但最终仅用 active_seconds 和 counters；一次 profile 约 32 ms。Work 的 execution report 也会逐个 Session 查询执行摘要，一次 profile 约 28 ms。

建议保留完整快照用于恢复，同时在同一事务维护轻量运行摘要：task_state、deleted_at、input_request_state、run_id/run_state、active_seconds、token counters、资源声明、更新时间等。页面和调度优先读取摘要，恢复/详情才读完整快照。索引应建立在实际筛选字段上；仅给 `agent_sessions.agent_id` 加索引不能消除大 JSON 解析成本。

## 4. 页面接口按标签分流了，但标签内仍全量返回历史

[`VIEW_FIELDS`](../../src/loom/autonomy/views.py) 已经避免加载非当前标签的部分集合，Overview 也使用 id/state 摘要。这些优化确实存在。

但 [`AgentStore.rows()` / `snapshot()`](../../src/loom/autonomy/store.py) 对当前标签所需集合仍完整读取，未分页：

- Goals 的 work_packages JSON 约 691 KB，占主要体积。
- 其中 latest_context 约 173 KB、acceptance_criteria 约 128 KB、objective 约 62 KB、acceptance_result 约 60 KB。
- Learning 同样携带约 686 KB 的完整 work_packages。
- 列表返回完整 Agent 对象，单个约 18 KB，侧栏实际只显示名称和状态。

字段大小按单独 JSON 编码估算，包含编码空白差异，不应与 HTTP 精确字节数直接相加。

应为每个标签定义列表 DTO，区分运行中条目和历史条目，历史分页；上下文、验收细节和诊断在展开时读取。完整审计记录可保留在原存储，不需要为了页面快而删除历史。

## 5. 前端刷新会放大上述成本

[`AgentsView`](../../src/loom/web/assets/views/agents.mjs) 的行为：

- 每 1.5 秒刷新列表和当前详情。已通过 `refreshing` 合并请求，不是无限并发叠加。
- 标签切换遇到正在运行的旧请求会等待其结束，没有按标签取消旧请求，产生额外排队。
- 每轮都重新创建侧栏；详情通过完整 `JSON.stringify()` 比较，一旦有变化就重建当前标签。
- Work 一次生成 5,844 个 DOM 后代节点，已有约 56 ms 同步 render，属于可感知长任务。
- Knowledge 渲染后还请求 catalog 和 knowledge-bases，本次浏览器采样额外等待知识库请求约 288 ms。

按当前 Work 响应体估算，若每次刷新都完成，单页面约返回 46 MB/分钟的 Agent JSON 数据。变化频繁时还会反复创建历史内容的 DOM。

建议用专门的视图版本/ETag 或事件通知检测变化，更新局部区域；列表和静态配置降低刷新频率，页面不可见时暂停轮询；切换标签取消旧请求；历史条目按需渲染。视图版本必须覆盖预算、Session 和相关集合变化，不能仅依赖 Agent 自身 revision。

## 优化顺序与验证标准

1. **P0：调度读取轻量化。** 避免每轮解码所有完整 Session；队列/阻塞状态使用摘要和索引，考虑事件唤醒配合低频兜底扫描。保留资源冲突、暂停、恢复和公平调度语义。
2. **P0：解除 Agent 首屏对全部 Session 列表的依赖。** Session 列表改为 SQL 分页和摘要投影。
3. **P1：瘦身 Agent API。** pending decisions / metrics 使用运行摘要；标签返回必要字段，历史分页，详情懒加载。
4. **P1：调整前端刷新与渲染。** 标签切换取消旧请求、条件刷新、局部更新、Work 历史按需渲染。

完成后在相同数据规模下重新测量：服务闲置/运行时 CPU、调度每轮 CPU、接口 P50/P95、响应字节数、首屏依赖链、标签切换时间、浏览器长任务。同时验证任务调度、恢复和摘要一致性。

此次结果足以确定优先优化方向，但未停用后台任务做 A/B，也未完成各后台线程 CPU 占比归因；不能承诺某项修改后的端到端延迟。当前不建议优先更换数据库、清理历史数据或仅增加前端 loading 动画。
