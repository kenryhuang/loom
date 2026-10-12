import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from loom.core import thaw_json
from loom.external_tools.store import ExternalToolStore, normalize
from loom.external_tools.transport import call_cli, call_mcp, environment, with_control
from loom.service.contracts import ServiceError
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskRequest

pytest.importorskip("jsonschema")


@pytest.mark.parametrize("field", ["env", "headers_env"])
def test_external_credentials_follow_model_dotenv_resolution(tmp_path, monkeypatch, field):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOOM_ENV_FILE", raising=False)
    monkeypatch.delenv("LOOM_TEST_MCP_AUTH", raising=False)
    (tmp_path / ".env").write_text('LOOM_TEST_MCP_AUTH="Bearer file-key"\nUNRELATED_SECRET=private\n')
    config = {field: {"Authorization": "LOOM_TEST_MCP_AUTH"}}
    assert environment(config, field) == {"Authorization": "Bearer file-key"}
    assert "LOOM_TEST_MCP_AUTH" not in os.environ
    alternate = tmp_path / "custom.env"
    alternate.write_text('LOOM_TEST_MCP_AUTH="Bearer alternate-key"\n')
    monkeypatch.setenv("LOOM_ENV_FILE", str(alternate))
    assert environment(config, field) == {"Authorization": "Bearer alternate-key"}
    monkeypatch.setenv("LOOM_TEST_MCP_AUTH", "Bearer process-key")
    assert environment(config, field) == {"Authorization": "Bearer process-key"}
    monkeypatch.delenv("LOOM_TEST_MCP_AUTH")
    alternate.unlink()
    with pytest.raises(ServiceError, match="LOOM_TEST_MCP_AUTH is not set"):
        environment(config, field)


def cli_config(**config):
    return {
        "name": "Echo",
        "description": "Echo a JSON message",
        "kind": "cli",
        "config": {
            "command": sys.executable,
            "args": ["-c", "import sys; print(sys.stdin.read())"],
            "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"], "additionalProperties": False},
            "effect_kind": "read_only",
            **config,
        },
    }


@pytest.mark.asyncio
async def test_cli_registration_binding_execution_restart_and_drift(tmp_path):
    store = ExternalToolStore(tmp_path)
    connection = store.save(cli_config())
    binding = connection["tools"][0]
    assert ExternalToolStore(tmp_path).catalog() == [binding]
    request = TaskRequest(
        "echo",
        task_spec={"tools": {"collections": ["external_tools"]}},
        metadata={"external_tool_bindings": [binding], "external_tools_directory": str(tmp_path)},
    )
    assembly = TaskAssembly(request, plan_mode="off")
    try:
        assert binding["id"] in assembly.handlers()
        result = await assembly.handlers()[binding["id"]]({"message": "hello"})
        assert result.ok, result.error
        output = thaw_json(result.value.value)
        retained = assembly.read_artifact(output["artifact"]["sha256"])
        assert json.loads(retained["result"]["stdout"]) == {"message": "hello"}
        assert output["artifact"]["kind"] == "external_tool_result"
        bad = await assembly.handlers()[binding["id"]]({"message": 3})
        assert not bad.ok
        store.save(cli_config(args=["-c", 'print("changed")']), connection["id"])
        drift = await assembly.handlers()[binding["id"]]({"message": "hello"})
        assert not drift.ok and "changed" in drift.error.message
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_cli_argument_boundaries_failures_truncation_and_timeout(tmp_path):
    script = 'import json,sys; print(json.dumps(sys.argv[1:])); print("z"*1000, file=sys.stderr); sys.exit(7)'
    config = normalize(cli_config(args=["-c", script, "{message}"], input_mode="arguments", max_output_chars=100))["config"]
    injected = "x; touch " + str(tmp_path / "should-not-exist")
    result, failed = await call_cli(config, {"message": injected})
    assert failed and result["exit_code"] == 7 and result["truncated"]
    assert not (tmp_path / "should-not-exist").exists()
    config = normalize(cli_config(args=["-c", "import time; time.sleep(10)"], timeout_seconds=1))["config"]
    with pytest.raises(TimeoutError):
        await call_cli(config, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("service_control", [False, True])
async def test_cli_cancellation_reaps_process(tmp_path, service_control):
    pidfile = tmp_path / "pid"
    script = f'import os,time; open({str(pidfile)!r}, "w").write(str(os.getpid())); time.sleep(60)'
    config = normalize(cli_config(args=["-c", script]))["config"]
    cancelled = False
    operation = call_cli(config, {})
    task = asyncio.create_task(with_control(operation, {"cancel_check": lambda: cancelled}) if service_control else operation)
    for _ in range(100):
        if pidfile.exists():
            break
        await asyncio.sleep(0.01)
    assert pidfile.exists()
    pid = int(pidfile.read_text())
    if service_control:
        cancelled = True
    else:
        task.cancel()
    with pytest.raises(ServiceError if service_control else asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_registry_validation_and_disabled_tools(tmp_path):
    store = ExternalToolStore(tmp_path)
    body = store.save(cli_config())
    with pytest.raises(ServiceError):
        store.resolve(["invented"])
    with pytest.raises(ServiceError):
        store.resolve([body["tools"][0]["id"]] * 2)
    body = store.save({**cli_config(), "enabled": False}, body["id"])
    assert not store.catalog()
    with pytest.raises(ServiceError):
        store.resolve([body["tools"][0]["id"]])
    for bad in [cli_config(env={"TOKEN": "literal secret!"}), cli_config(timeout_seconds=0), cli_config(input_schema={"type": "array"})]:
        with pytest.raises(ServiceError):
            store.save(bad)


@pytest.fixture
def mcp_server(tmp_path):
    pytest.importorskip("mcp")
    script = tmp_path / "server.py"
    script.write_text('''from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
mcp = FastMCP("fixture")
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(message: str) -> str:
    """Echo the supplied message for testing."""
    return "MCP: " + message
@mcp.tool()
def failure() -> str:
    raise ValueError("intentional failure")
mcp.run()
''')
    return {
        "name": "Fixture MCP",
        "description": "Test MCP tools",
        "kind": "mcp",
        "config": {"transport": "stdio", "command": sys.executable, "args": [str(script)], "timeout_seconds": 10},
    }


def test_mcp_stdio_discovery_and_calls(tmp_path, mcp_server):
    store = ExternalToolStore(tmp_path / "registry")
    connection = store.save(mcp_server)
    assert not connection["tools"]
    connection = store.discover(connection["id"])
    assert {t["name"] for t in connection["tools"]} == {"echo", "failure"}
    echo = next(t for t in connection["tools"] if t["name"] == "echo")
    assert echo["effect_kind"] == "read_only"

    async def run():
        result, failed = await call_mcp(connection["config"], "echo", {"message": "hello"})
        assert not failed and "MCP: hello" in json.dumps(result)
        _, failed = await call_mcp(connection["config"], "failure", {})
        assert failed

    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["streamable_http", "sse"])
def test_mcp_http_discovery_and_invocation(tmp_path, mcp_server, monkeypatch, protocol):
    import socket
    import subprocess
    import time
    from contextlib import asynccontextmanager

    import mcp.client.sse as sse_transport
    import mcp.client.streamable_http as transport

    credential_file = tmp_path / "credentials.env"
    credential_file.write_text('LOOM_TEST_MCP_AUTH="Bearer test-http-key"\n')
    monkeypatch.setenv("LOOM_ENV_FILE", str(credential_file))
    monkeypatch.delenv("LOOM_TEST_MCP_AUTH", raising=False)
    original_client = transport.streamable_http_client
    seen_headers = []

    @asynccontextmanager
    async def checked_client(url, *, http_client):
        seen_headers.append(http_client.headers.get("Authorization"))
        async with original_client(url, http_client=http_client) as streams:
            yield streams

    monkeypatch.setattr(transport, "streamable_http_client", checked_client)
    original_sse_client = sse_transport.sse_client

    @asynccontextmanager
    async def checked_sse_client(url, **kwargs):
        seen_headers.append(kwargs["headers"].get("Authorization"))
        async with original_sse_client(url, **kwargs) as streams:
            yield streams

    monkeypatch.setattr(sse_transport, "sse_client", checked_sse_client)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "http_server.py"
    source = Path(mcp_server["config"]["args"][0]).read_text()
    script.write_text(
        source.replace('FastMCP("fixture")', f'FastMCP("fixture", host="127.0.0.1", port={port})').replace(
            "mcp.run()", f'mcp.run(transport="{protocol.replace("_", "-")}")'
        )
    )
    with open(os.devnull, "w") as quiet:
        proc = subprocess.Popen([sys.executable, str(script)], stdout=quiet, stderr=quiet)
        try:
            for _ in range(200):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    assert proc.poll() is None
                    time.sleep(0.05)
            store = ExternalToolStore(tmp_path / "http-registry")
            connection = store.save(
                {
                    "name": "HTTP",
                    "description": "Remote MCP",
                    "kind": "mcp",
                    "config": {
                        "transport": protocol,
                        "url": f"http://127.0.0.1:{port}/{'sse' if protocol == 'sse' else 'mcp'}",
                        "headers_env": {"Authorization": "LOOM_TEST_MCP_AUTH"},
                    },
                }
            )
            assert len(store.discover(connection["id"])["tools"]) == 2
            output, failed = asyncio.run(call_mcp(connection["config"], "echo", {"message": "remote"}))
            assert not failed and "MCP: remote" in json.dumps(output)
            assert seen_headers == ["Bearer test-http-key", "Bearer test-http-key"]
            assert "test-http-key" not in json.dumps(store.list())
        finally:
            proc.terminate()
            proc.wait(timeout=10)
