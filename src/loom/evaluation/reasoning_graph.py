"""Complete recorded execution flow, with separately cited reasoning relationships."""

from collections import defaultdict

from loom.evaluation.behavior_contracts import stable_id

VERSION = 'loom.reasoning-graph.v3'


def project_process(questions, nodes, relations, scopes=()):
    claims = [n for n in nodes if n['kind'] == 'claim']
    by_line = defaultdict(list)
    for c in claims:
        by_line[c['line']].append(c)
    replacements, selected = {}, []
    responses = {n.get('round_id'): n for n in nodes if n['kind'] == 'model_response'}
    for node in nodes:
        n = dict(node)
        kind = n['kind']
        if kind == 'model_response' and n.get('role') == 'solver':
            if n['line'] in by_line:
                replacements[n['id']] = [c['id'] for c in by_line[n['line']]]
                continue
            n.update(phase='judgment', label=n.get('public_statement') or 'Model response recorded; statement unavailable',
                     status='recorded_statement', column=0)
        elif kind == 'model_request' and n.get('role') == 'solver' and n.get('round_id') not in responses:
            n.update(phase='deliberation', label='Model call without a recorded response', status='incomplete', column=0)
        elif kind == 'claim':
            response = next((r for r in responses.values() if r['line'] == n['line']), {})
            n.update(phase=n['claim_type'], column=0, round_id=response.get('round_id'))
        elif kind in {'action', 'collection'}:
            phase = {'finish': 'delivery', 'complete_node': 'completion_attempt', 'create_report': 'report', 'docs_output': 'report'}.get(n.get('tool_id'))
            n.update(phase=phase or ('collect' if n.get('classification') == 'read' else 'action'), column=1)
        elif kind in {'observation', 'imported_observation'}:
            interpretations = [s['label'] for c in claims for s in c.get('observation_summaries', [])
                               if s['id'] == n.get('tool_call_id', n.get('import_origin'))]
            n.update(phase='rejection' if n.get('status') == 'rejected' else 'report' if n.get('report_formed') else
                     'failure' if n.get('status') == 'failed' else 'observation', column=2,
                     label=n.get('outcome_label') or ' / '.join(dict.fromkeys(interpretations)) or n.get('result_excerpt') or n['label'],
                     interpretation_basis='inferred' if interpretations else 'recorded')
        elif kind == 'check':
            n.update(phase='verification', column=1)
        elif kind == 'run_state':
            n.update(phase='execution_state', column=1)
            n['content'] = n['label']
            n['label'] = {'running': 'Execution started / resumed', 'paused': 'Execution paused', 'suspended': 'Execution suspended',
                          'completed': 'Execution completed', 'failed': 'Execution failed', 'stopped': 'Execution stopped'}.get(n['status'], n['label'])
        elif kind in {'artifact_changed', 'decision_recorded'}:
            # Tool/result and response phases already carry these events when they have a binding.
            if n.get('decision_basis') == 'explicit_control_action':
                continue
            n.update(phase='artifact' if kind == 'artifact_changed' else 'decision', column=1)
        else:
            continue
        n['question_ids'] = list(dict.fromkeys([c['question_id'] for c in claims
            if c['id'] == n['id'] or n.get('tool_call_id') in c['observation_ids'] + c['action_ids']]))
        if not n['question_ids']:
            n['question_ids'] = [q['id'] for q in questions if q['episode_id'] == n['episode_id']
                                 and q['goal_revision_id'] == n['goal_revision_id'] and q['id'] == n['goal_revision_id']]
        selected.append(n)

    graph_nodes, graph_edges, boundaries = [], [], []
    for scope in scopes:
        key = scope['episode_id'], scope['goal_revision_id']
        rows = [n for n in selected if (n['episode_id'], n['goal_revision_id']) == key]
        scoped_questions = [q for q in questions if (q['episode_id'], q['goal_revision_id']) == key]
        root_q = next((q for q in scoped_questions if q['id'] == scope['id']), {})
        common = dict(episode_id=key[0], goal_revision_id=key[1], run_id=scope.get('run_id'), role='process',
                      basis='recorded', column=1, question_ids=[q['id'] for q in scoped_questions])
        start = dict(common, id=f"{scope['id']}:question", kind='question', phase='question', line=scope['origin'] or 0,
                     label=scope['label'], status='active_goal', evidence_refs=root_q.get('evidence_refs', []))
        for q in scoped_questions:
            if q['id'] == root_q.get('id'):
                continue
            rows.append(dict(common, id=f"{q['id']}:question", kind='question', phase='question', line=q.get('origin') or start['line'],
                             label=q['label'], status='subquestion', evidence_refs=q.get('evidence_refs', []), question_ids=[q['id']]))
        states = sorted((n for n in rows if n['kind'] == 'run_state'), key=lambda n: n['line'])
        last_state = states[-1] if states else None
        finishes = [n for n in rows if n.get('tool_id') == 'finish' and n.get('completed') and n.get('status') == 'success']
        reports = [n for n in rows if n.get('report_formed')]
        status = last_state['status'] if last_state else 'unknown'
        boundary_source = last_state
        if finishes and (not last_state or finishes[-1]['line'] > last_state['line']):
            status, boundary_source = 'completed', finishes[-1]
        if scope.get('superseded'):
            status = 'superseded'
            boundary_source = None
        ended = status in {'completed', 'failed', 'stopped', 'superseded'}
        end_label = {'completed': 'Task completed · report formed' if reports else 'Task completed',
                     'failed': 'Task failed', 'stopped': 'Task stopped', 'superseded': 'Goal superseded',
                     'paused': 'Paused · awaiting continuation', 'suspended': 'Suspended · awaiting continuation',
                     'running': 'Running · awaiting outcome', 'queued': 'Queued · awaiting execution'}.get(status, 'Trace ended · outcome unrecorded')
        end = dict(common, id=f"{scope['id']}:outcome", kind='terminal' if ended else 'boundary', phase='outcome',
                   line=max(boundary_source['line'] if boundary_source else scope.get('end') or start['line'],
                            max((n['line'] for n in rows), default=start['line'])),
                   label=end_label, status=status, terminal=ended, is_endpoint=True,
                   report_formed=bool(reports), evidence_refs=boundary_source.get('evidence_refs', []) if boundary_source else [],
                   content=(boundary_source or {}).get('content', ''), basis='recorded' if boundary_source else 'snapshot_boundary')
        # The last run-state event is represented by the outcome, not duplicated in the body.
        if last_state and boundary_source is last_state:
            rows.remove(last_state)
            replacements[last_state['id']] = [end['id']]
        rows.sort(key=lambda n: (n['line'], 0 if n['kind'] == 'question' else 1, n['id']))
        # A verification round often emits one event per criterion plus its gate result.
        # Keep all source details while presenting one phase in the execution flow.
        grouped, index = [], 0
        while index < len(rows):
            n = rows[index]
            if n['kind'] != 'check':
                grouped.append(n)
                index += 1
                continue
            batch = [n]
            index += 1
            while index < len(rows) and rows[index]['kind'] == 'check':
                batch.append(rows[index])
                index += 1
            checks = [r for r in batch if not r.get('event_type', '').startswith('acceptance.gate.')]
            gate = next((r for r in reversed(batch) if r.get('event_type', '').startswith('acceptance.gate.')), None)
            state = gate['status'] if gate else 'passed' if all(r.get('status') == 'passed' for r in batch) else 'mixed'
            identifier = stable_id('verification_phase', *[r['id'] for r in batch])
            for r in batch:
                replacements[r['id']] = [identifier]
            grouped.append(dict(n, id=identifier, status=state, label=f"{'Acceptance' if gate else 'Verification'} {state}"
                                + (f" · {len(checks)} checks" if checks else ''), end_line=batch[-1]['line'],
                                verification_results=batch, evidence_refs=[ref for r in batch for ref in r.get('evidence_refs', [])],
                                content='\n\n'.join(r['label'] for r in batch)))
        rows = grouped
        ids = {n['id'] for n in [start, *rows, end]}
        edges, seen_edges = [], set()

        def add(a, b, kind, basis='deterministic', layer='flow', refs=(), *, ids=ids, seen_edges=seen_edges, edges=edges, **extra):
            if a == b or a not in ids or b not in ids or (a, b, kind, layer) in seen_edges:
                return
            seen_edges.add((a, b, kind, layer))
            edges.append(dict(id=stable_id('process_edge', a, b, kind, layer), source=a, target=b, kind=kind,
                              basis=basis, layer=layer, evidence_refs=list(refs), **extra))

        for r in relations:
            for a in replacements.get(r['source'], [r['source']]):
                for b in replacements.get(r['target'], [r['target']]):
                    add(a, b, r['kind'], r.get('basis', 'recorded'), 'reasoning' if r.get('basis') == 'inferred' else 'flow',
                        r.get('evidence_refs', []))
        # A frontier joins finished branches into the next recorded phase. These edges express control flow,
        # never evidence adoption. Explicit parent/result bindings preserve parallel tool branches.
        incoming = defaultdict(list)
        for e in edges:
            if e['layer'] == 'flow':
                incoming[e['target']].append(e['source'])
        processed, frontier = {start['id']: start}, {start['id']}
        for n in [*rows, end]:
            parents = [p for p in incoming[n['id']] if p in processed]
            # A tool can run verification before returning (notably finish). Retain both
            # its call/result binding and the intervening check's recorded progression.
            if n['kind'] == 'observation' and parents:
                checks = [p for p in frontier if processed[p]['kind'] == 'check'
                          and processed[p]['line'] < n['line']
                          and any(processed[a]['line'] < processed[p]['line'] for a in parents)]
                for check in checks:
                    add(check, n['id'], 'continues', refs=processed[check].get('evidence_refs', []) + n.get('evidence_refs', []),
                        meaning='Recorded verification precedes the tool result; does not establish evidential support or adoption')
                parents += checks
            if not parents or n['id'] == end['id']:
                parents = sorted(frontier, key=lambda p: (processed[p]['line'], p))
                for parent in parents:
                    previous = processed[parent]
                    kind = 'ends' if n['id'] == end['id'] else 'next_round' if n['column'] == 0 else 'continues'
                    if kind == 'next_round' and previous.get('status') in {'rejected', 'failed'}:
                        kind = 'continues_after_failure'
                    add(parent, n['id'], kind, refs=previous.get('evidence_refs', []) + n.get('evidence_refs', []),
                        meaning='Recorded execution progression; does not establish evidential support or adoption')
            frontier.difference_update(parents)
            frontier.add(n['id'])
            processed[n['id']] = n
        graph_nodes += [start, *rows, end]
        graph_edges += edges
        boundaries.append(dict(scope_id=scope['id'], start_id=start['id'], end_id=end['id'], status=status, terminal=ended))
    return dict(schema_version=VERSION, nodes=graph_nodes, relations=graph_edges,
                questions=[dict(q, phase='question') for q in questions], boundaries=boundaries,
                time_basis='Recorded execution flow with fork/join tool bindings; reasoning edges require separate cited judgments')
