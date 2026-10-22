import asyncio
import base64
import hashlib
import json
import random
import struct
import sys
import zlib

import pytest

from loom.browser.scenarios import OBSERVATION_SCHEMA, SCENARIO_SCHEMA, validate_scenario
from loom.core import ok, thaw_json
from loom.llm import ImagePart, LlmMessage, LlmResponse, OpenAIProvider
from loom.runtime.checkpoints import decode, encode, plain
from loom.service.artifacts import ServiceArtifacts
from loom.service.contracts import ServiceError
from loom.tasks.assembly import TaskAssembly
from loom.tasks.config import load_task_config
from loom.tasks.request import TaskRequest, TaskRunOptions
from loom.tasks.runner import run_generic_task
from tests.tasks.test_acceptance import Judge, Sink


def png(width=128, height=128, noise=False, color=(255, 0, 0)):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))

    rng = random.Random(42)
    pixels = b"".join(b"\0" + (rng.randbytes(width * 3) if noise else bytes(color) * width) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def spec(root):
    return {"session_environment": {"plugin": "session", "resources": [{"id": "workspace", "kind": "directory", "uri": str(root)}]},
            "tools": {"collections": ["filesystem", "shell", "task_control", "browser"]}}


def scene(**changes):
    return {"id": "test-scene", "preview": {"argv": [sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"], "route": "/"},
            "ready_selector": "#ready", "captures": [{"id": "overview"}], **changes}


def visual_result(status="passed", recapture=False):
    return {"criterion_id": "appearance", "status": status, "reason": "Actual screenshot reviewed", "evidence_ids": ["overview"],
            "findings": [], "missing_evidence": ["Need another frame"] if status == "blocked" else [], "recapture": recapture}


def vision_provider(status="passed", recapture=False):
    calls = []

    async def http(url, request):
        body = request["body"]
        assert body["response_format"] == {"type": "json_object"}
        assert body["reasoning_effort"] == "none"
        parts = body["messages"][1]["content"]
        assert parts[0]["type"] == "text"
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
        raw = base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1])
        assert raw.startswith(b"\x89PNG")
        calls.append(body)
        verdict = status(len(calls)) if callable(status) else status
        return {"ok": True, "json": {"choices": [{"message": {"content": json.dumps(visual_result(verdict, recapture))}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 500, "completion_tokens": 50, "total_tokens": 550}}}

    provider = OpenAIProvider(model="qwen3.8-omni-flash", api_key="test", http_client=http, input_modalities=("text", "image"),
                             request_options={"modalities": ["text"], "reasoning_effort": "none", "response_format": {"type": "json_object"}})
    return provider, calls


def test_binary_artifact_upload_is_bounded_owned_and_integrity_checked(tmp_path):
    store = ServiceArtifacts(tmp_path)
    raw = png(256, 256, noise=True)
    identifier = store.begin_upload("worker-a", hashlib.sha256(raw).hexdigest(), len(raw), "image/png", "screenshot")
    with pytest.raises(ServiceError, match="belong"):
        store.upload("worker-b", identifier, offset=0, data="")
    with pytest.raises(ServiceError, match="chunk"):
        store.upload("worker-a", identifier, offset=0, data="x" * 65537)
    with pytest.raises(ServiceError, match="offset"):
        store.upload("worker-a", identifier, offset=10, data="")
    for offset in range(0, len(raw), 49152):
        store.upload("worker-a", identifier, offset=offset, data=base64.b64encode(raw[offset:offset + 49152]).decode())
    ref = store.upload("worker-a", identifier, finish=True)
    assert ref["media_type"] == "image/png"
    assert store.read(ref["sha256"]) == raw
    text = store.publish({"answer": "existing JSON"}, "report")
    assert json.loads(store.read(text["sha256"])) == {"answer": "existing JSON"}
    (tmp_path / ref["relative_path"]).write_bytes(b"corrupt")
    with pytest.raises(ServiceError, match="integrity"):
        store.read(ref["sha256"])


def test_image_messages_checkpoint_without_binary_and_materialize_only_at_provider(tmp_path):
    store = ServiceArtifacts(tmp_path)
    raw = png()
    part = ImagePart(store.publish_bytes(raw), 128, 128)
    message = LlmMessage("user", "Inspect", images=(part,))
    assert decode(encode(message)) == message
    assert "base64" not in json.dumps(plain(message))
    provider = OpenAIProvider(model="vision", api_key="test", input_modalities=("text", "image"), image_reader=store.read)
    body = provider._request_body([message], None, None, stream=False)
    assert body["messages"][0]["content"][1]["image_url"]["url"].endswith(base64.b64encode(raw).decode())
    with pytest.raises(ValueError, match="image-capable"):
        OpenAIProvider(model="text-only", api_key="test")._request_body([message], None, None, stream=False)


def test_visual_model_config_is_host_metadata_and_requires_image_capability(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text('visual_verification_model: cloud/omni\nproviders:\n  cloud:\n    models:\n      omni:\n        input_modalities: [text, image]\n')
    loaded = load_task_config(config).unwrap()
    assert loaded.visual_verification_model == "cloud/omni"
    assert loaded.models["cloud/omni"].input_modalities == ("text", "image")
    config.write_text(config.read_text().replace('[text, image]', '[text]'))
    assert not load_task_config(config).ok


@pytest.mark.parametrize("change", [{"steps": [{"action": "evaluate", "value": "evil()"}]}, {"captures": [{}] * 5},
                                   {"assertions": [{"id": "count", "kind": "count", "selector": "div", "expected": "8"}]}])
def test_invalid_browser_scenarios_are_rejected_before_execution(change):
    with pytest.raises(ValueError):
        validate_scenario(scene(**change))


def test_native_browser_contract_enforces_one_target_and_observe_needs_only_page_id():
    from jsonschema import Draft202012Validator

    from loom.llm.structured import native_schema

    def native_value(schema, value):
        # Simulate all keys required by provider strict output, unused keys null.
        if isinstance(value, dict):
            return {k: native_value(v, value[k]) if k in value else None for k, v in schema["properties"].items()}
        if isinstance(value, list):
            return [native_value(schema['items'], v) for v in value]
        return value

    validator = Draft202012Validator(native_schema(SCENARIO_SCHEMA))
    preview = native_value(SCENARIO_SCHEMA, scene())
    validator.validate(preview)
    external = native_value(SCENARIO_SCHEMA, {"url": "https://example.com"})
    validator.validate(external)
    preview['url'] = 'http://localhost:5173'
    assert not validator.is_valid(preview)
    assert not validator.is_valid(native_value(SCENARIO_SCHEMA, {}))
    with pytest.raises(ValueError, match='exactly one'):
        validate_scenario(preview)
    with pytest.raises(ValueError, match=r'preview.argv must include \{port\}'):
        validate_scenario(scene(preview={'argv': ['npm', 'run', 'dev']}))
    Draft202012Validator(OBSERVATION_SCHEMA).validate({'page_id': 'already-open'})


def test_native_visual_plan_nulls_normalize_before_schema_validation(tmp_path):
    from jsonschema import Draft202012Validator

    from loom.tasks.acceptance_protocols import plan_protocol

    assembly = TaskAssembly(TaskRequest('Check appearance', workspace=tmp_path, task_spec=spec(tmp_path)), plan_mode='off')
    proposal = {'criteria': [{'id': 'appearance', 'description': 'No extra bodies', 'verifier': 'visual', 'scope': [], 'required': True,
                             'check': {'scenario': scene(url=None, adapter_path=None, viewport=None, assertions=None,
                                                        preview={'argv': [sys.executable, '-m', 'http.server', '{port}'], 'cwd': None, 'route': None},
                                                        captures=[{'id': 'overview', 'selector': None, 'after_ms': None}])}}],
                'unresolved_requirements': []}
    protocol = plan_protocol(assembly)
    normalized, changes = protocol.normalize(proposal)
    Draft202012Validator(protocol.schema).validate(normalized)
    plan = assembly.acceptance.validate_plan(normalized)
    assert plan['criteria'][0]['check']['scenario']['preview']['argv'][-1] == '{port}'
    assert 'url' not in plan['criteria'][0]['check']['scenario']
    assert any(r == {'path': '/criteria/0/check/scenario/adapter_path', 'rule': 'native_optional_null'} for r in changes)
    normalized['criteria'][0]['check']['scenario']['preview']['argv'] = None
    with pytest.raises(ValueError):
        assembly.acceptance.validate_plan(normalized)


@pytest.mark.asyncio
async def test_visual_description_uses_text_despite_configured_json_mode(tmp_path):
    from types import SimpleNamespace

    requests = []

    async def http(url, request):
        body = request['body']
        assert 'response_format' not in body
        assert body['modalities'] == ['text']
        assert body['reasoning_effort'] == 'none'
        assert body['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
        requests.append(body)
        return {'ok': True, 'json': {'choices': [{'message': {'content': 'A red scene is visible.'}, 'finish_reason': 'stop'}],
                                    'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}}}

    provider = OpenAIProvider(model='qwen3.8-omni-flash', api_key='test', input_modalities=('text', 'image'), http_client=http,
                             request_options={'response_format': {'type': 'json_object'}, 'modalities': ['text'], 'reasoning_effort': 'none'})
    assembly = TaskAssembly(TaskRequest('Observe', workspace=tmp_path, task_spec=spec(tmp_path)), plan_mode='off')
    assembly.configure_acceptance(Judge(), visual_provider=provider)
    assembly._task_runtime = SimpleNamespace(run_id='run', loop_id='loop', trace_id='trace', trace_sink=Sink(), cancellation=None)
    try:
        image = ImagePart(assembly.publish_image(png()), 128, 128)
        result = await assembly.describe_images((image,), {'url': 'http://localhost', 'dom_text': '', 'page_errors': []})
        assert result == 'A red scene is visible.'
        assert len(requests) == 1
        assert provider.request_options['response_format']['type'] == 'json_object'
        assert assembly._local_usage.total_tokens == 110
    finally:
        await assembly.close()


@pytest.mark.asyncio
async def test_real_canvas_multiframe_capture_and_candidate_membership_binding(tmp_path):
    pytest.importorskip("playwright")
    (tmp_path / "index.html").write_text('''<div id="ready">Canvas scene</div><canvas width="320" height="240"></canvas><script>
      const c=document.querySelector('canvas'),gl=c.getContext('webgl');
      function draw(){gl.clearColor(.3,.2,.6,1);gl.clear(gl.COLOR_BUFFER_BIT);requestAnimationFrame(draw)} draw();</script>''')
    assembly = TaskAssembly(TaskRequest("Observe scene", workspace=tmp_path, task_spec=spec(tmp_path)), plan_mode="off")
    try:
        bundle, images, ref = await assembly.browser.capture(scene(canvas=True, captures=[{"id": "first", "selector": "canvas"},
                                                                                        {"id": "later", "selector": "canvas", "after_ms": 50}]))
        assert len(images) == len(bundle["frames"]) == 2
        assert bundle["canvas"][0]["webgl"] is True
        assert bundle["canvas"][0]["context_lost"] is False
        assert assembly.read_artifact(ref["sha256"])["source_binding"]["digest"]
        assert assembly.read_image(images[0].artifact["sha256"]).startswith(b"\x89PNG")
        automatic = scene(canvas=True)
        automatic.pop('captures')
        observed, _, _ = await assembly.browser.capture(automatic)
        assert [f['id'] for f in observed['frames']] == ['viewport', 'viewport-later']
        assert observed['frames'][1]['captured_at'] - observed['frames'][0]['captured_at'] >= 1
        (tmp_path / "unexpected.js").write_text("changed")
        with pytest.raises(ValueError, match="membership"):
            assembly.browser.validate_binding(bundle["source_binding"])
    finally:
        await assembly.close()
    assert assembly.browser.client.process is None


@pytest.mark.asyncio
async def test_loaded_framework_probe_reports_actual_duplicates_and_hidden_objects(tmp_path):
    pytest.importorskip('playwright')
    (tmp_path / '@react-three_fiber.js').write_text('export const _roots = new Map();')
    (tmp_path / 'index.html').write_text('''<canvas id="ready" width="320" height="240"></canvas><script type="module">
      import {_roots} from './@react-three_fiber.js'; const c=document.querySelector('canvas');
      const gl=c.getContext('webgl'); gl.clearColor(.2,.3,.4,1); gl.clear(gl.COLOR_BUFFER_BIT);
      const mesh=(name,visible=true)=>({isMesh:true,visible,name,geometry:{type:'SphereGeometry',parameters:{radius:1}},
        material:{map:{image:{src:location.origin+'/textures/earth.jpg'}}},children:[]});
      const scene={children:[mesh('one'),mesh('duplicate'),{visible:false,children:[mesh('hidden')]}]};
      _roots.set(c,{store:{getState:()=>({scene})}});</script>''')
    assembly = TaskAssembly(TaskRequest('Inspect scene', workspace=tmp_path, task_spec=spec(tmp_path)), plan_mode='off')
    try:
        bundle, _, _ = await assembly.browser.capture(scene(captures=[{'id': 'scene', 'after_ms': 300}]))
        facts = bundle['framework_facts']
        assert facts['complete'] and facts['mesh_count'] == 3
        assert facts['visible_geometry_counts'] == {'SphereGeometry': 2}
        assert facts['visible_texture_occurrences'] == {'/textures/earth.jpg': 2}
        assert any(row['name'] == 'hidden' and not row['visible_in_scene'] for row in facts['meshes'])
        assert bundle['frames'][0]['framework_facts'] == facts
    finally:
        await assembly.close()


@pytest.mark.parametrize("expected,status,calls", [(8, "passed", 1), (9, "blocked", 0)])
def test_visual_gate_uses_real_browser_images_and_cannot_override_hard_failures(tmp_path, expected, status, calls):
    pytest.importorskip("playwright")
    (tmp_path / "index.html").write_text('<div id="ready">Eight planets</div>' + '<div class="planet">planet</div>' * 8)
    criterion = {"id": "appearance", "description": "Eight planets rendered cleanly", "verifier": "visual", "scope": ["index.html"],
                 "check": {"scenario": scene(assertions=[{"id": "count", "kind": "count", "selector": ".planet", "expected": expected}])}}
    provider, seen = vision_provider()
    sink = Sink()
    result = asyncio.run(run_generic_task(TaskRequest("Fix excess planets", workspace=tmp_path, task_spec={**spec(tmp_path), "acceptance": {"max_repairs": 0}}),
        provider=Judge([criterion]), visual_provider=provider, options=TaskRunOptions(plan_mode="off", max_steps=1), trace_sink=sink)).unwrap()
    acceptance = thaw_json(result.run_result.context.state.scratch["acceptance"])
    assert acceptance["state"] == status, acceptance["reason"]
    assert len(seen) == calls
    assert acceptance["results"][0]["visual_evidence"]["kind"] == "visual_evidence"
    assert any(e["type"] == "visual.capture.completed" for e in sink.events)
    assert not any("data:image" in json.dumps(plain(e)) for e in sink.events)
    assert (acceptance["state"] == "passed") == any(e["type"] == "acceptance.gate.passed" for e in sink.events)


def test_visual_failure_with_deferred_command_judgment_enters_repair_and_recaptures(tmp_path):
    from loom.llm import LlmToolCall

    pytest.importorskip('playwright')
    (tmp_path / 'index.html').write_text('<body style="background:red"><div id="ready">Wrong appearance</div></body>')
    criteria = [
        {'id': 'build', 'description': 'Project remains executable', 'verifier': 'command', 'scope': ['index.html'],
         'check': {'tool_id': 'process_execute', 'input': {'argv': [sys.executable, '-c', 'print("Build OK")']}}},
        {'id': 'appearance', 'description': 'The requested scene is rendered correctly', 'verifier': 'visual', 'scope': [],
         'check': {'scenario': scene()}},
    ]

    class Repair(Judge):
        def __init__(self):
            super().__init__(criteria)
            self.solver_calls = 0

        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            if messages[0].content.startswith(('You design', 'You independently')):
                return await super().chat(messages, tools, cancellation, **kwargs)
            self.solver_calls += 1
            if self.solver_calls == 2:
                args = json.dumps({'path': 'index.html', 'content': '<body style="background:blue"><div id="ready">Correct appearance</div></body>'})
                return ok(LlmResponse(tool_calls=(LlmToolCall('repair', 'write_file', args),)))
            return ok(LlmResponse(tool_calls=(LlmToolCall(f'finish-{self.solver_calls}', 'finish', '{"report":"Scene fixed"}'),)))

    provider, seen = vision_provider(status=lambda n: 'failed' if n == 1 else 'passed')
    sink = Sink()
    result = asyncio.run(run_generic_task(TaskRequest('Fix visible scene', workspace=tmp_path, task_spec=spec(tmp_path)),
        provider=Repair(), visual_provider=provider, options=TaskRunOptions(plan_mode='off', max_steps=6), trace_sink=sink)).unwrap()
    acceptance = thaw_json(result.run_result.context.state.scratch['acceptance'])
    assert acceptance['state'] == 'passed', acceptance['reason']
    assert len(seen) == 2
    assert seen[0]['messages'][1]['content'][1] != seen[1]['messages'][1]['content'][1]
    checks = [e['result']['status'] for e in sink.events if e['type'] == 'verification.completed' and e['result']['criterion_id'] == 'appearance']
    assert checks == ['failed', 'passed']
    assert 'Correct appearance' in (tmp_path / 'index.html').read_text()


@pytest.mark.parametrize('visual_status,state', [('failed', 'needs_repair'), ('blocked', 'blocked')])
@pytest.mark.asyncio
async def test_deferred_checks_do_not_hide_the_actual_visual_failure_or_blocker(tmp_path, visual_status, state):
    from types import SimpleNamespace

    from loom.tasks.runner import make_task_context

    criteria = [
        {'id': 'build', 'description': 'Build succeeds', 'verifier': 'command', 'scope': [],
         'check': {'tool_id': 'process_execute', 'input': {'argv': ['build']}}},
        {'id': 'appearance', 'description': 'Page is correct', 'verifier': 'visual', 'scope': [], 'check': {'scenario': scene()}},
    ]
    assembly = TaskAssembly(TaskRequest('Fix appearance', workspace=tmp_path, task_spec=spec(tmp_path)), plan_mode='off')
    assembly.configure_acceptance(Judge(criteria))
    context = make_task_context(assembly.request, assembly=assembly, plan_mode='off').unwrap()
    runtime = SimpleNamespace(run_id='run', loop_id='loop', trace_id='trace', trace_sink=Sink(), cancellation=None)

    async def check(criterion, runtime):
        return ({'status': 'passed', 'reason': 'Command executed'} if criterion['id'] == 'build'
                else {'status': visual_status, 'reason': 'Actual visual result'}), {}

    assembly.acceptance.check = check
    try:
        assert not await assembly.acceptance.verify(context, runtime, 'candidate')
        assert assembly.acceptance.data['state'] == state
        assert assembly.acceptance.data['results'][0]['deferred'] is True
        if state == 'blocked':
            assert 'appearance: Actual visual result' in assembly.acceptance.data['reason']
            assert 'Outcome judgment skipped' not in assembly.acceptance.data['reason']
    finally:
        await assembly.close()


def test_visual_judgment_rejects_uncertainty_and_invalid_regions():
    from loom.tasks.visual_acceptance import visual_protocol

    _, validate = visual_protocol({"id": "appearance"}, [{"id": "overview"}])
    value = visual_result()
    value["findings"] = [{"evidence_id": "overview", "region": [.9, .9, .2, .2], "observation": "unclear", "certainty": "uncertain"}]
    with pytest.raises(ValueError, match="region"):
        validate(value)
    value["findings"][0]["region"] = []
    assert validate(value)["status"] == "blocked"


@pytest.mark.asyncio
async def test_optional_bad_overlay_does_not_retry_or_change_the_visual_verdict():
    from loom.llm.structured import request_structured
    from loom.tasks.visual_acceptance import visual_protocol

    protocol, validate = visual_protocol({'id': 'appearance'}, [{'id': 'overview'}])
    calls, events = [], []
    raw = visual_result('failed')
    raw['findings'] = [{'evidence_id': 'overview', 'region': [476, 452, 49, 50], 'observation': 'A demonstrated defect', 'certainty': 'observed'}]

    async def send(*args):
        calls.append(args)
        return LlmResponse(json.dumps(raw))

    async def event(kind, fields):
        events.append((kind, fields))

    result = await request_structured(protocol=protocol, payload={}, send=send, validate=validate, event=event)
    assert len(calls) == 1
    assert result.value['status'] == 'failed'
    assert result.value['findings'][0]['region'] == []
    assert result.value['findings'][0]['observation'] == 'A demonstrated defect'
    changes = next(fields['changes'] for kind, fields in events if kind == 'normalized')
    assert changes[0]['original'] == [476, 452, 49, 50]


@pytest.mark.parametrize("classification", ["evidence_method", "external_approval", "uncertain"])
def test_legacy_visual_migration_requires_independent_coverage_and_preserves_approvals(tmp_path, classification):
    from types import SimpleNamespace

    from loom.tasks.acceptance import AcceptanceBlocked
    from loom.tasks.runner import make_task_context
    from loom.tasks.workspace_probe import probe

    objective = "Repair the visible solar diagram"
    limitation = "Visual confirmation cannot be observed in this environment"
    criterion = {"id": "appearance", "description": "Rendered solar diagram has no extra bodies", "verifier": "visual", "scope": [],
                 "check": {"scenario": scene()}}

    class MigrationJudge(Judge):
        async def chat(self, messages, tools=None, cancellation=None, **kwargs):
            self.calls.append(messages)
            if messages[0].content.startswith("You design"):
                value = {"criteria": [criterion], "unresolved_requirements": []}
            else:
                value = {"mappings": [{"requirement": limitation, "classification": classification, "criterion_id": "appearance",
                                       "source_quote": objective, "reason": "New browser evidence covers the unchanged original outcome"}]}
            return ok(LlmResponse(json.dumps(value)))

    assembly = TaskAssembly(TaskRequest(objective, workspace=tmp_path, task_spec=spec(tmp_path)), plan_mode="off")
    judge = MigrationJudge()
    assembly.configure_acceptance(judge, visual_provider=vision_provider()[0])
    controller = assembly.acceptance
    context = make_task_context(assembly.request, assembly=assembly).unwrap()
    controller.goal(context)
    controller.data.update(plan={**controller.validate_plan({"criteria": [], "unresolved_requirements": [limitation]}), "revision": 1},
                           revision=1, profile=probe(tmp_path))
    runtime = SimpleNamespace(run_id="run", loop_id="loop", trace_id="trace", trace_sink=Sink(), cancellation=None)

    async def check():
        try:
            if classification == "evidence_method":
                await controller.prepare(context, runtime)
                assert controller.data["revision"] == 2
                assert controller.data["plan"]["unresolved_requirements"] == []
                assert controller.data["history"][0]["unresolved_requirements"] == [limitation]
                assert controller.data["plan"]["resolved_requirements"]["kind"] == "acceptance_migration"
                assert controller.data["state"] != "passed"
            else:
                with pytest.raises(AcceptanceBlocked, match="External or uncertain"):
                    await controller.prepare(context, runtime)
                assert len(judge.calls) == 2  # Plan + independent denial, no retry pressuring approval.
                assert controller.data["revision"] == 1
                assert controller.data["plan"]["unresolved_requirements"] == [limitation]
            calls = len(judge.calls)
            await controller.prepare(context, runtime)
            assert len(judge.calls) == calls  # No repeat without new capabilities/evidence.
        finally:
            await assembly.close()

    asyncio.run(check())


def test_unclear_visual_evidence_gets_only_one_recapture(tmp_path):
    pytest.importorskip("playwright")
    (tmp_path / "index.html").write_text('<div id="ready">Unclear scene</div>')
    criterion = {"id": "appearance", "description": "Scene is correct", "verifier": "visual", "scope": ["index.html"],
                 "check": {"scenario": scene()}}
    provider, seen = vision_provider("blocked", recapture=True)
    sink = Sink()
    result = asyncio.run(run_generic_task(TaskRequest("Inspect scene", workspace=tmp_path, task_spec=spec(tmp_path)),
        provider=Judge([criterion]), visual_provider=provider, options=TaskRunOptions(plan_mode="off", max_steps=2), trace_sink=sink)).unwrap()
    acceptance = thaw_json(result.run_result.context.state.scratch["acceptance"])
    assert acceptance["state"] == "blocked"
    assert "insufficient_visual_evidence" in acceptance["reason"]
    assert len(seen) == 2
    assert sum(e["type"] == "visual.capture.completed" for e in sink.events) == 2
