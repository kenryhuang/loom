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


def effective_limits(defaults, configured, plan):
    limits = {**defaults, **configured}
    visual = sum(c['verifier'] == 'visual' for c in (plan or {}).get('criteria', []))
    if visual:
        rounds = limits['max_repairs'] + 1
        if 'max_model_calls' not in configured:
            # Planning + per candidate: two acquisitions per visual criterion,
            # one final review, and one format correction per request.
            limits['max_model_calls'] = min(100000, 2 + 2 * (2 * visual + 1) * rounds)
        if 'max_seconds' not in configured:
            limits['max_seconds'] = min(100000, 60 + defaults['max_seconds'] * rounds)
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
    if data.get('feedback'):
        data['feedback']['state'] = 'pending'
        data['resume_feedback_pending'] = True
    for key in ('cause_code', 'decision', 'assessed_progress'):
        data.pop(key, None)
    controller._completion_receipt = None
    data.pop('blocked_signature', None)
    await controller.emit(runtime, 'acceptance.budget.renewed', previous_budget=previous, resume_request=request_id)
