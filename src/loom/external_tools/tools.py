"""Turn pinned external tool definitions into ordinary TaskAssembly bindings."""

import asyncio
from dataclasses import replace

from loom.core import Observation, ToolRef, err, make_loom_error, new_trace_id, now_iso, ok
from loom.external_tools.errors import external_error
from loom.external_tools.store import ExternalToolStore, schema_check
from loom.external_tools.transport import call_cli, call_mcp, with_control
from loom.service.contracts import ServiceError
from loom.tools.collections import ToolCollection


def external_collection(request):
    definitions = list(request.metadata.get("external_tool_bindings", []))
    directory = request.metadata.get("external_tools_directory")
    refs, handlers, effects = [], {}, {}
    for definition in definitions:
        identifier = definition["id"]
        refs.append(
            ToolRef(
                identifier,
                f"{definition['connector_name']} / {definition['name']}: {definition['description']}",
                input_schema=definition["input_schema"],
                metadata={"artifact_kind": "external_tool_result", "external_tool_id": identifier, "connector_id": definition["connector_id"]},
            )
        )
        effects[identifier] = definition["effect_kind"]

        async def invoke(value, _options=None, binding=definition):
            try:
                if not directory:
                    raise ServiceError("External tools require a service registry", 422)
                schema_check(binding["input_schema"], value, validate=True)
                connector = ExternalToolStore(directory).connection_for(binding)
                if connector["kind"] == "cli":
                    result, failed = await with_control(call_cli(connector["config"], value, request.workspace, _options), _options)
                else:
                    result, failed = await with_control(call_mcp(connector["config"], binding["name"], value), _options)
                if failed:
                    return err(
                        make_loom_error(
                            "EXTERNAL_TOOL_FAILED",
                            f"{binding['name']} reported an error",
                            retryable=False,
                            metadata={"output": result, "external_tool_id": binding["id"]},
                        )
                    )
                return ok(
                    Observation(
                        new_trace_id(),
                        binding["id"],
                        {"external_tool_id": binding["id"], "tool_name": binding["name"], "connector_id": binding["connector_id"], "result": result},
                        now_iso(),
                    )
                )
            except ServiceError as exc:
                return err(make_loom_error(exc.code, str(exc), retryable=False))
            except TimeoutError:
                return err(make_loom_error("EXTERNAL_TOOL_TIMEOUT", f"{binding['name']} exceeded its configured timeout", retryable=False))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = external_error(exc, binding["name"])
                return err(make_loom_error(failure.code, str(failure), retryable=False))

        handlers[identifier] = invoke
    collection = ToolCollection("external_tools", tuple(refs), handlers, effects)
    # CLI process groups can be reaped locally; cancellation of a remote side effect stays uncertain.
    collection._bindings = tuple(
        replace(b, supports_cancel=True) if next(d for d in definitions if d["id"] == b.ref.id)["kind"] == "cli" else b for b in collection.bindings()
    )
    collection.config = {"bindings": definitions}
    return collection
