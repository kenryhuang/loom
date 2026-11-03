"""Section-scoped settings, staged until the next task boundary."""

from copy import deepcopy

from loom.autonomy.contracts import role_contract, strict
from loom.service.contracts import ServiceError

SECTIONS = {
    'identity': {'name', 'role_text', 'methods'},
    'models': {'model', 'evaluation_model'},
    'execution': {'task_spec', 'plan_mode'},
    'scheduling': {'interval_seconds', 'timezone', 'scheduling_policy'},
    'budget': {'budget'},
    'learning': {'learning_policy', 'method_learning'},
    'resources': {'source_routes', 'knowledge_base_ids', 'external_tool_ids'},
}
NESTED = {
    'budget': {'daily_tokens'},
    'scheduling_policy': {'enabled', 'tick_seconds', 'max_recovery_attempts'},
    'learning_policy': {'max_resumes', 'max_attempts', 'max_evaluation_calls'},
}


def preview(agent, sections=None):
    role = deepcopy(agent['role'])
    for changes in (sections if sections is not None else (agent.get('pending_configuration') or {}).get('sections', {})).values():
        for key, value in changes.items():
            role[key] = {**role.get(key, {}), **value} if key in NESTED else deepcopy(value)
    return role


def revise(store, aid, cid, revision, section, changes):
    if section not in SECTIONS:
        raise ServiceError('Unknown configuration section')
    strict(changes, SECTIONS[section])
    if not changes:
        raise ServiceError('Configuration changes cannot be empty')
    for key in set(changes) & set(NESTED):
        strict(changes[key], NESTED[key])
    value = dict(type='configuration', agent_id=aid, revision=revision, section=section, changes=changes)
    with store.sessions.read() as db:
        replay = store.replay(db, cid, value)
        if replay:
            return replay
        agent = store.get(db, 'agents', aid)
    store.expect(agent, revision)
    sections = deepcopy((agent.get('pending_configuration') or {}).get('sections', {}))
    sections[section] = deepcopy(changes)
    role = preview(agent, sections)
    role.pop('schema_version', None)
    role = role_contract(role)
    # Persist only validated section values; preview is a pure read and must not
    # require a workspace directory to remain online just to show the dashboard.
    sections[section] = {key: {nested: role[key][nested] for nested in change} if key in NESTED else deepcopy(role[key])
                         for key, change in changes.items()}
    if section in {'execution', 'resources'}:
        store.validate_role_resources(role)
    with store.sessions.transaction() as db:
        if replay := store.replay(db, cid, value):
            return replay
        current = store.get(db, 'agents', aid)
        store.expect(current, revision)
        current.update(revision=revision + 1, next_schedule_at=store.now(),
                       pending_configuration=dict(sections=sections, saved_at=store.now(), command_id=cid))
        store.put(db, 'agents', current)
        store.event(db, aid, 'configuration.saved', dict(section=section, changes=changes, activation='next_task'))
        return store.receipt(db, cid, value, current)


def busy(store, db, agent):
    aid = agent['id']
    if agent.get('planner_lease') or db.execute(
        "SELECT 1 FROM agent_budget_calls WHERE agent_id=? AND state='reserved' LIMIT 1", (aid,),
    ).fetchone():
        return True
    if db.execute(
        "SELECT 1 FROM agent_work_packages w JOIN agent_sessions l ON l.session_id=json_extract(w.body,'$.session_id') "
        "WHERE w.agent_id=? AND l.agent_id=w.agent_id AND l.taken_over=0 AND json_extract(w.body,'$.state') IN "
        "('dispatched','running','blocked','failed') LIMIT 1", (aid,),
    ).fetchone():
        return True
    if db.execute(
        "SELECT 1 FROM agent_learning_cycles WHERE agent_id=? AND json_extract(body,'$.evaluation_id') IS NOT NULL "
        "AND json_extract(body,'$.state') IN ('pending','running','deferred') LIMIT 1", (aid,),
    ).fetchone():
        return True
    for table, owner in [('semantic_jobs', 'autonomy_budget'), ('skill_jobs', 'agent_binding')]:
        if db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone() and db.execute(
            f"SELECT 1 FROM {table} WHERE json_extract(body,'$.{owner}.agent_id')=? "
            "AND json_extract(body,'$.state') IN ('queued','running','deferred') LIMIT 1", (aid,),
        ).fetchone():
            return True
    return False


def activate(store, db, aid):
    """Returns False when existing work must finish before new settings apply."""
    agent = store.get(db, 'agents', aid)
    pending = agent.get('pending_configuration')
    if not pending:
        return True
    if agent['state'] != 'enabled' or busy(store, db, agent):
        return False
    role = preview(agent)
    from loom.autonomy.stages import role_signature

    previous_role = agent['role']
    previous_signature = role_signature(previous_role)
    previous_revision = agent['role_revision']
    agent.update(role=role, role_revision=previous_revision + 1, revision=agent['revision'] + 1,
                 generation=agent['generation'] + 1, guidance_version=agent['guidance_version'] + 1,
                 next_wake_at=store.now(), next_schedule_at=store.now(), next_learning_at=store.now())
    agent.pop('pending_configuration', None)
    store.put(db, 'agents', agent)
    store.put(db, 'agent_roles', dict(id=f"{aid}:{agent['role_revision']}", agent_id=aid,
                                     revision=agent['role_revision'], role=role))
    # Settings do not revise the delivery or acceptance contract. Pending work
    # uses the new configuration; historical deliveries keep their pinned role.
    for work in store.rows(db, 'agent_work_packages', aid):
        if work['state'] == 'pending' and work['role_revision'] == previous_revision and not work.get('superseded_contract'):
            work['role_revision'] = agent['role_revision']
            store.put(db, 'agent_work_packages', work)
    for stage in store.rows(db, 'agent_stages', aid):
        if stage.get('role_signature') == previous_signature and stage['state'] not in {'completed', 'cancelled', 'superseded'}:
            stage['role_signature'] = role_signature(role)
            stage['revision'] += 1
            store.put(db, 'agent_stages', stage)
    same_sources = all(role[key] == previous_role[key] for key in ('source_routes', 'knowledge_base_ids', 'external_tool_ids'))
    for source in store.rows(db, 'agent_sources', aid):
        if source.get('role_revision') != previous_revision:
            continue
        if same_sources:
            source['role_revision'] = agent['role_revision']
        else:
            source.update(next_refresh_at=store.now(), expires_at=store.now())
        store.put(db, 'agent_sources', source)
    store.event(db, aid, 'configuration.activated', dict(role_revision=agent['role_revision'], sections=list(pending['sections'])))
    return True
