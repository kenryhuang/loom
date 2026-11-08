# Learning 页 Learned knowledge 搜索性能诊断

诊断日期：2026-11-08。代码版本：`ed67386`。

后续修复与复测见[修复报告](2026-11-08-learning-memory-search-fix.zh-CN.md)。本文保留修复前的诊断结果。

**结论：主要瓶颈在记忆读取 SQL。每次搜索都从大型 Deep Eval JSON 中展开记忆、提取元数据并排序，随后才在 Python 中处理用户筛选和分页。前端筛选请求不取消旧请求、详情刷新重建搜索组件，会进一步放大等待。**

此次只做诊断和只读对照实验，没有修改产品代码、生产数据库、Agent 状态或重启服务。

## 范围与测量限制

- 真实数据：`runs/service/service.sqlite`，以 `mode=ro` 连接；真实 HTTP 服务为 `127.0.0.1:8765`。
- 后台 Agent 保持运行，数据可能变化；部分独立进程测量有重叠，不能把单次时间当作稳定 P50/P95 或严格隔离的性能承诺。
- 独立进程直接调用 `MemoryStore.browse()`，不经过 HTTP、前端或服务写事务；另用 cProfile 定位内部耗时。
- SQL 对照实验仅替换独立只读查询，保留候选字段、作用域、排序和 LIMIT；比较完整返回行的 SHA-256。
- 前端行为使用真实 `AgentsView` / `AgentLearningView` 与 JSDOM、可控延迟响应复现，不是生产浏览器端到端耗时测试。

## 数据规模与直接读取结果

当前有 144 个 `semantic_jobs`，`body` 内容合计 247,841,941 字符，最大单条 10,282,086 字符。另按 `length(CAST(body AS BLOB))` 测得 UTF-8 内容合计 260,072,651 字节（约 260 MB），最大单条约 10.57 MB。39 个 Session 的 body 合计 14,930,511 字符。

当前工作区内、未删除 Session 的已发布记录包括 3,274 条 facts、394 条 lessons、380 条 workflows，共 4,048 条可检索类型；另有 1,112 条 contributions，不属于运行时记忆检索类型。此统计未应用后续 Python 的逐条来源/大小校验。当前人工标注和屏蔽表均为空。

| 条件 | 直接 browse 墙钟时间 | CPU 时间 | 候选窗口内匹配数 | 返回数 |
| --- | ---: | ---: | ---: | ---: |
| 不筛选 | 16.94 秒 | 16.68 秒 | 499 | 20 |
| Facts | 20.30 秒 | 19.94 秒 | 405 | 20 |
| Human reviewed | 22.76 秒 | 22.19 秒 | 0 | 0 |
| Awaiting human review | 20.93 秒 | 20.49 秒 | 499 | 20 |
| 不存在的关键词 | 19.96 秒 | 19.79 秒 | 0 | 0 |

所有结果均带 `truncated=true`。首屏 JSON 约 78.5 KB，空结果约 306 字节（含诊断信息）；即使返回 0 条也要等待约 20 秒，已足以排除响应下载和卡片渲染是这段等待的主要来源。

单独一次 cProfile：`browse()` 共 17.325 秒，`_records()` 17.319 秒，SQLite `execute()` 累计 17.231 秒，约占总时间 99.5%。Python JSON 解码、摘要规范化、摘要哈希分别只有约 30、32、6 毫秒。作用域路径处理约 34 毫秒，也不是主因。

真实服务 HTTP 顺序采样（每个条件一次，无 If-None-Match）：

| 条件 | 完整响应 | 首字节等待 | 响应字节数 |
| --- | ---: | ---: | ---: |
| 不筛选 | 23.916 秒 | 23.915 秒 | 76,173 |
| Facts | 19.812 秒 | 19.811 秒 | 75,828 |
| Human reviewed | 20.996 秒 | 20.995 秒 | 290 |
| Awaiting human review | 25.073 秒 | 25.072 秒 | 76,173 |
| 不存在的关键词 | 24.331 秒 | 24.331 秒 | 290 |

响应体比独立进程测试略小是服务 JSON 序列化分隔符不同。空结果也需约 21 秒，主要时间在首字节返回前，而不是结果下载。

## 1. 大 JSON 查询和排序是主要瓶颈

代码：[`MemoryStore._records()`](../../src/loom/memory/store.py)。

查询从 `semantic_jobs.body` 的 `checkpoint.memory.records` 使用 `json_each` 展开记录，关联 Session 进行作用域/删除状态检查。在展开的结果上提取任务 run_id、created_at、state、source_digest、analysis_id 等字段，并按 JSON 中的 created_at 排序。

实际查询计划：

```text
SCAN j
SEARCH s USING INDEX sqlite_autoindex_sessions_1 (id=?)
SCAN r VIRTUAL TABLE INDEX 3:
USE TEMP B-TREE FOR ORDER BY
```

作用域在候选上限前应用，但没有可直接定位工作区和时间顺序的记忆索引。`LIMIT 501` 并不意味着只访问 501 条原始记录：查询仍需遍历评估任务、展开并筛选候选，维护排序结果。大 JSON 上的多个字段提取在展开查询中造成很高成本。

只读对照实验使用 `WITH candidates AS MATERIALIZED`：先在每个评估任务上提取小型元数据和 memory records JSON，再对这个中间结果执行 `json_each` 和排序。

| SQL | 耗时 | CPU | 返回候选 |
| --- | ---: | ---: | ---: |
| 原查询 | 24.16 秒 | 23.60 秒 | 501 |
| 物化小字段后展开，第 1 次 | 1.51 秒 | 1.48 秒 | 501 |
| 物化小字段后展开，第 2 次 | 1.40 秒 | 1.39 秒 | 501 |

三次结果的完整行哈希一致：`4e507cc7fb9578f554a28e828e12b791f440677bb622811e897c410ef6d2de3e`。这是当前数据上的结果等价验证，尚不是覆盖历史格式、标注、删除和并发更新的产品回归测试。实验表明改变查询形状即可显著降低成本，但 1.4 秒仍需要继续优化。

## 2. 用户筛选和分页没有减少前置扫描

代码：[`MemoryStore.browse()`](../../src/loom/memory/store.py)。

执行顺序是：

```text
_records() 读取最新 500 条候选并校验、应用标注
→ Python 按 kind / review / query 筛选
→ 统计 total
→ Python 切片 offset:offset+limit
```

因此切换类型、审核状态、关键词和翻页，都会重新支付同一段昂贵 SQL 成本。当前没有任何人工标注，选择“Human reviewed”仍先扫描历史评估，最后才得到 0 条。

还有完整性问题：Library 复用了运行时检索的 500 条候选上限。`total=499` 表示最新候选窗口中校验后的数量，并不是工作区全部记忆数量；筛选会漏掉窗口外的旧记忆。前端虽提示只显示最近记录，但仍无法通过翻页查到全部历史。管理页面的检索/分页应与运行时受限召回分开设计，不能简单移除上限后继续使用当前重查询。

## 3. 前端请求管理放大等待

代码：[`AgentLearningView.loadLibrary()`](../../src/loom/web/assets/views/agent-learning.mjs)、[`AgentsView.refreshOnce()` / `render()`](../../src/loom/web/assets/views/agents.mjs)。

- 每次 SELECT change 都立即发起新请求。`loadGeneration` 只忽略旧响应，不取消旧请求；各次搜索共用页面级 AbortSignal。
- 搜索等待不计入 `dirty`。每 1.5 秒的详情轮询如果发现数据变化，且没有输入焦点/编辑保护，会销毁并重建 Learning 组件，取消旧客户端请求并再次自动搜索。只有详情变化才重建，不是每次轮询必然重建。
- 取消浏览器 fetch 不会自动中断服务端已运行的 SQLite 查询；当前请求链路未将断连传播为 `sqlite3.interrupt()` 或进度回调。

可控响应实验结果：

1. 初次加载后连续修改两次类型，产生 3 个未完成搜索，已取消数为 0，`dirty=false`。
2. 模拟一次正常轮询详情变化，累计产生第 4 个搜索；前三个客户端信号被取消，界面重新显示 Loading。
3. 最新响应完成后才显示结果。

这证明长 SQL 与频繁状态变化组合时，存在重复搜索、反复 Loading 的放大机制；没有据此声称已测得用户当前浏览器的实际重试次数。

另外，现有 ETag 在 `autonomy.dispatch()` 完成、结果序列化后才计算。因此即使命中 304，也只能省响应传输，不能省掉这次 SQL。

## 建议的处理顺序

1. **先修读取路径和前端重发。** 按评估任务一次性提取必要字段；搜索使用独立请求控制；Learning 详情刷新保留记忆搜索组件和结果，只在筛选或记忆版本变化时更新。服务端按作用域、记忆版本、查询条件合并重复读取/缓存，不能只依赖客户端 abort 或响应 ETag。
2. **为 Library 提供完整分页语义。** 不再拿运行时最新 500 条候选当作整个记忆库；类型、作用域、审核状态和关键词应在适当层次先筛选，再分页。确认/纠正、拒绝/恢复、共享工作区标注等现有语义必须保留。
3. **记忆查询值得增加独立读模型。** 保留 Deep Eval 原 JSON 作为审计来源，另维护以 evaluation_id、record_id、workspace/session、created_at、kind、publication_state、source/content digest 等为字段的记忆投影，列表读必要摘要，展开时读取证据和完整内容。对实际过滤路径建索引；关键词索引需保持中英文检索语义。投影必须支持发布、变 stale、重评、人工标注和删除后的同步，以及历史回填/重建。

当前证据支持“查询逻辑有明显问题，记忆与大型评估 JSON 耦合也限制了后续性能”，不支持更换 SQLite。前一轮 Agent 标签加载测试未覆盖真实 `/memory/library` 重查询，不能用那一轮的页面提速判断此路径已经解决。

后续复测应在同一数据副本上比较不同筛选、空结果、下一页、重复条件和并发请求；记录 SQL/HTTP P50/P95、首字节、查询次数和有效版本缓存命中，并在 Agent 状态持续变化时验证结果能稳定显示。功能验证需覆盖超过 500 条历史记忆、作用域隔离和人工标注。
