import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from loom.tasks.acceptance import AcceptanceController
from loom.tasks.acceptance_contract import RULES_VERSION, InvalidAcceptanceContract
from loom.tasks.acceptance_feedback import decide
from loom.tasks.acceptance_protocols import plan_protocol
from loom.tasks.assembly import TaskAssembly
from loom.tasks.recovery import assess
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context
from tests.tasks.test_acceptance import Judge, Sink


def tool_check(tool='finish'):
    return dict(id='report', description='Deliver a complete report', verifier='tool_call', scope=[],
                check=dict(tool_id=tool, min_calls=1), required_tools=[tool])


@pytest.mark.parametrize('name', ['finish', 'custom_complete'])
def test_control_transitions_cannot_be_execution_evidence(tmp_path, name):
    assembly = TaskAssembly(TaskRequest('Deliver report', workspace=tmp_path), plan_mode='off')
    if name != 'finish':
        binding = assembly.bindings['finish']
        assembly.bindings[name] = replace(binding, ref=replace(binding.ref, id=name))
    with pytest.raises(InvalidAcceptanceContract, match='cannot supply an execution receipt'):
        assembly.acceptance.validate_plan(dict(criteria=[tool_check(name)]))
    variants = plan_protocol(assembly).schema['properties']['criteria']['items']['anyOf']
    tools = next(v['properties']['check']['properties']['tool_id']['enum'] for v in variants
                 if v['properties']['verifier']['enum'] == ['tool_call'])
    assert name not in tools and 'read_file' in tools


def test_bad_retained_contract_stops_before_solver_or_budget_renewal(tmp_path):
    assembly = TaskAssembly(TaskRequest('Deliver report', workspace=tmp_path), plan_mode='off')
    gate = assembly.acceptance
    context = make_task_context(assembly.request, assembly=assembly, plan_mode='off').unwrap()
    gate.goal(context)
    gate.data.update(plan=dict(criteria=[tool_check()], unresolved_requirements=[]), attempts=3, repair_rounds=2)
    assembly.acceptance_resume_request = 'resume-1'
    runtime = SimpleNamespace(run_id='run', loop_id='loop', trace_id='trace', trace_sink=Sink())
    with pytest.raises(InvalidAcceptanceContract):
        asyncio.run(gate.prepare(context, runtime))
    decision = decide(gate)
    assert decision.outcome == 'pause' and decision.reason_code == 'ACCEPTANCE_CONTRACT_INVALID'
    assert gate.data['model_calls'] == 0 and gate.data['repair_rounds'] == 2
    report = assess(dict(task=dict(revision=1, objective='Deliver report'), acceptance=gate.public()))
    assert report['mode'] == 'reassemble'
    assert any(c['code'] == 'ACCEPTANCE_CONTRACT' for c in report['causes'])


def test_old_valid_checkpoint_requires_fresh_verification_once(tmp_path):
    assembly = TaskAssembly(TaskRequest('Deliver report', workspace=tmp_path), plan_mode='off')
    gate = assembly.acceptance
    gate.data['plan'] = gate.validate_plan(dict(criteria=[]))
    gate.data['plan'].pop('rules_version')
    gate.data.update(state='passed', results=[dict(criterion_id='goal', status='passed')])
    gate.validate_retained_contract()
    assert gate.data['state'] == 'not_ready' and gate.data['plan']['rules_version'] == RULES_VERSION
    first = deepcopy(gate.snapshot())
    gate.validate_retained_contract()
    assert gate.snapshot() == first


def test_concrete_execution_cannot_be_downgraded_to_model_opinion(tmp_path):
    assembly = TaskAssembly(TaskRequest('Run pytest -q', workspace=tmp_path), plan_mode='off')
    gate = assembly.acceptance
    old = dict(id='tests', description='Run pytest -q', verifier='command', scope=[],
               check=dict(tool_id='shell_execute', input=dict(command='pytest -q')), required_tools=['shell_execute'])
    gate.data.update(plan=gate.validate_plan(dict(criteria=[old])), revision=1)
    proposed = {**old, 'verifier': 'semantic', 'check': {}, 'required_tools': []}
    with pytest.raises(InvalidAcceptanceContract):
        asyncio.run(gate.revise(dict(base_revision=1, reason='Use prose', criteria=[proposed]), None))


def test_failed_prerequisite_records_semantic_checks_as_not_run(tmp_path):
    assembly = TaskAssembly(TaskRequest('Read source and report', workspace=tmp_path), plan_mode='off')
    gate, sink = assembly.acceptance, Sink()
    assembly.configure_acceptance(Judge())
    context = make_task_context(assembly.request, assembly=assembly, plan_mode='off').unwrap()
    gate.goal(context)
    gate.data['plan'] = gate.validate_plan(dict(criteria=[tool_check('read_file')]))
    runtime = SimpleNamespace(run_id='run', loop_id='loop', trace_id='trace', trace_sink=sink, cancellation=None)
    assert not asyncio.run(gate.verify(context, runtime, 'Report without source'))
    assert {r['criterion_id']: r['status'] for r in gate.data['results']} == {'report': 'failed', 'goal': 'not_run'}
    assert gate.data['model_calls'] == 0
    assert next(e for e in sink.events if e['type'] == 'acceptance.gate.blocked')['assurance'] == 'checks_only'


def test_unknown_execution_never_accepts_a_cached_pass(tmp_path):
    assembly = TaskAssembly(TaskRequest('Deliver report', workspace=tmp_path), plan_mode='off')
    gate = assembly.acceptance
    gate.data.update(plan=gate.validate_plan(dict(criteria=[])), state='passed', pending_operations={'command': 'unknown'})
    assert decide(gate).reason_code == 'EXECUTION_UNKNOWN'


@pytest.mark.parametrize('identifier', ['candidate', 'tool-call-coverage', 'tool:forged', 'report:forged'])
def test_criterion_ids_cannot_shadow_host_evidence(tmp_path, identifier):
    gate = AcceptanceController(TaskAssembly(TaskRequest('Report', workspace=tmp_path), plan_mode='off'))
    with pytest.raises(InvalidAcceptanceContract, match='host evidence'):
        gate.validate_plan(dict(criteria=[dict(tool_check('read_file'), id=identifier)]))


def test_completion_persists_candidate_before_rejecting_a_bad_contract(tmp_path):
    assembly = TaskAssembly(TaskRequest('Deliver report', workspace=tmp_path), plan_mode='off')
    assembly.context = make_task_context(assembly.request, assembly=assembly, plan_mode='off').unwrap()
    assembly._task_runtime = SimpleNamespace(run_id='run', loop_id='loop', trace_id='trace', trace_sink=Sink(), cancellation=None)
    gate = assembly.acceptance
    gate.goal(assembly.context)
    gate.data['plan'] = dict(criteria=[tool_check()], unresolved_requirements=[])
    decision = asyncio.run(assembly.request_completion('Retain this report for a corrected contract'))
    assert decision.reason_code == 'ACCEPTANCE_CONTRACT_INVALID' and decision.outcome == 'pause'
    candidate = gate.public()['candidate_ref']
    assert assembly.read_artifact(candidate['sha256'])['candidate'] == 'Retain this report for a corrected contract'
    assert gate.data['model_calls'] == 0 and gate.data['repair_rounds'] == 0
    assert not assembly._acceptance_calls


@pytest.mark.parametrize('explicit_default', [True, False])
def test_wire_defaults_do_not_change_an_execution_requirement(explicit_default):
    from loom.tasks.acceptance_contract import preserve_evidence_requirements

    original = tool_check('read_file')
    proposed = deepcopy(original)
    (original if explicit_default else proposed)['check'].pop('min_calls')
    preserve_evidence_requirements(dict(criteria=[original]), dict(criteria=[proposed]))
    proposed['check']['min_calls'] = 2
    with pytest.raises(InvalidAcceptanceContract):
        preserve_evidence_requirements(dict(criteria=[original]), dict(criteria=[proposed]))


def test_compiled_worker_is_not_offered_a_contract_mutation_tool(tmp_path):
    assembly = TaskAssembly(TaskRequest('Report', workspace=tmp_path, metadata={'autonomy': {'engine_version': 2}}), plan_mode='off')
    assert 'revise_acceptance_plan' not in {ref.id for ref in assembly.context_tools()}


def test_method_fault_goes_to_owner_without_spending_solver_repairs(tmp_path):
    assembly = TaskAssembly(TaskRequest('Report', workspace=tmp_path), plan_mode='off')
    gate = assembly.acceptance
    gate.data.update(plan=gate.validate_plan(dict(criteria=[])), state='needs_repair', candidate='Report', reason='The verification method is invalid',
        results=[dict(criterion_id='goal',status='blocked',reason='Wrong verification method',gap=dict(kind='verification_method',targets=[],
                      required_fact='Actual report verification',suggested_action='Revise the contract'))])
    decision = decide(gate)
    assert decision.outcome == 'pause' and decision.reason_code == 'ACCEPTANCE_CONTRACT_INVALID'
    assert gate.data['repair_rounds'] == 0 and gate.data['feedback'] is None
