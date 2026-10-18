"""Bounded text filesystem operations; no encoding guessing for destructive edits."""
from __future__ import annotations
import codecs
import hashlib
import os
import stat
import tempfile
import uuid
from .contracts import ToolSpec, ToolkitError
from .policy import resolve_path, check_cancelled, remote_path, pinned_parent

MAX_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * MAX_BYTES
MAX_LINES = 10000
ENCODINGS = ('utf-8', 'utf-16-le', 'utf-16-be', 'gbk', 'gb18030', 'big5', 'latin1')
BOMS = ((codecs.BOM_UTF8, 'utf-8'), (codecs.BOM_UTF16_LE, 'utf-16-le'), (codecs.BOM_UTF16_BE, 'utf-16-be'))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bytes(context, path, limit=MAX_BYTES):
    if type(limit) is not int or not 0 <= limit <= MAX_ARTIFACT_BYTES:
        raise ToolkitError('limit_exceeded', 'Invalid bounded read budget')
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


def atomic_write(context, path, raw, expected_hash=None, *, max_bytes=MAX_BYTES):
    target = resolve_path(context, path)
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_ARTIFACT_BYTES:
        raise ToolkitError('limit_exceeded', 'Invalid bounded write budget')
    if len(raw) > max_bytes:
        raise ToolkitError('limit_exceeded', 'Output exceeds byte limit')
    if target.exists() and not target.is_file():
        raise ToolkitError('path_denied', 'Target must be a regular file')
    if expected_hash is not None:
        current = digest(read_bytes(context, path, max_bytes)) if target.exists() else ''
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
            resolve_path(context, path)
            if expected_hash is not None:
                current = digest(read_bytes(context, path, max_bytes)) if target.exists() else ''
                if current != expected_hash:
                    raise ToolkitError('conflict', 'File changed during write preparation')
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
    path = resolve_path(context, args['path'])
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
    return atomic_write(context, args['path'], bom + text.encode(encoding), args.get('expected_hash', '' if previous is None else None))


def edit(context, args):
    if remote_path(args['path']):
        return _remote(context, 'edit', args)
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
    return atomic_write(context, args['path'], bom + text.encode(encoding), args['expected_hash'])


def list_dir(context, args):
    if remote_path(args['path']):
        return _remote(context, 'list_dir', args)
    target = resolve_path(context, args['path'])
    limit = args.get('max_entries', 200)
    entries: list[dict[str, str]] = []
    with os.scandir(target) as iterator:
        for entry in iterator:
            check_cancelled(context)
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


def register_fs_tools(registry):
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
    }
    for name, (properties, required, handler, effect) in schemas.items():
        registry.register(ToolSpec(name, 'fs', f'Bounded filesystem {name}', {'type':'object','properties':properties,'required':required,'additionalProperties':False}, effect), handler)
