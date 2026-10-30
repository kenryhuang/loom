"""Readable exports generated exclusively from an immutable structured version."""

from loom.skills.contracts import canonical


def package(version):
    c = version['contract']
    text = [f"# {c['title']}", '', c['summary'], '', f"Version: {version['id']}",
            f"Lifecycle: {version['state']} · evidence: {version['evidence_state']}", '', '## Trigger', '', c['trigger'],
            '', '## Preconditions', '', *['- ' + s for s in c['preconditions']], '', '## Inputs', '']
    text.extend(f"- {p['name']} ({p['type']}): {p['binding']}" for p in c['parameters'])
    text.extend(['', '## Steps', ''])
    text.extend(f"{i}. {s['instruction']}" for i, s in enumerate(c['steps'], 1))
    text.extend(['', '## Branches', '', *['- ' + b['condition'] for b in c['branches']], '', '## Verification', '',
                 *['- ' + s for s in c['verification']], '', '## Avoid', '', *['- ' + s for s in c['avoid']],
                 '', '## Evidence gaps', '', *['- ' + s for s in [*c['gaps'], *version['limitations']]], ''])
    return dict(version_id=version['id'], files={
        'SKILL.md': '\n'.join(text), 'contract.json': canonical(c),
        'evidence.json': canonical(dict(claims=c['claims'], coverage=version['coverage'])),
        'validation.json': canonical(dict(split=version['split'], validation=version.get('validations', []),
                                         evidence_state=version['evidence_state'], limitations=version['limitations'])),
        'examples/README.md': 'Examples are the observed source excerpts in evidence.json. No synthetic examples are presented as execution evidence.\n',
    })
