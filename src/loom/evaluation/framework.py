"""Framework provenance and review obligations, independent of model judgments.

Runtime observations establish recorded host state, never task success or the
truth of model-authored plans, verification conclusions, or report contents.
Unknown event kinds remain context until explicitly classified here.
"""

VERSION = 'framework-evidence.1'
RUNTIME_STATE_EVENTS = {
    'acceptance.gate.blocked', 'acceptance.gate.passed', 'acceptance.budget.renewed',
    'acceptance.invalidated', 'verification.completed',
    'run.completed', 'run.failed', 'run.stopped', 'run.cancelled',
}
ROOT_BOUNDARY_PREFIXES = ('acceptance.', 'verification.', 'run.completed', 'run.failed', 'run.stopped',
                          'run.cancelled', 'task.configured', 'task.defined', 'task.assessed',
                          'task.decision', 'task.outcome', 'task.completed')


def boundary_event(event_type):
    return event_type.startswith((*ROOT_BOUNDARY_PREFIXES, 'plan.'))


def root_owned_card(card):
    return ((card.get('actor') or {}).get('role') == 'verification'
            or (card['kind'] == 'tool' and card['name'] == 'finish')
            or (card['kind'] == 'boundary' and card['name'].startswith(ROOT_BOUNDARY_PREFIXES)))


def card_policy(card):
    return dict(version=VERSION, ownership='root' if root_owned_card(card) else 'semantic_or_default',
                semantic_review=card['kind'] != 'boundary',
                stage_key_action=card['kind'] != 'boundary' and not root_owned_card(card))


def semantic_unknown_ids(cp):
    boundaries = {c['id'] for c in cp.get('extraction', {}).get('cards', []) if not card_policy(c)['semantic_review']}
    return {cid for cid, row in cp.get('assessments', {}).items() if row.get('status') == 'unknown' and cid not in boundaries}
