# 外部工具

External tools 与 Sessions、Evaluation / Evolve、Knowledge 并列，管理可复用的 MCP server 和 CLI 命令。注册中心保存连接；session 选择其中的具体工具，TaskAssembly 将它们装入 task loop。

## 安装和注册

```sh
uv pip install --python .venv/bin/python '.[external-tools]'
```

安装后重启服务。在 **External tools → Register connection** 填写名称、用途描述、类型和 JSON 配置。CLI 保存时只注册定义，不执行命令；MCP 保存后点击 **Discover tools**，通过初始化和 `tools/list` 获取工具描述与参数 schema，不调用业务工具。

支持 MCP stdio、MCP Streamable HTTP、旧版 HTTP+SSE，以及直接启动进程的 CLI。配置示例见 [英文文档](external-tools.md)。MCP 使用 [官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0)。

- MCP stdio：配置 `command`、`args`，可选 `cwd` 和 `env`。
- MCP HTTP：配置 `transport: "streamable_http"`、`url`，可选 `headers_env`。后者映射到服务环境变量，例如 `{"Authorization":"MY_MCP_AUTHORIZATION"}`；环境变量值包含完整的 `Bearer …`。
- CLI：固定可执行文件，声明 `input_schema`；`args` 支持整项 `{query}` 占位符。`input_mode: "json_stdin"` 将完整参数 JSON 写入标准输入，`arguments` 使用参数数组。参数直接传给进程，不做 shell 字符串插值。

`env` 的值也是服务环境变量名，不是密钥本身。配置中不保存解析后的密钥，推荐模型不会收到连接命令、环境变量值或 HTTP 认证信息。HTTP 默认直连；`trust_env: true` 可启用 HTTPX 环境代理配置。旧版 HTTP+SSE 可使用 `transport: "sse"` 和服务商提供的 SSE 地址接入。当前不支持交互式 OAuth 和跨调用保留 MCP 会话。Streamable HTTP 返回 405 时，应检查服务端协议；尚未升级的服务可显式选择 SSE。

凭据与模型配置采用相同读取规则：优先使用非空的进程环境变量，其次读取服务启动目录的 `.env`；可用 `LOOM_ENV_FILE` 指定其他文件。仅解析 `env` / `headers_env` 显式引用的变量，不会将整个 `.env` 注入子进程。发现工具和调用工具时都会读取配置。例如 `.env` 中写 `BAILIAN_MCP_AUTHORIZATION="Bearer sk-your-key"`，页面配置 `"headers_env": {"Authorization": "BAILIAN_MCP_AUTHORIZATION"}` 即可。值需填写完整内容，当前 `.env` 解析不展开 `${OTHER_VARIABLE}`。

## 选择和执行

新建 session 时可以手工勾选具体工具；**Recommend with LLM** 根据任务、工具名称和描述推荐，创建前可以调整。推荐阶段不能执行或注册工具。注册连接不会自动给已有 session 增加工具。

已有 session 在 paused、idle 或 completed 状态下，通过 **Configure external tools → Apply tools** 调整挂载。同样的工具保持勾选并再次 Apply，也会更新到注册中心的最新定义。停用连接后不能新选或调用。session 固定所选工具的定义和配置指纹；注册配置或工具 schema 改变后，需要重新绑定才能使用新版本。

暂停后也可以更新 Knowledge bases，保存不会自动开始执行。点击 **Resume** 后，Loom 重新组装工具组件，保留同一个 run 的计划、已完成操作、证据、历史和累计用量；尚未执行的旧工具调用会作废，由模型根据新定义重新决定下一步，已有验收结果重新验证。普通 Resume 不修改组件时仍直接恢复 checkpoint。更新前必须等待 worker 退出并解决待处理的输入或恢复问题；已保存工作流节点显式引用的工具不能直接移除。此入口不修改模型、工作流策略或其他插件配置。

执行链路：注册中心 → 选中的工具 ID → 固定定义 → `TaskAssembly / tools / external_tools` → 模型选择调用 → MCP/CLI 适配器 → Observation → 下一轮模型输入。调用复用原有执行日志、trace、checkpoint 和错误处理；有副作用的工具会参与验收失效机制。

成功返回保存为 `external_tool_result` artifact，不自动认定为已验证的研究来源。MCP 返回 `isError` 或 CLI 非零退出码都会产生失败调用。超时可配置为 1–300 秒；CLI 输出限量保留，取消/超时清理进程组。命令使用服务进程的系统权限，`read_only` 是能力描述，不是操作系统沙箱；远端取消不能撤销已经发生的副作用。

## API

接口使用现有 bearer 认证：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST | `/v1/external-tools` | 列表 / 注册 |
| GET / POST | `/v1/external-tools/{id}` | 查看 / 修改、启停 |
| POST | `/v1/external-tools/{id}/discover` | 发现工具，请求体 `{}` |

创建 session 传 `external_tool_ids`；修改使用带 revision 检查的 `set_external_tools` 命令。task spec 中的 `external_tools` 集合根据绑定自动增删。不同连接的同名工具使用不同别名。

注册数据保存在 `<service-data-dir>/external_tools/external_tools.sqlite`。每个 session 最多选择 64 个工具，每个连接最多发现 200 个工具；JSON Schema 引用限于本地引用。停用保留历史，当前不提供破坏性删除操作。
