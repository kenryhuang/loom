"""Conservative Chinese/English lineage; similarity proposes dependence, not causality."""

import re
import unicodedata

from loom.memory.store import tokens
from loom.skills.contracts import digest

VERSION = 'skill-lineage.zh-en.1'


def normalize(value):
    text = unicodedata.normalize('NFKC', value).lower()
    text = re.sub(r'https?://\S+|(?:[a-z]:)?/[^\s，。；]+|\b\d+\b', ' INSTANCE ', text)
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\u3400-\u9fff]+', ' ', text)).strip()


def related(left, right):
    same_session = left['session_id'] == right['session_id']
    uncertain = any(c.get('quality', {}).get('boundary_uncertain', True) for c in (left, right))
    if same_session and (uncertain or left.get('task_episode_id') == right.get('task_episode_id')):
        return 'session_lineage'
    if (left.get('source_digest') and left.get('source_digest') == right.get('source_digest')
            and (not same_session or set(left.get('run_ids', [])) & set(right.get('run_ids', [])))):
        return 'source_digest'
    for key in ('template_lineage', 'origin_task_id'):
        if left.get(key) and left.get(key) == right.get(key):
            return key
    a, b = normalize(left['task_contract']['objective']), normalize(right['task_contract']['objective'])
    if a and a == b:
        return 'normalized_objective'
    aa, bb = tokens(a), tokens(b)
    if min(len(aa), len(bb)) >= 8 and len(aa & bb) / len(aa | bb) >= .72:
        return 'possibly_related'
    return None


def assign(cases):
    """Transitive grouping is deterministic; unknown boundaries never gain independence."""
    parents = {c['id']: c['id'] for c in cases}
    reasons = {c['id']: [] for c in cases}

    def root(key):
        while key != parents[key]:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def union(a, b):
        a, b = root(a), root(b)
        parents[max(a, b)] = min(a, b)

    for i, left in enumerate(cases):
        for right in cases[:i]:
            reason = related(left, right)
            if reason:
                union(left['id'], right['id'])
                reasons[left['id']].append(dict(case_id=right['id'], reason=reason))
    families = {c['id']: 'family:' + digest(root(c['id']))[:24] for c in cases}
    # Snapshot/template links widen experimental isolation without claiming same task.
    for i, left in enumerate(cases):
        for right in cases[:i]:
            if any(left.get(k) and left.get(k) == right.get(k) for k in ('project_snapshot_digest', 'template_lineage')):
                union(left['id'], right['id'])
    return {c['id']: dict(task_family_id=families[c['id']], split_group_id='split:' + digest(root(c['id']))[:24],
                         lineage_version=VERSION, lineage=reasons[c['id']]) for c in cases}
