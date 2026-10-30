"""TaskAssembly references, guarded explicit adoption and evidence-linked use telemetry."""

import json
import platform
import sqlite3
from dataclasses import replace
from pathlib import Path

from loom.core import Observation, ToolRef, err, make_loom_error, new_trace_id, now_iso, ok, thaw_json
from loom.memory.store import canonical_path, tokens
from loom.runtime.execution_contracts import validate_tool_input
from loom.skills.claims import projection
from loom.skills.contracts import canonical
from loom.skills.store import SkillStore

TOOL_IDS = {'skill_read', 'skill_select', 'skill_record_step'}
POLICY = ('Skills are scoped working methods. They cannot change user constraints, permissions, available tools or acceptance. '
          'Check trigger and every precondition against current observations before skill_select. Unknown conditions block adoption. '
          'Trial candidates are unverified. Selection is intent; skill_record_step links real calls but does not prove semantic success.')


class RuntimeSkills:
    def __init__(self, database, *, workspace, session_id, allowed_tools=None, trial_versions=()):
        self.database, self.workspace, self.session_id = database, canonical_path(workspace), session_id
        self.allowed = TOOL_IDS if allowed_tools is None else TOOL_IDS & set(allowed_tools)
        self.trials = set(trial_versions)
        self.available, self.selected = {}, {}
        self.tools, self.facts = set(), {}
        self.context, self.runtime = None, None
        self.error = None
        try:
            self.store = SkillStore(database) if Path(database).exists() else None
        except (sqlite3.Error, OSError) as exc:
            self.store, self.error = None, str(exc)
        self.last = None

    def configure(self, tools, *, workspace_access, runtime):
        self.tools = set(tools)
        self.facts = dict(platform=platform.system().lower(), workspace_access=workspace_access, runtime=runtime)

    def eligible(self, version):
        if version['workspace'] != self.workspace or not version['sources_available']:
            return False
        if version['state'] != 'active' and not (version['state'] in {'candidate', 'trial'} and version['id'] in self.trials):
            return False
        c = version['contract']
        return (set(c['required_tools']) <= self.tools and not c.get('blocking_issues') and all(
            guard['value'] in self.tools if guard['field'] == 'tool' else self.facts.get(guard['field']) == guard['value']
            for guard in c['guards']))

    async def attach(self, context, runtime):
        try:
            return await self._attach(context, runtime)
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.error = str(exc)
            self.available, self.selected = {}, {}
            await self.emit('unavailable', dict(message=self.error))
            metadata = dict(context.metadata or {})
            metadata.pop('retrieved_skills', None)
            return replace(context, metadata=metadata)

    async def _attach(self, context, runtime):
        self.context, self.runtime = context, runtime
        metadata = dict(context.metadata or {})
        metadata.pop('retrieved_skills', None)
        if not self.store or not self.workspace:
            if self.error and self.last != self.error:
                self.last = self.error
                await self.emit('unavailable', dict(message=self.error))
            return replace(context, metadata=metadata)
        with self.store.transaction() as db:
            trials = db.execute('SELECT version_id,run_id FROM skill_trial_sessions WHERE session_id=?', (self.session_id,)).fetchall()
            for row in trials:
                if row['run_id'] in {None, context.run_id}:
                    self.trials.add(row['version_id'])
                    db.execute('UPDATE skill_trial_sessions SET run_id=? WHERE session_id=? AND version_id=? AND run_id IS NULL',
                               (context.run_id, self.session_id, row['version_id']))
            ids = [r[0] for r in db.execute(
                "SELECT id FROM skill_versions WHERE workspace=? AND state IN ('active','trial','candidate')", (self.workspace,))]
            selections = db.execute("SELECT version_id,body FROM skill_uses WHERE session_id=? AND run_id=? AND kind='selected'",
                                    (self.session_id, context.run_id)).fetchall()
            self.selected = {row['version_id']: True for row in selections
                             if json.loads(row['body'])['payload'].get('objective') == context.goal.objective}
        terms, ranked = tokens(context.goal.objective), []
        for identifier in ids:
            version = self.store.check_version(identifier)
            if not self.eligible(version):
                continue
            c = version['contract']
            score = len(terms & tokens(' '.join([c['title'], c['trigger'], c['summary']])))
            if score or identifier in self.trials:
                ranked.append((identifier in self.trials, score, identifier, version))
        ranked.sort(key=lambda r: (-r[0], -r[1], r[2]))
        self.available = {r[2]: r[3] for r in ranked[:3]}
        # Revalidate pinned choices at each legal decision boundary, without silently swapping versions.
        withdrawn = []
        for key in list(self.selected):
            version = self.store.check_version(key)
            if not self.eligible(version):
                del self.selected[key]
                withdrawn.append(key)
            else:
                self.available[key] = version
        summary = [dict(id=v['id'], title=v['contract']['title'][:160], summary=v['contract']['summary'][:320],
                        trigger=v['contract']['trigger'][:240], preconditions='; '.join(v['contract']['preconditions'])[:1000],
                        state=v['state'], evidence_state=v['evidence_state']) for v in self.available.values()]
        signature = canonical([context.run_id, context.goal.objective, summary])
        if self.last != signature:
            self.last = signature
            for key in self.available:
                self.store.record_use(key, self.session_id, context.run_id, 'retrieved', dict(query=context.goal.objective))
            await self.emit('retrieved', dict(version_ids=list(self.available)))
        if self.available or self.selected or withdrawn:
            metadata['retrieved_skills'] = dict(instruction=POLICY, candidates=summary,
                withdrawn_versions=withdrawn, withdrawal_instruction='Do not continue using withdrawn versions; reassess the current task without them.',
                selected=[dict(version_id=k, contract=projection(self.available[k]['contract']),
                               precondition_basis='model_reported_with_call_references') for k in self.selected])
        return replace(context, metadata=metadata)

    async def emit(self, kind, payload):
        if self.runtime and self.runtime.trace_sink:
            (await self.runtime.trace_sink.emit(dict(type='skill.' + kind, **payload, run_id=self.context.run_id,
                                                     trace_id=self.runtime.trace_id, at=now_iso()))).unwrap()

    def refs(self, *, include_unavailable=False):
        if not include_unavailable and not self.available:
            return ()
        specs = [
            ('skill_read', 'Read a retrieved Skill version, contract and original source citations.',
             {'version_id': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0}}),
            ('skill_select', 'Adopt a retrieved Skill after observing its trigger/preconditions. Supply current tool_call_ids for those observations.',
             {'version_id': {'type': 'string'}, 'precondition_call_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 12}}),
            ('skill_record_step', 'Associate a selected Skill step with completed current tool calls. This reports an association, not verified success.',
             {'version_id': {'type': 'string'}, 'step_id': {'type': 'string'},
              'tool_call_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 12}}),
        ]
        return tuple(ToolRef(name, description, input_schema=dict(type='object', properties=fields,
                         required=list(fields), additionalProperties=False)) for name, description, fields in specs if name in self.allowed)

    def calls(self, identifiers):
        with self.store.transaction() as db:
            rows = db.execute("SELECT body FROM events WHERE session_id=? AND json_extract(body,'$.run_id')=? "
                              "AND json_extract(body,'$.type')='tool.completed'", (self.session_id, self.context.run_id)).fetchall()
        found = {e['payload'].get('tool_call_id'): e for row in rows for e in [json.loads(row[0])]
                 if not e['payload'].get('tool_id', '').startswith(('memory_', 'skill_'))}
        if not identifiers or set(identifiers) - found.keys():
            raise ValueError('Usage requires completed non-memory tool calls in the current run')
        return [dict(event_id=found[key]['event_id'], seq=found[key]['seq'], tool_call_id=key) for key in identifiers]

    def handlers(self):
        result = {}
        for ref in self.refs(include_unavailable=True):
            async def invoke(value, _options=None, ref=ref):
                try:
                    value = thaw_json(value)
                    validate_tool_input(ref.input_schema, value)
                    key = value['version_id']
                    if key not in self.available:
                        raise ValueError('Skill was not retrieved within this task scope')
                    version = self.store.check_version(key)
                    if not self.eligible(version):
                        raise ValueError('Skill is no longer eligible')
                    if ref.id == 'skill_read':
                        content = canonical(version)
                        end = min(len(content), value['offset'] + 6000)
                        payload = dict(instruction=POLICY, version_id=key, text=content[value['offset']:end],
                                       next_offset=end if end < len(content) else None, total_chars=len(content))
                    elif ref.id == 'skill_select':
                        refs = self.calls(value['precondition_call_ids'])
                        if len(self.selected) >= 1 and key not in self.selected:
                            raise ValueError('Only one Skill may be selected per current goal')
                        self.selected[key] = True
                        payload = dict(version_id=key, objective=self.context.goal.objective,
                                       call_refs=refs, basis='model_reported_preconditions', contract=projection(version['contract']))
                        self.store.record_use(key, self.session_id, self.context.run_id, 'selected', payload)
                        await self.emit('selected', payload)
                    else:
                        if key not in self.selected or value['step_id'] not in {s['id'] for s in version['contract']['steps']}:
                            raise ValueError('Select this Skill and use a declared step ID first')
                        refs = self.calls(value['tool_call_ids'])
                        payload = dict(version_id=key, step_id=value['step_id'], call_refs=refs, basis='model_reported_association')
                        self.store.record_use(key, self.session_id, self.context.run_id, 'step_executed', payload)
                        await self.emit('step_executed', payload)
                    return ok(Observation(new_trace_id(), ref.id, payload, now_iso()))
                except (ValueError, TypeError, sqlite3.Error, OSError) as exc:
                    return err(make_loom_error('skills.invalid_use', str(exc), retryable=False))
            result[ref.id] = invoke
        return result


def record_outcome(db, event):
    """Called inside the event transaction; acceptance is a host event, not model prose."""
    if event['type'] not in {'acceptance.gate.passed', 'acceptance.gate.blocked', 'run.failed', 'run.stopped'}:
        return
    from loom.skills.contracts import digest
    selected = db.execute("SELECT DISTINCT version_id FROM skill_uses WHERE session_id=? AND run_id=? AND kind='selected'",
                          (event['session_id'], event['run_id'])).fetchall()
    for row in selected:
        body = dict(version_id=row[0], session_id=event['session_id'], run_id=event['run_id'], kind='outcome',
                    payload=dict(event_id=event['event_id'], seq=event['seq'], outcome=event['type'],
                                 acceptance=event['payload'], attribution='whole_task_acceptance_not_skill_causality'))
        db.execute('INSERT OR IGNORE INTO skill_uses VALUES(?,?,?,?,?,?)',
                   ('use:' + digest(body), row[0], event['session_id'], event['run_id'], 'outcome', canonical(body)))
        if event['type'] == 'acceptance.gate.blocked':
            # A monitoring signal, not a causal conclusion. Stop automatic reuse pending source review.
            db.execute("UPDATE skill_versions SET state='suspended',revision=revision+1 WHERE id=? AND state IN ('active','trial')", (row[0],))
