from copy import deepcopy

import pytest

from loom.evaluation.gaps import gaps
from loom.evaluation.goal_loops.incremental import supplement_repair_evidence, validated_supplements
from loom.evaluation.work_items import status


def test_unknown_is_completed_with_limitations_without_automatic_retry():
    cp = {'cursor': 1, 'total_cards': 1, 'extraction': {'cards': [{'id': 'c', 'kind': 'model', 'name': 'call'}]},
          'units': {'u': {'cards': ['c'], 'work_state': 'settled', 'disposition': 'abstained', 'issues': []}},
          'assessments': {'c': {'status': 'unknown', 'delta': 'No recorded result'}}, 'conflicts': []}
    original = deepcopy(cp)
    result = status(cp)
    assert result['execution_state'] == 'completed' and result['result_state'] == 'partial'
    assert result['completion_kind'] == 'limited' and result['retryable_work'] == 0
    assert result['gaps'][0]['code'] == 'evidence.unknown' and cp == original
    cp['conflicts'] = [{'code': 'graph.unresolved', 'category': 'semantic', 'reason': 'Unresolved'}]
    assert status(cp)['completion_kind'] == 'needs_repair'


def test_gap_projection_groups_dependency_symptoms_and_keeps_candidate_identity():
    errors = [dict(code='graph.evidence_scope', category='semantic', reason='Missing ref', record_id='c1', path=['refs']),
              dict(code='graph.assessment_dependency', category='semantic', reason='Missing assessment'),
              dict(code='graph.cards_pending', category='coverage', reason='Pending cards')]
    cp = {'units': {'u': {'issues': errors, 'disposition': 'quarantined'}}, 'conflicts': [{**e, 'batch_id': 'u'} for e in errors],
          'assessments': {}, 'memory': {'units': {}, 'records': {}}}
    result = gaps(cp)
    assert len(result) == 1 and result[0]['action'] == 'evidence'
    assert result[0]['record_id'] == 'c1'
    assert set(result[0]['symptoms']) == {'graph.assessment_dependency', 'graph.cards_pending'}
    assert result == gaps(cp)


def test_supplement_is_scoped_bounded_persisted_and_never_uses_future_or_secret():
    unit = {'id': 'batch', 'cards': ['c'], 'cutoff': 4, 'feedback': [{'code': 'graph.evidence_scope'}]}
    cards = {'c': {'excerpts': [{'ref': 'good', 'text': 'visible evidence'}, {'ref': 'future', 'text': 'later'},
                                {'ref': 'secret', 'text': 'sk-abcdefghijklmnopqrstuvwxyz0123456789'}]}}
    registry = {name: {'line': line, 'ref': {'line_number': line, 'field_path': 'content', 'start': 0, 'end': len(text)}}
                for name, line, text in [('good', 3, 'visible evidence'), ('future', 5, 'later'),
                                         ('secret', 3, 'sk-abcdefghijklmnopqrstuvwxyz0123456789')]}
    cp = {'identity': {'source': 'digest'}, 'read_chars': 0}
    supplement_repair_evidence(cp, unit, cards, {'c': {'refs': []}}, registry, {'max_read_rounds': 1, 'max_evidence_chars': 7})
    assert len(unit['extra']) == 1 and unit['extra'][0]['text'] == 'visible'
    ref = unit['extra'][0]['id']
    assert registry[ref]['ref']['end'] == 7 and cp['extra_evidence'][ref] == registry[ref]
    assert cp['read_chars'] == 7 and unit['supplement_manifest']['cutoff'] == 4
    supplement_repair_evidence(cp, unit, cards, {'c': {'refs': []}}, registry, {'max_read_rounds': 1, 'max_evidence_chars': 100})
    assert cp['read_chars'] == 7


def test_supplement_cannot_be_cited_by_another_action():
    from loom.evaluation.goal_loops.graph_updates import commit_updates
    from tests.evaluation.test_graph_updates import fixture
    from tests.evaluation.test_incremental_graph import assessment

    _, _, cp, extraction = fixture()
    first, second = extraction['cards'][:2]
    ref = first['refs'][0]
    row = assessment(second)
    row['refs'] = [ref]
    errors, _ = commit_updates(cp, [('assessments', row)], batch_id='test', cards=extraction['cards'],
                               delivered=extraction['registry'], cutoff=100, target_ids={second['id']},
                               evidence_owners={ref: first['id']})
    assert errors[0]['code'] == 'graph.evidence_owner'
    assert second['id'] not in cp['assessments']


def test_saved_supplement_is_revalidated_before_delivery():
    class Source:
        def resolve(self, pointer):
            return 'original'[pointer['start']:pointer['end']]

    cp = {'identity': {'source': 'digest'}}
    registry = {'ref': {'line': 2, 'ref': {'start': 0, 'end': 4}}}
    unit = {'cards': ['c'], 'extra': [{'id': 'ref', 'card_id': 'c', 'text': 'orig'}],
            'supplement_manifest': {'source': 'digest', 'cutoff': 2, 'refs': ['ref'], 'owners': {'ref': 'c'}}}
    assert validated_supplements(Source(), cp, unit, registry, 2) == unit['extra']
    unit['extra'][0]['text'] = 'fake'
    assert validated_supplements(Source(), cp, unit, registry, 2) == []
    unit['extra'][0].update(text='orig', card_id='other')
    assert validated_supplements(Source(), cp, unit, registry, 2) == []


@pytest.mark.asyncio
async def test_graph_repairs_bad_reference_with_scoped_source_evidence():
    import json

    from loom.core import ok
    from loom.evaluation.behavior_facts import build_behavior_facts
    from loom.llm import LlmResponse
    from tests.evaluation.test_behavior_sequence import action, source
    from tests.evaluation.test_incremental_graph import GraphJudge, run

    class RepairJudge(GraphJudge):
        async def chat(self, messages, *args, **kwargs):
            response = await super().chat(messages, *args, **kwargs)
            payload = json.loads(messages[1].content)
            value = json.loads(response.value.content)
            if len(self.requests) == 1:
                target = next(c['id'] for c in payload['cards'] if c['kind'] == 'tool')
                for row in value['groups'][0]['assessments']:
                    if row['card_id'] == target:
                        row['refs'] = ['undelivered-reference']
            elif payload['extra_evidence']:
                for row in value['groups'][0]['assessments']:
                    if row['card_id'] == payload['extra_evidence'][0]['card_id']:
                        row['refs'] = [payload['extra_evidence'][0]['id']]
            return ok(LlmResponse(content=json.dumps(value)))

    store = source(action(1))
    cp, judge = {}, RepairJudge()
    result = await run(store, build_behavior_facts(store), 'run', cp, judge, max_calls=20, max_evidence_chars=2000)
    assert any(request['extra_evidence'] for request in judge.requests[1:])
    assert cp['read_chars'] <= 1200
    assert result['result_state'] == 'ready'
