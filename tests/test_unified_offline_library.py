"""Unified offline discovery, truthful availability, provenance and assistant access."""
from __future__ import annotations

import asyncio
import hashlib
from io import StringIO
import json
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent import research_library, worker
from coat_sim.agent.runtime import allowed_project_tools
from coat_sim.core import refractiveindex_local as rii
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.unified_reference_library import UnifiedReferenceLibrary
from coat_sim.mcp import server
from coat_sim.refractiveindex_cli import main as cli
from tests.test_refractiveindex_tools_integration import sources as sources, tree_content

pytestmark = pytest.mark.contract
TOOLS = {"library_status", "library_search", "library_read"}


@pytest.fixture
def offline(sources, tmp_path, monkeypatch):
    source, store = sources
    snapshot = rii.build_from_directory(source, store, upstream_commit="a" * 40)["snapshot_id"]
    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", str(store))
    bundle = tmp_path / "research"
    bundle.mkdir()
    payload = b"# Fixture research\nFixture evidence and original conditions.\n"
    (bundle / "fixture.md").write_bytes(payload)
    (bundle / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "version": "fixture-1", "coverage_note": "Synthetic fixture, not scientific evidence.",
        "documents": [{"path": "fixture.md", "sha256": hashlib.sha256(payload).hexdigest(),
                       "source_url": "https://example.invalid/original", "source_commit": "b" * 40,
                       "source_path": "original/fixture.md", "source_blob_sha": "c" * 40,
                       "license": "Fixture-only rights", "evidence_scope": "Synthetic test only"}]}), encoding="utf-8")
    monkeypatch.setattr(research_library, "builtin_research_library_root", lambda: bundle)
    monkeypatch.setattr(worker, "_research_library_sidecar_path", lambda: tmp_path / "absent.json")
    return source, store, snapshot, bundle


def read_all(owner, path):
    pages, offset = [], 0
    while True:
        result = owner.read(path, offset=offset, max_chars=6000)
        assert result["ok"]
        pages.append(result["content"])
        offset = result["next_offset"]
        if offset is None:
            return "".join(pages)


def test_mixed_source_pagination_and_original_conversion_are_read_only(offline):
    _source, store, snapshot, _bundle = offline
    before = tree_content(store)
    owner = UnifiedReferenceLibrary()
    first = owner.search("fixture", limit=1)
    rows, page = list(first["results"]), first
    while page["next_offset"] is not None:
        page = UnifiedReferenceLibrary().search("fixture", offset=page["next_offset"], limit=1,
                                                expected_version=first["inventory_version"])
        rows.extend(page["results"])
    assert len(rows) == first["total_matches"] == 4
    assert len({row["source"]["path"] for row in rows}) == 4
    assert {row["source"]["availability"] for row in rows} == {"packaged_text", "installed_data"}
    note = next(row for row in rows if row["source"]["availability"] == "packaged_text")
    assert note["source"]["license"] == "Fixture-only rights"
    assert note["source"]["source_commit"] == "b" * 40
    read = server.library_read(**note["read_arguments"])
    assert read["source"]["sha256"] == note["source"]["sha256"]
    assert "Fixture evidence" in read["content"]
    n_only = next(row for row in rows if row["source"].get("source_path", "").endswith("nk/n-only.yml"))
    detail = json.loads(read_all(owner, n_only["source"]["path"]))
    assert detail["conditions"] == {"temperature": 300}
    assert detail["license"] == "CC0-1.0" and detail["source_sha256"] == n_only["source"]["source_sha256"]
    assert detail["blocks"][0]["range_um"] == [0.4, 0.7]
    assert n_only["source"]["wavelength_unit"] == "um"
    converted = server.refractiveindex_convert(**n_only["selection"]["arguments"], optical_set_id="chosen")
    assert converted["optical_set"]["n"] == [1.5, 1.6, 1.7]
    assert converted["optical_set"]["k"] is None
    assert converted["provenance"]["snapshot_id"] == snapshot
    assert converted["missing"] and not converted["assumptions"]
    assert tree_content(store) == before


def test_materials_and_reference_only_metadata_are_not_conflated(offline):
    owner = UnifiedReferenceLibrary()
    air = owner.search("builtin_ideal_air")["results"][0]
    record = json.loads(read_all(owner, air["source"]["path"]))
    assert record["availability"] == "packaged_material_data" and record["record"]["id"] == "builtin_ideal_air"
    zns = next(row for row in owner.search("ZnS", limit=10)["results"]
               if row["source"]["availability"] == "packaged_material_data")
    assert all(item["wavelength_unit"] in {"um", "nm"} for item in zns["source"]["optical_sets"])
    source = next(row for row in owner.search("Palik")["results"]
                  if row["source"]["availability"] == "reference_only_not_builtin")
    metadata = json.loads(read_all(owner, source["source"]["path"]))
    assert metadata["metadata_only"] and metadata["data_bundled"] is False
    assert metadata["source"]["rights"] == source["source"]["rights"]
    assert metadata["source"]["links"]


def test_missing_and_corrupt_rii_do_not_hide_packaged_text_or_create_storage(offline, tmp_path, monkeypatch):
    _source, store, snapshot, _bundle = offline
    missing = tmp_path / "not-installed"
    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", str(missing))
    owner = UnifiedReferenceLibrary()
    assert owner.status()["sources"]["rii"]["status"] == "missing"
    assert owner.search("fixture")["results"]
    assert not missing.exists()
    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", str(store))
    with (store / "versions" / snapshot / "index.sqlite3").open("ab") as stream:
        stream.write(b"changed-index")
    owner = UnifiedReferenceLibrary()
    result = owner.search("fixture")
    assert result["results"] and result["incomplete"]
    assert any(failure["source"] == "rii" for failure in result["failures"])
    assert owner.status()["sources"]["rii"]["status"] == "unavailable"


def test_changed_inventory_and_damaged_text_never_return_invented_content(offline):
    _source, _store, _snapshot, bundle = offline
    initial = UnifiedReferenceLibrary().search("fixture")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = "fixture-2"
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ProjectError, match="changed"):
        UnifiedReferenceLibrary().search("fixture", expected_version=initial["inventory_version"])
    (bundle / "fixture.md").write_text("CORRUPTED", encoding="utf-8")
    result = UnifiedReferenceLibrary().search("fixture")
    assert result["incomplete"] and any(row["source"] == "research" for row in result["failures"])
    assert all("CORRUPTED" not in row["excerpt"] for row in result["results"])


def test_assistant_default_uses_unified_provider_and_live_revocation(offline):
    provider, state, _, _ = worker._configured_research_library({})
    assert isinstance(provider, UnifiedReferenceLibrary) and state == "connected_builtin_read_only"
    tools = {tool.__name__: tool for tool in research_library.assistant_tools(provider, allow_model_context=True)}
    assert any(row["source"]["availability"] == "installed_data"
               for row in tools["research_library_search"]("fixture")["results"])
    readiness = research_library.status_tool(provider, allow_model_context=True)()
    assert readiness["offline_inventory"]["sources"]["rii"]["counts"]["indexed_entries"] == 3
    grants = iter([True, False])
    revoked = research_library.assistant_tools(provider, allow_model_context=True, live_grant=lambda: next(grants))[0]("fixture")
    assert revoked["error"] == "permission_revoked" and "results" not in revoked


def test_new_mcp_tools_are_closed_readonly_and_discussion_accessible():
    catalog = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert TOOLS <= allowed_project_tools("chat")
    for name in TOOLS:
        assert catalog[name].input_schema["additionalProperties"] is False
        assert catalog[name].annotations.read_only_hint is True
        assert catalog[name].annotations.destructive_hint is False


@pytest.mark.parametrize("name,arguments", [
    ("library_status", {"path": "C:/private"}),
    ("library_search", {"query": "fixture", "offset": True}),
    ("library_search", {"query": 3}),
    ("library_search", {"query": "fixture", "limit": 11}),
    ("library_search", {"query": "fixture", "root": "C:/private"}),
    ("library_read", {"path": "fixture.md", "max_chars": "6000"}),
    ("library_read", {"path": "fixture.md", "offset": False}),
])
def test_invalid_mcp_envelopes_never_reach_handlers(name, arguments):
    with patch.object(MCPServer, "call_tool", new_callable=AsyncMock) as sdk:
        with pytest.raises(ToolError, match="invalid_arguments"):
            asyncio.run(server.mcp.call_tool(name, arguments))
        sdk.assert_not_called()


@pytest.mark.parametrize("path", ["../../private.txt", "C:/private.txt", "https://example.invalid/private", "source/not-a-version/palik-handbook"])
def test_unified_reads_do_not_accept_arbitrary_paths_or_urls(offline, path):
    try:
        result = UnifiedReferenceLibrary().read(path)
    except (ValueError, OSError):
        return
    assert not result["ok"]


def test_shipped_snapshot_is_reachable_on_fresh_install_without_path_setup(tmp_path, monkeypatch):
    monkeypatch.delenv("COAT_SIM_REFRACTIVEINDEX_STORE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "fresh-local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "fresh-xdg"))
    monkeypatch.chdir(tmp_path)
    assert rii.default_store_root() == rii.packaged_store_root()
    store = rii.RefractiveIndexLocalStore(rii.default_store_root())
    status = store.status()
    assert status["counts"]["indexed_entries"] == 3720 and status["license"] == "CC0-1.0"
    owner = UnifiedReferenceLibrary()
    assert owner.status()["sources"]["rii"]["availability"] == "packaged_data"
    row = next(row for row in owner.search("Minenkov", limit=10)["results"] if row["source"]["availability"] == "packaged_data")
    detail = json.loads(read_all(owner, row["source"]["path"]))
    assert detail["snapshot_id"] == status["snapshot_id"] and detail["source_sha256"]
    assert not (tmp_path / "fresh-local").exists() and not (tmp_path / "fresh-xdg").exists()


def test_explicit_missing_override_never_falls_back_to_shipped_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "explicitly-selected"
    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", str(root))
    assert rii.default_store_root() == root
    assert server.library_status()["sources"]["rii"]["status"] == "missing"
    assert not root.exists()


def test_default_cli_build_cannot_modify_packaged_snapshot(offline, tmp_path, monkeypatch):
    source, _store, _snapshot, _bundle = offline
    target = tmp_path / "new-user-store"
    monkeypatch.setattr(rii, "default_store_root", lambda: rii.packaged_store_root())
    monkeypatch.setattr(rii, "writable_store_root", lambda: target)
    output = StringIO()
    assert cli(["build", "--source-dir", str(source), "--upstream-commit", "d" * 40], stdout=output) == 0
    assert (target / "CURRENT.json").is_file()
    assert json.loads(output.getvalue())["result"]["activated"] is True



def test_existing_cie_data_and_generated_e_preserve_source_scope(offline):
    owner = UnifiedReferenceLibrary()
    assert owner.status()["sources"]["cie"]["datasets"] == 6
    row = next(row for row in owner.search("D65", limit=10)["results"]
               if row["source"]["availability"] == "packaged_data")
    source = row["source"]
    assert source["license"] == "CC-BY-SA-4.0" and source["wavelength_unit"] == "nm"
    data = json.loads(read_all(owner, source["path"]))
    assert data["dataset"]["id"] == "D65" and data["dataset"]["sampleCount"] == 531
    assert data["dataset"]["doi"] == "10.25039/CIE.DS.hjfjmt59"
    assert "not" in data["packaging"]["attribution_notice"]
    generated = json.loads(read_all(owner, f"cie/{owner.cie.version}/E"))
    assert "generated" in json.dumps(generated["dataset"]).casefold()
    assert generated["dataset"].get("doi") is None


def test_cie_integrity_failure_is_partial_and_does_not_read_arbitrary_files(offline, tmp_path, monkeypatch):
    from coat_sim.core import offline_reference_assets as assets
    import shutil
    root = tmp_path / "cie"
    root.mkdir()
    (root / "data").mkdir()
    original = assets.cie_reference_root()
    for relative in ("MANIFEST_SHA256.json", "NOTICE_DATA.md", "data/cie_illuminants.json"):
        shutil.copyfile(original / relative, root / relative)
    monkeypatch.setattr(assets, "cie_reference_root", lambda: root)
    assert UnifiedReferenceLibrary().status()["sources"]["cie"]["datasets"] == 6
    (root / "data/cie_illuminants.json").write_text("changed", encoding="utf-8")
    result = UnifiedReferenceLibrary().search("fixture")
    assert result["results"] and result["incomplete"]
    assert any(failure["source"] == "cie" for failure in result["failures"])



def test_returned_shipped_snapshot_stays_readable_after_new_user_install(offline, tmp_path, monkeypatch):
    source, _store, _snapshot, _bundle = offline
    user = tmp_path / "later-user-install"
    monkeypatch.delenv("COAT_SIM_REFRACTIVEINDEX_STORE", raising=False)
    monkeypatch.setattr(rii, "writable_store_root", lambda: user)
    initial = UnifiedReferenceLibrary()
    row = next(row for row in initial.search("Minenkov", limit=10)["results"]
               if row["source"]["availability"] == "packaged_data")
    shipped_snapshot = row["source"]["snapshot_id"]
    installed = rii.build_from_directory(source, user, upstream_commit="e" * 40)
    assert rii.default_store_root() == user and installed["snapshot_id"] != shipped_snapshot
    old = json.loads(read_all(UnifiedReferenceLibrary(), row["source"]["path"]))
    assert old["snapshot_id"] == shipped_snapshot and old["availability"] == "packaged_data"
    detail = server.refractiveindex_detail(row["source"]["entry_id"], snapshot_id=shipped_snapshot)
    assert detail["source_sha256"] == row["source"]["source_sha256"]
