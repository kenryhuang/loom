import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { DiagnosisView } from '../../src/loom/web/assets/views/diagnosis.mjs';
import { TrajectoryView } from '../../src/loom/web/assets/views/trajectory.mjs';

const budgets = { max_steps: 20, max_llm_calls: 30, max_tokens: 300000, max_duration_seconds: 600, max_tool_calls: 100 };
const job = { id: 'diag1', state: 'completed', source_cursor: 9, created_at: 'today', budgets, counters: { model_calls: 3, tool_calls: 2, tokens: 25 },
  report: { summary: '<script>untrusted</script>', status: 'inconclusive', findings: [{ title: 'No evidence', layer: 'unknown', confidence: 'unknown', cause: 'Historical state missing', evidence_ids: ['ref:1@0:9'], recommendation: 'Collect evidence', validation: 'Replay later', risk: 'Cost' }], causal_chain: [{ from: 'Limit', to: 'Pause', relation: 'inferred', evidence_ids: ['ref:1@0:9'] }], coverage: { checked: ['trace'], missing: ['history'] } }, markdown: '# Diagnosis\n\nNo files modified.' };
function setup() {
  const dom = new JSDOM('<section id="root"></section>'); globalThis.document = dom.window.document;
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  const root = document.getElementById('root'); return { dom, root };
}
function api() {
  return {
    diagnoses: async () => ({ default_mode: 'worktree', runs: ['run1', 'run2'], models: ['main'], default_model: 'main', budgets, jobs: [] }),
    diagnosis: async () => job,
    diagnosisEvents: async () => ({ events: [], next_after: 0 }),
    diagnosisEvidence: async () => ({ content: 'snapshot evidence <img onerror="alert()">', sanitized: true }),
  };
}

test('diagnosis remains usable when base analysis fails and does not auto-start a model', async () => {
  const { dom, root } = setup(); let starts = 0;
  const view = new TrajectoryView(root, {}), client = { ...api(), startTrajectory: async () => { throw new Error("list has no read"); }, startDiagnosis: async () => { starts++; return job; } };
  await view.open(client, 'session', 10); await new Promise(resolve => setTimeout(resolve, 0));
  view.selectTab(3);
  assert.equal(root.querySelectorAll('[role=tab]').length, 4);
  assert.equal(view.diagnosisPanel.hidden, false);
  assert.equal(view.diagnosisView.start.disabled, false);
  assert.equal(view.diagnosisView.run.options.length, 4);
  assert.equal(starts, 0);
  await view.diagnosisView.command('start');
  assert.equal(starts, 1);
  assert.match(view.diagnosisPanel.textContent, /Historical state missing/);
  assert.equal(view.diagnosisPanel.querySelector('script'), null);
  assert.equal(view.diagnosisPanel.querySelector('.diagnosis-link.inferred') !== null, true);
  assert.equal(view.diagnosisView.start.closest('.diagnosis-toolbar'), view.diagnosisView.run.closest('.diagnosis-toolbar'));
  assert.equal(view.diagnosisView.start.parentElement.className, 'diagnosis-actions');
  assert.equal(view.diagnosisView.run.parentElement.className, 'diagnosis-selectors');
  await view.diagnosisView.evidence('ref:1@0:9');
  assert.match(view.diagnosisView.dialog.textContent, /snapshot evidence/);
  assert.equal(view.diagnosisView.dialog.querySelector('img'), null);
  view.close(); dom.window.close();
});

test('leaving stops polling without cancelling; stale results cannot replace a new session', async () => {
  const { dom, root } = setup(); let resolveOld, cancelled = 0;
  const view = new DiagnosisView(root);
  const old = view.open({ ...api(), diagnoses: () => new Promise(resolve => { resolveOld = resolve; }), diagnosisCommand: async () => { cancelled++; } }, 'old');
  await view.open(api(), 'new');
  resolveOld({ runs: ['old-run'], models: [], budgets, jobs: [job] }); await old;
  assert.equal(view.sessionId, 'new'); assert.doesNotMatch(root.textContent, /old-run/);
  view.close(); assert.equal(cancelled, 0); dom.window.close();
});

test('resume sends selected total budgets only to the diagnostic job', async () => {
  const { dom, root } = setup(); const commands = [];
  const paused = { ...job, state: 'budget_exhausted', report: null, error: 'Budget exhausted', partial_report: 'Inspected source' };
  const view = new DiagnosisView(root);
  await view.open({ ...api(), diagnoses: async () => ({ runs: [], models: [], budgets, jobs: [paused] }), diagnosis: async () => paused,
    diagnosisCommand: async (...args) => { commands.push(args); return paused; } }, 'target');
  assert.equal(view.resume.hidden, false); assert.match(root.textContent, /Partial report/);
  view.budgetFields.max_llm_calls.value = '60'; await view.command('resume');
  assert.deepEqual(commands[0].slice(0, 4), ['target', 'diag1', 'resume', { budgets: { ...budgets, max_llm_calls: 60 } }]);
  view.close(); dom.window.close();
});

test('worktree verification defaults to auto-delete and merge requires reviewing the saved patch', async () => {
  const { dom, root } = setup(); const commands = []; let created;
  const workspace = { state: 'deleted', branch: 'loom-diagnosis/diag1', target_branch: 'main', path: '/source/.worktrees/diag1',
    cleanup: 'delete', files: ['test_regression.py'], patch_sha256: 'abc123', patch_bytes: 44,
    checks: [{ argv: ['python', '-m', 'unittest'], status: 'passed', exit_code: 0, seconds: .1, output: '<script>test output</script>', patch_sha256: 'abc123' }] };
  let current = { ...job, mode: 'worktree', workspace };
  const view = new DiagnosisView(root), client = { ...api(), diagnosis: async () => current,
    startDiagnosis: async (_id, body) => { created = body; return current; },
    diagnosisChanges: async () => ({ workspace, patch: '+def test_regression(): pass' }),
    diagnosisCommand: async (...args) => { commands.push(args); current = { ...current, workspace: { ...workspace, state: 'merged' } }; return current; },
  };
  await view.open(client, 'target');
  assert.equal(view.mode.value, 'worktree'); assert.equal(view.cleanup.value, 'delete');
  await view.command('start');
  assert.equal(created.mode, 'worktree'); assert.equal(created.cleanup, 'delete');
  assert.match(root.textContent, /Auto-delete · patch retained/);
  assert.match(view.history.selectedOptions[0].textContent, /loom-diagnosis\/diag1/);
  assert.equal(view.resume.hidden, false); assert.equal(view.resume.disabled, true);
  assert.match(root.textContent, /final patch/);
  assert.equal(root.querySelector('script'), null);
  assert.equal([...root.querySelectorAll('button')].some(b => b.textContent === 'Merge reviewed changes'), false);
  assert.equal(commands.length, 0);
  await view.reviewChanges();
  assert.match(view.dialog.textContent, /test_regression/);
  const merge = [...view.dialog.querySelectorAll('button')].find(b => b.textContent === 'Merge reviewed changes');
  merge.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(commands[0].slice(0, 4), ['target', 'diag1', 'merge', { patch_sha256: 'abc123' }]);
  assert.match(root.textContent, /merged/);
  // Refreshing the same diagnosis must rebuild the workspace panel.
  client.diagnoses = async () => ({ runs: [], models: [], budgets, jobs: [current] });
  await view.open(client, 'target');
  assert.match(root.textContent, /Verification workspace/);
  view.close(); dom.window.close();
});

test('old running backend keeps read-only requests until service restart', async () => {
  const { dom, root } = setup(); let created;
  const view = new DiagnosisView(root);
  await view.open({ ...api(), diagnoses: async () => ({ runs: [], models: [], budgets, jobs: [] }),
    startDiagnosis: async (_id, body) => { created = body; return job; } }, 'target');
  assert.equal(view.mode.hidden, true); assert.equal(view.cleanup.hidden, true);
  assert.match(root.textContent, /Restart the service/);
  await view.command('start');
  assert.equal('mode' in created, false); assert.equal('cleanup' in created, false);
  view.close(); dom.window.close();
});

test('budget limits are visible and all invalid fields are explained before start or resume', async () => {
  const { dom, root } = setup(); let sent = 0;
  const limits = { max_steps: 100, max_llm_calls: 200, max_tokens: 2000000, max_duration_seconds: 3600, max_tool_calls: 500 };
  const view = new DiagnosisView(root), client = { ...api(),
    diagnoses: async () => ({ runs: [], models: [], budgets, budget_limits: limits, jobs: [] }),
    startDiagnosis: async () => { sent++; return job; },
    diagnosisCommand: async () => { sent++; return job; },
  };
  await view.open(client, 'target');
  assert.equal(view.budgetFields.max_tokens.max, '2000000');
  assert.equal(view.budgetRanges.max_tokens.textContent, '1–2,000,000');
  view.budgetFields.max_tokens.value = '10000000'; view.budgetFields.max_duration_seconds.value = '7200';
  await view.command('start');
  assert.equal(sent, 0); assert.equal(view.budgetPanel.open, true);
  assert.match(view.notice.textContent, /Tokens \(max_tokens\): received 10,000,000; allowed integer range is 1–2,000,000/);
  assert.match(view.notice.textContent, /Seconds \(max_duration_seconds\): received 7,200; allowed integer range is 1–3,600/);
  assert.equal(view.budgetFields.max_tokens.getAttribute('aria-invalid'), 'true');
  assert.equal(document.activeElement, view.budgetFields.max_tokens);
  view.job = job; view.render(job);
  assert.equal(view.notice.hidden, false); // Polling another diagnosis must not erase a submission error.
  assert.match(view.notice.textContent, /Tokens \(max_tokens\)/);
  await view.command('resume'); assert.equal(sent, 0);
  view.setBudgets(budgets); view.budgetFields.max_steps.value = '1.5';
  await view.command('start'); assert.equal(sent, 0);
  assert.match(view.notice.textContent, /Steps \(max_steps\).*1.5/);
  view.setBudgets(budgets); await view.command('start'); assert.equal(sent, 1);
  assert.equal(view.budgetFields.max_tokens.hasAttribute('aria-invalid'), false);
  view.close(); dom.window.close();
});

test('diagnosis shows token threshold and preserves the trigger values after summary usage', async () => {
  const { dom, root } = setup();
  const stopped = { ...job, state: 'budget_exhausted', counters: { tokens: 235548 },
    token_budget: { used: 235548, limit: 300000, wrap_up_at: 230000, reserve_tokens: 70000, last_call_tokens: 35000 },
    wrap_up: { token_budget: { used: 228000, limit: 300000, wrap_up_at: 209162, reserve_tokens: 90838, last_call_tokens: 45419 } },
  };
  const view = new DiagnosisView(root);
  await view.open({ ...api(), diagnoses: async () => ({ runs: [], models: [], budgets, jobs: [stopped] }), diagnosis: async () => stopped }, 'target');
  assert.match(view.status.textContent, /235,548 \/ 300,000 tokens/);
  assert.match(view.status.textContent, /Token wrap-up at 209,162/);
  assert.match(view.status.textContent, /90,838 reserved/);
  assert.equal(view.resume.disabled, false);
  assert.match(view.history.selectedOptions[0].textContent, /read-only · diag1/);
  view.close(); dom.window.close();
});
