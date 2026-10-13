"""Path-referenced local imports. Original files are never copied or removed."""

import hashlib
import os
from pathlib import Path

from loom.service.contracts import ServiceError

TEXT = {'.txt', '.md', '.markdown', '.rst', '.csv', '.json', '.py', '.js', '.ts', '.tsx', '.yaml', '.yml', '.toml', '.html', '.log'}
BINARY = {'.pdf', '.docx', '.pptx', '.xlsx'}
EXCLUDE = {'.git', '.worktrees', '.venv', 'node_modules', '__pycache__'}


def paths(store, kb_id, payload):
    if not isinstance(payload, dict) or set(payload) != {'path', 'kind'} or payload['kind'] not in {'file', 'folder'}:
        raise ServiceError('Local import requires path and kind (file/folder)')
    if not isinstance(payload['path'], str) or not payload['path'].strip():
        raise ServiceError('Enter an absolute path on the Loom service machine')
    root = Path(payload['path']).expanduser()
    if not root.is_absolute():
        raise ServiceError('Local import path must be absolute')
    root = root.resolve()
    extensions = TEXT | (BINARY if store.get(kb_id)['engine'] == 'yakdb_local' else set())
    if payload['kind'] == 'file':
        files = [root] if root.is_file() else []
    else:
        if not root.is_dir():
            raise ServiceError('Folder does not exist')
        files = []
        for directory, names, entries in os.walk(root, followlinks=False):
            names[:] = sorted(n for n in names if n not in EXCLUDE and not n.startswith('.'))
            for name in sorted(entries):
                path = Path(directory) / name
                if not name.startswith('.') and not path.is_symlink() and path.suffix.lower() in extensions:
                    files.append(path)
                    if len(files) > 1000:
                        raise ServiceError('Folder import supports up to 1000 files; choose a narrower folder', 413)
    if not files:
        raise ServiceError('No supported files found at this path')
    for path in files:
        if path.suffix.lower() not in extensions:
            raise ServiceError('Unsupported file type: ' + path.suffix)
    return {'files': [str(p) for p in files], 'root': str(root), 'completed': {}}


def run(store, kb_id, payload, progress, cancelled, checkpoint):
    completed = payload.setdefault('completed', {})
    total = len(payload['files'])
    for index, source in enumerate(payload['files']):
        if cancelled():
            raise ServiceError('Import interrupted', 409)
        path = Path(source)
        limit = 20 * 1024 * 1024 if store.get(kb_id)['engine'] == 'yakdb_local' else 500000
        if not path.is_file():
            raise ServiceError('Source file no longer exists: ' + source)
        if path.stat().st_size > limit:
            raise ServiceError(f'{source} exceeds the {limit:,}-byte limit for this engine', 413)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        progress(stage='indexing', current_document=source, indexed=index, total=total)
        if completed.get(source) == digest:
            continue
        # Full path identity prevents same-name files from replacing each other.
        name = path.stem + ' · ' + hashlib.sha256(source.encode()).hexdigest()[:12] + path.suffix
        def file_progress(_source=source, _index=index, **value):
            # Engine counters describe chunks or a single-document generation,
            # while folder coverage must always count completed source files.
            for key in ('indexed', 'total'):
                if key in value:
                    value['current_document_' + key] = value.pop(key)
            value.update(current_document=_source, indexed=_index, total=total)
            progress(**value)
        if store.get(kb_id)['engine'] == 'yakdb_local':
            result = store.index(kb_id, {'name': name, 'source_path': source}, progress=file_progress, cancelled=cancelled)
        else:
            try:
                content = raw.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise ServiceError('Source is not UTF-8 text: ' + source) from exc
            result = store.index(kb_id, {'name': name, 'content': content}, progress=file_progress, cancelled=cancelled)
        with store.connect() as db:
            db.execute('INSERT OR REPLACE INTO local_sources VALUES(?,?,?,?)', (result['document_id'], kb_id, source, digest))
            if store.get(kb_id)['engine'] != 'yakdb_local':
                db.execute("UPDATE documents SET content='' WHERE id=?", (result['document_id'],))
        completed[source] = digest
        checkpoint(payload)
    progress(stage='completed', indexed=total, total=total)
    return {'documents': total, 'chunks': store.get(kb_id)['chunk_count']}
