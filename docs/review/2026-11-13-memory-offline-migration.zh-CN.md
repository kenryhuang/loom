# Memory V2：离线一次性迁移

2026-11-13。本记录替代 2026-11-08 迁移记录中的启动回放、prepared 自动切换和 legacy 回退说明。

服务启动不再调用历史记忆迁移，也不再自动 ALTER 旧版 Memory 表。启动只检查少量 schema、namespace 和迁移进度元数据；遇到未完成迁移或旧读取模式，明确报错并要求离线迁移。正常的新数据库直接创建当前表结构。

记忆检索、列表、epoch 和原始引用只读 V2 表，删除了扫描 semantic_jobs.body 的旧读取实现及全历史列表缓存。evaluation_id/record_id 仍可定位已经导入 V2 的原始观察；返回值标明 historical。人工修改后的知识使用 memory_id/version_id 读取。人工审核优先级、来源撤回和范围隔离仍有效。

## 升级已有数据库

在停止服务后执行（备份路径必须不存在）：

```bash
.venv/bin/python -m loom.memory.migration \
  --database runs/service/service.sqlite \
  --backup /path/to/new-backup.sqlite
```

该命令持有与服务相同的 supervisor.lock；服务仍在运行时会拒绝执行，不会先创建备份或修改数据。迁移保留旧评估与人工标注，转换结果写入 V2。原始数据不删除。

每个 evaluation 的转换和 memory_import_jobs 进度在同一事务提交。中断后显式重跑，先用 ID 筛掉已完成项，再读取剩余正文；恢复时省略 --backup，保留首次备份。最终来源核对、Skill 依赖、全部 namespace 切换和 memory_import_state 完成标志在同一事务提交。完成后重跑直接返回报告，不再扫描历史 JSON，也不重复备份。删除了 --prepare-only、--mode 和 legacy 回滚入口。

历史导入产生的整合任务保持 shadow_pending，不触发整库模型调用。新评估由 memory.ingestion.sync_job 在保存事务中单条发布；运行时不读旧人工标注/抑制表，也不拼装完整 session，只读取删除状态和必要工作目录字段。用户审核命令的现有审计接口保留，权威修改直接落到 V2。

这次改动针对 Memory 历史回放。Agent 初始化、Semantic 初始化中的其他历史加载，以及 Session 存储自身的升级机制，不在本次修改范围；不能据此宣称所有启动耗时都已解决。
