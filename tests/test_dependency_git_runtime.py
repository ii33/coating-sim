"""Offline bootstrap tests: no real network, Git process or installer invoked."""

import hashlib
import io
from pathlib import Path
import stat
import threading
import zipfile

import pytest

from tools.dependency_manager import git_runtime as git
from tools.dependency_manager.mirrors import ProxyConfig


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def archive_bytes(entries=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in entries or [
            ("cmd/git.exe", b"fixture executable"),
            ("LICENSE.txt", b"fixture notice"),
        ]:
            archive.writestr(name, content)
    return stream.getvalue()


@pytest.fixture
def fixture_manifest(monkeypatch):
    manifest = git.git_runtime_manifest()
    data = archive_bytes()
    manifest["artifacts"]["x86_64"]["sha256"] = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(git, "git_runtime_manifest", lambda: manifest)
    return data


def plan(home):
    return git.plan_git_runtime(home / "private/git", platform_name="win32", machine="AMD64")


def fake_download(data):
    def download(artifact, destination, proxy, cancel_event, emit):
        Path(destination).write_bytes(data)

    return download


def test_manifest_fixed_official_hashes():
    manifest = git.git_runtime_manifest()
    assert manifest["version"] == "2.56.0.windows.2"
    assert (
        manifest["artifacts"]["x86_64"]["sha256"]
        == "da35e72aa21c005a5a0d298cfbae110bc1609a815730ea0dde84b01a1b3cd3be"
    )
    assert "not independent ZIP signature verification" in manifest["signature_evidence"]
    assert "GPL" in manifest["license"]


def test_plan_is_read_only_and_platform_bound(home):
    result = plan(home)
    assert result["status"] == "ready"
    assert not (home / "private").exists()
    assert (
        git.plan_git_runtime(home / "git", platform_name="linux", machine="AMD64")["status"]
        == "blocked"
    )
    assert (
        git.plan_git_runtime(home / "git", platform_name="win32", machine="i386")["status"]
        == "blocked"
    )


def test_existing_and_unrelated_paths_never_overwritten(home):
    (home / "private/git").mkdir(parents=True)
    (home / "private/git/keep").write_text("mine")
    assert plan(home)["status"] == "blocked"
    assert (home / "private/git/keep").read_text() == "mine"


def test_parent_symlink_blocked(home):
    (home / "actual").mkdir()
    (home / "private").symlink_to(home / "actual", target_is_directory=True)
    assert plan(home)["status"] == "blocked"


def test_confirmed_install_atomic_and_discoverable(home, fixture_manifest, monkeypatch):
    monkeypatch.setattr(git, "download_git", fake_download(fixture_manifest))
    result = git.execute_git_runtime(plan(home), confirmed=True)
    assert result["status"] == "completed", result
    assert git.find_managed_git(home / "private/git") == result["git"]
    assert not list((home / "private").glob(".mingit-stage-*"))
    assert git.discover_git(explicit=result["git"]) == result["git"]
    with pytest.raises(ValueError, match="does not exist"):
        git.discover_git(explicit=home / "missing")


def test_unmarked_git_is_not_discovered(home):
    executable = home / "cmd/git.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"unrelated")
    assert git.find_managed_git(home) is None


def test_no_confirmation_no_download(home, monkeypatch):
    monkeypatch.setattr(git, "download_git", lambda *args: pytest.fail("network"))
    assert git.execute_git_runtime(plan(home))["status"] == "failed"
    assert not (home / "private").exists()


def test_plan_tampering_rejected(home):
    value = plan(home)
    value["artifact"]["url"] = "https://evil.example/a.zip"
    assert git.execute_git_runtime(value, confirmed=True)["status"] == "failed"


def test_parent_changed_after_plan(home):
    (home / "private").mkdir()
    value = plan(home)
    (home / "private").rename(home / "old")
    (home / "private").mkdir()
    assert git.execute_git_runtime(value, confirmed=True)["status"] == "failed"


def test_destination_appearing_after_plan_is_preserved(home):
    value = plan(home)
    (home / "private/git").mkdir(parents=True)
    assert git.execute_git_runtime(value, confirmed=True)["status"] == "failed"
    assert (home / "private/git").is_dir()


def test_failure_cleans_staging_allows_retry(home, fixture_manifest, monkeypatch):
    value = plan(home)
    monkeypatch.setattr(git, "download_git", fake_download(b"corrupt"))
    assert git.execute_git_runtime(value, confirmed=True)["status"] == "failed"
    assert not (home / "private/git").exists()
    assert not list((home / "private").glob(".mingit-stage-*"))
    monkeypatch.setattr(git, "download_git", fake_download(fixture_manifest))
    assert git.execute_git_runtime(value, confirmed=True)["status"] == "completed"


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "C:/escape",
        "cmd\\escape",
        "cmd/file:stream",
        "cmd/NUL.txt",
        "cmd/trailing.",
    ],
)
def test_archive_paths_rejected(home, name):
    archive = home / "test.zip"
    archive.write_bytes(archive_bytes([(name, b"x")]))
    with pytest.raises(ValueError, match="Unsafe"):
        git._extract_zip(archive, home / "extract", threading.Event())
    assert not (home / "extract").exists()


def test_symlink_archive_rejected(home):
    archive = home / "test.zip"
    with zipfile.ZipFile(archive, "w") as output:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(info, "../../escape")
    with pytest.raises(ValueError, match="Unsafe"):
        git._extract_zip(archive, home / "extract", threading.Event())


def test_case_alias_and_expanded_limit(home):
    archive = home / "test.zip"
    archive.write_bytes(archive_bytes([("cmd/A", b"x"), ("CMD/a", b"x")]))
    with pytest.raises(ValueError, match="Duplicate"):
        git._extract_zip(archive, home / "extract", threading.Event())
    archive.write_bytes(archive_bytes())
    with pytest.raises(ValueError, match="size bound"):
        git._extract_zip(archive, home / "extract", threading.Event(), max_bytes=1)


class Response(io.BytesIO):
    def geturl(self):
        return "https://release-assets.githubusercontent.com/signed-asset?token=secret"


def test_download_hash_size_and_proxy_redaction(home, fixture_manifest, monkeypatch):
    captured = []

    class Opener:
        def open(self, request, timeout):
            captured.append(request.full_url)
            return Response(fixture_manifest)

    monkeypatch.setattr(git, "build_opener", lambda *args: Opener())
    artifact = git.git_runtime_manifest()["artifacts"]["x86_64"].copy()
    dest = home / "download.zip"
    git.download_git(artifact, dest, ProxyConfig(), threading.Event(), lambda e: None)
    assert dest.read_bytes() == fixture_manifest
    dest.unlink()
    artifact["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256"):
        git.download_git(artifact, dest, ProxyConfig(), threading.Event(), lambda e: None)
    assert not dest.exists()
    with pytest.raises(ValueError, match="size bound"):
        git.download_git(
            artifact, dest, ProxyConfig(), threading.Event(), lambda e: None, max_bytes=1
        )
    assert not dest.exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a",
        "https://evil.example/a",
        "https://github.com@evil.example/a",
        "https://github.com:444/a",
    ],
)
def test_redirect_allowlist(url):
    with pytest.raises(ValueError):
        git._official_url(url)


def test_cancel_before_download(home):
    event = threading.Event()
    event.set()
    result = git.execute_git_runtime(plan(home), confirmed=True, cancel_event=event)
    assert result["status"] == "cancelled"
    assert not (home / "private").exists()


def test_transient_proxy_secrets_never_enter_plan_or_errors(home, monkeypatch):
    proxy = ProxyConfig(mode="http", url="http://alice:verysecret@127.0.0.1:7890")
    value = git.plan_git_runtime(home / "git", proxy, platform_name="win32", machine="amd64")
    assert "verysecret" not in str(value)

    def fail(*args):
        raise OSError("failed http://alice:verysecret@127.0.0.1:7890")

    monkeypatch.setattr(git, "download_git", fail)
    result = git.execute_git_runtime(value, confirmed=True, proxy=proxy)
    assert result["status"] == "failed"
    assert "verysecret" not in str(result)
    assert not (home / "git").exists()


def test_download_cancelled_removes_partial_file(home, fixture_manifest, monkeypatch):
    event = threading.Event()

    class Opener:
        def open(self, request, timeout):
            return Response(fixture_manifest)

    monkeypatch.setattr(git, "build_opener", lambda *args: Opener())
    destination = home / "download.zip"
    with pytest.raises(InterruptedError):
        git.download_git(
            git.git_runtime_manifest()["artifacts"]["x86_64"],
            destination,
            ProxyConfig(),
            event,
            lambda e: event.set(),
        )
    assert not destination.exists()


def test_destination_created_during_download_is_preserved(home, fixture_manifest, monkeypatch):
    value = plan(home)

    def download(*args):
        fake_download(fixture_manifest)(*args)
        target = Path(value["directory"])
        target.mkdir()
        (target / "unrelated").write_text("keep")

    monkeypatch.setattr(git, "download_git", download)
    assert git.execute_git_runtime(value, confirmed=True)["status"] == "failed"
    assert (Path(value["directory"]) / "unrelated").read_text() == "keep"


def test_missing_executable_never_publishes(home, monkeypatch):
    monkeypatch.setattr(
        git, "download_git", fake_download(archive_bytes([("README.txt", b"missing")]))
    )
    assert git.execute_git_runtime(plan(home), confirmed=True)["status"] == "failed"
    assert not (home / "private/git").exists()


def test_download_does_not_delete_preexisting_file(home, fixture_manifest, monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return Response(fixture_manifest)

    monkeypatch.setattr(git, "build_opener", lambda *args: Opener())
    destination = home / "existing.zip"
    destination.write_bytes(b"unrelated")
    with pytest.raises(FileExistsError):
        git.download_git(
            git.git_runtime_manifest()["artifacts"]["x86_64"],
            destination,
            ProxyConfig(),
            threading.Event(),
            lambda event: None,
        )
    assert destination.read_bytes() == b"unrelated"
