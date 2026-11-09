import sqlite3

import pytest

from loom.autonomy.budget import BudgetProvider
from loom.core import ok
from loom.llm.api import LlmResponse, LlmStreamEvent, TokenUsage
from tests.autonomy.test_daily_budget import daily_agent


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_paid_reply_survives_settlement_lock_without_resending(tmp_path, monkeypatch, stream):
    _, _, ledger, binding = daily_agent(tmp_path)
    reply = LlmResponse(content='already paid', usage=TokenUsage(total_tokens=12))

    class Provider:
        model = 'fixture'
        calls = 0

        async def chat(self, *args, **kwargs):
            self.calls += 1
            return ok(reply)

        async def stream_chat(self, *args, **kwargs):
            self.calls += 1
            yield LlmStreamEvent(kind='completed', response=reply)

    def busy(*args):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(ledger, 'settle', busy)
    provider = Provider()
    wrapped = BudgetProvider(provider, ledger, binding, output_tokens=20)
    if stream:
        events = [event async for event in wrapped.stream_chat([])]
        assert events[-1].response is reply
    else:
        result = await wrapped.chat([])
        assert result.ok and result.value is reply
    assert provider.calls == 1
    summary = ledger.daily_summary(binding['agent_id'])
    assert summary['calls'] == 1 and summary['reserved_or_unknown'] >= 20


@pytest.mark.asyncio
async def test_provider_error_is_not_replaced_by_accounting_lock(tmp_path, monkeypatch):
    _, _, ledger, binding = daily_agent(tmp_path)

    class Provider:
        model = 'fixture'

        async def chat(self, *args, **kwargs):
            raise RuntimeError('original transport failure')

    def busy(*args):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(ledger, 'settle', busy)
    with pytest.raises(RuntimeError, match='original transport failure'):
        await BudgetProvider(Provider(), ledger, binding, output_tokens=20).chat([])
