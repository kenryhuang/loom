# External tools

External tools are registered MCP servers or CLI commands. They appear beside Sessions, Evaluation / Evolve and Knowledge in the web navigation. A connection is a reusable catalog entry; a session binds individual tools from that catalog.

## Setup

Install the optional adapters in the service environment, then restart Loom:

```sh
uv pip install --python .venv/bin/python '.[external-tools]'
```

Open **External tools → Register connection**. Give the connection a name and a description that explains when it is useful. Choose MCP or CLI and enter its connection configuration. Saving a CLI registers its declared tool without running the command. For MCP, click **Discover tools** to initialize a connection and save `tools/list` descriptions and input schemas. Discovery does not call the listed tools. MCP support uses the [official Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0).

### MCP over stdio

```json
{
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/project"],
  "env": {},
  "timeout_seconds": 60
}
```

The executable and packages must be available to the service. MCP servers start a fresh connection for each discovery or invocation and close it afterward. Stateful server conversations spanning multiple calls are not currently retained.

### MCP over Streamable HTTP

```json
{
  "transport": "streamable_http",
  "url": "http://localhost:8000/mcp",
  "headers_env": {"Authorization": "MY_MCP_AUTHORIZATION"},
  "timeout_seconds": 60
}
```

`MY_MCP_AUTHORIZATION` is a service environment variable containing the complete header value, such as `Bearer …`. `env` maps child environment names to service environment-variable names. Secret values are resolved at execution and are not saved in the registry or sent to the setup model. HTTP uses a direct connection by default; `trust_env: true` enables HTTPX environment proxy settings. Legacy HTTP+SSE is supported with `transport: "sse"` and the provider's SSE URL. Interactive OAuth registration is not included. A 405 on a Streamable HTTP endpoint may indicate a service that still needs a provider-side protocol upgrade; select SSE explicitly for such a service.

Credential lookup follows model configuration: a nonempty process environment variable takes precedence over the service working directory's `.env`; `LOOM_ENV_FILE` selects an alternative file. Discovery and invocation both resolve credentials at call time. Only variables explicitly referenced by `env` / `headers_env` are resolved; the entire `.env` is not exported to child processes. For example, store `BAILIAN_MCP_AUTHORIZATION="Bearer sk-your-key"` in `.env` and reference it via `"headers_env": {"Authorization": "BAILIAN_MCP_AUTHORIZATION"}`. Write the complete value: the current `.env` parser does not expand `${OTHER_VARIABLE}` references.

### CLI with typed arguments

```json
{
  "command": "python3",
  "args": ["/path/to/search_docs.py", "--query", "{query}"],
  "input_mode": "arguments",
  "input_schema": {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": false
  },
  "effect_kind": "read_only",
  "timeout_seconds": 60,
  "max_output_chars": 32000
}
```

A whole argument such as `{query}` is replaced by the matching input value. The executable is fixed, and arguments are passed directly without shell interpolation. `input_mode: "json_stdin"` instead writes the full input JSON to stdin; fixed arguments and placeholders remain available. Set `cwd` to an existing directory, or use the bound session workspace. Commands execute with the service user's OS permissions. Read-only metadata is descriptive, not an OS sandbox.

CLI output includes stdout, stderr, exit code and a truncation flag. Nonzero exit codes and MCP `isError` responses produce failed tool calls. Timeouts are configurable from 1–300 seconds. CLI output streams are drained with bounded retained buffers; timeout/cancellation reaps the process group. Remote cancellation cannot undo side effects already performed by a server.

## Session assembly and execution

In **New session → External tools**, choose individual tools. **Recommend with LLM** can choose from enabled, discovered tools using their descriptions; review or change the recommendation before creating the session. The setup model has no execution tools and cannot register or invoke a connection.

The session's **Configure external tools → Apply tools** action changes bindings when paused, idle or completed. Applying the same selection refreshes its registered definitions. Registration alone does not attach tools to existing sessions. Disabled connections cannot be selected or called. Tool definitions and configuration fingerprints are pinned to the session: after changing a connection or discovered schema, explicitly rebind its tools before using the new definition.

While paused, you can also update Knowledge bases. Saving does not start execution. **Resume** rebuilds the tool assembly while preserving the run, plan, completed operations, evidence, history and accumulated usage. Pending calls selected using the old tools are discarded; the model chooses its next action using the new definitions, and prior acceptance results require revalidation. Resume without binding changes retains normal checkpoint behavior. Wait for the worker to exit and resolve pending input/recovery first. Tools explicitly referenced by saved workflow nodes cannot be detached. These controls do not change the model, workflow policy or other plugin configuration.

```text
Registry → selected tool IDs → pinned definitions → TaskAssembly tools/external_tools
                                                     ↓
Task model → normal tool invocation → MCP / CLI adapter → Observation → next model call
                                                     ↓
                                       events + execution journal + result artifact
```

The model decides whether to invoke an attached tool based on its description and input schema. The existing execution runtime handles journaling, checkpoints and errors. Side-effecting tools participate in acceptance invalidation. Successful results are retained as `external_tool_result` artifacts; they are not automatically asserted to be verified research sources. The registry does not change Base/Deep semantics or count external server model usage as solver token usage.

## API and storage

All endpoints use the service bearer authentication:

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/v1/external-tools` | Connections and available tools |
| POST | `/v1/external-tools` | Register `{name, description, kind, config, enabled?}` |
| GET | `/v1/external-tools/{id}` | Connection definition and discovered tools |
| POST | `/v1/external-tools/{id}` | Replace configuration or enable/disable |
| POST | `/v1/external-tools/{id}/discover` | Discover MCP tools; body `{}` |

Session creation accepts `external_tool_ids`. Existing sessions use the revision-checked `set_external_tools` command with `{external_tool_ids: [...]}`. The `external_tools` collection is added/removed automatically from the task specification according to these IDs. Tool aliases are unique across connections, even when two MCP servers expose the same original tool name.

Configuration lives in `<service-data-dir>/external_tools/external_tools.sqlite`. Disabled entries retain their history; there is no destructive deletion action. A session binds at most 64 tools, and discovery accepts at most 200 tools per connection. Only local JSON Schema references are accepted.
