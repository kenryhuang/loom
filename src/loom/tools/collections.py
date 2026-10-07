"""Explicit tool collections with implementation and effect metadata."""

from __future__ import annotations

import asyncio
import zlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from loom.core import Observation, ToolRef, err, make_loom_error, new_trace_id, now_iso, ok
from loom.runtime.execution_contracts import ToolBinding
from loom.runtime.plugin_contracts import PluginManifest
from loom.tools.contracts import CatalogTool, ToolCatalog, ToolLifecycle


class ToolCollection:
    def __init__(self, plugin_id, refs, handlers, effects, *, control=False, resource_refs=()):
        self.manifest = PluginManifest(plugin_id, "tools")
        self.config = {}
        self._bindings = tuple(
            ToolBinding(
                ref,
                f"{plugin_id}/{ref.id}",
                plugin_id,
                placement="control" if control else "execution",
                effect_kind=effects[ref.id],
                resource_refs=resource_refs,
                supports_cancel=plugin_id == "shell",
                artifact_kind=(ref.metadata or {}).get("artifact_kind"),
                evidence_fields=tuple((ref.metadata or {}).get("evidence_fields", ())),
            )
            for ref in refs
        )
        self.entrypoints = {f"{plugin_id}/{name}": handler for name, handler in handlers.items()}

    def bindings(self):
        return self._bindings

    def catalog(self, _environment=None):
        return ToolCatalog(atomic=tuple(CatalogTool(binding.ref, ToolLifecycle("atomic")) for binding in self._bindings))

    def snapshot(self):
        return self.manifest.state(self.config, {})

    def restore(self, state):
        self.manifest.restore(self.config, state)

    def reconcile(self, _operation, _evidence):
        return {"status": "unknown"}


def builtin_collection(plugin_id, request):
    from loom.tasks.runner import _task_tool_refs
    from loom.tasks.tools import make_task_tools

    groups = {"filesystem": ("read_file", "edit_file", "write_file"), "shell": ("shell_execute", "process_execute"), "task_control": ("finish",)}
    names = groups[plugin_id]
    refs = tuple(ref for ref in _task_tool_refs() if ref.id in names)
    handlers = {name: handler for name, handler in make_task_tools(request).items() if name in names}
    effects = {
        "read_file": "read_only",
        "edit_file": "side_effecting",
        "write_file": "side_effecting",
        "shell_execute": "side_effecting",
        "process_execute": "side_effecting",
        "finish": "service_control",
    }
    return ToolCollection(plugin_id, refs, handlers, effects, control=plugin_id == "task_control")


def research_collection(_request):
    async def fetch_url(value, _options=None):
        url = value.get("url", "")
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError("Research URLs must use HTTP or HTTPS")

        def fetch():
            with urlopen(Request(url, headers={"User-Agent": "Loom research/1", "Accept-Encoding": "gzip, deflate"}), timeout=20) as response:
                raw = response.read(100001)
                content_encoding = response.headers.get("Content-Encoding", "").lower().strip()
                truncated = len(raw) > 100000
                if content_encoding == "gzip" or not content_encoding and raw.startswith(b"\x1f\x8b"):
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                elif content_encoding == "deflate":
                    header = len(raw) >= 2 and (raw[0] & 15) == 8 and int.from_bytes(raw[:2], "big") % 31 == 0
                    decoder = zlib.decompressobj(zlib.MAX_WBITS if header else -zlib.MAX_WBITS)
                elif content_encoding in {"", "identity"}:
                    decoder = None
                else:
                    raise ValueError(f"Unsupported HTTP content encoding: {content_encoding}")
                if decoder:
                    raw = decoder.decompress(raw, 100001)
                    truncated = truncated or len(raw) > 100000 or bool(decoder.unconsumed_tail)
                    if not truncated and not decoder.eof:
                        raise ValueError("Incomplete compressed HTTP response")
                charset = response.headers.get_content_charset() or "utf-8"
                content = raw[:100000].decode(charset, errors="replace")
                result = {
                    "requested_url": url,
                    "url": response.url,
                    "content": content,
                    "truncated": truncated,
                    "content_type": response.headers.get("Content-Type", ""),
                    "content_encoding": content_encoding,
                    "charset": charset,
                }
                if response.headers.get_content_type() == "text/html":
                    parser = _PageText()
                    parser.feed(content)
                    result["text"] = parser.text()
                return result

        try:
            result = await asyncio.to_thread(fetch)
        except HTTPError as exc:
            exc.close()
            return err(make_loom_error(
                "HTTP_STATUS", f"HTTP {exc.code} {exc.reason} while fetching {url}",
                retryable=exc.code == 429 or 500 <= exc.code < 600,
                metadata={"url": url, "status_code": exc.code},
            ))
        except TimeoutError:
            return err(make_loom_error(
                "HTTP_TIMEOUT", f"HTTP request timed out while fetching {url}", retryable=True, metadata={"url": url, "timeout_seconds": 20},
            ))
        except URLError as exc:
            code = "HTTP_TIMEOUT" if isinstance(exc.reason, TimeoutError) else "HTTP_NETWORK_ERROR"
            return err(make_loom_error(code, f"HTTP request failed for {url}: {exc.reason}", retryable=True, metadata={"url": url}))
        except (ValueError, LookupError, zlib.error) as exc:
            return err(make_loom_error("HTTP_DECODE_FAILED", f"Cannot decode response from {url}: {exc}", retryable=False, metadata={"url": url}))
        return ok(Observation(new_trace_id(), "fetch_url", result, now_iso()))

    ref = ToolRef(
        "fetch_url",
        "Read an HTTP/HTTPS source URL. Returns readable text; original decoded content is retained in a source artifact. "
        "truncated refers to the HTTP byte limit. page.has_more refers to stored text pages: follow read_more via read_artifact, not another fetch.",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False},
        metadata={"artifact_kind": "source", "evidence_fields": ["url"]},
    )
    return ToolCollection("web_research", (ref,), {"fetch_url": fetch_url}, {"fetch_url": "read_only"})


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._hidden = 0
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self._hidden += 1
        elif not self._hidden and tag in {"p", "div", "li", "br", "h1", "h2", "h3", "section"}:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self._hidden = max(0, self._hidden - 1)
        elif not self._hidden and tag in {"p", "div", "li", "h1", "h2", "h3", "section"}:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._hidden:
            self._parts.append(data)

    def text(self):
        return "\n".join(line for part in "".join(self._parts).splitlines() if (line := " ".join(part.split())))


def document_collection(request):
    from loom.tasks.tools import make_task_tools

    workspace = request.workspace if request is not None else None
    writer = make_task_tools(request)["write_file"] if workspace is not None else None

    async def create_report(value, _options=None):
        if not isinstance(value.get("content"), str) or not value["content"].strip():
            return err(make_loom_error("VALIDATION_FAILED", "Report content must be nonempty", retryable=False))
        output = {"report": value["content"], "sources": value.get("sources", [])}
        if writer is not None or "path" in value:
            if writer is None:
                return err(make_loom_error("VALIDATION_FAILED", "Saving a report requires a writable workspace", retryable=False))
            path = value.get("path")
            if not isinstance(path, str) or not path.strip() or Path(path).is_absolute():
                return err(make_loom_error("VALIDATION_FAILED", "Report path must be a nonempty workspace-relative path", retryable=False))
            written = await writer({"path": path, "content": value["content"]}, _options)
            if not written.ok:
                return written
            output.update(written.value.value)
        return ok(Observation(new_trace_id(), "create_report", output, now_iso()))

    ref = ToolRef(
        "create_report",
        "Create a Markdown report with explicit evidence source references and retain it as a report artifact. "
        + (
            "A writable workspace is bound: path is required. Save the complete analysis before finishing and mention its path in the final answer. "
            "Creates parent directories and replaces the file at the supplied path."
            if writer is not None
            else "No writable workspace is bound: creates an artifact only; omit path."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
                "path": {"type": "string", "minLength": 1, "description": "Workspace-relative document path, e.g. reports/ai-infra-study.md."},
            },
            "required": ["content", "path"] if writer is not None else ["content"],
            "additionalProperties": False,
        },
        metadata={"artifact_kind": "report", "evidence_fields": ["sources"]},
    )
    return ToolCollection("document_outputs", (ref,), {"create_report": create_report}, {"create_report": "side_effecting" if writer else "read_only"})
