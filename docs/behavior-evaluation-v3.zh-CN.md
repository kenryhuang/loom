# 行为评估 v3

[English documentation](behavior-evaluation-v3.md)

v3 将 **Base Eval**（调用数、步骤、token 和独立的目标验收）与 **Deep Eval**（意图对齐、计划质量、有效推进、调查效率、适应与恢复）分开。新建 Web/服务评估默认使用 v3；[设计文档](superpowers/specs/2026-10-08-behavior-evaluation-evolution-design.zh-CN.md)要求的人工校准尚未完成。

## 使用方法

Web：打开 Session 的轨迹分析，进入 **Deep evaluation → Evaluator settings → Analysis version → v3**，选择评估模型和预算。Base 页面会展示目标/计划时间线及验收结果。旧 v2 结果保持原有解释。

离线分析使用同一条流水线：

```bash
uv run python -m loom.evaluation.analyze --trace-path runs/task.trace.jsonl \
  --analysis-version v3 --out-dir .loom/evaluation-v3

uv run python -m loom.evaluation.analyze --trace-path runs/task.trace.jsonl \
  --analysis-version v3 --judge --config config.yaml --model kimi_judge \
  --judge-max-calls 40 --judge-max-tokens 300000 --judge-max-segments 24 \
  --out-dir .loom/evaluation-v3
```

模型名称替换为本地配置中的别名。添加 `--tui` 可查看评估活动。输出包括独立可读的 `evaluation-bundle.json`、`report.md`、固定源 trace、带哈希的事实账本和 `behavior-checkpoint.json`。使用相同输出目录可恢复相同源、模型和分析设置的任务；预算不足时提高资源上限。改变源、模型或分析设置时使用新目录。

服务端原有评估启动接口支持 `analysis_version: "v3"`，catalog 返回 `analysis_versions`。v3 中 `max_rounds` 表示重点复核的**行为片段上限**，不再表示只看前若干模型调用。重点选择之前扫描完整源索引；当前一个片段最多包含同一步骤、同一目标版本下连续的四次模型调用。这是确定性的复核窗口，不代表已证明其中存在因果关系。

## 模型与时间预算

在 `config.yaml` 或 TOML 顶层配置 `evaluation_model: judge`，其中 `judge` 必须是 `models` 下已有的模型别名，即可独立于任务的 `default_model` 选择评估模型。未配置时复用默认模型别名，但使用评估专用的生成限制；Web 也可以覆盖选择。评估会关闭已配置的 `enable_thinking`，将已配置的 `reasoning_effort` 降为 `low`，不修改任务模型配置。扫描/汇总输出最多 2048 token，片段分析最多 4096 token；更小的模型输出限制会保留。

新建 Web/服务评估默认：24 次调用、12 万已报告 token、600 秒、8 个重点片段、每阶段一次证据补读，单次调用最多 120 秒（`max_call_seconds`）。非流式评估请求有 socket 超时，并避免取消后的阻塞 HTTP 请求继续拖住事件循环退出。远端请求可能仍在运行，未返回的用量保持未知。

总时间的 20% 预留给汇总。重点分析调用超时后，停止新增片段分析，尝试在剩余预算内汇总已保存结论。进度区分别展示扫描、片段分析、汇总，以及补读证据、修正输出和阶段模型耗时。已验证的局部结论在运行中和中断后都可查看；待分析片段和未选入本次评估的片段分别展示。

历史 v2 任务保持原版本和配置。点击 **New behavior evaluation** 准备新建 v3 评估；恢复 v2 只复用 v2 批次。旧提示词版本的 v3 检查点仍可查看，提示词升级后需要新建评估。恢复时模型和分析配置保持不变，只允许提高总调用数、token 和时间预算。

## 如何理解结果

- 目标时间线区分消息被接受、被执行器应用和真正出现在模型请求中。自然语言修订含义不完整时保留原要求，直到完整有效目标被记录前，覆盖仍为 unknown。
- 计划被运行时接受，不代表覆盖了全部交付要求；节点完成也不等于已有外部证据。简单任务无需被强制要求有正式计划。
- 目标结果包括 `achieved`、`not_achieved`、`partially_verified`、`unverified`。运行结束、生成报告或命令成功，均不能单独证明完整目标达成。
- Host 验证记录包含明确范围、前后文件哈希、实际 oracle 和环境身份。只有完整要求覆盖及最终版本适用性得到确认，才能得到 achieved。现有报告完成检查会记录文档完整性验证，但只覆盖部分目标，且不保证工作区独占；它不能证明报告的分析结论正确。
- Deep Eval 按“扫描 → 重点片段与对照 → 综合”执行。重复调用只提名候选问题；有用的否定结果、条件变化后的合理重试可以是有效行为。
- 流水线完成、尝试覆盖和有效覆盖分别展示。全 unknown 的分析可以正常结束，但有效覆盖仍为零；未选片段保持 unknown。
- 综合阶段实际接收局部结论对应的原文和精确返回范围。综合失败后，已经保存的局部结论仍可查看。
- 效率指标需要有证据的因果或可见性端点。单纯声称找到根因、证据曾经存在、或后来无关的成功，都不能构成效率或恢复指标。没有可靠关联时，计划响应延迟等端点仍为 unknown。重叠因果窗口不能相加；冗余成本按模型调用的唯一归属去重。推断的成本不是预计节省量。

阶段预算分配为：扫描 15%、重点复核 60%、综合 20%，修复最多占调用数的 5%，每个阶段响应至多修复一次。极小调用预算给阶段保留最低额度，但仍受全局上限约束。内置 provider 的输出上限为扫描/综合 2048 tokens、重点复核 4096 tokens。token 阈值在发请求前检查，正在执行的请求可能使最终用量超过阈值；中断请求的缺失用量保持未知，续跑不清零累计开销。

## Evolve 与优化实验

v3 建议使用 `loom.evolution.hypotheses.v2`，按照稳定的行为模式、原因机制和干预位置归并。每条建议保留支持证据、反证、替代解释、预测端点和具体验证方案。建议仍处于未测试状态；具备实验条件不代表实验已经证明有效。

也可以直接从已有 v3 bundle 生成建议，无需再次调用模型：

```bash
uv run python -m loom.evolution.analyze \
  --evaluation-bundle .loom/evaluation-v3/evaluation-bundle.json \
  --out-dir .loom/evolution-v3
```

将 v3 种子分析接入已有受治理约束的优化流程：

```yaml
meta_harness:
  seed_analysis_version: v3
  # 保留原有 proposer_model、solver_model、judge_model 和任务集配置。
```

v3 种子任务要求明确、冻结的 task verifier；成本收益生效前，任务成功率必须达到 100%，并且不能相对基线回退。verifier 应覆盖必需产物和需要保留的行为。种子证据采用 v3，现有配对 runner 继续使用真实的 v1 测量产物及原有治理流程，不将行为维度伪装成旧版分数。独立的 `assess_paired_experiment` 适配器接收任务、环境、评估器一致的配对记录；即使更便宜，保留行为检查失败也会被拒绝。

## 校准与当前限制

本次实现不声称已达到人工校准的诊断准确率或 token 节省目标。默认切换是运行流程调整，并不代表已通过人工校准验收门槛。基于实际人工标注和 v2/v3 配对测量生成门槛报告：

```bash
uv run python -m loom.evaluation.calibration --corpus calibration.json --out calibration-report.json
```

corpus 是 JSON 数组。每条记录包含 `episode_id`、`family`（`coding`、`research`、`general`）、`split`、两位 `reviewer_ids`、`adjudicated`、`used_for_prompt_development`、`repeat_runs`、`evidence_audit_passed`、`unsupported_achieved`。`findings` 包含 category（`intent`、`plan`、`cycle`、`evidence_adoption`）、`human_present`、`v3_present`；`outcomes` 包含 `sufficient_evidence`、`human_status`、`v3_status`。成本比较使用 `v2_tokens`、`v3_tokens`、`v2_supported_coverage`、`v3_supported_coverage`。

报告展示原始计数、不确定性区间及独立质量/成本门槛；缺少人工标注时明确阻止切换默认版本。工具保守要求至少 30 个覆盖三个任务类型、经过独立复核的 held-out episode。语料负责人需确保复核身份及独立性；工具不会自行生成标注，也不会自动修改默认版本。
