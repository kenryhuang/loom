import asyncio
import json
from dataclasses import replace

import pytest

from loom.core import make_loom_error
from loom.llm import LlmMessage, LlmResponse, OpenAIProvider
from loom.llm.structured import (
    ProtocolError,
    ProtocolExhausted,
    StructuredProtocol,
    UnsupportedFormat,
    output_mode,
    parse_response,
    protocol_provider,
    request_structured,
    unsupported_format,
)

PROTOCOL = StructuredProtocol('decision', 1, {'type': 'object', 'properties': {'answer': {'type': 'string'}},
                                             'required': ['answer'], 'additionalProperties': False})


@pytest.mark.parametrize('raw', ['', '{', '{"answer":"a","answer":"b"}', '{"answer":NaN}', '{"answer":1e999}',
                                 '{"answer":"a"} trailing', '{} {}', '[' * 70 + '0' + ']' * 70])
def test_malformed_response_never_becomes_a_valid_object(raw):
    with pytest.raises(ProtocolError, match='JSON'):
        parse_response(LlmResponse(content=raw))


def test_complete_fence_is_normalized_but_truncated_or_refused_responses_are_rejected():
    value, changes = parse_response(LlmResponse(content='```json\n{"answer":"ok"}\n```'))
    assert value == {'answer': 'ok'} and changes[0]['rule'] == 'unwrap_json_fence'
    for reason, code in [('length', 'truncated'), ('content_filter', 'refused')]:
        with pytest.raises(ProtocolError) as error:
            parse_response(LlmResponse(content='{"answer":"looks complete"}', finish_reason=reason))
        assert error.value.code == code


@pytest.mark.asyncio
async def test_schema_correction_has_paths_and_preserves_context():
    requests, events = [], []

    async def send(payload, attempt, mode):
        requests.append(payload)
        return LlmResponse(content=json.dumps({'answer': 3 if attempt == 1 else 'fixed'}))

    async def event(kind, payload):
        events.append((kind, payload))

    result = await request_structured(protocol=PROTOCOL, payload={'goal': 'original'}, send=send, event=event)
    assert result.value == {'answer': 'fixed'} and result.attempts == 2
    assert requests[1]['goal'] == 'original'
    assert requests[1]['repair']['issues'][0]['path'] == '/answer'
    assert requests[1]['response_contract']['schema'] == PROTOCOL.schema
    assert [kind for kind, _ in events] == ['proposed', 'rejected', 'retrying', 'proposed', 'completed']


@pytest.mark.asyncio
async def test_exhaustion_is_bounded_and_non_object_response_cannot_crash_domain_validator():
    calls = []

    async def send(payload, attempt, mode):
        calls.append(attempt)
        return LlmResponse(content='null')

    with pytest.raises(ProtocolExhausted) as error:
        await request_structured(protocol=PROTOCOL, payload={}, send=send, validate=lambda v: v.get('answer'))
    assert error.value.code == 'invalid_schema' and calls == [1, 2]


@pytest.mark.asyncio
async def test_transport_cancellation_and_budget_failures_do_not_get_format_retries():
    for exc in (asyncio.CancelledError(), TimeoutError('network'), RuntimeError('budget exhausted')):
        calls = []

        async def send(payload, attempt, mode, exc=exc, calls=calls):
            calls.append(attempt)
            raise exc

        with pytest.raises(type(exc)):
            await request_structured(protocol=PROTOCOL, payload={}, send=send)
        assert calls == [1]


@pytest.mark.asyncio
async def test_one_capability_fallback_keeps_local_schema_validation():
    modes = []

    async def send(payload, attempt, mode):
        modes.append(mode)
        if attempt == 1:
            raise UnsupportedFormat()
        return LlmResponse(content=json.dumps({'answer': 3 if attempt == 2 else 'valid'}))

    result = await request_structured(protocol=PROTOCOL, payload={}, send=send, mode='json_schema')
    assert result.value == {'answer': 'valid'} and modes == ['json_schema', 'text', 'text']
    assert unsupported_format(make_loom_error('LLM_FAILED', 'response_format is not supported', retryable=False, cause={'status': 400}))
    for status in [401, 429, 500]:
        assert not unsupported_format(make_loom_error('LLM_FAILED', 'response_format is not supported', retryable=False, cause={'status': status}))
    assert not unsupported_format(make_loom_error('LLM_FAILED', 'Invalid schema definition', retryable=False, cause={'status': 400}))


@pytest.mark.asyncio
async def test_request_schema_reaches_http_without_mutating_shared_provider():
    bodies = []

    async def http(url, request):
        bodies.append(request['body'])
        return {'ok': True, 'status': 200, 'json': {'choices': [{'message': {'content': '{"answer":"ok"}'}, 'finish_reason': 'stop'}]}}

    shared = OpenAIProvider('test', 'qwen3.8-max', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1', http_client=http)
    other = replace(PROTOCOL, name='other', schema={'type': 'object', 'properties': {}, 'additionalProperties': False})
    assert output_mode(shared) == 'json_schema'
    first, second = [protocol_provider(shared, p, 'json_schema') for p in (PROTOCOL, other)]
    await asyncio.gather(first.chat([LlmMessage('user', 'JSON')]), second.chat([LlmMessage('user', 'JSON')]))
    assert {b['response_format']['json_schema']['name'] for b in bodies} == {'decision_v1', 'other_v1'}
    assert all(b['response_format']['json_schema']['strict'] for b in bodies)
    assert not shared.request_options
    assert output_mode(replace(shared, base_url='https://example.com/v1')) == 'text'


@pytest.mark.asyncio
async def test_provider_refusal_is_not_misreported_as_a_json_format_failure():
    async def http(url, request):
        return {'ok': True, 'json': {'choices': [{'message': {'content': None, 'refusal': 'Declined'}, 'finish_reason': 'stop'}]}}

    provider = OpenAIProvider('test', 'fixture', http_client=http)
    response = (await provider.chat([LlmMessage('user', 'JSON')])).unwrap()
    with pytest.raises(ProtocolError) as error:
        parse_response(response)
    assert error.value.code == 'refused' and not error.value.retryable


@pytest.mark.asyncio
async def test_truncated_retry_can_be_delegated_to_smaller_work_scheduler():
    for retry, expected in [(False, [1]), (True, [1, 2])]:
        calls = []

        async def send(payload, attempt, mode, calls=calls):
            calls.append(attempt)
            return LlmResponse(content='{', finish_reason='length')

        with pytest.raises(ProtocolExhausted) as error:
            await request_structured(protocol=PROTOCOL, payload={}, send=send, retry_truncated=retry)
        assert error.value.code == 'truncated' and calls == expected
