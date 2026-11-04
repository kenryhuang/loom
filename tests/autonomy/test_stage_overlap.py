from copy import deepcopy

import pytest

from loom.autonomy.acceptance import key as criterion_key
from loom.autonomy.stage_overlap import validate_overlap
from loom.service.contracts import ServiceError
from tests.autonomy.test_stage_contracts import configure, delivery
from tests.autonomy.test_stage_end_to_end import decision

INTEGRATION = 'Integrate this stage, run tests and build, and report each acceptance result'


def index():
    return dict(stages=[dict(id='galaxy', state='completed', delivery_key='galaxy',
                            expected_outcome='Calibrated galactic starfield', goal_ids=['old-implementation', 'old-integration'])],
                goals=[dict(id='old-integration', stage_id='galaxy', state='completed', objective=INTEGRATION)])


def proposed():
    return dict(delivery_key='cosmic', expected_outcome='Local group cosmic view', goals=[
        dict(key='implement', objective='Implement cosmic data, units and scene'),
        dict(key='verify', objective=INTEGRATION)])


def test_new_stage_integration_is_scoped_to_new_implementation():
    validate_overlap(proposed(), index())


@pytest.mark.parametrize('case', ['active', 'unassigned', 'nonfinal_old', 'nonfinal_new', 'no_new_work', 'one_goal', 'same_stage'])
def test_overlap_exception_cannot_reuse_actual_work_or_old_verification(case):
    stage, existing = proposed(), index()
    if case == 'active':
        existing['goals'][0]['state'] = 'active'
    elif case == 'unassigned':
        existing['goals'][0]['stage_id'] = None
    elif case == 'nonfinal_old':
        existing['stages'][0]['goal_ids'].append('another-final')
    elif case == 'nonfinal_new':
        stage['goals'].reverse()
    elif case == 'no_new_work':
        stage['goals'][0]['reuse_goal_id'] = 'old-implementation'
    elif case == 'one_goal':
        stage['goals'] = stage['goals'][1:]
    elif case == 'same_stage':
        stage['delivery_key'] = 'galaxy'
    with pytest.raises(ServiceError, match='overlaps existing work|repeats an existing delivery'):
        validate_overlap(stage, existing)


def test_two_stages_with_generic_integration_each_require_their_own_acceptance(agent_service):
    _, facade, aid, _ = agent_service
    configure(facade, aid, max_new_stages_per_day=3)
    delivery(facade, aid)

    def planner(agent, payload):
        proposal = decision(agent, payload)
        if proposal['action'] == 'propose_stage':
            stage = proposal['stage']
            integration = dict(key='integrate', objective=INTEGRATION, acceptance=['Report current-stage results'], depends_on=['deliver'])
            stage['goals'].append(integration)
        return proposal

    facade.supervisor.stages.planner.provider = planner
    facade.supervisor.stages.advance(aid)
    first = facade.snapshot(aid)['stages'][0]
    for goal in facade.snapshot(aid)['goals']:
        if goal['id'] in first['goal_ids']:
            delivery(facade, aid, goal)
    facade.supervisor.stages.advance(aid)  # Review the first completed stage.
    facade.supervisor.stages.advance(aid)  # Propose a distinct stage using the same integration template.
    snap = facade.snapshot(aid)
    assert len(snap['stages']) == 2
    second = snap['stages'][1]
    final = next(g for g in snap['goals'] if g['id'] == second['goal_ids'][-1])
    assert final['objective'] == INTEGRATION
    assert final['state'] == 'queued' and final['id'] != first['goal_ids'][-1]
    assert final['depends_on'] == second['goal_ids'][:-1]
    assert {criterion_key(c) for c in second['acceptance_criteria']} <= {criterion_key(c) for c in final['acceptance_criteria']}
    before = deepcopy(snap['stage_reviews'])
    facade.supervisor.stages.advance(aid)
    assert facade.snapshot(aid)['stage_reviews'] == before  # Previous stage evidence cannot finish the new stage.
