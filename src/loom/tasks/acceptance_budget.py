"""Bounded acceptance allocations, renewed only by an explicit service Resume."""

from copy import deepcopy

from loom.core import now_iso


def reserve_answer_budget(provider):
    """Give bounded Qwen 3.8 Max acceptance calls room to return their verdict."""
    from dataclasses import replace

    from loom.llm.api import OpenAIProvider
    from loom.llm.provider_wrappers import map_provider
    from loom.llm.request_options import materialize_request_options

    def adapt(base):
        if not isinstance(base, OpenAIProvider) or not (base.model == 'qwen3.8-max' or base.model.startswith('qwen3.8-max-')):
            return base
        options = materialize_request_options(base.request_options)
        cap = base.max_completion_tokens
        # Qwen supports thinking_budget; never inject it for other providers or
        # combine it with an explicitly selected effort/budget. Preserve explicit
        # settings and the original solver provider.
        if options.get('enable_thinking') is not True or 'thinking_budget' in options or 'reasoning_effort' in options or not cap or cap < 2:
            return base
        options['thinking_budget'] = cap // 2
        return replace(base, request_options=options)

    return map_provider(provider, adapt)


def command_timeout(limits, configured, criterion):
    """An explicit check deadline wins over the fallback, but not an owner cap."""
    requested = criterion.get('check', {}).get('input', {}).get('timeout_seconds', limits['command_timeout_seconds'])
    return min(requested, limits['command_timeout_seconds']) if 'command_timeout_seconds' in configured else requested


def allocation_plan(limits, plan, page_counts=None, configured=None, selection_count=0):
    criteria = (plan or {}).get('criteria', [])
    # Reserve single-criterion batches even when the normal grouping is larger:
    # evidence size can split every group. Goal is always a separate review.
    reviews = 1 + sum(c.get('id') != 'goal' for c in criteria)
    visual = sum(c['verifier'] == 'visual' for c in criteria)
    pages = sum((min(limits['max_evidence_pages'], n) if 'max_evidence_pages' in (configured or {}) else n) for key, n in (page_counts or {}).items()
                if key in {c.get('id') for c in criteria})
    rounds = limits['max_repairs'] + 1
    # Each review may expand evidence once; both views can need page inspection.
    # Every logical request allows a format fallback AND one protocol correction.
    requests = 2 * (reviews + pages + visual) + selection_count
    calls = 3 + 3 * requests * rounds
    command_seconds = rounds * sum(command_timeout(limits, configured or {}, c) for c in criteria if c['verifier'] == 'command')
    # browser_capture has a 180-second host deadline (cold preview, capture and
    # narration); allow both acquisitions independently of visual judgment time.
    capture_seconds = 2 * visual * 180 * rounds
    seconds = calls * limits['model_timeout_seconds'] + command_seconds + capture_seconds
    return dict(review_units=reviews, evidence_pages=pages, evidence_selections=selection_count, visual_checks=visual, rounds=rounds,
                protocol_attempts=3, command_seconds=command_seconds, visual_capture_seconds=capture_seconds,
                planned_model_calls=calls, planned_seconds=seconds)


def effective_limits(defaults, configured, plan, page_counts=None, selection_count=0):
    limits = {**defaults, **configured}
    if plan is not None:
        allocation = allocation_plan(limits, plan, page_counts, configured, selection_count)
        if 'max_model_calls' not in configured:
            limits['max_model_calls'] = max(defaults['max_model_calls'], allocation['planned_model_calls'])
        if 'max_seconds' not in configured:
            limits['max_seconds'] = max(defaults['max_seconds'], allocation['planned_seconds'])
    return limits


def public_budget(controller):
    data, limits = controller.data, controller.config
    return {
        'generation': data.get('budget_generation', 0),
        'model_calls': data['model_calls'], 'max_model_calls': limits['max_model_calls'],
        'elapsed_seconds': data['elapsed_seconds'], 'max_seconds': limits['max_seconds'],
        'failed_attempts': data.get('failed_attempts', data['attempts']), 'max_failed_attempts': limits['max_repairs'] + 1,
        'attempts': data['attempts'],
        'repair_rounds': data.get('repair_rounds', 0), 'max_repairs': limits['max_repairs'],
        'total_model_calls': data.get('archived_model_calls', 0) + data['model_calls'],
        'total_elapsed_seconds': data.get('archived_elapsed_seconds', 0) + data['elapsed_seconds'],
        'previous': (data.get('budget_history') or [None])[-1],
        'allocation': allocation_plan(limits, data['plan'], data.get('verification_page_counts'), controller._configured_limits,
                                      data.get('verification_selection_count', 0))
        if data.get('plan') is not None else None,
        'explicit_limits': sorted(k for k in ('max_model_calls', 'max_seconds') if k in controller._configured_limits),
        'renewal': 'Explicit Resume starts a new bounded allocation; task-wide usage and acceptance requirements are retained.',
    }


async def renew_on_resume(controller, request_id, runtime):
    """The service sets request_id; no model tool can replenish this allocation."""
    data = controller.data
    if not request_id or request_id == data.get('budget_resume_request'):
        return
    data['budget_resume_request'] = request_id
    exhausted = (data['model_calls'] >= controller.config['max_model_calls']
                 or data['elapsed_seconds'] >= controller.config['max_seconds'] or not controller.can_repair())
    needs_renewal = data['state'] in {'blocked', 'needs_repair'} or exhausted
    has_usage = data['model_calls'] or data['attempts'] or data['elapsed_seconds']
    if data['state'] == 'passed' or not needs_renewal or not has_usage:
        return
    previous = controller.assembly.publish_artifact({
        'budget': public_budget(controller), 'reason': data['reason'], 'results': deepcopy(data['results']),
        'limits_basis': 'Effective configuration at Resume; older allocations may have used earlier defaults.',
        'goal_digest': data['goal_digest'], 'revision': data['revision'], 'at': now_iso(), 'resume_request': request_id,
    }, 'acceptance_budget_history')
    data.setdefault('budget_history', []).append(previous)
    data['budget_history'] = data['budget_history'][-64:]
    data['archived_model_calls'] = data.get('archived_model_calls', 0) + data['model_calls']
    data['archived_elapsed_seconds'] = data.get('archived_elapsed_seconds', 0) + data['elapsed_seconds']
    data['budget_generation'] = data.get('budget_generation', 0) + 1
    data.update(model_calls=0, elapsed_seconds=0, attempts=0, failed_attempts=0, repair_rounds=0, state='not_ready',
                reason='Acceptance budget renewed by explicit Resume; fresh verification is required')
    unfinished = any(item['state'] != 'committed' for item in data.get('verification_ledger', {}).get('items', {}).values())
    if data.get('pending_verification') or unfinished:
        # A newer candidate is still being judged. Earlier repair feedback may
        # already be satisfied; replaying it here can turn pending command
        # judgments into UNKNOWN_GAP or repeat completed solver work.
        data.pop('resume_feedback_pending', None)
        data['resume_verification_pending'] = True
    elif data.get('feedback'):
        data['feedback']['state'] = 'pending'
        data['resume_feedback_pending'] = True
    for key in ('cause_code', 'decision', 'assessed_progress'):
        data.pop(key, None)
    controller._completion_receipt = None
    data.pop('blocked_signature', None)
    await controller.emit(runtime, 'acceptance.budget.renewed', previous_budget=previous, resume_request=request_id)
