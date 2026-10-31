"""Additive, transaction-safe schema; records keep immutable revisions separately."""

TABLES = {
    "agents",
    "agent_roles",
    "agent_goals",
    "agent_runs",
    "agent_work_packages",
    "agent_learning_cycles",
    "agent_guidance",
    "agent_contexts",
    "agent_routes",
}


def migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS autonomy_migrations(version INTEGER PRIMARY KEY)")
    for table in sorted(TABLES):
        db.execute(f"CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, body TEXT NOT NULL)")
        db.execute(f"CREATE INDEX IF NOT EXISTS {table}_agent ON {table}(agent_id)")
    statements = [
        "CREATE TABLE IF NOT EXISTS agent_commands(id TEXT PRIMARY KEY, input TEXT NOT NULL, receipt TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS agent_revisions(kind TEXT, id TEXT, revision INTEGER, body TEXT NOT NULL, PRIMARY KEY(kind,id,revision))",
        "CREATE TABLE IF NOT EXISTS agent_events(agent_id TEXT, seq INTEGER, body TEXT NOT NULL, PRIMARY KEY(agent_id,seq))",
        "CREATE TABLE IF NOT EXISTS agent_artifacts(agent_id TEXT, digest TEXT, ref TEXT NOT NULL, PRIMARY KEY(agent_id,digest))",
        "CREATE TABLE IF NOT EXISTS agent_sessions(session_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, "
        "work_package_id TEXT, taken_over INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS agent_run_links(run_id TEXT PRIMARY KEY, session_id TEXT, agent_id TEXT, work_package_id TEXT)",
        "CREATE TABLE IF NOT EXISTS agent_outbox(id TEXT PRIMARY KEY, agent_id TEXT, kind TEXT, state TEXT, body TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS agent_budget_calls(id TEXT PRIMARY KEY, agent_id TEXT, goal_id TEXT, work_package_id TEXT, "
        "window TEXT, category TEXT, reserved INTEGER, actual INTEGER, state TEXT, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS memory_annotations(id TEXT PRIMARY KEY, agent_id TEXT, evaluation_id TEXT, "
        "record_id TEXT, digest TEXT, revision INTEGER, body TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS memory_suppressions(agent_id TEXT, lineage TEXT, body TEXT NOT NULL, PRIMARY KEY(agent_id,lineage))",
        "CREATE TABLE IF NOT EXISTS agent_memory_uses(id TEXT PRIMARY KEY, agent_id TEXT, run_id TEXT, body TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS memory_versions(id TEXT PRIMARY KEY, value INTEGER NOT NULL)",
    ]
    for statement in statements:
        db.execute(statement)
    for table in ("memory_annotations", "memory_suppressions"):
        db.execute(f"CREATE INDEX IF NOT EXISTS {table}_source ON {table}(json_extract(body,'$.source.source_digest'))")
    db.execute("CREATE INDEX IF NOT EXISTS agent_budget_window ON agent_budget_calls(agent_id,window)")
    db.execute("CREATE INDEX IF NOT EXISTS agent_outbox_pending ON agent_outbox(agent_id,kind,state)")
    db.execute("INSERT OR IGNORE INTO autonomy_migrations VALUES(1)")
    db.execute("INSERT OR IGNORE INTO memory_versions VALUES('effective',0)")
