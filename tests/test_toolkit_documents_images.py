"""Synthetic-only document/image safety and fidelity regression tests."""
import codecs
import json
import zipfile
from dataclasses import replace

import pytest

from coat_sim.agent.toolkit import ToolContext, ToolRegistry
from coat_sim.agent.toolkit.documents import register_document_tools
from coat_sim.agent.toolkit.images import register_image_tools

pytestmark = pytest.mark.contract


@pytest.fixture
def kit(tmp_path):
    registry = ToolRegistry()
    register_document_tools(registry)
    register_image_tools(registry)
    context = ToolContext(allowed_capabilities=frozenset({'doc', 'split', 'img'}),
                          allowed_roots=(tmp_path,), authorize=lambda spec, args: True)
    return registry, context, tmp_path


def call(kit, name, **args):
    registry, context, _ = kit
    return registry.invoke(name, args, context)


@pytest.mark.parametrize('mode', ['lines', 'bytes', 'marker'])
@pytest.mark.parametrize('encoding,bom', [('utf-8', b''), ('utf-8', codecs.BOM_UTF8),
    ('utf-16-le', codecs.BOM_UTF16_LE), ('utf-16-be', codecs.BOM_UTF16_BE), ('gbk', b'')])
def test_exact_roundtrip_all_blank_lines_encodings(kit, mode, encoding, bom):
    _, _, root = kit
    raw = bom + '\r\n\n# 一\r\n\r\ncontent\r# 二\n\n\n'.encode(encoding)
    (root / 'source').write_bytes(raw)
    result = call(kit, 'split_file', path='source', output_dir='.', mode=mode, size=2,
                  marker='#', encoding=encoding)
    assert result['ok'], result
    manifest = result['data']['manifest']['path']
    result = call(kit, 'merge_files', manifest=manifest, output_path='merged')
    assert result['ok'], result
    assert (root / 'merged').read_bytes() == raw


@pytest.mark.parametrize('raw', [b'', b'\n', b'\n\n', b'no-final-newline', b'\xff\x00'])
def test_byte_roundtrip_arbitrary(kit, raw):
    root = kit[2]
    (root / 'source').write_bytes(raw)
    result = call(kit, 'split_file', path='source', output_dir='.', mode='bytes', size=1)
    assert result['ok']
    assert call(kit, 'merge_files', manifest=result['data']['manifest']['path'], output_path='merged')['ok']
    assert (root / 'merged').read_bytes() == raw


def test_merge_rejects_tampering_and_traversal(kit):
    root = kit[2]
    (root / 'source').write_text('one\ntwo\n')
    result = call(kit, 'split_file', path='source', output_dir='.', size=1)
    manifest_path = root / 'part.manifest.json'
    manifest = json.loads(manifest_path.read_text())
    (root / manifest['parts'][0]['name']).write_bytes(b'changed')
    assert call(kit, 'merge_files', manifest=str(manifest_path), output_path='merged')['error']['code'] == 'integrity_error'
    manifest['parts'][0]['name'] = '../escape.chunk'
    manifest_path.write_text(json.dumps(manifest))
    assert call(kit, 'merge_files', manifest=str(manifest_path), output_path='merged')['error']['code'] == 'invalid_manifest'
    assert not (root / 'merged').exists()
    assert result['ok']


def test_pack_deterministic_and_safe(kit):
    root = kit[2]
    (root / 'a').write_bytes(b'abc')
    files = [{'path': 'a', 'name': 'folder/a.txt'}]
    for output in ['one.zip', 'two.zip']:
        assert call(kit, 'pack', files=files, output_path=output)['ok']
    assert (root / 'one.zip').read_bytes() == (root / 'two.zip').read_bytes()
    with zipfile.ZipFile(root / 'one.zip') as archive:
        assert archive.read('folder/a.txt') == b'abc'
    for name in ['../a', '/a', 'C:/a', 'a\\b', 'a/../b', './a']:
        assert not call(kit, 'pack', files=[{'path': 'a', 'name': name}], output_path='bad.zip')['ok']
    (root / 'link').symlink_to(root / 'a')
    assert call(kit, 'pack', files=[{'path': 'link', 'name': 'x'}], output_path='bad.zip')['error']['code'] == 'path_denied'


def test_missing_and_bounded_ocr_provider(kit):
    registry, context, root = kit
    (root / 'a').write_bytes(b'fake document')
    assert call(kit, 'ocr_doc', path='a')['error']['code'] == 'capability_unavailable'
    seen = []
    def provider(raw, metadata):
        seen.append((raw, metadata))
        return 'recognized text'
    result = registry.invoke('ocr_doc', {'path': 'a'}, replace(context, providers={'ocr': provider}))
    assert result['data']['text'] == 'recognized text'
    assert seen == [(b'fake document', {'filename': 'a'})]


def synthetic_image(root, size=(32, 16)):
    image = pytest.importorskip('PIL.Image')
    with image.new('RGB', size, (50, 100, 150)) as fixture:
        fixture.save(root / 'input.png')


def test_image_artifact_and_mutations(kit):
    root = kit[2]
    synthetic_image(root)
    result = call(kit, 'read_image', path='input.png')
    assert result['ok'] and result['data']['type'] == 'image_artifact'
    assert result['data']['width'] == 32 and 'base64' not in result['data']
    result = call(kit, 'crop_image', path='input.png', output_path='crop.png', box=[1, 2, 11, 12])
    assert result['ok'] and result['data']['width'] == result['data']['height'] == 10
    result = call(kit, 'resize_image', path='input.png', output_path='resize.png', width=8, height=4)
    assert result['ok'] and result['data']['width'] == 8
    assert call(kit, 'image_info', path='resize.png')['data']['height'] == 4
    assert not call(kit, 'crop_image', path='input.png', output_path='bad.png', box=[0, 0, 33, 2])['ok']
    assert not call(kit, 'resize_image', path='input.png', output_path='bad.png', width=4096, height=4096)['ok']
    assert not (root / 'bad.png').exists()


def test_image_input_bounds_and_no_overwrite(kit):
    root = kit[2]
    synthetic_image(root, (4097, 1))
    assert call(kit, 'image_info', path='input.png')['error']['code'] == 'limit_exceeded'
    synthetic_image(root)
    assert call(kit, 'resize_image', path='input.png', output_path='input.png', width=8, height=4)['error']['code'] == 'conflict'
    (root / 'link.png').symlink_to(root / 'input.png')
    assert call(kit, 'read_image', path='link.png')['error']['code'] == 'path_denied'


@pytest.mark.parametrize('name,args', [
    ('split_file', {'path': 'a', 'output_dir': '.'}),
    ('merge_files', {'manifest': 'a', 'output_path': 'b'}),
    ('pack', {'files': [{'path': 'a', 'name': 'a'}], 'output_path': 'b'}),
    ('crop_image', {'path': 'a', 'output_path': 'b', 'box': [0, 0, 1, 1]}),
    ('resize_image', {'path': 'a', 'output_path': 'b', 'width': 1, 'height': 1}),
])
def test_central_authorization_precedes_io(kit, name, args):
    registry, context, root = kit
    result = registry.invoke(name, args, replace(context, authorize=lambda spec, args: False))
    assert result['error']['code'] == 'authorization_denied'
    assert list(root.iterdir()) == []


def test_tar_metadata_and_determinism(kit):
    import tarfile
    root = kit[2]
    (root / 'a').write_bytes(b'data')
    files = [{'path': 'a', 'name': 'nested/a'}]
    for output in ['one.tar', 'two.tar']:
        assert call(kit, 'pack', files=files, output_path=output, format='tar')['ok']
    assert (root / 'one.tar').read_bytes() == (root / 'two.tar').read_bytes()
    with tarfile.open(root / 'one.tar') as archive:
        entry = archive.getmembers()[0]
        assert entry.isfile() and entry.mtime == entry.uid == entry.gid == 0
        extracted = archive.extractfile(entry)
        assert extracted is not None
        assert extracted.read() == b'data'


def test_split_conflict_preflight_leaves_other_parts_unwritten(kit):
    root = kit[2]
    (root / 'source').write_bytes(b'1234')
    (root / 'part-0001.chunk').write_bytes(b'existing')
    assert call(kit, 'split_file', path='source', output_dir='.', mode='bytes', size=2)['error']['code'] == 'conflict'
    assert not (root / 'part-0000.chunk').exists()
    assert (root / 'part-0001.chunk').read_bytes() == b'existing'


def test_merge_rejects_source_hash_and_duplicate_parts(kit):
    root = kit[2]
    (root / 'source').write_bytes(b'abc')
    assert call(kit, 'split_file', path='source', output_dir='.', mode='bytes', size=1)['ok']
    path = root / 'part.manifest.json'
    manifest = json.loads(path.read_text())
    manifest['source_sha256'] = '0' * 64
    path.write_text(json.dumps(manifest))
    assert call(kit, 'merge_files', manifest=str(path), output_path='merged')['error']['code'] == 'integrity_error'
    manifest['parts'][1] = manifest['parts'][0]
    path.write_text(json.dumps(manifest))
    assert call(kit, 'merge_files', manifest=str(path), output_path='merged')['error']['code'] == 'invalid_manifest'


def test_document_image_root_confinement(kit):
    root = kit[2]
    outside = root.parent / 'outside-fixture'
    outside.write_bytes(b'not allowed')
    assert call(kit, 'split_file', path=str(outside), output_dir='.')['error']['code'] == 'path_denied'
    assert call(kit, 'read_image', path=str(outside))['error']['code'] == 'path_denied'
    synthetic_image(root)
    assert call(kit, 'resize_image', path='input.png', output_path=str(outside), width=2, height=2)['error']['code'] == 'path_denied'
    assert outside.read_bytes() == b'not allowed'


def test_images_cannot_decode_svg_or_animated_gif(kit):
    root = kit[2]
    (root / 'image.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert call(kit, 'read_image', path='image.svg')['error']['code'] == 'unsupported_image'
    image = pytest.importorskip('PIL.Image')
    with image.new('RGB', (2, 2)) as fixture:
        fixture.save(root / 'image.gif')
    assert call(kit, 'read_image', path='image.gif')['error']['code'] == 'unsupported_image'


def test_image_output_strips_metadata(kit):
    image = pytest.importorskip('PIL.Image')
    root = kit[2]
    with image.new('RGB', (4, 4)) as fixture:
        fixture.save(root / 'input.png', icc_profile=b'synthetic metadata')
    assert call(kit, 'resize_image', path='input.png', output_path='out.png', width=2, height=2)['ok']
    with image.open(root / 'out.png') as fixture:
        assert 'icc_profile' not in fixture.info


def test_cancelled_document_job_has_no_writes(kit):
    registry, context, root = kit
    (root / 'a').write_text('content')
    result = registry.invoke('split_file', {'path': 'a', 'output_dir': '.'},
                             replace(context, cancelled=lambda: True))
    assert result['error']['code'] == 'cancelled'
    assert sorted(p.name for p in root.iterdir()) == ['a']


def test_large_document_roundtrip_pack_and_replacement(kit):
    from coat_sim.agent.toolkit.fs import digest
    from coat_sim.agent.toolkit.documents import MAX_DOCUMENT_BYTES
    root = kit[2]
    raw = b'line\r\n' * 200_000
    assert 1024 * 1024 < len(raw) < MAX_DOCUMENT_BYTES
    (root / 'source').write_bytes(raw)
    result = call(kit, 'split_file', path='source', output_dir='.', mode='lines', size=100_000)
    assert result['ok'], result
    assert call(kit, 'merge_files', manifest=result['data']['manifest']['path'], output_path='merged')['ok']
    assert (root / 'merged').read_bytes() == raw
    assert call(kit, 'merge_files', manifest=result['data']['manifest']['path'], output_path='merged', expected_hash=digest(raw))['ok']
    assert call(kit, 'pack', files=[{'path': 'source', 'name': 'large.txt'}], output_path='large.zip')['ok']


def test_later_oversized_chunk_preflight_writes_nothing(kit):
    root = kit[2]
    (root / 'source').write_bytes(b'small\n' + b'x' * (1024 * 1024 + 1))
    result = call(kit, 'split_file', path='source', output_dir='.', mode='lines', size=1)
    assert result['error']['code'] == 'limit_exceeded'
    assert sorted(path.name for path in root.iterdir()) == ['source']


def test_document_budget_checked_before_allocating_or_writing(kit):
    from coat_sim.agent.toolkit.documents import MAX_DOCUMENT_BYTES
    root = kit[2]
    with (root / 'oversized').open('wb') as stream:
        stream.truncate(MAX_DOCUMENT_BYTES + 1)
    assert call(kit, 'split_file', path='oversized', output_dir='.', mode='bytes', size=1024 * 1024)['error']['code'] == 'limit_exceeded'
    assert call(kit, 'pack', files=[{'path': 'oversized', 'name': 'large'}], output_path='bad.zip')['error']['code'] == 'limit_exceeded'
    assert not (root / 'bad.zip').exists()
    assert not list(root.glob('*.chunk'))


def test_large_image_artifact_and_encoded_outputs(kit):
    import random
    from coat_sim.agent.toolkit.images import MAX_IMAGE_BYTES
    image = pytest.importorskip('PIL.Image')
    root = kit[2]
    # Synthetic full-HD noise is a realistic incompressible raster, never user imagery.
    with image.frombytes('RGB', (1920, 1080), random.Random(7).randbytes(1920 * 1080 * 3)) as fixture:
        fixture.save(root / 'large.png')
    assert 1024 * 1024 < (root / 'large.png').stat().st_size < MAX_IMAGE_BYTES
    assert call(kit, 'read_image', path='large.png')['ok']
    result = call(kit, 'crop_image', path='large.png', output_path='crop.png', box=[0, 0, 1920, 1080])
    assert result['ok'], result
    assert result['data']['bytes'] > 1024 * 1024
    result = call(kit, 'resize_image', path='large.png', output_path='resize.png', width=1920, height=1080)
    assert result['ok'], result
    assert result['data']['bytes'] > 1024 * 1024
    assert call(kit, 'resize_image', path='large.png', output_path='resize.png', width=1920, height=1080, expected_hash=result['data']['sha256'])['ok']


def test_image_byte_budget_rejects_before_decode(kit):
    from coat_sim.agent.toolkit.images import MAX_IMAGE_BYTES, _BoundedBuffer
    from coat_sim.agent.toolkit import ToolkitError
    root = kit[2]
    with (root / 'oversized.png').open('wb') as stream:
        stream.truncate(MAX_IMAGE_BYTES + 1)
    assert call(kit, 'read_image', path='oversized.png')['error']['code'] == 'limit_exceeded'
    buffer = _BoundedBuffer()
    buffer.seek(MAX_IMAGE_BYTES)
    with pytest.raises(ToolkitError, match='byte limit'):
        buffer.write(b'x')
    assert buffer.getvalue() == b''


def test_trusted_atomic_budget_does_not_expand_fs_schema(kit):
    from coat_sim.agent.toolkit.fs import MAX_BYTES, MAX_ARTIFACT_BYTES, atomic_write, register_fs_tools
    from coat_sim.agent.toolkit import ToolkitError
    registry, context, root = kit
    raw = b'x' * (MAX_BYTES + 1)
    with pytest.raises(ToolkitError):
        atomic_write(context, 'default-denied', raw)
    atomic_write(context, 'trusted', raw, max_bytes=MAX_ARTIFACT_BYTES)
    with pytest.raises(ToolkitError):
        atomic_write(context, 'over-budget', b'', max_bytes=MAX_ARTIFACT_BYTES + 1)
    register_fs_tools(registry)
    result = registry.invoke('read', {'path': 'trusted', 'max_bytes': MAX_BYTES + 1},
                            replace(context, allowed_capabilities={'fs'}))
    assert result['error']['code'] == 'invalid_arguments'
    assert not (root / 'default-denied').exists()


def test_archive_header_budget_is_included(kit, monkeypatch):
    from coat_sim.agent.toolkit import documents
    root = kit[2]
    (root / 'a').write_bytes(b'x' * 1024)
    monkeypatch.setattr(documents, 'MAX_DOCUMENT_BYTES', 1024)
    result = call(kit, 'pack', files=[{'path': 'a', 'name': 'a'}], output_path='bad.zip')
    assert result['error']['code'] == 'limit_exceeded'
    assert not (root / 'bad.zip').exists()


def test_manifest_declared_budget_rejected_before_chunk_reads(kit):
    from coat_sim.agent.toolkit.documents import MAX_DOCUMENT_BYTES
    root = kit[2]
    manifest = {'version': 1, 'source_bytes': MAX_DOCUMENT_BYTES + 1,
                'parts': [{'name': 'missing.chunk', 'bytes': 1, 'sha256': '0' * 64}]}
    (root / 'manifest').write_text(json.dumps(manifest))
    assert call(kit, 'merge_files', manifest='manifest', output_path='bad')['error']['code'] == 'limit_exceeded'
    assert not (root / 'bad').exists()


def test_byte_split_too_many_parts_has_no_outputs(kit):
    root = kit[2]
    (root / 'a').write_bytes(b'x' * 1001)
    result = call(kit, 'split_file', path='a', output_dir='.', mode='bytes', size=1)
    assert result['error']['code'] == 'limit_exceeded'
    assert sorted(path.name for path in root.iterdir()) == ['a']
