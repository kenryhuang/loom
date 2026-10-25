"""Atomic, revision-checked editing of the service's YAML configuration."""

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

from loom.service.contracts import ServiceError
from loom.tasks.config import _parse_task_config, parse_yaml_document, provider_config_document

LOCK = threading.RLock()
MASK = '••••••••'
SECRET_KEYS = {'api_key', 'password', 'secret', 'token', 'authorization'}


def mask(value):
    if isinstance(value, dict):
        return {k: MASK if k.lower() in SECRET_KEYS and v else mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def restore(value, old):
    if value == MASK:
        if old is None:
            raise ServiceError('A masked secret cannot be used as a new value')
        return old
    if isinstance(value, dict):
        return {k: restore(v, old.get(k) if isinstance(old, dict) else None) for k, v in value.items()}
    if isinstance(value, list):
        rows = old if isinstance(old, list) else []
        previous = {r['id']: r for r in rows if isinstance(r, dict) and 'id' in r}
        return [restore(v, previous.get(v['id']) if isinstance(v, dict) and 'id' in v else rows[i] if i < len(rows) else None)
                for i, v in enumerate(value)]
    return value


def yaml_text(value, indent=0):
    """Emit the JSON-compatible YAML subset supported by Loom's bundled parser."""
    lines = []
    rows = value.items() if isinstance(value, dict) else enumerate(value)
    for key, child in rows:
        yaml_key = json.dumps(str(key), ensure_ascii=False) if any(c in str(key) for c in ':#\n"') else str(key)
        prefix = ' ' * indent + (yaml_key + ':' if isinstance(value, dict) else '-')
        if isinstance(child, (dict, list)) and child:
            lines.extend([prefix, yaml_text(child, indent + 2)])
        else:
            lines.append(prefix + ' ' + json.dumps(child, ensure_ascii=False, allow_nan=False))
    return '\n'.join(lines)


class Settings:
    def __init__(self, service):
        self.service = service
        self.path = Path(service.config_path or Path.cwd() / 'config.yaml').resolve()

    def read(self):
        raw = self.path.read_text() if self.path.exists() else ''
        parsed = parse_yaml_document(raw, self.path)
        if not parsed.ok:
            raise ServiceError('Cannot parse configuration: ' + parsed.error.message)
        try:
            data = provider_config_document(parsed.value or {})
            # Retired task defaults are omitted from the editor and removed on save.
            data.pop("task", None)
            return raw, data
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc

    def get(self):
        with LOCK:
            raw, data = self.read()
            data.setdefault('embedding_profiles', self.service.store.knowledge.profiles())
            return {'path': str(self.path), 'revision': hashlib.sha256(raw.encode()).hexdigest(), 'config': mask(data)}

    def save(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'revision', 'config'} or not isinstance(payload['config'], dict):
            raise ServiceError('Settings requires revision and config')
        with LOCK:
            raw, old = self.read()
            if payload['revision'] != hashlib.sha256(raw.encode()).hexdigest():
                raise ServiceError('Configuration changed; reload settings before saving', 409)
            try:
                data = restore(provider_config_document(payload['config']), old)
            except ValueError as exc:
                raise ServiceError(str(exc)) from exc
            parsed = _parse_task_config(data, self.path)
            if not parsed.ok:
                raise ServiceError(parsed.error.message)
            if data.get('default_model') and data['default_model'] not in parsed.value.models:
                raise ServiceError('default_model must name a configured model')
            if data.get('meta_harness'):
                from loom.optimize.config import _parse_meta

                if not isinstance(data['meta_harness'], dict):
                    raise ServiceError('meta_harness must be a configuration group')
                try:
                    _parse_meta(data['meta_harness'], parsed.value.models)
                except (TypeError, ValueError, ArithmeticError) as exc:
                    raise ServiceError(f"{getattr(exc, 'field', 'meta_harness')}: {exc}") from exc
            from loom.knowledge.store import validate_profile
            profiles = data.get('embedding_profiles', [])
            if not isinstance(profiles, list):
                raise ServiceError('embedding_profiles must be an array')
            ids = set()
            previous_profiles = {p['id']: p for p in self.service.store.knowledge.profiles()}
            used_profiles = {b.get('embedding_profile_id') for b in self.service.store.knowledge.list()}
            for profile in profiles:
                if not isinstance(profile, dict) or not isinstance(profile.get('id'), str) or not profile['id'] or profile['id'] in ids:
                    raise ServiceError('Embedding profiles need unique nonempty IDs')
                validate_profile({k: v for k, v in profile.items() if k != 'id'})
                ids.add(profile['id'])
                previous = previous_profiles.get(profile['id'])
                if previous and profile['id'] in used_profiles and any(profile.get(k) != previous.get(k) for k in ('endpoint', 'model')):
                    raise ServiceError('An indexed embedding service cannot change endpoint/model; add a new service and rebuild the index', 409)
            if (used_profiles - {None}) - ids:
                raise ServiceError('Cannot remove an embedding service used by a knowledge base', 409)
            content = yaml_text(data) + '\n'
            check = parse_yaml_document(content, self.path)
            if not check.ok or check.value != data:
                raise ServiceError('Configuration cannot be represented in supported YAML')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.loom-config-', suffix='.yaml')
            try:
                with os.fdopen(fd, 'w') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(name, self.path.stat().st_mode & 0o777 if self.path.exists() else 0o600)
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)
            self.service.config_path = self.path
            self.service.store.knowledge.config_path = self.path
            return self.get()
