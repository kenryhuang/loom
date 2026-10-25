import json

from loom.core import ok
from loom.llm import LlmResponse, LlmToolCall
from tests.acceptance_fakes import FixtureVerifier


def fact(identifier='memory:planet', **changes):
    return dict(id=identifier, kind='facts', unit_id='u', run_id='historical-run', source_digest='trace-digest', graph_revision=1,
                title='行星渲染 duplicate planets', summary='Planet data had duplicate IDs; inspect the current array before changing rendering.',
                scope='Solar system workspace; historical snapshot', confidence='high', basis='observed', category='project',
                revalidate_when='After source or environment changes', goal_ids=['g'], stage_ids=['s'], action_ids=['a'],
                citations=[{'ref': 'e1', 'quote': 'duplicate planet IDs'}],
                evidence_refs=[{'line_number': 12, 'field_path': 'output', 'source_sha256': 'trace-digest'}], **changes)


def seed(store, workspace, *, records=None, evaluation='eval-memory', session=None, state='incomplete', frozen_scope=True):
    if workspace:
        workspace.mkdir(parents=True, exist_ok=True)
    sid = session or store.create(evaluation, {'workspace': str(workspace) if workspace else None})['session_id']
    records = [fact()] if records is None else records
    body = dict(id=evaluation, session_id=sid, run_id='historical-run', created_at='2026-10-24T01:00:00Z', state=state,
                checkpoint={'memory': {'identity': {'source': 'trace-digest'}, 'records': {r['id']: r for r in records},
                                       'units': {'u': {'rejected_records': [{'record': fact('rejected')}]}}}})
    if frozen_scope:
        body['memory_scope'] = {'workspace': str(workspace) if workspace else None}
    with store.transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS semantic_jobs(id TEXT PRIMARY KEY, session_id TEXT, cache_key TEXT, body TEXT)')
        db.execute('INSERT INTO semantic_jobs VALUES(?,?,?,?)', (evaluation, sid, evaluation, json.dumps(body)))
    return sid


class MemoryProvider:
    verification_provider = FixtureVerifier()
    model = 'memory-smoke'

    def __init__(self):
        self.calls = 0
        self.seen = []

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        self.calls += 1
        self.seen.append(messages)
        text = '\n'.join(m.content or '' for m in messages)
        assert 'Historical Deep eval memory' in text
        assert 'eval-memory' in text and 'trace-digest' in text
        names = {t['function']['name'] for t in tools or []}
        assert {'memory_search', 'memory_read'} <= names
        if self.calls == 1:
            name, args = 'memory_search', {'query': 'duplicate planets'}
        elif self.calls == 2:
            assert 'memory:planet' in text
            name, args = 'memory_read', {'evaluation_id': 'eval-memory', 'record_id': 'memory:planet'}
        else:
            assert 'duplicate planet IDs' in text
            name, args = 'finish', {'report': 'Memory retrieved with original citations; historical facts require fresh verification.'}
        return ok(LlmResponse('', (LlmToolCall(f'memory-call-{self.calls}', name, json.dumps(args)),)))


def memory_provider_factory(state):
    return MemoryProvider()


class PausedMemoryProvider:
    verification_provider = FixtureVerifier()
    model = 'paused-memory-smoke'

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        results = [m for m in messages if m.role == 'tool']
        if not results:
            name, args = 'request_input', {'question': 'Continue after evaluation?'}
        elif len(results) == 1:
            assert any(m.name == 'deep_eval_memory' and 'eval-memory' in m.content for m in messages)
            name, args = 'memory_read', {'evaluation_id': 'eval-memory', 'record_id': 'memory:planet'}
        else:
            assert 'duplicate planet IDs' in '\n'.join(m.content for m in results)
            name, args = 'finish', {'report': 'Resumed using newly evaluated memory without repeating the question.'}
        return ok(LlmResponse('', (LlmToolCall(f'paused-{len(results)}', name, json.dumps(args)),)))


def paused_memory_provider_factory(state):
    return PausedMemoryProvider()
