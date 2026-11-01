# Loom 文档索引

docs/ 按文档性质分为四个子目录，每个子目录有自己的 `index.md`：

| 目录 | 内容 | 数量 |
| --- | --- | --- |
| [design/](design/index.md) | 设计文档：架构方案、功能设计与实现说明、使用指南 | 71 |
| [plan/](plan/index.md) | 执行计划：面向落地的分步实施计划 | 33 |
| [spec/](spec/index.md) | 规格文档：协议、报告结构、指标口径等契约式定义 | 5 |
| [review/](review/index.md) | 审查与分析记录：样本核对、能力审查、验证报告 | 5 |

## 约定

- `YYYY-MM-DD-` 前缀：按日期归档的文档（源自 superpowers 工作流；原 `superpowers/specs/` 为设计文档，已并入 [design/](design/index.md)；原 `superpowers/plans/` 即现在的 [plan/](plan/index.md)）。
- `.zh-CN.md` 后缀：中文版本；同名无后缀文件为对应的英文版本。
- 参考附件（非 Markdown）：[meta-harness 论文 PDF](design/meta-harness-2603.28052v1.pdf)、[Harness Engineering 文章存档](design/2026-07-04-harness.html)，均在 design/ 下。
- 历史文档中引用的源码路径可能已随重构失效，以当前代码为准。
- 新增文档：按性质放入对应子目录，并在该目录 `index.md` 登记一行。

## Coding agent 快速入口

- 改任务执行 / 插件 / session：design 索引「总体架构与任务运行时」
- 改 plan 循环 / 工作流路由：design 索引「Plan 与执行控制」
- 改验收 / 视觉断言：design 索引「验收与视觉观察」+ spec 的验收相关契约
- 改 trace 分析 / 行为评估 / Deep Eval：design 索引「Trace 分析与行为评估」「Deep Eval」+ spec 的报告结构
- 改自主 Agent：design 索引「自主 Agent」
- 动手前先查 [plan/](plan/index.md) 是否已有对应实施计划，避免重复设计
