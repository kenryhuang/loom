"""Journaled browser tools and bounded, artifact-backed screenshot acquisition."""

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import signal
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from loom.browser.scenarios import OBSERVATION_SCHEMA, SCENARIO_SCHEMA, STEP, origin, validate_scenario
from loom.core import Observation, ToolRef, err, make_loom_error, new_trace_id, now_iso, ok
from loom.llm.images import ImagePart, image_dimensions
from loom.tasks.workspace_probe import IGNORE, bound_path, digest
from loom.tools.collections import ToolCollection


class BrowserClient:
    def __init__(self):
        self.process = None
        self.lock = asyncio.Lock()

    async def call(self, value, options=None):
        async with self.lock:
            if self.process is None:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "loom.browser.worker", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=24_000_000,
                )
                if options and options.get("process_started"):
                    options["process_started"](self.process.pid)
            try:
                self.process.stdin.write((json.dumps(value) + "\n").encode())
                await self.process.stdin.drain()
                pending = asyncio.create_task(self.process.stdout.readline())
                try:
                    async with asyncio.timeout(58):
                        while not pending.done():
                            await asyncio.wait({pending}, timeout=0.1)
                            if options and options.get("cancel_check") and options["cancel_check"]():
                                raise asyncio.CancelledError()
                        raw = await pending
                finally:
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                if not raw:
                    raise ValueError("browser_session_gone: worker exited")
                response = json.loads(raw)
                if not response["ok"]:
                    raise ValueError(response["error"])
                return response["value"]
            except BaseException:
                await self.close()
                raise

    async def close(self):
        process, self.process = self.process, None
        if not process:
            return
        if process.stdin:
            with suppress(BrokenPipeError):
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 4)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        # The worker owns a dedicated session; reap preview grandchildren too.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


class BrowserCollection(ToolCollection):
    def __init__(self, request):
        self.request = request
        self.assembly = None
        self.client = BrowserClient()
        self.folders = []
        self.observations = []
        self.page_bindings = {}
        refs = (
            ToolRef("browser_close", "Close this task's interactive browser and preview, releasing its resources.",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False}, timeout_ms=10000),
            ToolRef("browser_capture", "Independently replay a bounded Web/Canvas/WebGL scenario and retain actual screenshots, runtime facts and assertions. "
                    "For local workspace code use preview.argv with {port}; requires shell. Do not treat screenshot collection as acceptance.",
                    input_schema=SCENARIO_SCHEMA, timeout_ms=60000),
            ToolRef("browser_open", "Open a Web application in an isolated Chromium context. Use a preview argv with {port} for current workspace code; "
                    "external URLs require a bound URL resource. Returns page_id. Does not install dependencies.",
                    input_schema=SCENARIO_SCHEMA, timeout_ms=60000),
            ToolRef("browser_act", "Interact with an opened page using bounded recorded actions. page_id is ephemeral after restore.", input_schema={
                "type": "object", "required": ["page_id", "steps"], "additionalProperties": False, "properties": {
                    "page_id": {"type": "string"}, "steps": {"type": "array", "maxItems": 12, "items": STEP}}}, timeout_ms=60000),
            ToolRef("browser_observe", "Capture real viewport/canvas PNG images, DOM facts and page errors. Returns image artifact references and a visual "
                    "description; images can be used by independent acceptance. Supply page_id and optional captures/assertions.",
                    input_schema=OBSERVATION_SCHEMA, timeout_ms=60000),
        )
        handlers = {ref.id: self.handler(ref.id) for ref in refs}
        super().__init__("browser", refs, handlers, {"browser_capture": "side_effecting", "browser_open": "side_effecting",
                                                    "browser_act": "side_effecting", "browser_observe": "read_only", "browser_close": "side_effecting"})

    def capability(self):
        return {"installed": importlib.util.find_spec("playwright") is not None, "backend": "playwright_chromium", "canvas_webgl": "probe_on_open",
                "model": getattr(getattr(self.assembly, "visual_provider", None), "model", None)}

    def prepare(self, scenario, *, verification=False):
        scenario = validate_scenario(scenario)
        value = dict(scenario)
        allowed = [origin(r.uri) for r in self.assembly.resources if r.kind == "url"]
        if scenario.get("url"):
            local = urlsplit(scenario["url"]).hostname in {"127.0.0.1", "localhost", "::1"}
            if origin(scenario["url"]) not in allowed and not (local and self.request.workspace and not verification):
                raise ValueError("target_version_unknown: verification needs a candidate preview or explicitly bound URL resource")
        if scenario.get("preview"):
            if not self.request.workspace or "process_execute" not in self.assembly.bindings:
                raise ValueError("preview_requires_workspace_and_shell")
            root = Path(self.request.workspace).resolve()
            folder = tempfile.TemporaryDirectory(prefix="loom-visual-candidate-")
            self.folders.append(folder)
            target = Path(folder.name)
            files, total = {}, 0
            pending = [root]
            while pending:
                current = pending.pop()
                for path in sorted(current.iterdir()):
                    if path.name in IGNORE or path.name.startswith(".") or path.is_symlink():
                        continue
                    relative = path.relative_to(root).as_posix()
                    bound_path(root, relative)
                    if path.is_dir():
                        pending.append(path)
                    elif path.is_file():
                        if path.stat().st_size > 8_000_000:
                            raise ValueError(f"Candidate file exceeds snapshot limit: {relative}")
                        raw = path.read_bytes()
                        total += len(raw)
                        if len(files) >= 4096 or total > 64_000_000:
                            raise ValueError("Candidate snapshot exceeds bounded acquisition budget")
                        files[relative] = hashlib.sha256(raw).hexdigest()
                        destination = target / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(raw)
                        destination.chmod(path.stat().st_mode & 0o777)
            # Dependencies are explicit shared inputs, not copied source or hidden credentials.
            if (root / "node_modules").is_dir():
                dependencies = target / "node_modules"
                dependencies.mkdir()
                for package in (root / "node_modules").iterdir():
                    if package.name not in {".vite", ".cache"}:
                        (dependencies / package.name).symlink_to(package, target_is_directory=package.is_dir())
            value["preview"] = {**scenario["preview"], "cwd": str(bound_path(target, scenario["preview"].get("cwd", ".")))}
            value["source_binding"] = {"files": files, "digest": digest(files), "dependency_mode": "shared_packages_isolated_cache_not_cross_run_cacheable"}
        if scenario.get("adapter_path"):
            if not self.request.workspace:
                raise ValueError("Adapter requires workspace")
            path = bound_path(self.request.workspace, scenario["adapter_path"])
            if path.stat().st_size > 24000:
                raise ValueError("Adapter exceeds 24 KB")
            value["adapter_script"] = path.read_text()
            value["adapter_digest"] = hashlib.sha256(value["adapter_script"].encode()).hexdigest()
            value.setdefault("source_binding", {}).update(adapter_path=scenario["adapter_path"], adapter_digest=value["adapter_digest"])
        value["allowed_origins"] = allowed
        return value

    def source_inventory(self):
        if not self.request.workspace:
            return {}
        root = Path(self.request.workspace).resolve()
        pending, files, total = [root], {}, 0
        while pending:
            for item in pending.pop().iterdir():
                if item.name in IGNORE or item.name.startswith(".") or item.is_symlink():
                    continue
                name = item.relative_to(root).as_posix()
                bound_path(root, name)
                if item.is_dir():
                    pending.append(item)
                elif item.is_file():
                    size = item.stat().st_size
                    total += size
                    if len(files) >= 4096 or size > 8_000_000 or total > 64_000_000:
                        raise ValueError("Candidate source inventory exceeds visual binding limits")
                    files[name] = hashlib.sha256(item.read_bytes()).hexdigest()
        return files

    def validate_binding(self, binding):
        if binding.get("adapter_path"):
            script = bound_path(self.request.workspace, binding["adapter_path"])
            if hashlib.sha256(script.read_bytes()).hexdigest() != binding["adapter_digest"]:
                raise ValueError("stale_visual_evidence: adapter changed")
        expected = binding.get("files")
        if expected is not None:
            actual = self.source_inventory()
            if set(actual) != set(expected):
                raise ValueError("stale_visual_evidence: candidate membership changed")
            if actual != expected:
                raise ValueError("stale_visual_evidence: source changed during browser acquisition")

    def retain(self, result, scenario, binding=None):
        frames, images = [], []
        for index, frame in enumerate(result.pop("frames")):
            raw = base64.b64decode(frame.pop("png"), validate=True)
            width, height = image_dimensions(raw)
            ref = self.assembly.publish_image(raw)
            row = {**frame, "artifact": ref, "width": width, "height": height}
            frames.append(row)
            images.append(ImagePart(ref, width, height, row.get("id", f"frame-{index}")))
        bundle = {"schema_version": 1, **result, "frames": frames, "scenario": scenario, "scenario_digest": digest(scenario),
                  "source_binding": binding, "goal_digest": self.assembly.acceptance.data["goal_digest"],
                  "write_epoch": self.assembly.acceptance.data["write_epoch"], "run_id": self.assembly.run_id,
                  "assurance": "browser_capture_and_recorded_assertions"}
        ref = self.assembly.publish_artifact(bundle, "visual_evidence")
        self.observations.append(ref)
        return bundle, images, ref

    async def capture(self, scenario, *, options=None):
        folders = len(self.folders)
        client = BrowserClient()
        try:
            value = self.prepare(scenario, verification=True)
            result = await client.call({**{k: v for k, v in value.items() if k != "source_binding"}, "method": "capture"}, options)
            binding = value.get("source_binding")
            self.validate_binding(binding or {})
            return self.retain(result, scenario, binding)
        finally:
            await client.close()
            for folder in self.folders[folders:]:
                folder.cleanup()
            del self.folders[folders:]

    def handler(self, name):
        async def invoke(value, options=None):
            try:
                if name == "browser_close":
                    await self.close()
                    result = {"closed": True}
                elif name == "browser_capture":
                    bundle, images, ref = await self.capture(value, options=options)
                    result = {"artifact": ref, "frames": bundle["frames"], "assertions": bundle["assertions"], "page_errors": bundle["page_errors"]}
                    if not self.assembly.acceptance.verifying and self.assembly.visual_provider and self.assembly._task_runtime:
                        result["visual_description"] = await self.assembly.describe_images(images, bundle)
                elif name == "browser_open":
                    await self.close()
                    self.page_bindings.clear()
                    prepared = self.prepare(value)
                    result = await self.client.call({**{k: v for k, v in prepared.items() if k != "source_binding"}, "method": "open"}, options)
                    self.page_bindings[result["page_id"]] = prepared.get("source_binding")
                elif name == "browser_act":
                    result = await self.client.call({**value, "method": "act"}, options)
                else:
                    binding = self.page_bindings.get(value["page_id"])
                    self.validate_binding(binding or {})
                    result = await self.client.call({**value, "method": "observe"}, options)
                    bundle, images, ref = self.retain(result, {k: v for k, v in value.items() if k != "page_id"}, binding)
                    result = {"artifact": ref, "frames": bundle["frames"], "assertions": bundle["assertions"], "page_errors": bundle["page_errors"]}
                    if self.assembly.visual_provider and self.assembly._task_runtime:
                        result["visual_description"] = await self.assembly.describe_images(images, bundle)
                    else:
                        result["visual_description"] = "vision_model_unavailable: configure an image-capable visual_verification_model"
                return ok(Observation(new_trace_id(), name, result, now_iso()))
            except (ValueError, OSError, ImportError) as exc:
                return err(make_loom_error("BROWSER_UNAVAILABLE", str(exc), retryable=False))
        return invoke

    async def close(self):
        await self.client.close()
        for folder in self.folders:
            folder.cleanup()
        self.folders.clear()


def browser_collection(request):
    return BrowserCollection(request)
