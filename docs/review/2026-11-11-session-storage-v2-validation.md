# Session storage v2 validation

Design: [storage and migration](../design/2026-11-11-session-storage-v2.md).

## Historical copy

Source: `runs/service` (original supervisor left running; this task did not migrate or write the source database).
Destination: `runs/service-session-v2-20261111`.

The SQLite backup API captured the live WAL database. The untouched backup is
`service.before-session-v2.sqlite` inside the destination. APFS clones were used
for the working database and immutable artifact files. Auxiliary stores and their
SQLite databases, diagnostic/recovery files and the local credential were copied.
No supervisor was started on the copy; running task state was preserved, not recovered.

Verification completed:

| Data | Preserved count |
| --- | ---: |
| Session snapshots (exact decoded equality, including deleted sessions) | 60 |
| Messages | 414 |
| Events | 1,479,690 |
| Commands | 373 |
| Operations | 6,306 |
| Worker request receipts | 825,830 |
| Artifact references | 52,387 |
| Distinct artifact files checked against SHA-256 | 51,866 |

`PRAGMA integrity_check` returned `ok`. No artifact errors or preexisting cursor
mismatches were found. The aggregate canonical snapshot SHA-256 is
`9ab495ca891ab5cf7fa7bd4e17a87e8ec65586739c65ec4641717269f7f1ea82`.

Index construction and migration took about 208 seconds on this machine; migration
plus database verification took about 421 seconds. Artifact verification was done
subsequently. Reports are in the destination as `session-migration-report.json` and
`session-*-performance-report.json` / `session-performance-report.json`.

## Measurements

Read measurements used the same frozen historical database, nine repetitions and
median wall time. Tests/validation were running concurrently, so these are comparative
local measurements, not production latency guarantees. The largest nondeleted
snapshot used for the empty-event check was 1,902,276 UTF-8 bytes.

| Operation | Before | After |
| --- | ---: | ---: |
| Catalog first 50 sessions | 96.565 ms | 14.502 ms |
| Poll events with no new events | 27.135 ms | 3.309 ms |
| Select 152 process events from a session with 319,087 events (5 repetitions) | 16,179.905 ms | 2.493 ms |
| Commit 64 delta events, synthetic 1 MiB context + 64 KiB stream | 22.683 ms | 19.731 ms |
| Scalar update, synthetic 1 MiB context + 64 KiB stream | 14.539 ms | 2.896 ms |
| Append a 100-character message, same synthetic snapshot | 20.371 ms | 4.955 ms |
| WAL growth for the first message append | 1,157,752 bytes | 74,192 bytes |
| WAL growth for first scalar update, reader kept open | 4,152 bytes | 4,152 bytes |

The write benchmarks commit changes using the original HEAD `SessionStore` and the
final implementation; both have the same seeded data and existing dirty markers. SQLite can avoid writing unchanged pages even when
a complete JSON value is replaced; serialization of the entire snapshot was still
required before this change. WAL bytes depend on field size changes and page layout.
The new implementation avoids fetching/serializing cold fields in the first place.

## Reproduction and deployment

Create a fresh copy (destination must not exist):

```sh
.venv/bin/python -m loom.service.session_migration \
  --source runs/service --destination runs/service-session-v2-new
```

Keep the original service on its original directory until a deliberate cutover.
The migrated directory represents the backup point, not writes made later to the
live original. For a current cutover, stop the old supervisor and make a fresh copy,
then start the new supervisor against that verified copy. Startup also supports
transactional in-place upgrade after creating a pre-v2 backup, and refuses to upgrade
a legacy database while its old supervisor holds the ownership lock. Never run an
old binary against the new schema or overwrite a live SQLite main file without its WAL.

## Regression checks

- Focused controller, acceptance, worker transport, visual artifact, storage,
  writer/savepoint and API regression run: 72 passed.
- Follow-up storage/migration/visual tests after reducing metadata updates: 32 passed;
  live-supervisor migration refusal and read-only HTTP/cursor checks: 2 passed.
- Autonomy, memory, diagnostics, skills and client suite: 606 passed, 4 failed.
  Three failures in `test_end_to_end.py` and `test_execution_flow.py` were also
  reproduced in an untouched HEAD export (learning result expectations differ).
  The stage end-to-end test hit its 45-second deadline under concurrent validation
  load; an isolated rerun passed. No timeout thresholds were relaxed.
- Browser suite: 306 passed, 3 failed. All three failures (two Agent setup expectations
  and the app smoke test) also reproduce in the untouched HEAD export. The new cursor
  pagination transport test passes separately.
- Ruff on changed Python modules and tests, bytecode compilation and `git diff --check`
  pass. Regression logs were retained in `/tmp/loom-session-v2-*.log`.

Final post-optimization integration run (finite multi-stage Agent execution, acceptance
repair and worker transport): 16 passed. Both cursor transport and old-supervisor
pagination compatibility browser tests passed.
