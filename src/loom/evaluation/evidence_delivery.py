"""Immutable evidence spans and request-local handles. No model-generated offsets."""

from copy import deepcopy

from loom.evaluation.goal_loops.facts import identity
from loom.evaluation.issues import RuleViolation

VERSION = "evidence-delivery.1"
SPAN_CHARS = 400
PAGE_CHARS = 1200


def spans(entries):
    result, seen = [], set()
    for entry in entries:
        text = entry['text']
        start = 0
        while start < len(text):
            end = min(start + SPAN_CHARS, len(text))
            newline = text.rfind('\n', start, end)
            if end < len(text) and newline > start:
                end = newline + 1
            pointer = deepcopy(entry['ref'])
            if not entry.get('redacted'):
                pointer.update(start=(pointer.get('start') or 0) + start, end=(pointer.get('start') or 0) + end)
            value = text[start:end]
            key = 'span:' + identity(VERSION, pointer, value, entry['action_id'])
            if key not in seen:
                result.append({**entry, 'id': key, 'text': value, 'ref': pointer,
                               'text_digest': identity(value), 'display_range': [start, end]})
                seen.add(key)
            start = end
    return result


def manifest(unit):
    """Freeze the precise authorized spans for a request; handles are local."""
    actions = {a['id']: index + 1 for index, a in enumerate(unit['scope']['actions'])}
    documents, handles = {}, {}
    for entry in unit['evidence']:
        ref = entry['ref']
        doc = (entry['action_id'], ref['source_sha256'], ref['line_number'], ref.get('field_path'))
        docs = documents.setdefault(entry['action_id'], {})
        doc_number, count = docs.get(doc, (len(docs) + 1, 0))
        docs[doc] = (doc_number, count + 1)
        if unit.get('delivery_span_ids') is not None and entry['id'] not in unit['delivery_span_ids']:
            continue
        handle = f"A{actions[entry['action_id']]}.E{doc_number}.S{count + 1}"
        handles[handle] = entry['id']
    selected = set(handles.values())
    digest = identity(VERSION, unit['id'], handles, [(e['id'], e['text']) for e in unit['evidence'] if e['id'] in selected])
    return dict(version=VERSION, id='delivery:' + digest, handles=handles,
                span_digests={e['id']: identity(e['text']) for e in unit['evidence'] if e['id'] in selected})


def resolve(unit, handles):
    delivery = unit['manifest']
    entries = {e['id']: e for e in unit['evidence']}
    selected = []
    for handle in dict.fromkeys(handles):
        key = delivery['handles'].get(handle)
        if key not in entries:
            raise RuleViolation('memory.anchor_undelivered', 'Evidence handle was not delivered in this request',
                                path=('evidence',), evidence_id=handle)
        entry = entries[key]
        if delivery['span_digests'].get(key) != identity(entry['text']):
            raise RuleViolation('checkpoint.evidence_digest', 'Delivered evidence content changed', category='state', path=('evidence',))
        selected.append(entry)
    return selected


def deliver(unit):
    delivery = unit['manifest']
    entries = {e['id']: e for e in unit['evidence']}
    return [{**{k: entries[key][k] for k in ('text', 'action_id', 'basis', 'truncated')},
             'id': handle, 'field': entries[key]['ref'].get('field_path', '')}
            for handle, key in delivery['handles'].items()]


def prepare(unit):
    if unit.get('protocol_version') != 2:
        unit.setdefault('legacy_evidence', deepcopy(unit['evidence']))
        unit['evidence'] = spans(unit['evidence'])
        unit['protocol_version'] = 2
    unit['manifest'] = manifest(unit)


def cited_evidence(records, units):
    """Synthesis may see only original accepted quotes, never their omitted context."""
    evidence = {e['id']: e for u in units for e in [*u.get('legacy_evidence', []), *u['evidence']]}
    selected = []
    for record in records:
        for citation in record['citations']:
            entry = evidence.get(citation['ref'])
            if entry is None or citation['quote'] not in entry['text']:
                raise RuleViolation('checkpoint.citation_source', 'Accepted citation source is unavailable', category='state')
            start = entry['text'].index(citation['quote'])
            pointer = deepcopy(entry['ref'])
            if not entry.get('redacted'):
                offset = pointer.get('start') or 0
                pointer.update(start=offset + start, end=offset + start + len(citation['quote']))
            selected.append({**entry, 'ref': pointer, 'text': citation['quote'], 'quotes': [citation['quote']]})
    return spans(selected)


def page_catalog(unit, source):
    """Only documents already in the window may expose additional trace pages."""
    from loom.evaluation.evidence_store import text_value
    from loom.evaluation.memory.evidence import sanitize

    pages = {}
    seen = set()
    for entry in unit['evidence']:
        if not entry['truncated'] or entry.get('redacted'):
            continue
        pointer = {**entry['ref'], 'start': None, 'end': None}
        doc = identity(pointer, entry['action_id'])
        if doc in seen:
            continue
        seen.add(doc)
        try:
            raw = text_value(source.resolve(pointer))
        except (KeyError, ValueError):
            continue
        # Never authorize original-coordinate pages after redaction changed offsets.
        if sanitize(raw) != raw:
            continue
        for start in range(0, len(raw), PAGE_CHARS):
            end = min(start + PAGE_CHARS, len(raw))
            covered = sorted((e['ref'].get('start') or 0, e['ref'].get('end') or 0)
                             for e in unit['evidence'] if e['action_id'] == entry['action_id']
                             and {**e['ref'], 'start': None, 'end': None} == pointer)
            cursor = start
            for lo, hi in covered:
                if lo <= cursor:
                    cursor = max(cursor, hi)
            if cursor >= end:
                continue
            key = 'page:' + identity(doc, start, end)
            pages[key] = dict(ref={**pointer, 'start': start, 'end': end}, action_id=entry['action_id'],
                              basis=entry['basis'], truncated=True, redacted=False)
            if len(pages) >= 32:
                return pages
    return pages


def read_page(unit, handle, source):
    from loom.evaluation.evidence_store import text_value
    from loom.evaluation.memory.evidence import sanitize

    page = unit.get('pages', {}).get(handle)
    if page is None:
        raise RuleViolation('memory.page_undelivered', 'Page is outside the delivered document catalog', path=('evidence_requests',))
    try:
        text = text_value(source.resolve(page['ref']))
    except (KeyError, ValueError) as exc:
        raise RuleViolation('memory.source_unavailable', 'The requested trace page is unavailable', path=('evidence_requests',)) from exc
    if sanitize(text) != text:
        raise RuleViolation('memory.source_unavailable', 'The requested trace page is not safe to deliver', path=('evidence_requests',))
    return spans([{**page, 'id': handle, 'text': text}])
