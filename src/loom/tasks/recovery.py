"""Host recovery classification. Descriptions never choose a transition."""

from copy import deepcopy

from loom.tasks.workspace_probe import digest

TOOL_COLLECTIONS = {
    'read_file': 'filesystem', 'write_file': 'filesystem', 'edit_file': 'filesystem',
    'shell_execute': 'shell', 'process_execute': 'shell', 'browser_capture': 'browser',
    'fetch_url': 'web_research', 'create_report': 'document_outputs', 'finish': 'task_control',
}
BUDGET_CODES = {'ACCEPTANCE_BUDGET_EXHAUSTED', 'ACCEPTANCE_INPUT_BUDGET', 'ACCEPTANCE_OUTPUT_BUDGET', 'ACCEPTANCE_TIMEOUT'}
MANUAL_CODES = {'EXECUTION_UNKNOWN', 'ACCEPTANCE_EXTERNAL_DEPENDENCY', 'ACCEPTANCE_UNKNOWN_GAP', 'ACCEPTANCE_BINDING_UNAVAILABLE',
                'ACCEPTANCE_SOURCE_CHANGED'}
PLAN_CODES = {'TOOLS_UNAVAILABLE', 'TOKEN_BUDGET', 'ACCEPTANCE_CONTRACT', 'EXTERNAL_DEPENDENCY', 'EXECUTION_UNKNOWN'}


def assess(state, *, failure=None):
    """Report every cause, including simultaneous capability and budget gaps."""
    acceptance = state.get('acceptance') or {}
    decision = acceptance.get('decision') or {}
    code = decision.get('reason_code') or acceptance.get('cause_code') or (state.get('run') or {}).get('stop_code')
    causes = []

    def add(code, **details):
        row = dict(code=code, details=details)
        if row not in causes:
            causes.append(row)

    failure = failure if failure is not None else (state.get('run') or {}).get('failure') or {}
    if (state.get('run') or {}).get('stop_code') == 'ACTIVE_TIME_BUDGET_EXCEEDED':
        add('ACTIVE_TIME_BUDGET')
    if (failure.get('code') in {'LLM_FAILED', 'LLM_TRANSPORT_ERROR', 'TIMEOUT'}
            and failure.get('retryable') is True):
        add('PROVIDER_TRANSIENT', failure=failure)
    budget = acceptance.get('budget') or decision.get('budget') or {}
    if code in BUDGET_CODES or any(budget.get(used, 0) >= budget.get(limit, float('inf')) for used, limit in (
            ('model_calls', 'max_model_calls'), ('elapsed_seconds', 'max_seconds'))):
        add('ACCEPTANCE_BUDGET', budget=budget, cause_code=code, input_budget=acceptance.get('input_budget', {}))
    if code in MANUAL_CODES:
        add(code)
    if code == 'TOKEN_BUDGET_EXCEEDED':
        add('TOKEN_BUDGET')
    if code in {'ACCEPTANCE_PROTOCOL', 'ACCEPTANCE_FAILED', 'ACCEPTANCE_VERIFICATION_ERROR'}:
        add('ACCEPTANCE_PROTOCOL', protocol=acceptance.get('protocol', {}))
    if (state.get('run') or {}).get('stop_code') == 'PLAN_INCOMPLETE':
        add('PLAN_INCOMPLETE')
    for result in acceptance.get('results', []):
        if result.get('status') == 'passed' or result.get('deferred'):
            continue
        gap = result.get('gap') or {}
        targets = gap.get('targets', [])
        missing = [t['value'] for t in targets if t.get('kind') == 'tool']
        if missing:
            add('TOOLS_REQUIRED', tool_ids=sorted(set(missing)), criterion_id=result['criterion_id'])
        if gap.get('kind') == 'external_dependency':
            add('ACCEPTANCE_EXTERNAL_DEPENDENCY', criterion_id=result['criterion_id'], gap=gap)
        elif gap.get('kind') == 'insufficient_evidence_view':
            add('EVIDENCE_VIEW', criterion_id=result['criterion_id'], gap=gap)
        elif gap.get('kind') == 'verification_method':
            add('ACCEPTANCE_CONTRACT', criterion_id=result['criterion_id'], gap=gap)
    for item in (state.get('plan') or {}).get('items', []):
        if item.get('status') == 'blocked':
            blocker = item.get('blocker') or {}
            kind = blocker.get('code')
            add({'TOKEN_BUDGET': 'TOKEN_BUDGET', 'TOOLS_UNAVAILABLE': 'TOOLS_REQUIRED'}.get(kind, kind or 'UNKNOWN'),
                tool_ids=blocker.get('tool_ids', []), plan_item_id=item['id'])
    if acceptance.get('state') in {'blocked', 'needs_repair'}:
        if (acceptance.get('plan') or {}).get('criteria') and (acceptance.get('results') or code not in BUDGET_CODES):
            add('ACCEPTANCE_REVIEW', assessment_id=decision.get('assessment_id'))
        elif not causes:
            add('LEGACY_ACCEPTANCE', unresolved_requirements=(acceptance.get('plan') or {}).get('unresolved_requirements', []))
    if not causes:
        return None
    source = dict(run_id=(state.get('run') or {}).get('id'), task_revision=state['task'].get('revision'),
                  task=state['task'], acceptance=acceptance, plan=state.get('plan'), autonomy=state.get('autonomy'), failure=failure)
    mode = 'reassemble'
    if any(c['code'] in MANUAL_CODES | {'EXTERNAL_DEPENDENCY', 'UNKNOWN'} for c in causes):
        mode = 'manual'
    elif ({c['code'] for c in causes} <= {'ACCEPTANCE_BUDGET', 'TOKEN_BUDGET', 'PROVIDER_TRANSIENT', 'ACTIVE_TIME_BUDGET'}
          and not any(c['details'].get('plan_item_id') for c in causes)
          and code not in {'ACCEPTANCE_INPUT_BUDGET', 'ACCEPTANCE_OUTPUT_BUDGET'}):
        mode = 'resume'
    return dict(schema_version=1, source_key=digest(source), causes=causes, mode=mode,
                reset=['workflow', 'plan', 'acceptance', 'context'] if mode == 'reassemble' else [],
                original_objective=(state.get('recovery_context') or {}).get('original_objective', state['task']['objective']))


def validate_plan_blocker(value):
    if not isinstance(value, dict) or set(value) - {'code', 'tool_ids'} or value.get('code') not in PLAN_CODES:
        raise ValueError('Blocked plan items require a structured blocker code')
    tools = value.get('tool_ids', [])
    if not isinstance(tools, list) or any(not isinstance(t, str) or not t for t in tools):
        raise ValueError('blocker.tool_ids must be tool IDs')
    if value['code'] == 'TOOLS_UNAVAILABLE' and not tools:
        raise ValueError('TOOLS_UNAVAILABLE requires tool_ids')
    return deepcopy(value)
