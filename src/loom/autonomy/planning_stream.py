"""Bounded, durable planning previews; only the planner's validated commit publishes work."""

import logging
import sqlite3
import time
import uuid

from loom.core.sqlite import is_busy
from loom.llm.api import request_llm_response

PREVIEW_CHARS = 6000


class PlanningStream:
    def __init__(self, store, agent_id, run_id, attempt, mode, provider):
        self.store, self.agent_id, self.run_id = store, agent_id, run_id
        self.last_flush = 0
        self.dirty = False
        self.data = {'request_id': uuid.uuid4().hex, 'attempt': attempt, 'mode': mode,
                     'model': getattr(provider, 'model', None), 'streaming': hasattr(provider, 'stream_chat'),
                     'state': 'requesting', 'started_at': store.now(), 'updated_at': store.now(), 'channels': {}}

    def flush(self, *, force=False):
        try:
            self._flush(force=force)
        except sqlite3.OperationalError as exc:
            if not is_busy(exc):
                raise
            # Preview writes must not discard a paid reply or restart a model request.
            logging.getLogger(__name__).warning("Planning preview busy; retaining latest draft in memory: run=%s", self.run_id)

    def _flush(self, *, force=False):
        if not self.run_id or (not force and (not self.dirty or time.monotonic() - self.last_flush < 0.75)):
            return
        self.data['updated_at'] = self.store.now()
        with self.store.sessions.transaction() as db:
            row = db.execute('SELECT 1 FROM agent_runs WHERE id=? AND agent_id=?', (self.run_id, self.agent_id)).fetchone()
            if row is None:
                return
            run = self.store.get(db, 'agent_runs', self.run_id, self.agent_id)
            if run['state'] != 'planning':
                return  # An interrupted/superseded attempt cannot overwrite terminal progress.
            run['stream'] = self.data
            self.store.put(db, 'agent_runs', run)
            self.store.event(db, self.agent_id, 'plan.stream.updated', {
                'agent_run_id': self.run_id, 'request_id': self.data['request_id'], 'state': self.data['state'],
                'attempt': self.data['attempt'], 'characters': {k: v['characters'] for k, v in self.data['channels'].items()},
            })
        self.last_flush, self.dirty = time.monotonic(), False

    async def emit(self, event):
        channel = {'llm.content.delta': 'content', 'llm.reasoning.delta': 'reasoning',
                   'llm.reasoning_context.delta': 'reasoning_context'}.get(event['type'])
        if channel and event.get('delta'):
            value = self.data['channels'].setdefault(channel, {'text': '', 'characters': 0, 'truncated': False})
            value['characters'] += len(event['delta'])
            value['text'] = (value['text'] + event['delta'])[-PREVIEW_CHARS:]
            value['truncated'] = value['characters'] > len(value['text'])
            self.data['state'] = 'streaming'
            first = 'first_output_at' not in self.data
            self.data.setdefault('first_output_at', event.get('at') or self.store.now())
            self.dirty = True
            self.flush(force=first)

    async def request(self, provider, messages):
        self.flush(force=True)
        try:
            result = await request_llm_response(provider, messages, stream=True, emit_event=self.emit)
            self.data['state'] = 'received' if result.ok else 'failed'
            if result.ok:
                # The final response is authoritative, including providers emitting only reasoning deltas.
                if result.value.content:
                    text = result.value.content
                    self.data['channels']['content'] = {'text': text[-PREVIEW_CHARS:], 'characters': len(text),
                                                        'truncated': len(text) > PREVIEW_CHARS}
            else:
                self.data['error'] = result.error.message[:1200]
            return result
        except BaseException as exc:
            self.data.update(state='failed', error=str(exc)[:1200])
            raise
        finally:
            self.data['finished_at'] = self.store.now()
            self.flush(force=True)


async def planning_response(store, agent, run_id, provider, messages, attempt, mode):
    return await PlanningStream(store, agent['id'], run_id, attempt, mode, provider).request(provider, messages)
