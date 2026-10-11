"""Versioned acquisition and execution ledger. No model or workspace reads."""

import json
import shlex
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict

from loom.evaluation.behavior_contracts import stable_id
from loom.evaluation.evidence_store import text_value

VERSION = 'loom.reasoning-activity.v3'
READ_TOOLS = {'read_file', 'list_directory', 'search_files', 'web_search', 'fetch_url', 'knowledge_search'}
WRITE_TOOLS = {'write_file', 'edit_file', 'create_report', 'docs_output'}
CONTROL_TOOLS = {'finish', 'enter_plan', 'submit_plan', 'update_plan', 'complete_node', 'continue_react'}


def classify(tool, inputs, metadata=None):
    """Explicit operation metadata and conservative historical patterns, never shell execution."""
    operations = (metadata or {}).get('operations', [])
    kinds = {o.get('kind') for o in operations if isinstance(o, Mapping)} if isinstance(operations, list | tuple) else set()
    if kinds and kinds <= {'read', 'write', 'execute', 'control'}:
        return (next(iter(kinds)) if len(kinds) == 1 else 'mixed'), 'recorded_operations', len(operations), 'read' in kinds
    if tool in READ_TOOLS:
        return 'read', 'tool_registry', None, True
    if tool in WRITE_TOOLS:
        return 'write', 'tool_registry', None, False
    if tool in CONTROL_TOOLS:
        return 'control', 'tool_registry', None, False
    if tool in {'shell_execute', 'process_execute'}:
        command = inputs.get('command') if isinstance(inputs, Mapping) else None
        if isinstance(command, str) and not any(c in command for c in '\n\r;&|><$`*?(){}'):
            try:
                argv = shlex.split(command)
                if len(argv) >= 2 and argv[0] == 'cat' and all(not p.startswith('-') for p in argv[1:]):
                    return 'read', 'simple_cat_allowlist', len(argv) - 1, True
            except ValueError:
                pass
        # A shell can mix any operations; its name alone establishes no acquisition coverage.
        return 'unknown', 'opaque_command', None, False
    return 'unknown', 'unregistered_tool', None, False


def build_reasoning_activity(store, facts):
    models = {c['id']: c for c in facts['model_calls']}
    ledger = {c['id']: c for c in facts['base']['statistics']['calls']['tools']}
    goals = defaultdict(list)
    for g in facts['goal_revisions']:
        if g.get('applied_line') is not None and not g.get('covered_by_revision'):
            goals[g['episode_id']].append((g['applied_line'], g['id']))
    goal_index = {ep: ([r[0] for r in sorted(rows)], [r[1] for r in sorted(rows)]) for ep, rows in goals.items()}
    repeats = defaultdict(list)
    for r in facts.get('sequence', {}).get('repetitions', []):
        repeats[r['line']].append(r['id'])
    acquisitions, anchors, edges, public = [], [], [], {}
    for c in models.values():
        response = store.resolve(c['response_ref']) if c.get('response_ref') else {}
        content = response.get('content') if isinstance(response, Mapping) else response
        try:
            parsed = json.loads(content) if isinstance(content, str) else {}
        except (ValueError, TypeError):
            parsed = {}
        if isinstance(parsed, dict):
            action = parsed.get('action')
            action = action if isinstance(action, dict) else {}
            public[c['id']] = dict(judgment=parsed.get('reasoning') or (content if not parsed else None), purpose=action.get('description'))
        scope = {k: c.get(k) for k in ('episode_id', 'goal_revision_id', 'run_id', 'step_number')}
        for kind, key in (('model_request', 'request_ref'), ('model_response', 'response_ref')):
            ref = c.get(key)
            if ref:
                judgment = public.get(c['id'], {}).get('judgment') if kind == 'model_response' else None
                anchors.append(dict(id=f"{c['id']}:{kind}", kind=kind, line=ref['line_number'], label=kind.replace('_', ' '),
                                    public_statement=str(judgment)[:1600] if judgment else None,
                                    round_id=c['id'], role=c['usage_role'], evidence_refs=[ref], basis='recorded', **scope))
        if c.get('request_ref') and c.get('response_ref'):
            edges.append(dict(id=stable_id('execution', c['id']), source=f"{c['id']}:model_request",
                              target=f"{c['id']}:model_response", kind='returns', basis='recorded'))
    for t in facts['tool_uses']:
        ref = t.get('input_ref') or t.get('raw_output_ref') or next(iter(t.get('evidence_refs', [])), None)
        if not ref:
            continue
        parent = models.get(t['round_id'], {})
        ep = facts['event_episode_ids'].get(str(ref['line_number']))
        lines, ids = goal_index.get(ep, ([], []))
        gi = bisect_right(lines, ref['line_number']) - 1
        goal = ids[gi] if gi >= 0 else None
        measured = ledger.get(t['id'], {})
        event = store.event(ref)
        metadata = event.payload.get('metadata') or {}
        inputs = store.resolve(t['input_ref']) if t.get('input_ref') else {}
        kind, basis, operation_count, has_read = classify(t['tool_id'], inputs, metadata)
        raw = t.get('raw_output_ref')
        if not raw and measured.get('status') == 'failed':
            failure = next((store.event(r) for r in t.get('evidence_refs', []) if store.event(r).event_type == 'tool.failed'), None)
            if failure:
                raw = asdict(store.ref(failure, 'error' if 'error' in failure.payload else None))
        role = measured.get('role', parent.get('usage_role', t.get('usage_role', 'solver')))
        # Explicit top-level and metadata roles take precedence over inherited defaults.
        role = event.payload.get('usage_role') or metadata.get('usage_role') or parent.get('usage_role') or role
        path = inputs.get('path') if isinstance(inputs, Mapping) else None
        command = inputs.get('command') if isinstance(inputs, Mapping) else None
        purpose = public.get(t['round_id'], {}).get('purpose')
        raw_value = store.resolve(raw) if raw else None
        outcome = raw_value if isinstance(raw_value, Mapping) else {}
        result_status = 'rejected' if outcome.get('accepted') is False else measured.get('status', 'unknown')
        report = outcome.get('report')
        report = report if isinstance(report, str) and report.strip() else None
        report_formed = bool(report) and result_status == 'success'
        result_label = str(outcome.get('reason') or outcome.get('error') or '')[:600]
        if report_formed:
            result_label = f"Report formed: {report.splitlines()[0].lstrip('# ').strip()}"
        if isinstance(raw_value, Mapping):
            raw_value = raw_value.get('stdout') or raw_value.get('text') or raw_value.get('content') or raw_value
        output = text_value(raw_value) if raw_value is not None else ''
        excerpt = output if len(output) <= 1000 else output[:450] + '\n… [middle omitted] …\n' + output[-450:]
        activity_label = str(purpose or f"{t['tool_id']}{': ' + str(path or command) if path or command else ''}")[:600]
        row = dict(id=t['id'], tool_id=t['tool_id'], round_id=t['round_id'], run_id=t.get('run_id'), episode_id=ep,
                   goal_revision_id=goal, step_number=t.get('step_number'), role=role, classification=kind,
                   classification_basis=basis, classifier_version=VERSION, operation_count=operation_count,
                   contains_read=has_read, line=ref['line_number'], result_line=raw['line_number'] if raw else None,
                   request_ref=t.get('input_ref'), result_ref=raw, status=measured.get('status', 'unknown'),
                   result_status=result_status, completed=outcome.get('completed') is True, report_formed=report_formed,
                   ownership_basis='request' if t.get('input_ref') else 'terminal_fallback',
                   duration_ms=measured.get('duration_ms'), label=activity_label,
                   result_excerpt=excerpt,
                   resource=path, result_chars=t.get('output_char_length'), truncated=t.get('source_truncated'),
                   injections=t.get('injections', []), repeat_fact_ids=repeats.get(raw['line_number'] if raw else None, []),
                   evidence_refs=t.get('evidence_refs', []))
        acquisitions.append(row)
        scope = {k: row[k] for k in ('episode_id', 'goal_revision_id', 'run_id', 'step_number', 'role')}
        for suffix, anchor, anchor_kind in [('request', t.get('input_ref'), 'collection' if kind == 'read' else 'action'),
                                           ('result', raw, 'observation')]:
            if anchor:
                anchors.append(dict(id=f"{t['id']}:{suffix}", kind=anchor_kind, line=anchor['line_number'],
                                    label=(result_label or row['label']) if suffix == 'result' else row['label'],
                                    tool_id=t['tool_id'], tool_call_id=t['id'], round_id=t['round_id'], classification=kind,
                                    status=result_status if suffix == 'result' else row['status'],
                                    outcome_label=result_label if suffix == 'result' else None,
                                    completed=row['completed'] if suffix == 'result' else False,
                                    report_formed=report_formed if suffix == 'result' else False,
                                    content=excerpt if suffix == 'result' else str(command or path or purpose or '')[:1600],
                                    result_excerpt=excerpt if suffix == 'result' else None,
                                    evidence_refs=[anchor], basis='recorded', **scope))
        if t.get('input_ref') and raw:
            edges.append(dict(id=stable_id('execution', t['id']), source=f"{t['id']}:request", target=f"{t['id']}:result",
                              kind='returns', basis='recorded'))
        if parent.get('response_ref') and t.get('input_ref'):
            edges.append(dict(id=stable_id('emits', t['id']), source=f"{parent['id']}:model_response",
                              target=f"{t['id']}:request", kind='emits', basis='recorded'))
    for o in facts.get('sequence', {}).get('observations', []):
        if o['kind'] not in {'verification_result', 'decision_recorded', 'artifact_changed'}:
            continue
        label = o['label']
        if o.get('event_type', '').startswith('acceptance.gate.') and o.get('evidence_refs'):
            payload = store.event(o['evidence_refs'][0]).payload
            label = (payload.get('acceptance') or {}).get('reason') or payload.get('reason') or label
        anchors.append({**o, 'label': label, 'kind': 'check' if o['kind'] == 'verification_result' else o['kind'],
                        'role': 'verification' if o['kind'] == 'verification_result' else 'solver'})
    for event in store.events:
        status = event.payload.get('state') if event.event_type == 'run.state.changed' else {
            'run.started': 'running', 'run.completed': 'completed', 'run.failed': 'failed', 'run.stopped': 'stopped',
            'run.paused': 'paused', 'run.suspended': 'suspended',
        }.get(event.event_type)
        if not status:
            continue
        ep = facts['event_episode_ids'].get(str(event.line_number))
        lines, ids = goal_index.get(ep, ([], []))
        gi = bisect_right(lines, event.line_number) - 1
        ref = dict(source_sha256=store.source_sha256, line_number=event.line_number, event_hash=event.hash)
        anchors.append(dict(id=stable_id('run_state', ep, event.line_number), kind='run_state', line=event.line_number,
                            episode_id=ep, goal_revision_id=ids[gi] if gi >= 0 else None, run_id=event.run_id, role='process',
                            status=status, label=str(event.payload.get('reason') or event.payload.get('error') or event.payload.get('message') or status)[:600],
                            evidence_refs=[ref], basis='recorded'))
    counts = Counter(r['classification'] for r in acquisitions if r['role'] == 'solver')
    return dict(schema_version=VERSION, acquisitions=acquisitions,
                execution_anchors=sorted(anchors, key=lambda a: (a['line'], a['id'])), execution_edges=edges,
                classification_coverage=dict(total=sum(counts.values()), known=sum(v for k, v in counts.items() if k != 'unknown'),
                                             by_class=dict(counts), basis='solver_physical_calls'))
