
from copy import deepcopy

import pytest

from loom.autonomy.acceptance import refs
from loom.service.contracts import ServiceError
from loom.service.controller import LoomService
from tests.autonomy.helpers import evaluation_provider, role, solver_provider
from tests.autonomy.test_execution_flow import until


def proposal():
    return dict(action="propose_stage", reason="Inspect implementation before choosing delivery", source_refs=[], baseline="Implementation unknown",
        stage=dict(kind="discovery", title="Project baseline", expected_outcome="Evidence-backed implementation report",
                   expected_contribution="Identify a concrete next delivery", estimated_cost="One bounded inspection", delivery_key="baseline",
                   scope=["Current workspace"], non_goals=["Modify source files"], assumptions=[], acceptance=["Cite recorded implementation evidence"],
                   stop_conditions=["Workspace unavailable"], goals=[dict(key="inspect", objective="Inspect current source and report facts",
                       acceptance=["Cite notes.txt"], depends_on=[], work_packages=[dict(key="inspect", objective="Read notes.txt and report evidence",
                           acceptance=["Cite notes.txt"], environment="inspect", execution_tools=["read_file"], estimated_seconds=120,
                           verification=dict(criteria=[dict(id="read", description="Read implementation evidence", verifier="tool_call", scope=[],
                               check=dict(tool_id="read_file", min_calls=1), required=True, required_tools=["read_file"])],
                               unresolved_requirements=[]))])]), future=[])


@pytest.fixture
def unified(tmp_path):
    (tmp_path / "notes.txt").write_text("Session fixture evidence: implementation facts.")
    service = LoomService(tmp_path / "data", autonomy_enabled=True, provider_factory=solver_provider, evaluation_provider_factory=evaluation_provider,
                          agent_plan_provider=lambda *_: proposal())
    facade = service.background.autonomy
    raw = role(tmp_path)
    raw["long_term_objectives"][0]["stage_planning"] = {"mode": "autonomous"}
    agent = facade.store.create("create", raw)
    facade.store.command(agent["id"], "enable", "enable", 1)
    yield service, facade, agent["id"], tmp_path
    service.close()


def test_default_engine_publishes_whole_discovery_stage_before_dispatch(unified):
    service, facade, aid, _ = unified
    service.setup_recommender = lambda *_a, **_k: pytest.fail("Compiled dispatch must never recommend tools")
    facade.supervisor.scheduler.advance(aid, manual=True)
    snapshot = facade.store.snapshot(aid)
    assert snapshot["planning_engine"] == 2
    assert len(snapshot["stages"]) == len(snapshot["goals"]) == len(snapshot["work_packages"]) == len(snapshot["plan_revisions"]) == 1
    stage, goal, work = snapshot["stages"][0], snapshot["goals"][0], snapshot["work_packages"][0]
    assert stage["kind"] == "discovery" and goal["stage_id"] == work["stage_id"] == stage["id"]
    assert work["state"] == "pending" and work["verification_plan"]
    facade.supervisor.scheduler.advance(aid, manual=True)
    work = facade.store.snapshot(aid)["work_packages"][0]
    session = service.snapshot(work["session_id"])
    assert session["autonomy"]["verification_plan"] == work["verification_plan"]
    assert session["autonomy"]["contract_digest"] == work["contract_digest"]
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert len(service.store.list_sessions()) == 1


def test_invalid_proposal_creates_no_partial_queue_and_does_not_repeat(unified):
    service, facade, aid, _ = unified
    calls = []
    def invalid(*_):
        calls.append(True)
        value = proposal()
        value["stage"]["goals"][0]["work_packages"][0]["execution_tools"] = ["shell_execute"]
        return value
    service.agent_plan_provider = invalid
    for _ in range(3):
        facade.supervisor.scheduler.advance(aid, manual=True)
    snapshot = facade.store.snapshot(aid)
    assert not snapshot["stages"] and not snapshot["goals"] and not snapshot["work_packages"]
    assert len(calls) == 1
    state = snapshot["objective_planning"][0]
    assert "shell_execute" in state["reason"] and state["blocker"]["allowed_actions"][0]["type"] == "replan"


def test_plan_cannot_commit_after_pause(unified):
    service, facade, aid, _ = unified
    def pause(*_):
        facade.store.command(aid, "pause", "pause", facade.store.snapshot(aid)["revision"])
        return proposal()
    service.agent_plan_provider = pause
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert not facade.store.snapshot(aid)["work_packages"]


def test_revision_retains_old_contract_and_publishes_new_one_atomically(unified):
    _, facade, aid, _ = unified
    facade.supervisor.scheduler.advance(aid, manual=True)
    snapshot = facade.store.snapshot(aid)
    state = snapshot["objective_planning"][0]
    old = snapshot["work_packages"][0]
    from loom.autonomy.plan_commands import request_revision
    request_revision(facade, aid, "quality", dict(command_id="revise", expected_revision=state["revision"], reason="Inspect with updated instructions"))
    assert facade.store.snapshot(aid)["work_packages"][0]["state"] == "pending"
    facade.supervisor.scheduler.advance(aid, manual=True)
    snapshot = facade.store.snapshot(aid)
    assert len(snapshot["plan_revisions"]) == 2
    assert snapshot["work_packages"][0]["superseded_contract"]
    assert snapshot["plan_revisions"][0]["work_packages"][0]["contract_digest"] == old["contract_digest"]
    assert len(snapshot["stages"]) == 1


@pytest.mark.parametrize('package_count', [1, 2])
def test_worker_loads_compiled_verification_without_second_model_plan(unified, package_count):
    service, facade, aid, _ = unified
    def plan_or_review(agent, payload):
        if payload.get("stage"):
            return dict(action="review_stage", reason="Recorded evidence satisfies this stage", source_refs=[e["id"] for e in payload["evidence"]],
                result="completed", checks=[dict(c, source_refs=[e["id"] for e in payload["evidence"]]) for c in refs(payload["stage"]["acceptance_criteria"])])
        if payload["history"]:
            return dict(action="wait", reason="No new delivery requested", source_refs=[])
        value = proposal()
        if package_count == 2:
            following = deepcopy(value['stage']['goals'][0]['work_packages'][0])
            following.update(key='summary', objective='Summarize notes.txt with the verified preceding result')
            value['stage']['goals'][0]['work_packages'].append(following)
        return value
    service.agent_plan_provider = plan_or_review
    facade.supervisor.scheduler.advance(aid, manual=True)
    facade.supervisor.scheduler.advance(aid, manual=True)
    service.start()
    result = until(lambda: facade.store.snapshot(aid), lambda s: s["work_packages"][0]["state"] in {"succeeded", "failed"})
    work = result["work_packages"][0]
    assert work["state"] == "succeeded", service.snapshot(work["session_id"]).get("run")
    state = service.snapshot(work["session_id"])
    assert state["acceptance"]["plan"]["origin"] == "compiled_plan"
    facade.supervisor.scheduler.advance(aid, manual=True)
    until(lambda: facade.store.snapshot(aid), lambda s: s["stages"][0]["state"] == "completed")
    works = facade.store.snapshot(aid)['work_packages']
    assert len(works) == package_count and all(w['state'] == 'succeeded' for w in works)
    if package_count == 2:
        bound = service.snapshot(works[1]['session_id'])['autonomy']['input_bindings']
        assert bound[0]['run_id'] == works[0]['terminal_run_id']
        assert bound[0]['receipt']['status'] == 'passed'


def test_migration_retires_only_unstarted_contracts_and_is_replayable(agent_service):
    service, facade, aid, _ = agent_service
    from loom.autonomy.plan_commands import migrate
    from tests.autonomy.test_binding import prepare
    old = prepare(facade, aid)
    before = facade.store.snapshot(aid)
    body = dict(command_id="migrate", expected_revision=before["revision"])
    result = migrate(facade, aid, body)
    assert result == migrate(facade, aid, body)
    snapshot = facade.store.snapshot(aid)
    assert snapshot["planning_engine"] == 2
    assert snapshot["work_packages"][0]["id"] == old["id"] and snapshot["work_packages"][0]["superseded_contract"]
    assert not service.store.list_sessions()


def test_migration_rejects_in_flight_session_without_mutation(agent_service):
    _, facade, aid, _ = agent_service
    from loom.autonomy.plan_commands import migrate
    from tests.autonomy.test_binding import prepare
    work = prepare(facade, aid)
    facade.supervisor.dispatcher.dispatch(aid, work["id"])
    before = facade.store.snapshot(aid)
    with pytest.raises(ServiceError, match="Resolve execution"):
        migrate(facade, aid, dict(command_id="unsafe", expected_revision=before["revision"]))
    assert facade.store.snapshot(aid)["planning_engine"] == 1
    assert not facade.store.snapshot(aid)["work_packages"][0].get("superseded_contract")


def test_compiler_retries_model_error_before_any_work_is_published(unified, monkeypatch):
    import json

    from loom.autonomy import plan_planner as module
    from loom.core import ok
    from tests.autonomy.test_planner import ReplyProvider, reply

    service, facade, aid, _ = unified
    invalid = proposal()
    invalid["stage"]["goals"][0]["work_packages"][0]["execution_tools"] = ["shell_execute"]
    provider = ReplyProvider([reply(json.dumps(invalid)), reply(json.dumps(proposal()))])
    service.agent_plan_provider = None
    service.config_path = "fixture.yaml"
    monkeypatch.setattr(module, "load_task_config", lambda _: ok(None))
    monkeypatch.setattr(module, "_create_provider", lambda *_a, **_k: ok(provider))
    facade.supervisor.scheduler.advance(aid, manual=True)
    snapshot = facade.snapshot(aid)
    assert len(provider.payloads) == 2
    assert "shell_execute" in provider.payloads[1]["repair"]["validation_error"]
    assert len(snapshot["work_packages"]) == 1 and len(snapshot["stages"]) == 1
    assert snapshot["budget"][0]["calls"] == 2


def test_revoked_actual_binding_stops_dispatch_without_recommendation(unified, monkeypatch):
    from loom.tasks import assembly as module

    service, facade, aid, _ = unified
    facade.supervisor.scheduler.advance(aid, manual=True)
    work = facade.store.snapshot(aid)["work_packages"][0]
    original = module.builtin_collection
    def without_reader(plugin_id, request):
        result = original(plugin_id, request)
        result._bindings = tuple(b for b in result._bindings if b.ref.id != "read_file")
        return result
    monkeypatch.setattr(module, "builtin_collection", without_reader)
    with pytest.raises(ServiceError, match="binding changed"):
        facade.supervisor.dispatcher.dispatch(aid, work["id"])
    assert not service.store.list_sessions()


def test_independent_objective_can_plan_when_another_proposal_is_invalid(unified):
    service, facade, aid, _ = unified
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, "agents", aid)
        other = deepcopy(agent["role"]["long_term_objectives"][0])
        other.update(id="other", desired_outcome="Inspect a second independent area")
        agent["role"]["long_term_objectives"].append(other)
        facade.store.put(db, "agents", agent)
    def proposals(_agent, payload):
        value = proposal()
        if payload["objective"]["id"] == "quality":
            value["stage"]["goals"][0]["work_packages"][0]["execution_tools"] = ["shell_execute"]
        return value
    service.agent_plan_provider = proposals
    facade.supervisor.scheduler.advance(aid, manual=True)
    snapshot = facade.store.snapshot(aid)
    assert [s["objective_id"] for s in snapshot["stages"]] == ["other"]
    assert next(s for s in snapshot["objective_planning"] if s["objective_id"] == "quality")["blocker"]


def test_wait_does_not_replan_on_its_own_state_write(unified):
    service, facade, aid, _ = unified
    calls = []
    def wait(*_):
        calls.append(True)
        return dict(action="wait", reason="Await new project observations", source_refs=[])
    service.agent_plan_provider = wait
    for _ in range(4):
        facade.supervisor.scheduler.advance(aid, manual=True)
    assert len(calls) == 1


def test_dependency_prevents_early_dispatch(unified):
    service, facade, aid, _ = unified
    value = proposal()
    second = deepcopy(value['stage']['goals'][0])
    second.update(key='summary', objective='Summarize verified repository boundaries', depends_on=['inspect'])
    value['stage']['goals'].append(second)
    service.agent_plan_provider = lambda *_: value
    facade.supervisor.scheduler.advance(aid, manual=True)
    works = facade.store.snapshot(aid)['work_packages']
    facade.supervisor.dispatcher.dispatch(aid, works[1]['id'])
    assert not service.store.list_sessions()
    facade.supervisor.dispatcher.dispatch(aid, works[0]['id'])
    assert len(service.store.list_sessions()) == 1


def test_completed_goal_reuse_requires_receipt_and_unchanged_inputs(unified):
    from loom.autonomy.plan_compiler import compile_plan, environments

    service, facade, aid, _ = unified
    inputs = []
    service.agent_plan_provider = lambda _a, p: inputs.append(deepcopy(p)) or proposal()
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.store.snapshot(aid)
    payload = inputs[0]
    payload.update(stage=snap['stages'][0], existing_goals=deepcopy(snap['goals']))
    old = payload['existing_goals'][0]
    old['state'] = 'completed'
    envs = environments(service, snap)
    def compile_it():
        return compile_plan(service, snap, payload, proposal(), envs, 'revision-run', facade.store.now())
    assert compile_it()['goals'][0]['id'] != old['id']
    payload['evidence'] = [dict(id='receipt', kind='delivery', goal_id=old['id'], passed=True,
                               verified_criteria=refs(old['acceptance_criteria']))]
    reused = compile_it()
    assert reused['goals'][0]['id'] == old['id'] and not reused['work_packages']
    payload['source_status']['sources'].append(('changed', 'new-digest', 'ready', 2))
    assert compile_it()['goals'][0]['id'] != old['id']


def test_checkpointed_plan_replays_after_restart_without_another_model_call(unified, monkeypatch):
    from loom.autonomy.supervisor import AgentSupervisor

    service, facade, aid, _ = unified
    coordinator = facade.supervisor.unified
    def crash(*_):
        raise KeyboardInterrupt('crash after durable decision')
    monkeypatch.setattr(coordinator, 'commit', crash)
    with pytest.raises(KeyboardInterrupt):
        coordinator.advance(aid, 'quality')
    assert not facade.store.snapshot(aid)['work_packages']
    service.agent_plan_provider = lambda *_: pytest.fail('Checkpointed decision must be replayed')
    recovered = AgentSupervisor(service, facade.store, service.background)
    assert recovered.unified.advance(aid, 'quality')
    assert len(facade.store.snapshot(aid)['plan_revisions']) == 1


@pytest.mark.parametrize('trigger', ['periodic', 'routine', 'observation'])
def test_all_triggers_publish_stages_without_autonomous_stage_mode(unified, trigger):
    from loom.autonomy.goals import observe

    _, facade, aid, _ = unified
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        objective = agent['role']['long_term_objectives'][0]
        objective.pop('stage_planning')
        if trigger == 'periodic':
            objective['schedule'] = dict(every_seconds=3600, acceptance=['Scheduled report must cite evidence'])
        else:
            objective['iteration'] = dict(review_seconds=3600, max_depth=2, acceptance=['Review must identify the observed gap'])
        if trigger == 'routine':
            facade.store.put(db, 'agent_profile', dict(id='habit', agent_id=aid, state='active', revision=1,
                text='Review source quality', habit=dict(objective_id='quality', every_seconds=3600)))
        facade.store.put(db, 'agents', agent)
    if trigger == 'observation':
        observe(facade.store, aid, 'observation', dict(objective_id='quality', indicator='quality', value='unknown', state='gap', source='human'))
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.store.snapshot(aid)
    assert len(snap['stages']) == len(snap['work_packages']) == 1, snap['objective_planning']
    expected = 'Scheduled report must cite evidence' if trigger == 'periodic' else 'Review must identify the observed gap'
    assert expected in snap['work_packages'][0]['acceptance']


def test_migration_preserves_completed_objective_state(agent_service):
    from loom.autonomy.plan_commands import migrate
    from loom.autonomy.stages import planning_state

    _, facade, aid, _ = agent_service
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        state = planning_state(facade.store, db, agent, agent['role']['long_term_objectives'][0])
        state['state'] = 'completed'
        facade.store.put(db, 'agent_objective_planning', state)
    migrate(facade, aid, dict(command_id='migrate-complete', expected_revision=agent['revision']))
    assert facade.store.snapshot(aid)['objective_planning'][0]['state'] == 'completed'


def test_budget_wait_requires_new_budget_or_day_not_another_schedule_click(unified):
    service, facade, aid, _ = unified
    calls = []
    def no_budget(*_):
        calls.append(True)
        error = ServiceError('Daily planning budget unavailable', 409, code='AGENT_BUDGET')
        error.budget_block = dict(scopes=[dict(scope='daily')])
        raise error
    service.agent_plan_provider = no_budget
    for _ in range(3):
        facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.store.snapshot(aid)
    assert len(calls) == 1 and snap['objective_planning'][0]['next_review_at'] > facade.store.now()
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        agent['role']['budget']['daily_tokens'] += 1
        facade.store.put(db, 'agents', agent)
    service.agent_plan_provider = lambda *_: proposal()
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert len(facade.store.snapshot(aid)['plan_revisions']) == 1


def test_result_reference_rejects_success_without_recorded_receipt(unified):
    from loom.autonomy.plan_inputs import resolve

    service, facade, aid, _ = unified
    value = proposal()
    value['stage']['goals'][0]['work_packages'].append(deepcopy(value['stage']['goals'][0]['work_packages'][0]))
    value['stage']['goals'][0]['work_packages'][1]['key'] = 'summary'
    service.agent_plan_provider = lambda *_: value
    facade.supervisor.scheduler.advance(aid, manual=True)
    works = facade.store.snapshot(aid)['work_packages']
    assert works[1]['input_refs'] == [dict(kind='verified_result', work_package_id=works[0]['id'], contract_digest=works[0]['contract_digest'])]
    with facade.store.sessions.transaction() as db:
        predecessor = facade.store.get(db, 'agent_work_packages', works[0]['id'], aid)
        predecessor['state'] = 'succeeded'
        facade.store.put(db, 'agent_work_packages', predecessor)
        with pytest.raises(ServiceError, match='verified receipt'):
            resolve(facade.store, db, aid, works[1])


def test_finite_objective_completes_only_after_stage_receipts(unified):
    from loom.autonomy.acceptance import sync_objectives

    service, facade, aid, _ = unified
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        previous = deepcopy(agent)
        objective = agent['role']['long_term_objectives'][0]
        objective['mode'] = 'finite'
        objective['stage_planning']['completion_criteria'] = ['Deliver a verified implementation report']
        sync_objectives(agent, previous)
        facade.store.put(db, 'agents', agent)
    def plan_or_review(_agent, payload):
        if payload.get('stage') or payload['history']:
            owner = payload.get('stage') or payload['objective']
            receipts = [e['id'] for e in payload['evidence'] if e.get('passed')]
            return dict(action='review_stage' if payload.get('stage') else 'complete_objective', reason='All required results have recorded receipts',
                source_refs=receipts, **({'result': 'completed'} if payload.get('stage') else {}),
                checks=[dict(c, source_refs=receipts) for c in refs(owner['acceptance_criteria'])])
        value = proposal()
        value['stage']['covers'] = refs(payload['objective']['acceptance_criteria'])
        return value
    service.agent_plan_provider = plan_or_review
    service.start()
    snap = until(lambda: facade.store.snapshot(aid), lambda s: bool(s['objective_planning']) and s['objective_planning'][0]['state'] == 'completed')
    assert len(snap['stages']) == 1 and snap['stages'][0]['state'] == 'completed'
    assert snap['work_packages'][0]['acceptance_result']['status'] == 'passed'


def test_retry_requests_whole_revision_without_creating_a_goal(unified):
    from loom.autonomy.plan_commands import retry_goal

    _, facade, aid, _ = unified
    facade.supervisor.scheduler.advance(aid, manual=True)
    with facade.store.sessions.transaction() as db:
        goal = facade.store.rows(db, 'agent_goals', aid)[0]
        goal['state'] = 'blocked'
        facade.store.put(db, 'agent_goals', goal)
    body = dict(command_id='retry', expected_revision=goal['revision'], change='Use updated project observations', change_kind='input')
    result = retry_goal(facade, aid, goal['id'], body)
    assert result == retry_goal(facade, aid, goal['id'], body)
    snap = facade.store.snapshot(aid)
    assert len(snap['goals']) == len(snap['work_packages']) == 1
    assert snap['objective_planning'][0]['revision_requested']


def test_configuration_activates_at_safe_boundary_and_recompiles(unified):
    from tests.autonomy.test_configuration import save

    _, facade, aid, _ = unified
    facade.supervisor.scheduler.advance(aid, manual=True)
    old = facade.store.snapshot(aid)['work_packages'][0]
    save(facade, aid, 'identity', dict(role_text='Review with the newly provided engineering constraints'))
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.store.snapshot(aid)
    assert not snap.get('pending_configuration')
    assert len(snap['plan_revisions']) == 2
    assert next(w for w in snap['work_packages'] if w['id'] == old['id'])['superseded_contract']
    assert snap['role']['role_text'] == 'Review with the newly provided engineering constraints'


def test_new_routine_window_wakes_a_waiting_plan(unified, monkeypatch):
    from datetime import timedelta

    service, facade, aid, _ = unified
    calls = []
    def wait(*_):
        calls.append(True)
        return dict(action='wait', reason='No new source changes this window', source_refs=[])
    service.agent_plan_provider = wait
    with facade.store.sessions.transaction() as db:
        agent = facade.store.get(db, 'agents', aid)
        agent['role']['long_term_objectives'][0].pop('stage_planning')
        facade.store.put(db, 'agents', agent)
        facade.store.put(db, 'agent_profile', dict(id='routine', agent_id=aid, state='active', revision=1,
            text='Inspect repository', habit=dict(objective_id='quality', every_seconds=3600)))
    facade.supervisor.scheduler.advance(aid, manual=True)
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert len(calls) == 1
    later = facade.store.clock() + timedelta(seconds=3601)
    monkeypatch.setattr(facade.store, 'clock', lambda: later)
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert len(calls) == 2


def test_failed_execution_does_not_prevent_configuration_repair(unified):
    from loom.autonomy.configuration import busy
    from tests.autonomy.test_configuration import save

    service, facade, aid, _ = unified
    facade.supervisor.scheduler.advance(aid, manual=True)
    facade.supervisor.scheduler.advance(aid, manual=True)
    with facade.store.sessions.transaction() as db:
        work = facade.store.rows(db, 'agent_work_packages', aid)[0]
        session = service.store._load(db, work['session_id'])
        session['task']['state'] = 'failed'
        service.store._save(db, session)
        work['state'] = 'failed'
        facade.store.put(db, 'agent_work_packages', work)
        assert not busy(facade.store, db, facade.store.get(db, 'agents', aid))
    save(facade, aid, 'identity', dict(role_text='Use the corrected engineering requirements'))
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.store.snapshot(aid)
    assert not snap.get('pending_configuration') and len(snap['plan_revisions']) == 2


def test_initial_plan_can_cite_registered_workspace_observation(unified):
    service, facade, aid, _ = unified
    seen = []
    def propose(_agent, payload):
        seen.append(payload)
        value = proposal()
        value['source_refs'] = [e['id'] for e in payload['evidence'] if e['kind'] == 'workspace_observation']
        assert len(value['source_refs']) == 1
        return value
    service.agent_plan_provider = propose
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.store.snapshot(aid)
    assert len(snap['stages']) == 1
    fact = seen[0]['evidence'][0]
    assert fact['snapshot_path'] == '/workspace_snapshot' and fact['passed'] is False
    assert fact['verified_criteria'] == []
    assert snap['plan_revisions'][0]['proposal']['source_refs'] == [fact['id']]


def test_workspace_observation_cannot_complete_a_stage(unified):
    from loom.autonomy.plan_commands import request_revision

    service, facade, aid, _ = unified
    facade.supervisor.scheduler.advance(aid, manual=True)
    state = facade.store.snapshot(aid)['objective_planning'][0]
    request_revision(facade, aid, 'quality', dict(command_id='review-unproven', expected_revision=state['revision'],
                                               reason='Review the new workspace observations'))
    def false_review(_agent, payload):
        observation = next(e['id'] for e in payload['evidence'] if e['kind'] == 'workspace_observation')
        assert all(not c['source_refs'] for c in payload['acceptance_support'])
        return dict(action='review_stage', result='completed', reason='Claimed verification from a directory listing', source_refs=[observation],
                    checks=[dict(c, source_refs=[observation]) for c in refs(payload['stage']['acceptance_criteria'])])
    service.agent_plan_provider = false_review
    facade.supervisor.unified.advance(aid, 'quality')
    snap = facade.store.snapshot(aid)
    assert snap['stages'][0]['state'] == 'active'
    assert 'No passed execution contract' in snap['objective_planning'][0]['reason']


def test_empty_evidence_namespace_allows_only_empty_citations(unified):
    from jsonschema import Draft202012Validator

    from loom.autonomy.plan_compiler import environments, schema
    from loom.autonomy.stage_planner import validate_evidence_refs

    service, facade, aid, _ = unified
    contract = schema(environments(service, facade.store.snapshot(aid)), dict(evidence=[]))
    assert Draft202012Validator(contract).is_valid(proposal())
    invalid = proposal()
    invalid['source_refs'] = ['workspace_snapshot', 'README.md']
    assert not Draft202012Validator(contract).is_valid(invalid)
    with pytest.raises(ServiceError) as error:
        validate_evidence_refs(invalid['source_refs'], set(), '/source_refs')
    assert error.value.details['issues'][0]['path'] == '/source_refs'
    assert error.value.details['available_refs'] == []
    assert 'Use []' in str(error.value)


def test_reference_correction_is_auditable_and_uses_fitted_input_namespace(unified, monkeypatch):
    import json

    from loom.autonomy import plan_planner as module
    from loom.core import ok
    from tests.autonomy.test_planner import ReplyProvider, reply

    service, facade, aid, _ = unified
    invalid = proposal()
    invalid['source_refs'] = ['README.md']
    provider = ReplyProvider([reply(json.dumps(invalid)), reply(json.dumps(proposal()))])
    service.agent_plan_provider = None
    service.config_path = 'fixture.yaml'
    monkeypatch.setattr(module, 'load_task_config', lambda _: ok(None))
    monkeypatch.setattr(module, '_create_provider', lambda *_a, **_k: ok(provider))
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert len(provider.payloads) == 2
    first, second = provider.payloads
    allowed = first['response_contract']['schema']['properties']['source_refs']['items']['enum']
    assert allowed == [e['id'] for e in first['evidence']]
    assert 'README.md' in second['repair']['validation_error']
    assert 'Allowed evidence IDs' in second['repair']['validation_error']
    with facade.store.sessions.read() as db:
        events = [json.loads(r[0]) for r in db.execute(
            "SELECT body FROM agent_events WHERE agent_id=? AND json_extract(body,'$.type') LIKE 'plan.protocol.%'", (aid,))]
    attempts = [e for e in events if e['type'] == 'plan.protocol.proposed']
    assert len(attempts) == 2
    rejected = facade.store.read_artifact(aid, attempts[0]['payload']['proposal_ref']['sha256'])
    assert rejected['source_refs'] == ['README.md']
    assert len(facade.store.snapshot(aid)['work_packages']) == 1


def test_exhausted_citation_correction_stays_local_without_transport_retry(unified, monkeypatch):
    import json

    from loom.autonomy import plan_planner as module
    from loom.core import ok
    from tests.autonomy.test_planner import ReplyProvider, reply

    service, facade, aid, _ = unified
    invalid = proposal()
    invalid['source_refs'] = ['invented-evidence']
    provider = ReplyProvider([reply(json.dumps(invalid)), reply(json.dumps(invalid))])
    service.agent_plan_provider = None
    service.config_path = 'fixture.yaml'
    monkeypatch.setattr(module, 'load_task_config', lambda _: ok(None))
    monkeypatch.setattr(module, '_create_provider', lambda *_a, **_k: ok(provider))
    facade.supervisor.scheduler.advance(aid, manual=True)
    snap = facade.store.snapshot(aid)
    assert not snap['work_packages'] and not snap['stages']
    assert snap['objective_planning'][0]['next_review_at'] == ''
    assert not snap['objective_planning'][0].get('transient_attempts')
    facade.supervisor.scheduler.advance(aid, manual=True)
    assert len(provider.payloads) == 2


def test_executable_verification_schema_disallows_unresolved_requirements(unified):
    from jsonschema import Draft202012Validator

    from loom.autonomy.plan_compiler import environments, schema

    service, facade, aid, _ = unified
    contract = schema(environments(service, facade.store.snapshot(aid)), dict(evidence=[]))
    value = proposal()
    value['stage']['assumptions'] = ['Current implementation details still need inspection']
    assert Draft202012Validator(contract).is_valid(value)
    value['stage']['goals'][0]['work_packages'][0]['verification']['unresolved_requirements'] = ['A required verifier is unavailable']
    assert not Draft202012Validator(contract).is_valid(value)
