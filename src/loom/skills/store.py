"""Transactional catalog, immutable versions, job checkpoints and reverse dependencies."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from loom.core import now_iso
from loom.skills.contracts import canonical, digest

SCHEMA = '''
CREATE TABLE IF NOT EXISTS skill_cases(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, session_id TEXT NOT NULL,
    revision TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS skill_cases_scope ON skill_cases(workspace,id);
CREATE TABLE IF NOT EXISTS skill_case_revisions(id TEXT, revision TEXT, body TEXT NOT NULL, PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS skill_source_cursors(session_id TEXT PRIMARY KEY, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skill_dirty_sessions(session_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS skill_jobs(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skill_versions(id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, workspace TEXT NOT NULL,
    state TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS skill_versions_scope ON skill_versions(workspace,state);
CREATE TABLE IF NOT EXISTS skill_dependencies(version_id TEXT, case_id TEXT, case_revision TEXT,
    PRIMARY KEY(version_id,case_id));
CREATE TABLE IF NOT EXISTS skill_exposures(workspace TEXT, split_group_id TEXT, job_id TEXT, purpose TEXT,
    PRIMARY KEY(workspace,split_group_id,job_id,purpose));
CREATE TABLE IF NOT EXISTS skill_uses(id TEXT PRIMARY KEY, version_id TEXT NOT NULL, session_id TEXT NOT NULL,
    run_id TEXT, kind TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skill_validations(id TEXT PRIMARY KEY, version_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skill_trial_sessions(session_id TEXT, version_id TEXT, run_id TEXT,
    PRIMARY KEY(session_id,version_id));
'''


class SkillStore:
    def __init__(self, database):
        self.path = Path(database)
        with self.transaction() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=10000')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def cases(self, workspace, *, after='', limit=100):
        with self.transaction() as db:
            rows = db.execute('SELECT body FROM skill_cases WHERE workspace=? AND id>? ORDER BY id LIMIT ?',
                              (workspace, after, limit + 1)).fetchall()
        values = [json.loads(row[0]) for row in rows[:limit]]
        return dict(cases=values, next_cursor=values[-1]['id'] if len(rows) > limit else None)

    def all_cases(self, workspace):
        cursor, result = '', []
        while True:
            page = self.cases(workspace, after=cursor)
            result.extend(page['cases'])
            cursor = page['next_cursor']
            if not cursor:
                return result

    def replace_session(self, session_id, cases, fingerprint):
        with self.transaction() as db:
            old = {r['id']: json.loads(r['body']).get('source_revision', r['revision'])
                   for r in db.execute('SELECT id,revision,body FROM skill_cases WHERE session_id=?', (session_id,))}
            db.execute('DELETE FROM skill_cases WHERE session_id=?', (session_id,))
            for case in cases:
                db.execute('INSERT OR REPLACE INTO skill_cases VALUES(?,?,?,?,?)',
                           (case['id'], case['scope']['workspace'] or '', session_id, case['revision'], canonical(case)))
                db.execute('INSERT OR IGNORE INTO skill_case_revisions VALUES(?,?,?)', (case['id'], case['revision'], canonical(case)))
            current = {c['id']: c.get('source_revision', c['revision']) for c in cases}
            for key, revision in old.items():
                if current.get(key) != revision:
                    db.execute("UPDATE skill_versions SET state='suspended', revision=revision+1 WHERE state IN ('active','trial','candidate') "
                               'AND id IN (SELECT version_id FROM skill_dependencies WHERE case_id=?)', (key,))
            db.execute('INSERT OR REPLACE INTO skill_source_cursors VALUES(?,?)', (session_id, fingerprint))
            db.execute('DELETE FROM skill_dirty_sessions WHERE session_id=?', (session_id,))

    def save_job(self, body):
        with self.transaction() as db:
            db.execute('UPDATE skill_jobs SET state=?,body=? WHERE id=?', (body['state'], canonical(body), body['id']))

    def job(self, identifier):
        with self.transaction() as db:
            row = db.execute('SELECT body FROM skill_jobs WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('Skill induction job not found')
        return json.loads(row[0])

    def version(self, identifier, *, db=None):
        if db is None:
            with self.transaction() as connection:
                return self.version(identifier, db=connection)
        row = db.execute('SELECT body,state,revision FROM skill_versions WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('Skill version not found')
        value = json.loads(row['body'])
        return {**value, 'state': row['state'], 'revision': row['revision']}

    def dependencies_valid(self, version_id, *, db):
        rows = db.execute('''SELECT c.body,d.case_revision FROM skill_dependencies d
            LEFT JOIN skill_cases c ON c.id=d.case_id WHERE d.version_id=?''', (version_id,)).fetchall()
        return bool(rows) and all(row['body'] and json.loads(row['body'])['revision'] == row['case_revision']
                                  and self.source_valid(json.loads(row['body']), db=db) for row in rows)

    def source_valid(self, case, *, db):
        session = db.execute('SELECT body FROM sessions WHERE id=?', (case['session_id'],)).fetchone()
        if not session or json.loads(session[0]).get('deleted_at'):
            return False
        provenance = case['provenance']
        if not provenance.get('record_id'):
            return True
        row = db.execute('SELECT body FROM semantic_jobs WHERE id=?', (provenance['evaluation_id'],)).fetchone()
        if not row:
            return False
        body = json.loads(row[0])
        memory = body.get('checkpoint', {}).get('memory', {})
        record = memory.get('records', {}).get(provenance['record_id'])
        return (bool(record) and record.get('publication_state') == 'published'
                and memory.get('identity', {}).get('source') == case['source_digest']
                and memory.get('identity', {}).get('graph_revision') == body.get('checkpoint', {}).get('graph_revision')
                and body.get('memory_scope', {}).get('workspace') == case['scope']['workspace']
                and digest(record) == provenance['record_digest'])

    def check_version(self, identifier):
        with self.transaction() as db:
            version = self.version(identifier, db=db)
            if version.get('source', {}).get('kind') == 'human_import':
                raise ValueError('Imported methods are explicit Agent guidance; manage adoption through the Agent profile')
            valid = self.dependencies_valid(identifier, db=db)
            if not valid and version['state'] in {'active', 'trial', 'candidate'}:
                db.execute("UPDATE skill_versions SET state='suspended',revision=revision+1 WHERE id=?", (identifier,))
                version.update(state='suspended', revision=version['revision'] + 1)
            version['sources_available'] = valid
            return version

    def insert_version(self, job, candidate):
        identifier = 'skillv:' + digest([job['id'], candidate])[:32]
        body = dict(id=identifier, skill_id=job['skill_id'], workspace=job['workspace'], job_id=job['id'],
                    created_at=now_iso(), contract=candidate, evidence_state='proposed',
                    coverage=job['coverage'], split=job['split'], validation=dict(status='not_executed'),
                    limitations=job['limitations'], parent_version_id=job.get('parent_version_id'))
        with self.transaction() as db:
            # Compare-and-swap against parent revision: concurrent induction cannot replace a newer decision.
            if job.get('parent_version_id'):
                parent = self.version(job['parent_version_id'], db=db)
                if parent['revision'] != job['parent_revision']:
                    raise ValueError('Parent Skill revision changed; create a fresh induction job')
            for case in job['delivered_cases']:
                if not self.source_valid(case, db=db):
                    raise ValueError('Case source changed during induction')
                current = db.execute('SELECT revision,body FROM skill_cases WHERE id=?', (case['id'],)).fetchone()
                if (not current or current[0] != case['revision'] or any(json.loads(current[1]).get(k) != case.get(k)
                        for k in ('task_family_id', 'split_group_id'))):
                    raise ValueError('Case revision changed during induction')
            db.execute("INSERT OR IGNORE INTO skill_versions VALUES(?,?,?,'candidate',1,?)",
                       (identifier, job['skill_id'], job['workspace'], canonical(body)))
            for case in job['delivered_cases']:
                db.execute('INSERT OR IGNORE INTO skill_dependencies VALUES(?,?,?)', (identifier, case['id'], case['revision']))
        return identifier

    def transition(self, identifier, state, revision):
        if type(revision) is not int or revision < 1:
            raise ValueError('Expected a positive integer Skill revision')
        if state not in {'trial', 'active', 'suspended', 'deprecated', 'retired'}:
            raise ValueError('Unsupported Skill state')
        with self.transaction() as db:
            version = self.version(identifier, db=db)
            if version['revision'] != revision:
                raise ValueError('Skill revision conflict')
            if version.get('source', {}).get('kind') == 'human_import':
                raise ValueError('Imported methods are explicit Agent guidance; manage adoption through the Agent profile')
            if version['state'] == 'retired':
                raise ValueError('Retired versions are immutable; induce a new candidate')
            if state in {'active', 'trial'}:
                if not self.dependencies_valid(identifier, db=db):
                    raise ValueError('Skill sources are stale or unavailable')
                if version['contract'].get('blocking_issues'):
                    raise ValueError('Resolve critical evidence conflicts in a new version first')
                if state == 'active':
                    rows = db.execute('SELECT body FROM skill_validations WHERE version_id=?', (identifier,)).fetchall()
                    if not any(json.loads(row[0]).get('publication_passed') for row in rows):
                        raise ValueError('Independent executable validation is required before activation')
                    db.execute("UPDATE skill_versions SET state='deprecated',revision=revision+1 WHERE skill_id=? AND state='active' AND id<>?",
                               (version['skill_id'], identifier))
            db.execute('UPDATE skill_versions SET state=?,revision=revision+1 WHERE id=?', (state, identifier))
        return self.version(identifier)

    def record_use(self, version_id, session_id, run_id, kind, payload):
        if kind not in {'retrieved', 'selected', 'step_executed', 'outcome'}:
            raise ValueError('Unknown Skill usage event')
        body = dict(version_id=version_id, session_id=session_id, run_id=run_id, kind=kind, payload=payload)
        key = 'use:' + digest(body)
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO skill_uses VALUES(?,?,?,?,?,?)',
                       (key, version_id, session_id, run_id, kind, canonical({**body, 'at': now_iso()})))
        return key
