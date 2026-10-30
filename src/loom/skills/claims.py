"""Host materialization of claim support; no synthetic execution or independence."""

from copy import deepcopy

from loom.skills.contracts import CANDIDATE, canonical, validate

METHOD_MAX_CHARS = 6000


def method_size(value):
    """Measure the exact runtime projection, including host composition status."""
    method = projection({**value, 'composition_status': 'proposed_composition'})
    return dict(characters=len(canonical(method)), maximum=METHOD_MAX_CHARS,
                fields={key: len(canonical(part)) for key, part in method.items()})


def projection(contract):
    return {k: v for k, v in contract.items() if k not in {'claims', 'blocking_issues'}}


def evidence_errors(value, manifest):
    """Report evidence defects together so a size error cannot mask them for several retries."""
    errors = []
    for claim in value['claims']:
        if claim['basis'] == 'observed_pattern' and not claim['supports']:
            errors.append(f"{claim['id']}: Observed claims require original delivered evidence")
        for role in ('supports', 'contradicts', 'unknown'):
            invalid = [handle for handle in claim[role] if handle not in manifest]
            if invalid:
                errors.append(f"{claim['id']}.{role}: Undelivered evidence handle: {', '.join(invalid)}")
    return errors


def materialize(value, manifest, cases):
    validate(value, CANDIDATE)
    value = deepcopy(value)
    size = method_size(value)
    errors = evidence_errors(value, manifest)
    if size['characters'] > METHOD_MAX_CHARS:
        errors.insert(0, f"Executable method projection is {size['characters']} characters; maximum is {METHOD_MAX_CHARS}. "
                      'Shorten repeated explanations without removing constraints, checks or unresolved gaps.')
    if errors:
        raise ValueError('; '.join(errors))
    claims = {c['id']: c for c in value['claims']}
    if len(claims) != len(value['claims']):
        raise ValueError('Claim IDs must be unique')
    by_case = {c['id']: c for c in cases}
    for claim in value['claims']:
        if set(claim['supports']) & set(claim['contradicts']):
            raise ValueError('One evidence handle cannot both support and contradict a claim')
        if claim['basis'] == 'observed_pattern' and not claim['supports']:
            raise ValueError('Observed claims require original delivered evidence')
        refs = []
        for role in ('supports', 'contradicts', 'unknown'):
            for handle in claim[role]:
                if handle not in manifest:
                    raise ValueError(f'Undelivered evidence handle: {handle}')
                refs.append(dict(role=role, handle=handle, **manifest[handle]))
        if set(claim['dependencies']) - claims.keys() or claim['id'] in claim['dependencies']:
            raise ValueError('Unknown or self-referential claim dependency')
        claim['evidence_refs'] = refs
        claim['supporting_family_ids'] = sorted({r['family_id'] for r in refs if r['role'] == 'supports'
            and not by_case[r['case_id']]['quality']['boundary_uncertain']
            and not by_case[r['case_id']]['quality'].get('interpretation_conflict')
            and not by_case[r['case_id']]['exposure']['skill_versions_seen']
            and not by_case[r['case_id']]['exposure']['prior_memory_refs']
            and r['basis'] == 'observed_output'})
        claim['contradicting_family_ids'] = sorted({r['family_id'] for r in refs if r['role'] == 'contradicts'})
        # Even corroborated component steps do not make the composed route execution-tested.
        claim['evidence_state'] = 'conflicted' if claim['contradicts'] else 'locally_observed' if claim['supporting_family_ids'] else 'proposed'
    visited, visiting = set(), set()

    def visit(key):
        if key in visiting:
            raise ValueError('Cyclic claim dependencies')
        if key not in visited:
            visiting.add(key)
            for dep in claims[key]['dependencies']:
                visit(dep)
            visiting.remove(key)
            visited.add(key)
    for key in claims:
        visit(key)
    steps = set()
    for step in value['steps']:
        if step['id'] in steps or set(step['depends_on']) - steps:
            raise ValueError('Steps must have unique IDs and depend only on earlier steps')
        steps.add(step['id'])
        if not step['claim_ids'] or set(step['claim_ids']) - claims.keys():
            raise ValueError('Every step must reference declared claims')
    for branch in value['branches']:
        if not branch['observable_before_action'] or not branch['claim_ids'] or set(branch['claim_ids']) - claims.keys():
            raise ValueError('Branches require observable pre-action conditions and claim references')
    value['composition_status'] = 'proposed_composition'
    value['blocking_issues'] = [c['id'] for c in value['claims'] if c['contradicts']]
    return value


def wire_candidate(value):
    """Strip host-only fields when requesting a bounded revision."""
    return {k: ([{f: c[f] for f in CANDIDATE['properties']['claims']['items']['properties']} for c in value[k]]
                if k == 'claims' else value[k]) for k in CANDIDATE['properties']}
