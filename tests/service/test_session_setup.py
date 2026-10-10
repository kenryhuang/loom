import asyncio
import json

import pytest

from loom.core import ok
from loom.llm.api import LlmResponse, TokenUsage
from loom.service.contracts import ServiceError
from loom.service.session_setup import SessionSetup
from tests.service.test_api import api  # noqa: F401


def proposal(**changes):
    return {
        "task_type": "coding",
        "tool_collections": ["filesystem", "task_control"],
        "external_tools": [],
        "rationale": "Read the indexing implementation without running commands.",
        "collection_reasons": {"filesystem": "Inspect the implementation"},
        **changes,
    }


class Recommender:
    model = "setup-test"

    def __init__(self, value=None, delay=0):
        self.value = proposal() if value is None else value
        self.delay = delay
        self.calls = []

    async def chat(self, messages, tools=None):
        assert tools is None
        self.calls.append(messages)
        await asyncio.sleep(self.delay)
        return ok(LlmResponse(json.dumps(self.value), usage=TokenUsage(20, 10, 30)))


def test_setup_recommendation_does_not_create_a_session_and_manual_override_executes(api):  # noqa: F811
    service, server, client, directory = api
    provider = Recommender()
    server.session_setup.provider_factory = lambda _: provider
    before = service.store.list_sessions()
    catalog = client._json("/v1/web/catalog")
    assert catalog["external_tools"] == []
    assert {r["id"] for r in catalog["tool_collections"]} >= {"filesystem", "shell", "knowledge", "task_control"}
    result = client._json("/v1/session-setup/recommend", {"objective": "Explain yakDB indexing"})
    assert result["task_type"] == "coding"
    assert result["tool_collections"] == ["filesystem", "task_control"]
    assert result["usage"]["total_tokens"] == 30
    assert result["acceptance"] == catalog["acceptance"]
    assert result["acceptance"]["required"] is True
    assert result["acceptance"]["plan_stage"] == "before_execution"
    assert result["acceptance"]["verify_stage"] == "before_completion"
    assert result["acceptance"]["defaults"]["max_repairs"] == 2
    assert "Runtime acceptance is mandatory" in provider.calls[0][0].content
    assert service.store.list_sessions() == before
    supplied = json.loads(provider.calls[0][1].content)
    assert supplied["task_description"] == "Explain yakDB indexing"
    assert supplied["external_tools"] == []
    # Review can add a collection and bind a directory before normal session creation.
    sid = client.create(
        {
            "workspace": str(directory),
            "task_spec": {
                "session_environment": {"plugin": "session", "resources": [{"id": "workspace", "kind": "directory", "uri": str(directory)}]},
                "tools": {"collections": [*result["tool_collections"], "shell"]},
            },
        }
    )["session_id"]
    assert client.snapshot(sid)["task"]["task_spec"]["tools"]["collections"] == ["filesystem", "task_control", "shell"]


@pytest.mark.parametrize(
    "bad",
    [
        proposal(tool_collections=["not_installed"]),
        proposal(external_tools=["fake-mcp"]),
        proposal(task_type="imaginary"),
        proposal(acceptance={"required": False}),
        {"task_type": "coding"},
        proposal(collection_reasons={"shell": "not selected"}),
    ],
)
def test_unavailable_model_suggestions_are_rejected(api, bad):  # noqa: F811
    _, server, client, _ = api
    server.session_setup.provider_factory = lambda _: Recommender(bad)
    with pytest.raises(ServiceError, match="unsupported configuration"):
        client._json("/v1/session-setup/recommend", {"objective": "Do work"})


def test_recommendation_timeout_and_invalid_request_are_explicit(api):  # noqa: F811
    service, server, client, _ = api
    server.session_setup = SessionSetup(service, server.web_frontend, provider_factory=lambda _: Recommender(delay=1), timeout=0.01)
    with pytest.raises(ServiceError, match="timed out"):
        client._json("/v1/session-setup/recommend", {"objective": "Do work"})
    with pytest.raises(ServiceError, match="objective"):
        client._json("/v1/session-setup/recommend", {"objective": ""})
    with pytest.raises(ServiceError, match="accepts"):
        client._json("/v1/session-setup/recommend", {"objective": "Do work", "api_key": "not allowed"})


def test_knowledge_and_plugins_are_selected_from_installed_catalog_and_bound(api):  # noqa: F811
    service, server, client, _ = api
    base = service.store.knowledge.create({"name": "AI Infra", "description": "AI systems performance", "engine": "sqlite_fts"})
    provider = Recommender(proposal(
        task_type="general", tool_collections=["task_control"], collection_reasons={},
        knowledge_base_ids=[base["id"]], knowledge_reasons={base["id"]: "Relevant reference"},
        plugins={"context": "bounded_context", "workflow": "dynamic"}, plugin_reasons={"workflow": "Direct question"},
    ))
    server.session_setup.provider_factory = lambda _: provider
    result = client._json("/v1/session-setup/recommend", {"objective": "Use AI Infra to explain inference performance"})
    assert result["knowledge_base_ids"] == [base["id"]]
    assert "knowledge" in result["tool_collections"]
    supplied = json.loads(provider.calls[0][1].content)
    assert supplied["knowledge_bases"][0]["name"] == "AI Infra"
    assert "dynamic" in [p["id"] for p in supplied["plugins"]["workflow"]]
    sid = client.create({"knowledge_base_ids": result["knowledge_base_ids"], "task_spec": {
        "tools": {"collections": result["tool_collections"]},
        **{kind: {"plugin": identifier} for kind, identifier in result["plugins"].items()},
    }})["session_id"]
    task = client.snapshot(sid)["task"]
    assert task["knowledge_base_ids"] == [base["id"]]
    assert task["task_spec"]["workflow"]["plugin"] == "dynamic"


@pytest.mark.parametrize("change", [{"knowledge_base_ids": ["missing"]}, {"plugins": {"workflow": "missing"}}, {"plugins": {"untrusted": "arbitrary"}}])
def test_uninstalled_bindings_are_rejected(api, change):  # noqa: F811
    _, server, client, _ = api
    server.session_setup.provider_factory = lambda _: Recommender(proposal(**change))
    with pytest.raises(ServiceError, match="unsupported configuration"):
        client._json("/v1/session-setup/recommend", {"objective": "Do work"})
