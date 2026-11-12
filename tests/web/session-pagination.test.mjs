import test from 'node:test';
import assert from 'node:assert/strict';
import { SessionApi } from '../../src/loom/web/assets/api.mjs';

test('session catalog follows opaque cursors without offset scans', async () => {
  const api = new SessionApi('http://localhost', 'token');
  const paths = [];
  api.json = async path => {
    paths.push(path);
    if (paths.length === 1) return { sessions: [{ session_id: 'a' }], next_cursor: 'opaque+/=' };
    return { sessions: [{ session_id: 'b' }], next_cursor: null };
  };
  assert.deepEqual(await api.sessions(), [{ session_id: 'a' }, { session_id: 'b' }]);
  assert.equal(paths[0], '/v1/sessions?limit=200');
  const next = new URL(paths[1], 'http://localhost');
  assert.equal(next.searchParams.get('cursor'), 'opaque+/=');
  assert.equal(next.searchParams.has('offset'), false);
});

test('updated frontend can finish pagination against a supervisor awaiting restart', async () => {
  const api = new SessionApi('http://localhost', 'token');
  const paths = [];
  api.json = async path => {
    paths.push(path);
    return paths.length === 1
      ? { sessions: [{ session_id: 'a' }], next_offset: 200 }
      : { sessions: [{ session_id: 'b' }], next_offset: null };
  };
  assert.equal((await api.sessions()).length, 2);
  assert.equal(new URL(paths[1], 'http://localhost').searchParams.get('offset'), '200');
});
