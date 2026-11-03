"""Read complete failure evidence for workers predating structured run errors."""

import json


def failure_details(db, state):
    run = state.get('run') or {}
    failure = run.get('failure') or {}
    if isinstance(failure.get('retryable'), bool) or not failure.get('code'):
        return failure
    # Only the last request outcome of this run can supply missing fields. A
    # newer attempt/request or successful response invalidates the old evidence.
    row = db.execute(
        "SELECT body FROM events WHERE session_id=? AND json_extract(body,'$.run_id')=? "
        "AND json_extract(body,'$.type') IN ('llm.failed','llm.requested','llm.completed','run.started') "
        "ORDER BY seq DESC LIMIT 1", (state['session_id'], run.get('id')),
    ).fetchone()
    event = json.loads(row[0]) if row else {}
    error = event.get('payload', {}).get('error') or {}
    if event.get('type') != 'llm.failed' or error.get('code') != failure['code'] or type(error.get('retryable')) is not bool:
        return failure
    return {**error, **failure, 'retryable': error['retryable'], 'source_event_seq': event['seq']}
