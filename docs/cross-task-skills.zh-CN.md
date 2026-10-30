# 跨任务 Skill：实现与使用

2026-10-26。实现依据：[跨任务归纳设计](superpowers/specs/2026-10-26-cross-task-skill-induction-design.zh-CN.md)。本文记录首期同 workspace、声明式候选的实际行为与边界。

## 使用入口

启动新版 `loom serve`，在 **Analysis → Skills** 中输入要比较的问题或方法，选择 evaluation 模型和调用上限。Deep evaluation → Memory 的记录详情也有「加入跨任务归纳」入口，会把该记录的问题带入 Skills 表单。

开始归纳会独立使用 evaluation 模型，不占用原任务或 Deep eval 的预算。首次生成前需要至少一份有原始证据的 published memory；没有足够独立任务时仍可生成候选，页面明确显示不足。查看案例目录会将这些内容登记为已暴露的发现数据。

候选详情展示去重家族数、条件覆盖、步骤、逐条支持／反例／未知矩阵和原始 trace 链接。可下载包含 `SKILL.md`、`contract.json`、`evidence.json`、`validation.json` 和 examples 说明的 JSON 文件包。结构化版本是权威数据，导出 Markdown 不能赋予验证状态。

「显式试用下一次 run」只给当前 Session 的下一次 run 提供该版本，执行中的 run 不能被此操作改变。候选的证据状态仍是 proposed。active 的发布入口只在真实成对验证通过后出现；普通用户操作不能跳过执行验证。

## 归纳链路

1. **来源对账**：Session 与 Deep eval 提交轻量 dirty 通知，后台对账；页面访问也触发对账。分页扫描整个 workspace，没有 RuntimeMemory 的 500 条截断。失败、未知、未提取 memory 的任务保留元数据。
2. **任务身份与血缘**：新 `run.started` 保存实际生效的任务合同。相同目标 restart 保留 episode；后续新目标有新 episode。历史缺合同或目标中途变化无法定位到局部证据的案例保守标为 boundary_uncertain，不增加独立支持。相同源执行的多次评估保留为解释视角；冲突不会投票消失。中英文词、中文二元词、路径与端口规范化用于近似去重。
3. **冻结用途**：先冻结 corpus、家族／split group、discovery／validation／holdout、模型配置摘要与预算。已查看、已参与归纳或已经暴露 Skill／memory 的案例不能被当作未见留出样本。
4. **召回与重排**：同范围内按合同与问题关键词召回最多 100 条；模型比较目标、起始状态和约束。Host 保留失败与未知代表，不依赖模型仅挑成功案例。初始最多 18 个家族，为反例留空间；每轮加入最多 3 个新家族，整批最多 24 个。
5. **对齐与候选**：原始引用转成本次请求的证据句柄。对齐语义作用、明确依赖和有证据的条件差异，再生成参数、步骤、运行时 guards、分支和必要检查。Host 验证句柄、步骤依赖、循环依赖和分支的可观察性。
6. **独立批评与反例检索**：另一次模型调用核对原始片段并提出反例查询，最多两轮修订。范围不明、解释冲突、检索／证据截断都保留；重要缺口阻止采用。不同案例的局部步骤合成后，整条方法始终是 proposed composition。
7. **不可变版本**：新版本按内容寻址，生命周期单独更新并检查 revision。源记录修订、撤销、删除、图版本失配或家族合并会阻止继续使用；证据不可用时停止导出。使用中的版本在下一次合法模型请求边界重新检查。

每个模型调用先持久化预算预留，再发送请求。已完成阶段按输入摘要缓存；失败恢复复用阶段，未知响应保留费用预留。作业总上限冻结，耗尽后需创建不同预算的新作业，不能用恢复清零。取消在请求边界生效，正在进行的请求受单次 timeout 限制。

方法的运行时 JSON（不含 claims 与 blocking_issues，包含 composition_status）最多 6,000 字符。超限修订会收到实际总长度、各字段长度及建议预算，以 5,000 字符为目标保留余量，保留前置条件、验证与未知项。失败作业不会生成可采用版本；页面说明失败和长度诊断，数据库保留完成阶段及最终被拒草稿，恢复仍受原预算约束。

证据引用错误与长度错误一起反馈；无效引用需要重写完整候选，不能作为证据保留下来压缩。只有引用检查通过的超限草稿才进入独立的方法压缩，证据矩阵保持不变，省略的字段保留原值。`supports`／`contradicts`／`unknown` 只接受已交付的证据句柄；命题 ID 与未知项解释不能冒充原始证据。

默认上限：9 次调用、1,000,000 token（含未知请求预留）、1,200 秒、单次 180 秒、48,000 证据字符、160,000 提示字符、两轮修订。token 预留使用保守的多语言 UTF-8 字节上界，实报用量返回后结算。

## TaskAssembly 复用

service worker 使用原有受信任数据库路径；独立 `loom task --memory-store ...` 也可读取同 workspace 的 active Skill。未配置此路径不启用 Skills。现有 RuntimeMemory 的查询范围和 500 条上限不变。

- 检索最多三个相关 active 摘要，显式试用时可提供指定 candidate／trial。
- guards 根据实际 workspace access、平台、runtime 和已装配的工具判断，未知即不采用；不会添加新工具或权限。
- `skill_read` 读取完整合同和引用；`skill_select` 需要当前 run 中已完成的真实工具调用引用，用来支持模型对自然语言前置条件的判断；`skill_record_step` 关联已完成的实际调用与步骤。
- 自然语言条件与步骤归属仍是模型解释，标为 `model_reported_*`，不能当作 Host 已证明语义正确。工具调用引用的真实性由 Host 校验。
- 检索、选用、步骤关联和 Host 的 acceptance outcome 分别留痕。任务验收结果不宣称是 Skill 的因果效果。试用／active 版本遇到采用后的 acceptance blocked，会暂停等待复查。
- `skill_*` 与 `memory_*` 的返回内容在后续 Deep eval 中都标为 historical_reference，不能靠读到旧总结制造新的 observed 成功证据。

## 独立执行验证

Python 集成入口为 `loom.skills.validation.SkillPairedValidation`，复用 `PairedExperimentRunner` 和现有 hard-objective gate。`loom.skills.execution.OptimizeSkillBackend` 复用现有 Meta-Harness 的真实 solver、快照物化、外部 verifier 和 trace analysis。

验证前提供冻结的 `PreparedTaskSets`、任务家族与划分组。`sealed_task(...)` 将已有快照与可执行 verifier 转成验证输入。不要把当前工作目录冒充历史初始状态。

```python
from loom.skills.execution import OptimizeSkillBackend, sealed_task
from loom.skills.validation import SkillPairedValidation

# prepared_tasks、artifact_store、provider 来自现有 Meta-Harness 配置。
# environment 是环境、模型、工具、权限、验收器、知识、记忆及预算的冻结 digest 映射。
backend = OptimizeSkillBackend(prepared_tasks, artifact_store, provider,
                               environment=environment)
validator = SkillPairedValidation(skill_store, artifact_store, backend, trial_directory)
result = await validator.run(version_id, sealed_tasks, environment, repetitions=3)
```

当前验证策略要求 discovery 至少三个有真实观察支持的家族和两种有原始证据的相关条件；validation 和 holdout **各**至少两个未见家族、两个隔离组，每阶段至少五对有效执行。默认每个任务重复三次，所有候选侧验收必须通过且不能退化。声明式来源检查及语义审查也必须通过。

两侧使用不同的工作目录、相同模型与验收器；候选侧只增加指定方法。自包含快照后端拒绝外部 memory／knowledge／tool metadata，关闭自动 Skill 检索；无 snapshot、verifier 或原始执行产物时不能通过。相关中文／英文改写不能靠换 family ID 冒充独立样本。基础设施失败保留，候选未采用方法也不从总体效果中剔除。验证过程保存实际 trace、evaluation artifact、配对指标、预留试验计划和执行记录。相同验证输入重复调用返回已有记录，不偷偷重新计费。

这个接口面向受信任的执行器；没有允许模型或浏览器上传一个“通过”分数就激活版本的接口。当前 UI 支持试用、查看验证产物与激活通过版本，实验数据准备和执行通过 Python 集成。

## HTTP 接口

所有接口沿用 service bearer credential，且必须带 `?session_id=...`；workspace 从 Session 的实际资源绑定取得。

| 接口 | 行为 |
| --- | --- |
| `GET /v1/skills` | 目录统计、作业、版本、模型和预算配置 |
| `GET /v1/skills/cases` | 使用 `?session_id=...&after=...`，稳定游标分页 |
| `POST /v1/skills/jobs` | `query`、可选 `model`、`settings`、`parent_version_id` |
| `GET /v1/skills/jobs/{id}` | 作业状态与用量，不返回留出语料 |
| `POST /v1/skills/jobs/{id}/resume`、`/cancel` | 恢复或取消 |
| `GET /v1/skills/versions/{id}`、`/package` | 版本、证据、使用与验证记录；导出文件包 |
| `POST /v1/skills/versions/{id}/trial` | `revision`；给下一次 run 安排一次显式试用 |
| `POST /v1/skills/versions/{id}/state` | `state`、`revision`；服务端校验来源、冲突与发布条件 |

## 当前边界

这是首期实现，不表示设计中的所有演进能力已经开放。

- 首期只支持同 workspace 的声明式方法；没有跨 workspace 推广、自动生成脚本或增加 subagent 并行权限。
- 分支可进入候选，但快照执行后端尚未提供逐分支执行回执；含分支的 Skill 暂不能自动发布为 active。
- 没有通用的历史环境重建器。缺快照或外部依赖的历史任务只能用于发现，不能离线证明复现成功。
- 来源对账、去重、撤销和使用失败监测已经自动运行；自动语义分类新案例、周期性自动付费重归纳、大语料分层综合、手工导入／编辑包尚未开放。可用「重新归纳」创建后继候选。
- 归纳作业可断点恢复；执行实验保留已经开始的记录和不确定成本，当前不自动重跑中断的成对实验。
- 统计家族数是最低覆盖门槛，不是普遍有效或因果关系的证明。初始事实快照和语义解释的证据不足继续显式保留。

实现模块位于 `src/loom/skills/`，service 入口为 `src/loom/service/skills.py`，UI 为 `views/skills.mjs`。

## 本次验证

- Skills 专项测试：18 passed，覆盖真实证据解析、重复评估与中文变体去重、同 Session 多目标、留出内容隔离、暴露污染、预算与中断恢复、有限修复、撤销、HTTP 编码 ID、运行时工具、成对实验以及真实快照／verifier 执行。
- 相关后端回归：129 passed（Skills、trace memory、RuntimeMemory、ManagedStep、HTTP、semantic evaluation、同目标 restart）。后续两项新增 Skills 防回归测试也通过。
- 前端全套：220 passed。Ruff 与 `git diff --check` 通过。
- 扩展回归发现的四项既有失败，已在独立导出的未修改 `193d52b` 上复现：`test_large_research_result_compacts_and_retains_session_artifact`、`test_service_reads_source_pages_to_the_end_without_refetching`、`test_long_history_has_a_bounded_prompt_and_keeps_the_current_request`、`test_resume_renews_time_allowance_durably_without_resetting_usage_or_checkpoint`。本次不改变这些上下文压缩、source 分页和历史 resume 断言行为。
- 历史数据库只读副本对账得到 119 个案例，其中 108 个有可用 memory 证据；抽查三个原始 trace 引用均可解析。现有历史合同不足，这 119 个案例均保守标记 boundary_uncertain，不能因此获得独立支持资格。
- 本地服务加载后，Skills API 和前端资源返回 HTTP 200；第一次建目录约 5.2 秒，缓存对账约 1.2 秒。没有为验证调用付费模型或生成真实 Skill。
