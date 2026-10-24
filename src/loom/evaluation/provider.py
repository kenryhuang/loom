"""Bound evaluator output and transport while preserving configured model options."""

import asyncio
import json
import threading
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import is_dataclass, replace

from loom.core import ok
from loom.llm.api import OpenAIProvider
from loom.llm.request_options import materialize_request_options
from loom.tasks.config import create_provider_from_task_config


def create_evaluation_provider(config, *, model_name=None, env=None, http_client=None):
    """Apply explicit evaluation options to a private copy of the selected model."""
    created = create_provider_from_task_config(
        config, model_name=model_name or config.evaluation_model, env=env, http_client=http_client,
    )
    if not created.ok:
        return created
    provider = created.value
    options = materialize_request_options(provider.request_options)
    options.update(materialize_request_options(config.evaluation_request_options))
    return ok(replace(provider, request_options=options))


def evaluator_provider(provider, *, output_tokens, timeout_seconds, install_http=True):
    if not is_dataclass(provider) or not hasattr(provider, "max_completion_tokens"):
        return provider
    options = materialize_request_options(getattr(provider, "request_options", {}))
    # Thinking modes and effort values are model-specific constraints. Resource
    # limits must not silently replace the configured provider's valid options.
    # None explicitly removes the solver's output cap for metadata-based evaluation.
    cap = None if output_tokens is None else min(output_tokens, getattr(provider, "max_completion_tokens", None) or output_tokens)
    overrides = {"max_completion_tokens": cap, "request_options": options}
    if install_http and isinstance(provider, OpenAIProvider) and provider.http_client is None:
        overrides["http_client"] = bounded_http_client(timeout_seconds)
    return replace(provider, **overrides)


def bounded_http_client(timeout_seconds):
    async def send(url, request):
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def deliver(value, error):
            if not future.done():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(value)

        def worker():
            value, error = None, None
            try:
                req = urllib.request.Request(url, data=json.dumps(request["body"]).encode(),
                                             method=request["method"], headers=request["headers"])
                try:
                    response = urllib.request.urlopen(req, timeout=timeout_seconds)  # noqa: S310
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    value = {"status": response.code, "ok": 200 <= response.code < 300,
                             "json": json.loads(response.read().decode("utf-8"))}
            except Exception as exc:
                error = exc
            # The cancelled evaluation's event loop may already be closed.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(deliver, value, error)

        # Do not put blocking urllib I/O in asyncio's default executor: asyncio.run
        # waits for that executor on exit, defeating cancellation/time budgets.
        threading.Thread(target=worker, name="loom-evaluation-http", daemon=True).start()
        return await asyncio.wait_for(future, timeout_seconds)

    return send
