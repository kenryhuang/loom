"""Actionable transport errors without reflecting URLs, headers or remote bodies."""

import httpx

from loom.service.contracts import ServiceError


def external_error(exc, operation):
    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            for child in error.exceptions:
                yield from leaves(child)
        else:
            yield error

    errors = list(leaves(exc))
    for error in errors:
        if isinstance(error, ServiceError):
            return error
    for error in errors:
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            advice = {
                401: "Check the API key and full Authorization value (Bearer plus the key).",
                403: "Check service activation and API key permissions.",
                404: "Check the endpoint URL and selected MCP transport.",
                405: (
                    "The endpoint rejected the HTTP method. Check the MCP transport; "
                    "a legacy SSE service needs transport=sse and its SSE URL, or a server-side upgrade."
                ),
                429: "The service rate limit or quota was reached; check usage before retrying.",
            }.get(status, "Check the provider service status and endpoint configuration.")
            return ServiceError(f"{operation} failed: HTTP {status}. {advice}", 502, "EXTERNAL_TOOL_HTTP_ERROR")
    if any(isinstance(error, (TimeoutError, httpx.TimeoutException)) for error in errors):
        return ServiceError(f"{operation} timed out; check connectivity and timeout_seconds.", 504, "EXTERNAL_TOOL_TIMEOUT")
    if any(isinstance(error, httpx.RequestError) for error in errors):
        return ServiceError(
            f"{operation} failed to connect or exchange HTTP data; check DNS, TLS, network and proxy settings.", 502, "EXTERNAL_TOOL_CONNECTION_ERROR"
        )
    if any(isinstance(error, FileNotFoundError) for error in errors):
        return ServiceError(f"{operation} failed: executable or working directory not found.", 422, "EXTERNAL_TOOL_PROCESS_ERROR")
    kinds = ", ".join(dict.fromkeys(type(error).__name__ for error in errors))[:200]
    return ServiceError(f"{operation} failed ({kinds}); check server compatibility and configuration.", 502, "EXTERNAL_TOOL_ERROR")
