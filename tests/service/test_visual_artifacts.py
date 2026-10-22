import base64
import hashlib
import itertools
import json

import pytest

from loom.runtime.checkpoints import encode
from loom.service.contracts import ServiceError
from tests.browser.test_visual import png
from tests.service.test_api import api  # noqa: F401
from tests.service.test_controller import create


def visual_service_provider(state):
    from tests.browser.test_visual import Judge, scene, vision_provider

    criterion = {"id": "appearance", "description": "Eight planets rendered cleanly", "verifier": "visual", "scope": ["index.html"],
                 "check": {"scenario": scene(assertions=[{"id": "count", "kind": "count", "selector": ".planet", "expected": 8}])}}
    provider = Judge([criterion])
    provider.visual_provider = vision_provider()[0]
    return provider


def test_worker_chunk_upload_and_authenticated_image_delivery(api):  # noqa: F811
    service, _, client, directory = api
    sid = create(service, directory / "workspace")
    descriptor = service.prepare_attempt(sid)
    serial = itertools.count()

    def rpc(kind, value):
        return service.worker_record(sid, dict(epoch=descriptor["epoch"], attempt_id=descriptor["run"]["attempt_id"],
            record_id=f"image-{next(serial)}", type=kind, payload=encode(value)))

    raw = png(256, 256, noise=True)
    upload = rpc("artifact_upload_start", dict(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw), media_type="image/png", kind="screenshot"))
    for offset in range(0, len(raw), 49152):
        rpc("artifact_upload_chunk", dict(upload_id=upload, offset=offset, data=base64.b64encode(raw[offset:offset + 49152]).decode()))
    ref = rpc("artifact_upload_finish", dict(upload_id=upload))
    with client._open(client._path(sid, "artifacts/" + ref["sha256"])) as response:
        assert response.headers["Content-Type"] == "image/png"
        assert response.read() == raw
    received = []
    for offset in range(0, len(raw), 49152):
        part = rpc("read_artifact_chunk", dict(digest=ref["sha256"], offset=offset))
        assert len(part["data"]) <= 65536
        received.append(base64.b64decode(part["data"]))
    assert b"".join(received) == raw
    other = client.create({})["session_id"]
    with pytest.raises(ServiceError, match="not referenced"):
        client._json(client._path(other, "artifacts/" + ref["sha256"]))
    assert "base64" not in json.dumps(service.events(sid, limit=1000))


def test_visual_acceptance_completes_in_durable_worker_and_images_survive_restart(tmp_path):
    pytest.importorskip("playwright")
    from loom.service.controller import LoomService
    from tests.browser.test_visual import spec
    from tests.service.test_controller import wait_state

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "index.html").write_text('<div id="ready">Eight planets</div>' + '<div class="planet">planet</div>' * 8)
    service = LoomService(tmp_path / "data", provider_factory=visual_service_provider).start()
    try:
        sid = service.create("visual-task", {"objective": "Fix planet rendering", "workspace": str(root),
            "plan_mode": "off", "task_spec": {**spec(root), 'workflow': {'plugin': 'dynamic'}}})["session_id"]
        state = wait_state(service, sid, "idle", timeout=30)
        assert state["acceptance"]["state"] == "passed", state["acceptance"]["reason"]
        events = service.events(sid, limit=1000)
        assert sum(e['type'] == 'visual.capture.completed' for e in events) == 1
        assert sum(e['type'] == 'llm.requested' and e['payload'].get('acceptance_stage') == 'visual' for e in events) == 1
        visual_ref = state["acceptance"]["results"][0]["visual_evidence"]
        bundle = json.loads(service.store.read_artifact(sid, visual_ref["sha256"]))
        image = bundle["frames"][0]["artifact"]
        assert service.store.read_artifact(sid, image["sha256"]).startswith(b"\x89PNG")
    finally:
        service.close()
    service = LoomService(tmp_path / "data")
    try:
        assert service.store.read_artifact(sid, image["sha256"]).startswith(b"\x89PNG")
        assert service.store.artifact_ref(sid, image["sha256"])["media_type"] == "image/png"
    finally:
        service.close()
