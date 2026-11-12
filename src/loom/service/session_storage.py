"""Field-granular session persistence and the legacy snapshot compatibility view."""

from __future__ import annotations

import json
from collections.abc import MutableMapping

from loom.service.contracts import ServiceError, canonical

ADMISSION = "deleted_at IS NULL AND (task_state='queued' OR blocked_count>0)"
PROCESS_PREDICATE = """(json_extract(body,'$.type') IN ('run.started','run.failed','run.stopped','run.recovery.required')
 OR (json_extract(body,'$.type')='run.state.changed' AND json_extract(body,'$.payload.state')!='running')
 OR (json_extract(body,'$.type')='message.created' AND json_extract(body,'$.payload.role')='assistant'))"""
ACTIVITY_PREDICATE = "json_extract(body,'$.type') NOT LIKE 'llm.%.delta'"


def materialize(value):
    if isinstance(value, MutableMapping):
        return {k: materialize(value[k]) for k in value}
    if isinstance(value, list):
        return [materialize(v) for v in value]
    return value


class Fields(MutableMapping):
    """A transaction-scoped mapping. Unaccessed JSON never crosses into Python."""

    def __init__(self, db, sid, scope):
        self.db, self.sid, self.scope = db, sid, scope
        self.names = dict.fromkeys(r[0] for r in db.execute(
            'SELECT name FROM session_fields WHERE session_id=? AND scope=?', (sid, scope)))
        self.values_cache, self.original, self.removed = {}, {}, set()

    def __getitem__(self, key):
        if key not in self.names:
            raise KeyError(key)
        if key not in self.values_cache:
            raw = self.db.execute('SELECT value FROM session_fields WHERE session_id=? AND scope=? AND name=?',
                                  (self.sid, self.scope, key)).fetchone()[0]
            self.original[key] = raw
            self.values_cache[key] = json.loads(raw)
        return self.values_cache[key]

    def __setitem__(self, key, value):
        self.names[key] = None
        self.removed.discard(key)
        self.values_cache[key] = value

    def __delitem__(self, key):
        if key not in self.names:
            raise KeyError(key)
        del self.names[key]
        self.values_cache.pop(key, None)
        self.removed.add(key)

    def __iter__(self):
        return iter(self.names)

    def __len__(self):
        return len(self.names)

    def copy(self):
        return materialize(self)

    def flush(self, skip=()):
        changed = set(self.removed)
        for name in self.removed:
            self.db.execute('DELETE FROM session_fields WHERE session_id=? AND scope=? AND name=?', (self.sid, self.scope, name))
        for name, value in self.values_cache.items():
            if name in skip:
                continue
            raw = canonical(materialize(value))
            if self.original.get(name) != raw:
                put_field(self.db, self.sid, self.scope, name, raw)
                self.original[name] = raw
                changed.add(name)
        self.removed.clear()
        return changed


class Session(Fields):
    def __getitem__(self, key):
        if key in {'task', 'run'} and key not in self.values_cache:
            marker = super().__getitem__(key)
            if marker is not None:
                self.values_cache[key] = Fields(self.db, self.sid, key)
        elif key == 'messages' and key not in self.values_cache:
            self.values_cache[key] = [json.loads(r[0]) for r in self.db.execute(
                'SELECT body FROM session_messages WHERE session_id=? ORDER BY position', (self.sid,))]
        return super().__getitem__(key)


def put_field(db, sid, scope, name, raw):
    db.execute('INSERT INTO session_fields VALUES(?,?,?,?) ON CONFLICT(session_id,scope,name) '
               'DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value', (sid, scope, name, raw))


def lifecycle_changed(state):
    """Agent result projection depends on lifecycle facts, not token streams/time."""
    if not isinstance(state, Session):
        return True
    fields = [(state, ('autonomy', 'control'))]
    for scope, names in (('task', ('state',)), ('run', ('id', 'state', 'stop_code', 'attempt_id'))):
        if scope not in state.values_cache:
            continue
        value = state.values_cache[scope]
        if not isinstance(value, Fields):
            return True
        fields.append((value, names))
    for section, names in fields:
        for name in names:
            if name in section.removed:
                return True
            if name in section.values_cache and canonical(section.values_cache[name]) != section.original.get(name):
                return True
    return False


def head(db, sid, *, deleted=False):
    row = db.execute('SELECT * FROM session_heads WHERE id=?', (sid,)).fetchone()
    if row is None or (row['deleted_at'] and not deleted):
        raise ServiceError('Session not found', 404)
    return row


def section(db, sid, scope):
    return {name: json.loads(raw) for name, raw in db.execute(
        'SELECT name,value FROM session_fields WHERE session_id=? AND scope=?', (sid, scope))}


def load(db, sid, *, lazy=False, deleted=False, messages_limit=None, context=True):
    head(db, sid, deleted=deleted)
    if lazy:
        return Session(db, sid, 'root')
    sections = {'root': {}, 'task': {}, 'run': {}}
    clause = " AND NOT(scope='root' AND name='context')" if not context else ''
    for scope, name, raw in db.execute('SELECT scope,name,value FROM session_fields WHERE session_id=?' + clause, (sid,)):
        sections[scope][name] = json.loads(raw)
    result = sections['root']
    for scope in ('task', 'run'):
        if scope in result and result[scope] is not None:
            result[scope] = sections[scope]
    if 'messages' in result:
        if messages_limit is None:
            rows = db.execute('SELECT body FROM session_messages WHERE session_id=? ORDER BY position', (sid,))
            result['messages'] = [json.loads(r[0]) for r in rows]
        else:
            rows = db.execute('SELECT body FROM session_messages WHERE session_id=? ORDER BY position DESC LIMIT ?', (sid, messages_limit))
            result['messages'] = list(reversed([json.loads(r[0]) for r in rows]))
    return result


def _save_messages(db, sid, messages):
    # Only callers that actually access messages pay this comparison cost.
    for position, message in enumerate(messages):
        raw = canonical(message)
        db.execute('INSERT INTO session_messages VALUES(?,?,?,?,?) ON CONFLICT(session_id,position) '
                   'DO UPDATE SET seq=excluded.seq,command_id=excluded.command_id,body=excluded.body '
                   'WHERE body IS NOT excluded.body', (sid, position, message.get('seq'), message.get('command_id'), raw))
    db.execute('DELETE FROM session_messages WHERE session_id=? AND position>=?', (sid, len(messages)))


def save(db, state, *, ordinal=None):
    sid = state['session_id']
    changed = {'root': set(), 'task': set(), 'run': set()}
    if ordinal is None:
        if not isinstance(state, Session):
            db.execute('INSERT INTO session_heads(id) SELECT ? WHERE NOT EXISTS(SELECT 1 FROM session_heads WHERE id=?)', (sid, sid))
    else:
        db.execute('INSERT INTO session_heads(ordinal,id) VALUES(?,?)', (ordinal, sid))
    if isinstance(state, Session):
        changed['root'] = state.flush(skip={'task', 'run', 'messages'})
        accessed = state.values_cache
    else:
        accessed = state
        names = set(state)
        changed['root'] = names.copy()
        for (name,) in db.execute("SELECT name FROM session_fields WHERE session_id=? AND scope='root'", (sid,)).fetchall():
            if name not in names:
                changed['root'].add(name)
                db.execute("DELETE FROM session_fields WHERE session_id=? AND scope='root' AND name=?", (sid, name))
        for name, value in state.items():
            if name not in {'task', 'run', 'messages'}:
                put_field(db, sid, 'root', name, canonical(value))
    for scope in ('task', 'run'):
        if scope not in accessed:
            continue
        value = accessed[scope]
        if isinstance(value, Fields):
            changed[scope] = value.flush()
        else:
            put_field(db, sid, 'root', scope, 'null' if value is None else '{}')
            changed['root'].add(scope)
            existing = {r[0] for r in db.execute('SELECT name FROM session_fields WHERE session_id=? AND scope=?', (sid, scope))}
            changed[scope] = existing | set(value or {})
            for name in existing - set(value or {}):
                db.execute('DELETE FROM session_fields WHERE session_id=? AND scope=? AND name=?', (sid, scope, name))
            for name, item in (value or {}).items():
                put_field(db, sid, scope, name, canonical(item))
    if 'messages' in accessed:
        put_field(db, sid, 'root', 'messages', '[]')
        _save_messages(db, sid, accessed['messages'])
    sync_head(db, sid, changed)


def sync_head(db, sid, changed):
    # All paths, including compatibility SQL writes, derive metadata identically.
    columns = {column for column, (scope, name) in HEAD_INPUTS.items() if name in changed[scope]}
    if 'run' in changed['root']:
        columns.add('run_state')
    if not columns:
        return
    values = {k: v for k, v in HEAD_VALUES.items() if k in columns}
    assignments = ','.join(f'{k}={v}' for k, v in values.items())
    guard = ' OR '.join(f'{k} IS NOT {v}' for k, v in values.items())
    db.execute(f'UPDATE session_heads SET {assignments} WHERE id=? AND ({guard})', (sid,))


def field(scope, name):
    return f"(SELECT json(value) FROM session_fields f WHERE f.session_id=session_heads.id AND scope='{scope}' AND name='{name}')"


def scalar(scope, name):
    return f"json_extract({field(scope, name)},'$')"


HEAD_VALUES = {
    'title': scalar('root', 'title'), 'created_at': scalar('root', 'created_at'),
    'deleted_at': scalar('root', 'deleted_at'), 'event_cursor': f"COALESCE({scalar('root', 'event_cursor')},0)",
    'task_state': scalar('task', 'state'), 'run_state': scalar('run', 'state'),
    'workspace': scalar('task', 'workspace'), 'resource_claims': field('task', 'resource_claims'),
    'workspace_blocked': field('root', 'workspace_blocked'),
    'blocked_count': f"COALESCE(json_array_length({field('root', 'workspace_blocked')}),0)",
    'agent_id': f"json_extract({field('root', 'autonomy')},'$.agent_id')",
    'queued_since': scalar('root', 'queued_since'),
}
HEAD_UPDATE = 'UPDATE session_heads SET ' + ','.join(f'{k}={v}' for k, v in HEAD_VALUES.items())
HEAD_INPUTS = {
    **{name: ('root', name) for name in ('title', 'created_at', 'deleted_at', 'event_cursor', 'workspace_blocked', 'queued_since')},
    'task_state': ('task', 'state'), 'run_state': ('run', 'state'), 'workspace': ('task', 'workspace'),
    'resource_claims': ('task', 'resource_claims'), 'agent_id': ('root', 'autonomy'), 'blocked_count': ('root', 'workspace_blocked'),
}


def _object(scope):
    return f"COALESCE((SELECT json_group_object(name,json(value)) FROM session_fields WHERE session_id=h.id AND scope='{scope}'),'{{}}')"


def create_schema(db):
    statements = [
        'CREATE TABLE IF NOT EXISTS session_storage_versions(version INTEGER PRIMARY KEY)',
        '''CREATE TABLE IF NOT EXISTS session_heads(
            ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
            title TEXT, created_at TEXT, deleted_at TEXT, event_cursor INTEGER NOT NULL DEFAULT 0,
            last_activity_at TEXT NOT NULL DEFAULT '', task_state TEXT, run_state TEXT,
            workspace TEXT, resource_claims TEXT, workspace_blocked TEXT, blocked_count INTEGER NOT NULL DEFAULT 0,
            agent_id TEXT, queued_since TEXT)''',
        '''CREATE TABLE IF NOT EXISTS session_fields(session_id TEXT NOT NULL,scope TEXT NOT NULL,name TEXT NOT NULL,
            value TEXT NOT NULL,PRIMARY KEY(session_id,scope,name)) WITHOUT ROWID''',
        '''CREATE TABLE IF NOT EXISTS session_messages(session_id TEXT NOT NULL,position INTEGER NOT NULL,seq INTEGER,
            command_id TEXT,body TEXT NOT NULL,PRIMARY KEY(session_id,position)) WITHOUT ROWID''',
        'CREATE INDEX IF NOT EXISTS session_messages_seq ON session_messages(session_id,seq)',
        'CREATE INDEX IF NOT EXISTS session_messages_command ON session_messages(session_id,command_id)',
        'CREATE INDEX IF NOT EXISTS sessions_activity ON session_heads(last_activity_at DESC,ordinal DESC) WHERE deleted_at IS NULL',
        f'CREATE INDEX IF NOT EXISTS sessions_admission ON session_heads(ordinal DESC) WHERE {ADMISSION}',
        "CREATE INDEX IF NOT EXISTS sessions_running ON session_heads(id) WHERE deleted_at IS NULL AND run_state='running'",
        'CREATE INDEX IF NOT EXISTS operations_session ON operations(session_id)',
        'CREATE INDEX IF NOT EXISTS commands_session ON commands(session_id,state)',
        'CREATE INDEX IF NOT EXISTS artifacts_digest ON artifacts(digest)',
        "CREATE INDEX IF NOT EXISTS events_run ON events(session_id,json_extract(body,'$.run_id'),seq)",
        "CREATE INDEX IF NOT EXISTS events_type ON events(session_id,json_extract(body,'$.type'),seq)",
        f'CREATE INDEX IF NOT EXISTS events_activity ON events(session_id,seq) WHERE {ACTIVITY_PREDICATE}',
        f'CREATE INDEX IF NOT EXISTS events_processes ON events(session_id,seq) WHERE {PROCESS_PREDICATE}',
    ]
    for statement in statements:
        db.execute(statement)


def compatibility_view(db):
    root, task, run = (_object(s) for s in ('root', 'task', 'run'))
    db.execute(f'''CREATE VIEW sessions AS SELECT h.id,
        json_set({root}, '$.task',json({task}), '$.run',
            CASE WHEN (SELECT value FROM session_fields WHERE session_id=h.id AND scope='root' AND name='run')='null'
            THEN NULL ELSE json({run}) END,
            '$.messages',json(COALESCE((SELECT json_group_array(json(body)) FROM
                (SELECT body FROM session_messages WHERE session_id=h.id ORDER BY position)),'[]'))) AS body
        FROM session_heads h''')
    # Preserve old maintenance SQL semantics. Live writes never use these triggers.
    split = '''
        DELETE FROM session_fields WHERE session_id=NEW.id;
        INSERT INTO session_fields SELECT NEW.id,'root',key,
            CASE WHEN key IN ('task','run') AND type='object' THEN '{}'
                 WHEN key='messages' THEN '[]' WHEN type='true' THEN 'true' WHEN type='false' THEN 'false'
                 ELSE json_quote(value) END FROM json_each(NEW.body);
        INSERT INTO session_fields SELECT NEW.id,'task',key,
            CASE type WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' ELSE json_quote(value) END
            FROM json_each(NEW.body,'$.task') WHERE key IS NOT NULL;
        INSERT INTO session_fields SELECT NEW.id,'run',key,
            CASE type WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' ELSE json_quote(value) END
            FROM json_each(NEW.body,'$.run') WHERE key IS NOT NULL;
        DELETE FROM session_messages WHERE session_id=NEW.id;
        INSERT INTO session_messages SELECT NEW.id,CAST(key AS INTEGER),json_extract(value,'$.seq'),
            json_extract(value,'$.command_id'),value FROM json_each(NEW.body,'$.messages');
    '''
    for action in ('INSERT', 'UPDATE'):
        db.execute(f'''CREATE TRIGGER sessions_{action.lower()} INSTEAD OF {action} ON sessions BEGIN
            INSERT INTO session_heads(id) SELECT NEW.id WHERE NOT EXISTS(SELECT 1 FROM session_heads WHERE id=NEW.id);
            {split}
            {HEAD_UPDATE} WHERE id=NEW.id;
            UPDATE session_heads SET last_activity_at=COALESCE(
                (SELECT json_extract(body,'$.at') FROM events WHERE session_id=NEW.id ORDER BY seq DESC LIMIT 1),created_at,'') WHERE id=NEW.id;
        END''')
    db.execute('''CREATE TRIGGER session_activity AFTER INSERT ON events BEGIN
        UPDATE session_heads SET last_activity_at=COALESCE(json_extract(NEW.body,'$.at'),last_activity_at)
        WHERE id=NEW.session_id AND NEW.seq=(SELECT MAX(seq) FROM events WHERE session_id=NEW.session_id);
    END''')


def migrate(db):
    versioned = db.execute("SELECT 1 FROM sqlite_master WHERE name='session_storage_versions'").fetchone()
    if versioned and db.execute('SELECT 1 FROM session_storage_versions WHERE version=2').fetchone():
        return
    legacy = db.execute("SELECT 1 FROM sqlite_master WHERE name='sessions' AND type='table'").fetchone()
    db.execute('DROP INDEX IF EXISTS sessions_admission')
    create_schema(db)
    if legacy:
        for row in db.execute('SELECT rowid,id,body FROM sessions ORDER BY rowid'):
            state = json.loads(row[2])
            save(db, state, ordinal=row[0])
            restored = load(db, row[1], deleted=True)
            if restored != state:
                raise ValueError(f'Session migration changed snapshot: {row[1]}')
        db.execute('DROP TABLE sessions')
    db.execute('''UPDATE session_heads SET last_activity_at=COALESCE(
        (SELECT json_extract(body,'$.at') FROM events WHERE session_id=session_heads.id ORDER BY seq DESC LIMIT 1),created_at,'')''')
    compatibility_view(db)
    db.execute('INSERT INTO session_storage_versions VALUES(2)')
