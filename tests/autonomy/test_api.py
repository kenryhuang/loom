import http.client
import json
import threading

from loom.service.api import ServiceHTTPServer
from tests.autonomy.helpers import goals


def test_authenticated_agent_api_commands_and_scope(agent_service):
    service, facade, aid, _ = agent_service
    server = ServiceHTTPServer(("127.0.0.1", 0), service, "test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)

    def request(path, body=None, token="test-token"):
        connection.request(
            "POST" if body is not None else "GET",
            path,
            body=json.dumps(body) if body else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        result = connection.getresponse()
        return result.status, json.loads(result.read())

    try:
        assert request("/v1/agents", token="wrong")[0] == 401
        assert request("/v1/agents")[1]["agents"][0]["id"] == aid
        body = {"command_id": "new-goal", "goal": goals()[0]}
        first = request(f"/v1/agents/{aid}/goals", body)
        assert first[0] == 200
        assert request(f"/v1/agents/{aid}/goals", body) == first
        assert request(f"/v1/agents/{aid}/commands", {"command_id": "pause", "type": "pause", "expected_revision": 1})[0] == 409
        assert request(f"/v1/agents/{aid}/context/missing")[0] == 404
        assert request(f"/v1/agents/{aid}/guidance", {"command_id": "guide", "text": "just this", "scope": "current_task"})[0] == 400
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
