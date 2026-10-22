"""Private JSON-lines browser worker. stdout contains only bounded protocol records."""

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import suppress

from loom.browser.frameworks import framework_facts
from loom.browser.readiness import wait_for_render
from loom.browser.scenarios import origin


class BrowserWorker:
    def __init__(self):
        self.browser = self.playwright = None
        self.pages = {}
        self.processes = []

    async def start(self):
        if self.browser:
            return
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)

    async def open(self, value):
        await self.start()
        url = value.get("url")
        preview = value.get("preview")
        if preview:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            argv = [arg.replace("{port}", str(port)) for arg in preview["argv"]]
            if not any("{port}" in arg for arg in preview["argv"]):
                raise ValueError("Preview argv must include a {port} placeholder")
            env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}}
            process = subprocess.Popen(argv, cwd=preview["cwd"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            self.processes.append(process)
            url = f"http://127.0.0.1:{port}" + preview.get("route", "/")
            for _ in range(100):
                if process.poll() is not None:
                    raise ValueError("preview_start_failed: preview process exited")
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            else:
                raise ValueError("preview_start_failed: application did not listen")
        allowed = set(value.get("allowed_origins", [])) | {origin(url)}
        context = await self.browser.new_context(viewport=value.get("viewport", {"width": 1440, "height": 900}),
                                                 device_scale_factor=1, locale="en-US", color_scheme="dark", service_workers="block")

        async def route(request):
            target = request.request.url
            if target.startswith(("data:", "blob:", "about:")) or origin(target) in allowed:
                await request.continue_()
            else:
                await request.abort()

        await context.route("**/*", route)
        await context.add_init_script("""(() => {
          const get = HTMLCanvasElement.prototype.getContext, contexts = new WeakMap();
          HTMLCanvasElement.prototype.getContext = function(kind, ...args) {
            const value = get.call(this, kind, ...args);
            if (value && !contexts.has(this)) {
              const webgl = /^(webgl2?|experimental-webgl)$/.test(kind);
              const entry = {gl:webgl ? value : null,kind,render_calls:0,draw_calls:0};
              contexts.set(this, entry);
              if (webgl) for (const name of ['clear','drawArrays','drawElements','drawArraysInstanced','drawElementsInstanced']) {
                const original = value[name];
                if (typeof original !== 'function') continue;
                value[name] = function(...args) {
                  const result = Reflect.apply(original, this, args);
                  entry.render_calls++; if (name !== 'clear') entry.draw_calls++;
                  return result;
                };
              }
            }
            return value;
          };
          Object.defineProperty(window, '__loomCanvasState', {value: () => [...document.querySelectorAll('canvas')].map(c => {
            const entry = contexts.get(c), gl = entry?.gl;
            const extension = gl && gl.getExtension('WEBGL_debug_renderer_info');
            const bounds = c.getBoundingClientRect(), style = getComputedStyle(c);
            return {width:c.width,height:c.height,webgl:!!gl,kind:entry?.kind || 'uninitialized',
              visible:bounds.width > 0 && bounds.height > 0 && style.visibility !== 'hidden' && style.display !== 'none',
              render_calls:entry?.render_calls || 0,draw_calls:entry?.draw_calls || 0,
              context_lost:gl ? gl.isContextLost() : null,
              renderer:gl ? gl.getParameter(extension ? extension.UNMASKED_RENDERER_WEBGL : gl.RENDERER) : null};
          })});
        })()""")
        page = await context.new_page()
        page.set_default_timeout(10000)
        errors, request_failures, http_errors = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)[:1000]) if len(errors) < 20 else None)
        page.on("requestfailed", lambda request: request_failures.append(
            {"url": request.url[:2000], "resource_type": request.resource_type, "error": request.failure}) if len(request_failures) < 20 else None)
        page.on("response", lambda response: http_errors.append(
            {"url": response.url[:2000], "status": response.status}) if response.status >= 400 and len(http_errors) < 20 else None)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if response and response.status >= 400:
            await context.close()
            raise ValueError(f"application_http_error: HTTP {response.status}")
        await page.evaluate("document.fonts.ready")
        identifier = uuid.uuid4().hex
        self.pages[identifier] = dict(page=page, context=context, errors=errors, request_failures=request_failures,
                                      http_errors=http_errors, adapter=value.get("adapter_script"))
        return {"page_id": identifier, "url": page.url, "browser_version": self.browser.version}

    def session(self, identifier):
        if identifier not in self.pages:
            raise ValueError("browser_session_gone: open a new page after restore")
        return self.pages[identifier]

    async def act(self, value):
        session = self.session(value["page_id"])
        page = session["page"]
        steps = []
        for step in value.get("steps", []):
            action = step["action"]
            locator = page.locator(step.get("selector", "html"))
            if action == "wait_visible":
                await locator.wait_for(state="visible")
            elif action == "wait_text":
                from playwright.async_api import expect

                await expect(locator).to_contain_text(step["value"])
            elif action == "click":
                await locator.click()
            elif action == "fill":
                await locator.fill(step["value"])
            elif action == "select":
                await locator.select_option(step["value"])
            elif action == "press":
                await locator.press(step["value"])
            elif action == "scroll":
                await page.mouse.wheel(step.get("x", 0), step.get("y", 500))
            elif action == "drag":
                await locator.drag_to(page.locator(step["target"]))
            elif action == "adapter":
                if not session["adapter"]:
                    raise ValueError("adapter_unavailable")
                await page.evaluate(session["adapter"], {"operation": step["value"]})
            else:
                raise ValueError("Unsupported browser action")
            steps.append({**step, "status": "completed", "at": time.time()})
        return {"steps": steps, "url": page.url}

    async def observe(self, value):
        session = self.session(value["page_id"])
        page = session["page"]
        if value.get("ready_selector"):
            await page.locator(value["ready_selector"]).wait_for(state="visible")
        readiness = await wait_for_render(page, canvas_required=bool(value.get("canvas")))
        canvas = await page.evaluate("window.__loomCanvasState()")
        if value.get("canvas") and (not canvas or any(not c["width"] or not c["height"] or c["context_lost"] for c in canvas)):
            raise ValueError("canvas_not_ready: missing canvas, empty buffer or WebGL context lost")
        await page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        assertions = []
        for row in value.get("assertions", []):
            locator, kind = page.locator(row["selector"]), row["kind"]
            if kind == "count":
                actual = await locator.count()
                passed = actual == row["expected"]
            elif kind == "text":
                actual = (await locator.inner_text())[:4000]
                passed = actual == row["expected"]
            else:
                actual = await locator.is_visible()
                passed = actual if kind == "visible" else not actual
            assertions.append({**row, "actual": actual, "status": "passed" if passed else "failed", "assurance": "host_observed"})
        facts = None
        if session["adapter"]:
            facts = await page.evaluate(session["adapter"], {"operation": "observe"})
            if len(json.dumps(facts)) > 24000:
                raise ValueError("Adapter facts exceed limit")
        frames, used = [], 0
        captures = value.get("captures")
        if not captures:
            # A context can exist before asynchronous scene/texture loading draws
            # the first useful frame. Do not reduce an omitted WebGL scenario to
            # the first empty buffer; retain separate timed observations.
            canvas = await page.evaluate("window.__loomCanvasState()")
            captures = ([{"id": "viewport", "after_ms": 1500}, {"id": "viewport-later", "after_ms": 1000}]
                        if value.get("canvas") or any(c["webgl"] for c in canvas) else [{"id": "viewport"}])
        for index, capture in enumerate(captures):
            await asyncio.sleep(capture.get("after_ms", 0) / 1000)
            target = page.locator(capture["selector"]) if capture.get("selector") else page
            raw = await target.screenshot(type="png", animations="allow", timeout=15000)
            used += len(raw)
            if len(raw) > 4_000_000 or used > 16_000_000:
                raise ValueError("Screenshot byte budget exceeded")
            frames.append({"canvas": await page.evaluate("window.__loomCanvasState()"), "id": capture.get("id", f"frame-{index}"),
                           "png": base64.b64encode(raw).decode(),
                           "captured_at": time.time(), "selector": capture.get("selector"), "framework_facts": await framework_facts(page)})
        return {"url": page.url, "viewport": page.viewport_size, "browser_version": self.browser.version, "readiness": readiness,
                "canvas": await page.evaluate("window.__loomCanvasState()"), "instrumentation": "native_canvas_render_observer_v2",
                "assertions": assertions, "adapter_facts": facts, "adapter_assurance": "application_reported",
                "framework_facts": frames[-1]['framework_facts'],
                "page_errors": list(session["errors"]), "request_failures": list(session["request_failures"]),
                "http_errors": list(session["http_errors"]), "dom_text": (await page.locator("body").inner_text())[:6000], "frames": frames}

    async def call(self, value):
        method = value["method"]
        if method == "open":
            return await self.open(value)
        if method == "act":
            return await self.act(value)
        if method == "observe":
            return await self.observe(value)
        if method == "capture":
            opened = await self.open(value)
            try:
                steps = await self.act({**value, **opened})
                return {**await self.observe({**value, **opened}), "steps": steps["steps"], "instance_id": opened["page_id"]}
            finally:
                await self.pages.pop(opened["page_id"])["context"].close()
        raise ValueError("Unknown browser operation")

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)
                if process.poll() is None:
                    process.kill()
                    process.wait()


async def main():
    worker = BrowserWorker()
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                if len(line) > 100000:
                    raise ValueError("Browser request exceeds limit")
                value = json.loads(line)
                async with asyncio.timeout(55):
                    result = await worker.call(value)
                response = {"ok": True, "value": result}
            except Exception as exc:
                response = {"ok": False, "error": str(exc)[:2000] or type(exc).__name__}
            print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
