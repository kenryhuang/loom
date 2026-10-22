"""Bounded rendering readiness; DOMContentLoaded alone is not a visual frame."""

import asyncio
import time

from loom.browser.frameworks import framework_facts


async def wait_for_render(page, *, canvas_required=False, timeout_seconds=12):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    started = time.monotonic()
    phase, notes = "document_load", []
    try:
        async with asyncio.timeout(timeout_seconds):
            await page.wait_for_load_state("load")
            phase = "asset_settle"
            try:
                # Vite imports and textures can start after DOMContentLoaded.
                # A continuously polling page must not consume the whole budget.
                await page.wait_for_load_state("networkidle", timeout=3000)
            except PlaywrightTimeoutError:
                notes.append("network_idle_not_reached")
            phase = "canvas_initialization"
            if canvas_required:
                await page.locator("canvas").first.wait_for(state="visible")
            await page.wait_for_function("""() => window.__loomCanvasState().every(c =>
                !c.visible || (c.width > 0 && c.height > 0 && c.kind !== 'uninitialized' &&
                !c.context_lost && (!c.webgl || c.render_calls > 0)))""")
            phase = "scene_mount"
            # R3F may render an empty scene while Suspense loads textures. Read
            # its existing store; never modify the app or infer pixel contents.
            facts = await framework_facts(page)
            while facts and (facts.get("status") == "timeout" or
                             facts.get("complete") and facts.get("inspected_nodes", 0) <= 1):
                await asyncio.sleep(0.2)
                facts = await framework_facts(page)
            phase = "animation_frame"
            await page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    except (TimeoutError, PlaywrightTimeoutError):
        return {"status": "timed_out", "phase": phase, "waited_seconds": round(time.monotonic() - started, 3),
                "notes": notes, "assurance": "readiness_diagnostics_not_visual_acceptance"}
    return {"status": "ready", "phase": phase, "waited_seconds": round(time.monotonic() - started, 3),
            "notes": notes, "assurance": "readiness_diagnostics_not_visual_acceptance"}
