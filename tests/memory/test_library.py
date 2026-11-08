import json
from concurrent.futures import ThreadPoolExecutor

from loom.autonomy.store import AgentStore
from loom.memory.library import MemoryLibraryCache
from loom.memory.store import MemoryStore
from loom.service.store import SessionStore
from tests.memory.helpers import seed


def test_library_cache_reuses_scoped_rows_and_invalidates_on_revision(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / 'data')
    AgentStore(store)
    sid = seed(store, tmp_path / 'project')
    seed(store, tmp_path / 'other', evaluation='private')
    memory = MemoryStore(store.path, workspace=tmp_path / 'project')
    cache = MemoryLibraryCache()
    original = MemoryStore._records
    scans = []

    def scan(self, *args, **kwargs):
        scans.append(self.workspace)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryStore, '_records', scan)
    first = memory.browse(cache=cache)
    first['records'][0]['citations'].clear()
    first['records'][0]['source']['annotation_revision'] = 99
    second = MemoryStore(store.path, workspace=tmp_path / 'project').browse(kind='facts', cache=cache)
    assert second['records'][0]['citations']
    assert 'annotation_revision' not in second['records'][0]['source']
    assert not memory.browse(review='reviewed', cache=cache)['records']
    assert memory.browse(offset=20, cache=cache)['total'] == 1
    assert len(scans) == 1
    private = MemoryStore(store.path, workspace=tmp_path / 'other').browse(cache=cache)
    assert private['records'][0]['source']['evaluation_id'] == 'private'
    assert len(scans) == 2
    # Publication changes invalidate the same version protocol used by runtime retrieval.
    with store.transaction() as db:
        job = json.loads(db.execute("SELECT body FROM semantic_jobs WHERE id='eval-memory'").fetchone()[0])
        job['checkpoint']['memory']['records']['memory:planet']['summary'] = 'New evidence'
        db.execute("UPDATE semantic_jobs SET body=? WHERE id='eval-memory'", (json.dumps(job),))
        db.execute("UPDATE memory_versions SET value=value+1 WHERE id='effective'")
    assert memory.browse(query='evidence', cache=cache)['records'][0]['summary'] == 'New evidence'
    assert len(scans) == 3
    store.update(sid, lambda state, emit, db: state.update(deleted_at='now'))
    assert memory.browse(cache=cache)['total'] == 0
    assert len(scans) == 4


def test_concurrent_filters_share_one_scan_and_cache_is_bounded(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / 'data')
    AgentStore(store)
    seed(store, tmp_path)
    memory = MemoryStore(store.path, workspace=tmp_path)
    original = memory._records
    scans = []

    def scan(**kwargs):
        scans.append(True)
        return original(**kwargs)

    monkeypatch.setattr(memory, '_records', scan)
    cache = MemoryLibraryCache()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: memory.browse(cache=cache), range(4)))
    assert len(scans) == 1 and all(r['total'] == 1 for r in results)
    too_small = MemoryLibraryCache(max_bytes=1)
    memory.browse(cache=too_small)
    memory.browse(cache=too_small)
    assert len(scans) == 3 and not too_small.entries and too_small.size == 0


def test_unversioned_database_never_caches_results(tmp_path):
    store = SessionStore(tmp_path / 'data')
    seed(store, tmp_path)
    memory = MemoryStore(store.path, workspace=tmp_path)
    cache = MemoryLibraryCache()
    assert memory.browse(cache=cache)['total'] == 1
    with store.transaction() as db:
        db.execute('DELETE FROM semantic_jobs')
    assert memory.browse(cache=cache)['total'] == 0
    assert not cache.entries
