"""Executable Skill trials using the existing Meta-Harness fresh-workspace runner."""

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from loom.campaigns.experiments import TrialExecution, ValidatedCandidate
from loom.evaluation.experiments import TrialEntry
from loom.optimize.task_sets import _snapshot_workspace
from loom.optimize.trial_executor import OptimizeTrialExecutor
from loom.skills.contracts import canonical, digest
from loom.skills.runtime import POLICY
from loom.tasks.assembly import TaskAssembly
from loom.tasks.request import TaskHarness, TaskRequest


def sealed_task(prepared, *, family_id, split_group_id, phase):
    """Build validation input from an already frozen task, never today's working tree."""
    task = prepared.task
    if not task.verifier:
        raise ValueError('Executable acceptance verifier required')
    if task.metadata:
        raise ValueError('First Skill backend supports self-contained tasks without external memory, knowledge or tools')
    return dict(task_fingerprint=prepared.fingerprint_key, family_id=family_id, split_group_id=split_group_id, phase=phase,
                snapshot_digest=prepared.snapshot.digest, snapshot_ref=str(prepared.snapshot.path),
                acceptance_digest=digest(asdict(task.verifier)),
                contract=dict(objective=task.objective, constraints=list(task.constraints), expected_outputs=list(task.expected_outputs)),
                skill_versions_seen=[], prior_memory_refs=[])


class OptimizeSkillBackend:
    """Reuse real solver execution, snapshot materialization, verifier and trace analysis.

    No historical-session replay or shared live stores. External-dependency tasks
    require a different backend that can freeze those dependencies explicitly.
    """

    def __init__(self, prepared_tasks, artifacts, provider, *, environment, timeout_seconds=900):
        self.tasks, self.artifacts, self.provider = prepared_tasks, artifacts, provider
        self.environment, self.timeout_seconds = dict(environment), timeout_seconds

    async def __call__(self, *, side, task, seed, trial_id, directory, environment, skill,
                       shared_memory, shared_knowledge, automatic_skills):
        prepared = self.tasks.task_by_fingerprint.get(task['task_fingerprint'])
        if (not prepared or environment != self.environment or shared_memory or shared_knowledge or automatic_skills
                or bool(skill) != (side == 'candidate')):
            return TrialExecution.infrastructure_failure(trial_id, side, 'SKILL_TRIAL_CONFIGURATION_MISMATCH')
        expected = sealed_task(prepared, family_id=task['family_id'], split_group_id=task['split_group_id'], phase=task['phase'])
        if expected != task:
            return TrialExecution.infrastructure_failure(trial_id, side, 'SEALED_TASK_CHANGED')
        # Rehash and recopy into a fresh isolated root; mutable snapshot paths cannot silently change the experiment.
        verified = _snapshot_workspace(Path(task['snapshot_ref']), directory / 'sealed')
        if not verified.ok or verified.value.digest != task['snapshot_digest']:
            return TrialExecution.infrastructure_failure(trial_id, side, 'SNAPSHOT_DIGEST_MISMATCH')
        method = None
        if skill:
            method = {k: v for k, v in skill['contract'].items() if k not in {'claims', 'blocking_issues'}}
            assembly = TaskAssembly(TaskRequest(prepared.task.objective, workspace=verified.value.path), plan_mode='off')
            try:
                import platform
                facts = dict(platform=platform.system().lower(), workspace_access='read-write', runtime='native_os')
                tools = set(assembly.bindings)
                if set(method['required_tools']) - tools or not all(
                        g['value'] in tools if g['field'] == 'tool' else facts.get(g['field']) == g['value'] for g in method['guards']):
                    return TrialExecution.infrastructure_failure(trial_id, side, 'SKILL_CAPABILITY_GUARD_FAILED')
            finally:
                await assembly.close()
        artifact = self.artifacts.publish_bytes(canonical(skill or {}).encode(), kind='skill_trial', schema_version='loom.skill.trial.v1').unwrap()
        baseline = self.artifacts.publish_bytes(canonical(environment).encode(), kind='baseline', schema_version='loom.skill.baseline.v1').unwrap()
        candidate = ValidatedCandidate(trial_id, skill['version_id'] if skill else 'baseline', artifact, baseline, digest(environment),
                                      environment['solver_digest'], environment['tools_digest'],
                                      environment['permissions_digest'], environment['evaluator_digest'])
        # Only a scoped declaration differs; no new tools or permissions are introduced.
        addendum = POLICY + '\nExplicit trial method (may be declined if inapplicable):\n' + canonical(method) if method else ''
        harness = TaskHarness(system_prompt_addendum=addendum)
        isolated = SimpleNamespace(task_by_fingerprint={task['task_fingerprint']: replace(prepared, snapshot=verified.value)})
        executor = OptimizeTrialExecutor(isolated, self.artifacts, workspace_root=directory / 'execution', solver_provider=self.provider,
                                        candidate_harnesses={candidate.candidate_id: harness}, trial_timeout_seconds=self.timeout_seconds)
        result = await executor.execute(side, candidate, TrialEntry(task['task_fingerprint'], seed, seed), trial_id)
        return replace(result, metrics={**dict(result.metrics), 'acceptance': result.metrics.get('task_success_rate')})
