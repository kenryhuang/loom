"""Native desktop selection returning original paths, without uploading copies."""

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from loom.service.contracts import ServiceError, object_value

_PICKER_LOCK = threading.Lock()
# Paths are passed as argv, never interpolated into AppleScript or shell code.
# https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/PromptforaFileorFolder.html
_SCRIPT = '''on run argv
    set startFolder to (POSIX file (item 2 of argv)) as alias
    try
        activate
        if item 1 of argv is "folder" then
            set picked to choose folder with prompt "Choose a folder to import into Loom" default location startFolder
        else
            set picked to choose file with prompt "Choose a file to import into Loom" default location startFolder
        end if
        return POSIX path of picked
    on error number -128
        return ""
    end try
end run'''


def _command(kind, initial):
    if sys.platform == 'darwin':
        return ['/usr/bin/osascript', '-e', _SCRIPT, kind, str(initial)]
    if sys.platform.startswith('linux') and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        if executable := shutil.which('zenity'):
            return [executable, '--file-selection', '--title=Choose a local ' + kind, '--filename=' + str(initial) + '/',
                    *(['--directory'] if kind == 'folder' else [])]
        if executable := shutil.which('kdialog'):
            return [executable, '--getexistingdirectory' if kind == 'folder' else '--getopenfilename', str(initial),
                    '--title', 'Choose a local ' + kind]
    raise ServiceError('The native file chooser is unavailable on this service machine. Enter the path manually.', 503)


def pick_path(store, kb_id, payload):
    payload = object_value(payload)
    if set(payload) - {'kind', 'path'} or payload.get('kind') not in {'file', 'folder'}:
        raise ServiceError('Path selection requires kind (file/folder) and optional path')
    raw = payload.get('path', '')
    if not isinstance(raw, str) or len(raw) > 4096 or '\x00' in raw:
        raise ServiceError('Invalid initial path')
    store.get(kb_id)
    initial = Path(raw.strip()).expanduser() if raw.strip() else Path.home()
    if not initial.is_absolute():
        raise ServiceError('Enter an absolute initial path')
    if not initial.is_dir():
        initial = initial.parent if initial.parent.is_dir() else Path.home()
    command = _command(payload['kind'], initial)
    if not _PICKER_LOCK.acquire(blocking=False):
        raise ServiceError('A file chooser is already open. Finish or cancel that selection first.', 409)
    try:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ServiceError('File selection timed out. Choose again or enter the path manually.', 408) from exc
        except OSError as exc:
            raise ServiceError('Cannot open the native file chooser. Enter the path manually.', 503) from exc
        if sys.platform.startswith('linux') and result.returncode == 1 and not result.stdout and not result.stderr:
            return {'path': None}
        if result.returncode:
            raise ServiceError('Cannot open the native file chooser. Enter the path manually.', 503)
        # Remove the protocol newline only; spaces are legal in filenames.
        value = result.stdout.removesuffix('\n')
        if not value:
            return {'path': None}
        chosen = Path(value)
        if not chosen.is_absolute() or not (chosen.is_dir() if payload['kind'] == 'folder' else chosen.is_file()):
            raise ServiceError('The selected path is no longer available or has the wrong type')
        return {'path': str(chosen)}
    finally:
        _PICKER_LOCK.release()
