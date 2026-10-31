"""Authenticated service facade for workspace-scoped Skill jobs and versions."""

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

from loom.core import now_iso
from loom.evaluation.provider import create_evaluation_provider
from loom.memory.store import bound_workspace
from loom.service.contracts import ServiceError
from loom.skills.catalog import CaseCatalog
from loom.skills.contracts import VERSION, canonical, digest
from loom.skills.induction import BudgetStop, Induction
from loom.skills.retrieval import freeze_split
from loom.skills.store import SkillStore
from loom.tasks.config import load_task_config

DEFAULTS = dict(max_calls=9, max_tokens=1000000, max_seconds=1200, max_call_seconds=180,
                max_prompt_chars=160000, max_evidence_chars=48000, output_tokens=8000, revision_rounds=2)
BOUNDS = dict(max_calls=(1, 12), max_tokens=(1000, 5000000), max_seconds=(1, 3600), max_call_seconds=(1, 240),
              max_prompt_chars=(10000, 250000), max_evidence_chars=(1000, 96000), output_tokens=(1000, 16000), revision_rounds=(0, 2))


class SessionSkills:
    def __init__(self, trajectory, semantic):
        self.trajectory, self.semantic = trajectory, semantic
        self.store = SkillStore(trajectory.store.path)
        self.catalog = CaseCatalog(self.store, trajectory)
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.cancels, self.futures = {}, {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='loom-skills')
        self.index_error = None
        with self.store.transaction() as db:
            for row in db.execute("SELECT body FROM skill_jobs WHERE state IN ('queued','running')").fetchall():
                body = json.loads(row[0])
                body.update(state='interrupted', error='Service restarted; resume retains completed stages and unknown request costs')
                # A lost response is never assumed free, including time.
                for request in body['requests']:
                    if request['state'] == 'reserved':
                        request['state'] = 'response_unknown'
                        body['usage']['seconds'] += body['settings']['max_call_seconds']
                db.execute('UPDATE skill_jobs SET state=?,body=? WHERE id=?', (body['state'], canonical(body), body['id']))
        self.indexer = threading.Thread(target=self._index_updates, name='loom-skill-catalog', daemon=True)
        self.indexer.start()

    def _index_updates(self):
        while not self.closed.wait(5):
            try:
                with self.store.transaction() as db:
                    rows = db.execute('SELECT s.id,s.body FROM skill_dirty_sessions d JOIN sessions s ON s.id=d.session_id LIMIT 100').fetchall()
                    for row in rows:
                        if not bound_workspace(json.loads(row['body'])['task']):
                            db.execute('DELETE FROM skill_dirty_sessions WHERE session_id=?', (row['id'],))
                scopes = {bound_workspace(json.loads(row['body'])['task']) for row in rows}
                for scope in scopes - {None}:
                    if self.closed.is_set():
                        return
                    self.catalog.reconcile(scope)
                self.index_error = None
            except Exception as exc:
                # Indexing must never fail the task/evaluator transaction; keep dirty sources for reconciliation.
                self.index_error = str(exc)

    def close(self):
        self.closed.set()
        self.indexer.join(timeout=10)
        for event in self.cancels.values():
            event.set()
        self.pool.shutdown(wait=True)

    def scope(self, session_id):
        with self.trajectory.store.transaction() as db:
            state = self.trajectory.store._load(db, session_id)
        workspace = bound_workspace(state['task'])
        if not workspace:
            raise ServiceError('Cross-task Skills require an explicit workspace', 409)
        return workspace

    def listing(self, session_id):
        workspace = self.scope(session_id)
        stats = self.catalog.reconcile(workspace)
        with self.store.transaction() as db:
            jobs = [self.public_job(json.loads(r[0])) for r in db.execute(
                'SELECT body FROM skill_jobs WHERE workspace=? ORDER BY rowid DESC LIMIT 100', (workspace,))]
            ids = [r[0] for r in db.execute(
                "SELECT id FROM skill_versions WHERE workspace=? AND state<>'user_guidance' ORDER BY rowid DESC LIMIT 100", (workspace,))]
        return dict(workspace=workspace, catalog=stats, index_error=self.index_error, jobs=jobs, versions=[self.public_version(i) for i in ids],
                    defaults=DEFAULTS, bounds=BOUNDS, models=self.semantic.options()['models'],
                    default_model=self.semantic.options()['default_model'])

    @staticmethod
    def public_job(job):
        return {k: job[k] for k in ('id', 'state', 'query', 'model', 'created_at', 'stage', 'usage', 'settings', 'error',
                                  'version_id', 'coverage', 'limitations', 'searches', 'parent_version_id',
                                  'candidate_diagnostic') if k in job}

    def public_version(self, identifier):
        version = self.store.check_version(identifier)
        if not version['sources_available']:
            return {k: version[k] for k in ('id', 'skill_id', 'workspace', 'state', 'revision', 'sources_available')}
        with self.store.transaction() as db:
            version['validations'] = [json.loads(r[0]) for r in db.execute(
                'SELECT body FROM skill_validations WHERE version_id=?', (identifier,))]
            version['uses'] = [json.loads(r[0]) for r in db.execute(
                'SELECT body FROM skill_uses WHERE version_id=? ORDER BY rowid DESC LIMIT 100', (identifier,))]
        if any(v.get('publication_passed') for v in version['validations']):
            version['evidence_state'] = 'independently_tested'
        return version

    def start(self, session_id, options):
        if not isinstance(options, dict) or set(options) - {'query', 'model', 'settings', 'parent_version_id'}:
            raise ValueError('Use query, model, settings and optional parent_version_id')
        query = options.get('query')
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 4000:
            raise ValueError('Provide a problem or method to compare (1–4000 characters)')
        workspace = self.scope(session_id)
        self.catalog.reconcile(workspace)
        settings = dict(DEFAULTS)
        supplied = options.get('settings', {})
        if not isinstance(supplied, dict) or set(supplied) - settings.keys():
            raise ValueError('Unknown Skill budget option')
        for key, value in supplied.items():
            low, high = BOUNDS[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{key} must be between {low} and {high}')
            settings[key] = value
        catalog = self.semantic.options()
        model = options.get('model') or catalog['default_model']
        if model not in {m['id'] for m in catalog['models']}:
            raise ValueError('Choose a configured evaluation model')
        config_digest = hashlib.sha256(Path(self.semantic.config_path).read_bytes()).hexdigest() if self.semantic.config_path else 'test'
        corpus = self.store.all_cases(workspace)
        if not any(c['evidence'] for c in corpus):
            raise ValueError('No usable Deep eval memory in this workspace')
        parent = self.store.check_version(options['parent_version_id']) if options.get('parent_version_id') else None
        if parent and parent['workspace'] != workspace:
            raise ValueError('Parent Skill is outside this workspace')
        fingerprint = digest([(c['id'], c['revision'], c['task_family_id'], c['split_group_id']) for c in corpus])
        identity = digest([VERSION, workspace, fingerprint, query, model, config_digest, settings,
                           parent and [parent['id'], parent['revision']]])
        identifier = 'induction:' + identity[:32]
        with self.lock:
            with self.store.transaction() as db:
                existing = db.execute('SELECT body FROM skill_jobs WHERE id=?', (identifier,)).fetchone()
                if existing:
                    return self.public_job(json.loads(existing[0]))
                if db.execute("SELECT COUNT(*) FROM skill_jobs WHERE state IN ('queued','running')").fetchone()[0] >= 4:
                    raise ValueError('Four Skill induction jobs are already queued or running')
                exposed = {r[0] for r in db.execute('SELECT split_group_id FROM skill_exposures WHERE workspace=?', (workspace,))}
                # Case identities previously exposed stay exposed even after lineage groups coalesce.
                old_jobs = [json.loads(r[0]) for r in db.execute('SELECT body FROM skill_jobs WHERE workspace=?', (workspace,))]
                seen_cases = {c['id'] for job in old_jobs for c in job.get('delivered_cases', [])}
                seen_cases.update(cid for job in old_jobs for cid in job.get('exposed_case_ids', []))
                exposed.update(c['split_group_id'] for c in corpus if c['id'] in seen_cases)
                body = dict(id=identifier, workspace=workspace, session_id=session_id, query=query, model=model,
                    state='queued', stage='frozen', created_at=now_iso(), settings=settings,
                    algorithm=VERSION, config_digest=config_digest, corpus_revision=fingerprint, corpus=corpus,
                    split=freeze_split(corpus, exposed_groups=exposed), checkpoints={}, requests=[],
                    usage=dict(calls=0, tokens=0, seconds=0), delivered_cases=[], coverage={},
                    limitations=['Historical applicability is not executable verification; whole-route composition remains proposed.'],
                    skill_id=parent['skill_id'] if parent else 'skill:' + digest([workspace, query])[:24],
                    parent_version_id=parent['id'] if parent else None, parent_revision=parent['revision'] if parent else None)
                db.execute('INSERT INTO skill_jobs VALUES(?,?,?,?)', (identifier, workspace, body['state'], canonical(body)))
            self._submit(identifier)
        return self.public_job(body)

    def _submit(self, identifier):
        self.cancels[identifier] = threading.Event()
        self.futures[identifier] = self.pool.submit(self._work, identifier)

    def _work(self, identifier):
        body = self.store.job(identifier)
        try:
            if self.semantic.provider_factory:
                provider = self.semantic.provider_factory(body['model'])
            else:
                current = hashlib.sha256(Path(self.semantic.config_path).read_bytes()).hexdigest()
                if current != body['config_digest']:
                    raise ValueError('Model configuration changed; create a new induction job')
                config = load_task_config(self.semantic.config_path).unwrap()
                provider = create_evaluation_provider(config, model_name=body['model']).unwrap()
            def stopped():
                return self.closed.is_set() or self.cancels[identifier].is_set()
            body.update(state='running', error=None)
            self.store.save_job(body)
            asyncio.run(Induction(self.store, self.catalog, provider, stopped=stopped).run(body))
        except BudgetStop as exc:
            body.update(state='cancelled' if self.cancels[identifier].is_set() else 'budget_exhausted', error=str(exc))
            self.store.save_job(body)
        except Exception as exc:
            body.update(state='failed', error=str(exc))
            self.store.save_job(body)

    def resume(self, identifier):
        with self.lock:
            if identifier in self.futures and not self.futures[identifier].done():
                raise ValueError('Skill job is already running')
            body = self.store.job(identifier)
            if body['state'] not in {'failed', 'interrupted', 'cancelled'}:
                raise ValueError('Only failed, interrupted or cancelled jobs can resume with their frozen budgets')
            body.update(state='queued', error=None)
            self.store.save_job(body)
            self._submit(identifier)
        return self.public_job(body)

    def dispatch(self, method, parts, query, payload):
        parts = [unquote(part) for part in parts]
        if method == 'POST' and not isinstance(payload, dict):
            raise ValueError('Skill requests require a JSON object')
        session_id = query.get('session_id', [''])[0]
        workspace = self.scope(session_id)
        if not parts and method == 'GET':
            return self.listing(session_id)
        if parts == ['cases'] and method == 'GET':
            self.catalog.reconcile(workspace)
            result = self.store.cases(workspace, after=query.get('after', [''])[0])
            # Inspecting detailed case content makes it discovery data for future jobs.
            with self.store.transaction() as db:
                for case in result['cases']:
                    db.execute('INSERT OR IGNORE INTO skill_exposures VALUES(?,?,?,?)',
                               (workspace, case['split_group_id'], 'ui-inspection', 'discovery'))
            return result
        if parts == ['jobs'] and method == 'POST':
            return self.start(session_id, payload)
        if len(parts) >= 2 and parts[0] == 'jobs':
            job = self.store.job(parts[1])
            if job['workspace'] != workspace:
                raise ValueError('Job is outside this workspace')
            if len(parts) == 2 and method == 'GET':
                return self.public_job(job)
            if parts[2:] == ['resume'] and method == 'POST':
                return self.resume(parts[1])
            if parts[2:] == ['cancel'] and method == 'POST':
                if parts[1] in self.cancels:
                    self.cancels[parts[1]].set()
                return self.public_job(job)
        if len(parts) >= 2 and parts[0] == 'versions':
            version = self.store.version(parts[1])
            if version['workspace'] != workspace:
                raise ValueError('Version is outside this workspace')
            if len(parts) == 2 and method == 'GET':
                return self.public_version(parts[1])
            if parts[2:] == ['state'] and method == 'POST':
                return self.store.transition(parts[1], payload.get('state'), payload.get('revision'))
            if parts[2:] == ['trial'] and method == 'POST':
                with self.trajectory.store.transaction() as db:
                    session = self.trajectory.store._load(db, session_id)
                if (session.get('run') or {}).get('state') in {'running', 'suspended'}:
                    raise ValueError('Finish the current run before scheduling a bounded trial for the next run')
                self.store.transition(parts[1], 'trial', payload.get('revision'))
                with self.store.transaction() as db:
                    db.execute('INSERT OR REPLACE INTO skill_trial_sessions VALUES(?,?,NULL)', (session_id, parts[1]))
                return self.public_version(parts[1])
            if parts[2:] == ['package'] and method == 'GET':
                from loom.skills.package import package
                version = self.public_version(parts[1])
                if not version['sources_available']:
                    raise ValueError('Unavailable source evidence cannot be exported')
                return package(version)
        raise ServiceError('Skill route not found', 404)
