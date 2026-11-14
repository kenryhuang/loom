"""Frozen source directory shared by every batch in one verification snapshot.

Paths describe dependencies, not an exhaustive account of what a shell receipt
contains. Directory selection is separate from loading original evidence.
"""

import json
from copy import deepcopy

VIEW_VERSION = 3


class SourceDirectory:
    def __init__(self, batch):
        self.batch, self.assembly = batch, batch.assembly
        self.entries = None

    def load(self):
        if self.entries is not None:
            return self.entries
        from loom.tasks.acceptance_evidence import coalesce_reads, current_calls

        saved = self.batch.pending.get('source_directory')
        if saved:
            value = self.batch.ledger.read(saved)
            self.batch.ledger.validate_sources(value)
        else:
            calls = coalesce_reads(current_calls(self.assembly))
            checks = {c['id']: c for c in self.batch.gate.data['plan']['criteria']}
            rows = [(r, True) for r in calls]
            rows.extend((r, False) for r in self.batch.evidence if r['id'] not in {'candidate', 'tool-call-coverage'}
                        and not r['id'].startswith('tool:'))
            entries = []
            for row, tool in rows:
                item = deepcopy(row)
                ref = self.assembly.publish_artifact({'row': item, 'tool_receipt': tool}, 'verification_source')
                criterion = checks.get(row['id'], {})
                check = criterion.get('check', {})
                entries.append({'id': row['id'], 'source_ref': ref, 'tool': row.get('tool') or check.get('tool_id', ''),
                    'paths': row.get('paths', criterion.get('scope', [])),
                    'input': row.get('input') or json.dumps(check.get('input', {}), ensure_ascii=False),
                    'description': criterion.get('description', row['id']),
                    'ok': row.get('ok', row.get('result', {}).get('status') in {'passed', 'pending'}),
                    'content_bytes': (row.get('artifact') or {}).get('byte_size', 0) + ref.get('byte_size', ref.get('size', 0)),
                    'origin': 'tool_receipt' if tool else 'verification_check' if criterion else 'report',
                    'equivalent_call_ids': row.get('equivalent_call_ids', [row['id']])})
            value = {'version': VIEW_VERSION, 'sources': entries}
            work = self.batch.ledger.item('discover_sources', {'version': VIEW_VERSION, 'binding': self.batch.binding},
                                          {'sources': entries})
            self.batch.ledger.commit(work, value)
            saved = self.assembly.publish_artifact(value, 'verification_source_directory')
            self.batch.pending['source_directory'] = saved
            self.batch.ledger.persist()
        self.entries = value['sources']
        return self.entries

    def materialize(self, entry):
        """Only the host can turn a registered ID into its immutable source row."""
        from loom.service.contracts import ServiceError
        from loom.tasks.acceptance import AcceptanceBlocked
        from loom.tasks.acceptance_evidence import materialize_calls

        try:
            value = self.batch.read_source(entry['source_ref']['sha256'])
            if value['tool_receipt'] and value['row'].get('artifact'):
                self.batch.read_source(value['row']['artifact']['sha256'])
        except (KeyError, ValueError, OSError, ServiceError) as exc:
            raise AcceptanceBlocked('Saved verification source is unavailable; restore it before resuming',
                                    code='ACCEPTANCE_EVIDENCE_UNAVAILABLE') from exc
        row = value['row']
        if row['id'] != entry['id']:
            raise AcceptanceBlocked('Verification source identity mismatch', code='ACCEPTANCE_EVIDENCE_UNAVAILABLE')
        if value['tool_receipt']:
            return materialize_calls(self.assembly, [row], full=True, read_artifact=self.batch.read_source)[0]
        self.batch.ledger.validate_sources(row)
        return deepcopy(row)

    def candidates(self, criteria, *, target_ids=(), target_tools=(), targets=(), broad=False):
        ids = {c['id'] for c in criteria}
        # Semantic questions can depend on ANY check or tool receipt, including
        # another batch's command output. A directory preview is not proof.
        semantic = any(c['verifier'] == 'semantic' for c in criteria)
        selected = []
        for row in self.load():
            if row['id'] in ids:
                continue  # Its direct check is already pinned in the batch.
            match = row['id'] in target_ids or row['tool'] in target_tools
            match |= any(p == t or p.startswith(t.rstrip('/') + '/') for p in row['paths'] for t in targets)
            # A path can appear in command input even though recorded paths only
            # contain cwd. This is a selection hint, never a parsed dependency.
            match |= any(t in row['input'] for t in targets)
            if semantic or broad or match:
                selected.append({**row, 'directory_only': True, 'pinned': match})
        return selected


def receipt_view(value, receipt_id=None):
    """Remove inaccessible nested citation APIs from host-produced summaries."""
    if isinstance(value, list):
        return [receipt_view(row, receipt_id) for row in value]
    result = deepcopy(value)
    if not isinstance(result, dict):
        return result
    result.pop('evidence_ids', None)
    if receipt_id:
        result['receipt_id'] = receipt_id
    if isinstance(result.get('execution_check'), dict):
        result['execution_check'] = receipt_view(result['execution_check'], receipt_id)
    return result


def citation_view(evidence):
    """Small request-local names; nested receipt provenance is not a citation API.

    The bijection is persisted with the request and decoded only after strict
    validation. Original source IDs are never guessed or accepted by similarity.
    """
    aliases = {}
    occupied = {r['id'] for r in evidence}
    if len(occupied) != len(evidence):
        from loom.tasks.acceptance import AcceptanceBlocked

        raise AcceptanceBlocked('Duplicate evidence IDs in verification view', code='ACCEPTANCE_EVIDENCE_UNAVAILABLE')
    for index, row in enumerate(evidence):
        identifier = row['id']
        if len(identifier) <= 64:
            continue
        prefix = identifier.split(':', 1)[0]
        prefix = prefix if prefix in {'tool', 'report', 'verification'} else 'evidence'
        alias = f'{prefix}:ref_{index + 1}'
        while alias in occupied:
            alias += '_'
        aliases[identifier] = alias
        occupied.add(alias)
    view = []
    for row in evidence:
        item = deepcopy(row)
        item['id'] = aliases.get(row['id'], row['id'])
        # Only host-owned receipt fields are transformed. Source text, command
        # output and candidate contents are untouched untrusted data.
        for key in ('result', 'results', 'observations'):
            if key in item:
                item[key] = receipt_view(item[key], item['id'])
        view.append(item)
    return view, {alias: identifier for identifier, alias in aliases.items()}


def resolve_citations(rows, aliases):
    rows = deepcopy(rows)
    for row in rows:
        row['evidence_ids'] = [aliases.get(ref, ref) for ref in row['evidence_ids']]
        for target in (row.get('gap') or {}).get('targets', []):
            if target['kind'] == 'source':
                target['value'] = aliases.get(target['value'], target['value'])
    return rows


def validate_receipt(manifest, assembly):
    """A persisted passed flag is not authority without a valid cited response."""
    from loom.tasks.acceptance import AcceptanceBlocked
    from loom.tasks.acceptance_batches import validate_page
    from loom.tasks.acceptance_protocols import validate_verification

    try:
        rows, criteria, evidence = manifest['results'], manifest['criteria'], manifest['evidence']
        if 'page' in manifest:
            validate_page({'observations': rows}, criteria, evidence)
        else:
            validate_verification({'results': rows}, criteria, evidence, assembly)
        return rows
    except (KeyError, TypeError, ValueError) as exc:
        raise AcceptanceBlocked('Saved verification receipt has invalid criteria or evidence references',
                                code='ACCEPTANCE_EVIDENCE_UNAVAILABLE') from exc
