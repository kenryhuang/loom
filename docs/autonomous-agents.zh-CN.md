# 自主 Agent：运行与人工介入

P0～P3 提供显式目标和周期职责的执行、学习闭环。一个 Agent 保存长期 Role，AgentRun 负责拆分和派发，SessionRun 使用现有执行内核完成工作。任务验收、Deep eval 和人工偏好是不同事实，分别保存和展示。

## 启动

```bash
uv run loom serve --data-dir .loom/service --config /absolute/path/to/task-config.json --enable-agents
```

沿用已有模型配置和服务凭据。打开服务输出的 `/web/` 地址，连接后进入 **Agents**。省略 `--enable-agents` 时可查看和编辑 Agent 历史，但不会启动自主编排、派发、学习或已排队的自主执行。普通 Session 和手动 Analysis 仍然可用。

主机和服务进程需要持续运行。关闭浏览器不会停止任务；机器休眠期间不会执行。服务持有目录级进程锁，同一数据目录只能有一个执行 supervisor。

## 首次配置

1. 创建 Role：填写角色原文、长期目标、工作目录、来源位置、默认方法、模型、检查间隔和预算。新 Agent 默认未启用。
2. 在「目标」添加本次交付目标与可检查的验收条件。长期目标提供方向，显式 Goal 提供本次完成合同。
3. 启用 Agent。编排最多拆分三个顺序工作包；末个工作包保留原 Goal 的全部验收条件。
4. 在「执行」查看 Session、验收结果和 Session 选择依据；在「学习」查看固定快照评估、已接受记忆及阻塞原因。

周期职责必须显式配置间隔及验收条件。每个职责同时保留一个未解决的周期目标；停机错过的窗口合并到最新窗口，不补跑全部历史窗口。单纯设置「检查间隔」不会凭空创建目标。没有就绪目标时记录 `no_action`，不调用规划模型。

API 中的 Role 示例（替换为真实目录）：

```json
{
  "command_id": "create-research-agent-1",
  "role": {
    "name": "研发搭档",
    "role_text": "维护设计与实现的一致性。中文输出，结论附依据。",
    "workspace": "/absolute/path/to/project",
    "long_term_objectives": [{
      "id": "design-quality", "desired_outcome": "检查设计与实现的差异", "mode": "ongoing",
      "schedule": {"every_seconds": 86400, "acceptance": ["报告有来源引用的差异；无法核实的内容标明未知"]}
    }],
    "source_routes": [{"id": "implementation", "topic": "实现事实", "paths": ["src", "tests"]}],
    "methods": ["先检查实现，再建议修改设计"],
    "timezone": "Asia/Shanghai", "interval_seconds": 300,
    "budget": {"daily_tokens": 1000000, "goal_tokens": 100000000, "work_tokens": 100000,
               "learning_tokens": 250000, "max_calls": 2000, "output_tokens": 8192}
  }
}
```

`POST /v1/agents` 创建；`POST /v1/agents/{id}/goals` 接收 `command_id` 和 `goal: {objective, acceptance}`。所有请求沿用 `Authorization: Bearer <service credential>`。Role 的结构化字段由用户提供并通过 Host 校验；系统保存原文、显式字段与默认派生字段，不假称已从任意自然语言可靠抽取完整权限。

## Session 复用与合同

只考虑同 Agent、兼容目录/模型/知识库/工具、空闲且没有人工输入或恢复待办的 Session，再按目标词汇相关性选择。选择依据保存在 routes。新工作包创建独立运行和验收关联，历史上下文可供参考，不能继承上次的验收通过结论。

每个 Agent 默认只推进一个活动工作包。自主和人工执行共用现有 workspace 读写仲裁；人工排队优先，自主等待超过五分钟后提升优先级。副作用未知仍使用现有 recovery 流程，不能通过 Agent 重派发跳过对账。

「接管 Session」阻止后续自主路由和 Agent 控制命令干预该 Session；当前执行仍保留原工作包与预算身份，执行控制在 Sessions 中操作。「归还」恢复后续路由资格。已结束的 Agent 工作之后发送独立人工消息，会开启普通人工运行，不重开旧工作包。

暂停先持久化意图，再通过控制 outbox 通知 Session，界面显示是否仍在等待停止。暂停或取消 Goal 不改变已发生的工具事实。Role 修订保留旧版本；执行环境仍固定于原合同，新的明确指导在下一合法模型请求边界更新，来源权限撤销在新工具操作准入时检查。待派发工作遇到 Role 版本变化会阻塞，需修订 Goal 或创建后继目标。

## 自动学习与人工标注

每个绑定执行的 `completed`、`failed`、`stopped` 终态与学习待办在同一事务中提交。暂停、等待输入和恢复不触发终态评估。学习固定终态事件游标，先完成 factual analysis，再针对原 run 执行 `goal_graph_v1 + extract_memory`；即使 Session 已开始下一轮，也不把后来的事件加入旧快照。

评估保留已有 Deep eval 检查点。状态与质量分开：`completed + partial` 表示处理完成但仍有缺口；无可分析证据为 `not_applicable`。每日学习预算耗尽会延后至所配置时区的下一日，续跑同一 evaluation，不重跑源任务。Goal 累计预算、配置冲突、评估自身资源上限等需要人工处理后重试。API 重试可带 `limits: {max_calls, max_seconds, max_tokens}`，仅允许现有评估协议支持的资源额度变更。

记忆仍限制在同 workspace；没有 workspace 时只允许同 Session。人工可认可、拒绝、更正、限定范围、打标签、置顶、停用和恢复。标注为覆盖层，保留原始提取和 trace；更正标为 `human_asserted`，认可不会将推断改成观察。相同来源再次提取不能绕过拒绝。摘要注入、memory_search/read 和页面共用有效视图；源删除后失效。

「工作方式」支持仅当前任务或长期指导、锁定/停用/恢复、手工方法文本与工作目录内 UTF-8 文档导入。文档按当时内容形成不可变版本，后续文件修改不会悄悄改变已导入方法。方法复用现有 Skill 版本存储，采用状态为 `user_guidance`，不伪造可执行验证或 active Skill。

实际生效的 ContextSnapshot 可在「学习」查看。运行中的指导不会改写已经发送的模型请求或尚未完成的工具交换，而是在 `before_llm` 替换管理块并记录 `context.updated`。记忆检索只表示看到了参考；模型通过 `agent_record_use` 关联真实已完成动作时，记录模型声明的采用，仍不声称因果改善。

## 预算与恢复

概览的「调整预算」或「工作方式 → 修订角色与预算」可设置每日总量、单工作包、单目标累计额度及每日调用次数。新 Agent 的工作包默认额度为每日总量的 1/10（向下取整，至少 1 token），每日调用次数默认 2000；显式填写的额度优先，已有配置不会被默认值覆盖。选择「每日总量的 1/10」时，保存为按当前每日额度计算出的具体数值；以后调整每日额度可再次选择按比例计算。

单目标、每日总量和工作包额度同时约束请求。单纯提高工作包额度不会提高单目标累计上限。仅修改预算不会使待派发工作包因 Role 版本变化而失效；已创建 Session 的 token 上限仍需暂停后通过 Session 的预算设置同步调整。

单目标累计额度默认 **1 亿 tokens**，显式配置的旧值保留。一个 Goal 可拆成多个工作包：Goal 额度统计该目标的规划、所有工作包的执行与验收，以及学习；工作包额度只统计该工作包的执行与验收，不包括学习。重试或复用 Session 不会清零这些累计用量，跨日也不会重置。额度是同时检查的上限，不是给每个工作包预先分配的余额。例如 Goal 为 1 亿、每个工作包为 1000 万时，多个工作包共同使用 Goal 的额度，还需为规划和学习留出空间。

预算拒绝信息会逐项列出超限范围（`daily` / `goal` / `work` / `learning` 或每日调用次数）、配置上限 `limit`、已占用 `used`、实际结算 `actual`、未结算或未知用量预留 `reserved_or_unknown`、本次申请 `requested`、预计合计 `projected`、可用余额 `remaining`、超出量 `exceeded_by`，并注明目标/工作包 ID 或日期窗口。`requested` 是发送前的保守预留，不是已经消耗的 token；被拒绝的调用不会记账。Goal 与工作包是累计限制，不会在次日重置。

规划、执行、验收和自动评估的每次模型请求都先预留 token，再根据真实 usage 结算。无 usage 或请求结果不明保留未知用量预留，不能算作零。每日额度按配置时区分窗，Goal/工作包累计消耗跨日保留；学习使用独立日子额度。没有可靠价格时只展示 token、调用数与未知项，不显示虚构费用。

估计采用请求字节数和输出上限，偏保守。已有 TaskSpec 和 evaluator 的时间/步数限制仍然生效。未知用量可能需要增加合同预算，系统不会自动释放未知预留或推定外部请求没有发生。

验收的单次输出和时间限制独立于 Goal 总量。Qwen 3.8 Max 开启思考且未显式配置 `thinking_budget` / `reasoning_effort` 时，验收请求默认将思考额度设为有效输出上限的一半，为最终判定留出空间；例如输出上限 8192 时，思考额度为 4096。显式模型配置保留，执行模型配置不变。验收超时会记录阶段、模型、实际请求时限与输出上限；暂停后 Resume 可续用现有检查点并更新验收分配。

同一任务在失败后 Resume，即使生成新的 run ID，也会保留最后检查点中的已观察结果、工具证据与执行快照；新任务或显式重新开始仍使用新的执行状态。验收阻塞后 Resume，如果原计划条目已全部结束，会保留历史条目并增加一个进行中的验收修复步骤，允许补读文件、补充证据和修复交付物；最终仍需通过原验收条件。Session 的运行失败和暂停原因显示在 Processing 内，长原因展开查看。

command ID 支持相同输入重放、不同输入冲突；人工修订携带 expected revision。Session 派发与所有权绑定、工作包、receipt 和 outbox 原子提交。后台在重启时对账终态并恢复待办。学习积压达到 20 项时暂缓派发，处理失败、取消或完成学习后可恢复推进。

## API 入口

| 路径（相对 `/v1/agents/{id}`） | 用途 |
| --- | --- |
| `commands`、`role/revisions`、`role-revisions` | 控制与 Role 版本 |
| `goals`、`goals/{goal}/commands` | 目标及修订、暂停、取消 |
| `runs`、`work-packages`、`routes`、`sessions` | 编排和执行关联 |
| `sessions/{session}/commands` | bind / takeover / release |
| `learning`、`learning/{cycle}/commands` | 学习进度、retry / cancel |
| `memory`、`memory/annotations` | 有效检索、标注与标注历史 |
| `guidance`、`profile`、`profile/commands`、`methods/import` | 明确偏好、方法版本与采用 |
| `context`、`context/{snapshot}`、`artifacts/{digest}` | 实际输入及归属校验 |
| `events?after={cursor}` | 顺序事件追赶，每次最多 200 条 |

历史列表支持 `offset` / `limit`（最多 200），返回 `next_offset`。页面当前按快照轮询，服务事件游标可供其他客户端增量追赶。

## 验证与当前范围

无需外部模型的完整演示：

```bash
PYTHONPATH=src:. uv run python tests/autonomy/smoke.py
uv run pytest -q tests/autonomy tests/service/test_background.py
npm --prefix tests/web test
```

smoke 使用真实 HTTP、SQLite、执行 worker 和 Deep eval 管线，模型判断由可控 fixture 提供；它验证编排、身份、恢复和注入，不证明真实模型判断质量。`--serve` 可保留临时演示用于浏览器检查，Ctrl-C 关闭并验证重启恢复。

当前以单个主要 Agent、一个 workspace、本机 native_os 为支持范围。P4 的自主目标发现、推断偏好、知识自动刷新、方法效果学习，以及 P5 的跨角色共享、外部事件和隔离并发尚未开放。手工方法与显式偏好已经生效，长期习惯推断不属于本次实现。
