"""Scope-first retrieval, frozen experimental groups and balanced representatives."""

from collections import defaultdict

from loom.memory.store import tokens
from loom.skills.contracts import digest


def freeze_split(cases, *, exposed_groups=()):
    groups = defaultdict(list)
    for case in cases:
        groups[case['split_group_id']].append(case)
    result = {}
    eligible = []
    for group, values in sorted(groups.items()):
        if (group in exposed_groups or any(c['quality']['boundary_uncertain'] or c['exposure']['skill_versions_seen']
                                          or c['exposure']['prior_memory_refs'] for c in values)):
            result[group] = 'discovery'
        else:
            eligible.append(group)
    # Deterministic partition frozen before any model gets content; small corpora await future tests.
    eligible.sort(key=lambda g: digest(['skill-split.1', g]))
    reliable_discovery = sum(any(not c['quality']['boundary_uncertain'] and not c['exposure']['skill_versions_seen']
                                 and not c['exposure']['prior_memory_refs'] for c in groups[g]) for g in result)
    capacity = max(0, (len(eligible) - max(0, 3 - reliable_discovery)) // 2)
    reserved = min(capacity, max(2, len(eligible) // 5)) if capacity >= 2 else 0
    for i, group in enumerate(eligible):
        result[group] = 'holdout' if i < reserved else 'validation' if i < reserved * 2 else 'discovery'
    return result


def recall(cases, query, *, split, limit=100, exclude=()):
    terms = tokens(query)
    ranked = []
    excluded = set(exclude)
    for case in cases:
        if split[case['split_group_id']] != 'discovery' or case['id'] in excluded:
            continue
        text = ' '.join([case['task_contract'].get('objective') or '', str(case.get('problem', {})), str(case.get('record') or {})])
        overlap = terms & tokens(text)
        if not overlap:
            continue
        score = len(overlap) + 2 * len(terms & tokens(case['task_contract'].get('objective') or ''))
        ranked.append((score, case))
    ranked.sort(key=lambda r: (-r[0], r[1]['id']))
    return [r[1] for r in ranked[:limit]], dict(query=query, matched=len(ranked), limit=limit,
                                              truncated=len(ranked) > limit, reason='contract/problem keyword overlap; semantic comparability pending')


def representatives(cases, *, limit=24):
    buckets = defaultdict(list)
    for case in cases:
        if not case['evidence']:
            role = 'unknown'
        elif (case.get('record') or {}).get('kind') == 'lessons' or case['quality'].get('interpretation_conflict'):
            role = 'failure'
        else:
            role = 'observation'
        buckets[role].append(case)
    selected, families = [], set()

    def take(role, count):
        for case in buckets[role]:
            if case['task_family_id'] not in families and count and len(selected) < limit:
                selected.append(case)
                families.add(case['task_family_id'])
                count -= 1

    for role, count in [('failure', 6), ('unknown', 6), ('observation', 12)]:
        take(role, count)
    for role in ('failure', 'unknown', 'observation'):
        take(role, limit - len(selected))
    return selected


def delivery(cases, *, max_chars=48000):
    manifest, delivered, used, omitted = {}, [], 0, []
    for case in cases:
        row = {k: case[k] for k in ('id', 'task_family_id', 'task_contract', 'problem', 'context', 'outcome', 'quality', 'exposure')}
        row['record'] = {k: v for k, v in (case['record'] or {}).items() if k in {
            'title', 'summary', 'scope', 'trigger', 'steps', 'validation', 'basis', 'preserve', 'avoid', 'failure_reason', 'better_approach'}}
        row['evidence'] = []
        # Original quotes, not just evaluator summaries; partial delivery stays explicit.
        for index, entry in enumerate(case['evidence']):
            if used + len(entry['text']) > max_chars:
                omitted.append(case['id'])
                continue
            handle = f"C{len(delivered) + 1}.E{index + 1}"
            manifest[handle] = dict(case_id=case['id'], case_revision=case['revision'], family_id=case['task_family_id'],
                                    source={**case['provenance'], 'session_id': case['session_id']},
                                    ref=entry['ref'], text=entry['text'], basis=entry.get('basis', 'unknown'))
            row['evidence'].append(dict(handle=handle, text=entry['text'], basis=entry.get('basis', 'unknown')))
            used += len(entry['text'])
        delivered.append(row)
    return dict(cases=delivered, manifest=manifest, evidence_chars=used, omitted_case_ids=sorted(set(omitted)))
