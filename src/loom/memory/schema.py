"""Memory V2 authority. Additive DDL; never repurpose legacy memory_versions."""

SCHEMA = '''
CREATE TABLE IF NOT EXISTS memory_schema(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS memory_namespaces(
 id TEXT PRIMARY KEY, scope_kind TEXT NOT NULL, scope_key TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'shadow', checkpoint TEXT,
 UNIQUE(scope_kind,scope_key));
CREATE TABLE IF NOT EXISTS memory_sources(
 id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL, session_id TEXT NOT NULL,
 evaluation_id TEXT NOT NULL, record_id TEXT NOT NULL, revision TEXT NOT NULL,
 root_id TEXT NOT NULL, available INTEGER NOT NULL, body TEXT NOT NULL,
 UNIQUE(namespace_id,evaluation_id,record_id,revision));
CREATE INDEX IF NOT EXISTS memory_sources_reference ON memory_sources(evaluation_id,record_id,available);
CREATE INDEX IF NOT EXISTS memory_sources_session ON memory_sources(session_id,available);
CREATE TABLE IF NOT EXISTS memory_observations(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, evidence_group_id TEXT NOT NULL,
 observed_at TEXT, anchor TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memory_observations_source ON memory_observations(source_id);
CREATE TABLE IF NOT EXISTS memory_entities(id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_entity_aliases(
 namespace_id TEXT NOT NULL, alias TEXT NOT NULL, entity_id TEXT NOT NULL, observation_id TEXT NOT NULL,
 PRIMARY KEY(namespace_id,alias,entity_id));
CREATE TABLE IF NOT EXISTS memory_items(
 id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL, kind TEXT NOT NULL, claim_slot TEXT NOT NULL,
 latest_revision INTEGER NOT NULL, canonical_redirect TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memory_items_visibility ON memory_items(id,canonical_redirect,latest_revision);
CREATE INDEX IF NOT EXISTS memory_items_slot ON memory_items(namespace_id,claim_slot,canonical_redirect);
CREATE TABLE IF NOT EXISTS memory_item_versions(
 id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
 content_digest TEXT NOT NULL, signature TEXT NOT NULL, record TEXT NOT NULL,
 conditions TEXT NOT NULL, epistemic_status TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(memory_id,revision));
CREATE INDEX IF NOT EXISTS memory_item_versions_signature ON memory_item_versions(signature,memory_id);
CREATE TABLE IF NOT EXISTS memory_validity(
 id INTEGER PRIMARY KEY, version_id TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
 recorded_at TEXT NOT NULL, recorded_until TEXT, lifecycle TEXT NOT NULL,
 freshness TEXT NOT NULL, revalidate_after TEXT, environment_fingerprint TEXT);
CREATE INDEX IF NOT EXISTS memory_validity_eligibility ON memory_validity(version_id,recorded_until,lifecycle,freshness,valid_from,valid_to,revalidate_after);
CREATE INDEX IF NOT EXISTS memory_validity_version ON memory_validity(version_id,recorded_until);
CREATE TABLE IF NOT EXISTS memory_supports(
 version_id TEXT NOT NULL, observation_id TEXT NOT NULL, bundle_id TEXT NOT NULL,
 polarity TEXT NOT NULL DEFAULT 'supports', PRIMARY KEY(version_id,observation_id,bundle_id));
CREATE INDEX IF NOT EXISTS memory_supports_observation ON memory_supports(observation_id,version_id);
CREATE TABLE IF NOT EXISTS memory_relations(
 id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL, kind TEXT NOT NULL,
 from_version TEXT NOT NULL, to_version TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memory_relations_namespace ON memory_relations(namespace_id,kind);
CREATE INDEX IF NOT EXISTS memory_relations_source ON memory_relations(from_version,kind);
CREATE INDEX IF NOT EXISTS memory_relations_target ON memory_relations(to_version,kind);
CREATE TABLE IF NOT EXISTS memory_conflicts(
 id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL, claim_slot TEXT NOT NULL,
 state TEXT NOT NULL, resolution TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_conflict_members(
 conflict_id TEXT NOT NULL, version_id TEXT NOT NULL, PRIMARY KEY(conflict_id,version_id));
CREATE TABLE IF NOT EXISTS memory_reviews(
 id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL, version_id TEXT NOT NULL,
 action TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_tombstones(
 namespace_id TEXT NOT NULL, signature TEXT NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(namespace_id,signature));
CREATE TABLE IF NOT EXISTS memory_events(
 id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL, operation TEXT NOT NULL,
 body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_policies(namespace_id TEXT PRIMARY KEY, auto_merge INTEGER NOT NULL DEFAULT 0,
 calibration TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_jobs(
 id TEXT PRIMARY KEY, namespace_id TEXT NOT NULL, state TEXT NOT NULL,
 input TEXT NOT NULL, result TEXT, error TEXT, lease_until TEXT, budget_ref TEXT);
CREATE TABLE IF NOT EXISTS memory_outbox(
 id INTEGER PRIMARY KEY, namespace_id TEXT NOT NULL, version_id TEXT NOT NULL,
 epoch INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending', body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memory_outbox_pending ON memory_outbox(state,namespace_id,id);
CREATE TABLE IF NOT EXISTS memory_index_state(
 namespace_id TEXT NOT NULL, backend TEXT NOT NULL, fingerprint TEXT NOT NULL,
 generation TEXT, indexed_epoch INTEGER NOT NULL DEFAULT 0, error TEXT, last_outbox INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(namespace_id,backend,fingerprint));
CREATE TABLE IF NOT EXISTS memory_index_entries(
 namespace_id TEXT NOT NULL, backend TEXT NOT NULL, fingerprint TEXT NOT NULL,
 version_id TEXT NOT NULL, content_digest TEXT NOT NULL, generation TEXT NOT NULL,
 PRIMARY KEY(namespace_id,backend,fingerprint,version_id));
CREATE TABLE IF NOT EXISTS memory_projection(
 version_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, namespace_id TEXT NOT NULL,
 kind TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, scope TEXT NOT NULL,
 review_state TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
 source TEXT NOT NULL, support_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS memory_projection_filter ON memory_projection(namespace_id,kind,review_state,support_count,version_id,memory_id,created_at);
CREATE INDEX IF NOT EXISTS memory_projection_recent ON memory_projection(namespace_id,created_at DESC,version_id DESC);
CREATE INDEX IF NOT EXISTS memory_projection_page ON memory_projection(namespace_id,kind,review_state,created_at DESC,version_id);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(version_id UNINDEXED, terms);
CREATE TABLE IF NOT EXISTS memory_migration_issues(
 evaluation_id TEXT NOT NULL, record_id TEXT NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(evaluation_id,record_id));
CREATE TABLE IF NOT EXISTS memory_source_cursors(evaluation_id TEXT PRIMARY KEY, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_skill_dependencies(
 skill_version_id TEXT NOT NULL, memory_version_id TEXT NOT NULL, PRIMARY KEY(skill_version_id,memory_version_id));
INSERT OR IGNORE INTO memory_schema VALUES(1);
'''


def migrate(db):
    # executescript commits an existing transaction; individual statements don't.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    columns = {r[1] for r in db.execute('PRAGMA table_info(memory_projection)')}
    if 'tags' not in columns:
        db.execute("ALTER TABLE memory_projection ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
    columns = {r[1] for r in db.execute('PRAGMA table_info(memory_index_state)')}
    if 'last_outbox' not in columns:
        db.execute('ALTER TABLE memory_index_state ADD COLUMN last_outbox INTEGER NOT NULL DEFAULT 0')


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_schema'").fetchone())
