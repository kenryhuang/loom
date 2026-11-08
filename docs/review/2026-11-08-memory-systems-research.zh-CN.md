# 记忆系统重设计：参考机制与本地能力核查

调研日期：2026-11-08。Loom 基线：`8f56aff`。本文区分官方当前能力、历史论文机制、本地已安装能力，以及据此提出的 Loom 设计判断。没有部署新服务、调用付费模型、修改现有记忆或执行自动合并。

推荐设计见 [Memory V2](../design/memory-v2.zh-CN.md)，实施顺序见[迁移与验证计划](../plan/2026-11-08-memory-v2.md)。

## 1. 当前系统缺少什么

现有 Deep Eval records 是各次执行的提取结果；`MemoryStore.search()` 的签名去重只影响本次召回，不合并持久化知识，也不维护跨评估的实体、事实身份、替代关系或冲突集合。人工标注修正读取视图，依赖的仍是 evaluation/record 身份。

上一轮改动解决了查询性能、完整历史分页和重复请求，未实现语义上的知识整理。当前内存缓存也只是读缓存，不是知识结构。

相关现有实现：

- [提取与记录准入](../../src/loom/evaluation/memory/records.py)：逐条接受、隔离、证据和候选身份。
- [运行时检索](../../src/loom/memory/store.py)：作用域、有限召回、检索内去重。
- [人工标注](../../src/loom/memory/annotations.py)：原记录外叠加确认、纠正、屏蔽。
- [上下文快照](../../src/loom/autonomy/context.py)与[实际消息组装](../../src/loom/llm/api.py)：存在两路记忆检索，最终消息排除快照内 memory、另注入 retrieved_memory。
- [SkillStore](../../src/loom/skills/store.py)：已经有版本、依赖、来源有效性和 suspended 状态，不应再造一个与它脱节的“Skills 表”。

只读抽查上一轮性能测试的固定数据副本：4,043 条有效记录，来自 108 个 evaluation，包括 3,274 条 facts、394 条 lessons、375 条 workflows；另有 5 条未通过既有记录校验。按 kind/title/summary/scope/basis/validation/preconditions/trigger/steps/failed_approach/better_approach/revalidate_when 全部相同计算，只有 1 组完全重复、1 条冗余。按类别和规范化标题计算，有 56 组重复标题、63 条额外记录，最大一组 4 条。

这些是字符串统计，**不是语义重复率或冲突率**。标题不同可能表达同一知识；标题相同也可能是不同版本、不同条件或不同证据。迁移不能用标题相同或向量接近直接删除记录。

## 2. 参考机制及其适用边界

### 2.1 Graphiti / Zep：时态事实与来源图

官方设计用 episode 保存输入来源，以实体和有有效时间区间的关系表达知识，支持增量整合、历史查询，以及关键词、语义和图遍历检索。Graphiti 是图构建框架，应用仍需承担服务治理和隔离。[官方项目说明](https://github.com/getzep/graphiti)

Loom 借鉴：有效时间与记录时间分离；旧事实转为历史而非被抹掉；实体身份和条件参与冲突判定。Loom 不直接采纳“模型判冲突后按新旧时间覆盖”的全部实现策略。不同分支/环境不能互相淘汰；未知时间不能伪造；未来生效不能立即替代当前事实。这些要求进入独立验收用例。

### 2.2 Hindsight：经历、整合知识与可更新专题

官方 observations 机制在后台比较新事实和现有整合结果，维护支持来源；近重复整理受作用域约束。来源删除会影响对应派生知识。[Observations](https://hindsight.vectorize.io/developer/observations)

Mental Models 提供围绕主题维护的综合内容，保留来源及历史版本，并支持增量更新。[Mental Models](https://hindsight.vectorize.io/developer/mental-models)

Loom 借鉴：原始经历与整合知识分层、来源依赖和后台更新。专题摘要应是可重建投影，不能成为比原始证据更高一级的独立事实。高相似阈值也不能代替作用域、时态、否定词和条件检查。

### 2.3 Mem0：必须区分论文与当前产品

2025 年论文描述提取后检索相似记忆，再由模型选择 ADD / UPDATE / DELETE / NOOP 的流程。这是值得参考的“先比较再写入”结构。[论文方法部分](https://arxiv.org/html/2504.19413v1)

但本次查询的官方 `add` 文档明确说明当前平台和 OSS 的新增管线采用追加存储，不覆盖或删除旧记忆；显式 `update` 才修改已有内容及索引。不能据旧论文推定当前安装 Mem0 即自动满足 Loom 的去重和冲突维护。[当前 Add 文档](https://docs.mem0.ai/core-concepts/memory-operations/add)、[当前 Update 文档](https://docs.mem0.ai/core-concepts/memory-operations/update)

Loom 借鉴：把“提取候选”和“针对已有知识作出变更”分成两个阶段，并使变更成为可审计操作。不能将事实更新等同于无条件覆盖一段文本。

### 2.4 Letta：少量常驻记忆、按需读取与后台整理

当前 Agent SDK 使用记忆仓库：`system/` 文件进入每轮系统上下文，其余内容按需读取；后台 dreaming 可以整理记忆。该设计与旧版 block/archival API 应分开看。[当前 Memory 文档](https://docs.letta.com/agent-sdk/memory)

Loom 借鉴：将上下文工作集与长期记忆容量解耦，整理工作不占用正常回答的关键路径。Loom 中自动提取的知识仍是参考数据，不因被放进“核心记忆”而获得系统指令权限；人工长期原则沿用既有独立通道。

### 2.5 LangMem：记忆类型与写入时机分离

LangMem 区分 semantic、episodic 和 procedural 记忆，支持同步形成与后台形成；核心转换逻辑与具体存储分离。[官方概念指南](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)

Loom 借鉴：保留 Facts/Lessons/Skills 产品分类，同时在内部区分“某次经历”“可复用知识”“可执行程序”。用户偏好是有主体的语义信息；人工指令与自动推断偏好不能混成一个权威等级。

### 2.6 研究补充：组织与更新也会产生错误

A-MEM 提出动态索引、链接和记忆演化，说明仅把片段放进数据库不足以形成组织化记忆；这是研究方法，不能据论文直接宣布生产可靠性。[A-MEM](https://arxiv.org/abs/2502.12110)

连续 LLM 整理可能引入内容漂移，相关研究观察到整理收益并不持续单调增长。[Useful Memories Become Faulty When Continuously Updated by LLMs](https://arxiv.org/abs/2605.12978)

MemTxn 将支持来源检查、可见版本选择和故障恢复放在记忆更新边界，属于近期研究参考，而非 Loom 已验证的组件。[MemTxn](https://arxiv.org/abs/2607.27834)

据此提出的 Loom 约束：只改受影响的知识单元；保留原始证据和旧版本；让模型提交变更提案，由宿主检查范围、依赖、版本和状态；不以“整理次数增加”作为质量改善指标。

## 3. 本地引擎：已核实能力

### 3.1 YakDB

本机 Loom Python 环境检测到 `yakdb 0.2.4`，模块来自 `/Users/huanggui/workspace/yakDB/yakdb/__init__.py`。本地源码提交为 `ab3706cc78c1e167c77662c292fb527ab4d2d054`，remote 为 `https://github.com/openyak/yakDB.git`。

核查了本地 `yakdb_core/storage/sqlite.py`、`services/search_service.py`、`services/ingest_service.py` 及 Loom 的 [worker](../../src/loom/knowledge/yakdb_worker.py) 和 [adapter](../../src/loom/knowledge/yakdb_local.py)：

- 擅长文件解析、分页文本、FTS5 检索、中文回退搜索、来源页定位和内容级识别。
- Loom adapter 按文件名替换文档，文档更新/删除通过隔离 worker 处理。
- 发布时复制旧 metadata 数据库形成新 generation，再原子切换指针。
- 当前集成不是向量相似检索，也没有发现面向 memory claim 的时态、条件冲突裁决或技能验证状态机。

因此推荐用作 PDF/Office/文档证据入口和可选文本投影后端。文件字节去重不能等价于知识语义去重。不要把每条新事实作为一次独立文件导入：现有每次 generation 拷贝和 worker 启动会放大成本。

这些结论针对上述本地版本与 Loom adapter，不泛化为 YakDB 所有版本的能力上限。项目定位可交叉查看[官方仓库](https://github.com/openyak/yakDB)。

### 3.2 LightRAG

本机安装并由 `pyproject.toml` 固定 `lightrag-hku==1.5.7`。核查安装包 `lightrag.py`，存在文档删除、实体/关系编辑、实体合并和 custom KG 插入 API。上游也提供实体合并与图维护机制。[上游 Core 文档](https://github.com/HKUDS/LightRAG/blob/main/docs/ProgramingWithCore.md)

Loom 的实际集成见 [lightrag_worker.py](../../src/loom/knowledge/lightrag_worker.py) 和 [lightrag_local.py](../../src/loom/knowledge/lightrag_local.py)：

- 使用 JsonKVStorage、NanoVectorDBStorage、NetworkXStorage、JsonDocStatusStorage。
- 对文档执行模型抽取与 embedding；检索返回 chunks、实体和关系数据，不直接使用最终生成答案。
- 文档替换先在新 generation 删除旧文档再插入，成功后切换指针；失败保留旧索引。
- 当前 search 与 publish 共用 ownership 锁，按操作启动 worker，publish 复制旧 generation。
- 当前 adapter 没有提供 Memory V2 所需的 claim/version 双时间、审核语义、scope predicate、关系贡献逐条版本映射和确定性失效门禁。

因此推荐用作可选的实体关系召回索引，不能把原始 `ainsert` 的实体/关系合并当作知识正确性的最终裁决。它的实体去重与 Loom 的“两个命题是否等价、何时失效”是不同问题。

当前未执行 LightRAG 付费抽取或 embedding 实验，也没有宣称它在此数据集上的召回率和延迟优于其他实现。上线前需对已安装版本做单独适配验收，不能直接使用 upstream main 的接口承诺。

### 3.3 已有 SQLite 检索能力

Loom 已有 `sqlite_fts` 和 `sqlite_hybrid`：本地 FTS、向量与关键词混合检索、embedding profile 绑定等能力，见 [knowledge-bases.md](../design/knowledge-bases.md)。这些能力可复用，但 memory 的权威状态和 knowledge-base 的文档生命周期不能混为一套。

第一阶段无需更换主数据库。缺的是规范化知识结构、持续整合协议和独立索引，而不只是某一种搜索引擎。

## 4. 方案取舍

| 方案 | 优势 | 对本问题仍缺少什么 | 判断 |
| --- | --- | --- | --- |
| 继续向 evaluation JSON 追加，再提高相似度召回 | 改动少 | 没有稳定知识身份、冲突、时效和更新事务 | 不作为长期设计 |
| 全量送入 LightRAG，图结果直接注入 | 复用本地引擎、关系召回 | 需要额外解决版本、真值、权限与旧摘要污染 | 不作为权威层 |
| 全量送入 YakDB | 文档解析和检索易复用 | 不负责语义整合和冲突裁决 | 用于证据层或可选投影 |
| 整体引入 Graphiti/Hindsight | 有成熟的相近机制 | 新数据库/服务、现有审计和 Skill 体系迁移、需要再次验证治理语义 | 作为参考与后续比较基线 |
| Loom 知识注册表 + 持续整合 + 可替换索引 | 能沿用证据、人工标注、运行审计和 SkillStore | 需要实现生命周期、事务、评测 | 推荐 |

最终判断属于本项目架构建议，并非上述项目官方对 Loom 的评估。

## 5. 验证设计应借鉴什么

LongMemEval 的任务覆盖信息提取、跨会话推理、知识更新、时间推理和无法回答时的弃答。[官方数据与说明](https://github.com/xiaowu0162/LongMemEval)

它可作为通用回归的补充，但对 Loom 还必须增加分支/环境冲突、工具证据、同源重复提取、Skill 依赖撤销、人工纠正和索引故障恢复。具体样本与上线门槛写入实施计划。本文不使用不同论文自报分数直接给引擎排名，也不把模型自身判分当成唯一正确性依据。
