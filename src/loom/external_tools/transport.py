"""Bounded MCP clients and CLI processes. Registration never executes a CLI tool."""

import asyncio
import json
import os
import re
import signal
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from loom.service.contracts import ServiceError
from loom.tasks.config import _env_value


def environment(config, field="env"):
    """Resolve only declared variables, using the same .env fallback as model keys."""
    values = {}
    for key, variable in config.get(field, {}).items():
        value = _env_value(variable, None)
        if not value:
            raise ServiceError(f"Environment variable {variable} is not set", 422)
        values[key] = value
    return values


@asynccontextmanager
async def mcp_session(config):
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise ServiceError('Install Loom external tools support: uv pip install "loom[external-tools]"', 422) from exc
    async with AsyncExitStack() as stack:
        if config["transport"] == "stdio":
            params = StdioServerParameters(command=config["command"], args=config.get("args", []), env=environment(config), cwd=config.get("cwd"))
            errors = stack.enter_context(open(os.devnull, "w"))  # noqa: SIM115 — ExitStack owns this handle
            reader, writer = await stack.enter_async_context(stdio_client(params, errlog=errors))
        else:
            headers = environment(config, "headers_env")
            import httpx

            def http_client_factory(headers=None, timeout=None, auth=None):
                return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, trust_env=config.get("trust_env", False), follow_redirects=False)

            if config["transport"] == "sse":
                reader, writer = await stack.enter_async_context(
                    sse_client(
                        config["url"],
                        headers=headers,
                        timeout=config["timeout_seconds"],
                        sse_read_timeout=config["timeout_seconds"],
                        httpx_client_factory=http_client_factory,
                    )
                )
            else:
                client = await stack.enter_async_context(http_client_factory(headers=headers, timeout=config["timeout_seconds"]))
                reader, writer, _ = await stack.enter_async_context(streamable_http_client(config["url"], http_client=client))
        session = await stack.enter_async_context(ClientSession(reader, writer))
        await session.initialize()
        yield session


async def discover(config):
    async with asyncio.timeout(config["timeout_seconds"]):
        async with mcp_session(config) as session:
            tools, cursor, seen = [], None, set()
            while True:
                result = await session.list_tools(cursor=cursor)
                tools.extend(t.model_dump(by_alias=True, exclude_none=True) for t in result.tools)
                if len(tools) > 200:
                    raise ServiceError("A connection may expose at most 200 tools", 422)
                cursor = result.nextCursor
                if not cursor:
                    return tools
                if cursor in seen:
                    raise ServiceError("MCP returned a repeated tools cursor", 502)
                seen.add(cursor)


async def call_mcp(config, name, arguments):
    async with asyncio.timeout(config["timeout_seconds"]):
        async with mcp_session(config) as session:
            result = await session.call_tool(name, arguments=arguments)
            value = result.model_dump(by_alias=True, exclude_none=True)
            raw = json.dumps(value, ensure_ascii=False)
            if len(raw) > config["max_output_chars"]:
                value = {"isError": result.isError, "text": raw[: config["max_output_chars"]], "truncated": True}
            return value, bool(result.isError)


async def call_cli(config, arguments, workspace=None, options=None):
    # Only whole arguments can be substituted: values never become shell syntax or the executable.
    args = []
    for arg in config.get("args", []):
        match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", arg)
        if match:
            if match[1] not in arguments:
                raise ServiceError(f"Missing CLI argument: {match[1]}", 422)
            value = arguments[match[1]]
            args.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        else:
            args.append(arg)
    proc = await asyncio.create_subprocess_exec(
        config["command"],
        *args,
        cwd=config.get("cwd") or workspace,
        env={**os.environ, **environment(config)},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    limit = config["max_output_chars"]

    async def read(stream):
        chunks, size, total = [], 0, 0
        while block := await stream.read(8192):
            total += len(block)
            if size < limit:
                chunks.append(block[: limit - size])
                size += len(chunks[-1])
        return b"".join(chunks).decode("utf-8", errors="replace"), total > limit

    async def write():
        try:
            if config.get("input_mode", "json_stdin") == "json_stdin":
                proc.stdin.write(json.dumps(arguments, ensure_ascii=False).encode() + b"\n")
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    tasks = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr)), asyncio.create_task(write())]
    try:
        if (options or {}).get("process_started"):
            options["process_started"](proc.pid)
        async with asyncio.timeout(config["timeout_seconds"]):
            out, error, _ = await asyncio.gather(*tasks)
            await proc.wait()
        return {"stdout": out[0], "stderr": error[0], "exit_code": proc.returncode, "truncated": out[1] or error[1]}, proc.returncode != 0
    finally:
        # Also reap descendants when the direct child has already exited.
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def with_control(operation, options=None):
    """Observe service pause/stop requests while awaiting an external call."""
    task = asyncio.create_task(operation)
    try:
        while not task.done():
            check = (options or {}).get("cancel_check")
            if check and check():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise ServiceError("External tool interrupted by task control", 409, "EXTERNAL_TOOL_CANCELLED")
            await asyncio.wait({task}, timeout=0.05)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
