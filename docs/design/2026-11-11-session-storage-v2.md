# Session storage v2

## Layout and invariants

Keep one SQLite WAL database and atomic transactions across commands, events,
operations and session state. SQLite still has a single writer; per-session mutexes
cannot make concurrent SQLite writes safe or faster. Read transactions, notification
conditions and filesystem preparation must not acquire the database writer mutex.

- `session_heads`: explicit indexed catalog/admission fields (`id`, stable creation
  ordinal, timestamps, deletion, task/run state, event cursor, workspace/claims,
  agent binding). No messages, execution context or task specification in this row.
- `session_fields`: JSON values keyed by `(session_id, scope, name)`, with root,
  task and run scopes. Each independently mutable field is a separate row. Thus
  counters, active time, cursor, control, checkpoint reference and stream buffers
  do not rewrite task configuration, context, messages or each other. Large root
  context and workflow values are fetched only when accessed by an update callback.
- `session_messages`: ordered individual messages, with sequence and command indexes;
  snapshot reads only the most recent 200, and a cursor endpoint exposes older pages.
- `events`, `commands`, `operations`, `worker_records`, `artifacts`: preserve existing
  identities and replay semantics. Add indexes for session operations, run/type
  event queries, activity events and process milestones.
- `sessions(id, body)`: compatibility view reconstructing the old complete JSON for
  diagnostics/memory/legacy SQL consumers. Writable compatibility triggers are slow
  paths, never used by live worker updates. No duplicate full snapshot is stored.

Updates use transaction-scoped lazy mappings, compare only fields actually loaded or
assigned, and persist changed fields only. Full snapshots materialize before closing
connections. Deletions, nulls, nested mutation and callback rollback retain their
original semantics. Existing dictionaries passed to `_save` remain supported.

Catalog queries use `(last_activity_at DESC, ordinal DESC)` with an opaque versioned
cursor; offset remains for old clients only. Ordering is live: a session whose activity
changes between pages can move ahead of the cursor. Refresh is required for a fully
current list; this is not a multi-request historical snapshot.

## Locking and artifacts

Keep cross-repository reentrant write transactions/savepoints. Register notifications
as post-commit callbacks deduplicated per session, discard them on savepoint/outer rollback, and wake only
subscribers of the affected session. Each subscription condition is independent of
the database mutex. Read cursor before waiting to avoid a lost wakeup.

Drain worker records outside the controller-wide scheduler lock; reacquire it for
state validation and commit. Control commands can proceed while a checkpoint is staged.

Prepare immutable artifact bytes into a temporary file (including fsync) before the
write transaction. Inside the transaction atomically install the prepared file and
register its reference. Final installation remains serialized with artifact deletion,
so cleanup cannot unlink a file between existence checking and reference insertion.
A crash before registration may leave an unreferenced content-addressed file, never
a committed reference to unfinished data. Transaction-derived fallback artifacts
retain the synchronous path where their bytes cannot be known beforehand.

## Migration and verification

Use SQLite's backup API for a consistent copy including WAL. Never copy only the main
file of a live WAL database. Keep an untouched backup and migrate a separate copy;
copy referenced artifact files with the data directory. Do not start a supervisor on
that copy (which would recover/modify running sessions).

Schema migration is versioned and transactional, preserves creation order, every JSON
field/message/event/command and artifact reference, and checks reconstructed snapshot
equality before dropping the old table. Verify SQLite integrity, table counts, per-session
snapshot equality, event cursors and referenced artifact content. Reopening is
idempotent. Startup of legacy data creates a consistent pre-migration database backup
before migrating; deployment must stop the old supervisor before switching schema.
