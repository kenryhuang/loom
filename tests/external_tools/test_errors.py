import httpx
import pytest

from loom.external_tools.errors import external_error
from loom.external_tools.store import ExternalToolStore
from loom.service.contracts import ServiceError


@pytest.mark.parametrize("status,advice", [(401, "Bearer"), (403, "activation"), (404, "endpoint"), (405, "transport=sse"), (429, "quota")])
def test_nested_http_error_is_actionable_without_exposing_credentials(status, advice):
    request = httpx.Request("POST", "https://example.test/mcp?key=secret", headers={"Authorization": "Bearer secret"})
    response = httpx.Response(status, request=request, text="secret response")
    error = httpx.HTTPStatusError("secret exception", request=request, response=response)
    result = external_error(ExceptionGroup("outer", [ExceptionGroup("inner", [error])]), "MCP discovery")
    assert f"HTTP {status}" in str(result) and advice in str(result)
    assert "secret" not in str(result) and "ExceptionGroup" not in str(result)


def test_grouped_timeout_and_missing_environment():
    result = external_error(ExceptionGroup("outer", [httpx.ReadTimeout("secret")]), "MCP call")
    assert result.code == "EXTERNAL_TOOL_TIMEOUT" and "secret" not in str(result)
    original = ServiceError("Environment variable TEST_KEY is not set", 422)
    assert external_error(ExceptionGroup("outer", [original]), "MCP call") is original


def test_discovery_exposes_nested_error_and_preserves_saved_tools(tmp_path, monkeypatch):
    store = ExternalToolStore(tmp_path)
    connection = store.save({"name": "test", "description": "test", "kind": "mcp", "config": {"transport": "sse", "url": "https://example.test/sse"}})

    async def success(config):
        return [{"name": "existing", "inputSchema": {"type": "object"}}]

    monkeypatch.setattr("loom.external_tools.transport.discover", success)
    saved = store.discover(connection["id"])

    async def failure(config):
        raise ExceptionGroup("task group", [TimeoutError()])

    monkeypatch.setattr("loom.external_tools.transport.discover", failure)
    with pytest.raises(ServiceError, match="timed out") as result:
        store.discover(connection["id"])
    assert result.value.code == "EXTERNAL_TOOL_TIMEOUT"
    assert store.get(connection["id"])["tools"] == saved["tools"]
