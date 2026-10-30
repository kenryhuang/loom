"""Skill adapter for the existing paired experiment engine.

An execution backend must reconstruct a sealed snapshot into the supplied fresh
side directory and return real trace/evaluation artifacts. Historical traces are
never executable backends. The adapter owns injection, isolation and publication
criteria; an arbitrary HTTP validation report cannot promote a Skill.
"""

import inspect
import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from loom.campaigns.contracts import ExperimentPhase, ObjectiveDirection, ObjectiveSpec, TaskSetRef
from loom.campaigns.experiments import PairedExperimentRunner, ValidatedCandidate, objective_gate_result
from loom.evaluation.experiments import TrialEntry, TrialPlan
from loom.skills.contracts import canonical, digest
from loom.skills.lineage import related

SUCCESS = ObjectiveSpec('acceptance', ObjectiveDirection.MAXIMIZE, hard=True, absolute_limit=1,
                        max_baseline_regression=0, min_valid_pairs=5, missing_policy='fail_closed')


class SkillPairedValidation:
    """Trusted execution integration, intentionally not an endpoint accepting model-supplied scores."""

    def __init__(self, store, artifacts, execute, directory):
        self.store, self.artifacts, self.execute = store, artifacts, execute
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    async def run(self, version_id, tasks, environment, *, repetitions=3):
        version = self.store.check_version(version_id)
        if not version['sources_available'] or version['contract'].get('blocking_issues'):
            raise ValueError('Skill source/evidence checks must pass before executable validation')
        if type(repetitions) is not int or not 3 <= repetitions <= 5 or not 4 <= len(tasks) <= 12:
            raise ValueError('Use 4–12 sealed tasks and 3–5 repetitions')
        required = {'environment_digest', 'solver_digest', 'tools_digest', 'permissions_digest', 'evaluator_digest',
                    'knowledge_digest', 'memory_digest', 'budget_digest'}
        if set(environment) != required or any(not isinstance(v, str) or len(v) != 64 for v in environment.values()):
            raise ValueError('Freeze environment, solver, tools, permissions, evaluator, knowledge, memory and budget digests')
        identity = 'skill-validation:' + digest([version_id, tasks, environment, repetitions])
        with self.store.transaction() as db:
            existing = db.execute('SELECT body FROM skill_validations WHERE id=?', (identity,)).fetchone()
            if existing:
                return json.loads(existing[0])
        job = self.store.job(version['job_id'])
        seen_ids = set(job.get('exposed_case_ids', [])) | {c['id'] for c in job['delivered_cases']}
        discovery = [c for c in job['corpus'] if c['id'] in seen_ids]
        discovery_families = {c['task_family_id'] for c in discovery}
        discovery_groups = {c['split_group_id'] for c in discovery}
        discovery_snapshots = {c.get('project_snapshot_digest') for c in discovery} - {None}
        phases = {'validation': [], 'holdout': []}
        groups, fingerprints = {}, set()
        lineage_rows = []
        with self.store.transaction() as db:
            exposed = {r[0] for r in db.execute('SELECT split_group_id FROM skill_exposures WHERE workspace=?', (version['workspace'],))}
        for task in tasks:
            mandatory = {'task_fingerprint', 'family_id', 'split_group_id', 'phase', 'snapshot_digest', 'acceptance_digest',
                         'snapshot_ref', 'contract', 'skill_versions_seen', 'prior_memory_refs'}
            if set(task) != mandatory or task['phase'] not in phases:
                raise ValueError('Use sealed task, lineage, snapshot, acceptance, contract and exposure fields')
            if not task['snapshot_digest'] or not task['acceptance_digest'] or not task['snapshot_ref']:
                raise ValueError('Task cannot be replayed without an initial snapshot and executable acceptance contract')
            if task['task_fingerprint'] in fingerprints:
                raise ValueError('Duplicate validation task fingerprint')
            fingerprints.add(task['task_fingerprint'])
            lineage = dict(session_id='trial:' + task['task_fingerprint'], task_contract=task['contract'])
            if any(related(lineage, c) for c in discovery):
                raise ValueError('Validation task is a normalized or near-duplicate discovery task')
            for previous, prior_lineage in lineage_rows:
                if related(lineage, prior_lineage) and any(task[k] != previous[k] for k in ('family_id', 'split_group_id', 'phase')):
                    raise ValueError('Related task variants cannot claim independent families or cross splits')
            lineage_rows.append((task, lineage))
            if (task['family_id'] in discovery_families or task['split_group_id'] in discovery_groups | exposed
                    or task['snapshot_digest'] in discovery_snapshots or task['skill_versions_seen'] or task['prior_memory_refs']):
                raise ValueError('Validation task has discovery/Skill/memory exposure')
            frozen_role = job['split'].get(task['split_group_id'])
            if frozen_role and frozen_role != task['phase']:
                raise ValueError('Task differs from the pre-induction split')
            for kind, key in [('family', task['family_id']), ('group', task['split_group_id']), ('snapshot', task['snapshot_digest'])]:
                if groups.get((kind, key), task['phase']) != task['phase']:
                    raise ValueError('Related tasks cannot cross validation and holdout')
                groups[kind, key] = task['phase']
            phases[task['phase']].append(task)
        if any(len({t['family_id'] for t in rows}) < 2 or len({t['split_group_id'] for t in rows}) < 2 for rows in phases.values()):
            raise ValueError('Each phase needs two previously unseen families and isolation groups')
        with self.store.transaction() as db:
            existing = db.execute('SELECT body FROM skill_validations WHERE id=?', (identity,)).fetchone()
            if existing:
                return json.loads(existing[0])  # No hidden duplicate executions or cost reset.
            frozen = dict(id=identity, version_id=version_id, state='running', publication_passed=False,
                          tasks=tasks, environment=environment, repetitions=repetitions, phases={}, executions=[])
            db.execute('INSERT INTO skill_validations VALUES(?,?,?)', (identity, version_id, canonical(frozen)))
            for task in tasks:
                db.execute('INSERT OR IGNORE INTO skill_exposures VALUES(?,?,?,?)',
                           (version['workspace'], task['split_group_id'], identity, task['phase']))
        candidate_artifact = self.artifacts.publish_bytes(canonical(version['contract']).encode(), kind='skill',
                                                          schema_version='loom.skill.v1', suffix='.json').unwrap()
        baseline = self.artifacts.publish_bytes(canonical(environment).encode(), kind='skill_baseline',
                                               schema_version='loom.skill.baseline.v1', suffix='.json').unwrap()
        candidate = ValidatedCandidate(identity, version_id, candidate_artifact, baseline,
            digest(environment), environment['solver_digest'], environment['tools_digest'],
            environment['permissions_digest'], environment['evaluator_digest'])
        try:
            for phase, rows in phases.items():
                manifest = self.artifacts.publish_bytes(canonical(rows).encode(), kind='task_set',
                    schema_version='loom.skill.task_set.v1', suffix='.json').unwrap()
                task_set = TaskSetRef(identity + ':' + phase, ExperimentPhase(phase), manifest, digest(rows))
                by_key = {t['task_fingerprint']: t for t in rows}
                plan = TrialPlan(tuple(TrialEntry(t['task_fingerprint'], repeat, repeat) for t in rows for repeat in range(repetitions)))

                async def execute(side, _candidate, entry, trial_id, by_key=by_key):
                    # The backend receives ONLY its own task and a frozen explicit injection, with fresh stores per side.
                    folder = Path(tempfile.mkdtemp(prefix='skill-trial-', dir=self.directory))
                    injection = None if side == 'baseline' else dict(version_id=version_id, contract=version['contract'], mode='trial')
                    call = dict(side=side, task=by_key[entry.task_fingerprint], seed=entry.seed, trial_id=trial_id,
                                directory=folder, environment=dict(environment), skill=injection,
                                shared_memory=False, shared_knowledge=False, automatic_skills=False)
                    attempt = dict(trial_id=trial_id, side=side, directory=str(folder), state='started', exposed=side == 'candidate')
                    frozen['executions'].append(attempt)
                    self.save(frozen)
                    value = self.execute(**call)
                    result = await value if inspect.isawaitable(value) else value
                    # Preserve infrastructure failures and non-adoption. Never filter to successful users of the Skill.
                    if result.failure_kind != 'infrastructure':
                        if result.trace_ref is None or result.evaluation_ref is None:
                            raise ValueError('Executed trials require original trace and evaluation artifacts')
                        trace = self.artifacts.read_bytes(result.trace_ref).unwrap()
                        evaluation = json.loads(self.artifacts.read_bytes(result.evaluation_ref).unwrap())
                        if not trace or evaluation.get('schema_version') != 'loom.evaluation.bundle.v1':
                            raise ValueError('Invalid original execution evidence')
                        if result.metrics.get('acceptance') not in {0, 1}:
                            raise ValueError('Acceptance must report actual binary verifier output')
                    attempt.update(state='completed', failure_kind=result.failure_kind,
                                   metrics=dict(result.metrics), usage=dict(tokens=result.solver_tokens, cost=result.cost))
                    self.save(frozen)
                    return result

                bundle = (await PairedExperimentRunner(execute, objectives=(SUCCESS,)).evaluate(
                    candidate, task_set, plan, experiment_id=identity + ':' + phase)).unwrap()
                phase_passed = (bundle.status.value == 'completed' and not bundle.failures and bundle.metrics
                                and objective_gate_result(SUCCESS, bundle.metrics[0]).value == 'passed')
                ref = self.artifacts.publish_bytes(canonical(asdict(bundle)).encode(), kind='skill_experiment',
                                                   schema_version='loom.skill.experiment.v1', suffix='.json').unwrap()
                frozen['phases'][phase] = dict(passed=bool(phase_passed), artifact=asdict(ref),
                                               metrics=[asdict(m) for m in bundle.metrics], failures=[asdict(f) for f in bundle.failures])
                self.save(frozen)
                if not phase_passed:
                    break  # Keep holdout unread if validation already fails.
            # Branchful candidates need explicit branch execution coverage; retain candidate until that backend is supplied.
            frozen.update(state='completed', publication_passed=(version['coverage'].get('trial_coverage_met', False)
                and all(frozen['phases'].get(p, {}).get('passed') for p in phases) and not version['contract']['branches']))
            if version['contract']['branches']:
                frozen['limitation'] = 'Declared branch coverage needs an execution backend with branch receipts; automatic publication blocked'
            if not self.store.check_version(version_id)['sources_available']:
                frozen.update(publication_passed=False, limitation='Source changed during validation')
        except BaseException as exc:
            frozen.update(state='interrupted', publication_passed=False, error=str(exc))
            self.save(frozen)
            raise
        self.save(frozen)
        return frozen

    def save(self, body):
        with self.store.transaction() as db:
            db.execute('UPDATE skill_validations SET body=? WHERE id=?', (canonical(body), body['id']))
