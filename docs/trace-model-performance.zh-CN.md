# Trace analysis：模型性能统计

## 指标口径

| 指标 | 计算方式 | 单位 |
| --- | --- | --- |
| Duration | 客户端调用开始到响应完成，包含同次调用内的 tool-choice 降级重试 | ms，界面自动转换 |
| TTFT | 调用开始到首个非空输出；包括正文、reasoning、工具名称或工具参数，不计空片段、连接建立和 usage 消息 | ms，界面自动转换 |
| TPS | provider 报告的 completion tokens ÷ `(Duration − TTFT)` 秒数 | tokens/s |

这是客户端观测值，包含网络传输和本地流处理，不代表 provider 内部的纯解码速度。TPS 的分子采用 provider 报告的输出 token 数，不用字符数或流片段数估算。

新调用在 `request_llm_response` 使用单调时钟计时，将 `duration_ms`、`ttft_ms` 写入 `LlmResponse`，随 `llm.completed.response` 保存。非流式请求只测 Duration，TTFT/TPS 留空。

历史 trace 优先使用已记录计时，否则用 request、首个有效流片段和完成事件的时间戳计算。Session analysis 每次调用只保留首个有效片段，避免加载完整 token 流；按 run、loop、trace、step 和 call ID 隔离。

缺少 usage、无法识别首输出、时钟倒退或生成区间为零时不生成 TPS。失败或未完成的调用可以展示已有 TTFT，但不生成完成响应的 TPS。

## 聚合与页面

Model & token cost 只显示两个表，每行一个模型，遵循当前 run 筛选：

1. 性能：Calls、TTFT avg/P95、TPS avg/P95、Duration avg/P95。
2. Token：输入 total/max、输出 total/max。

平均值为有测量值的调用的算术平均；P95 使用 nearest-rank（排序后第 `ceil(n × .95)` 项）。缺失值不作为零参与计算。Token total 只累加已报告的调用。单元格悬浮提示显示样本覆盖率与口径，缺失量统一放在 Data quality。

Call explorer 分为 Tool calls 和 Model calls 两个独立表。工具表展示工具名、执行耗时、状态；模型表展示模型、TTFT、TPS、输入/输出 token、Duration、状态。每行按钮展开详情，保留请求、响应、工具输入输出、上下文变化和验证证据入口。未关联到模型的工具也进入工具表。状态筛选作用于每条调用自身。

分析版本提升到 `session-trajectory.4.6`，刷新分析会重算新指标，不复用旧版本缓存。旧 trace 无法补测的指标显示 `—`。
