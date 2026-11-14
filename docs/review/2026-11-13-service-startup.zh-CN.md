# 服务启动：只恢复未结束工作

本次继续处理 Memory 历史迁移移出启动后，仍存在的两次评估全量扫描和 Agent 完整 Session 加载。原始测量见 [JSON](2026-11-13-service-startup.json)。

## 改动

- `SessionSemantic` 使用 `semantic_jobs_active` 部分索引，只读取 queued/running 评估，保留 checkpoint、usage 等原有恢复数据，并标记为 interrupted。已结束评估不读取正文、不解码、不重写。
- `EvaluationArtifacts` 启动只重试已经提交的删除意图，删除全库产物归属回填。发布结果和恢复评估原本就在同一事务登记归属；删除历史评估时仍只在目标 Session 内核对旧归属和共享引用。
- `AgentSupervisor` 用 Session 的惰性字段访问恢复状态，不读取历史消息和上下文。planning 计划由 `agent_runs_planning` 部分索引筛选后读取，不再解析全部历史计划。
- 索引由 SQLite 随 INSERT / UPDATE / DELETE 自动维护，包含旧服务进程直接更新评估的情况。新库在空表上创建索引；已有历史但缺少索引时，启动明确提示准备命令，不会在启动时扫描历史建索引。

已有数据库的一次性准备命令：

```bash
.venv/bin/python -m loom.service.startup_indexes --database runs/service/service.sqlite
```

准备在一个事务中创建两个索引，不改任务正文、状态或产物；SQLite 会短暂串行化并发写入。重复执行不重建。当前 `runs/service/service.sqlite` 已完成准备，耗时 12.651 秒，不需要用户再次执行。

## 测量

使用当前库的一致性副本，大小 5,365,010,432 字节：72 个 Session、260 个评估、57 条 Agent Session 关联、415 个 Agent run。未对原库运行启动恢复基准。

| 阶段 | 改动前 | 改动后 |
| --- | ---: | ---: |
| 核心及全部后台组件初始化 | 36.6488 s | 0.3321 s |
| Semantic 初始化（含产物管理器） | 34.8985 s | 0.0104 s |
| 其中产物管理器 | 16.1720 s | 0.0051 s |
| AgentSupervisor 初始化 | 1.6570 s | 0.2535 s |

以上为相同 cProfile 测量方式，不包括解释器启动和模块导入。OS 页缓存没有清空，不是严格冷启动对比。

另用三个全新 Python 进程测量真实 `/web/` HTTP 200：1.0836 s、0.5629 s、0.6412 s，包含解释器启动、模块导入、全部 Agent 组件初始化及 HTTP 服务。为避免副本调度真实工作，未开启自动调度循环，也没有加载模型配置；因此不能当作当前在线实例完整重启的测量。

当前在线实例保留运行：检测到一个活动评估，没有为基准测试中断它。索引已就绪，代码优化在下一次重启生效。

## 验证

测试覆盖恢复时保留 checkpoint/usage、拒绝启动时自动索引历史、SQL 查询确实使用部分索引、原始 SQL 发布/替换/删除后的索引一致性、跳过历史评估正文、跳过 Session 上下文和消息、恢复计划状态，以及既有评估产物删除、评估恢复、Agent 重启与 Memory 运行时回归。

相关回归 93 项通过，Ruff 与 git diff --check 通过。
