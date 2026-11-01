# 设计文档索引

架构方案、功能设计与实现说明、使用指南。`YYYY-MM-DD-` 前缀文档源自 superpowers 工作流的 specs/（内容为设计文档）。同名 `.md` / `.zh-CN.md` 为英中对应版本。

## 总体架构与任务运行时

- [2026-06-05-loom-py-design.md](2026-06-05-loom-py-design.md) — Loom Python 移植总体设计，最早的架构基线
- [2026-06-28-real-project-smoke-loop-design.md](2026-06-28-real-project-smoke-loop-design.md) — 真实项目 smoke 循环设计
- [2026-07-01-generic-task-runner-design.md](2026-07-01-generic-task-runner-design.md) — 通用任务 runner 设计
- [2026-10-03-persistent-service-sessions-design.md](2026-10-03-persistent-service-sessions-design.md) — 常驻服务与持续 Session 交互设计
- [2026-10-04-general-task-plugin-runtime-design.md](2026-10-04-general-task-plugin-runtime-design.md) — 通用任务执行与插件 runtime 设计
- [2026-08-01-context-compaction-resume-design.md](2026-08-01-context-compaction-resume-design.md) — 上下文压缩与可恢复任务运行
- [2026-11-01-online-task-spec-design.zh-CN.md](2026-11-01-online-task-spec-design.zh-CN.md) — 在线 Task Spec 机制设计
- [general-task-runtime.md](general-task-runtime.md) — 通用任务与执行插件：TaskAssembly、`--task-spec`、配置方式（当前行为）
- [web-frontend.md](web-frontend.md) — 持久 session 的 Web 前端使用指南
- [session-setup.md](session-setup.md) — Session 创建流程与 LLM 配置推荐
- [streaming-event-transport.zh-CN.md](streaming-event-transport.zh-CN.md) — Streaming 事件的异步批量持久化
- [run-stopping.md](run-stopping.md) — 常驻 session 的协作式停止：检索账本与进度提醒
- [conversation-process-redesign.md](conversation-process-redesign.md) — Conversation 过程展示重构方案
- [session-self-diagnosis.zh-CN.md](session-self-diagnosis.zh-CN.md) — Session 自诊断：隔离 worktree 中编辑、验证、保留补丁

## Plan 与执行控制

- [2026-07-31-plan-execute-loop-design.md](2026-07-31-plan-execute-loop-design.md) — Plan & Execute 循环设计
- [2026-07-31-unified-task-command-design.md](2026-07-31-unified-task-command-design.md) — 统一 task 命令设计
- [2026-08-01-llm-driven-plan-checkpoints-design.md](2026-08-01-llm-driven-plan-checkpoints-design.md) — LLM 驱动的 plan 检查点
- [2026-08-01-tool-failure-plan-barrier-design.md](2026-08-01-tool-failure-plan-barrier-design.md) — 可恢复工具失败与 plan 更新屏障
- [2026-08-02-auto-plan-workflow-routing-design.md](2026-08-02-auto-plan-workflow-routing-design.md) — 自动 plan 工作流路由
- [2026-08-02-plan-transition-step-boundary-design.md](2026-08-02-plan-transition-step-boundary-design.md) — plan 切换与 step 边界
- [2026-08-02-streaming-workflow-route-tool-choice-design.md](2026-08-02-streaming-workflow-route-tool-choice-design.md) — 流式工作流路由与工具选择
- [adaptive-dag-subagents.zh-CN.md](adaptive-dag-subagents.zh-CN.md) — Adaptive plan：DAG 执行图与 Context 复用（opt-in `adaptive_dag` 插件）

## 工具、知识与外部集成

- [2026-06-23-tools-system-design.md](2026-06-23-tools-system-design.md) — 工具系统设计
- [2026-07-18-task-edit-file-design.md](2026-07-18-task-edit-file-design.md) — task 内 `edit_file` 工具设计
- [external-tools.md](external-tools.md) / [中文](external-tools.zh-CN.md) — 外部工具（MCP server / CLI）注册中心与 session 绑定
- [knowledge-bases.md](knowledge-bases.md) — Session 知识库：引擎选择、embedding 绑定、检索工具

## 验收与视觉观察

- [2026-10-10-runtime-acceptance-gate-design.md](2026-10-10-runtime-acceptance-gate-design.md) / [中文](2026-10-10-runtime-acceptance-gate-design.zh-CN.md) — 主 loop 的任务验收与 verify gate
- [acceptance-method-repair.zh-CN.md](acceptance-method-repair.zh-CN.md) — 验收条件、验证方案与证据复用（任务类型无关）
- [acceptance-tool-evidence.zh-CN.md](acceptance-tool-evidence.zh-CN.md) — 工具调用验收与证据保留
- [2026-10-31-task-loop-acceptance-feedback-and-resume.zh-CN.md](2026-10-31-task-loop-acceptance-feedback-and-resume.zh-CN.md) — Task loop 验收反馈闭环与完整恢复
- [2026-10-22-visual-observation-and-acceptance-design.zh-CN.md](2026-10-22-visual-observation-and-acceptance-design.zh-CN.md) — 视觉观察与验收体系设计
- [visual-observation-and-acceptance.zh-CN.md](visual-observation-and-acceptance.zh-CN.md) — Web / Canvas / WebGL 视觉观察与验收使用指南

## Trace 分析与行为评估

- [2026-06-28-trace-driven-evolution-design.md](2026-06-28-trace-driven-evolution-design.md) — Trace 驱动演进设计
- [2026-07-02-trace-evaluation-evolution-platform-design.md](2026-07-02-trace-evaluation-evolution-platform-design.md) — Trace 评估与演进平台设计
- [2026-07-03-trace-analysis-kernel-redesign.md](2026-07-03-trace-analysis-kernel-redesign.md) — Trace 分析内核重构
- [2026-07-04-evaluation-evolution-bundle-contract-design.md](2026-07-04-evaluation-evolution-bundle-contract-design.md) — 评估到演进 bundle 契约设计
- [2026-08-06-evaluation-judge-streaming-design.md](2026-08-06-evaluation-judge-streaming-design.md) — 评估 judge 流式化设计
- [2026-09-05-trace-effectiveness-analysis-design.md](2026-09-05-trace-effectiveness-analysis-design.md) — Trace 五维精确诊断设计
- [trace-effectiveness-analysis.md](trace-effectiveness-analysis.md) — Trace 效果分析：v2 用法与 v3 入口
- [session-trajectory-analysis.md](session-trajectory-analysis.md) — Session 轨迹分析：当前实现说明
- [2026-10-08-behavior-evaluation-evolution-design.md](2026-10-08-behavior-evaluation-evolution-design.md) / [中文](2026-10-08-behavior-evaluation-evolution-design.zh-CN.md) — 行为评估与演进设计（v3 总体）
- [behavior-evaluation-v3.md](behavior-evaluation-v3.md) / [中文](behavior-evaluation-v3.zh-CN.md) — 行为评估 v3：使用方式与落地状态
- [behavior-sequence-refactor.md](behavior-sequence-refactor.md) / [中文](behavior-sequence-refactor.zh-CN.md) — 跨步骤行为评估重构（analyzer 3.5 / rubric 3.3）
- [reasoning-efficiency-graph.md](reasoning-efficiency-graph.md) / [中文](reasoning-efficiency-graph.zh-CN.md) — 推理效率：证据与决策过程图
- [action-utility-evaluation.zh-CN.md](action-utility-evaluation.zh-CN.md) — Action 语义效用评价

## Deep Eval

- [deep-eval-goal-loops.md](deep-eval-goal-loops.md) — 基于目标闭环的执行过程与成本分析（主设计）
- [deep-eval-goal-tree-design.md](deep-eval-goal-tree-design.md) — Deep Eval 与 Goal Tree 当前实现设计
- [deep-eval-validation.zh-CN.md](deep-eval-validation.zh-CN.md) — `goal_graph_v1` 分析管线与确定性校验
- [deep-eval-reliability-design.zh-CN.md](deep-eval-reliability-design.zh-CN.md) — Deep Eval 可靠性重构设计
- [deep-eval-memory.md](deep-eval-memory.md) — Deep eval Memory：独立经验提取阶段
- [runtime-memory.md](runtime-memory.md) — Deep eval memory 在执行期的复用（检索、注入、审计）
- [2026-10-26-cross-task-skill-induction-design.zh-CN.md](2026-10-26-cross-task-skill-induction-design.zh-CN.md) — Deep eval 跨任务归纳 Skill 设计
- [cross-task-skills.zh-CN.md](cross-task-skills.zh-CN.md) — 跨任务 Skill 实现与使用

## 自主 Agent

- [2026-10-25-autonomous-agent-design.zh-CN.md](2026-10-25-autonomous-agent-design.zh-CN.md) — 自主 Agent：数字分身与持续工作设计
- [2026-10-30-autonomous-agent-execution-extensions.zh-CN.md](2026-10-30-autonomous-agent-execution-extensions.zh-CN.md) — 自主 Agent 执行扩展：隔离目录、模型分工与流水调度
- [autonomous-agents.zh-CN.md](autonomous-agents.zh-CN.md) — 自主 Agent 完整运行与调度设计：阶段规划、执行/分析双队列、预算恢复与人工介入

## Meta-Harness 与模型配置

- [meta-harness.md](meta-harness.md) — Governed Meta-Harness：优化、campaign、实验与治理接口指南
- [2026-07-18-governed-meta-harness-design.md](2026-07-18-governed-meta-harness-design.md) — Meta-Harness 治理集成设计
- [2026-07-19-meta-harness-optimize-command-design.md](2026-07-19-meta-harness-optimize-command-design.md) — 一键 Meta-Harness 优化命令设计
- [2026-07-18-bailian-request-options-design.md](2026-07-18-bailian-request-options-design.md) — 百炼请求选项设计
- 附件：[meta-harness-2603.28052v1.pdf](meta-harness-2603.28052v1.pdf)（论文）、[2026-07-04-harness.html](2026-07-04-harness.html)（Harness Engineering 文章存档）

## TUI

- [2026-06-28-tui-plugin-llm-streaming-design.md](2026-06-28-tui-plugin-llm-streaming-design.md) — TUI 插件与流式 LLM 设计
- [2026-07-19-optimize-operations-tui-design.md](2026-07-19-optimize-operations-tui-design.md) — Optimize 操作 TUI 设计
- [2026-08-01-tui-plan-timeline-history-design.md](2026-08-01-tui-plan-timeline-history-design.md) — TUI plan 时间线历史
- [2026-08-01-tui-semantic-event-summary-design.md](2026-08-01-tui-semantic-event-summary-design.md) — TUI 语义事件摘要
- [2026-08-01-tui-thinking-metrics-design.md](2026-08-01-tui-thinking-metrics-design.md) — TUI thinking 指标
