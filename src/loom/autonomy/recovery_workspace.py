"""Copy the current working tree (including uncommitted content) for verification."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from loom.service.contracts import ServiceError


def input_manifest(root, exclude=()):
    """Tracked and nonignored files are validation inputs; ignored build outputs may change."""
    root = Path(root).resolve()
    if (root / '.git').is_dir():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                                env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}, capture_output=True, check=True)
        paths = sorted(set(os.fsdecode(p) for p in result.stdout.split(b'\0') if p))
    else:
        paths = sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file())
    manifest = {}
    for name in paths:
        path = root / name
        if any(path.resolve().is_relative_to(p) for p in exclude):
            continue
        if path.is_symlink():
            manifest[name] = 'link:' + os.readlink(path)
        elif path.is_file():
            with path.open('rb') as stream:
                manifest[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
        else:
            manifest[name] = None
    return manifest


def tree_digest(root, *, exclude=()):
    root = Path(root).resolve()
    h = hashlib.sha256()
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if Path(directory, d).resolve() not in exclude)
        for name in sorted([*files, *(d for d in dirs if Path(directory, d).is_symlink())]):
            path = Path(directory, name)
            relative = str(path.relative_to(root))
            h.update(relative.encode() + b'\0')
            if path.is_symlink():
                if not path.resolve().is_relative_to(root):
                    raise ServiceError('独立验证副本不支持指向工作区外的符号链接：' + relative, 409, code='EXECUTION_CONTRACT')
                h.update(b'link:' + os.readlink(path).encode())
            else:
                if not path.is_file():
                    raise ServiceError('工作区包含不可复制的特殊文件：' + relative, 409, code='EXECUTION_CONTRACT')
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        h.update(chunk)
    return h.hexdigest()


def copy_workspace(data_dir, source, identity):
    source, data_dir = Path(source).resolve(), Path(data_dir).resolve()
    if source == data_dir or source.is_relative_to(data_dir):
        raise ServiceError('服务数据目录不能作为自动验证副本的源目录', 409, code='EXECUTION_CONTRACT')
    # A test/workspace may contain the service directory; never recursively copy it.
    excluded = (data_dir,) if data_dir.is_relative_to(source) else ()
    target = data_dir / 'recovery_workspaces' / identity
    receipt = target.parent / (identity + '.json')
    before = tree_digest(source, exclude=excluded)
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved['source_digest'] == before and target.is_dir() and input_manifest(target) == saved['inputs']:
            return saved
        raise ServiceError('恢复副本对应的原始工作区已变化，需要重新评估', 409, code='EXECUTION_CONTRACT')
    if (source / '.git').is_file():
        raise ServiceError('链接式 Git worktree 需要独立验证环境，不能复制其外部 Git 元数据', 409, code='EXECUTION_CONTRACT')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)  # Only this recovery's incomplete, never-dispatched copy.
    shutil.copytree(source, target, symlinks=True,
                    ignore=lambda directory, names: [n for n in names if Path(directory, n).resolve() in excluded])
    after = tree_digest(source, exclude=excluded)
    if before != after or tree_digest(target) != before:
        raise ServiceError('复制期间源文件变化，验证副本未提交', 409, code='EXECUTION_CONTRACT')
    inputs = input_manifest(source, excluded)
    if inputs != input_manifest(target):
        raise ServiceError('验证副本的输入清单与源工作区不一致', 409, code='EXECUTION_CONTRACT')
    saved = dict(source=str(source), path=str(target), source_digest=before, inputs=inputs, excluded=[str(p) for p in excluded])
    receipt.write_text(json.dumps(saved))
    return saved


def preservation_evidence(copy):
    current = tree_digest(copy['source'], exclude=tuple(Path(p) for p in copy.get('excluded', [])))
    inputs_unchanged = input_manifest(copy['path']) == copy['inputs']
    return dict(id='source-preservation', source=copy['source'], source_digest=copy['source_digest'], current_digest=current,
                unchanged=current == copy['source_digest'] and inputs_unchanged, inputs_unchanged=inputs_unchanged, execution_workspace=copy['path'],
                meaning='Original contents preserved, including pre-existing uncommitted changes. Clean Git status is not asserted.')
