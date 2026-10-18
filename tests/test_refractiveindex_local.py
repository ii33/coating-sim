"""Offline reference-store contract: original data survives, imports stay explicit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from coat_sim.core.refractiveindex_local import (
    RefractiveIndexLocalStore, RiiLocalError, build_from_directory, default_store_root, _yaml,
)
from coat_sim.core import refractiveindex_local as local_rii

COMMIT = "a" * 40


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _document(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"REFERENCES": "Original reference https://example.invalid/paper",
            "COMMENTS": "Sample-specific film; do not execute <script>anything</script>",
            "CONDITIONS": {"temperature": 293, "direction": "o", "substrate": "specific glass"},
            "DATA": blocks}


def _make_source(root: Path) -> Path:
    root.mkdir()
    (root / "LICENSE").write_text("CC0 1.0 Universal\nTest fixture only\n", encoding="utf-8")
    (root / "README.md").write_text("Synthetic offline fixture, not experimental data", encoding="utf-8")
    documents = {
        "nk.yml": _document([{"type": "tabulated nk", "data": ".4 1.5 .01\n.45 1.6 .02\n.5 1.7 .03\n.6 1.8 .04\n.7 1.9 .05"}]),
        "n.yml": _document([{"type": "tabulated n", "data": ".4 1.5\n.7 1.6"}]),
        "formula.yml": _document([
            {"type": "formula 5", "wavelength_range": ".4 .7", "coefficients": "1.5 .001 -2"},
            {"type": "tabulated k", "data": ".45 .01\n.65 .02"}]),
        "k.yml": _document([{"type": "tabulated k", "data": ".4 .01\n.7 .02"}]),
        "n2.yml": _document([{"type": "tabulated n2", "data": ".4 1e-20\n.7 2e-20"}]),
        "mixed.yml": _document([
            {"type": "formula 5", "wavelength_range": ".4 .7", "coefficients": "1.5"},
            {"type": "tabulated nk", "data": ".4 1.6 .01\n.7 1.7 .02"}]),
        "independent.yml": _document([
            {"type": "tabulated n", "data": ".4 1.5\n.7 1.6"},
            {"type": "tabulated k", "data": ".45 .01\n.65 .02"}]),
        "bad.yml": _document([{"type": "tabulated nk", "data": ".4 1.5 .01\n.4 1.6 .02\n.7 1.7 .03"}]),
        "unknown.yml": _document([{"type": "future epsilon", "data": ".4 2\n.7 3"}]),
        "orphan.yml": _document([{"type": "tabulated n", "data": ".4 1.2\n.7 1.3"}]),
    }
    nk_pages = []
    for filename, document in documents.items():
        _write(root / "database/data/main/X" / filename, document)
        if filename not in ("orphan.yml", "n2.yml"):
            nk_pages.append({"PAGE": filename[:-4], "name": filename[:-4] + " sample", "data": "main/X/" + filename})
    nk_pages.append({"PAGE": "nk", "name": "duplicate navigation", "data": "main//X/nk.yml"})
    _write(root / "database/catalog-nk.yml", [{"SHELF": "main", "name": "Main", "content": [
        {"DIVIDER": "test divider"}, {"BOOK": "X", "name": "X material", "content": nk_pages}]}])
    _write(root / "database/catalog-n2.yml", [{"SHELF": "main", "name": "Main n2", "content": [
        {"BOOK": "X", "name": "X nonlinear", "content": [{"PAGE": "n2", "name": "nonlinear sample", "data": "main/X/n2.yml"}]}]}])
    _write(root / "database/data/main/X/about.yml", {"ABOUT": "Reference material description"})
    return root


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Path, Path, RefractiveIndexLocalStore]:
    source = _make_source(tmp_path / "source")
    root = tmp_path / "store"
    build_from_directory(source, root, upstream_commit=COMMIT)
    return source, root, RefractiveIndexLocalStore(root)


def _id(store: RefractiveIndexLocalStore, filename: str) -> str:
    rows = store.search("", catalog="all")["entries"]
    return next(row["entry_id"] for row in rows if row["source_path"] == "main/X/" + filename)


def test_missing_status_and_invalid_parameters_never_create_store(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    store = RefractiveIndexLocalStore(root)
    assert store.status()["status"] == "missing"
    with pytest.raises(RiiLocalError, match="installed") as caught:
        store.search()
    assert caught.value.code == "STORE_MISSING"
    assert not root.exists()
    for arguments in ({"limit": True}, {"limit": 101}, {"offset": -1}, {"catalog": "bad"},
                      {"capability": "calculable"}, {"cataloged": 1}, {"query": "x" * 201}):
        with pytest.raises(RiiLocalError) as error:
            store.search(**arguments)
        assert error.value.code == "INVALID_QUERY"


def test_default_location_is_explicit_and_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", str(tmp_path / "trusted"))
    assert default_store_root() == tmp_path / "trusted"
    assert not (tmp_path / "trusted").exists()
    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", "relative/store")
    with pytest.raises(RiiLocalError) as caught:
        default_store_root()
    assert caught.value.code == "INVALID_STORE_PATH"


def test_relative_windows_localappdata_falls_back_without_mkdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_rii, "os", SimpleNamespace(name="nt", environ={"LOCALAPPDATA": "relative/local"}))
    expected = Path.home() / "AppData/Local/CoatSim/reference-databases/refractiveindex"
    with patch.object(Path, "mkdir", side_effect=AssertionError("path lookup must not write")):
        assert default_store_root() == expected


def test_true_counts_classification_duplicates_and_uncatalogued_data(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    _source, _root, store = installed
    status = store.status()
    assert status["counts"]["indexed_entries"] == 10
    assert status["counts"]["nk"] == 9
    assert status["counts"]["n2"] == 1
    assert status["counts"]["catalog_occurrences"] == 10
    assert status["counts"]["cataloged"] == 9
    assert status["counts"]["uncatalogued"] == 1
    assert status["upstream_commit_verified"] is False
    page = store.search("", catalog="all", limit=3)
    next_page = store.search("", catalog="all", offset=3, limit=3)
    assert page["total"] == 10 and page["has_more"]
    assert not set(row["entry_id"] for row in page["entries"]) & set(row["entry_id"] for row in next_page["entries"])
    item = store.get_entry(_id(store, "nk.yml"))
    assert len(item["classifications"]) == 2
    assert item["entry_id"] == "rii_" + hashlib.sha256(b"main/X/nk.yml").hexdigest()[:24]
    assert store.search(cataloged=False)["entries"][0]["source_path"].endswith("orphan.yml")
    assert store.search(shelf="main", book="X")["total"] == 8
    assert store.search("SPECIFIC GLASS")["total"] == 9
    assert store.search("%_'")["total"] == 0


def test_lossless_is_not_inferred_and_source_bytes_remain_exact(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    source, root, store = installed
    entry_id = _id(store, "n.yml")
    detail = store.get_entry(entry_id, raw_limit=20)
    assert detail["raw_yaml"]["truncated"] and len(detail["raw_yaml"]["text"]) == 20
    assert detail["conditions"]["temperature"] == 293
    result = store.convert_entry(entry_id, optical_set_id="selected_n")
    assert result["optical_set"]["k"] is None
    assert result["missing"] and result["assumptions"] == []
    assert result["provenance"]["entry_id"] == entry_id
    assert result["provenance"]["snapshot_id"] == store.snapshot_id
    raw_path = Path("database/data/main/X/n.yml")
    assert (root / "versions" / str(store.snapshot_id) / "source" / raw_path).read_bytes() == (source / raw_path).read_bytes()
    assert result["production_validation"] == "not_established"


def test_formula_independent_k_preserves_separate_domain(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    store = installed[2]
    result = store.convert_entry(_id(store, "formula.yml"), optical_set_id="formula")
    candidate = result["optical_set"]
    assert candidate["kind"] == "rii_formula" and candidate["formula_number"] == 5
    assert candidate["wavelength_min"] == .4 and candidate["wavelength_max"] == .7
    assert candidate["k_data"]["wavelength_min"] == .45 and candidate["k_data"]["wavelength_max"] == .65
    assert result["provenance"]["block_selection"] == [0, 1]


def test_multiple_representations_require_selection_and_keep_exclusions(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    store = installed[2]
    entry_id = _id(store, "mixed.yml")
    assert store.get_entry(entry_id)["conversion"]["status"] == "selection_required"
    with pytest.raises(RiiLocalError) as caught:
        store.convert_entry(entry_id, optical_set_id="mixed")
    assert caught.value.code == "BLOCK_SELECTION_REQUIRED"
    result = store.convert_entry(entry_id, optical_set_id="chosen", block_selection=[1])
    assert result["optical_set"]["n"] == [1.6, 1.7]
    assert result["provenance"]["excluded_blocks"] == [0] and result["warnings"]
    for selection in ([], [0, 0], [True], [99]):
        with pytest.raises(RiiLocalError):
            store.convert_entry(entry_id, optical_set_id="bad", block_selection=selection)


@pytest.mark.parametrize("filename,code", [
    ("k.yml", "MISSING_N"), ("n2.yml", "NONLINEAR_N2_REFERENCE_ONLY"),
    ("independent.yml", "INDEPENDENT_TABLE_GRIDS"), ("bad.yml", "OPTICAL_VALIDATION_FAILED"),
    ("unknown.yml", "UNSUPPORTED_DATA_TYPE"),
])
def test_unsupported_invalid_and_n2_remain_searchable_without_inventing_values(
        installed: tuple[Path, Path, RefractiveIndexLocalStore], filename: str, code: str) -> None:
    store = installed[2]
    entry_id = _id(store, filename)
    assert store.get_entry(entry_id)["conversion"]["status"] != "supported"
    with pytest.raises(RiiLocalError) as caught:
        store.convert_entry(entry_id, optical_set_id="chosen")
    assert caught.value.code == code


def test_preview_is_bounded_and_not_used_as_import_data(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    store = installed[2]
    entry_id = _id(store, "nk.yml")
    preview = store.preview(entry_id, max_points=3)["blocks"][0]
    assert preview["source_point_count"] == 5 and preview["display_point_count"] == 3
    assert preview["sampled"] and not preview["calculated"]
    assert preview["points"] == [[.4, 1.5, .01], [.5, 1.7, .03], [.7, 1.9, .05]]
    assert len(store.convert_entry(entry_id, optical_set_id="whole")["optical_set"]["n"]) == 5
    formula = store.preview(_id(store, "formula.yml"), block_indices=[0], max_points=5)["blocks"][0]
    assert formula["calculated"] and formula["quantity"] == "n"
    nonlinear = store.preview(_id(store, "n2.yml"))["blocks"][0]
    assert nonlinear["quantity"] == "n2" and nonlinear["points"][0][1] == 1e-20
    assert store.preview(_id(store, "unknown.yml"))["blocks"][0]["status"] == "unsupported"
    for count in (1, 1001, True):
        with pytest.raises(RiiLocalError):
            store.preview(entry_id, max_points=count)


def test_reader_pins_old_snapshot_after_explicit_atomic_update(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    source, root, old = installed
    old_id = old.snapshot_id
    path = source / "database/data/main/X/n.yml"
    document = json.loads(path.read_text())
    document["COMMENTS"] = "Changed source for explicit new version"
    _write(path, document)
    result = build_from_directory(source, root, upstream_commit=COMMIT)
    assert result["snapshot_id"] != old_id
    assert old.snapshot_id == old_id
    assert "Changed source" not in old.get_entry(_id(old, "n.yml"))["comments"]
    current = RefractiveIndexLocalStore(root)
    assert "Changed source" in current.get_entry(_id(current, "n.yml"))["comments"]
    assert (root / "versions" / str(old_id)).is_dir()


def test_no_activate_idempotent_rebuild_and_no_upstream_script_copy(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    source, root, store = installed
    (source / "run_me.py").write_text("raise RuntimeError('must never execute')", encoding="utf-8")
    same = build_from_directory(source, root, upstream_commit=COMMIT)
    assert same["snapshot_id"] == store.snapshot_id
    assert not (root / "versions" / str(store.snapshot_id) / "source/run_me.py").exists()
    other = build_from_directory(source, root, upstream_commit="b" * 40, activate=False)
    assert other["activated"] is False
    assert RefractiveIndexLocalStore(root).snapshot_id == store.snapshot_id
    assert RefractiveIndexLocalStore(root, snapshot_id=other["snapshot_id"]).status()["status"] == "ready"


def test_failed_cancelled_or_unsafe_update_keeps_current(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    source, root, store = installed
    before = (root / "CURRENT.json").read_bytes()
    with pytest.raises(RiiLocalError) as caught:
        build_from_directory(source, root, upstream_commit=COMMIT, cancel_check=lambda: True)
    assert caught.value.code == "CANCELLED"
    assert (root / "CURRENT.json").read_bytes() == before
    catalog = source / "database/catalog-nk.yml"
    data = json.loads(catalog.read_text())
    data[0]["content"][1]["content"][0]["data"] = "../../outside.yml"
    _write(catalog, data)
    with pytest.raises(RiiLocalError) as caught:
        build_from_directory(source, root, upstream_commit=COMMIT)
    assert caught.value.code == "UNSAFE_SOURCE_PATH"
    assert (root / "CURRENT.json").read_bytes() == before
    assert RefractiveIndexLocalStore(root).snapshot_id == store.snapshot_id
    assert not list((root / "versions").glob(".building-*"))


def test_source_and_index_tampering_are_detected(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    _source, root, store = installed
    entry_id = _id(store, "n.yml")
    source_file = root / "versions" / str(store.snapshot_id) / "source/database/data/main/X/n.yml"
    source_file.write_bytes(source_file.read_bytes() + b"\n")
    with pytest.raises(RiiLocalError) as caught:
        store.get_entry(entry_id)
    assert caught.value.code == "SOURCE_INTEGRITY_ERROR"
    index = root / "versions" / str(store.snapshot_id) / "index.sqlite3"
    with index.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(RiiLocalError) as caught:
        RefractiveIndexLocalStore(root)
    assert caught.value.code == "INVALID_STORE"


def test_readers_are_sqlite_read_only_and_never_network(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    store = installed[2]
    connection = store._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM entries")
    finally:
        connection.close()
    with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
        assert store.search("sample")["total"] > 0
        entry_id = _id(store, "nk.yml")
        store.get_entry(entry_id)
        store.preview(entry_id)
        store.convert_entry(entry_id, optical_set_id="offline")


@pytest.mark.parametrize("text", [
    "a: 1\na: 2", "a: &x [1,2]\nb: *x", "a: !!python/object/apply:os.system ['bad']",
    "a: .nan", "a: " + "[" * 34 + "0" + "]" * 34,
])
def test_yaml_rejects_duplicate_keys_aliases_tags_nonfinite_and_excess_depth(text: str) -> None:
    with pytest.raises(RiiLocalError):
        _yaml(text.encode())


def test_symlink_and_nonofficial_source_url_rejected(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    (source / "database/data/leak.yml").symlink_to(source / "LICENSE")
    with pytest.raises(RiiLocalError) as caught:
        build_from_directory(source, tmp_path / "store", upstream_commit=COMMIT)
    assert caught.value.code == "UNSAFE_SOURCE_PATH"
    with pytest.raises(RiiLocalError) as caught:
        build_from_directory(source, tmp_path / "store", upstream_commit=COMMIT, source_url="https://untrusted.invalid")
    assert caught.value.code == "INVALID_SOURCE_URL"


def test_symlink_store_versions_rejected_before_any_external_write(tmp_path: Path) -> None:
    source = _make_source(tmp_path / "source")
    root, outside = tmp_path / "store", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "versions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RiiLocalError) as caught:
        build_from_directory(source, root, upstream_commit=COMMIT)
    assert caught.value.code == "UNSAFE_STORE_PATH"
    assert not list(outside.iterdir()) and not (root / "CURRENT.json").exists()


def test_damaged_existing_version_never_replaces_healthy_current(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    source, root, old = installed
    healthy = build_from_directory(source, root, upstream_commit="b" * 40)
    before = (root / "CURRENT.json").read_bytes()
    manifest_path = root / "versions" / str(old.snapshot_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("counts")
    _write(manifest_path, manifest)
    with pytest.raises(RiiLocalError) as caught:
        build_from_directory(source, root, upstream_commit=COMMIT)
    assert caught.value.code == "INVALID_STORE"
    assert (root / "CURRENT.json").read_bytes() == before
    assert RefractiveIndexLocalStore(root).snapshot_id == healthy["snapshot_id"]


@pytest.mark.parametrize("manifest_value", [[], None, 0, {"schema_version": 1}])
def test_malformed_manifest_has_structured_error(
        installed: tuple[Path, Path, RefractiveIndexLocalStore], manifest_value: Any) -> None:
    _source, root, store = installed
    _write(root / "versions" / str(store.snapshot_id) / "manifest.json", manifest_value)
    with pytest.raises(RiiLocalError) as caught:
        RefractiveIndexLocalStore(root)
    assert caught.value.code == "INVALID_STORE"


def test_actual_copied_bytes_are_bounded_despite_source_growth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _make_source(tmp_path / "source")
    initial_bytes = sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
    monkeypatch.setattr(local_rii, "MAX_TOTAL_BYTES", initial_bytes + 100)
    changed = False

    def grow_once() -> bool:
        nonlocal changed
        if not changed:
            (source / "README.md").write_text("x" * 1000, encoding="utf-8")
            changed = True
        return False

    root = tmp_path / "store"
    with pytest.raises(RiiLocalError) as caught:
        build_from_directory(source, root, upstream_commit=COMMIT, cancel_check=grow_once)
    assert caught.value.code == "SOURCE_LIMIT"
    assert not (root / "CURRENT.json").exists()


def test_table_limit_stops_before_parsing_extra_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    real_numbers = local_rii._numbers
    calls = 0

    def counted(value: Any, label: str) -> list[float]:
        nonlocal calls
        calls += 1
        return real_numbers(value, label)

    monkeypatch.setattr(local_rii, "_numbers", counted)
    with pytest.raises(RiiLocalError) as caught:
        local_rii._table({"type": "tabulated n", "data": "1 2\n" * 100002})
    assert caught.value.code == "SOURCE_LIMIT" and calls == 100000


def test_utf16_is_rejected_at_parse_boundary() -> None:
    with pytest.raises(RiiLocalError) as caught:
        _yaml("DATA: []".encode("utf-16"))
    assert caught.value.code == "INVALID_YAML"


def test_reference_text_is_bounded_with_visible_truncation(installed: tuple[Path, Path, RefractiveIndexLocalStore]) -> None:
    source, root, _store = installed
    path = source / "database/data/main/X/n.yml"
    document = json.loads(path.read_text())
    document["REFERENCES"] = "r" * 21000
    document["COMMENTS"] = "c" * 21000
    _write(path, document)
    build_from_directory(source, root, upstream_commit=COMMIT)
    store = RefractiveIndexLocalStore(root)
    entry_id = _id(store, "n.yml")
    result = store.get_entry(entry_id)
    assert len(result["references"]) == 20000 and result["references_truncated"]
    assert len(result["comments"]) == 20000 and result["comments_truncated"]
    with pytest.raises(RiiLocalError) as caught:
        store.convert_entry(entry_id, optical_set_id="too_large")
    assert caught.value.code == "PROVENANCE_TOO_LARGE"


@pytest.mark.parametrize("number,coefficients", [
    (1, "1"), (2, "1"), (3, "2"), (4, "2 0 0 .1 2 0 0 2 2"), (5, "1.5"),
    (6, ".0001"), (7, "1.5"), (8, ".2 .01 .01 0"), (9, "2 .01 .01 .01 1 .5"),
])
def test_all_nine_formula_types_survive_original_coefficients_and_preview(
        tmp_path: Path, number: int, coefficients: str) -> None:
    source = _make_source(tmp_path / "source")
    _write(source / "database/data/main/X/formula.yml", _document([
        {"type": f"formula {number}", "wavelength_range": ".4 .7", "coefficients": coefficients}]))
    root = tmp_path / "store"
    build_from_directory(source, root, upstream_commit=COMMIT)
    store = RefractiveIndexLocalStore(root)
    entry_id = _id(store, "formula.yml")
    converted = store.convert_entry(entry_id, optical_set_id="formula")["optical_set"]
    assert converted["formula_number"] == number
    assert converted["coefficients"] == [float(value) for value in coefficients.split()]
    assert store.preview(entry_id, max_points=5)["blocks"][0]["status"] == "ok"
