# Conversation 过程展示重构方案

2026-10-06。此文记录实现对照后的设计方案；当前落地情况见文末。

## 目标

让读者不展开 JSON 就能回答：现在在做什么、刚完成了什么、得到了什么、是否需要我介入。
原始事件保持完整、可定位；主视图围绕用户消息、执行动作和结果组织。

## 对照依据

本次核对的是本地项目当前源代码，没有将旧版宣传截图当作当前页面效果。

| 方面 | Loom 当前实现 | DeepTutor 当前实现 | Loom 改造决定 |
| --- | --- | --- | --- |
| 一级信息 | `Tool call (tool_id) · argument · Done`；模型回复截取第一句 | 动作动词 + 对象 + 状态点，长对象截断 | 一级显示有意义的动作和结果摘要 |
| 信息组织 | 按 event descriptor 聚合部分生命周期，详情仍面向 payload | 按 call_id 归并，再选择有内容的动作、步骤 | 独立的 Activity projection，与 DOM 和原始事件分离 |
| 工具详情 | 每行重复 Formatted / Raw、Tool / Input / Output；嵌套对象逐字段展开 | 一组调用/结果共享 label/value 网格，结果用 Markdown | 按工具类别渲染重点；原始数据移到检查器 |
| 模型过程 | Content/Reasoning 中仍可能直接显示序列化的 action JSON | 过程文字、工具动作和最终答案有不同位置 | 解码已知回复协议；操作协议不作为普通文字展示 |
| 折叠 | 多层 details，展开全部会展开大量技术细节 | 工作中展开、结束后折叠，手动选择优先 | 主过程 + 动作详情两层；手动选择保持 |
| 阅读连续性 | `record()` 更新可能重建详情子树；Raw 标签、局部位置可能重置 | 用户展开状态、跟随滚动与停止跟随有独立状态 | 按稳定 ID 局部更新，保留选择、焦点、阅读位置 |
| 最终答案 | Result 折叠块 | 答案是主内容，过程退到一行 | 答案默认可见，过程作为其上方附属信息 |

代码入口：

- Loom：`src/loom/web/assets/renderers.mjs` 的 `builtinRenderers` / `modelSummary`。
- Loom：`src/loom/web/assets/views/feed.mjs` 的 `buildTabbedBody`、`formatLlmPayload`、`record`、`detail`、`layout`。
- DeepTutor：`web/features/chat/trace/TracePresentation.tsx` 的 `describeToolCall`、`ToolExchangeDetail`、`TraceRowItem`、`AssistantActivity`。
- DeepTutor：`web/components/activity/ActivityRow.tsx`、`ActivityDetailGrid.tsx`。
- DeepTutor：`web/features/chat/trace/selectors.ts`、`web/features/chat/messages/ChatMessageList.tsx`。

注意：DeepTutor 也有通用对象的 JSON fallback，并非所有数据都经过专用渲染。
Loom 需要借鉴其信息层级，再适配持久 session、多 run、暂停恢复、并发工具和历史分页。
不能照搬其中按相邻 tool_call/tool_result 配对的办法；Loom 必须依据身份和作用域关联。

## 页面结构

```text
你
检查服务启动失败的原因，并修复。

● 正在验证修改   pytest tests/service/…                 ▾
│ ✓ 检索代码     找到 6 处相关定义                       ▸
│ ✓ 读取文件     controller.py、api.py 等 3 个文件         ▸
│ ✓ 修改文件     api.py  ·  +18 / −6                     ▸
│ ! 执行命令     首次检查失败：缺少配置项                  ▸
│ ✓ 更新配置     config.yaml                             ▸
│ ● 运行测试     tests/service/…                         ▸

已修复配置加载时的问题。……
[修改文件] [相关输出]
```

以上文字和数量是展示示例。实际标题、结果和统计只能来自已记录的数据。
没有结构化测试结果时，显示“命令退出码 0”，不编造“测试通过”。

执行结束且用户未手动保持展开时：

```text
✓ 执行过程 · 9 项操作 · 2 分 45 秒                      ▸

已修复……                         ← 正文默认展开
```

这里的“执行结束”不等于独立验证任务成功；暂停、终止、失败分别标识。

## 三个阅读层级

### 1. 默认：活动摘要

每个动作一行：状态点、动作名称、具体对象、简短结果。常态不显示 event type、call ID、revision、JSON 字段树。

运行中固定的 process header 显示**当前最新可见动作**，不固定累积失败或错误计数。
正常完成的历史动作以紧凑行保留；失败留在实际发生的位置。新动作到来后 header 更新到最新动作。
完成后的 process 可以显示动作总数和耗时，不显示 event 总数。
若历史只加载一部分，明确显示“已加载 N 项”，不能拿局部计数冒充总数。

### 2. 展开：动作相关详情

| 类别 | 一级摘要 | 展开后重点 |
| --- | --- | --- |
| 搜索代码/文件 | 搜索词、范围、命中数 | `path:line` 命中列表与片段；无匹配单独标识 |
| 读取文件 | 相对路径、读取范围 | 带行号的文本预览；完整内容按需读取 |
| 写入/编辑 | 文件名与有证据的变更量 | diff、创建内容或修改范围；无基线时不生成伪 diff |
| shell/测试 | 实际命令或可可靠识别的用途、退出状态 | command、必要的 cwd、输出摘要、stderr；长日志在检查器中分页 |
| 网络获取 | 站点、页面标题或请求对象 | 来源链接、已记录摘要和有效正文；HTML 不直接执行 |
| 计划调整 | 最新计划及当前项 | 一个持续更新的 checklist，修改说明按需看；不刷每次 revision |
| 模型过程 | 已记录的行动说明或短文字 | 可读说明；回复协议解码后展示内容；不逐轮摆出请求 messages、tools schema 和 usage |
| 出错/等待 | 发生了什么、是否可继续/需要输入 | 具体原因、关联动作、已记录的恢复状态；可操作项来自 service 支持的命令 |
| 未知插件工具 | 可读名称、对象、真实状态 | 少量关键字段 + 有限结果预览；原始记录始终可访问 |

路径优先相对 workspace 展示。与 DeepTutor 的通用隐藏规则不同，Loom 不能普遍隐藏 cwd：
当前目录不同于 session workspace、跨工作区执行或故障定位需要时，必须显示。

### 3. 按需：原始记录检查器

每条动作只有一个低权重“查看原始记录”入口，打开侧边抽屉；窄屏用独立对话框。
这里提供完整输入/输出、原始事件、错误对象、ID、时间和复制操作，以及跳转对应 Trajectory 的入口。
移除每个动作内重复的 Formatted / Raw tab。打开检查器不会挤出第四个固定页面栏，也不改变 Session info 的数据更新。

代码、日志、用户要求输出的 JSON 仍按原样展示；从主视图移走的是运行协议与调试字段。

## 合并、折叠与滚动

1. request/start/delta/completion 归到同一个动作，状态原位更新；provider chunk 不生成新行。
2. 模型提出的 tool action 不表示工具已执行；直到对应执行事件出现才呈现执行状态。
3. 同类连续读取、检索可以合并成“读取 3 个文件”，展开看到各项。遇到失败、等待、写入、run 边界立即停止合并。
4. 批量合并不改变源事件顺序；并行工具保持独立状态，不能按“最近一条”配对。
5. 用户手动展开/收起优先于自动行为；新 delta、恢复历史、切换标签不会重置。
6. 过程区域保持有上限的滚动高度，header 固定。动作详情默认使用短预览，避免一行套一个滚动盒。
7. 读者上滚时停止追随；显示“有新进展/回到最新”，不抢滚动位置或焦点。
8. 未回答的问题在过程外保持可见，并关联输入框；回答后回归该轮历史。恢复不会制造一条全新的无关任务。
9. 每个历史用户轮次保留完整的过程入口，不能只给最后一轮 process。
10. task/run/budget 等常规状态继续只更新 Session info。最终答复独立展示，不重复塞进过程。

Loom 的最终答复边界应依据 message/report/finish 等真实协议和执行状态确定。
DeepTutor 的尾部文字先当答案、后续工具出现再降回过程，不能直接套到 Loom 的 JSON action 流上。
对尚未确认用途的流式内容使用稳定的说明区域，避免把半截 JSON 或暂时说明冒充最终答复。

## 实现分层

```text
持久 events / snapshot / artifact
              ↓
ActivityProjection：归并生命周期、顺序、恢复、状态
              ↓
ActivityPresenter：动作名称、对象、摘要、详情模型
              ↓
ConversationTurn → ProcessHeader + ActivityList + AssistantAnswer
                                            ↘ 原始记录检查器
```

沿用现有原生 JS、SSE 和持久存储。拆分当前集中在 feed.mjs 的职责：

- `activity/projection.mjs`：纯函数/reducer；实时与历史共用；处理去重、部分记录和身份关联。
- `activity/content.mjs`：已知回复协议、typed output envelope、JSON 字符串的有限解包；类型不匹配时不猜。
- `activity/presenters.mjs`：插件式 presenter 注册表，按工具能力匹配，不把所有工具塞进一个巨大的 switch。
- `views/activity-row.mjs`：稳定 DOM 的动作行和详情；只更新变化节点。
- `views/activity-inspector.mjs`：按需加载原始记录/长日志。
- `views/feed.mjs`：只负责编排用户消息、执行片段、答复和提问。

建议投影字段：

```text
Activity {
  id, task_id?, turn_id?, process_id, run_id,
  kind, state, title, subject, summary,
  started_at?, ended_at?, source_event_refs[],
  detail_kind, detail_ref?, children[], partial
}
```

Activity 与原始事件分开；不改写已有 trace，不运行 LLM 来给每个事件生成一句话。
标题先使用明确的行动描述与工具参数，结果摘要由领域规则提取。
缺少依据就显示朴素动作名，不能声称“发现根因”“已修复”“全部验证通过”。

服务侧按需补充可版本化的 activity summary、总动作计数和稳定 turn/process 关联，历史和 SSE 使用同一套规则。
原始 history API 与事件完整保留。若当前协议不足以确定 turn 归属，先记录待确认边界，不能靠相邻 DOM 猜配。
Call ID 不足以全局唯一：使用 session/run/trace/loop 等已有作用域；跨暂停恢复依据明确 lineage 或唯一稳定身份关联。

加载 artifact 后必须重算同一条 Activity 的标题与摘要，并更新详情，不只填充 Raw。
在详情尚未读取时明确标识“结果待加载”；不误报“没有输出”。大内容、完整 request 和 schema 不随每个 delta 反复解析。

## Conversation 与 Trajectory 分工

- Conversation：任务协作、当前进展、行动结果、提问、最终答复。
- Trajectory：模型轮次、上下文、token、证据链、深度评估及改进假设。
- 原始记录检查器：技术排查，能从上述两处定位同一份证据。

Conversation 不混入评估任务的事件，也不把 v2 judge 的事后结论冒充执行当时的发现。

## 落地顺序

1. **投影和内容解码**：先用真实历史 trace 验证关联、去重、回复 JSON 解包；补充当前工具与数据形态的适配清单。
2. **主视图重排**：用户消息 → 一个过程入口 → 默认可见答复；紧凑动作行、工具专用详情和原始检查器一起交付。
3. **状态与长会话**：固定最新动作、手动展开优先、稳定滚动、按需 artifact、分段历史和暂停恢复。
4. **真实轨迹验收**：同时比较实时执行、刷新重放、旧任务历史、暂停恢复、报错后的继续执行。

这四步组成同一次替换，不把新旧两套“Formatted/Raw 卡片 + Activity 行”叠在一起。

## 验收标准

- 收起状态下能读懂正在做什么；展开过程后扫视几行能知道主要行动及结果，不必打开 JSON。
- 默认没有 llm.requested、revision、调用 ID、完整 request messages 等运行字段。
- 同一次调用只有一条活动记录，流式/重放得到同样的动作顺序和终态。
- 已回答文本只展示一次；失败和未知副作用不会因去重、折叠或历史分页消失。
- 用户阅读详情时不因新 delta 切回默认视图、丢失选区、跳到底部或抢焦点。
- 每个历史轮次能看到过程；不使用“当前窗口内条数”伪装完整活动计数。
- 命令成功、测试摘要、业务目标完成分开措辞；缺失或截断内容有明确提示。
- 任意摘要都能定位源事件；未知工具有可读降级，不产生空白卡片。
- 分页与按需详情约束 DOM/内存规模；长轨迹不对每个 delta 重建整个详情树。
- 使用键盘可展开/收起和关闭检查器；状态不只依赖颜色；Markdown/HTML 不执行不可信脚本。

建议以“命令失败→定位→修改→再次执行”的真实会话，以及 yakDB 的长轨迹，作为逐屏对照验收样例。


## 本次落地

已在 `feat/persistent-service-sessions` 实现主视图替换：

- `activity/content.mjs`、`projection.mjs`、`presenters.mjs` 分离协议解码、生命周期归并与动作展示；工具按 process/trace/call 身份关联。
- `views/activity-row.mjs` 提供稳定摘要和按需详情；覆盖 Observation 包装的命令、文件、diff、搜索、网页、计划，以及未知工具降级。
- `views/activity-inspector.mjs` 提供单一原始记录对话框、复制、分段展示、source artifact 和当前 session 的 Trajectory 入口。
- Conversation 按用户消息、动作列表、默认可见答案编排；最新动作 header、连续读取/搜索分组、一个持续更新的计划 checklist。已确认成功的计划调用归入 checklist 的原始记录；失败和结果未加载的调用保留独立行。
- 保留历史 process 加载和暂停恢复；手动折叠优先；上滚停止跟随；正文选区、未变化的详情节点和控制焦点不随流式更新重建。
- 状态更新仍进入 Session info。移除每行 Formatted/Raw 标签及对应旧样式；最终答案不受 Fold all 影响。

验证包含 Web 自动化回归、真实服务连接、真实 17,472 条事件轨迹的只读重放，以及 Chromium 桌面和窄屏截图检查。不运行额外模型调用。

边界：摘要来自明确参数与结果，不做模型推断；未知插件工具使用有限字段预览和原始记录。当前仍沿用已有 process 边界及分页协议，没有引入后端 activity 索引；未确认完整历史时显示 loaded actions，不冒充总数。Trajectory 入口导航到当前 session 分析页，尚不按动作定位具体轮次。


### 摘要与工具调用的后续调整

思考行优先展示已记录的 `summary`，其次展示 action 的 `description` 或待调用工具名称；缺失时使用简短状态文字，不再把推理正文硬截断成摘要。完整正文仍在展开详情中。DeepTutor 本身对思考正文使用折叠预览，并不额外生成模型摘要；这里没有引入额外 LLM 调用。

所有实际工具调用增加 `Tool · 工具名` 标识。成功的计划调用也恢复独立行和自己的原始记录入口，当前 checklist 继续独立更新；连续读取/检索仍可成组展开查看。
