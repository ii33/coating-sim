"""Private workspace recovery contracts, using only temporary synthetic data."""
from dataclasses import replace
import json
import os
from types import SimpleNamespace

import pytest

from coat_sim.agent.toolkit import ToolContext, ToolRegistry, ToolkitError
from coat_sim.agent.toolkit import fs
from coat_sim.agent.toolkit.documents import register_document_tools
from coat_sim.agent.toolkit.images import register_image_tools

pytestmark = pytest.mark.contract


@pytest.fixture
def kit(tmp_path):
    registry = ToolRegistry()
    fs.register_fs_tools(registry)
    register_document_tools(registry)
    register_image_tools(registry)
    context = ToolContext(frozenset({'fs', 'doc', 'split', 'img'}), (tmp_path,),
                          lambda spec, args: True)
    return registry, context, tmp_path


def invoke(kit, name, **args):
    return kit[0].invoke(name, args, kit[1])


def recycled(kit, name='notes.txt', raw=b'original\x00\xff\n'):
    path = kit[2] / name
    path.write_bytes(raw)
    result = invoke(kit, 'delete', path=name, expected_hash=fs.digest(raw))
    assert result['ok'], result
    return result['data']


def test_delete_restore_roundtrip_and_independent_backup(kit):
    raw = b'private binary\x00\xff\r\n'
    data = recycled(kit, raw=raw)
    assert not (kit[2] / 'notes.txt').exists()
    assert data['deleted'] and data['recoverable']
    rows = invoke(kit, 'recycle_list')['data']['entries']
    assert len(rows) == 1 and rows[0]['recycle_id'] == data['recycle_id']
    assert rows[0]['available'] and rows[0]['sha256'] == fs.digest(raw)
    result = invoke(kit, 'restore', recycle_id=data['recycle_id'])
    assert result['ok'] and result['data']['snapshot_retained']
    assert (kit[2] / 'notes.txt').read_bytes() == raw
    (kit[2] / 'notes.txt').write_bytes(b'new content')
    result = invoke(kit, 'restore', recycle_id=data['recycle_id'], path='another.txt')
    assert result['ok'], result
    assert (kit[2] / 'another.txt').read_bytes() == raw
    assert len(invoke(kit, 'recycle_list')['data']['entries']) == 1


def test_restore_refuses_collision_and_keeps_snapshot(kit):
    data = recycled(kit)
    (kit[2] / 'notes.txt').write_bytes(b'keep me')
    result = invoke(kit, 'restore', recycle_id=data['recycle_id'])
    assert result['error']['code'] == 'conflict'
    assert (kit[2] / 'notes.txt').read_bytes() == b'keep me'
    assert invoke(kit, 'restore', recycle_id=data['recycle_id'], path='renamed')['ok']
    assert not list(kit[2].glob('.toolkit-*'))


def test_restore_collision_created_at_commit_is_not_overwritten(kit, monkeypatch):
    data = recycled(kit)
    original_link = fs.os.link
    def competing_link(source, destination, **kwargs):
        (kit[2] / 'notes.txt').write_bytes(b'concurrent')
        return original_link(source, destination, **kwargs)
    monkeypatch.setattr(fs.os, 'link', competing_link)
    result = invoke(kit, 'restore', recycle_id=data['recycle_id'])
    assert result['error']['code'] == 'conflict'
    assert (kit[2] / 'notes.txt').read_bytes() == b'concurrent'


def test_delete_requires_hash_regular_file_and_bounded_bytes(kit, monkeypatch):
    root = kit[2]
    (root / 'a').write_bytes(b'data')
    assert invoke(kit, 'delete', path='a')['error']['code'] == 'invalid_arguments'
    assert invoke(kit, 'delete', path='a', expected_hash='x' * 64)['error']['code'] == 'invalid_arguments'
    assert invoke(kit, 'delete', path='a', expected_hash='0' * 64)['error']['code'] == 'conflict'
    (root / 'folder').mkdir()
    assert not invoke(kit, 'delete', path='folder', expected_hash=fs.digest(b''))['ok']
    if hasattr(os, 'mkfifo'):
        os.mkfifo(root / 'pipe')
        assert invoke(kit, 'delete', path='pipe', expected_hash=fs.digest(b''))['error']['code'] == 'path_denied'
    monkeypatch.setattr(fs, 'MAX_ARTIFACT_BYTES', 3)
    assert invoke(kit, 'delete', path='a', expected_hash=fs.digest(b'data'))['error']['code'] == 'limit_exceeded'
    assert (root / 'a').read_bytes() == b'data'
    assert not (root / '.toolkit').exists()


@pytest.mark.parametrize('path', ['../escape', 'nested/../../escape', 'remote:/file'])
def test_recovery_paths_fail_closed(kit, path):
    assert not invoke(kit, 'delete', path=path, expected_hash='0' * 64)['ok']
    data = recycled(kit)
    assert not invoke(kit, 'restore', recycle_id=data['recycle_id'], path=path)['ok']


def test_symlinks_and_reparse_routes_are_not_followed(kit):
    root = kit[2]
    (root / 'actual').write_bytes(b'content')
    (root / 'link').symlink_to(root / 'actual')
    assert invoke(kit, 'delete', path='link', expected_hash=fs.digest(b'content'))['error']['code'] == 'path_denied'
    data = recycled(kit)
    assert invoke(kit, 'restore', recycle_id=data['recycle_id'], path='link')['error']['code'] == 'path_denied'
    content = root / '.toolkit/recycle' / data['recycle_id'] / 'content'
    content.unlink()
    content.symlink_to(root / 'actual')
    assert invoke(kit, 'restore', recycle_id=data['recycle_id'])['error']['code'] == 'path_denied'
    assert not invoke(kit, 'recycle_list')['data']['entries'][0]['available']
    assert (root / 'actual').read_bytes() == b'content'


def test_symlink_recycle_root_does_not_touch_external_directory(kit, tmp_path):
    root = kit[2]
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (root / '.toolkit').symlink_to(outside, target_is_directory=True)
    (root / 'a').write_bytes(b'keep')
    result = invoke(kit, 'delete', path='a', expected_hash=fs.digest(b'keep'))
    assert result['error']['code'] == 'path_denied'
    assert list(outside.iterdir()) == [] and (root / 'a').read_bytes() == b'keep'


@pytest.mark.parametrize('corruption', ['content', 'manifest', 'traversal', 'reserved', 'missing'])
def test_invalid_snapshot_never_restores_or_purges(kit, corruption):
    data = recycled(kit)
    entry = kit[2] / '.toolkit/recycle' / data['recycle_id']
    if corruption == 'content':
        (entry / 'content').write_bytes(b'corruption')
    elif corruption == 'manifest':
        (entry / 'metadata.json').write_text('{bad json')
    elif corruption == 'missing':
        (entry / 'content').unlink()
    else:
        metadata = json.loads((entry / 'metadata.json').read_text())
        metadata['original_path'] = '../outside' if corruption == 'traversal' else '.toolkit/state'
        (entry / 'metadata.json').write_text(json.dumps(metadata))
    result = invoke(kit, 'restore', recycle_id=data['recycle_id'])
    assert not result['ok']
    assert not (kit[2] / 'notes.txt').exists()
    assert entry.exists() and (entry / 'metadata.json').exists()


def test_id_collision_keeps_prior_snapshot_and_new_source(kit, monkeypatch):
    monkeypatch.setattr(fs.uuid, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    first = recycled(kit, 'one', b'first')
    (kit[2] / 'two').write_bytes(b'second')
    result = invoke(kit, 'delete', path='two', expected_hash=fs.digest(b'second'))
    assert result['error']['code'] == 'conflict'
    assert (kit[2] / 'two').read_bytes() == b'second'
    assert invoke(kit, 'restore', recycle_id=first['recycle_id'])['ok']
    assert (kit[2] / 'one').read_bytes() == b'first'


def test_changed_source_preserved_with_recoverable_snapshot(kit, monkeypatch):
    def changed(context, entry):
        (kit[2] / 'notes.txt').write_bytes(b'new content')
        return True
    monkeypatch.setattr(fs, '_sync_recovery_directories', changed)
    (kit[2] / 'notes.txt').write_bytes(b'original')
    result = invoke(kit, 'delete', path='notes.txt', expected_hash=fs.digest(b'original'))
    assert result['error']['code'] == 'conflict'
    assert (kit[2] / 'notes.txt').read_bytes() == b'new content'
    row = invoke(kit, 'recycle_list')['data']['entries'][0]
    assert invoke(kit, 'restore', recycle_id=row['recycle_id'], path='recovered')['ok']
    assert (kit[2] / 'recovered').read_bytes() == b'original'


@pytest.mark.parametrize('cancel', [False, True])
def test_delete_rechecks_live_grant_at_unlink(kit, monkeypatch, cancel):
    registry, context, root = kit
    revoked = False
    def preparation(context, entry):
        nonlocal revoked
        revoked = True
        return True
    monkeypatch.setattr(fs, '_sync_recovery_directories', preparation)
    context = replace(context, authorize=lambda spec, args: not revoked,
                      cancelled=lambda: cancel and revoked)
    (root / 'a').write_bytes(b'original')
    result = registry.invoke('delete', {'path': 'a', 'expected_hash': fs.digest(b'original')}, context)
    assert result['error']['code'] == ('cancelled' if cancel else 'authorization_denied')
    assert (root / 'a').read_bytes() == b'original'
    assert invoke(kit, 'recycle_list')['data']['entries'][0]['available']


def test_restore_and_write_recheck_grant_after_preparation(kit, monkeypatch):
    data = recycled(kit)
    registry, context, root = kit
    original_sync = fs.os.fsync
    revoked = False
    def revoke(fd):
        nonlocal revoked
        original_sync(fd)
        revoked = True
    monkeypatch.setattr(fs.os, 'fsync', revoke)
    context = replace(context, authorize=lambda spec, args: not revoked)
    result = registry.invoke('restore', {'recycle_id': data['recycle_id']}, context)
    assert result['error']['code'] == 'authorization_denied'
    assert not (root / 'notes.txt').exists()
    revoked = False
    result = registry.invoke('write', {'path': 'new', 'text': 'denied'}, context)
    assert result['error']['code'] == 'authorization_denied'
    assert not (root / 'new').exists()
    assert not list(root.glob('.toolkit-*'))


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory fsync ordering')
def test_directory_sync_precedes_source_unlink_and_failure_preserves_source(kit, monkeypatch):
    sync = fs._sync_recovery_directories
    unlink = fs.os.unlink
    events = []
    def record_sync(context, entry):
        result = sync(context, entry)
        events.append('directories_synced')
        return result
    def record_unlink(path, **kwargs):
        if str(path) == 'notes.txt':
            events.append('source_unlinked')
        return unlink(path, **kwargs)
    monkeypatch.setattr(fs, '_sync_recovery_directories', record_sync)
    monkeypatch.setattr(fs.os, 'unlink', record_unlink)
    assert recycled(kit)['directory_synced']
    assert events == ['directories_synced', 'source_unlinked']
    def fail_sync(context, entry):
        raise OSError('synthetic directory fsync error')
    monkeypatch.setattr(fs, '_sync_recovery_directories', fail_sync)
    (kit[2] / 'another').write_bytes(b'preserve')
    result = invoke(kit, 'delete', path='another', expected_hash=fs.digest(b'preserve'))
    assert result['error']['code'] == 'io_error'
    assert (kit[2] / 'another').read_bytes() == b'preserve'


def test_mkdir_parents_depth_existing_and_file_collision(kit):
    assert not invoke(kit, 'mkdir', path='one/two')['ok']
    assert invoke(kit, 'mkdir', path='one/two', parents=True)['ok']
    assert invoke(kit, 'mkdir', path='one/two')['ok']
    assert invoke(kit, 'write', path='one/two/note', text='saved')['ok']
    assert invoke(kit, 'mkdir', path='one/two/note')['error']['code'] == 'path_denied'
    assert invoke(kit, 'mkdir', path='/'.join(['nested'] * 33), parents=True)['error']['code'] == 'limit_exceeded'
    (kit[2] / 'link').symlink_to(kit[2] / 'one', target_is_directory=True)
    assert invoke(kit, 'mkdir', path='link/child', parents=True)['error']['code'] == 'path_denied'
    assert invoke(kit, 'mkdir', path='../outside', parents=True)['error']['code'] == 'path_denied'


def test_mkdir_rechecks_grant_between_parent_creations(kit):
    registry, context, root = kit
    calls = 0
    def authorize(spec, args):
        nonlocal calls
        calls += 1
        return calls <= 2
    result = registry.invoke('mkdir', {'path': 'one/two/three', 'parents': True},
                             replace(context, authorize=authorize))
    assert result['error']['code'] == 'authorization_denied'
    assert (root / 'one').is_dir() and not (root / 'one/two').exists()


@pytest.mark.parametrize('protected', ['.toolkit/content', '.toolkit-memory-state.json',
                                      '.TOOLKIT-tasks-state.json.lock'])
def test_generic_tools_cannot_read_or_mutate_internal_state(kit, protected):
    root = kit[2]
    path = root / protected
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'state')
    for name, args in [
        ('read', {'path': protected}),
        ('read_many', {'paths': [protected]}),
        ('write', {'path': protected, 'text': 'overwrite', 'expected_hash': fs.digest(b'state')}),
        ('edit', {'path': protected, 'old': 'state', 'new': 'changed', 'expected_hash': fs.digest(b'state')}),
        ('delete', {'path': protected, 'expected_hash': fs.digest(b'state')}),
        ('mkdir', {'path': protected}),
        ('split_file', {'path': protected, 'output_dir': '.'}),
        ('read_image', {'path': protected}),
        ('pack', {'files': [{'path': protected, 'name': 'state'}], 'output_path': 'leaked.zip'}),
    ]:
        assert invoke(kit, name, **args)['error']['code'] == 'path_denied', (name, args)
    (root / 'source').write_bytes(b'source')
    assert invoke(kit, 'pack', files=[{'path': 'source', 'name': 'source'}],
                  output_path=protected, expected_hash=fs.digest(b'state'))['error']['code'] == 'path_denied'
    assert path.read_bytes() == b'state'
    names = {row['name'] for row in invoke(kit, 'list_dir', path='.')['data']['entries']}
    assert path.parts[-1] not in names and '.toolkit' not in names
    with pytest.raises(ToolkitError, match='reserved'):
        fs.read_bytes(kit[1], path)
    with pytest.raises(ToolkitError, match='reserved'):
        fs.atomic_write(kit[1], path, b'overwrite')
    assert fs.read_bytes(kit[1], path, internal=True) == b'state'
    assert invoke(kit, 'read', path=protected, internal=True)['error']['code'] == 'invalid_arguments'


def test_image_output_cannot_replace_recovery_snapshot(kit):
    image = pytest.importorskip('PIL.Image')
    data = recycled(kit)
    content = kit[2] / '.toolkit/recycle' / data['recycle_id'] / 'content'
    original = content.read_bytes()
    with image.new('RGB', (4, 4)) as fixture:
        fixture.save(kit[2] / 'source.png')
    result = invoke(kit, 'resize_image', path='source.png', output_path=str(content),
                    width=2, height=2, expected_hash=fs.digest(original))
    assert result['error']['code'] == 'path_denied'
    assert content.read_bytes() == original


def test_recycle_listing_is_bounded_and_invalid_ids_are_rejected(kit):
    assert invoke(kit, 'recycle_list')['data'] == {'entries': [], 'truncated': False}
    recycled(kit, 'one', b'one')
    recycled(kit, 'two', b'two')
    result = invoke(kit, 'recycle_list', max_entries=1)
    assert len(result['data']['entries']) == 1 and result['data']['truncated']
    for identifier in ['../escape', 'x' * 32, 'A' * 32]:
        assert invoke(kit, 'restore', recycle_id=identifier)['error']['code'] == 'invalid_arguments'
    assert invoke(kit, 'recycle_list', max_entries=1001)['error']['code'] == 'invalid_arguments'
    assert not any('purge' in spec.name for spec in kit[0].specs())


def test_native_windows_aliases_cannot_bypass_reserved_state():
    from coat_sim.agent.toolkit.policy import validate_path_syntax
    for path in (r'C:\safe\.toolkit.\file', r'C:\safe\a:stream', r'sub\.toolkit \file',
                 r'C:\safe\NUL', r'C:\safe\TOOLKI~1\file', r'C:\safe\.TOOLK~1.JSO'):
        with pytest.raises(ToolkitError):
            validate_path_syntax(path, windows=True)
