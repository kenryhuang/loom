from copy import deepcopy

from loom.skills.contracts import digest
from loom.skills.lineage import LineageIndex, assign, feature_key
from tests.skills.helpers import fixture, seed


def case(identifier, session, objective, source=None, **fields):
    return dict(id=identifier, session_id=session, task_contract=dict(objective=objective), source_digest=source,
                quality=dict(boundary_uncertain=True), **fields)


def test_deleting_a_bridge_splits_families_and_does_not_keep_old_unions():
    cases = [case('a', 'one', 'alpha'), case('b', 'one', 'beta', 'bridge'), case('c', 'three', 'gamma', 'bridge')]
    index = LineageIndex()
    original = assign(cases, index=index)
    assert len({g['task_family_id'] for g in original.values()}) == 1
    reduced = assign([cases[0], cases[2]], index=index)
    assert reduced['a']['task_family_id'] != reduced['c']['task_family_id']
    assert index.last_comparisons == 0  # Removal drops edges, not unrelated pair checks.
    assert reduced == assign([cases[0], cases[2]])


def test_changed_case_rechecks_its_edges_but_unchanged_cases_reuse_features(monkeypatch):
    import loom.skills.lineage as lineage

    calls = []
    original = lineage.normalize
    monkeypatch.setattr(lineage, 'normalize', lambda value: calls.append(value) or original(value))
    cases = [case(str(i), str(i), 'different objective ' + str(i), source=str(i)) for i in range(12)]
    index = LineageIndex()
    assign(cases, index=index)
    calls.clear()
    unchanged = assign(cases, index=index)
    assert not calls and index.last_comparisons == 0
    changed = deepcopy(cases)
    changed[0]['task_contract']['objective'] = 'an entirely new request'
    updated = assign(changed, index=index)
    assert calls == ['an entirely new request']
    assert index.last_comparisons == len(cases) - 1
    assert updated == assign(changed)
    assert unchanged == assign(cases)


def test_persisted_edges_warm_after_restart_and_snapshot_links_remain_separate():
    cases = [case('a', 'one', 'first', project_snapshot_digest='same'),
             case('b', 'two', 'second', project_snapshot_digest='same'), case('c', 'three', 'third')]
    expected = assign(cases)
    assert expected['a']['task_family_id'] != expected['b']['task_family_id']
    assert expected['a']['split_group_id'] == expected['b']['split_group_id']
    saved = [{**c, **expected[c['id']], 'lineage_features_digest': digest(feature_key(c))} for c in cases]
    restored = LineageIndex()
    assert assign(saved, index=restored) == expected
    assert restored.last_comparisons == 0
    saved[0]['project_snapshot_digest'] = 'changed'
    updated = assign(saved, index=restored)
    assert updated['a']['split_group_id'] != updated['b']['split_group_id']
    assert updated == assign(saved)


def test_catalog_does_not_reload_or_regroup_cases_for_streaming_counter_changes(tmp_path, monkeypatch):
    base, store, sources, catalog = fixture(tmp_path)
    evaluation = seed(base, sources, tmp_path, 0)
    first = catalog.reconcile(str(tmp_path))
    monkeypatch.setattr(store, 'all_cases', lambda *_: (_ for _ in ()).throw(AssertionError('Unchanged corpus must not reload')))
    base.update(evaluation['session_id'], lambda state, emit, db: state['run'].update(active_seconds=123, counters=dict(calls=12)))
    again = catalog.reconcile(str(tmp_path))
    assert again == {**first, 'updated_cases': 0}


def test_copied_feature_marker_cannot_certify_a_new_case_identity():
    original = case('a', 'one', 'first')
    groups = assign([original])
    saved = {**original, **groups['a'], 'lineage_features_digest': digest(feature_key(original))}
    duplicate = {**deepcopy(saved), 'id': 'b'}
    restored = LineageIndex()
    actual = assign([saved, duplicate], index=restored)
    assert restored.last_comparisons == 1
    assert actual['a']['task_family_id'] == actual['b']['task_family_id']
    assert actual == assign([saved, duplicate])
