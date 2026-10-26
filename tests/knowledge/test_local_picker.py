import subprocess
from types import SimpleNamespace

import pytest

from loom.knowledge import local_picker
from loom.service.contracts import ServiceError


@pytest.fixture
def store():
    return SimpleNamespace(get=lambda _: {'engine': 'sqlite_fts'})


@pytest.mark.parametrize('kind', ['file', 'folder'])
def test_native_picker_returns_absolute_path_and_keeps_paths_out_of_script(tmp_path, monkeypatch, store, kind):
    monkeypatch.setattr(local_picker.sys, 'platform', 'darwin')
    chosen = tmp_path / '中文 "quoted" $(noop)'
    chosen.mkdir() if kind == 'folder' else chosen.touch()
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=str(chosen) + '\n')
    monkeypatch.setattr(local_picker.subprocess, 'run', run)
    assert local_picker.pick_path(store, 'kb', {'kind': kind, 'path': str(chosen)}) == {'path': str(chosen)}
    command, options = calls[0]
    assert command[:2] == ['/usr/bin/osascript', '-e']
    assert str(chosen) not in command[2]
    assert command[3:] == [kind, str(chosen if kind == 'folder' else tmp_path)]
    assert options['timeout'] == 180 and 'shell' not in options


def test_cancel_timeout_and_error_release_picker_lock(tmp_path, monkeypatch, store):
    monkeypatch.setattr(local_picker, '_command', lambda *_: ['picker'])
    payload = {'kind': 'file', 'path': str(tmp_path)}
    monkeypatch.setattr(local_picker.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='\n'))
    assert local_picker.pick_path(store, 'kb', payload) == {'path': None}
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired('picker', 180)
    monkeypatch.setattr(local_picker.subprocess, 'run', timeout)
    with pytest.raises(ServiceError) as failure:
        local_picker.pick_path(store, 'kb', payload)
    assert failure.value.status == 408 and not local_picker._PICKER_LOCK.locked()
    monkeypatch.setattr(local_picker.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=2, stdout='', stderr='error'))
    with pytest.raises(ServiceError) as failure:
        local_picker.pick_path(store, 'kb', payload)
    assert failure.value.status == 503 and not local_picker._PICKER_LOCK.locked()
    local_picker._PICKER_LOCK.acquire()
    try:
        with pytest.raises(ServiceError) as busy:
            local_picker.pick_path(store, 'kb', payload)
        assert busy.value.status == 409
    finally:
        local_picker._PICKER_LOCK.release()


@pytest.mark.parametrize('payload', [[], {'kind': 'web'}, {'kind': 'file', 'path': None}, {'kind': 'file', 'path': 'relative'},
                                    {'kind': 'file', 'path': '\x00'}, {'kind': 'folder', 'extra': True}])
def test_invalid_selection_does_not_open_dialog(store, payload, monkeypatch):
    monkeypatch.setattr(local_picker.subprocess, 'run', lambda *a, **k: pytest.fail('must not launch'))
    with pytest.raises(ServiceError):
        local_picker.pick_path(store, 'kb', payload)


def test_linux_chooser_and_headless_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(local_picker.sys, 'platform', 'linux')
    monkeypatch.setenv('DISPLAY', ':0')
    monkeypatch.setattr(local_picker.shutil, 'which', lambda name: '/usr/bin/' + name)
    command = local_picker._command('folder', tmp_path)
    assert command[0] == '/usr/bin/zenity' and '--directory' in command
    monkeypatch.delenv('DISPLAY')
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    with pytest.raises(ServiceError, match='unavailable'):
        local_picker._command('file', tmp_path)
