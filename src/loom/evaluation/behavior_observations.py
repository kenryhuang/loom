"""Deterministic behavior observations and costs, independent of semantic evaluation."""

import hashlib
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict
from datetime import datetime

from loom.evaluation.behavior_contracts import stable_id
from loom.evaluation.evidence_store import text_value
from loom.evaluation.observation_cost import observation_cost_counter


def goal_at(goals, episode, line):
    return max((g for g in goals if g['episode_id'] == episode and g.get('applied_line') is not None
                and g['applied_line'] <= line and not g.get('covered_by_revision')),
               key=lambda g: g['applied_line'], default=None)


def _result(store, tool):
    value = store.resolve(tool['raw_output_ref']) if tool.get('raw_output_ref') else None
    if isinstance(value, str):
        return {'content': value}
    if isinstance(value, list):
        return {'results': value}
    if not isinstance(value, dict):
        return None
    # Known output fields only: duration, argv, observation IDs are transport metadata.
    selected = {k: value[k] for k in ('stdout', 'stderr', 'exit_code', 'status', 'ok', 'content', 'results', 'error') if k in value}
    if not any(k in selected for k in ('stdout', 'stderr', 'content', 'results', 'error')):
        return None
    return selected


def _tool_duration(store, tool):
    refs = tool.get('evidence_refs', [])
    if not refs or not tool.get('raw_output_ref'):
        return None
    value = store.resolve(tool['raw_output_ref'])
    duration = value.get('duration_ms') if isinstance(value, dict) else None
    if type(duration) in (int, float) and duration >= 0:
        return duration
    try:
        start = store.event(tool.get('input_ref') or refs[0]).at
        end = store.event(tool['raw_output_ref']).at
        return max(0, round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000))
    except (ValueError, TypeError):
        return None


def enrich_sequence(store, sequence, plans, calls, tools, segments, by_line, goals, episodes, ledger):
    """Add source-linked facts; no source execution, model calls, or claims of useful progress."""
    facts = dict(model_calls=calls, tool_uses=tools, episodes=episodes, goal_revisions=goals, token_ledger=ledger,
                 event_episode_ids={str(k): v for k, v in by_line.items()}, event_times={str(e.line_number): e.at for e in store.events})
    call_map = {c['id']: c for c in calls}
    owners = {r: s['id'] for s in segments for r in s['round_ids']}
    plans_by_id = {p['id']: p for p in plans}
    observations, bindings = [], []
    interval_cost = observation_cost_counter(facts)
    scope_index = {}
    for segment in segments:
        scope = segment['episode_id'], segment['goal_revision_id']
        scope_index.setdefault(scope, []).append((min(r['line_number'] for r in segment['evidence_refs']), segment['id']))
    scope_index = {key: ([r[0] for r in sorted(rows)], [r[1] for r in sorted(rows)]) for key, rows in scope_index.items()}

    def scopes(line, ep, goal_id):
        # Include terminal verification after the last solver round, without crossing goals/episodes.
        lines, ids = scope_index.get((ep, goal_id), ([], []))
        index = bisect_right(lines, line) - 1
        return [ids[index]] if index >= 0 else []

    def observation(kind, line, ep, label, refs, **extra):
        goal = goal_at(goals, ep, line)
        row = dict(id=stable_id('observation', kind, line, label), kind=kind, line=line, episode_id=ep,
                   goal_revision_id=goal['id'] if goal else None, label=label, evidence_refs=refs,
                   basis='recorded', semantic_progress='not_assessed', **extra)
        row.setdefault('segment_ids', scopes(line, ep, row['goal_revision_id']))
        observations.append(row)
        return row

    for call in calls:
        if call['usage_role'] == 'verification':
            continue
        plan = plans_by_id.get(call.get('plan_snapshot_id'))
        node = next((n for n in (plan or {}).get('nodes', []) if n['id'] == call.get('plan_node_id')), None)
        status = 'bound' if node else 'ambiguous_node' if plan and plan.get('active_node_ids') else 'no_active_node' if plan else 'no_effective_plan'
        nodes = {n['id']: n for n in (plan or {}).get('nodes', [])}
        unmet = [d for d in (node or {}).get('dependencies', []) if nodes.get(d, {}).get('declared_status') not in {'completed', 'done'}]
        ref = call.get('request_ref') or call.get('response_ref')
        refs = ([ref] if ref else []) + (plan or {}).get('evidence_refs', [])
        if call.get('response_ref'):
            refs.append(call['response_ref'])
        row = dict(round_id=call['id'], plan_snapshot_id=(plan or {}).get('id'), node_id=(node or {}).get('id'),
                   status=status, unmet_dependencies=unmet, dependency_status='not_declared_complete' if unmet else 'satisfied' if node else 'unknown',
                   activity_after_declared_completion=bool(node and node['declared_status'] in {'completed', 'done'}), evidence_refs=refs)
        bindings.append(row)
        if ref:
            action_lines = [call['response_ref']['line_number']] if call.get('response_ref') else []
            action_lines += [r['line_number'] for t in tools if t['round_id'] == call['id']
                             for r in (t.get('input_ref'), t.get('raw_output_ref')) if r]
            observation('plan_binding', ref['line_number'], call['episode_id'], (node or {}).get('description') or status, refs,
                        round_id=call['id'], step_number=call['step_number'], action_line=(call.get('response_ref') or ref)['line_number'],
                        action_lines=action_lines,
                        **{k: v for k, v in row.items() if k not in {'round_id', 'evidence_refs'}})
    sequence['plan_bindings'] = bindings
    for row in sequence['plan_execution']:
        row['recorded_execution'] = 'observed' if row['round_ids'] else 'no_recorded_calls'
    sequence['plan_summary'] = {**dict(Counter(r['status'] for r in bindings)),
                                'dependency_violations': sum(bool(r['unmet_dependencies']) for r in bindings),
                                'calls_after_declared_completion': sum(r['activity_after_declared_completion'] for r in bindings),
                                'nodes_without_recorded_calls': sum(r['effective'] and not r['round_ids'] for r in sequence['plan_execution'])}

    eligible = [t for t in tools if t['usage_role'] != 'verification']
    resource_tools = {r['tool_call_id'] for r in sequence['resources']}
    prior, comparable = {}, []
    existing_pairs = {tuple(r['tool_ids']) for r in sequence['repetitions']}
    for tool in sorted(eligible, key=lambda t: (t.get('raw_output_ref') or t.get('input_ref') or {}).get('line_number', 0)):
        result = _result(store, tool)
        if result is None or tool['status'] != 'complete':
            continue
        comparable.append(tool['id'])
        call = call_map.get(tool['round_id'], {})
        ref = tool['raw_output_ref']
        ep = call.get('episode_id') or by_line.get(ref['line_number'])
        goal = call.get('goal_revision_id')
        digest = hashlib.sha256(text_value(result).encode()).hexdigest()
        key = ep, goal, digest
        previous = prior.get(key)
        if previous and (previous['id'], tool['id']) not in existing_pairs:
            same_input = bool(tool.get('input_ref') and previous.get('input_ref') and
                              text_value(store.resolve(tool['input_ref'])) == text_value(store.resolve(previous['input_ref'])))
            sequence['repetitions'].append(dict(id=stable_id('repeat-output', previous['id'], tool['id']),
                kind='same_tool_result', path=f"{tool['tool_id']} output", episode_id=ep, goal_revision_id=goal,
                first_line=previous['raw_output_ref']['line_number'], line=ref['line_number'],
                tool_ids=[previous['id'], tool['id']], round_ids=[previous['round_id'], tool['round_id']],
                content_sha256=digest, same_input=same_input, status='needs_context',
                limitations=['Returned payload equality does not identify files, prove unchanged workspace state, or establish waste.'],
                basis='Identical recorded result fields; command metadata and duration excluded',
                evidence_refs=[previous['raw_output_ref'], ref]))
        prior[key] = tool
        if previous is None and tool['id'] not in resource_tools and not any(s['line'] == ref['line_number']
                and s['kind'] == 'tool_result_observed' for s in sequence['signals']):
            sequence['signals'].append(dict(id=stable_id('signal', 'tool_result_observed', ref['line_number']),
                kind='tool_result_observed', line=ref['line_number'], episode_id=ep, round_id=tool['round_id'],
                step_number=tool['step_number'], label=tool['tool_id'], evidence_refs=[ref],
                basis='recorded', semantic_progress='not_assessed'))
    tool_map, usage = {t['id']: t for t in tools}, {r['round_id']: r for r in ledger}

    def repeat_cost(ids):
        chosen = [tool_map[i] for i in sorted(set(ids))]
        durations = [_tool_duration(store, t) for t in chosen]
        rounds = sorted({t['round_id'] for t in chosen})
        tokens = [usage.get(r, {}).get('total_tokens') for r in rounds]
        return dict(tool_calls=len(chosen), known_tool_ms=sum(v or 0 for v in durations),
                    unmeasured_tool_calls=sum(v is None for v in durations), associated_model_calls=len(rounds),
                    associated_known_tokens=sum(v or 0 for v in tokens), unmeasured_model_calls=sum(v is None for v in tokens),
                    round_ids=rounds, tool_ids=sorted(set(ids)), basis='Repeated calls only; model usage is associated, not attributable waste or savings')

    repeated_ids = []
    for row in sequence['repetitions']:
        row.setdefault('kind', 'same_resource_content')
        row['cost'] = repeat_cost(row['tool_ids'][1:])
        repeated_ids.extend(row['tool_ids'][1:])
        obs = observation(row['kind'], row['line'], row['episode_id'], row['path'], row['evidence_refs'],
                          repetition_id=row['id'], first_line=row['first_line'], cost=row['cost'],
                          status=row['status'], limitations=row['limitations'],
                          segment_ids=[owners[row['round_ids'][-1]]] if row['round_ids'][-1] in owners else [])
        if row['kind'] == 'same_tool_result' and row.get('same_input'):
            sequence['candidate_windows'].append(dict(id=obs['id'], pattern_type='repeated_unchanged_result',
                segment_ids=list(dict.fromkeys(owners[r] for r in row['round_ids'] if r in owners)),
                status='candidate', basis=row['basis'], evidence_refs=row['evidence_refs']))
    sequence['repetition_cost'] = repeat_cost(repeated_ids)
    for window in sequence['candidate_windows']:
        if window['pattern_type'] == 'low_progress_window' and window.get('evidence_refs'):
            refs = window['evidence_refs']
            line = max(r['line_number'] for r in refs)
            observation('no_new_result_window', line, by_line.get(line),
                        f"{window['tool_calls']} calls without a new recognizable result", refs,
                        first_line=min(r['line_number'] for r in refs), status='candidate',
                        limitations=[window['basis']], tool_calls=window['tool_calls'])
    sequence['observation_coverage'] = dict(solver_tool_calls=len(eligible), completed_tool_calls=sum(t['status'] == 'complete' for t in eligible),
                                          comparable_result_calls=len(comparable), resource_identity_calls=len(resource_tools),
                                          calls_without_resource_identity=len(eligible) - len(resource_tools))

    for signal in sequence['signals']:
        line, ep = signal['line'], signal['episode_id']
        goal = goal_at(goals, ep, line)
        signal['goal_revision_id'] = goal['id'] if goal else None
        signal['segment_ids'] = scopes(line, ep, signal['goal_revision_id'])
        extra = {k: signal[k] for k in ('round_id', 'step_number') if k in signal}
        if signal['kind'] == 'verification_result':
            event = store.event(signal['evidence_refs'][0])
            payload = event.payload
            result = payload.get('result') or {}
            acceptance = payload.get('acceptance') or {}
            status = result.get('status') or payload.get('status') or acceptance.get('state') or 'unknown'
            if event.event_type == 'acceptance.gate.passed':
                status = 'passed'
            elif event.event_type == 'acceptance.gate.blocked':
                status = 'blocked'
            extra.update(status=status, assurance=result.get('assurance') or payload.get('assurance'),
                         criterion_id=result.get('criterion_id'), event_type=event.event_type,
                         goal_digest=payload.get('goal_digest'), recorded_goal_revision=payload.get('goal_revision'),
                         goal_binding='unrecorded' if payload.get('goal_revision') is None or not goal or goal.get('producer_revision') is None
                         else 'matched' if payload['goal_revision'] == goal['producer_revision'] else 'mismatch')
        observation(signal['kind'], line, ep, signal['label'], signal['evidence_refs'], **extra)
    for event in store.events:
        p = event.payload
        if event.event_type in {'decision.recorded', 'plan.submitted', 'plan.updated', 'workflow.revised'}:
            text = p.get('decision') or p.get('explanation') or p.get('reason')
            if isinstance(text, str) and text.strip():
                observation('decision_recorded', event.line_number, by_line.get(event.line_number), text,
                            [asdict(store.ref(event))], event_type=event.event_type)
    for tool in eligible:
        if tool['tool_id'] in {'continue_react', 'enter_plan', 'submit_plan', 'update_plan', 'complete_node', 'finish'} and tool.get('input_ref'):
            ref = tool['input_ref']
            related = [r for r in [ref, tool.get('raw_output_ref'), call_map.get(tool['round_id'], {}).get('response_ref')] if r]
            observation('decision_recorded', ref['line_number'], by_line.get(ref['line_number']),
                        f"Selected {tool['tool_id']}", related, decision_basis='explicit_control_action',
                        action_lines=[r['line_number'] for r in related],
                        status=tool['status'], round_id=tool['round_id'], step_number=tool['step_number'])
    # Costs start at applied goal/episode boundaries. Adjacent observation intervals own calls once.
    observations.sort(key=lambda r: r['line'])
    episode_map = {e['id']: e for e in episodes}
    last, milestones = {}, {}
    for row in observations:
        ep = episode_map.get(row['episode_id'])
        if ep is None:
            continue
        goal = goal_at(goals, row['episode_id'], row['line'])
        origin = max(ep['start_line'], goal['applied_line'] if goal else ep['start_line'])
        row['at'] = facts['event_times'].get(str(row['line']))
        row['since_goal'] = interval_cost(origin, row['line'], row['episode_id'])
        scope = row['episode_id'], row['goal_revision_id']
        row['since_previous_observation'] = interval_cost(last.get(scope, origin), row['line'], row['episode_id'], exclusive_start=scope in last)
        last[scope] = row['line']
        kind = 'first_content' if row['kind'] in {'resource_observed', 'tool_result_observed'} else (
            'first_decision' if row['kind'] == 'decision_recorded' else (
            'first_acceptance_pass' if row.get('event_type') == 'acceptance.gate.passed' else
            'first_verification_pass' if row['kind'] == 'verification_result' and row.get('status') in {'passed', 'supported'} else None))
        if kind and row.get('goal_binding') != 'mismatch':
            milestones.setdefault((*scope, kind), dict(kind=kind, observation_id=row['id'], line=row['line'], at=row['at'],
                                                       episode_id=scope[0], goal_revision_id=scope[1], cost=row['since_goal']))
    sequence.update(schema_version='loom.behavior-sequence.v2', observations=observations, milestones=list(milestones.values()))


def focus_observations(facts, segment, limit=8):
    """Bound semantic work to selected source-linked facts; uncovered rows stay unreviewed."""
    rows = [r for r in facts.get('sequence', {}).get('observations', []) if segment['id'] in r.get('segment_ids', [])]
    def priority(row):
        if (row['kind'] in {'same_resource_content', 'same_tool_result', 'no_new_result_window'}
                or row.get('unmet_dependencies') or row.get('activity_after_declared_completion')):
            return 0
        if row['kind'] in {'verification_result', 'decision_recorded'}:
            return 1
        return 2 if row['kind'] in {'resource_observed', 'tool_result_observed', 'artifact_changed'} else 3
    selected = sorted(rows, key=lambda r: (priority(r), r['line']))[:limit]
    fields = ('id', 'kind', 'line', 'label', 'evidence_refs', 'segment_ids', 'status', 'assurance', 'criterion_id',
              'event_type', 'goal_binding', 'unmet_dependencies', 'dependency_status', 'activity_after_declared_completion',
              'limitations', 'first_line', 'action_line', 'action_lines')
    result = []
    for row in selected:
        compact = {k: row[k] for k in fields if k in row}
        compact['cost_from_goal'] = {k: row.get('since_goal', {}).get(k) for k in ('known_tokens', 'model_calls', 'tool_calls', 'elapsed_ms')}
        if row.get('cost'):
            compact['repeated_call_cost'] = {k: v for k, v in row['cost'].items() if k not in {'round_ids', 'tool_ids'}}
        result.append(compact)
    return result
