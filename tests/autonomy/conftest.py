import pytest

from loom.service.controller import LoomService
from tests.autonomy.helpers import evaluation_provider, role, single_package, solver_provider


@pytest.fixture
def agent_service(tmp_path):
    (tmp_path / "notes.txt").write_text("Session fixture evidence: known current workspace.")
    service = LoomService(tmp_path / "data", provider_factory=solver_provider, agent_planner=single_package, evaluation_provider_factory=evaluation_provider)
    facade = service.background.autonomy
    agent = facade.store.create("create-agent", role(tmp_path))
    facade.store.command(agent["id"], "enable", "enable", 1)
    yield service, facade, agent["id"], tmp_path
    service.close()
