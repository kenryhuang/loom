"""Versioned wire contracts. The host, never the model, owns provenance and status."""

import hashlib
import json

from jsonschema import Draft202012Validator

VERSION = 'skill-induction.1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def obj(**fields):
    return dict(type='object', properties=fields, required=list(fields), additionalProperties=False)


def array(item, maximum=24, minimum=0):
    return dict(type='array', items=item, maxItems=maximum, minItems=minimum)


TEXT = dict(type='string', minLength=1, maxLength=1600)
ID = dict(type='string', minLength=1, maxLength=200)
IDS = array(ID, 32)
GUARD = obj(field=dict(enum=['workspace_access', 'platform', 'tool', 'runtime']),
            value=ID)
CLAIM = obj(id=ID, text=TEXT, kind=dict(enum=['trigger', 'precondition', 'step', 'branch', 'verification']),
            basis=dict(enum=['observed_pattern', 'proposed_composition']), supports=IDS, contradicts=IDS, unknown=IDS,
            dependencies=IDS, exceptions=array(TEXT, 8), verification_requirements=array(TEXT, 8))
CANDIDATE = obj(title=TEXT, summary=TEXT, trigger=TEXT, preconditions=array(TEXT, 12),
                guards=array(GUARD, 12, 1), required_tools=IDS,
                parameters=array(obj(name=ID, type=dict(enum=['string', 'integer', 'boolean', 'path']), binding=TEXT), 12),
                steps=array(obj(id=ID, instruction=TEXT, claim_ids=IDS, depends_on=IDS), 12, 1),
                branches=array(obj(condition=TEXT, observable_before_action=dict(type='boolean'), claim_ids=IDS), 8),
                verification=array(TEXT, 12, 1), avoid=array(TEXT, 12), claims=array(CLAIM, 40, 1),
                gaps=array(TEXT, 20))
ALIGNMENT = obj(selected_cases=array(ID, 24),
                mappings=array(obj(role=TEXT, evidence=IDS, differences=TEXT, causal_basis=TEXT), 40),
                condition_variations=array(obj(condition=TEXT, evidence=IDS), 12),
                excluded=array(obj(case_id=ID, reason=TEXT), 100), gaps=array(TEXT, 20))
REVIEW = obj(approved=dict(type='boolean'),
             issues=array(obj(claim_id=ID, severity=dict(enum=['critical', 'gap']), reason=TEXT, evidence=IDS), 40),
             counterexample_queries=array(TEXT, 4, 1))
SELECTION = obj(ranking=array(obj(case_id=ID, comparability=dict(enum=['comparable', 'boundary', 'unknown']), reason=TEXT), 100, 1))


def validate(value, schema):
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        raise ValueError('; '.join(f'{list(e.path)}: {e.message}' for e in errors[:8]))
    return value
