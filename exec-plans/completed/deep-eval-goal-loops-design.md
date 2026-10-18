# Deep Eval Goal Loops：设计核对与交付记录

日期：2026-10-17。状态：**设计核对已完成；后续首版实现已交付，见 [实施记录](deep-eval-goal-loops-implementation.md)。**

以下是设计阶段的历史记录；“当前”均指实施前的代码基线。实际入口及实现差异见设计正文第 13 节。

设计正文：[Deep Eval：基于目标闭环的执行过程与成本分析](../../docs/deep-eval-goal-loops.md)。

本文件原来只有标题。本次将完整设计保留在上述正文，补齐这里的交付记录，避免出现两份互相漂移的设计。

## 本次范围

以当前源码校正 Reasoning efficiency / Deep evaluation 的现状、数据链路、分析范围、成本能力和实施落点。保留目标树 → Attempt → Phase、事实与语义分层、排他成本归属、先还原结构再诊断的设计思路。

## 已核对并修正

| 项目 | 当前实际情况及修正 |
| --- | --- |
| 模块归属 | 事实快照在 service/trajectory.py，语义作业在 service/semantic.py，分析引擎在 evaluation/，Evolve proposals 在 evolution/；移除不存在路径作为实施依据 |
| 来源范围 | 当前固定整个 session 截至 cursor 的来源；Deep start 没有 run_id 参数。新增单 run 是新功能，需要身份、引用及成本范围适配 |
| 原文引用 | EvidenceStore 固定 source bytes；L 行号与 session seq 不等同。快照过滤多数流事件并保留首个有效流输出；event_detail 恢复不等于所有历史文件已读入 |
| 事实图 | Base-only 已能生成执行流、验证、交付和结束边界；不能把当前系统描述成没有完整关系图 |
| 语义图 | Deep focus 已生成有引用的 reasoning transitions；完整 flow 不代表语义因果，现有 cycle_block 不是目标闭环 |
| 分析覆盖 | scan/focus/synthesis；同作用域最多四个非 verification 调用构成 segment，focus 受预算选择。scan 完成不代表全原文已读 |
| 成本 | 已有 usage ledger、调用 duration、TTFT/TPS、run/model 聚合与区间成本；缺少新语义层级的唯一 owner/shared bucket/守恒汇总 |
| 页面职责 | Reasoning efficiency 为本次主要重构页面；Deep evaluation 负责作业、预算、历史和覆盖，结果通过 onResult 连接 |
| 版本和恢复 | 明确 bundle/analyzer/rubric/activity/process/graph 各版本轴；旧恢复要求身份兼容，不承诺保留 JSON 即可恢复任意旧 analyzer |
| 样本依据 | 删除无法在仓库核对的三 run 审计定量结论，以现有测试夹具说明需求；真实样本需另登记来源和标注 |
| 实施路线 | 新分析放在拟新增 evaluation/goal_loops/；扩展现有 service/API/UI 分派，不修改任务执行流程 |

## 主要核对入口

- [事实快照及恢复](../../src/loom/service/trajectory.py)、[语义作业和预算](../../src/loom/service/semantic.py)、[HTTP 路由](../../src/loom/service/api.py)。
- [行为事实组装](../../src/loom/evaluation/behavior_facts.py)、[scan/focus/synthesis](../../src/loom/evaluation/behavior_judge.py)、[精确证据引用](../../src/loom/evaluation/evidence_store.py)。
- [Reasoning activity](../../src/loom/evaluation/reasoning_activity.py)、[判断校验](../../src/loom/evaluation/reasoning_process.py)、[指标组装](../../src/loom/evaluation/reasoning_metrics.py)、[执行流投影](../../src/loom/evaluation/reasoning_graph.py)。
- [成本统计](../../src/loom/evaluation/base_metrics.py)、[usage ledger](../../src/loom/evaluation/token_ledger.py)、[区间查询](../../src/loom/evaluation/observation_cost.py)。
- [页面数据连接](../../src/loom/web/assets/views/trajectory.mjs)、[评估作业 UI](../../src/loom/web/assets/views/evaluation.mjs)、[Reasoning efficiency](../../src/loom/web/assets/views/reasoning.mjs)。

## 验证记录

执行现有回归，未新增或修改测试，也未调用真实 evaluator：

```sh
.venv/bin/python -m pytest tests/evaluation/test_reasoning_graph.py tests/evaluation/test_reasoning_process.py tests/evaluation/test_base_metrics.py tests/evaluation/test_behavior_observations.py -q
```

结果：73 passed。用于确认当前事实图、引用约束和成本行为，不代表新目标闭环分析器已经实现或通过语义质量验收。

未修改运行代码、历史评估结果或任务 workspace，未重启服务。后续已按正文 S1–S6 建立独立实施记录。
