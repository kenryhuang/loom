"""One disposition policy for structured rule results, independent of prose."""

POLICY_VERSION = 'evaluation-policy.2'
POLICIES = {
    'format': dict(scope='request', severity='warning', disposition='quarantine', repair_strategy='format'),
    'semantic': dict(scope='candidate', severity='warning', disposition='quarantine', repair_strategy='candidate'),
    'coverage': dict(scope='obligation', severity='warning', disposition='abstain', repair_strategy='candidate'),
    'provider': dict(scope='request', severity='error', disposition='pause', repair_strategy='provider'),
    'budget': dict(scope='job', severity='warning', disposition='pause', repair_strategy='capacity'),
    'state': dict(scope='job', severity='error', disposition='stop', repair_strategy='migration'),
    'internal': dict(scope='job', severity='error', disposition='stop', repair_strategy='host'),
    'cancelled': dict(scope='job', severity='warning', disposition='pause', repair_strategy='resume'),
}


def policy(error):
    value = dict(POLICIES.get(error.get('category', 'semantic'), POLICIES['internal']))
    if error.get('record_index') is not None and value['scope'] == 'request':
        value['scope'] = 'candidate'
    if error['code'] == 'memory.credentials':
        value.update(repair_strategy='none', disposition='quarantine')
    return {**error, **value, 'policy_version': POLICY_VERSION}


def fatal(issues):
    return [e for e in issues if policy(e)['disposition'] in {'stop', 'pause'}]
