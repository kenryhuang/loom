# Memory V2 实现与迁移记录

日期：2026-11-08。关联：[设计](../design/memory-v2.zh-CN.md)、[原实施计划](../plan/2026-11-08-memory-v2.md)、[性能原始数据](2026-11-08-memory-v2-benchmark.json)。

## 已实现

- 同一个 service SQLite 内的 namespace、原始来源/观察、知识身份、不可变版本、双时间有效性、支持 bundle、冲突、人工审核、事件、任务/outbox、索引状态和查询投影。
- 确定性精确整合、同源不重复计票、同一源记录修订的稳定知识身份；CREATE / ATTACH_SUPPORT / NOOP / REVISE / SUPERSEDE / CONFLICT / RESOLVE / SPLIT / MERGE / RETIRE / RESTORE，以及有并发保护的合并撤销。已确认的合并别名再出现时归入目标知识。
- 单值原子事实的重叠冲突识别；集合追加不等于替换。提取协议增加可选 claim，旧 checkpoint 的缺省字段仍合法；旧记录不会被模型批量改写成未经验证的原子事实。
- 版本内容不覆盖；现实有效时间与系统接纳时间分开。未来版本不会提前替代当前版本；历史查询可指定 valid_at / known_at。
- 来源失效立即撤销有效支持，AND/OR bundle 与无环依赖门禁；既有 Skill 绑定具体知识版本，下一请求边界检查，不重写已执行动作。
- Agent context 与执行 prompt 共用同一 MemoryPacket。canonical memory_read 使用 memory_id/version_id；旧 evaluation_id/record_id 继续返回原始提取文本，并标明 historical。
- Learning 列表直接查询轻量索引，支持状态、类别、审核过滤和 epoch 绑定游标；证据/历史按需加载，支持纠正、撤回、恢复、冲突决议和等价提案接受。
- 新增语义整合持久任务，候选检索与模型请求在权威写事务外执行，使用 Agent BudgetLedger 的 memory_consolidation 类别。结果先持久化再提交；未知响应或服务中断不自动重复付费。
- 语义判断默认 shadow。校准工具检查人工标签、证据根划分、精确率/置信区间、冲突召回与关键错误，并绑定模型/协议 fingerprint。校准通过才能启用自动语义合并。
- 可重建索引发布契约、批量 outbox watermark、固定模型空间、失败重试和返回结果的权威复核。LightRAG 提供显式批量适配器；YakDB/现有文档读取器提供带页定位的证据入口。旧图摘要不会直接进入上下文。

## 当前数据库迁移

已对 `runs/service/service.sqlite` 做 SQLite 在线备份，位置：

```text
runs/service/service.before-memory-v2.sqlite
```

随后执行 prepared 迁移，并在确认没有运行中的任务或评估后重启本地服务完成切换：6 个 namespace 均为 runtime_v2 / complete，4,168 份有效原始观察、4,168 个初始知识身份、5 条原有身份/来源校验异常。原 evaluation、原引用和已发出的 context 保留；逐条核对原始观察与 source revision 的 SHA-256，4,168 份均一致。这里的数量不是语义去重率：历史语义整理没有未经校准就自动执行。

切换后实际 HTTP Facts 列表返回 3,329 条，首个探测请求约 37 ms，详情接口正常。4,168 个历史整合任务保持 shadow_pending，没有自动发起整库模型调用。

启动流程先运行增量核对，再将 prepared namespace 切至 runtime_v2；新写入在同一事务同步权威投影。每个源的游标和导入数据原子提交，重启可以继续；先迁移拒绝/退役墓碑，再开放读取。拒绝覆盖层在切回 legacy 时仍生效。

手工执行入口（先选一个未存在的备份路径）：

```bash
.venv/bin/python -m loom.memory.migration \
  --database runs/service/service.sqlite \
  --backup /path/to/new-backup.sqlite \
  --prepare-only
```

不带 `--prepare-only` 时会完成尚未切换的 namespace。已完成 namespace 的显式 shadow/legacy 配置不会被重启覆盖。程序化 `migration.set_mode(database, namespace, mode)` 用于逐范围切换或回滚读取路由，保留新版历史及撤回状态。

## 测试和性能

新增序列测试覆盖：重复迁移、同源证据、原记录保留、拒绝后重提取、独立支持撤回、必要支持丢失、并发写入、未来/晚到信息、条件冲突、合并撤销、已合并改写再次出现、源修订保持身份、分拆、依赖环、旧索引命中、rollback 拒绝覆盖、Packet 一致性和语义校准门槛。

针对原 4,043 条有效记忆快照，以下不使用旧全量记录缓存，也不扫描 semantic_jobs.body：

| 查询 | 中位数 | P95 |
| --- | ---: | ---: |
| 当前知识列表 | 15.11 ms | 34.33 ms |
| Facts 过滤 | 13.00 ms | 18.44 ms |
| 关键词过滤 | 9.76 ms | 11.20 ms |
| 本地运行时检索与组装 | 75.15 ms | 83.71 ms |

100k 合成记录规模下，覆盖索引和“分页后读取正文”优化后：

| 查询 | 热查询中位数 | 热查询 P95 | 首次调用 |
| --- | ---: | ---: | ---: |
| 当前知识列表 | 242.85 ms | 248.08 ms | 568.98 ms |
| Facts 过滤 | 236.81 ms | 340.70 ms | 236.22 ms |
| 本地运行时检索 | 111.70 ms | 129.54 ms | 935.70 ms |

首次调用没有清空系统页缓存，不能称为严格冷启动测试；冷路径仍有优化空间。合成数据用于检查查询复杂度，不证明语义召回质量。10k 的初始实现测试列表 P95 为 102.05 ms；100k 初始计数约 1 s，已通过覆盖索引及避免正文读取降至上表水平。

相关 Memory/Context/Skill 回归 85 项通过；Memory/提取兼容性回归 115 项通过；前端 303 项通过。更广的 Autonomy/Service/Skill/Memory 初跑为 713 通过、7 失败：新增行格式与冻结 JSON 断言问题修复后定向回归通过，一个并发测试超时重跑通过；其余 3 个学习状态断言已在原始 HEAD 上复现（预期 incomplete，实际 completed），未将它们掩盖为本次通过。最后一轮前端全量运行有一个既有 app 异步 DOM 集成用例瞬态失败；独立重跑通过。新增 Learning V2 用例通过。

## 默认关闭或尚未完成的验证

1. 没有 1,200 对真实人工标注结果，不能宣称语义合并达到 99.5% 精确率；自动语义合并保持关闭。后台可以产生 shadow 提案，人工接受仍经过范围、证据和 revision 检查。历史迁移任务为 shadow_pending，不会因重启自动消耗整个历史库的模型预算。
2. LightRAG/YakDB 没有成为第二套真值库，也没有默认开启图检索。适配器契约使用本地替身验证撤回与陈旧命中门禁；本轮未执行真实远程 embedding/LLM 图索引质量实验。现有 LightRAG 库的共享长锁及 generation 回收仍属可选图引擎上线前的专项工作。
3. 原子条件当前采用明确的环境、平台、分支、版本、配置等精确匹配。无法完整结构化的条件保持原文/需验证状态，不猜测语义版本区间或跨工作区归一化。
4. 这是保留历史的逻辑撤回/版本演进实现；隐私删除的跨外部引擎物理擦除、复杂实体别名治理和自动后台环境探测，不在默认启动路径执行。
