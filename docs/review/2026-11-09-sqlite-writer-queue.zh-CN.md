# Agent 数据库锁超时修复

## 现场事实

研发搭档在 2026-11-08 15:25:38 UTC 的规划检查中记录 `database is locked`。
调度器将该异常与当前 generation 绑定，后续定时检查一直跳过；数据库恢复可写后，Agent 仍显示旧错误。
同次规划的预算预约保留为 unknown，不能据此判断模型是否已成功返回，更不能直接删除预约。
原异常没有保留 Python 堆栈，因此无法追溯当时具体哪个事务持锁。

现有代码有两个独立的失效路径：预算结算异常会丢弃已经返回的模型响应；记忆线程在处理异常时再次写库，写库失败会让整个线程退出。

## 写入协议

SessionStore、BudgetLedger、Memory Repository、SkillStore 统一使用 `loom.core.sqlite.write_transaction`：

1. 按数据库的规范路径取得进程内共享队列锁，再打开写连接。
2. 外层事务负责 BEGIN IMMEDIATE、提交、回滚和关闭。
3. 同线程嵌套写入复用连接，以 savepoint 隔离内层回滚；外层回滚覆盖全部内层写入。
4. WAL 读取继续使用独立的只读事务，不进入写队列。跨进程竞争仍由 SQLite 的 busy timeout 处理。

预算预约必须先于物理模型请求持久化。如果返回后的结算遇到 SQLite busy，保留原预约作为保守计费，并返回已有响应；记录 call ID 与实际 usage，不能因结算争锁而重发模型请求。其他数据库错误仍向上传播。

## 恢复规则

- 调度检查在尚未取得 planner lease 时遇到 busy，进入 waiting，下次定时检查重试。
- 已取得 lease，或本轮已产生规划预算预约的失败仍需要显式处理；即使 planner 已清理 lease，也不能自动重发可能已付费的请求。
- 记忆线程每轮清空选中 job；busy 不把 job 标为业务失败；错误记录本身失败也不能杀死线程。
- 空闲的记忆恢复检查、Agent 后台 ID 枚举使用读取，避免无意义地争抢写锁。

## 回归覆盖

覆盖四种 Repository 的竞争写入、独立 WAL 读取、嵌套事务提交与回滚、普通及流式响应在结算锁超时后的保留、原始请求异常保留、调度等待与付费请求保护、后台线程错误处理及旧 job 隔离。

线上已平滑重载服务，并以 `recover-sqlite-lock-20261109` 正常调度命令恢复研发搭档。
新规划调用成功结算 10,288 tokens；2026-11-09 13:29:19 UTC 成功派发
`work_ab8b5419d84e4a32b9ebd4773dde955a`，原始 Session 为
`sess_a569f5a2a3074f4a87e2253383396f5b`，确认进入 running，Agent error 清空。
旧 unknown 预约没有删除，预算配置没有调整。
