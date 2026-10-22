# Visual observation and acceptance implementation

Scope: implement the accepted Web/Canvas/WebGL design in
`docs/superpowers/specs/2026-10-22-visual-observation-and-acceptance-design.zh-CN.md`.
Preferred provider: dashscope/qwen3.8-omni-flash, JSON Mode with local validation.

- [x] Binary artifacts, bounded worker transfer, authenticated media delivery.
- [x] Typed image messages, provider capabilities and configuration/settings.
- [x] Managed browser worker, structured scenes/actions, WebGL multi-frame evidence.
- [x] Visual criteria, independent image judgment, evidence/version binding and budgets.
- [x] Legacy unresolved-method migration, recovery and informative blocking.
- [x] Web evidence viewer, capability recommendations and documentation.
- [x] Regression, real browser DOM/WebGL, configured provider image smoke.

Do not manually mark the historical session passed or edit its database. Preserve genuine
external approval requirements. Ordinary text/JSON paths remain compatible.

Delivered: see `docs/visual-observation-and-acceptance.zh-CN.md` for the actual v1
configuration, scenario contract, installation and boundaries. Configured the local
image model and restarted the idle service; all 12 sessions were retained. The
solar session subsequently bound browser tools through its normal edit/restart
flow and completed fresh verification; see the final production outcome below.

Validation on 2026-10-22:

- Browser and durable service tests: 16 passed, 2 live tests skipped by default.
- Explicit configured Qwen image smoke: 2 passed, stream and non-stream, real
  red/blue image pair and valid JSON with reported usage.
- Real solar candidate: isolated preview, two 1440×900 Canvas frames, no page
  errors, actual independent Qwen appearance judgment. Ignored local manifest and
  judgment under `.loom/visual-smoke-2026-10-22/`; this is an appearance smoke,
  not proof of the original task's exact object-count criterion.
- Web frontend: 173 passed, including authenticated evidence hydration and finding
  overlays.
- Full Python run: 1817 passed, 8 skipped, 4 failed. Two optimize call-count
  expectations and the source paging fixture (`read_more`) failed identically in
  a clean detached checkout at e8868f6. The fourth was an incomplete package
  allowlist (new browser plus existing diagnostics/external_tools), updated here;
  subsequent package/media-service focused run: 5 passed.
- Ruff on changed/new Python, usage JSON/link validation and `git diff --check`
  passed. Production catalog confirms the browser collection and configured
  visual model after restart.

Restart follow-up: the real acceptance planner filled both `url` and `preview`
because exclusivity was only checked in the domain validator. The wire/native
schema now enforces one non-null target; nullable optional scenario fields are
audited and normalized before local schema validation. Missing preview `{port}`
is rejected during planning, and observation has its own page-ID-only contract.
Added regressions for native target exclusivity, nested null normalization and
required input preservation. A real qwen3.8-max plan for the original task was
accepted in one call and its generated preview produced a WebGL screenshot without
page errors. Resumed the original paused session through the normal command API;
its production plan also completed in one native-schema call and execution began.

Following the live run exposed a second request-boundary issue: free-text visual
observation inherited the configured JSON response format, causing a Dashscope
400 requiring the word `json`. The private observation provider now omits that
format while preserving it for formal judgments. A regression checks the exact
request and immutable shared options; a real saved session screenshot was also
successfully described. Default WebGL acquisitions without explicit captures now
retain two delayed frames rather than only the initial empty buffer. Updated
focused tests: 23 passed, 2 live tests skipped (before adding the third live case);
acceptance/structured/service regression group: 83 passed; opt-in actual provider
suite including text observation: 3 passed.

The formal live judgment then exposed a gate classification bug: a failed visual
criterion deferred the outcome review of successful commands, but those deferred
rows promoted the gate to environment-blocked and skipped model repair. Such rows
are now explicitly deferred and excluded from primary blockers. Real blockers and
unresolved approvals still suspend. Added a full browser/image/command regression
that fails visually, edits the candidate, independently recaptures different image
bytes and passes; also covered true blocked versus failed states. Browser module:
20 passed; acceptance/structured/service group: 83 passed. Original judgments and
screenshots remain in the session; no failed verdict was rewritten to passed.

Final follow-up checks: combined browser/protocol/durable-image suite 26 passed,
3 opt-in cases skipped by default; separate live suite 3 passed. Together with
the 83 acceptance/structured/service cases, 109 relevant regression tests pass.
The original session is resumed and reviewing the model's visual count finding;
the infrastructure checks do not certify the solar candidate itself as accepted.

Budget recovery follow-up: the original acceptance allocation had 6 cumulative
model calls (including two obsolete plan protocol attempts and a pause) and 109.7
seconds; all three candidate checks, including blocked checks, had consumed the
old repair quota. Resume previously replenished only task time, making the
acceptance allocation unrecoverable. Explicit service Resume now carries a durable
one-shot allocation request. The controller archives counters/results, preserves
requirements, pending operations and task-wide usage, then opens a new bounded
allocation. Merely restoring a checkpoint, calling finish or revising a method
cannot renew it. Only conclusive failures consume the candidate repair allowance.
Unspecified visual defaults cover acquisitions/protocol repair and candidate
repair (20 calls/600s for this plan); explicit configured caps remain strict.

Added budget preflight before checks, exact call/time/failure accounting in Web
and TUI, allocation-aware activity grouping, previous-allocation links and pending
labels for deferred judgments. Verification: 10 budget/durable-service tests,
97 acceptance/browser/TUI regressions (3 live cases skipped), and all 174 frontend
tests passed. The real original session resumed through the command API with
0/20 calls, 0/600s and 0/3 failures; 6 prior calls/109.7s were archived and all
359,018 previously used task tokens remained accounted for.

The live run also exposed unsupported visual counting: normal red/grey/blue
planets were called extra bodies without a count oracle. A bounded host probe now
reads an already loaded same-origin React Three Fiber root store. Fresh source-
bound captures retain actual scene geometry, inherited visibility, texture
occurrences, positions, completeness flags and probe hash. The solar scene has
10 sphere meshes: the sun, eight individually textured planets, and an inward
starfield background sphere; there are also eight orbit line meshes. These facts
are supplied alongside actual screenshots and never replace pixel evidence or
hard browser assertions. A fixture verifies duplicate texture counts and hidden
ancestors; the actual solar candidate + Qwen judgment passed with this evidence.

Unusable optional finding coordinates are now omitted with audited original
values while preserving the verdict and observation, avoiding paid retries solely
for overlay formatting. Finally, dynamic final-node handoff uses the current
same-candidate completion receipt, preventing an immediate duplicate visual check;
checkpoint restore and a new finish request still require fresh verification.

Final production outcome: `sess_b5450f9db3be4e5dbe8150768ea82331` reached
`run.state=completed`, `acceptance.state=passed`, all 6 criteria passed through
actual worker verification (events 91972 and 92013), including independent Qwen
image judgment and the final full-goal review. No database acceptance state or
historical failed verdict was rewritten. The record retains 6 prior allocation
calls/109.7s and 6 current calls/137.0s; task-wide paid usage remains cumulative.
