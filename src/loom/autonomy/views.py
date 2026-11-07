"""Page-specific reads; inactive tabs do not load their historical collections."""

VIEW_FIELDS = {
    'overview': {'goals', 'work_packages', 'learning'},
    'goals': {'goals', 'work_packages', 'stages', 'roadmaps', 'objective_planning', 'stage_reviews'},
    'work': {'goals', 'work_packages', 'learning', 'stages', 'objective_planning'},
    'profile': {'guidance'},
    'knowledge': set(),
    'learning': {'guidance', 'learning', 'work_packages'},
}



def add_work_metrics(db, aid, works):
    """Use the same current Session run counters as the Session details."""
    import json

    from loom.runtime.checkpoints import decode, encode

    sessions = {row[0]: json.loads(row[1]) if row[1] else None for row in db.execute(
        "SELECT s.id,json_extract(s.body,'$.run') FROM sessions s "
        "JOIN agent_sessions a ON a.session_id=s.id WHERE a.agent_id=?", (aid,))}
    for work in works:
        run = sessions.get(work.get('session_id'))
        if not run:
            continue
        counters = decode(run.get('counters', encode({})))
        work['execution_seconds'] = run.get('active_seconds')
        work['actual_tokens'] = getattr(counters.get('usage'), 'total_tokens', 0)
