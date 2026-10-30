import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from loom.campaigns.artifacts import ArtifactStore
from loom.campaigns.experiments import TrialExecution
from loom.llm import build_messages
from loom.skills.contracts import canonical, digest
from loom.skills.induction import Induction
from loom.skills.runtime import RuntimeSkills
from loom.skills.validation import SkillPairedValidation
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context
from tests.skills.helpers import Judge
from tests.skills.test_induction import job, ready
from tests.tasks.test_task_runner import RecordingTraceSink


def version_fixture(tmp_path):
    base, store, sources, catalog = ready(tmp_path)
    result = asyncio.run(Induction(store, catalog, Judge()).run(job(store, store.all_cases(str(tmp_path)))))
    return base, store, sources, store.version(result['version_id'])


def test_runtime_explicit_trial_guards_usage_and_revocation(tmp_path):
    base, store, _, version = version_fixture(tmp_path)
    sid = base.create('runtime', dict(objective='HTTP connection', workspace=str(tmp_path)))['session_id']
    context = make_task_context(TaskRequest('HTTP connection', workspace=tmp_path), plan_mode='off').unwrap()
    sink = RecordingTraceSink()
    runtime = SimpleNamespace(trace_sink=sink, trace_id='trace')
    skills = RuntimeSkills(base.path, workspace=tmp_path, session_id=sid)
    skills.configure(['shell_execute'], workspace_access='read-write', runtime='native_os')

    async def run():
        first = await skills.attach(context, runtime)
        assert 'retrieved_skills' not in first.metadata  # Candidates do not auto-inject.
        skills.trials.add(version['id'])
        second = await skills.attach(first, runtime)
        assert version['id'] in second.metadata['retrieved_skills']['candidates'][0]['id']
        assert version['id'] not in build_messages(second)[0].content
        handlers = skills.handlers()
        bad = await handlers['skill_select'](dict(version_id=version['id'], precondition_call_ids=['fabricated']))
        assert not bad.ok
        with base.transaction() as db:
            state = base._load(db, sid)
            state['run'] = dict(id=context.run_id, state='running')
            base.append(db, state, 'tool.completed', dict(tool_call_id='actual', tool_id='shell_execute', output='fresh and reused differ'))
            base._save(db, state)
        chosen = await handlers['skill_select'](dict(version_id=version['id'], precondition_call_ids=['actual']))
        assert chosen.ok
        recorded = await handlers['skill_record_step'](dict(version_id=version['id'], step_id='compare', tool_call_ids=['actual']))
        assert recorded.ok
        third = await skills.attach(second, runtime)
        assert third.metadata['retrieved_skills']['selected']
        with base.transaction() as db:
            state = base._load(db, sid)
            base.append(db, state, 'acceptance.gate.passed', dict(check_refs=['real-check']))
            assert db.execute("SELECT COUNT(*) FROM skill_uses WHERE kind='outcome'").fetchone()[0] == 1
        current = store.version(version['id'])
        store.transition(version['id'], 'suspended', current['revision'])
        final = await skills.attach(third, runtime)
        assert tuple(final.metadata['retrieved_skills']['withdrawn_versions']) == (version['id'],)
        assert not final.metadata['retrieved_skills']['selected']
        assert not skills.selected
    asyncio.run(run())


def task_set():
    return [dict(task_fingerprint=f'task-{i}', family_id=f'unseen-family-{i}', split_group_id=f'unseen-group-{i}',
                 phase='validation' if i < 2 else 'holdout', snapshot_digest=digest(['snapshot', i]),
                 acceptance_digest=digest(['verifier', i]), snapshot_ref=f'sealed-snapshot-{i}',
                 contract=dict(objective=['HTTP inspect websocket upgrade header forwarding',
                                          'HTTP audit streaming compression compatibility',
                                          'HTTP characterize distributed session affinity routing',
                                          'HTTP diagnose asynchronous upstream timeout policy'][i]),
                 skill_versions_seen=[], prior_memory_refs=[]) for i in range(4)]


def environment():
    return {k: digest(k) for k in ('environment_digest', 'solver_digest', 'tools_digest', 'permissions_digest',
                                  'evaluator_digest', 'knowledge_digest', 'memory_digest', 'budget_digest')}


def test_paired_real_receipts_minimum_pairs_isolation_and_activation(tmp_path):
    _, store, _, version = version_fixture(tmp_path)
    artifacts = ArtifactStore(tmp_path / 'experiments')
    calls = []

    async def execute(**call):
        calls.append(call)
        assert not call['shared_memory'] and not call['automatic_skills']
        assert call['skill'] is None if call['side'] == 'baseline' else call['skill']['version_id'] == version['id']
        # These are deliberately synthetic execution fixtures; production requires a real snapshot backend.
        trace = artifacts.publish_bytes(canonical(dict(side=call['side'], trial=call['trial_id'])).encode(),
                                        kind='trace', schema_version='trace.v1').unwrap()
        evaluation = artifacts.publish_bytes(canonical(dict(schema_version='loom.evaluation.bundle.v1',
            bundle_id=call['trial_id'], acceptance=True)).encode(), kind='evaluation', schema_version='loom.evaluation.bundle.v1').unwrap()
        return TrialExecution.success(call['trial_id'], call['side'], {'acceptance': 1}, evaluation, trace)

    validator = SkillPairedValidation(store, artifacts, execute, tmp_path / 'trials')
    result = asyncio.run(validator.run(version['id'], task_set(), environment()))
    assert result['publication_passed'] and len(calls) == 24
    assert len({c['directory'] for c in calls}) == 24
    assert all(p['metrics'][0]['valid_pairs'] == 6 for p in result['phases'].values())
    assert store.transition(version['id'], 'active', version['revision'])['state'] == 'active'


def test_validation_rejects_contamination_missing_snapshot_and_retains_failures(tmp_path):
    _, store, _, version = version_fixture(tmp_path)
    artifacts = ArtifactStore(tmp_path / 'experiments')
    calls = []

    def fail(**call):
        calls.append(call)
        return TrialExecution.infrastructure_failure(call['trial_id'], call['side'], 'SNAPSHOT_UNAVAILABLE')

    validator = SkillPairedValidation(store, artifacts, fail, tmp_path / 'trials')
    tasks = task_set()
    invalid = deepcopy(tasks)
    invalid[0]['prior_memory_refs'] = ['held-out-result-leak']
    with pytest.raises(ValueError, match='exposure'):
        asyncio.run(validator.run(version['id'], invalid, environment()))
    invalid = deepcopy(tasks)
    invalid[0]['snapshot_ref'] = ''
    with pytest.raises(ValueError, match='cannot be replayed'):
        asyncio.run(validator.run(version['id'], invalid, environment()))
    assert not calls
    result = asyncio.run(validator.run(version['id'], tasks, environment()))
    assert not result['publication_passed']
    assert result['phases']['validation']['failures']
    assert 'holdout' not in result['phases']
    with pytest.raises(ValueError, match='executable validation'):
        store.transition(version['id'], 'active', version['revision'])


def test_optimize_backend_executes_fresh_snapshot_and_actual_verifier(tmp_path):
    from dataclasses import replace

    from loom.optimize.task_sets import _snapshot_workspace
    from loom.skills.execution import OptimizeSkillBackend, sealed_task
    from tests.optimize.test_trial_executor import RecordingSolverProvider, _prepared_task
    from tests.skills.helpers import candidate

    prepared, entry = _prepared_task(tmp_path)
    original = prepared.task_by_fingerprint[entry.task_fingerprint]
    snapshot = _snapshot_workspace(original.snapshot.path, tmp_path / 'sealed').unwrap()
    original = replace(original, snapshot=snapshot)
    prepared.task_by_fingerprint[entry.task_fingerprint] = original
    sealed = sealed_task(original, family_id='fresh-family', split_group_id='fresh-group', phase='validation')
    provider = RecordingSolverProvider()
    artifacts = ArtifactStore(tmp_path / 'artifacts')
    backend = OptimizeSkillBackend(prepared, artifacts, provider, environment=environment(), timeout_seconds=15)
    async def execute():
        results = []
        for side in ('baseline', 'candidate'):
            directory = tmp_path / side
            directory.mkdir()
            results.append(await backend(side=side, task=sealed, seed=1, trial_id='paired-real', directory=directory,
                environment=environment(), skill=None if side == 'baseline' else dict(version_id='candidate', contract=candidate([])),
                shared_memory=False, shared_knowledge=False, automatic_skills=False))
        return results
    results = asyncio.run(execute())
    assert all(r.failure_kind is None and r.metrics['acceptance'] == 1 for r in results)
    assert 'Explicit trial method' not in provider.calls[0]['system']
    assert any('Explicit trial method' in call['system'] for call in provider.calls[1:])
    assert results[0].trace_ref.sha256 != results[1].trace_ref.sha256
    assert snapshot.path.joinpath('README.md').read_text() == '# Trial workspace\n'


def test_unavailable_skill_database_does_not_break_the_task(tmp_path):
    database = tmp_path / 'broken.sqlite'
    database.write_text('not a database')
    skills = RuntimeSkills(database, workspace=tmp_path, session_id='current')
    context = make_task_context(TaskRequest('Inspect', workspace=tmp_path), plan_mode='off').unwrap()
    sink = RecordingTraceSink()
    runtime = SimpleNamespace(trace_sink=sink, trace_id='trace')
    result = asyncio.run(skills.attach(context, runtime))
    assert result.goal == context.goal
    assert not skills.refs()
    assert sink.events[-1]['type'] == 'skill.unavailable'
