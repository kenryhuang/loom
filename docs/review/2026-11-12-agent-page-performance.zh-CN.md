# Agent 页面查询性能修复

## 瓶颈与修改

Session 存储已按字段拆分，但 Agent Goals 的运行统计、Work 的执行状态和 Learning 的待回答问题
仍通过 `sessions.body` 兼容视图读取。该视图会组装完整 context、run 和消息历史，
即使外层 SQL 只取几个 JSON 字段，也要反复构造和解析大型快照。

- Goals：直接关联 `session_heads` 和运行时间、counters 字段，保留 token 解码逻辑。
- Work：按需读取少量运行和控制字段；context/checkpoint 只检查是否存在，不读取其内容。
- Learning：直接查询 pending input_request，附带任务 revision 和 objective。
- 保留 Agent 范围、已删除 Session 过滤、待回答问题、执行状态和恢复控制展示。

没有改动数据库结构、任务调度、缓存有效性或页面轮询频率。

## 测量

对本地 `agent_e2b0db524a714257883cd8512994dbb1` 的同一份数据库进行只读测量。
修复前实际 HTTP 测量为 Goals 2.15 秒、Work 7.74 秒、Learning 0.98 秒；这些请求并发执行，包含服务竞争和 HTTP 开销。

另用独立进程直接调用页面查询函数，保持相同数据库和读取路径，排除 HTTP 开销：

| 页面 | 修改前 | 修改后 |
| --- | ---: | ---: |
| Overview | 0.046 秒 | 0.052 秒 |
| Goals | 0.799 秒 | 0.042 秒 |
| Work | 4.608 秒 | 0.139 秒 |
| Learning | 0.527 秒 | 0.029 秒 |

Work 仍返回 24 条运行状态。上述为单次本机测量，不代表延迟分位数或重启后的 HTTP 实测。

## 验证

- 后端页面、运行统计、执行展示、Session 存储回归：34 项通过。
- Agent 前端交互回归：45 项通过。
- 回归用例在带有大型 context/checkpoint 的 Session 上禁止读取兼容快照和消息表，
  同时检查运行时间、token、checkpoint 可用性和待回答问题仍正确展示。
- 修改文件静态检查通过；`execution.py` 保留既有长行，检查时仅排除 E501。`git diff --check` 通过。

准备加载修复时发现两个 Session 正在执行，服务重启时机待确认；本次测量未中断它们。
