"""Session controls preserve paused work or explicitly start a fresh revision."""

import json
import time
from copy import deepcopy

import pytest

from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from tests.service.fakes import provider_factory, turn_prompt_provider_factory
from tests.service.test_controller import command, create, wait_state


def wait_worker(service):
    deadline = time.monotonic() + 5
    while service.active:
        assert time.monotonic() < deadline
        time.sleep(.02)


def test_revise_starts_clean_and_keeps_history_boundary_after_service_restart(tmp_path):
    service = LoomService(tmp_path / 'data', provider_factory=turn_prompt_provider_factory).start()
    try:
        sid = create(service, tmp_path / 'workspace', 'Original task')
        first = wait_state(service, sid, 'idle')
        wait_worker(service)
        spec = deepcopy(first['task']['task_spec'])
        spec['context'] = {'plugin': 'research_context'}
        spec['workflow'] = {'plugin': 'dynamic'}
        spec['tools']['collections'] = ['task_control']
        revise = {'command_id': 'revise', 'type': 'revise_task', 'payload': {'task': {
            'objective': 'Revised task', 'task_spec': spec, 'limits': {'max_tokens': 123456}}},
            'expected_task_revision': first['task']['revision']}
        receipt = service.submit(sid, revise)
        assert service.submit(sid, revise) == receipt
        second = wait_state(service, sid, 'idle')
        assert second['run']['id'] != first['run']['id']
        assert second['run']['task_episode_id'] != first['run']['task_episode_id']
        assert second['task']['limits']['max_tokens'] == 123456
        assert second['task']['task_spec']['workflow']['plugin'] == 'dynamic'
        reports = [m['content'] for m in second['messages'] if m['role'] == 'assistant']
        assert len(reports) == 2
        assert json.loads(reports[-1]) == {'objective': 'Revised task', 'history': []}
    finally:
        service.close()
    service = LoomService(tmp_path / 'data', provider_factory=turn_prompt_provider_factory).start()
    try:
        command(service, sid, 'submit_message', content='Follow up on the revision')
        third = wait_state(service, sid, 'idle')
        answer = json.loads(third['messages'][-1]['content'])
        assert [m['content'] for m in answer['history']] == ['Revised task', reports[-1]]
    finally:
        service.close()


@pytest.mark.parametrize('answer_while_paused', [False, True])
def test_pause_resume_preserves_question_and_context(tmp_path, answer_while_paused):
    service = LoomService(tmp_path / 'data', provider_factory=provider_factory).start()
    try:
        sid = create(service, tmp_path / 'workspace', 'question')
        pending = wait_state(service, sid, 'awaiting_input')
        wait_worker(service)
        command(service, sid, 'pause')
        paused = service.store.snapshot(sid)
        assert paused['task']['state'] == 'paused'
        if answer_while_paused:
            command(service, sid, 'answer_input', request_id=pending['input_request']['id'], answer='Payments')
            assert service.snapshot(sid)['task']['state'] == 'paused'
        command(service, sid, 'resume')
        if not answer_while_paused:
            resumed = service.store.snapshot(sid)
            assert resumed['task']['state'] == 'awaiting_input'
            assert resumed['context'] == paused['context']
            assert resumed['run']['checkpoint'] == paused['run']['checkpoint']
            assert resumed['input_request'] == paused['input_request']
            command(service, sid, 'answer_input', request_id=pending['input_request']['id'], answer='Payments')
        done = wait_state(service, sid, 'idle')
        assert done['run']['id'] == pending['run']['id']
        assert done['run']['task_episode_id'] == pending['run']['task_episode_id']
        assert sum(e['type'] == 'input.requested' for e in service.events(sid)) == 1
    finally:
        service.close()


def test_inactive_pause_rejected_and_legacy_completed_session_needs_no_reopen(tmp_path):
    service = LoomService(tmp_path / 'data')
    try:
        sid = service.create('empty', {'workspace': str(tmp_path)})['session_id']
        with pytest.raises(ServiceError, match='Only active'):
            command(service, sid, 'pause')
        command(service, sid, 'complete_task')
        command(service, sid, 'submit_message', content='A new request')
        assert service.snapshot(sid)['task']['state'] == 'queued'
    finally:
        service.close()


def test_revise_discards_saved_progress_and_pending_guidance_atomically(tmp_path):
    from loom.runtime.checkpoints import decode, encode

    service = LoomService(tmp_path / 'data')
    try:
        sid = create(service, tmp_path / 'workspace', 'Original task')
        first = service.prepare_attempt(sid)

        def suspend(state, emit, db):
            state['input_cursor'] = state['messages'][0]['seq']
            state['task']['state'] = 'paused'
            state['run'].update(state='suspended', steps=90, active_seconds=500,
                                counters=encode({'llm_calls': 100}), checkpoint={'sha256': 'must-not-load'})
            state.update(context={'obsolete': True}, plan={'old': True}, acceptance={'state': 'passed'})

        service.store.update(sid, suspend)
        command(service, sid, 'submit_message', content='Obsolete guidance')
        before = service.store.snapshot(sid)
        with pytest.raises(ServiceError):
            command(service, sid, 'revise_task', task={'limits': {'max_steps': 0}})
        assert service.store.snapshot(sid) == before
        command(service, sid, 'revise_task', task={'model': 'updated'})
        revised = service.prepare_attempt(sid)
        assert revised['run']['id'] != first['run']['id']
        assert revised['run']['task_episode_id'] != first['run']['task_episode_id']
        assert revised['run']['steps'] == revised['run']['active_seconds'] == 0
        assert decode(revised['run']['counters']) == {}
        assert revised['context'] is None and revised['run']['checkpoint'] is None
        assert 'plan' not in revised and 'acceptance' not in revised
        assert [m['content'] for m in revised['messages'] if m['seq'] > revised['input_cursor']] == ['Original task']
        assert revised['history_start_seq'] > before['messages'][-1]['seq']
    finally:
        service.close()
