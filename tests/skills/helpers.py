import json
from dataclasses import asdict
from types import SimpleNamespace

from loom.core import ok
from loom.evaluation.evidence_store import EvidenceStore
from loom.llm import LlmResponse, TokenUsage
from loom.service.contracts import canonical
from loom.service.store import SessionStore
from loom.skills.catalog import CaseCatalog
from loom.skills.store import SkillStore


class Sources:
    def __init__(self, store):
        self.store, self.sources = store, {}

    def _load(self, sid, aid):
        with self.store.transaction() as db:
            self.store._load(db, sid)
        return dict(id=aid)

    def _data(self, body):
        return self.sources[body['id']], {}


def fixture(tmp_path):
    base = SessionStore(tmp_path / 'service')
    store = SkillStore(base.path)
    source = Sources(base)
    with base.transaction() as db:
        db.execute('CREATE TABLE semantic_jobs(id TEXT PRIMARY KEY,session_id TEXT,cache_key TEXT,body TEXT)')
    return base, store, source, CaseCatalog(store, source)


def seed(base, sources, workspace, index, *, objective=None, uncertain=False, status='published', exposure=False):
    objective = objective or ['HTTP 诊断交易结算代理连接故障', 'HTTP 检查医疗预约网关请求顺序', 'HTTP 恢复交通数据上传服务解析'][index % 3] + f' 批次{index}'
    sid = base.create(f'create-{index}', dict(objective=objective, workspace=str(workspace)))['session_id']
    run, aid, eid = f'run-{index}', f'analysis-{index}', f'evaluation-{index}'
    text = f'HTTP observed recovery {index}; connection verified; environment condition {index}'
    raw = dict(id=f'event-{index}', type='tool.completed', payload=dict(run_id=run, tool_id='shell_execute', output=text))
    source = EvidenceStore(workspace / 'trace.jsonl', (canonical(raw) + '\n').encode())
    pointer = asdict(source.ref(source.events[0], 'output'))
    record = dict(id=f'memory-{index}', kind='lessons', title='HTTP connection recovery', summary='Compare a new connection with reuse',
        scope='Only observed HTTP reuse', run_id=run, unit_id='u', source_digest=source.source_sha256,
        publication_state=status, graph_revision=1, goal_ids=['g'], stage_ids=['s'], action_ids=['a'],
        trigger='HTTP reused connection fails', validation='observed_recovery', failed_approach='Reuse after invalid request',
        failure_reason='Unconsumed bytes observed', better_approach='Compare fresh and reused connections',
        citations=[dict(ref='e1', quote=text)], evidence_refs=[pointer])
    evidence = dict(id='e1', action_id='a', ref=pointer, text=text, basis='observed_output', truncated=False, redacted=False)
    body = dict(id=eid, session_id=sid, analysis_id=aid, run_id=run, model='test', created_at=f'2026-10-26T00:00:{index:02d}Z',
        state='completed', memory_scope=dict(workspace=str(workspace)), checkpoint=dict(graph_revision=1, memory=dict(
            identity=dict(source=source.source_sha256, graph_revision=1), records={record['id']: record}, units={'u': dict(evidence=[evidence])})))
    with base.transaction() as db:
        db.execute('INSERT INTO semantic_jobs VALUES(?,?,?,?)', (eid, sid, eid, canonical(body)))
        state = base._load(db, sid)
        state['run'] = dict(id=run, state='completed')
        base.append(db, state, 'run.started', dict(task_episode_id=None if uncertain else f'episode-{index}',
                    task_contract=dict(objective=objective, task_spec=state['task']['task_spec'])))
        if exposure:
            base.append(db, state, 'skill.retrieved', dict(version_ids=['old-skill']))
        base._save(db, state)
    sources.sources[aid] = source
    return body


def candidate(handles):
    return dict(title='HTTP 连接复用诊断', summary='Compare fresh and reused connections before repair', trigger='HTTP reuse failures',
        preconditions=['Observe a connection-specific difference'], guards=[dict(field='runtime', value='native_os')], required_tools=['shell_execute'],
        parameters=[dict(name='endpoint', type='string', binding='Current task endpoint')],
        steps=[dict(id='compare', instruction='Compare fresh and reused connections', claim_ids=['comparison'], depends_on=[])],
        branches=[], verification=['Verify original failure and unaffected requests'], avoid=['Do not assume a restart is necessary'],
        claims=[dict(id='comparison', text='Compare fresh and reused connections', kind='step', basis='observed_pattern',
            supports=handles, contradicts=[], unknown=[], dependencies=[], exceptions=[], verification_requirements=['Real request observations'])], gaps=[])


class Judge:
    model = 'fixture-skills'

    def __init__(self, *, fail_stage=None, forged=False):
        self.calls, self.messages = [], []
        self.fail_stage, self.forged = fail_stage, forged

    async def chat(self, messages, tools=None, cancellation=None, **kwargs):
        assert not tools
        payload = json.loads(messages[1].content)
        stage, data = payload['stage'], payload['input']
        self.calls.append(stage)
        self.messages.append(payload)
        if stage == self.fail_stage:
            raise ValueError('fixture connection interrupted')
        handles = [e['handle'] for c in data.get('cases', []) for e in c.get('evidence', [])]
        if stage == 'selection':
            result = dict(ranking=[dict(case_id=c['id'], comparability='comparable', reason='Comparable goal and starting state') for c in data['cases']])
        elif stage == 'alignment':
            result = dict(selected_cases=[c['id'] for c in data['cases']], mappings=[dict(role='compare', evidence=handles,
                differences='Connection policy differs', causal_basis='Explicit output observations')],
                condition_variations=[dict(condition=f'condition {i}', evidence=[h]) for i, h in enumerate(handles[:2])], excluded=[], gaps=[])
        elif stage.startswith('review'):
            result = dict(approved=True, issues=[], counterexample_queries=['HTTP connection'])
        else:
            result = candidate(['made-up'] if self.forged else handles)
            if 'claims' not in payload['schema']['properties']:
                result.pop('claims')
        return ok(LlmResponse(content=json.dumps(result), usage=TokenUsage(100, 100, 200)))


def semantic(judge):
    return SimpleNamespace(provider_factory=lambda model: judge, config_path=None,
                           options=lambda: dict(models=[dict(id='test', label='Fixture')], default_model='test'))
