"""CLI reads never build; local snapshot construction requires an explicit command."""
from io import StringIO
import json
from pathlib import Path
from typing import Any

import pytest

from coat_sim.refractiveindex_cli import main

pytestmark = pytest.mark.contract

SNAPSHOT = "a" * 40 + "-" + "b" * 16 + "-v1"
ENTRY = "rii_" + "c" * 24


@pytest.fixture
def owner(tmp_path, monkeypatch):
    from coat_sim.core import refractiveindex_local

    calls: list[tuple[Any, ...]] = []
    default = tmp_path / "per-user-reference-store"

    class Store:
        def __init__(self, root, snapshot_id=None):
            calls.append(("open", Path(root), snapshot_id))

        def __getattr__(self, method):
            assert method in {"status", "search", "get_entry", "preview", "convert_entry"}

            def invoke(*args, **kwargs):
                calls.append((method, args, kwargs))
                return {"snapshot_id": SNAPSHOT, "method": method}

            return invoke

    def build(source_dir, store_root, **kwargs):
        calls.append(("build", source_dir, store_root, kwargs))
        return {"snapshot_id": SNAPSHOT, "activated": kwargs["activate"]}

    monkeypatch.setattr(refractiveindex_local, "default_store_root", lambda: default)
    monkeypatch.setattr(refractiveindex_local, "RefractiveIndexLocalStore", Store)
    monkeypatch.setattr(refractiveindex_local, "build_from_directory", build)
    return calls, default


def invoke(arguments):
    stdout = StringIO()
    code = main(arguments, stdout=stdout)
    text = stdout.getvalue()
    assert text.endswith("\n")
    assert len(text.splitlines()) == 1
    return code, json.loads(text)


@pytest.mark.parametrize("arguments,method,positional,keywords", [
    (["status"], "status", (), {}),
    (["search"], "search", ("",), {"catalog": "nk", "shelf": None, "book": None,
        "capability": None, "cataloged": None, "offset": 0, "limit": 50}),
    (["search", "--query", "glass", "--catalog", "all", "--uncataloged", "--limit", "2"],
        "search", ("glass",), {"catalog": "all", "shelf": None, "book": None,
        "capability": None, "cataloged": False, "offset": 0, "limit": 2}),
    (["detail", ENTRY, "--raw-limit", "0"], "get_entry", (ENTRY,), {"raw_limit": 0}),
    (["preview", ENTRY, "--block-indices", "0", "2", "--max-points", "10"],
        "preview", (ENTRY,), {"block_indices": [0, 2], "max_points": 10}),
    (["convert", ENTRY, "--optical-set-id", "exact", "--name", "Reference", "--block-selection", "1"],
        "convert_entry", (ENTRY,), {"optical_set_id": "exact", "name": "Reference", "block_selection": [1]}),
])
def test_reads_forward_version_and_arguments_without_any_build(owner, arguments, method, positional, keywords):
    calls, default = owner
    code, reply = invoke(["--snapshot-id", SNAPSHOT, *arguments])
    assert code == 0 and reply["ok"] is True
    assert reply["operation"] == arguments[0]
    assert reply["effect"] == "read"
    assert calls == [("open", default, SNAPSHOT), (method, positional, keywords)]
    assert not default.exists()


@pytest.mark.parametrize("activate", [True, False])
def test_explicit_build_calls_only_builder_and_does_not_fetch_metadata_url(owner, tmp_path, activate):
    calls, _ = owner
    source, target = tmp_path / "already-downloaded", tmp_path / "chosen-store"
    url = "https://github.com/polyanskiy/refractiveindex.info-database/tree/" + "a" * 40
    args = ["--store", str(target), "build", "--source-dir", str(source),
            "--upstream-commit", "a" * 40, "--source-url", url]
    if not activate:
        args.append("--no-activate")
    code, reply = invoke(args)
    assert code == 0 and reply["ok"] is True
    assert reply["effect"] == "write"
    assert calls == [("build", source, target, {"upstream_commit": "a" * 40,
        "source_url": url, "activate": activate})]


def test_snapshot_id_cannot_be_used_as_build_destination(owner):
    calls, _ = owner
    code, reply = invoke(["--snapshot-id", SNAPSHOT, "build", "--source-dir", "local",
                          "--upstream-commit", "a" * 40])
    assert code == 2 and reply["ok"] is False
    assert "--snapshot-id" in reply["error"]["message"]
    assert calls == []


@pytest.mark.parametrize("arguments", [
    ["--store", "https://example.invalid/store", "status"],
    ["--store", "file:///tmp/store", "status"],
    ["build", "--source-dir", "https://example.invalid/archive", "--upstream-commit", "a" * 40],
])
def test_urls_are_never_treated_as_local_paths_or_download_requests(owner, arguments):
    calls, _ = owner
    code, reply = invoke(arguments)
    assert code == 2 and reply["ok"] is False
    assert "local filesystem path" in reply["error"]["message"]
    assert calls == []


def test_domain_error_is_a_finite_json_error_with_nonzero_exit(owner, monkeypatch):
    from coat_sim.core import refractiveindex_local

    class Missing(ValueError):
        code = "SNAPSHOT_MISSING"

    def reject(*args, **kwargs):
        raise Missing("requested snapshot is not installed")

    monkeypatch.setattr(refractiveindex_local, "RefractiveIndexLocalStore", reject)
    code, reply = invoke(["detail", ENTRY])
    assert code == 2
    assert reply == {"ok": False, "operation": "detail", "error": {
        "code": "SNAPSHOT_MISSING", "message": "requested snapshot is not installed", "effect": "none"}}


def test_interruption_is_reported_without_retrying_the_builder(owner, monkeypatch):
    from coat_sim.core import refractiveindex_local

    calls = []

    def interrupted(*args, **kwargs):
        calls.append("build")
        raise KeyboardInterrupt

    monkeypatch.setattr(refractiveindex_local, "build_from_directory", interrupted)
    code, reply = invoke(["build", "--source-dir", "local", "--upstream-commit", "a" * 40])
    assert code == 130 and reply["error"]["code"] == "cancelled"
    assert calls == ["build"]


def test_real_missing_status_does_not_create_storage(tmp_path, monkeypatch):
    missing = tmp_path / "never-created" / "reference-store"
    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", str(missing))
    code, reply = invoke(["status"])
    assert code == 0 and reply["result"]["status"] == "missing"
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


def test_help_advertises_explicit_build_and_no_download(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["--help"])
    assert stopped.value.code == 0
    text = capsys.readouterr().out
    assert "WRITE:" in text and "build" in text and "never downloads" in text
