"""Bounded text filesystem operations; no encoding guessing for destructive edits."""
from __future__ import annotations
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from .contracts import ToolSpec, ToolkitError
from .policy import (resolve_path, user_path, user_write_path, check_cancelled, check_authorized,
                     remote_path, pinned_parent)

MAX_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * MAX_BYTES
MAX_LINES = 10000
ENCODINGS = ('utf-8', 'utf-16-le', 'utf-16-be', 'gbk', 'gb18030', 'big5', 'latin1')
BOMS = ((codecs.BOM_UTF8, 'utf-8'), (codecs.BOM_UTF16_LE, 'utf-16-le'), (codecs.BOM_UTF16_BE, 'utf-16-be'))
RECYCLE_ID = re.compile(r'^[0-9a-f]{32}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
MAX_DIRECTORY_DEPTH = 32
MAX_RECYCLE_METADATA_BYTES = 16384


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bytes(context, path, limit=MAX_BYTES, *, internal=False):
    if type(limit) is not int or not 0 <= limit <= MAX_ARTIFACT_BYTES:
        raise ToolkitError('limit_exceeded', 'Invalid bounded read budget')
    (resolve_path if internal else user_path)(context, path)
    with pinned_parent(context, path) as (parent_fd, target):
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(target.name if parent_fd is not None else target, flags, dir_fd=parent_fd)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ToolkitError('path_denied', 'Only regular files are readable')
            if os.fstat(stream.fileno()).st_size > limit:
                raise ToolkitError('limit_exceeded', 'File exceeds byte limit')
            raw = stream.read(limit + 1)

    if len(raw) > limit:
        raise ToolkitError('limit_exceeded', 'File exceeds byte limit')
    return raw


def decode(raw, encoding=None):
    bom = b''
    for marker, codec in BOMS:
        if raw.startswith(marker):
            bom, detected = marker, codec
            if encoding and encoding != detected:
                raise ToolkitError('encoding_conflict', 'Encoding contradicts byte-order mark')
            encoding = detected
            break
    encoding = encoding or 'utf-8'
    try:
        text = raw[len(bom):].decode(encoding, errors='strict')
        lossy = False
    except UnicodeDecodeError:
        text = raw[len(bom):].decode(encoding, errors='replace')
        lossy = True
    crlf = text.count('\r\n')
    lf, cr = text.count('\n') - crlf, text.count('\r') - crlf
    kinds = [name for name, count in [('crlf', crlf), ('lf', lf), ('cr', cr)] if count]
    eol = kinds[0] if len(kinds) == 1 else ('mixed' if kinds else 'none')
    return text, encoding, bom, eol, lossy


def atomic_write(context, path, raw, expected_hash=None, *, max_bytes=MAX_BYTES,
                 authorization=None, internal=False):
    target = (resolve_path if internal else user_write_path)(context, path)
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_ARTIFACT_BYTES:
        raise ToolkitError('limit_exceeded', 'Invalid bounded write budget')
    if len(raw) > max_bytes:
        raise ToolkitError('limit_exceeded', 'Output exceeds byte limit')
    if target.exists() and not target.is_file():
        raise ToolkitError('path_denied', 'Target must be a regular file')
    if expected_hash is not None:
        current = digest(read_bytes(context, path, max_bytes, internal=internal)) if target.exists() else ''
        if current != expected_hash:
            raise ToolkitError('conflict', 'File changed since it was read')
    with pinned_parent(context, path) as (parent_fd, target):
        if parent_fd is None:
            fd, temporary = tempfile.mkstemp(prefix='.toolkit-', dir=target.parent)
        else:
            temporary = '.toolkit-' + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            check_cancelled(context)
            (resolve_path if internal else user_write_path)(context, path)
            if expected_hash is not None:
                current = digest(read_bytes(context, path, max_bytes, internal=internal)) if target.exists() else ''
                if current != expected_hash:
                    raise ToolkitError('conflict', 'File changed during write preparation')
            if authorization is not None:
                check_authorized(context, *authorization)
            if not internal:
                user_write_path(context, path)
            if parent_fd is None:
                if target.exists():
                    os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
                os.replace(temporary, target)
            else:
                try:
                    info = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise ToolkitError('path_denied', 'Target must remain a regular file')
                    os.chmod(temporary, stat.S_IMODE(info.st_mode), dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                os.replace(temporary, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass

    return {'path': str(path), 'bytes': len(raw), 'sha256': digest(raw)}


def _remote(context, operation, args):
    parsed = remote_path(args.get('path', ''))
    if parsed is None:
        return None
    alias, path = parsed
    from .remote import checked_remote_call
    return checked_remote_call(context, operation, {**args, 'host': alias, 'path': path})


def read(context, args):
    if remote_path(args['path']):
        return _remote(context, 'read', args)
    user_path(context, args['path'])
    raw = read_bytes(context, args['path'], args.get('max_bytes', MAX_BYTES))
    text, encoding, bom, eol, lossy = decode(raw, args.get('encoding'))
    lines = text.splitlines(keepends=True)
    start, count = args.get('start_line', 1)-1, args.get('max_lines', MAX_LINES)
    return {'path': args['path'], 'text': ''.join(lines[start:start+count]), 'encoding': encoding,
            'bom': bool(bom), 'eol': eol, 'lossy': lossy, 'bytes': len(raw), 'sha256': digest(raw),
            'total_lines': len(lines), 'truncated': start > 0 or start+count < len(lines)}


def write(context, args):
    if remote_path(args['path']):
        return _remote(context, 'write', args)
    path = user_write_path(context, args['path'])
    previous = read_bytes(context, args['path']) if path.exists() else None
    encoding, bom, eol = args.get('encoding', 'utf-8'), b'', 'none'
    if previous is not None:
        _, encoding, bom, eol, lossy = decode(previous, args.get('encoding'))
        if lossy:
            raise ToolkitError('lossy_edit', 'Specify the correct encoding before replacing this file')
        if 'expected_hash' not in args:
            raise ToolkitError('conflict', 'Replacing a file requires expected_hash')
    text = args['text']
    selected = args.get('eol', 'source')
    if selected == 'source':
        selected = eol
    if selected in ('lf', 'crlf', 'cr'):
        text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', {'lf':'\n','crlf':'\r\n','cr':'\r'}[selected])
    if 'bom' in args:
        bom = next((marker for marker, codec in BOMS if codec == encoding), b'') if args['bom'] else b''
        if args['bom'] and not bom:
            raise ToolkitError('invalid_arguments', 'This encoding has no supported BOM')
    return atomic_write(context, args['path'], bom + text.encode(encoding),
                        args.get('expected_hash', '' if previous is None else None),
                        authorization=(FS_TOOLS['write'][0], args))


def edit(context, args):
    if remote_path(args['path']):
        return _remote(context, 'edit', args)
    user_write_path(context, args['path'])
    raw = read_bytes(context, args['path'])
    text, encoding, bom, eol, lossy = decode(raw, args.get('encoding'))
    if lossy:
        raise ToolkitError('lossy_edit', 'Lossy decoding cannot be edited')
    if digest(raw) != args['expected_hash']:
        raise ToolkitError('conflict', 'File changed since it was read')
    count = text.count(args['old'])
    if count != args.get('occurrences', 1):
        raise ToolkitError('match_conflict', 'Exact match count differs from requested occurrences')
    replacement = args['new']
    if eol in ('lf', 'crlf', 'cr'):
        replacement = replacement.replace('\r\n', '\n').replace('\r', '\n').replace('\n', {'lf':'\n','crlf':'\r\n','cr':'\r'}[eol])
    projected_bytes = (len(raw) + count *
                       (len(replacement.encode(encoding)) - len(args['old'].encode(encoding))))
    if projected_bytes > MAX_BYTES:
        raise ToolkitError('limit_exceeded', 'Edited file would exceed byte limit')
    text = text.replace(args['old'], replacement)
    return atomic_write(context, args['path'], bom + text.encode(encoding), args['expected_hash'],
                        authorization=(FS_TOOLS['edit'][0], args))


def list_dir(context, args):
    if remote_path(args['path']):
        return _remote(context, 'list_dir', args)
    target = user_path(context, args['path'])
    limit = args.get('max_entries', 200)
    entries: list[dict[str, str]] = []
    with os.scandir(target) as iterator:
        for entry in iterator:
            check_cancelled(context)
            if entry.name.casefold() == '.toolkit' or entry.name.casefold().startswith('.toolkit-'):
                continue
            if len(entries) >= limit:
                return {'entries': sorted(entries, key=lambda row: row['name']), 'truncated': True}
            info = entry.stat(follow_symlinks=False)
            kind = 'blocked' if entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400 else ('directory' if entry.is_dir(follow_symlinks=False) else 'file')
            entries.append({'name': entry.name, 'kind': kind})
    return {'entries': sorted(entries, key=lambda row: row['name']), 'truncated': False}


def read_many(context, args):
    results, budget = [], args.get('max_bytes', MAX_BYTES)
    for path in args['paths']:
        check_cancelled(context)
        row = read(context, {'path': path, 'max_bytes': budget, 'max_lines': args.get('max_lines', MAX_LINES), **({'encoding': args['encoding']} if 'encoding' in args else {})})
        budget -= row['bytes']
        if budget < 0:
            raise ToolkitError('limit_exceeded', 'Combined files exceed byte limit')
        results.append(row)
    return {'files': results}


def _workspace_root(context):
    if not context.allowed_roots:
        raise ToolkitError('path_denied', 'No permitted filesystem roots')
    root = resolve_path(context, context.allowed_roots[0])
    if not root.is_dir():
        raise ToolkitError('path_denied', 'Workspace must be an existing directory')
    return root


def _workspace_path(context, path):
    target = user_write_path(context, path)
    if not target.is_relative_to(_workspace_root(context)):
        raise ToolkitError('path_denied', 'Recovery operations use the first permitted workspace')
    return target


def _mkdir_one(context, target, authorization, *, public=False):
    with pinned_parent(context, target) as (parent_fd, checked):
        check_authorized(context, *authorization)
        if public:
            user_write_path(context, target)
        try:
            os.mkdir(checked.name if parent_fd is not None else checked, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            info = os.stat(checked.name if parent_fd is not None else checked,
                           dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ToolkitError('path_denied', 'Directory path is occupied by a non-directory') from None
        resolve_path(context, target)


def mkdir(context, args):
    target = user_write_path(context, args['path'])
    root = max((root for root in context.allowed_roots if target.is_relative_to(root)),
               key=lambda item: len(item.parts))
    relative = target.relative_to(root)
    if len(relative.parts) > MAX_DIRECTORY_DEPTH:
        raise ToolkitError('limit_exceeded', 'Directory depth exceeds 32 components')
    if not root.is_dir():
        raise ToolkitError('path_denied', 'Workspace must be an existing directory')
    authorization = (FS_TOOLS['mkdir'][0], args)
    if args.get('parents', False):
        current = root
        for component in relative.parts:
            current = current / component
            user_write_path(context, current)
            _mkdir_one(context, current, authorization, public=True)
    elif relative.parts:
        _mkdir_one(context, target, authorization, public=True)
    return {'path': str(args['path']), 'kind': 'directory'}


def _recycle_directory(context, authorization=None):
    target = _workspace_root(context) / '.toolkit' / 'recycle'
    if authorization is not None:
        _mkdir_one(context, target.parent, authorization)
        _mkdir_one(context, target, authorization)
    return resolve_path(context, target)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sync_recovery_directories(context, entry):
    """Durably order directory entries where POSIX directory fsync is available.

    Windows has no portable stdlib directory-fsync contract. There we guarantee
    recoverability after process interruption, not arbitrary power loss.
    """
    if os.name == 'nt':
        return False
    root = _workspace_root(context)
    for directory in (entry, entry.parent, entry.parent.parent, root):
        with pinned_parent(context, directory) as (parent_fd, target):
            fd = os.open(target.name if parent_fd is not None else target,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return True


def _file_snapshot(context, path):
    """Bounded regular-file snapshot with an identity for the final unlink check."""
    with pinned_parent(context, path) as (parent_fd, target):
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(target.name if parent_fd is not None else target, flags, dir_fd=parent_fd)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ToolkitError('path_denied', 'Only regular files can be recycled; directories are unsupported')
            if before.st_size > MAX_ARTIFACT_BYTES:
                raise ToolkitError('limit_exceeded', 'Recycled file exceeds 32 MiB')
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
            after = os.fstat(stream.fileno())
            if len(raw) > MAX_ARTIFACT_BYTES:
                raise ToolkitError('limit_exceeded', 'Recycled file exceeds 32 MiB')
            if _identity(before) != _identity(after):
                raise ToolkitError('conflict', 'File changed while preparing its recovery snapshot')
    return raw, after


def delete(context, args):
    """Copy before unlink. An interrupted delete may leave a recoverable copy.

    No directory recursion and no purge are provided. External writers still
    require host isolation; path checks are not an OS sandbox or an unlink CAS.
    """
    target = _workspace_path(context, args['path'])
    if not SHA256.fullmatch(args['expected_hash']):
        raise ToolkitError('invalid_arguments', 'expected_hash must be a lowercase SHA-256')
    raw, info = _file_snapshot(context, target)
    expected = digest(raw)
    if expected != args['expected_hash']:
        raise ToolkitError('conflict', 'File changed since it was read')
    authorization = (FS_TOOLS['delete'][0], args)
    recycle = _recycle_directory(context, authorization)
    # Exclusive UUID directories prevent collision overwrites, even if an ID
    # generator is accidentally reused. Never remove an existing entry.
    for _ in range(8):
        recycle_id = uuid.uuid4().hex
        entry = recycle / recycle_id
        with pinned_parent(context, entry) as (parent_fd, checked):
            check_authorized(context, *authorization)
            try:
                os.mkdir(checked.name if parent_fd is not None else checked, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
        break
    else:
        raise ToolkitError('conflict', 'Could not allocate a unique recovery entry')
    metadata = {'version': 1, 'recycle_id': recycle_id,
                'original_path': target.relative_to(_workspace_root(context)).as_posix(),
                'bytes': len(raw), 'sha256': expected, 'mode': stat.S_IMODE(info.st_mode) & 0o777,
                'created_at': datetime.now(timezone.utc).isoformat()}
    atomic_write(context, entry / 'content', raw, '', max_bytes=MAX_ARTIFACT_BYTES,
                 authorization=authorization, internal=True)
    atomic_write(context, entry / 'metadata.json', json.dumps(metadata, ensure_ascii=False).encode(), '',
                 max_bytes=MAX_RECYCLE_METADATA_BYTES, authorization=authorization, internal=True)
    directory_synced = _sync_recovery_directories(context, entry)
    with pinned_parent(context, target) as (parent_fd, checked):
        latest, latest_info = _file_snapshot(context, target)
        if digest(latest) != expected or _identity(latest_info) != _identity(info):
            raise ToolkitError('conflict', 'File changed during recovery preparation; original was preserved')
        check_authorized(context, *authorization)
        current = os.stat(checked.name if parent_fd is not None else checked,
                          dir_fd=parent_fd, follow_symlinks=False)
        if _identity(current) != _identity(info) or not stat.S_ISREG(current.st_mode):
            raise ToolkitError('conflict', 'File was replaced before deletion; original was preserved')
        user_write_path(context, target)
        os.unlink(checked.name if parent_fd is not None else checked, dir_fd=parent_fd)
    return {**metadata, 'deleted': True, 'recoverable': True, 'directory_synced': directory_synced}


def _recycle_metadata(context, recycle_id):
    if not isinstance(recycle_id, str) or not RECYCLE_ID.fullmatch(recycle_id):
        raise ToolkitError('invalid_arguments', 'Invalid recovery entry identifier')
    entry = resolve_path(context, _recycle_directory(context) / recycle_id)
    raw = read_bytes(context, entry / 'metadata.json', MAX_RECYCLE_METADATA_BYTES, internal=True)
    try:
        value = json.loads(raw)
        valid = (isinstance(value, dict) and set(value) == {
            'version', 'recycle_id', 'original_path', 'bytes', 'sha256', 'mode', 'created_at'}
            and type(value['version']) is int and value['version'] == 1
            and value['recycle_id'] == recycle_id
            and isinstance(value['original_path'], str) and 0 < len(value['original_path']) <= 4096
            and not Path(value['original_path']).is_absolute()
            and value['original_path'] not in ('.', '')
            and type(value['bytes']) is int and 0 <= value['bytes'] <= MAX_ARTIFACT_BYTES
            and isinstance(value['sha256'], str) and SHA256.fullmatch(value['sha256'])
            and type(value['mode']) is int and 0 <= value['mode'] <= 0o777
            and isinstance(value['created_at'], str) and len(value['created_at']) <= 64)
        if not valid:
            raise ValueError()
        _workspace_path(context, value['original_path'])
    except ToolkitError as exc:
        if exc.code == 'cancelled':
            raise
        raise ToolkitError('invalid_state', 'Recovery metadata is invalid; snapshot was preserved') from None
    except (ValueError, KeyError, TypeError):
        raise ToolkitError('invalid_state', 'Recovery metadata is invalid; snapshot was preserved') from None
    return entry, value


def recycle_list(context, args):
    directory = _recycle_directory(context)
    if not directory.exists():
        return {'entries': [], 'truncated': False}
    rows: list[dict] = []
    limit = args.get('max_entries', 200)
    with os.scandir(directory) as entries:
        for entry in entries:
            check_cancelled(context)
            if len(rows) >= limit:
                return {'entries': sorted(rows, key=lambda row: row['recycle_id']), 'truncated': True}
            if not RECYCLE_ID.fullmatch(entry.name):
                # Unknown entries count against the scan budget as well.
                rows.append({'recycle_id': entry.name[:128], 'available': False, 'error': 'invalid_state'})
                continue
            try:
                location, metadata = _recycle_metadata(context, entry.name)
                content = resolve_path(context, location / 'content')
                info = content.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size != metadata['bytes']:
                    raise ToolkitError('invalid_state', 'Recovery content size or file type is invalid')
                rows.append({**metadata, 'available': True, 'integrity': 'checked_on_restore'})
            except (ToolkitError, OSError) as exc:
                if isinstance(exc, ToolkitError) and exc.code == 'cancelled':
                    raise
                rows.append({'recycle_id': entry.name, 'available': False,
                             'error': exc.code if isinstance(exc, ToolkitError) else 'io_error'})
    return {'entries': sorted(rows, key=lambda row: row['recycle_id']), 'truncated': False}


def restore(context, args):
    entry, metadata = _recycle_metadata(context, args['recycle_id'])
    path = args.get('path', metadata['original_path'])
    target = _workspace_path(context, path)
    raw = read_bytes(context, entry / 'content', MAX_ARTIFACT_BYTES, internal=True)
    if len(raw) != metadata['bytes'] or digest(raw) != metadata['sha256']:
        raise ToolkitError('invalid_state', 'Recovery content failed integrity verification; snapshot was preserved')
    authorization = (FS_TOOLS['restore'][0], args)
    with pinned_parent(context, target) as (parent_fd, checked):
        # Prepare in the destination directory, then publish without replacement.
        # This never replaces a destination created during the operation. A
        # separate copy prevents editing the restored file from corrupting backup.
        if parent_fd is None:
            fd, temporary = tempfile.mkstemp(prefix='.toolkit-', dir=target.parent)
        else:
            temporary = '.toolkit-' + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            user_write_path(context, path)
            os.chmod(temporary, metadata['mode'], dir_fd=parent_fd)
            check_authorized(context, *authorization)
            user_write_path(context, path)
            try:
                if os.name == 'nt':
                    # Windows rename refuses an existing target and does not
                    # require hard-link support (e.g. on a removable volume).
                    os.rename(temporary, checked)
                else:
                    os.link(temporary, checked.name if parent_fd is not None else checked,
                            src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            except FileExistsError:
                raise ToolkitError('conflict', 'Restore destination already exists; choose another path') from None
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    return {'recycle_id': args['recycle_id'], 'path': str(path), 'bytes': len(raw),
            'sha256': metadata['sha256'], 'restored': True, 'snapshot_retained': True}


def _fs_tools():
    string = {'type': 'string', 'maxLength': MAX_BYTES}
    path = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
    encoding = {'type': 'string', 'enum': list(ENCODINGS)}
    hash_schema = {'type': 'string', 'maxLength': 64}
    def bounded(maximum):
        return {'type': 'integer', 'minimum': 1, 'maximum': maximum}
    schemas = {
        'read': ({'path':path, 'encoding':encoding, 'max_bytes':bounded(MAX_BYTES), 'start_line':bounded(1000000), 'max_lines':bounded(MAX_LINES)}, ['path'], read, 'read'),
        'write': ({'path':path, 'text':string, 'encoding':encoding, 'eol':{'type':'string','enum':['source','lf','crlf','cr']}, 'bom':{'type':'boolean'}, 'expected_hash':hash_schema}, ['path','text'], write, 'write'),
        'edit': ({'path':path, 'old':{**string,'minLength':1}, 'new':string, 'encoding':encoding, 'expected_hash':hash_schema, 'occurrences':bounded(10000)}, ['path','old','new','expected_hash'], edit, 'write'),
        'list_dir': ({'path':path, 'max_entries':bounded(1000)}, ['path'], list_dir, 'read'),
        'read_many': ({'paths':{'type':'array','items':path,'minItems':1,'maxItems':32}, 'encoding':encoding, 'max_bytes':bounded(MAX_BYTES), 'max_lines':bounded(MAX_LINES)}, ['paths'], read_many, 'read'),
        'mkdir': ({'path':path, 'parents':{'type':'boolean'}}, ['path'], mkdir, 'write'),
        'delete': ({'path':path, 'expected_hash':{**hash_schema, 'minLength':64}}, ['path','expected_hash'], delete, 'write'),
        'recycle_list': ({'max_entries':bounded(1000)}, [], recycle_list, 'read'),
        'restore': ({'recycle_id':{'type':'string','minLength':32,'maxLength':32}, 'path':path}, ['recycle_id'], restore, 'write'),
    }
    descriptions = {
        'mkdir': 'Create a local workspace directory; parents is optional, depth is bounded to 32',
        'delete': 'Recoverably delete one local regular file up to 32 MiB using its SHA-256; directories are unsupported',
        'recycle_list': 'List bounded recovery snapshots in the first workspace; interrupted deletions may retain the original',
        'restore': 'Restore a verified recovery snapshot without overwriting; the snapshot remains recoverable',
    }
    tools = {}
    for name, (properties, required, handler, effect) in schemas.items():
        tools[name] = (ToolSpec(name, 'fs', descriptions.get(name, f'Bounded filesystem {name}'),
                      {'type':'object','properties':properties,'required':required,'additionalProperties':False}, effect), handler)
    return tools


FS_TOOLS = _fs_tools()


def register_fs_tools(registry):
    for spec, handler in FS_TOOLS.values():
        registry.register(spec, handler)
