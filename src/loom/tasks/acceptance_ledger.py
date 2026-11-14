"""Durable verification work, independent of solver messages and batch layout.

The task execution boundary provides the transaction/worker fencing. Artifacts
hold immutable inputs/outputs; checkpoints contain only the work index. Unknown
source dependencies deliberately retain whole-snapshot invalidation.
"""

from copy import deepcopy

from loom.tasks.workspace_probe import digest

VERSION = 1


def dependencies(binding, kind, inputs):
    """Narrow only checks with enumerated file dependencies; unknowns stay global.

    Source population changes still invalidate semantic work even when its
    selected originals did not change (new counterevidence must be considered).
    """
    criteria = [inputs['criterion']] if kind == 'check' else inputs.get('payload', {}).get('criteria', [])
    scoped = bool(criteria) and all(c.get('scope') and c['id'] != 'goal' and c['verifier'] != 'visual' for c in criteria)
    if not scoped or binding.get('files') is None:
        return {'binding': binding, 'reusable': binding.get('cacheable', False)}
    scopes = [p for c in criteria for p in c['scope']]
    files = {p: value for p, value in binding['files'].items()
             if any(p == scope or p.startswith(scope.rstrip('/') + '/') for scope in scopes)}
    value = {k: binding.get(k) for k in ('goal', 'plan', 'environment', 'source_preservation', 'task_episode_id')}
    value['files'] = files
    if kind != 'check' or any(c['verifier'] not in {'command', 'artifact'} for c in criteria):
        value.update(candidate=binding.get('candidate'), evidence=binding.get('evidence'))
    return {'binding': value, 'reusable': True}


def artifact_refs(value):
    if isinstance(value, dict):
        if isinstance(value.get('sha256'), str) and ('kind' in value or 'size' in value or 'media_type' in value):
            yield value
        for key, child in value.items():
            if key in {'input', 'output', 'content', 'candidate', 'serialized_fragment'}:
                continue  # Source contents are data, not host dependency declarations.
            if key in {'artifact', 'judgment_artifact', 'input_ref', 'output_ref'} and isinstance(child, dict) and child.get('sha256'):
                yield child
            else:
                yield from artifact_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from artifact_refs(child)


class VerificationLedger:
    def __init__(self, gate, binding):
        self.gate, self.assembly = gate, gate.assembly
        saved = gate.data.get('verification_ledger')
        if saved is None or saved.get('version') != VERSION or saved['binding'] != binding:
            retained = {}
            if saved and saved.get('version') == VERSION:
                for key, item in saved['items'].items():
                    old = item.get('dependencies', {})
                    if item['state'] != 'committed' or not old.get('reusable'):
                        continue
                    inputs = self.read(item['input_ref'])
                    if old == dependencies(binding, item['kind'], inputs):
                        retained[key] = deepcopy(item)
            saved = {'version': VERSION, 'binding': deepcopy(binding), 'items': retained, 'plans': {}, 'sequence': 0}
            gate.data['verification_ledger'] = saved
        self.data = saved

    def expire_dynamic(self):
        self.data['items'] = {key: item for key, item in self.data['items'].items()
                              if item.get('dependencies', {}).get('reusable')}
        self.data['plans'] = {}
        self.persist()

    def persist(self):
        self.data['sequence'] += 1
        self.assembly.persist_acceptance()

    def read(self, ref):
        from loom.service.contracts import ServiceError
        from loom.tasks.acceptance import AcceptanceBlocked

        try:
            if ref.get('media_type', 'application/json') != 'application/json':
                return self.assembly.read_image(ref['sha256'])
            return self.assembly.read_artifact(ref['sha256'])
        except (KeyError, ValueError, OSError, ServiceError) as exc:
            raise AcceptanceBlocked('Saved verification work artifact is unavailable; restore it before resuming',
                                    code='ACCEPTANCE_EVIDENCE_UNAVAILABLE') from exc

    def validate_sources(self, value, seen=None):
        # Validate the complete receipt DAG, including sources behind reductions.
        seen = set() if seen is None else seen
        for ref in artifact_refs(value):
            if ref['sha256'] in seen:
                continue
            seen.add(ref['sha256'])
            content = self.read(ref)
            if ref.get('kind', '').startswith(('verification', 'visual')):
                if ref.get('kind') == 'verification_evidence' and isinstance(content, dict) and 'criteria' in content:
                    from loom.tasks.acceptance_sources import validate_receipt

                    validate_receipt(content, self.assembly)
                self.validate_sources(content, seen)

    def plan(self, kind, identity, factory):
        key = digest({'kind': kind, 'identity': identity})
        saved = self.data['plans'].get(key)
        if saved is None:
            value = factory()
            saved = {'artifact': self.assembly.publish_artifact(value, 'verification_work_plan')}
            self.data['plans'][key] = saved
            self.persist()
        return self.read(saved['artifact'])

    def item(self, kind, identity, inputs):
        key = digest({'kind': kind, 'identity': identity})
        item = self.data['items'].get(key)
        input_digest = digest(inputs)
        if item is None or item.get('input_digest') != input_digest:
            item = {'id': key, 'kind': kind, 'state': 'pending', 'attempts': 0, 'input_digest': input_digest,
                    'input_ref': self.assembly.publish_artifact(inputs, 'verification_work_input'),
                    'dependencies': dependencies(self.data['binding'], kind, inputs)}
            self.data['items'][key] = item
            self.persist()
        return item

    def result(self, item):
        if item['state'] != 'committed':
            return None
        value = self.read(item['output_ref'])
        self.validate_sources(value)
        return value

    def start(self, item):
        from loom.tasks.acceptance import AcceptanceBlocked

        if item['attempts'] >= self.gate.config['max_work_attempts']:
            item['state'] = 'blocked'
            self.persist()
            raise AcceptanceBlocked('Verification work made no progress within its retry limit', code='ACCEPTANCE_NO_PROGRESS')
        item.update(state='running', attempts=item['attempts'] + 1,
                    calls_before=self.gate.data['model_calls'], seconds_before=self.gate.data['elapsed_seconds'])
        self.persist()

    def commit(self, item, output):
        # Persist the receipt before any UI event or dependent work. A failed
        # boundary propagates; no dependent operation is allowed to start.
        item.update(state='committed', output_ref=self.assembly.publish_artifact(output, 'verification_work_output'))
        self.usage(item)
        self.persist()

    def usage(self, item):
        item['model_calls'] = item.get('model_calls', 0) + max(0, self.gate.data['model_calls'] - item.pop('calls_before', self.gate.data['model_calls']))
        item['elapsed_seconds'] = item.get('elapsed_seconds', 0) + max(
            0, self.gate.data['elapsed_seconds'] - item.pop('seconds_before', self.gate.data['elapsed_seconds']))

    def interrupted(self, item, exc):
        code = getattr(exc, 'code', None)
        budget_blocked = code in {'ACCEPTANCE_BUDGET_EXHAUSTED', 'MODEL_CALL_BUDGET_EXCEEDED', 'TOKEN_BUDGET_EXCEEDED'}
        item.update(state='waiting_budget' if budget_blocked else 'retryable', error=str(exc), cause_code=code)
        # Budget admission failures do not consume a retry allowance.
        if budget_blocked and self.gate.data['model_calls'] == item.get('calls_before'):
            item['attempts'] -= 1
        self.usage(item)
        self.persist()


def record_activity(gate, kind, fields):
    """Small durable UI cursor; no artifact reads or model work for progress."""
    if kind == 'acceptance.verifying':
        gate.data.pop('verification_activity', None)
        return
    if not kind.startswith(('verification.', 'acceptance.gate.')):
        return
    current = gate.data.get('verification_activity', {})
    ids = fields.get('criterion_ids') or ([fields['criterion_id']] if fields.get('criterion_id') else [])
    if kind in {'verification.started', 'verification.batch.started', 'verification.selection.started', 'verification.pages.planned'}:
        stage = fields.get('stage') or ('select_evidence' if 'selection' in kind else 'verify_page' if 'pages' in kind else 'check')
        current = {'stage': stage, 'criterion_ids': ids, 'state': 'running'}
        if fields.get('page'):
            current['page'] = deepcopy(fields['page'])
        if 'pages' in fields:
            current['page'] = {'index': fields.get('completed_pages', 0), 'total': fields['pages']}
            current['state'] = 'planned'
        if 'sources' in fields:
            current['sources'] = fields['sources']
    elif kind in {'verification.batch.completed', 'verification.selection.completed'}:
        current = {**current, 'state': 'saved'}
        for key in ('stage', 'page', 'selected', 'sources'):
            if fields.get(key) is not None:
                current[key] = deepcopy(fields[key])
    elif kind == 'verification.completed':
        row = fields['result']
        current = {'stage': 'verify', 'criterion_ids': [row['criterion_id']], 'state': row['status'], 'reason': row.get('reason')}
    elif kind.startswith('acceptance.gate.'):
        current = {**current, 'state': gate.data['state'], 'reason': gate.data.get('reason')}
    else:
        return
    gate.data['verification_activity'] = current


def public_progress(gate):
    ledger = gate.data.get('verification_ledger')
    if not ledger:
        return None
    counts = {}
    kinds = {}
    for item in ledger['items'].values():
        counts[item['state']] = counts.get(item['state'], 0) + 1
        count = kinds.setdefault(item['kind'], {'committed': 0, 'total': 0})
        count['total'] += 1
        count['committed'] += item['state'] == 'committed'
    return {'revision': digest(ledger['binding']), 'sequence': ledger['sequence'], 'states': counts, 'work': kinds,
            'committed': counts.get('committed', 0), 'total': len(ledger['items']),
            'current': deepcopy(gate.data.get('verification_activity'))}
