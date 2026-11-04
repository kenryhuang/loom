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


def related(left, right, *, text_features=None):
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
    if text_features is None:
        a, b = normalize(left['task_contract']['objective']), normalize(right['task_contract']['objective'])
        aa, bb = tokens(a), tokens(b)
    else:
        (a, aa), (b, bb) = text_features
    if a and a == b:
        return 'normalized_objective'
    if min(len(aa), len(bb)) >= 8 and len(aa & bb) / len(aa | bb) >= .72:
        return 'possibly_related'
    return None


def assign(cases, *, index=None):
    """Transitive grouping is deterministic; unknown boundaries never gain independence."""
    return (index or LineageIndex()).update(cases)


def feature_key(case):
    return (VERSION, case['id'], case.get('scope', {}).get('workspace'), case['session_id'], case.get('task_episode_id'),
            case.get('quality', {}).get('boundary_uncertain', True),
            case.get('source_digest'), tuple(sorted(case.get('run_ids', []))), case.get('template_lineage'),
            case.get('origin_task_id'), case.get('project_snapshot_digest'), case['task_contract']['objective'])


class LineageIndex:
    """Recompute edges incident to changed cases; deletions can split groups.

    Persisted feature digests and reason edges warm the index after restart.
    Roots are rebuilt from current edges so old unions never survive deletion.
    """

    def __init__(self):
        self.keys, self.edges, self.text = {}, {}, {}
        self.initialized = False
        self.last_comparisons = 0

    def update(self, cases):
        current = {c['id']: c for c in cases}
        keys = {c['id']: feature_key(c) for c in cases}
        changed = {key for key, value in keys.items() if self.keys.get(key) != value}
        removed = self.keys.keys() - keys.keys()
        if not self.initialized:
            valid = {c['id'] for c in cases if c.get('lineage_version') == VERSION
                     and c.get('lineage_features_digest') == digest(keys[c['id']])}
            for case in cases:
                if case['id'] not in valid:
                    continue
                for edge in case.get('lineage', []):
                    if edge['case_id'] in valid:
                        self.edges[tuple(sorted((case['id'], edge['case_id'])))] = (edge['reason'], False)
            # Snapshot links affect split isolation independently of reason edges.
            buckets = {}
            for case in cases:
                if case['id'] in valid:
                    for field in ('project_snapshot_digest', 'template_lineage'):
                        if case.get(field):
                            buckets.setdefault((field, case[field]), []).append(case['id'])
            for values in buckets.values():
                for i, left in enumerate(values):
                    for right in values[:i]:
                        pair = tuple(sorted((left, right)))
                        self.edges[pair] = (self.edges.get(pair, (None, False))[0], True)
            changed -= valid
            self.initialized = True
        affected = changed | removed
        self.edges = {pair: edge for pair, edge in self.edges.items() if not affected.intersection(pair)}
        objectives = {key[-1] for key in keys.values()}
        self.text = {text: value for text, value in self.text.items() if text in objectives}
        for objective in objectives - self.text.keys():
            normalized = normalize(objective)
            self.text[objective] = (normalized, frozenset(tokens(normalized)))
        self.last_comparisons = 0
        for left in sorted(changed):
            for right in current:
                if left == right or right in changed and left > right:
                    continue
                pair = tuple(sorted((left, right)))
                a, b = current[left], current[right]
                reason = related(a, b, text_features=(self.text[keys[left][-1]], self.text[keys[right][-1]]))
                split = any(a.get(k) and a.get(k) == b.get(k) for k in ('project_snapshot_digest', 'template_lineage'))
                if reason or split:
                    self.edges[pair] = (reason, split)
                self.last_comparisons += 1
        self.keys = keys
        return grouped(cases, self.edges)


def grouped(cases, edges):
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

    order = {c['id']: i for i, c in enumerate(cases)}
    for (left, right), (reason, _) in edges.items():
        if reason:
            union(left, right)
            prior, later = sorted((left, right), key=order.__getitem__)
            reasons[later].append(dict(case_id=prior, reason=reason))
    for values in reasons.values():
        values.sort(key=lambda edge: order[edge['case_id']])
    families = {c['id']: 'family:' + digest(root(c['id']))[:24] for c in cases}
    # Snapshot/template links widen experimental isolation without claiming same task.
    for (left, right), (_, split) in edges.items():
        if split:
            union(left, right)
    return {c['id']: dict(task_family_id=families[c['id']], split_group_id='split:' + digest(root(c['id']))[:24],
                         lineage_version=VERSION, lineage=reasons[c['id']]) for c in cases}
