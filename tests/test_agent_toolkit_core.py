import codecs
import json
import pytest
from coat_sim.agent.toolkit import ToolContext, ToolSpec, ToolRegistry
from coat_sim.agent.toolkit.fs import register_fs_tools

@pytest.fixture
def setup(tmp_path):
    registry = ToolRegistry()
    register_fs_tools(registry)
    context = ToolContext(frozenset({'fs'}), (tmp_path,), lambda spec, args: True)
    return registry, context, tmp_path


def test_disabled_and_denied(setup):
    r, c, p = setup
    assert r.invoke('read', {'path':'x'}, ToolContext())['error']['code'] == 'capability_disabled'
    assert r.invoke('read', {'path':'x'}, ToolContext(frozenset({'fs'}), (p,)))['error']['code'] == 'authorization_denied'
    assert r.specs(ToolContext()) == ()

@pytest.mark.parametrize('args', [{'path':'x','unknown':1}, {'path':'x','max_bytes':True}, {'path':'x','max_bytes':float('nan')}, {'path':'x','max_lines':0}])
def test_strict_schema(setup, args):
    r,c,p=setup
    assert r.invoke('read',args,c)['error']['code'] == 'invalid_arguments'

@pytest.mark.parametrize('encoding,bom', [('utf-8',codecs.BOM_UTF8),('utf-16-le',codecs.BOM_UTF16_LE),('utf-16-be',codecs.BOM_UTF16_BE),('gbk',b'')])
def test_preserve_encoding_bom_eol(setup, encoding, bom):
    r,c,p=setup
    raw=bom+'中文\r\nhello\r\n'.encode(encoding)
    (p/'x').write_bytes(raw)
    read=r.invoke('read',{'path':'x','encoding':encoding},c)
    assert read['ok'] and read['data']['eol']=='crlf'
    result=r.invoke('edit',{'path':'x','old':'hello','new':'world','encoding':encoding,'expected_hash':read['data']['sha256']},c)
    assert result['ok'],result
    assert (p/'x').read_bytes()==bom+'中文\r\nworld\r\n'.encode(encoding)
    stale=r.invoke('edit',{'path':'x','old':'world','new':'new','encoding':encoding,'expected_hash':read['data']['sha256']},c)
    assert stale['error']['code']=='conflict'


def test_paths_and_remote_fail_closed(setup, tmp_path):
    r,c,p=setup
    for path in ['../outside','x/../../outside',str(p.parent/'outside')]:
        assert r.invoke('read',{'path':path},c)['error']['code']=='path_denied'
    (p/'link').symlink_to(p.parent)
    assert r.invoke('read',{'path':'link/outside'},c)['error']['code']=='path_denied'
    assert r.invoke('read',{'path':'server:/x'},c)['error']['code']=='unknown_host'
    assert not (p/'server:').exists()


def test_write_and_lossy(setup):
    r,c,p=setup
    created=r.invoke('write',{'path':'x','text':'hello'},c)
    assert created['ok']
    assert r.invoke('write',{'path':'x','text':'bad'},c)['error']['code']=='conflict'
    assert r.invoke('write',{'path':'x','text':'world','expected_hash':created['data']['sha256']},c)['ok']
    (p/'bad').write_bytes(b'\xff\xfe\x00')
    assert r.invoke('read',{'path':'bad'},c)['data']['lossy']
    assert r.invoke('edit',{'path':'bad','old':'x','new':'y','expected_hash':'x'},c)['error']['code']=='lossy_edit'


def test_cancel_limits_authorization_and_immutable(setup):
    r,c,p=setup
    (p/'x').write_text('hello\nworld\n')
    calls=[]
    def authorize(spec,args):
        calls.append(spec.effect)
        with pytest.raises(TypeError):
            args['path']='other'
        return True
    c=ToolContext(frozenset({'fs'}),(p,),authorize)
    assert r.invoke('read',{'path':'x','max_lines':1},c)['data']['truncated']
    assert calls==['read']
    assert r.invoke('read_many',{'paths':['x','x'],'max_bytes':15},c)['error']['code']=='limit_exceeded'
    assert r.invoke('read',{'path':'x'},ToolContext(frozenset({'fs'}),(p,),authorize,lambda:True))['error']['code']=='cancelled'
    with pytest.raises(TypeError):
        r.specs()[0].parameters['properties']['path']['type']='integer'
    assert len(json.dumps(r.specs()[0].to_dict()))>0


def test_duplicate_and_provider_error(setup):
    r,c,p=setup
    with pytest.raises(ValueError):
        register_fs_tools(r)
    spec=ToolSpec('test','fs','test',{'type':'object','properties':{}})
    r.register(spec,lambda ctx,args: {'bad':float('nan')})
    assert not r.invoke('test',{},c)['ok']


def test_remote_provider_explicit_dispatch(setup):
    r,c,p=setup
    calls=[]
    def remote(operation,args,ctx):
        calls.append((operation,args['host'],args['path']))
        return {'text':'remote','bytes':6}
    c=ToolContext(frozenset({'fs'}),(p,),lambda spec,args: True,providers={'remote_fs':remote, 'remote_hosts': {'server': {'hostname': 'example.invalid', 'strict_host_key': True, 'known_hosts': 'external-known-hosts', 'roots': ['/folder']}}})
    assert r.invoke('read',{'path':'Server:/folder/a'},c)['data']['text']=='remote'
    assert calls==[('read','server','/folder/a')]


def test_atomic_cancel_cleans_up(setup):
    from coat_sim.agent.toolkit.fs import atomic_write
    from coat_sim.agent.toolkit import ToolkitError
    r,c,p=setup
    (p/'x').write_bytes(b'original')
    polls=[]
    def cancelled():
        polls.append(1)
        return len(polls)>2
    c=ToolContext(frozenset({'fs'}),(p,),lambda spec,args:True,cancelled)
    with pytest.raises(ToolkitError):
        atomic_write(c,'x',b'changed')
    assert (p/'x').read_bytes()==b'original'
    assert not list(p.glob('.toolkit-*'))


def test_edit_replacement_preserves_uniform_eol(setup):
    r,c,p=setup
    (p/'x').write_bytes(b'one\r\ntwo\r\n')
    original=r.invoke('read',{'path':'x'},c)['data']
    result=r.invoke('edit',{'path':'x','old':'two','new':'new\nline','expected_hash':original['sha256']},c)
    assert result['ok']
    assert (p/'x').read_bytes()==b'one\r\nnew\r\nline\r\n'


def test_edit_preflights_expansion_before_replace(setup, monkeypatch):
    from coat_sim.agent.toolkit import fs
    registry, context, root = setup
    raw = b"a" * 10000
    (root / "large-edit").write_bytes(raw)
    class NoExpansion(str):
        def replace(self, *args, **kwargs):
            raise AssertionError("oversized replacement must not allocate")
    monkeypatch.setattr(fs, "decode", lambda *args: (NoExpansion(raw.decode()), "utf-8", b"", "none", False))
    result = registry.invoke("edit", {"path": "large-edit", "old": "a", "new": "b" * 1000,
                                     "occurrences": 10000, "expected_hash": fs.digest(raw)}, context)
    assert result["error"]["code"] == "limit_exceeded"
    assert (root / "large-edit").read_bytes() == raw


def test_native_windows_path_syntax_is_not_blanket_denied():
    from coat_sim.agent.toolkit.policy import validate_path_syntax
    from coat_sim.agent.toolkit import ToolkitError
    for value in (r"C:\safe\file.txt", "C:/safe/file.txt", r"sub\file.txt"):
        validate_path_syntax(value, windows=True)
    for value in (r"C:relative.txt", r"C:\safe\..\outside", r"\\server\share\file", r"\\?\C:\file"):
        with pytest.raises(ToolkitError):
            validate_path_syntax(value, windows=True)
    with pytest.raises(ToolkitError):
        validate_path_syntax(r"C:\safe\file.txt", windows=False)
