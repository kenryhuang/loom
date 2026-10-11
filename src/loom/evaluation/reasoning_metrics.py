"""Host-owned process graph, window accounting and conditional distributions."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from math import ceil
from statistics import median

from loom.evaluation.base_metrics import timestamp
from loom.evaluation.behavior_contracts import stable_id
from loom.evaluation.reasoning_process import VERSION


def cost_index(facts):
    """One prefix index per goal/role; no cumulative identity arrays in report nodes."""
    rows, step_rows = defaultdict(list), defaultdict(list)
    activity = facts.get('reasoning_activity', {})
    ledger = {r['round_id']: r for r in facts.get('token_ledger', [])}
    for c in facts.get('model_calls', []):
        ref = c.get('request_ref') or c.get('response_ref')
        if not ref:
            continue
        scope = c['episode_id'], c.get('goal_revision_id'), c.get('usage_role', 'solver')
        step_rows[scope].append((ref['line_number'], (c.get('run_id'), c.get('trace_id'), c.get('step_number'))))
        usage = ledger.get(c['id'], {})
        values = dict(model_calls=1, ownership_fallback=int(not c.get('request_ref')))
        for field in ('total_tokens', 'prompt_tokens', 'completion_tokens'):
            values[field] = usage.get(field) or 0
            values['missing_' + field] = int(usage.get(field) is None)
        rows[c['episode_id'], c.get('goal_revision_id'), c.get('usage_role', 'solver')].append((ref['line_number'], values))
    for t in activity.get('acquisitions', []):
        kind = t['classification']
        values = dict(tool_calls=1, reads=int(kind == 'read'), read_successes=int(kind == 'read' and t['status'] == 'success'),
                      read_including_mixed=int(t['contains_read']), mixed_calls=int(kind == 'mixed'), unknown_calls=int(kind == 'unknown'),
                      repeated_reads=int(kind == 'read' and bool(t['repeat_fact_ids'])),
                      tool_duration_ms=t['duration_ms'] or 0, missing_tool_duration=int(t['duration_ms'] is None),
                      ownership_fallback=int(t['ownership_basis'] != 'request'))
        for status in ('failed', 'cancelled', 'incomplete', 'unknown'):
            values['read_' + status] = int(kind == 'read' and t['status'] == status)
        rows[t['episode_id'], t['goal_revision_id'], t['role']].append((t['line'], values))
    fields = {'model_calls', 'tool_calls', 'reads', 'read_successes', 'read_including_mixed', 'mixed_calls', 'unknown_calls',
              'repeated_reads', 'tool_duration_ms', 'missing_tool_duration', 'ownership_fallback',
              'total_tokens', 'prompt_tokens', 'completion_tokens', 'missing_total_tokens', 'missing_prompt_tokens',
              'missing_completion_tokens', 'read_failed', 'read_cancelled', 'read_incomplete', 'read_unknown'}
    indexes = {}
    for scope, values in rows.items():
        values.sort(key=lambda r: r[0])
        prefixes = {f: [0] for f in fields}
        for _, amounts in values:
            for f in fields:
                prefixes[f].append(prefixes[f][-1] + amounts.get(f, 0))
        indexes[scope] = ([line for line, _ in values], prefixes)
    step_indexes = {scope: ([r[0] for r in sorted(values)], [r[1] for r in sorted(values)]) for scope, values in step_rows.items()}
    times = {int(k): timestamp(v) for k, v in facts.get('event_times', {}).items()}
    runs = {e['id']: e.get('run_id') for e in facts.get('episodes', [])}
    runtime = facts.get('base', {}).get('statistics', {}).get('timing', {}).get('intervals', [])

    def cost(scope, start, end, *, exclusive=False, role='solver'):
        if start is None or end is None or end < start:
            return dict(accounting_status='unknown', basis='missing_or_reversed_endpoints')
        lines, prefixes = indexes.get((*scope, role), ([], {f: [0] for f in fields}))
        a, b = bisect_right(lines, start) if exclusive else bisect_left(lines, start), bisect_right(lines, end)
        result = {f: prefixes[f][b] - prefixes[f][a] for f in fields}
        steps, identities = step_indexes.get((*scope, role), ([], []))
        left = bisect_right(steps, start) if exclusive else bisect_left(steps, start)
        result['steps'] = len({sid for sid in identities[left:bisect_right(steps, end)] if sid[-1] is not None})
        result['active_runtime_ms'] = None
        if times.get(start) is not None and times.get(end) is not None and runtime:
            result['active_runtime_ms'] = sum(max(0, min(times[end], r['end_ms']) - max(times[start], r['start_ms']))
                                              for r in runtime if r['run_id'] == runs.get(scope[0]) and r['state'] == 'running')
        elapsed = times.get(end) - times.get(start) if times.get(end) is not None and times.get(start) is not None else None
        result.update(start_line=start, end_line=end, role=role, exclusive_start=exclusive,
                      accounting_status='partial' if result['ownership_fallback'] or result['missing_total_tokens'] or result['unknown_calls']
                      or result['mixed_calls'] else 'measured',
                      wall_ms=elapsed if elapsed is not None and elapsed >= 0 else None,
                      time_basis='wall_span_including_waits', active_runtime_basis='all_role_running_lifecycle_overlap',
                      token_basis='final_usage_of_request_owned_calls')
        return result
    return cost, sorted({s[2] for s in indexes})


def distribution(values):
    ordered = sorted(values)
    return dict(n=len(values), mean=sum(values) / len(values) if values else None,
                median=median(values) if values else None, p90=ordered[ceil(.9 * len(values)) - 1] if values else None)


def build_reasoning_process(facts, semantic=None):
    semantic = semantic or {}
    transitions = sorted({r['id']: r for r in semantic.get('reasoning_transitions', [])}.values(), key=lambda r: (r['line'], r['id']))
    activity = facts.get('reasoning_activity', {})
    tools_by_id = {t['id']: t for t in activity.get('acquisitions', [])}
    cost, roles = cost_index(facts)
    episodes = {e['id']: e for e in facts.get('episodes', [])}
    goals = [g for g in facts.get('goal_revisions', []) if g.get('applied_line') is not None and not g.get('covered_by_revision')]
    scopes = {}
    for g in goals:
        episode = episodes.get(g['episode_id'], {})
        scopes[g['episode_id'], g['id']] = dict(id=g['id'], episode_id=g['episode_id'], goal_revision_id=g['id'],
            label=g['objective'], origin=g['applied_line'], end=episode.get('end_line'), run_id=episode.get('run_id'),
            terminal=episode.get('execution_status') in {'completed', 'failed', 'stopped'})
    for c in facts.get('model_calls', []) + activity.get('acquisitions', []):
        scope = c['episode_id'], c.get('goal_revision_id')
        if scope not in scopes:
            ep = episodes.get(scope[0], {})
            scopes[scope] = dict(id=stable_id('unscoped', *scope), episode_id=scope[0], goal_revision_id=scope[1],
                                 label='Goal not recorded', origin=ep.get('start_line'), end=ep.get('end_line'),
                                 run_id=ep.get('run_id'), terminal=ep.get('execution_status') in {'completed', 'failed', 'stopped'})
    for ep in episodes.values():
        if not any(scope[0] == ep['id'] for scope in scopes):
            scopes[ep['id'], None] = dict(id=stable_id('unscoped', ep['id'], None), episode_id=ep['id'], goal_revision_id=None,
                label='Goal not recorded', origin=ep.get('start_line'), end=ep.get('end_line'), run_id=ep.get('run_id'),
                terminal=ep.get('execution_status') in {'completed', 'failed', 'stopped'})
    ordered_scopes = sorted(scopes.values(), key=lambda s: s['origin'] or 0)
    for i, s in enumerate(ordered_scopes):
        later = next((n for n in ordered_scopes[i + 1:] if n['episode_id'] == s['episode_id']), None)
        if later:
            s.update(end=later['origin'] - 1, terminal=True, superseded=True)
        scope = s['episode_id'], s['goal_revision_id']
        s['costs_by_role'] = {role: cost(scope, s['origin'], s['end'], role=role) for role in roles}
    questions = {s['id']: dict(id=s['id'], label=s['label'], origin=s['origin'], kind='unknown',
                              evidence_refs=next((g.get('evidence_refs', []) for g in goals if g['id'] == s['id']), []),
                              episode_id=s['episode_id'], goal_revision_id=s['goal_revision_id']) for s in ordered_scopes}
    for t in transitions:
        q = questions.setdefault(t['question_id'], dict(id=t['question_id'], label=t['question_label'], origin=t['question_origin'],
                      kind=t['question_kind'], episode_id=t['episode_id'], goal_revision_id=t['goal_revision_id']))
        kinds = {r['question_kind'] for r in transitions if r['question_id'] == t['question_id'] and r['question_kind'] != 'unknown'}
        q['kind'] = next(iter(kinds)) if len(kinds) == 1 else 'unknown'
    windows, relations, cycles = [], list(activity.get('execution_edges', [])), []
    import_nodes = {}
    by_claim = defaultdict(list)
    for t in transitions:
        previous = by_claim[t['claim_id']][-1] if by_claim[t['claim_id']] else None
        if previous:
            relations.append(dict(id=stable_id('revises', t['id']), source=previous['id'], target=t['id'],
                                  kind='revises', basis='inferred', evidence_refs=t['evidence_refs']))
        by_claim[t['claim_id']].append(t)
        for oid in t['observation_ids']:
            tool = tools_by_id.get(oid)
            source_id = f'{oid}:result' if tool else oid
            imported = next((i for i in t.get('imports', []) if i['observation_id'] == oid), None)
            if imported:
                source_id = stable_id('imported_observation', oid, t['episode_id'], t['goal_revision_id'])
                import_nodes[source_id] = dict(id=source_id, kind='imported_observation', line=imported['delivery_ref']['line_number'],
                    role='solver', episode_id=t['episode_id'], goal_revision_id=t['goal_revision_id'], question_id=t['question_id'],
                    label=f"Imported: {tool['label'] if tool else oid}", import_origin=oid, basis='recorded',
                    evidence_refs=[tool['result_ref'], imported['delivery_ref']] if tool else t['evidence_refs'])
            relations.append(dict(id=stable_id('support', oid, t['id']), source=source_id,
                                  target=t['id'], kind='refutes' if t['status'] == 'refuted' else 'supports',
                                  basis='inferred', perspective=t['evidence_use'], evidence_refs=t['evidence_refs']))
        for aid in t['action_ids']:
            relations.append(dict(id=stable_id('selects', t['id'], aid), source=t['id'], target=f'{aid}:request',
                                  kind='selects', basis='inferred', evidence_refs=t['evidence_refs']))
            later = next((r for r in transitions if r['line'] > t['line'] and r['question_id'] == t['question_id']
                          and aid in r['observation_ids'] and r['evidence_use'] == 'adopted'), None)
            cycles.append(dict(id=stable_id('cycle', t['id'], aid), question_id=t['question_id'], start_id=t['id'],
                               end_id=later['id'] if later else None, action_id=aid, start_line=t['line'],
                               end_line=later['line'] if later else None, status='closed' if later else 'open'))
    # Several parallel actions leading to the same reassessment constitute one cycle.
    grouped = {}
    for c in cycles:
        key = c['start_id'], c['end_id']
        grouped.setdefault(key, {**c, 'action_ids': []})['action_ids'].append(c['action_id'])
    cycles = list(grouped.values())
    for scope, s in scopes.items():
        rows = [t for t in transitions if (t['episode_id'], t['goal_revision_id']) == scope]
        for series in ('evidence', 'judgment'):
            qualifying = [t for t in rows if series == 'judgment' or
                          (t['evidence_use'] == 'adopted' and t['observation_ids'] and t['status'] in {'supported', 'refuted', 'verified_within_scope'})]
            endpoints = defaultdict(list)
            for t in qualifying:
                endpoints[t['line']].append(t['id'])
            start, exclusive = s['origin'], False
            for end, ids in sorted(endpoints.items()):
                windows.append(dict(id=stable_id('window', *scope, series, start, end), scope_id=s['id'], series=series,
                                    start_line=start, end_line=end, status='closed', endpoint_status='inferred', endpoint_ids=ids,
                                    cost=cost(scope, start, end, exclusive=exclusive)))
                start, exclusive = end, True
            if start is not None and s['end'] is not None and start < s['end']:
                windows.append(dict(id=stable_id('window', *scope, series, start, s['end'], 'tail'), scope_id=s['id'], series=series,
                                    start_line=start, end_line=s['end'], status='censored' if s['terminal'] else 'open',
                                    endpoint_status='not_reviewed' if not rows else 'open_window', endpoint_ids=[],
                                    cost=cost(scope, start, s['end'], exclusive=exclusive)))
    metrics = {}
    for s in ordered_scopes:
        scope_windows = [w for w in windows if w['scope_id'] == s['id']]
        series_metrics = {}
        for series in ('evidence', 'judgment'):
            closed = [w for w in scope_windows if w['series'] == series and w['status'] == 'closed']
            tails = [w for w in scope_windows if w['series'] == series and w['status'] != 'closed']
            series_metrics[series] = {'id': stable_id('reasoning_metric', s['id'], series), **distribution([w['cost']['reads'] for w in closed]),
                                     'first_reads': closed[0]['cost']['reads'] if closed else None,
                                     'partial_windows': sum(w['cost']['accounting_status'] != 'measured' for w in closed),
                                     'open_windows': len(tails), 'open_reads': sum(w['cost'].get('reads', 0) for w in tails),
                                     'basis': 'known_pure_reads_per_reviewed_closed_goal_window'}
        metrics[s['id']] = series_metrics
    for q in questions.values():
        scope = q['episode_id'], q['goal_revision_id']
        rows = [t for t in transitions if t['question_id'] == q['id']]
        kind = 'cause' if q['kind'] == 'diagnostic' else 'conclusion'
        supported = [r for r in rows if r['claim_type'] == kind and r['status'] == 'supported' and r['evidence_use'] == 'adopted']
        endpoint = supported[0] if supported else None
        still_supported = [r for r in supported if by_claim[r['claim_id']][-1]['status'] in {'supported', 'verified_within_scope'}]
        verified = next((r for r in rows if r['claim_type'] == kind and r['status'] == 'verified_within_scope'), None)
        q['endpoint'] = dict(id=stable_id('reasoning_metric', q['id'], 'endpoint'), kind=kind,
                             applicability='unknown' if q['kind'] == 'unknown' else 'applicable',
                             status='inferred' if endpoint else 'insufficient_evidence' if rows else 'not_reviewed',
                             occurrence_id=endpoint['id'] if endpoint else None,
                             cost=cost(scope, q['origin'], endpoint['line']) if endpoint else None,
                             first_proposed_id=next((r['id'] for r in rows if r['claim_type'] == kind), None),
                             disposition=by_claim[endpoint['claim_id']][-1]['status'] if endpoint else None,
                             unrefuted_at_snapshot_id=still_supported[0]['id'] if still_supported else None,
                             verified_id=verified['id'] if verified else None,
                             cause_to_verification=cost(scope, endpoint['line'], verified['line'], exclusive=True)
                             if endpoint and verified and endpoint['claim_id'] == verified['claim_id'] else None,
                             root_cause_applicability='applicable' if q['kind'] == 'diagnostic' else
                             'unknown' if q['kind'] == 'unknown' else 'not_applicable')
        q['adoption_lags'] = [dict(occurrence_id=r['id'], cost=cost(scope, r['visible_at'], r['line'], exclusive=True))
                              for r in rows if r['evidence_use'] == 'adopted' and r['visible_at'] is not None]
        q['cycles'] = dict(closed=sum(c['status'] == 'closed' for c in cycles if c['question_id'] == q['id']),
                           open=sum(c['status'] != 'closed' for c in cycles if c['question_id'] == q['id']))
    nodes = list(activity.get('execution_anchors', [])) + list(import_nodes.values()) + [dict(t, kind='claim', role='solver') for t in transitions]
    from loom.evaluation.reasoning_graph import project_process

    return dict(schema_version=VERSION, classifier_version=activity.get('schema_version'), scopes=ordered_scopes,
                graph=project_process(list(questions.values()), nodes, relations, ordered_scopes),
                questions=list(questions.values()), occurrences=transitions, nodes=sorted(nodes, key=lambda n: (n['line'], n['id'])),
                relations=relations, cycles=cycles, windows=windows, metrics=metrics,
                coverage=dict(classification=activity.get('classification_coverage', {}),
                              reviewed_segments=semantic.get('coverage', {}).get('attempted_segments', 0),
                              source_segments=len(facts.get('segments', [])), transitions=len(transitions),
                              status=semantic.get('coverage', {}).get('status', 'not_evaluated')),
                validation_diagnostics=semantic.get('reasoning_diagnostics', []))
