"""Explicit custom workspace roots never grant raw managed-project writes."""
import asyncio
import pytest

from coat_sim.agent.toolkit import ToolContext, ToolkitError
from coat_sim.agent.toolkit.adapter import ToolkitAdapter, build_registry
from coat_sim.agent.toolkit import fs


@pytest.fixture
def kit(tmp_path):
    project = tmp_path / 'project'
    (project / 'data' / 'bbs').mkdir(parents=True)
    (project / 'project.coatproj').write_text('synthetic descriptor, intentionally malformed')
    (project / 'data' / 'bbs' / 'post.cbbs').write_text('original post')
    (project / 'data' / 'input.txt').write_text('original input')
    context = ToolContext(frozenset({'fs', 'doc', 'split', 'img', 'tasks'}), (tmp_path,),
        lambda spec, arguments: True)
    return build_registry(), context, tmp_path, project


@pytest.mark.parametrize('relative', ['project/project.coatproj', 'project/data/bbs/post.cbbs',
    'project/data/input.txt', 'project/new.txt', 'new.coatproj', 'standalone.cbbs'])
def test_public_write_rejects_managed_descriptor_and_all_project_descendants(kit, relative):
    registry, context, root, _ = kit
    target = root / relative
    before = target.read_bytes() if target.exists() else None
    arguments = {'path': relative, 'text': 'corrupt'}
    if before is not None:
        arguments['expected_hash'] = fs.digest(before)
    result = registry.invoke('write', arguments, context)
    assert result['error']['code'] == 'path_denied'
    assert (target.read_bytes() if target.exists() else None) == before


def test_custom_adapter_root_cannot_bypass_project_service(kit):
    _, _, root, project = kit
    config = {'assistant_toolkit_enabled': True, 'assistant_toolkit_allow_model_context': True,
        'assistant_toolkit_workspace_root': str(root), 'assistant_toolkit_capabilities': ['fs'],
        'assistant_toolkit_profile_capabilities': ['fs'], 'task_mode': 'operate'}
    adapter = ToolkitAdapter(build_registry(), config, installation_root=root.parent / 'install',
        authority=lambda: {'allowed': True, 'mode': 'operate'})
    result = asyncio.run(adapter.action('write', {'path': 'project/data/input.txt', 'text': 'changed',
        'expected_hash': fs.digest(b'original input')}))
    assert result['error']['code'] == 'path_denied'
    assert project.joinpath('data/input.txt').read_text() == 'original input'


def test_edit_delete_restore_and_mkdir_all_reject_managed_targets(kit):
    registry, context, root, project = kit
    for name, arguments in (
        ('edit', {'path': 'project/data/input.txt', 'old': 'original', 'new': 'changed',
            'expected_hash': fs.digest(b'original input')}),
        ('delete', {'path': 'project/data/input.txt', 'expected_hash': fs.digest(b'original input')}),
        ('mkdir', {'path': 'project/data/new-dir'}),
        ('mkdir', {'path': 'project/data/nested/child', 'parents': True}),
    ):
        result = registry.invoke(name, arguments, context)
        assert result['error']['code'] == 'path_denied', (name, result)
    (root / 'loose.txt').write_text('recoverable')
    deleted = registry.invoke('delete', {'path': 'loose.txt', 'expected_hash': fs.digest(b'recoverable')}, context)
    assert deleted['ok'], deleted
    restored = registry.invoke('restore', {'recycle_id': deleted['data']['recycle_id'],
        'path': 'project/data/restored.txt'}, context)
    assert restored['error']['code'] == 'path_denied'
    assert not project.joinpath('data/restored.txt').exists()
    assert project.joinpath('data/input.txt').read_text() == 'original input'


def test_document_and_image_outputs_use_same_public_guard(kit):
    registry, context, root, project = kit
    (root / 'source').write_text('one\ntwo')
    image = pytest.importorskip('PIL.Image')
    image.new('RGB', (4, 4)).save(root / 'input.png')
    for name, arguments in (
        ('pack', {'files': [{'path': 'source', 'name': 'source.txt'}], 'output_path': 'project/data/archive.zip'}),
        ('split_file', {'path': 'source', 'output_dir': 'project/data', 'size': 1}),
        ('resize_image', {'path': 'input.png', 'output_path': 'project/data/output.png', 'width': 2, 'height': 2}),
    ):
        result = registry.invoke(name, arguments, context)
        assert result['error']['code'] == 'path_denied', (name, result)
    assert sorted(path.name for path in project.joinpath('data').iterdir()) == ['bbs', 'input.txt']


def test_public_reads_and_trusted_internal_state_are_preserved(kit):
    registry, context, root, project = kit
    read = registry.invoke('read', {'path': 'project/data/input.txt'}, context)
    assert read['ok'] and read['data']['text'] == 'original input'
    with pytest.raises(ToolkitError):
        fs.atomic_write(context, project / 'new.txt', b'bad')
    fs.atomic_write(context, project / '.toolkit-internal.json', b'{}', internal=True)
    assert project.joinpath('.toolkit-internal.json').read_bytes() == b'{}'
    assert registry.invoke('write', {'path': 'standalone.txt', 'text': 'allowed'}, context)['ok']
    assert root.joinpath('standalone.txt').read_text() == 'allowed'


def test_descriptor_appearing_during_authorization_blocks_atomic_commit(tmp_path):
    calls = 0
    def authority(spec, arguments):
        nonlocal calls
        calls += 1
        if calls == 2:
            (tmp_path / 'project.coatproj').write_text('newly managed')
        return True
    context = ToolContext(frozenset({'fs'}), (tmp_path,), authority)
    result = build_registry().invoke('write', {'path': 'never.txt', 'text': 'bad'}, context)
    assert result['error']['code'] == 'path_denied'
    assert not tmp_path.joinpath('never.txt').exists()
    assert not list(tmp_path.glob('.toolkit-*'))
