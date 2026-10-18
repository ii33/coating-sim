"""Explicitly scoped JSON state; cooperating writers use OS locks and revision CAS.

Lock files are intentionally retained: unlinking them can split concurrent locks.
No home directory, installation state, or caller-owned TASKS.md is accessed.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import stat
import sys

from . import ToolkitError
from .fs import atomic_write, read_bytes, digest
from .policy import resolve_path, pinned_parent, check_cancelled

TEXT = {'type': 'string', 'maxLength': 4000}
IDENTIFIER = {'type': 'string', 'minLength': 1, 'maxLength': 120}
SCOPE_PROPERTIES = {
    'state_root': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
    'project_id': IDENTIFIER,
    'scope': {'type': 'string', 'enum': ['project', 'session']},
    'session_id': IDENTIFIER,
}
SCOPE_REQUIRED = ['state_root', 'project_id', 'scope']
REVISION = {'type': 'integer', 'minimum': 0}


def location(ctx, args, kind):
    scope = {'project_id': args['project_id'], 'scope': args['scope']}
    if args['scope'] == 'session':
        if not args.get('session_id'):
            raise ToolkitError('invalid_arguments', 'Session scope requires session_id')
        scope['session_id'] = args['session_id']
    elif 'session_id' in args:
        raise ToolkitError('invalid_arguments', 'Project scope must not include session_id')
    root = resolve_path(ctx, args['state_root'])
    if not root.is_dir():
        raise ToolkitError('path_denied', 'state_root must be an existing directory')
    key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    return resolve_path(ctx, root / f'.toolkit-{kind}-{key}.json'), scope


def load(ctx, path, scope, kind):
    try:
        raw = read_bytes(ctx, path)
    except FileNotFoundError:
        return None, ''
    try:
        value = json.loads(raw)
        valid = (isinstance(value, dict) and set(value) == {'version', 'kind', 'scope', 'revision', 'data'}
                 and type(value['version']) is int and value['version'] == 1
                 and value['kind'] == kind and value['scope'] == scope
                 and type(value['revision']) is int and value['revision'] >= 1
                 and isinstance(value['data'], dict))
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise ToolkitError('invalid_state', 'Unrecognized state; existing file was preserved')
    return value, digest(raw)


@contextmanager
def locked(ctx, path):
    lock = resolve_path(ctx, str(path) + '.lock')
    with pinned_parent(ctx, lock) as (parent, target):
        fd = os.open(target.name if parent is not None else target,
                     os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0), 0o600, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ToolkitError('path_denied', 'Lock must be a regular file')
            try:
                if sys.platform == 'win32':
                    import msvcrt
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b'0')
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ToolkitError('conflict', 'State is busy; reread and retry') from exc
            check_cancelled(ctx)
            yield
        finally:
            os.close(fd)


def mutate(ctx, args, kind, update):
    path, scope = location(ctx, args, kind)
    with locked(ctx, path):
        current, expected_hash = load(ctx, path, scope, kind)
        revision = current['revision'] if current else 0
        if args['expected_revision'] != revision:
            raise ToolkitError('conflict', 'State revision changed; reread before updating')
        data = update(current['data'] if current else None)
        result = {'version': 1, 'kind': kind, 'scope': scope,
                  'revision': revision + 1, 'data': data}
        atomic_write(ctx, path, json.dumps(result, ensure_ascii=False, indent=2,
                                         allow_nan=False).encode('utf-8'), expected_hash)
    return result


def schema(extra=None, required=(), mutation=False):
    props = dict(SCOPE_PROPERTIES)
    props.update(extra or {})
    required = SCOPE_REQUIRED + list(required)
    if mutation:
        props['expected_revision'] = REVISION
        required.append('expected_revision')
    return {'type': 'object', 'properties': props, 'required': required}
