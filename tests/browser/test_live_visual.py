"""Opt-in provider smoke: LOOM_VISUAL_LIVE=1 uv run --extra browser pytest this_file."""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from loom.evaluation.provider import bounded_http_client
from loom.llm import ImagePart, LlmMessage, request_llm_response
from loom.service.artifacts import ServiceArtifacts
from loom.tasks.assembly import TaskAssembly
from loom.tasks.config import create_provider_from_task_config, load_task_config
from loom.tasks.request import TaskRequest
from tests.browser.test_visual import png
from tests.tasks.test_acceptance import Sink

pytestmark = pytest.mark.skipif(os.environ.get("LOOM_VISUAL_LIVE") != "1", reason="Explicit live model opt-in required")


@pytest.mark.parametrize("stream", [False, True])
def test_configured_omni_really_receives_multiple_images_and_returns_json(tmp_path, stream):
    config = load_task_config(os.environ.get("LOOM_VISUAL_CONFIG", "config.yaml")).unwrap()
    provider = create_provider_from_task_config(config, model_name=config.visual_verification_model).unwrap()
    store = ServiceArtifacts(Path(tmp_path))
    images = tuple(ImagePart(store.publish_bytes(png(color=color)), 128, 128, f"image-{i}")
                   for i, color in enumerate(((255, 0, 0), (0, 0, 255))))
    provider = replace(provider, image_reader=store.read, max_completion_tokens=512,
                       http_client=None if stream else bounded_http_client(40))

    async def run():
        async with asyncio.timeout(45):
            response = await request_llm_response(provider, [LlmMessage("system", "Inspect actual images. Return only JSON."),
                LlmMessage("user", 'Return JSON {"colors": [dominant color of image 1, dominant color of image 2], "image_count": integer}. '
                           'Use simple English color names based on the image pixels.', images=images)], stream=stream)
        value = json.loads(response.unwrap().content)
        assert value["colors"] == ["red", "blue"]
        assert value["image_count"] == 2
        assert response.value.usage.total_tokens > 0

    asyncio.run(run())


def test_configured_json_model_can_describe_images_as_plain_text(tmp_path):
    config = load_task_config(os.environ.get("LOOM_VISUAL_CONFIG", "config.yaml")).unwrap()
    provider = create_provider_from_task_config(config, model_name=config.visual_verification_model, http_client=bounded_http_client(30)).unwrap()
    store = ServiceArtifacts(tmp_path)
    image = ImagePart(store.publish_bytes(png()), 128, 128, 'red-scene')

    async def run():
        assembly = TaskAssembly(TaskRequest('Observe an actual image'), plan_mode='off')
        assembly.configure_acceptance(provider, visual_provider=provider)
        assembly.read_image = store.read
        assembly._task_runtime = SimpleNamespace(run_id='live', loop_id='observe', trace_id='image', trace_sink=Sink(), cancellation=None)
        try:
            text = await assembly.describe_images((image,), {'url': 'http://localhost', 'dom_text': '', 'page_errors': []})
            assert text.strip() and not text.startswith(('visual_description_unavailable:', 'visual_input_budget_exceeded:'))
            assert assembly._local_usage.total_tokens > 0
        finally:
            await assembly.close()

    asyncio.run(run())
