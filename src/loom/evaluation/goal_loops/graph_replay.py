"""Versioned replay of saved responses against their original evidence delivery.

Revalidation never calls a model, widens evidence scope, or edits response text.
Accepted records remain authoritative; independent failed records can be admitted
by an explicitly supported validator upgrade.
"""

from copy import deepcopy

from loom.evaluation.goal_loops.coverage import window_issues
from loom.evaluation.goal_loops.graph_normalization import terminal_metadata_records
from loom.evaluation.goal_loops.graph_updates import VALIDATION_VERSION, commit_updates, parse_updates
from loom.evaluation.work_items import settle
from loom.llm.api import LlmResponse

REPLAYABLE_VERSIONS = {"graph-validation.1", "graph-validation.2"}


def replay_saved_response(cp, unit, response, cards):
    previous = response.get('revalidated_with', response.get('validation_version'))
    if unit.get('retry_requested') or previous not in REPLAYABLE_VERSIONS or not isinstance(response.get('delivered_ref_ids'), list):
        return False
    allowed = response.get('allowed_record_types')
    if allowed is None:
        # Validator v1 did not save the narrow request contract separately, but
        # every rejected record type carries that contract in structured details.
        scopes = [e['details']['allowed_types'] for e in response.get('validation_errors', [])
                  if e.get('code') == 'response.record_type' and 'allowed_types' in e.get('details', {})]
        if scopes:
            allowed = sorted(set.intersection(*(set(scope) for scope in scopes)))
    registry = {**cp.get('extraction', {}).get('registry', {}), **cp.get('extra_evidence', {})}
    delivered = {key: registry[key] for key in response['delivered_ref_ids']
                 if key in registry and registry[key]['line'] <= unit['cutoff']}
    records, errors = parse_updates(LlmResponse(content=response.get('text', ''), finish_reason=response.get('finish_reason')))
    errors = [{**e, 'stage': 'graph', 'unit_id': unit['id']} for e in errors]
    normalized = terminal_metadata_records(records, normalizations=cp.setdefault('protocol_normalizations', []), batch_id=unit['id'])
    rejected, _ = commit_updates(cp, normalized, batch_id=unit['id'], cards=cards, delivered=delivered,
                                 cutoff=unit['cutoff'], target_ids=set(unit['cards']), allowed_fields=allowed,
                                 visible_ids=response.get('visible_entity_ids'))
    errors += rejected
    errors = window_issues(cp, unit) + errors
    response.setdefault('revalidations', []).append(dict(
        from_version=previous, to_version=VALIDATION_VERSION, errors=deepcopy(errors), graph_revision=cp['graph_revision']))
    response['revalidated_with'] = VALIDATION_VERSION
    unit.update(feedback=deepcopy(errors), complete=not errors)
    settle(unit, errors, accepted=sum(cid in cp['assessments'] for cid in unit['cards']))
    cp['conflicts'] = [e for e in cp['conflicts'] if e['batch_id'] != unit['id']]
    cp['conflicts'] += [dict(batch_id=unit['id'], **deepcopy(e)) for e in errors]
    cp.setdefault('checkpoint_reconciliations', []).append(dict(
        batch_id=unit['id'], from_validation=previous, to_validation=VALIDATION_VERSION,
        complete=not errors, remaining_issues=len(errors), model_calls=0))
    return True
