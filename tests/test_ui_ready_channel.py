"""Isolated filesystem and listener tests for the local startup channel.

The Listener is mocked so these tests exercise pathname policy without relying
on AF_UNIX socket support in the test container.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import stat
import tempfile
from types import SimpleNamespace

import pytest

from coat_sim.ui import ready_channel as module


pytestmark = pytest.mark.contract


@pytest.fixture
def short_base():
    """Use a unique short POSIX root without changing TMPDIR or user settings."""
    base = Path(tempfile.mkdtemp(prefix="ready-channel-test-", dir="/tmp")).resolve()
    try:
        yield base
    finally:
        shutil.rmtree(base)


def _root_for_address_length(base: Path, address_length: int) -> Path:
    """Create a default tempfile root whose resulting AF_UNIX address is exact."""
    probe = Path(tempfile.mkdtemp(prefix="coat-ui-ready-", dir=base))
    child_bytes = len(os.fsencode(probe.name))
    probe.rmdir()
    # address = root + '/' + tempfile child + '/event.sock'
    root_bytes = address_length - 1 - child_bytes - len(os.fsencode("/event.sock"))
    fill = root_bytes - len(os.fsencode(str(base))) - 1
    assert fill > 0
    root = base / ("r" * fill)
    root.mkdir(mode=0o700)
    assert len(os.fsencode(str(root))) == root_bytes
    return root


def _recording_listener(monkeypatch):
    calls = []

    class RecordingListener:
        def __init__(self, address, *, family, authkey):
            self.address = address
            self.family = family
            self.authkey = authkey
            self.closed = False
            calls.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(module, "Listener", RecordingListener)
    return calls


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX filesystem policy is POSIX-only")
def test_default_unix_channel_keeps_private_tempdir_and_32_byte_key(monkeypatch, short_base):
    listeners = _recording_listener(monkeypatch)
    monkeypatch.setattr(module.tempfile, "tempdir", str(short_base))

    channel = module.UiReadyChannel()
    directory = channel._temporary_directory
    assert channel.family == "AF_UNIX"
    assert directory is not None
    assert directory.parent == short_base
    assert directory.name.startswith("coat-ui-ready-")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert isinstance(channel.authkey, bytes) and len(channel.authkey) == 32
    assert len(os.fsencode(channel.address)) <= module.MAX_UNIX_ADDRESS_BYTES
    assert len(listeners) == 1
    assert listeners[0].address == channel.address
    assert listeners[0].family == "AF_UNIX"
    assert listeners[0].authkey is channel.authkey

    channel.close()
    assert listeners[0].closed
    assert not directory.exists()


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX pathname byte limit is POSIX-only")
@pytest.mark.parametrize("address_length", [103, 104])
def test_unix_address_boundary_is_measured_in_encoded_bytes(
        monkeypatch, short_base, address_length):
    listeners = _recording_listener(monkeypatch)
    default_root = _root_for_address_length(short_base, address_length)
    monkeypatch.setattr(module.tempfile, "tempdir", str(default_root))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (short_base,))

    channel = module.UiReadyChannel()
    address_bytes = len(os.fsencode(channel.address))
    assert channel._temporary_directory is not None
    if address_length == 103:
        assert address_bytes == 103
        assert channel._temporary_directory.parent == default_root
    else:
        assert address_bytes <= 103
        assert channel._temporary_directory.parent == short_base
        assert list(default_root.iterdir()) == []
    assert stat.S_IMODE(channel._temporary_directory.stat().st_mode) == 0o700
    assert len(listeners) == 1 and listeners[0].address == channel.address
    channel.close()


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX pathname byte limit is POSIX-only")
def test_unicode_path_uses_filesystem_bytes_not_character_count(monkeypatch, short_base):
    listeners = _recording_listener(monkeypatch)
    probe = Path(tempfile.mkdtemp(prefix="coat-ui-ready-", dir=short_base))
    child_chars = len(probe.name)
    child_bytes = len(os.fsencode(probe.name))
    probe.rmdir()
    fixed_bytes = len(os.fsencode(str(short_base))) + 1 + child_bytes + len(os.fsencode("/event.sock"))
    accents = (module.MAX_UNIX_ADDRESS_BYTES - fixed_bytes) // 2 + 1
    unicode_root = short_base / ("é" * accents)
    unicode_root.mkdir(mode=0o700)
    address_chars = len(str(unicode_root)) + 1 + child_chars + len("/event.sock")
    address_bytes = (len(os.fsencode(str(unicode_root))) + 1 + child_bytes
                     + len(os.fsencode("/event.sock")))
    assert address_chars <= module.MAX_UNIX_ADDRESS_BYTES < address_bytes

    monkeypatch.setattr(module.tempfile, "tempdir", str(unicode_root))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (short_base,))
    channel = module.UiReadyChannel()
    assert channel._temporary_directory is not None
    assert channel._temporary_directory.parent == short_base
    assert len(os.fsencode(channel.address)) <= module.MAX_UNIX_ADDRESS_BYTES
    assert list(unicode_root.iterdir()) == []
    assert len(listeners) == 1
    channel.close()


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX root ownership checks are POSIX-only")
def test_safe_short_root_resolves_symlinks_and_rejects_shared_nonsticky_root(short_base):
    private = short_base / "private"
    private.mkdir(mode=0o700)
    link = short_base / "private-link"
    link.symlink_to(private, target_is_directory=True)
    assert module._safe_short_root(link) == private.resolve()

    shared = short_base / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    assert module._safe_short_root(shared) is None

    sticky = short_base / "sticky-shared"
    sticky.mkdir()
    sticky.chmod(0o1777)
    assert module._safe_short_root(sticky) == sticky.resolve()


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX root ownership checks are POSIX-only")
def test_safe_sticky_root_requires_root_or_current_user_owner(monkeypatch, short_base):
    foreign = short_base / "foreign-sticky"
    root_owned = short_base / "root-sticky"
    foreign.mkdir()
    root_owned.mkdir()
    real_stat = Path.stat
    sticky_directory_mode = stat.S_IFDIR | 0o1777

    def controlled_stat(path, *args, **kwargs):
        if path == foreign:
            return SimpleNamespace(st_mode=sticky_directory_mode, st_uid=os.getuid() + 1)
        if path == root_owned:
            return SimpleNamespace(st_mode=sticky_directory_mode, st_uid=0)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", controlled_stat)
    assert module._safe_short_root(foreign) is None
    assert module._safe_short_root(root_owned) == root_owned.resolve()


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX pathname fallback is POSIX-only")
def test_long_path_fallback_uses_resolved_symlink_root(monkeypatch, short_base):
    listeners = _recording_listener(monkeypatch)
    default_root = _root_for_address_length(short_base, 104)
    actual_root = short_base / "canonical-short-root"
    actual_root.mkdir(mode=0o700)
    alias = short_base / "short-root-alias"
    alias.symlink_to(actual_root, target_is_directory=True)
    monkeypatch.setattr(module.tempfile, "tempdir", str(default_root))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (alias,))

    channel = module.UiReadyChannel()
    assert channel._temporary_directory is not None
    assert channel._temporary_directory.parent == actual_root.resolve()
    assert len(os.fsencode(channel.address)) <= module.MAX_UNIX_ADDRESS_BYTES
    assert list(default_root.iterdir()) == []
    assert len(listeners) == 1
    channel.close()


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX pathname fallback is POSIX-only")
def test_no_safe_short_root_raises_enametoolong_and_removes_initial_dir(
        monkeypatch, short_base):
    default_root = _root_for_address_length(short_base, 104)
    unsafe = short_base / "unsafe-shared-root"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    missing = short_base / "missing-root"
    monkeypatch.setattr(module.tempfile, "tempdir", str(default_root))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (missing, unsafe))

    with pytest.raises(OSError) as caught:
        module._unix_ready_address()
    assert caught.value.errno == errno.ENAMETOOLONG
    assert "103" in str(caught.value)
    assert list(default_root.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX pathname fallback is POSIX-only")
def test_only_an_overlong_fallback_path_advances_to_next_root(monkeypatch, short_base):
    listeners = _recording_listener(monkeypatch)
    default_root = _root_for_address_length(short_base, 104)
    overlong_root = _root_for_address_length(short_base, 110)
    monkeypatch.setattr(module.tempfile, "tempdir", str(default_root))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (overlong_root, short_base))

    channel = module.UiReadyChannel()
    assert channel._temporary_directory is not None
    assert channel._temporary_directory.parent == short_base
    assert len(os.fsencode(channel.address)) <= module.MAX_UNIX_ADDRESS_BYTES
    assert list(default_root.iterdir()) == []
    assert list(overlong_root.iterdir()) == []
    assert len(listeners) == 1
    channel.close()


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX pathname fallback is POSIX-only")
def test_mkdtemp_eperm_propagates_without_trying_next_root(monkeypatch, short_base):
    default_root = _root_for_address_length(short_base, 104)
    first, second = short_base / "first-safe", short_base / "second-safe"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    monkeypatch.setattr(module.tempfile, "tempdir", str(default_root))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (first, second))
    real_mkdtemp = tempfile.mkdtemp
    failure = PermissionError(errno.EPERM, "test fallback mkdir denied")
    attempted_roots = []

    def deny_first_fallback(*, prefix, **kwargs):
        root = kwargs.get("dir")
        attempted_roots.append(root)
        if root == first:
            raise failure
        return real_mkdtemp(prefix=prefix, **kwargs)

    monkeypatch.setattr(module.tempfile, "mkdtemp", deny_first_fallback)
    with pytest.raises(PermissionError) as caught:
        module._unix_ready_address()
    assert caught.value is failure
    assert attempted_roots == [None, first]
    assert list(default_root.iterdir()) == []
    assert list(first.iterdir()) == [] and list(second.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX pathname fallback is POSIX-only")
def test_default_mkdtemp_eperm_propagates_without_trying_fallback_roots(
        monkeypatch, short_base):
    default_root = _root_for_address_length(short_base, 104)
    first, second = short_base / "first-safe", short_base / "second-safe"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    monkeypatch.setattr(module.tempfile, "tempdir", str(default_root))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (first, second))
    real_mkdtemp = tempfile.mkdtemp
    failure = PermissionError(errno.EPERM, "test initial mkdir denied")
    attempted_roots = []

    def deny_default_creation(*, prefix, **kwargs):
        attempted_roots.append(kwargs.get("dir"))
        if kwargs.get("dir") is None:
            raise failure
        return real_mkdtemp(prefix=prefix, **kwargs)

    monkeypatch.setattr(module.tempfile, "mkdtemp", deny_default_creation)
    with pytest.raises(PermissionError) as caught:
        module._unix_ready_address()
    assert caught.value is failure
    assert attempted_roots == [None]
    assert list(default_root.iterdir()) == []
    assert list(first.iterdir()) == [] and list(second.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX pathname fallback is POSIX-only")
def test_fallback_chmod_eperm_cleans_directory_without_trying_next_root(monkeypatch, short_base):
    default_root = _root_for_address_length(short_base, 104)
    first, second = short_base / "first-safe", short_base / "second-safe"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    monkeypatch.setattr(module.tempfile, "tempdir", str(default_root))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (first, second))
    real_os = module.os
    failure = PermissionError(errno.EPERM, "test fallback chmod denied")

    class OsWithDeniedFallbackChmod:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def chmod(self, path, mode):
            if Path(path).parent == first:
                raise failure
            return real_os.chmod(path, mode)

    monkeypatch.setattr(module, "os", OsWithDeniedFallbackChmod())
    with pytest.raises(PermissionError) as caught:
        module._unix_ready_address()
    assert caught.value is failure
    assert list(default_root.iterdir()) == []
    assert list(first.iterdir()) == [] and list(second.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX pathname fallback is POSIX-only")
def test_short_root_stat_eperm_propagates_without_trying_next_root(monkeypatch, short_base):
    default_root = _root_for_address_length(short_base, 104)
    first, second = short_base / "first-safe", short_base / "second-safe"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    monkeypatch.setattr(module.tempfile, "tempdir", str(default_root))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (first, second))
    real_stat = Path.stat
    failure = PermissionError(errno.EPERM, "test fallback root stat denied")

    def deny_first_stat(path, *args, **kwargs):
        if path == first:
            raise failure
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny_first_stat)
    with pytest.raises(PermissionError) as caught:
        module._unix_ready_address()
    assert caught.value is failure
    assert list(default_root.iterdir()) == []
    assert list(first.iterdir()) == [] and list(second.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX pathname fallback is POSIX-only")
@pytest.mark.parametrize("address_length", [103, 104])
def test_listener_eperm_cleans_socket_and_does_not_try_next_root(
        monkeypatch, short_base, address_length):
    default_root = _root_for_address_length(short_base, address_length)
    first, second = short_base / "first-safe", short_base / "second-safe"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    monkeypatch.setattr(module.tempfile, "tempdir", str(default_root))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (first, second))
    failure = PermissionError(errno.EPERM, "test AF_UNIX listener denied")
    calls = []

    class DeniedListener:
        def __init__(self, address, *, family, authkey):
            calls.append((address, family, authkey,
                          stat.S_IMODE(Path(address).parent.stat().st_mode)))
            Path(address).write_bytes(b"mock socket path")
            raise failure

    monkeypatch.setattr(module, "Listener", DeniedListener)
    with pytest.raises(PermissionError) as caught:
        module.UiReadyChannel()
    assert caught.value is failure
    assert len(calls) == 1 and calls[0][1] == "AF_UNIX"
    assert calls[0][3] == 0o700
    if address_length == 103:
        assert len(os.fsencode(calls[0][0])) == 103
        assert Path(calls[0][0]).parent.parent == default_root
    else:
        assert len(os.fsencode(calls[0][0])) <= 103
        assert Path(calls[0][0]).parent.parent == first
    assert list(default_root.iterdir()) == []
    assert list(first.iterdir()) == [] and list(second.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX directory permission handling is POSIX-only")
def test_initial_chmod_eperm_cleans_directory_and_does_not_fallback(monkeypatch, short_base):
    second = short_base / "second-safe"
    second.mkdir(mode=0o700)
    monkeypatch.setattr(module.tempfile, "tempdir", str(short_base))
    monkeypatch.setattr(module, "SHORT_UNIX_TEMP_ROOTS", (second,))
    real_os = module.os
    failure = PermissionError(errno.EPERM, "test initial chmod denied")

    class OsWithDeniedChmod:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def chmod(self, _path, _mode):
            raise failure

    monkeypatch.setattr(module, "os", OsWithDeniedChmod())
    with pytest.raises(PermissionError) as caught:
        module._unix_ready_address()
    assert caught.value is failure
    assert list(short_base.iterdir()) == [second]
    assert list(second.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership check")
def test_short_root_is_rejected_when_getuid_is_unavailable(monkeypatch, short_base):
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=None))
    assert module._safe_short_root(short_base) is None


def test_af_pipe_address_and_listener_behavior_are_unchanged(monkeypatch):
    listeners = _recording_listener(monkeypatch)
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt", getpid=lambda: 12345))

    def forbidden_unix_address():
        pytest.fail("AF_PIPE initialization must not use the AF_UNIX helper")

    monkeypatch.setattr(module, "_unix_ready_address", forbidden_unix_address)

    channel = module.UiReadyChannel()
    assert channel.family == "AF_PIPE"
    assert channel._temporary_directory is None
    assert channel.address.startswith(r"\\.\pipe\coat-ui-ready-12345-")
    assert len(listeners) == 1 and listeners[0].address == channel.address
    assert listeners[0].family == "AF_PIPE"
    assert listeners[0].authkey is channel.authkey and len(channel.authkey) == 32
    channel.close()
    assert listeners[0].closed
