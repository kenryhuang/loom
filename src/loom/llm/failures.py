"""Classify provider transport exceptions by type, never by error text."""

import http.client
import socket
import ssl
import urllib.error

from loom.core import make_loom_error


def provider_failure(exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    status = getattr(exc, 'status', None)
    code, retryable = 'LLM_FAILED', status is None or status == 429 or status >= 500
    if isinstance(reason, TimeoutError):
        code, retryable = 'TIMEOUT', True
    elif isinstance(reason, ssl.SSLCertVerificationError):
        code, retryable = 'LLM_TLS_ERROR', False
    elif isinstance(reason, (ssl.SSLEOFError, ConnectionError, socket.gaierror, http.client.IncompleteRead)):
        code, retryable = 'LLM_TRANSPORT_ERROR', True
    elif isinstance(reason, ssl.SSLError):
        code, retryable = 'LLM_TLS_ERROR', False
    elif isinstance(exc, urllib.error.URLError) and status is None:
        code, retryable = 'LLM_TRANSPORT_ERROR', True
    message = str(exc) or ('LLM request timed out' if code == 'TIMEOUT' else type(exc).__name__)
    cause = dict(name=type(exc).__name__, message=message)
    if reason is not exc:
        cause['reason'] = dict(name=type(reason).__name__, message=str(reason))
    if isinstance(status, int):
        cause.update(status=status, body=getattr(exc, 'body', {}))
    return make_loom_error(code, message, retryable=retryable, cause=cause)
