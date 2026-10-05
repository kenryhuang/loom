# 工具执行与进度复核

`shell` 工具集合提供两个独立工具，避免模型在 shell 语法和 argv 参数之间混用。

| 工具 | 参数 | 执行语义 |
| --- | --- | --- |
| `shell_execute` | `command: string` | 执行 shell 脚本，支持 `&&`、管道、重定向、变量展开 |
| `process_execute` | `argv: string[]` | 直接执行程序，每个元素是一个原样参数，不做 shell 解析 |

两者都支持工作区内的 `cwd` 和正整数 `timeout_seconds`。例如：

```json
{"command":"git fetch && git status --short","cwd":"."}
```

```json
{"argv":["git","log","-5","--format=%h %s"],"cwd":"."}
```

`shell_execute.command` 不再接受数组；之前的 argv 调用应迁移到
`process_execute.argv`。把数组 JSON 编码后塞进 `command` 也会被拒绝，并给出修正示例。
显式 `allowed_tools` 白名单需要包含 `process_execute` 才会向模型开放该工具。

Native runtime 默认使用 `/bin/bash -o pipefail -c`，不加载 login shell 的启动配置。
shell 由执行 runtime 配置决定，工具接口不依赖具体后端。可以在 task spec 中设置：

```yaml
execution_runtime:
  plugin: native_os
  workspace_resource: project
  # argv 前缀，运行时把脚本作为最后一个参数追加。
  shell: [/bin/bash, -o, pipefail, -c]
```

两个工具共用异步执行、输出截断、超时与取消清理、进程登记逻辑；副作用发生前先登记进程。
执行结果保留 `exit_code`、`stdout`、`stderr`，增加 `ok` 和 `status`：
`succeeded`、`nonzero_exit`、`launch_failed`、`timed_out`、`no_match`。
参数错误返回 `VALIDATION_FAILED` 与 `error_kind: invalid_input`。
直接调用 `grep`/`rg` 时，退出码 1 表示无匹配，记为 `no_match`；退出码 2 等仍是失败。
任意 shell 脚本的非零退出码按失败处理，不能根据脚本中的某一个搜索命令推断整个脚本成功。

## 自动模式的进度复核

首次路由继续决定直接执行还是进入 Plan。进入 ReAct 后，复核关注当前请求的达成情况，
提示包含目标、最近 12 次操作、结果摘要，以及失败或重复操作的触发原因。

默认触发条件：连续失败 2 次、上次决策后调用工具 12 次，或最近 12 次操作中
相同操作产出相同证据至少 3 次。比较重复证据时忽略执行耗时。
选择继续后有 6 次工具调用的冷却期。默认不再在第 3 次复核后关闭检查，
执行仍受 session 的时间、模型调用次数、token 等预算约束。

模型在复核时选择一个动作：

- `finish`：目标已达到，提交最终结果，立即终止当前执行及同批尚未执行的调用。
- `continue_react`：说明 `reason`、具体 `evidence_gap` 和必要的 `next_action`，继续取证。
- `enter_plan`：依赖关系、失败或新增信息需要调整工作流时，进入显式规划。

复核不能绕过来源验证、输出证据约束或未完成的 Plan。缺工具或无法读取目标来源，
仍不能被标记为成功。它检查已有证据并指导模型收敛，不是独立的目标完成证明器。

可在 legacy workflow 插件中调整阈值；`mode: off` 不启用自动复核，
Plan 执行仍通过检查清单更新进度：

```yaml
workflow:
  plugin: legacy_planning
  mode: auto
  progress_review:
    failure_threshold: 2
    tool_call_threshold: 12
    cooldown_calls: 6
    max_reviews: null
```

保持现有 `workflow.routing.requested`、`workflow.route.selected` 事件名兼容消费者。
成功 `finish` 后直接执行的 route 进入 `completed`。进度摘要与 route 一起保存到检查点；
恢复时刷新当前工具 schema，不会把旧 argv 当成 shell 脚本执行。
旧检查点如果已提交成功 `finish`，恢复直接返回已提交结果，不再请求模型。
