"""Lossless bounded document pipeline. No executables or implicit downloads."""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import zipfile
from pathlib import PurePosixPath

from .contracts import ToolSpec, ToolkitError
from .fs import MAX_BYTES, ENCODINGS, atomic_write, decode, digest, read_bytes
from .policy import check_cancelled, resolve_path

MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_PARTS = 1000


def _artifact(path, raw, media_type):
    return {'type': 'artifact', 'path': str(path), 'media_type': media_type,
            'bytes': len(raw), 'sha256': digest(raw)}


def split_file(context, args):
    raw = read_bytes(context, args['path'], MAX_DOCUMENT_BYTES)
    text, encoding, bom, eol, lossy = decode(raw, args.get('encoding'))
    mode = args.get('mode', 'lines')
    size = args.get('size', 1000)
    if mode == 'bytes':
        if (len(raw) + size - 1) // size > MAX_PARTS:
            raise ToolkitError('limit_exceeded', 'Too many chunks; increase chunk size')
        chunks = [raw[i:i + size] for i in range(0, len(raw), size)] or [b'']
    else:
        if lossy:
            raise ToolkitError('encoding_error', 'Text splitting requires a lossless encoding')
        marker = args.get('marker')
        if mode == 'marker' and not marker:
            raise ToolkitError('invalid_arguments', 'Marker mode requires a nonempty literal marker')
        chunks = []
        current = bytearray(bom)
        line_count = 0
        # Iterate physical lines without allocating a list proportional to line count.
        for match in re.finditer(r'[^\r\n]*(?:\r\n|\r|\n|$)', text):
            line = match.group()
            if not line:
                continue
            check_cancelled(context)
            boundary = (line_count >= size if mode == 'lines'
                        else bool(marker and line.startswith(marker) and line_count))
            if boundary:
                if len(chunks) >= MAX_PARTS - 1:
                    raise ToolkitError('limit_exceeded', 'Too many chunks; increase chunk size')
                chunks.append(bytes(current))
                current = bytearray()
                line_count = 0
            if len(line) > MAX_BYTES:
                raise ToolkitError('limit_exceeded', 'Line exceeds chunk budget; use bytes mode')
            encoded = line.encode(encoding)
            if len(current) + len(encoded) > MAX_BYTES:
                raise ToolkitError('limit_exceeded', 'Chunk exceeds 1 MiB; use smaller groups or bytes mode')
            current.extend(encoded)
            line_count += 1
        chunks.append(bytes(current))
        source_hash = hashlib.sha256()
        for chunk in chunks:
            source_hash.update(chunk)
        if source_hash.hexdigest() != digest(raw):
            raise ToolkitError('encoding_error', 'Encoding cannot round-trip the source bytes')
    directory = resolve_path(context, args['output_dir'])
    if not directory.is_dir():
        raise ToolkitError('path_denied', 'Output directory must already exist')
    prefix = args.get('prefix', 'part')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', prefix):
        raise ToolkitError('invalid_arguments', 'Prefix must be a simple ASCII name')
    manifest_path = directory / (prefix + '.manifest.json')
    paths = [directory / f'{prefix}-{i:04d}.chunk' for i in range(len(chunks))]
    # Check every destination before the first mutation; never silently overwrite.
    for path in [*paths, manifest_path]:
        if resolve_path(context, path).exists():
            raise ToolkitError('conflict', 'Split output already exists')
    manifest = {'version': 1, 'mode': mode, 'source_encoding': encoding if not lossy else None,
                'source_eol': eol if not lossy else None, 'source_bom': bom.hex(),
                'source_bytes': len(raw), 'source_sha256': digest(raw),
                'parts': [{'name': path.name, 'bytes': len(chunk), 'sha256': digest(chunk)}
                          for path, chunk in zip(paths, chunks, strict=True)]}
    manifest_raw = (json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode()
    if len(manifest_raw) > MAX_BYTES or any(len(chunk) > MAX_BYTES for chunk in chunks):
        raise ToolkitError('limit_exceeded', 'Chunk or manifest exceeds 1 MiB')
    for path, chunk in zip(paths, chunks, strict=True):
        check_cancelled(context)
        atomic_write(context, path, chunk, expected_hash='')
    atomic_write(context, manifest_path, manifest_raw, expected_hash='')
    return {'manifest': _artifact(manifest_path, manifest_raw, 'application/json'),
            'parts': len(chunks), 'source_sha256': digest(raw), 'exact_bytes': True}


def merge_files(context, args):
    manifest_path = resolve_path(context, args['manifest'])
    try:
        manifest = json.loads(read_bytes(context, manifest_path))
    except (ValueError, UnicodeError) as exc:
        raise ToolkitError('invalid_manifest', 'Manifest is not valid JSON') from exc
    if not isinstance(manifest, dict) or manifest.get('version') != 1:
        raise ToolkitError('invalid_manifest', 'Unsupported manifest version')
    parts = manifest.get('parts')
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        raise ToolkitError('invalid_manifest', 'Invalid part list')
    source_bytes = manifest.get('source_bytes')
    if type(source_bytes) is not int or not 0 <= source_bytes <= MAX_DOCUMENT_BYTES:
        raise ToolkitError('limit_exceeded', 'Manifest source exceeds document budget')
    chunks, names, total = [], set(), 0
    for part in parts:
        check_cancelled(context)
        if not isinstance(part, dict):
            raise ToolkitError('invalid_manifest', 'Invalid part entry')
        name = part.get('name')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.chunk', name)
                or name in names):
            raise ToolkitError('invalid_manifest', 'Parts must be unique sibling chunk names')
        names.add(name)
        declared_bytes = part.get('bytes')
        if (type(declared_bytes) is not int or not 0 <= declared_bytes <= MAX_BYTES
                or total + declared_bytes > source_bytes):
            raise ToolkitError('limit_exceeded', 'Chunk exceeds remaining document budget')
        try:
            chunk = read_bytes(context, manifest_path.parent / name, declared_bytes)
        except ToolkitError as exc:
            if exc.code == 'limit_exceeded':
                raise ToolkitError('integrity_error', 'Chunk exceeds its declared size') from exc
            raise
        if len(chunk) != part.get('bytes') or digest(chunk) != part.get('sha256'):
            raise ToolkitError('integrity_error', 'Chunk size or SHA-256 differs from manifest')
        total += len(chunk)
        chunks.append(chunk)
    raw = b''.join(chunks)
    if len(raw) != manifest.get('source_bytes') or digest(raw) != manifest.get('source_sha256'):
        raise ToolkitError('integrity_error', 'Merged source hash differs from manifest')
    output = resolve_path(context, args['output_path'])
    atomic_write(context, output, raw, expected_hash=args.get('expected_hash', ''),
                 max_bytes=MAX_DOCUMENT_BYTES)
    return _artifact(output, raw, 'application/octet-stream')


class _ArchiveBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_DOCUMENT_BYTES:
            raise ToolkitError('limit_exceeded', 'Archive exceeds document budget')
        return super().write(data)


def pack(context, args):
    """Pack explicit regular files only; no recursive traversal or extraction."""
    entries, names, total = [], set(), 0
    for item in args['files']:
        check_cancelled(context)
        name = item['name']
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
                or ':' in name or '\x00' in name or str(path) != name or name in names):
            raise ToolkitError('path_denied', 'Archive members must have unique safe relative names')
        names.add(name)
        raw = read_bytes(context, item['path'], MAX_DOCUMENT_BYTES - total)
        total += len(raw)
        entries.append((name, raw))
    output = resolve_path(context, args['output_path'])
    archive_format = args.get('format', 'zip')
    if archive_format == 'zip':
        projected = total + 22 + sum(76 + 2 * len(name.encode('utf-8')) for name, _ in entries)
    else:
        projected = sum(512 + ((len(raw) + 511) // 512) * 512 for _, raw in entries) + 1024
        projected = ((projected + 10239) // 10240) * 10240
    if projected > MAX_DOCUMENT_BYTES:
        raise ToolkitError('limit_exceeded', 'Archive including headers exceeds document budget')
    stream = _ArchiveBuffer()
    if archive_format == 'zip':
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, raw in sorted(entries):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100600 << 16
                archive.writestr(info, raw)
    else:
        with tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, raw in sorted(entries):
                tar_info = tarfile.TarInfo(name)
                tar_info.size, tar_info.mode, tar_info.mtime = len(raw), 0o600, 0
                archive.addfile(tar_info, io.BytesIO(raw))
    raw = stream.getvalue()
    atomic_write(context, output, raw, expected_hash=args.get('expected_hash', ''),
                 max_bytes=MAX_DOCUMENT_BYTES)
    return _artifact(output, raw, 'application/zip' if archive_format == 'zip' else 'application/x-tar')


def ocr_doc(context, args):
    provider = context.providers.get('ocr')
    if provider is None:
        raise ToolkitError('capability_unavailable', 'OCR provider is not configured; no automatic installation or model download')
    raw = read_bytes(context, args['path'], MAX_DOCUMENT_BYTES)
    # Provider receives bytes, never unrestricted filesystem or subprocess access.
    result = provider(raw, {'filename': resolve_path(context, args['path']).name})
    check_cancelled(context)
    if not isinstance(result, str) or len(result.encode('utf-8')) > MAX_BYTES:
        raise ToolkitError('provider_error', 'OCR provider must return bounded text')
    return {'type': 'text', 'text': result, 'source_sha256': digest(raw)}


def register_document_tools(registry):
    path = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
    schemas = {
        'split_file': ('split', split_file, 'write', {'path': path, 'output_dir': path,
            'mode': {'type': 'string', 'enum': ['lines', 'bytes', 'marker']},
            'size': {'type': 'integer', 'minimum': 1, 'maximum': MAX_BYTES},
            'marker': {'type': 'string', 'minLength': 1, 'maxLength': 256},
            'prefix': {'type': 'string', 'minLength': 1, 'maxLength': 64},
            'encoding': {'type': 'string', 'enum': list(ENCODINGS)}}, ['path', 'output_dir']),
        'merge_files': ('doc', merge_files, 'write', {'manifest': path, 'output_path': path,
            'expected_hash': {'type': 'string', 'maxLength': 64}}, ['manifest', 'output_path']),
        'pack': ('doc', pack, 'write', {'output_path': path,
            'format': {'type': 'string', 'enum': ['zip', 'tar']},
            'expected_hash': {'type': 'string', 'maxLength': 64},
            'files': {'type': 'array', 'minItems': 1, 'maxItems': 100,
                      'items': {'type': 'object', 'properties': {'path': path, 'name': path},
                                'required': ['path', 'name'], 'additionalProperties': False}}},
            ['output_path', 'files']),
        'ocr_doc': ('doc', ocr_doc, 'read', {'path': path}, ['path']),
    }
    for name, (capability, handler, effect, properties, required) in schemas.items():
        registry.register(ToolSpec(name, capability, f'Bounded document operation: {name}',
            {'type': 'object', 'properties': properties, 'required': required,
             'additionalProperties': False}, effect), handler)
