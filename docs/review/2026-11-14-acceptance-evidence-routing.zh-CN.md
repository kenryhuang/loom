# sess_941068421f454452a58a948ed7c717a8 验收阻塞分析

## 检查范围

只读检查运行数据库、不可变 artifact 和验收实现，未重启服务、重跑命令、改动 Solaris 工作区或修改验收结论。

该 session 被多个工作包复用，标题仍是早期文档交付任务，不能用整个 session 的历史事件判断当前任务。

当前工作包为 `work_a2bd13739b9e457f8aea3bcefccc29ab`：运行 test/build/lint，形成 chunk 基线对比及归因、确认源码/依赖未改动、形成测试覆盖评估及缺口。当前新 run 为 `run_7b8c9805a9d0494a81eeb6a760af79d0`；检查时处于 running，验收 not_ready、results 为空。下面分析的是最近已产生阻塞结论的 `run_630c42248b674fa09d0f0c5bd5701e71`，goal revision 52。

## 最新完整阻塞结论

2026-11-14 16:50:55（Asia/Shanghai），事件 52748：

| 验收项 | 结果 | 证据/原因 |
|---|---|---|
| npm-test-execution | passed | 28 个测试文件、397 个测试通过 |
| npm-build-execution | passed | 构建成功，chunk 列表及体积已采集 |
| npm-lint-execution | passed | lint 退出码 0 |
| git-zero-src-changes-and-no-new-deps | passed | src/、package.json、package-lock.json 核验输出为空 |
| test-coverage-evaluation | blocked | 有测试清单，没有完整行为覆盖与缺口分析交付 |
| chunk-baseline-comparison | blocked | 该批次看不到已采集的 build 输出和基线文档 |
| scale-config-and-data-layer-unchanged | blocked | 该批次看不到 git diff 工具证据 |
| goal | not_run | 等待明细条件通过，属于连带未执行 |

来源：`runs/service/artifacts/00d50643e6e5b4602db372e03d88952c6452b73d3303af689727ea875001725a.json`。

## 已复现的根因

### 1. 分批验收隔离了存在依赖关系的证据

`acceptance_batches.BatchReview.view` 对确定性检查的原始结果按 criterion ID 过滤：仅当前批次包含同 ID 检查时附带原始 detail。整体 goal 可看到收据，但普通语义批次无法取得其他批次的检查输出。

因此，build 批次可以通过，而 chunk 对比批次没有 build 输出；源码未改动检查通过，而 SCALE_CONFIG 未改动批次没有其可支持的 git 证据。分批任务之间缺少显式证据依赖及原文检索通道，分组方式影响了证据可见性。

### 2. 把文件 scope 当成了完整证据筛选条件

当前 run 的五次执行工具调用全部为 shell_execute：test、build、lint、git 检查、grep 基线文档。`record_call` 的 paths 只提取输入对象里的 path/file_path/cwd；这五次都只得到 `.`。command 字符串中的文档和源码路径没有进入 paths。

`acceptance_evidence.evidence(full=True)` 对带 scope 的语义检查开启 scoped_review，通过 paths 与 scope 的精确/子路径匹配选证据，因此全部落选。显式 gap 请求同一文档路径时仍走同一筛选条件，无法修复。

基线内容确实存在：事件 52460、artifact `009988fad9c478c5ba30f3a6505f5b65dfd5a0db8801e725e0fe9da03bb9d129` 包含 §4 chunk 体积前后对比及归因表。并非没有读取。

使用真实事件重建五条 receipt，直接调用现有 evidence 和 BatchReview.view，未调用模型或执行任务命令，得到：

```
chunk-baseline-comparison: collected=5, selected=0
scale-config-and-data-layer-unchanged: collected=5, selected=0
fresh checks available: npm-test, npm-build, npm-lint, git
chunk batch receives: candidate, tool-call-coverage
baseline receipt contains section: True
```

最后的 chunk 判断 artifact 为 `2cd196ebde87407c1b5db1dc0058566e39ab514b7db6a42b52df88323afded84`；SCALE_CONFIG 判断 artifact 为 `949ff18b57ab4aa55f441c3ca0951293ec689d8bd43b05730881f9b19b305870`，都确认了此证据缺失。

### 3. 空 finish 声明进入了昂贵的完整验收

candidate artifact `18fde5d2d751ff5f32be9cc02d4d3e7d83e5f4756c107fb2043cf9b9339b271c` 的内容是一个决策对象：action.kind=custom、target=finish、input={}。reasoning 声称提交最终报告，但实际没有报告正文，覆盖评估只说“可基于实测测试文件清单形成”。

真正 finish 工具的 handler 已要求非空 report/content。然而非工具 custom 决策经过 managed_step 的 direct_answer 路径，未命中这一检查；报告抽取失败后，整个决策 JSON 被格式化为 candidate，进入完整验收。

这既是求解器未完成交付，也是入口预检不足。不能把 reasoning 里的“已完成”当成报告；也不应未经任务要求强制报告必须落盘为 docs 文件，直接交付完整报告同样可以满足要求。

### 4. 输出预算失败是另一条独立分支

事件 52657、52663 显示 acceptance_verification 连续两次被标记为 truncated，随后出现 ACCEPTANCE_OUTPUT_BUDGET。事件 52720 又发生一次截断，后续纠错才成功。这不是被证明的业务不通过。

当时 input_budget 记录 max_output_tokens=8192；较大的 acceptance 总调用/时间预算不能直接解决单次响应截断。当前 task-wide max_model_calls 仍为 200，与 acceptance 内部 max_model_calls=588 是不同额度；该数字不是上述 16:50 最终阻塞的直接原因。

## 验收项本身是否不合理

test/build/lint、基线对比、源码/依赖约束、覆盖缺口分析均来自当前工作包目标，不是凭空加码。但定义和消费证据的协议存在缺陷：

- scope 同时承担文件依赖/缓存失效和证据路由，而 shell 获取的证据不一定有准确 paths。
- 语义条件没有声明它依赖哪些命令结果；不能假定每个条件只使用自己的证据。
- 测试文件存在且测试通过，不等于行为覆盖充分；缺口分析仍是实际未完成的交付。
- 命令后的 `echo EXIT_CODE:$?` 让 shell 外层退出码变成 echo 的退出码。当前模型判断读取了输出中的真实状态，但这种 verifier 编码容易让执行成功与验证通过混淆，应使用结构化退出码和输出证据，不把外层 0 当结论。
- 最新重新规划的 final-report 与 goal 存在重复检查空间，但这不是本次三个 blocked 的直接根因。

## 修复方向

1. 建立统一证据目录，包含执行调用和验收命令收据。批次按证据依赖选择来源，缺口修复能按 source ID 取回任意已存在原文；不能限定当前 criterion ID。
2. 文件 scope 负责依赖/失效，不充当证据存在性的硬门槛。shell 的输入/输出以目录元信息参与有界选择；无法确定路径时标记未知，不等于无关。避免把所有原文重新塞进每批上下文。
3. 对 target=finish 的结构化候选统一做报告预检；空报告先返回求解器补齐，不进入整套验收。保留普通直接回答和有效报告的完成路径。
4. 区分 evidence_view_missing、deliverable_missing、verification_output_limit、prerequisite_not_run。已有证据优先本地补视图，交付缺失只修交付，输出预算只恢复未完成判断，避免全部重新执行。

以上记录初次诊断。后续已补齐统一来源目录、跨批证据获取、持久恢复与严格引用校验，详见 [分阶段验证设计的补齐记录](../design/2026-11-14-incremental-acceptance.zh-CN.md)。同一组真实 artifact 离线复放后，两个语义批次均可读取全部五条工具证据以及 build/git 检查输出；没有调用真实模型或修改运行中任务。

空 custom finish 的入口预检和单次模型输出截断仍是独立问题，不能将本次证据路由修复描述为全部验收阻塞均已解决。
