"""Reference metadata stays bounded, inert, versioned, and distinct from installation."""
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from coat_sim.core import reference_sources as reference
from coat_sim.core.project_dependencies import ProjectError

pytestmark = pytest.mark.contract


@pytest.fixture
def resource(tmp_path, monkeypatch):
    data = json.loads(reference.REFERENCE_SOURCES_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "reference_sources.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(reference, "REFERENCE_SOURCES_PATH", path)
    return path, data


def test_shipped_catalog_has_evidence_and_separate_access_rights_and_installation():
    result = reference.reference_sources_catalog(limit=50)
    assert result["schema_version"] == 1 and result["catalog_version"]
    assert result["metadata_only"] is True
    assert result["host_installation"] == "not_checked"
    assert result["untrusted_reference"] is True
    assert result["total"] >= 17
    ids = {row["id"] for row in result["items"]}
    assert {f"{vendor}-optical-glass" for vendor in
            ("schott", "ohara", "hoya", "cdgm", "nikon", "sumita")} <= ids
    assert "merck-patinal" in ids
    for summary in result["items"]:
        detail = reference.reference_sources_detail(summary["id"], expected_version=result["version"])
        row = detail["source"]
        assert row["rights"]["evidence_urls"]
        assert row["rights"]["offline_use"] in reference.RIGHTS_STATUSES
        assert row["rights"]["redistribution"] in reference.RIGHTS_STATUSES
        assert row["integration"]["host_installation"] == "not_checked"
        assert row["integration"]["bundled_numeric_data"] is False
        assert all(link["url"].startswith("https://") for link in row["links"])
    raw = reference.REFERENCE_SOURCES_PATH.read_text(encoding="utf-8")
    assert "/workspace/" not in raw and "/home/" not in raw and "sakuya" not in raw


def test_known_rights_do_not_become_generic_permission():
    cie = reference.reference_sources_detail("cie-standard-colorimetry")["source"]
    assert cie["rights"]["license"] == "CC-BY-SA-4.0"
    assert cie["rights"]["redistribution"] == "conditional"
    schott = reference.reference_sources_detail("schott-optical-glass")["source"]
    assert schott["access"]["download_available"] is True
    assert schott["offline_status"] == "official_download"
    assert schott["rights"]["redistribution"] != "allowed"
    ito = reference.reference_sources_detail("zenodo-ito-15055400")["source"]
    assert ito["rights"]["license"] == "CC-BY-4.0"
    assert sum(file["bytes"] for file in ito["files"]) == 859455
    assert ito["integration"]["workflow"] == "raw_reference_files_only"
    nist = reference.reference_sources_detail("nist-chemistry-webbook")["source"]
    assert nist["rights"]["offline_use"] == "unknown"
    assert nist["rights"]["redistribution"] == "unknown"


def test_pagination_is_stable_lossless_and_can_be_version_pinned():
    all_rows = reference.reference_sources_catalog(limit=50)
    ids: list[str] = []
    offset = 0
    while True:
        result = reference.reference_sources_catalog(offset=offset, limit=3,
                                                       expected_version=all_rows["version"])
        ids.extend(row["id"] for row in result["items"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert ids == [row["id"] for row in all_rows["items"]]
    assert len(ids) == len(set(ids)) == all_rows["total"]
    assert reference.reference_sources_catalog(offset=10000)["items"] == []


def test_search_is_literal_casefold_all_terms_with_enum_filters():
    assert reference.reference_sources_catalog(query="schott")["total"] == 1
    assert reference.reference_sources_catalog(query="SCHOTT")["total"] == 1
    assert reference.reference_sources_catalog(query=".*")["items"] == []
    assert reference.reference_sources_catalog(query="SCHOTT UNKNOWNNEEDLE")["total"] == 0
    assert reference.reference_sources_catalog(query="  ")["total"] > 0
    rows = reference.reference_sources_catalog(category="substrates",
                                                  offline_status="official_download")["items"]
    assert len(rows) == 6 and all(row["category"] == "substrates" for row in rows)
    assert reference.reference_sources_catalog(query="Merck H4")["total"] == 1


def test_results_are_detached_from_next_read_and_hash_matches_bytes(resource):
    path, _ = resource
    result = reference.reference_sources_detail("schott-optical-glass")
    assert result["version"] == hashlib.sha256(path.read_bytes()).hexdigest()
    result["source"]["rights"]["offline_use"] = "forged"
    assert reference.reference_sources_detail("schott-optical-glass")["source"]["rights"]["offline_use"] != "forged"
    summary = reference.reference_sources_catalog()["items"][0]
    summary["rights"]["notes"].append("forged")
    assert "forged" not in reference.reference_sources_catalog()["items"][0]["rights"]["notes"]


def test_version_change_rejects_stale_catalog_and_detail(resource):
    path, _ = resource
    version = reference.reference_sources_catalog()["version"]
    path.write_bytes(path.read_bytes() + b"\n")
    for call in (lambda: reference.reference_sources_catalog(expected_version=version),
                 lambda: reference.reference_sources_detail("schott-optical-glass", expected_version=version)):
        with pytest.raises(ProjectError) as error:
            call()
        assert error.value.code == "reference_sources_changed"


@pytest.mark.parametrize("arguments", [
    {"query": None}, {"query": "x" * 201}, {"query": "x\0"}, {"query": "a " * 13},
    {"offset": -1}, {"offset": True}, {"offset": 1.0}, {"offset": 10001},
    {"limit": 0}, {"limit": 51}, {"limit": False}, {"limit": "20"},
    {"category": []}, {"category": "other"}, {"offline_status": False},
    {"offline_status": "installed"}, {"expected_version": "bad"},
])
def test_strict_arguments_rejected_before_resource_access(monkeypatch, arguments):
    monkeypatch.setattr(reference, "_load", lambda: pytest.fail("invalid request read a file"))
    with pytest.raises(ProjectError) as error:
        reference.reference_sources_catalog(**arguments)
    assert error.value.code == "invalid_arguments"


@pytest.mark.parametrize("source_id", ["", None, "../secret", "/tmp/secret", "https://example.com", "x" * 81])
def test_no_caller_controlled_paths_or_urls(source_id):
    with pytest.raises(ProjectError) as error:
        reference.reference_sources_detail(source_id)
    assert error.value.code == "invalid_arguments"


def test_unknown_id_is_clear_and_no_path_parameters_exist():
    with pytest.raises(ProjectError) as error:
        reference.reference_sources_detail("nonexistent")
    assert error.value.code == "reference_source_not_found"
    for fn in (reference.reference_sources_catalog, reference.reference_sources_detail):
        assert not ({"path", "url", "root", "catalog_path", "storage_dir"}
                    & set(inspect.signature(fn).parameters))


@pytest.mark.parametrize("failure", ["empty", "oversize", "duplicate_key", "malformed", "invalid_url",
                                     "duplicate_id", "claims_installation", "claims_data", "invalid_rights"])
def test_malformed_resources_fail_closed(resource, failure):
    path, data = resource
    if failure == "empty":
        path.write_bytes(b"")
    elif failure == "oversize":
        path.write_bytes(b" " * (reference.MAX_SOURCE_BYTES + 1))
    elif failure == "duplicate_key":
        path.write_text('{"schema_version":1,"schema_version":1}')
    elif failure == "malformed":
        path.write_bytes(b"not JSON")
    else:
        changed = deepcopy(data)
        if failure == "invalid_url":
            changed["sources"][0]["links"][0]["url"] = "file:///tmp/secret"
        elif failure == "duplicate_id":
            changed["sources"][1]["id"] = changed["sources"][0]["id"]
        elif failure == "claims_installation":
            changed["sources"][0]["integration"]["host_installation"] = "available"
        elif failure == "claims_data":
            changed["sources"][0]["integration"]["bundled_numeric_data"] = True
        else:
            changed["sources"][0]["rights"]["redistribution"] = "probably"
        path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ProjectError) as error:
        reference.reference_sources_catalog()
    assert error.value.code == "reference_sources_invalid"


def test_missing_resource_reports_unavailable(resource):
    path, _ = resource
    path.unlink()
    with pytest.raises(ProjectError) as error:
        reference.reference_sources_catalog()
    assert error.value.code == "reference_sources_unavailable"


def test_symbolic_link_resource_rejected(resource):
    path, _ = resource
    target = path.with_suffix(".target")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(ProjectError) as error:
        reference.reference_sources_catalog()
    assert error.value.code == "reference_sources_invalid"


def test_reads_do_not_create_storage_or_use_network(tmp_path, monkeypatch):
    import socket
    import urllib.request

    def forbidden(*args, **kwargs):
        pytest.fail("metadata query must not write or use the network")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    assert reference.reference_sources_catalog()["total"] > 0
    assert reference.reference_sources_detail("merck-patinal")["source"]["id"] == "merck-patinal"
    assert not list(tmp_path.iterdir())
