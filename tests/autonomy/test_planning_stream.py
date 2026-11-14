"""Planning progress is durable and bounded without publishing unvalidated work."""

import asyncio
import json

import pytest

from loom.autonomy.planning_stream import PREVIEW_CHARS, planning_response
from loom.core import ok
from loom.llm.api import LlmResponse, LlmStreamEvent


def prepare(facade, aid):
    with facade.store.sessions.transaction() as db:
        facade.store.put(db, 'agent_runs', dict(id='stream-run', agent_id=aid, objective_id='quality', kind='unified_plan', state='planning'))


def read(facade, aid):
    with facade.store.sessions.transaction() as db:
        return facade.store.get(db, 'agent_runs', 'stream-run', aid)


def test_stream_is_visible_before_completion_and_does_not_publish_work(agent_service):
    _, facade, aid, _ = agent_service
    prepare(facade, aid)
    content = '草案' * PREVIEW_CHARS

    class Provider:
        model = 'test-stream'

        async def stream_chat(self, *args, **kwargs):
            yield LlmStreamEvent(kind='content.delta', content_delta='first')
            progress = read(facade, aid)
            assert progress['state'] == 'planning'
            assert progress['stream']['channels']['content']['text'] == 'first'
            assert 'outcome' not in progress
            yield LlmStreamEvent(kind='content.delta', content_delta=content)
            yield LlmStreamEvent(kind='completed', response=LlmResponse(content='first' + content))

        async def chat(self, *args, **kwargs):
            pytest.fail('Streaming must not cause a second, non-streaming request')

    result = asyncio.run(planning_response(facade.store, {'id': aid}, 'stream-run', Provider(), [], 1, 'json'))
    assert result.ok and result.value.content == 'first' + content
    run = read(facade, aid)
    assert run['state'] == 'planning' and run['stream']['state'] == 'received'
    channel = run['stream']['channels']['content']
    assert channel == dict(text=content[-PREVIEW_CHARS:], characters=len(content) + 5, truncated=True)
    with facade.store.sessions.transaction() as db:
        events = [json.loads(row[0]) for row in db.execute('SELECT body FROM agent_events WHERE agent_id=?', (aid,))]
        assert all('草案' not in json.dumps(event, ensure_ascii=False) for event in events)
        assert not facade.store.rows(db, 'agent_work_packages', aid)


def test_failed_stream_retains_partial_output_and_retry_replaces_it(agent_service):
    _, facade, aid, _ = agent_service
    prepare(facade, aid)

    class Broken:
        model = 'test-stream'

        async def stream_chat(self, *args, **kwargs):
            yield LlmStreamEvent(kind='content.delta', content_delta='partial')
            raise RuntimeError('connection lost')

    result = asyncio.run(planning_response(facade.store, {'id': aid}, 'stream-run', Broken(), [], 1, 'json'))
    assert not result.ok
    previous = read(facade, aid)['stream']
    assert previous['state'] == 'failed' and previous['channels']['content']['text'] == 'partial'

    class NonStreaming:
        model = 'test-chat'
        calls = 0

        async def chat(self, *args, **kwargs):
            self.calls += 1
            return ok(LlmResponse(content='corrected'))

    provider = NonStreaming()
    result = asyncio.run(planning_response(facade.store, {'id': aid}, 'stream-run', provider, [], 2, 'prompt'))
    current = read(facade, aid)['stream']
    assert result.ok and provider.calls == 1
    assert current['request_id'] != previous['request_id']
    assert current['attempt'] == 2 and not current['streaming']
    assert current['channels']['content']['text'] == 'corrected'


def test_superseded_run_cannot_be_overwritten_by_late_stream(agent_service):
    _, facade, aid, _ = agent_service
    prepare(facade, aid)

    class Provider:
        model = 'test-stream'

        async def stream_chat(self, *args, **kwargs):
            yield LlmStreamEvent(kind='content.delta', content_delta='before')
            with facade.store.sessions.transaction() as db:
                run = facade.store.get(db, 'agent_runs', 'stream-run', aid)
                run['state'] = 'superseded'
                facade.store.put(db, 'agent_runs', run)
            yield LlmStreamEvent(kind='content.delta', content_delta='after')
            yield LlmStreamEvent(kind='completed', response=LlmResponse(content='beforeafter'))

    asyncio.run(planning_response(facade.store, {'id': aid}, 'stream-run', Provider(), [], 1, 'json'))
    run = read(facade, aid)
    assert run['state'] == 'superseded'
    assert run['stream']['channels']['content']['text'] == 'before'


def test_stream_keeps_single_budget_reservation_and_settles_actual_usage(tmp_path):
    from loom.autonomy.budget import BudgetProvider
    from loom.llm.api import TokenUsage
    from tests.autonomy.test_daily_budget import daily_agent

    store, _, ledger, binding = daily_agent(tmp_path)
    aid = binding['agent_id']
    with store.sessions.transaction() as db:
        store.put(db, 'agent_runs', dict(id='budget-stream', agent_id=aid, state='planning'))

    class Provider:
        model = 'test-stream'

        async def stream_chat(self, *args, **kwargs):
            yield LlmStreamEvent(kind='content.delta', content_delta='draft')
            yield LlmStreamEvent(kind='completed', response=LlmResponse(content='draft', usage=TokenUsage(total_tokens=12)))

    wrapped = BudgetProvider(Provider(), ledger, binding, category='stage_planning', output_tokens=20)
    result = asyncio.run(planning_response(store, {'id': aid}, 'budget-stream', wrapped, [], 1, 'text'))
    assert result.ok
    summary = ledger.daily_summary(aid)
    assert summary['calls'] == 1 and summary['used'] == 12


def test_preview_lock_does_not_discard_paid_response(agent_service, monkeypatch):
    import sqlite3

    from loom.autonomy.planning_stream import PlanningStream

    _, facade, aid, _ = agent_service
    prepare(facade, aid)

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    class Provider:
        model = 'test-stream'

        async def stream_chat(self, *args, **kwargs):
            yield LlmStreamEvent(kind='content.delta', content_delta='draft')
            yield LlmStreamEvent(kind='completed', response=LlmResponse(content='draft'))

    monkeypatch.setattr(PlanningStream, '_flush', busy)
    result = asyncio.run(planning_response(facade.store, {'id': aid}, 'stream-run', Provider(), [], 1, 'text'))
    assert result.ok and result.value.content == 'draft'
