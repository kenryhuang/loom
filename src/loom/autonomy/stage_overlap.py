"""Shared proposal/commit checks for repeated deliveries and stage integration."""

from difflib import SequenceMatcher

from loom.service.contracts import ServiceError


def delivery_index(stages, goals):
    # Keep all identities for validation even when the narrative history is bounded.
    return dict(
        stages=[{k: s.get(k) for k in ('id', 'state', 'delivery_key', 'expected_outcome', 'goal_ids')} for s in stages],
        goals=[{k: g.get(k) for k in ('id', 'state', 'stage_id', 'objective')} for g in goals],
    )


def validate_overlap(stage, existing):
    stages = existing.get('stages', [])
    for old in stages:
        similarity = SequenceMatcher(None, stage['expected_outcome'].casefold(), old['expected_outcome'].casefold()).ratio()
        if old['state'] not in {'cancelled', 'superseded'} and (old['delivery_key'] == stage['delivery_key'] or similarity > 0.85):
            raise ServiceError('Proposed stage repeats an existing delivery; provide a concrete different contribution')

    completed_integrations = {s['goal_ids'][-1] for s in stages if s['state'] == 'completed' and s.get('goal_ids')}
    for index, item in enumerate(stage['goals']):
        if item.get('reuse_goal_id'):
            continue
        # The final Goal inherits this new stage's entire acceptance contract and
        # depends on its preceding work. A generic integration description is not
        # the same delivery as verifying an already-completed, different stage.
        integrates_new_work = index == len(stage['goals']) - 1 and any(
            not g.get('reuse_goal_id') for g in stage['goals'][:index]
        )
        for old in existing.get('goals', []):
            if old['state'] in {'cancelled', 'superseded'}:
                continue
            similarity = SequenceMatcher(None, old['objective'].casefold(), item['objective'].casefold()).ratio()
            if similarity <= 0.9:
                continue
            if integrates_new_work and old['state'] == 'completed' and old.get('stage_id') and old['id'] in completed_integrations:
                continue
            error = ServiceError(
                f"/stage/goals/{index}/objective: Stage Goal overlaps existing work {old['id']}; "
                'reuse a compatible unassigned completed Goal or describe the concrete new scope'
            )
            error.details = dict(goal_id=old['id'], stage_id=old.get('stage_id'), similarity=similarity)
            raise error
