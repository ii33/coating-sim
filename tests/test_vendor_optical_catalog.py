"""Synthetic AGF contract fixtures only; no redistributed vendor catalog data."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from coat_sim.core.optical_constants import evaluate_optical_set, validate_optical_set
from coat_sim.core.vendor_optical_catalog import (
    VendorCatalogError, load_vendor_catalog, parse_agf, vendor_format_capabilities,
)

pytestmark = pytest.mark.contract

# Invented numeric examples, not any manufacturer's measured glass.
SELLMEIER = (
    "NM SYNTH-A 2 000000 1.5 50 0 1\r\n"
    "GC Test material <script>not executable</script>\r\n"
    "ED 5 6 2.5 0 0\r\n"
    "CD 1 .02 .1 .03 .2 100 0 0\r\n"
    "TD 1e-6 2e-8 3e-11 4e-7 5e-10 .2 22\r\n"
    "LD .4 2.5\r\n"
    "IT .4 .8 10\r\n"
    "IT .6 .9 25\r\n"
    "IT .8 0 10\r\n"
    "IT 1 1 10\r\n"
    "XY preserve unknown future fields\r\n"
)
SCHOTT = "NM SYNTH-B 1 000000 1.5 50\nCD 2.25 .01 .001 .0001 .00001 .000001\nLD .4 2.5\n"
HEADER = "CC Synthetic example catalog v1\r\n! Not a real manufacturer\r\n"


def catalog(text: str = HEADER + SELLMEIER):
    return parse_agf(text, manufacturer="Synthetic vendor", catalog_version="v1",
                     source_url="https://example.invalid/catalog.agf",
                     license_note="Synthetic test fixture")


def prepare(store, *args, **kwargs):
    return store.prepare_import(*args, allow_reference_air_approximation=True, **kwargs)


def entry_id(store) -> str:
    return store.search()["entries"][0]["entry_id"]


def assert_code(expected: str, operation) -> None:
    with pytest.raises(VendorCatalogError) as caught:
        operation()
    assert caught.value.code == expected


def test_status_search_and_full_source_provenance_survive() -> None:
    store = catalog(HEADER + SELLMEIER + SCHOTT)
    status = store.status()
    assert status["entry_count"] == 2
    assert status["conversion_counts"] == {"supported": 2}
    assert status["formula_counts"] == {"2": 1, "1": 1}
    assert status["manufacturer"] == "Synthetic vendor"
    assert status["catalog_version"] == "v1"
    assert status["source_sha256"] == hashlib.sha256((HEADER + SELLMEIER + SCHOTT).encode()).hexdigest()
    assert status["read_only"]
    assert status["raw_header"] == HEADER
    assert store.search("test material")["total"] == 1
    assert store.search("synth", limit=1, offset=1)["entries"][0]["grade"] == "SYNTH-B"
    detail = store.detail(entry_id(store))
    entry = detail["entry"]
    assert entry["raw_entry"] == SELLMEIER
    assert entry["fields"]["XY"][0]["value"] == "preserve unknown future fields"
    assert entry["reference_temperature_c"] == 22
    assert entry["thermal_coefficients"]["D0"] == 1e-6
    assert entry["nm"]["extra_tokens"] == ["0", "1"]
    assert detail["untrusted_reference"]
    imported = prepare(store, entry_id(store), optical_set_id="chosen_id", name="Chosen name")
    result = imported["optical_set"]
    assert result == validate_optical_set(result)
    assert result["id"] == "chosen_id" and result["name"] == "Chosen name"
    persisted = json.loads(result["notes"].split("\n", 1)[1])
    assert persisted["raw_entry"] == SELLMEIER
    assert persisted["raw_header"] == HEADER
    assert persisted["source_sha256"] == status["source_sha256"]
    assert persisted["internal_transmittance_policy"] == "reference_only"
    assert not imported["writes_material_library"] and not imported["changes_active_selection"]


def test_exact_sellmeier_mapping_is_agf2_to_rii2_not_squaring_L() -> None:
    store = catalog()
    record = prepare(store, entry_id(store))["optical_set"]
    assert record["formula_number"] == 2
    assert record["coefficients"] == [0, 1, .02, .1, .03, .2, 100]
    for wave in (.4, .5875618, 1.5, 2.5):
        squared = 1 + sum(k * wave**2 / (wave**2 - pole)
                          for k, pole in ((1, .02), (.1, .03), (.2, 100)))
        n, k = evaluate_optical_set(record, wave * 1000, "nm")
        assert n == pytest.approx(math.sqrt(squared), rel=1e-14)
        assert k is None
    with pytest.raises(ValueError, match="有效"):
        evaluate_optical_set(record, 399, "nm")


def test_exact_schott_mapping_is_agf1_to_rii3() -> None:
    store = catalog(SCHOTT)
    record = prepare(store, entry_id(store))["optical_set"]
    assert record["formula_number"] == 3
    assert record["coefficients"] == [2.25, .01, 2, .001, -2, .0001, -4, .00001, -6, .000001, -8]
    for w in (.4, .8, 2.5):
        expected = math.sqrt(2.25 + .01*w**2 + .001*w**-2 + .0001*w**-4 + .00001*w**-6 + .000001*w**-8)
        assert evaluate_optical_set(record, w, "um") == (pytest.approx(expected), None)


def test_internal_transmittance_preserves_individual_thickness_and_zero_without_k() -> None:
    store = catalog()
    preview = store.preview(entry_id(store), max_points=11)
    assert len(preview["points"]) == 11
    assert preview["columns"] == ["wavelength_um", "n", "k"]
    assert preview["points"][0][0] == .4 and preview["points"][-1][0] == 2.5
    assert all(row[2] is None for row in preview["points"])
    assert preview["internal_transmittance"][0]["thickness_mm"] == 10
    assert preview["internal_transmittance"][1]["thickness_mm"] == 25
    assert preview["internal_transmittance"][2]["internal_transmittance"] == 0
    assert preview["internal_transmittance"][3]["internal_transmittance"] == 1
    assert all(row["status"] == "reference_only" for row in preview["internal_transmittance"])
    assert preview["reference_index_medium"] == "relative_to_reference_air"
    imported = prepare(store, entry_id(store))
    assert imported["k"] is None and imported["provenance"]["k_status"] == "unknown"
    assert "k_data" not in imported["optical_set"] and "k" not in imported["optical_set"]


def test_invalid_transmission_is_preserved_only_and_does_not_invent_a_curve() -> None:
    store = catalog(SELLMEIER + "IT -1 .5 0\nIT 1 .8 10\nIT 2 NaN 10\nIT bad\n")
    detail = store.detail(entry_id(store))["entry"]
    assert detail["conversion"]["status"] == "supported"
    assert len(detail["internal_transmittance"]) == 8
    assert all(row["status"] == "invalid" for row in detail["internal_transmittance"][4:])
    assert detail["reference_warnings"]
    assert evaluate_optical_set(prepare(store, entry_id(store))["optical_set"], 1, "um")[1] is None


@pytest.mark.parametrize("number", [4, 5, 7, 8, 9, 10, 11, 100])
def test_unmapped_formulas_are_reference_only_even_when_numbers_overlap_rii(number: int) -> None:
    text = SELLMEIER.replace("SYNTH-A 2", f"SYNTH-A {number}")
    store = catalog(text)
    row = store.search()["entries"][0]
    assert row["conversion"]["status"] == "unsupported"
    assert store.detail(row["entry_id"])["entry"]["raw_entry"] == text
    assert_code("UNSUPPORTED_FORMULA", lambda: prepare(store, row["entry_id"]))
    assert_code("UNSUPPORTED_FORMULA", lambda: store.preview(row["entry_id"]))


@pytest.mark.parametrize("change,code", [
    (("CD 1 .02 .1 .03 .2 100 0 0", "CD 1 .02 .1 .03 .2"), "UNSUPPORTED_COEFFICIENT_LAYOUT"),
    (("CD 1 .02 .1 .03 .2 100 0 0", "CD 1 .02 .1 .03 .2 100 1 0"), "UNSUPPORTED_COEFFICIENT_LAYOUT"),
    (("CD 1 .02 .1 .03 .2 100 0 0", "CD NaN .02 .1 .03 .2 100"), "INVALID_AGF_RECORD"),
    (("LD .4 2.5", "LD 2.5 .4"), "INVALID_AGF_RECORD"),
    (("LD .4 2.5", "LD .4 2.5\nLD .5 2.4"), "INVALID_AGF_RECORD"),
    (("LD .4 2.5", ""), "INVALID_AGF_RECORD"),
    (("CD 1 .02 .1 .03 .2 100 0 0", "CD 1 .25 .1 .03 .2 100"), "OPTICAL_VALIDATION_FAILED"),
    (("TD 1e-6 2e-8 3e-11 4e-7 5e-10 .2 22", "TD 1 2 3"), "INVALID_AGF_RECORD"),
    (("NM SYNTH-A 2 000000 1.5 50 0 1", "NM BROKEN"), "INVALID_AGF_RECORD"),
])
def test_malformed_or_ambiguous_records_never_guess(change, code: str) -> None:
    store = catalog(SELLMEIER.replace(*change))
    row = store.search()["entries"][0]
    assert row["conversion"]["status"] != "supported"
    assert_code(code, lambda: prepare(store, row["entry_id"]))
    assert store.detail(row["entry_id"])["entry"]["raw_entry"]


def test_temperature_is_preserved_not_corrected_or_defaulted() -> None:
    store = catalog()
    assert store.preview(entry_id(store), temperature_c=22, max_points=2)["reference_temperature_c"] == 22
    assert_code("UNSUPPORTED_TEMPERATURE", lambda: store.preview(entry_id(store), temperature_c=20))
    assert_code("UNSUPPORTED_TEMPERATURE", lambda: prepare(store, entry_id(store), temperature_c=20))
    assert_code("INVALID_QUERY", lambda: prepare(store, entry_id(store), temperature_c=True))
    for text in (SCHOTT, SCHOTT + "TD\n"):
        no_temp = catalog(text)
        detail = no_temp.detail(entry_id(no_temp))["entry"]
        assert detail["reference_temperature_c"] is None
        assert detail["reference_warnings"]
        assert prepare(no_temp, entry_id(no_temp))["provenance"]["reference_temperature_c"] is None
        assert_code("UNSUPPORTED_TEMPERATURE", lambda no_temp=no_temp: no_temp.preview(entry_id(no_temp), temperature_c=20))


def test_all_return_values_are_detached_and_duplicate_grades_are_distinct() -> None:
    store = catalog(SELLMEIER + SELLMEIER)
    rows = store.search()["entries"]
    assert rows[0]["entry_id"] != rows[1]["entry_id"]
    key = rows[0]["entry_id"]
    rows[0]["conversion"]["status"] = "bad"
    detail = store.detail(key)
    detail["entry"]["coefficients"][0] = 1e9
    detail["catalog"]["manufacturer"] = "changed"
    result = prepare(store, key)
    result["optical_set"]["coefficients"][1] = 0
    assert store.detail(key)["entry"]["coefficients"][0] == 1
    assert store.status()["manufacturer"] == "Synthetic vendor"
    assert store.search()["entries"][0]["conversion"]["status"] == "supported"
    assert prepare(store, key)["optical_set"]["coefficients"][1] == 1


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-be"])
def test_local_encoding_and_exact_file_hash(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "chosen.AGF"
    data = (HEADER + SELLMEIER).encode(encoding)
    if encoding == "utf-16-be":
        data = b"\xfe\xff" + data
    path.write_bytes(data)
    store = load_vendor_catalog(path, manufacturer="Synthetic vendor", catalog_version="v1")
    assert store.status()["source_sha256"] == hashlib.sha256(data).hexdigest()
    assert store.detail(entry_id(store))["entry"]["raw_entry"] == SELLMEIER
    assert path.read_bytes() == data
    assert list(tmp_path.iterdir()) == [path]


def test_explicit_legacy_encoding_and_fortran_exponents(tmp_path: Path) -> None:
    path = tmp_path / "legacy.agf"
    path.write_bytes(("CC caf\u00e9\n" + SELLMEIER.replace("1e-6", "1D-6")).encode("cp1252"))
    args = {"manufacturer": "Synthetic vendor", "catalog_version": "v1"}
    assert_code("INVALID_ENCODING", lambda: load_vendor_catalog(path, **args))
    store = load_vendor_catalog(path, encoding="cp1252", **args)
    assert store.status()["catalog_comments"] == ["caf\u00e9"]
    assert store.detail(entry_id(store))["entry"]["thermal_coefficients"]["D0"] == 1e-6


def test_source_change_rekeys_entry_and_formats_are_explicit(tmp_path: Path) -> None:
    first = catalog()
    second = catalog(HEADER + SELLMEIER.replace(".02", ".021"))
    assert entry_id(first) != entry_id(second)
    assert_code("ENTRY_NOT_FOUND", lambda: second.detail(entry_id(first)))
    args = {"manufacturer": "Merck", "catalog_version": "not specified"}
    assert_code("UNSUPPORTED_FORMAT", lambda: load_vendor_catalog(tmp_path / "mixture.pdf", **args))
    assert_code("UNSUPPORTED_FORMAT", lambda: load_vendor_catalog(tmp_path / "glass.bgf", **args))
    assert_code("SOURCE_READ_FAILED", lambda: load_vendor_catalog(tmp_path / "missing.agf", **args))
    assert_code("INVALID_AGF", lambda: catalog("CC No materials"))
    assert_code("INVALID_AGF", lambda: catalog("NM\0"))
    formats = {row["format"]: row for row in vendor_format_capabilities()}
    assert formats["agf"]["supported_formula_numbers"] == [1, 2, 3, 6, 12, 13]
    assert formats["pdf"]["reason"] == "REFERENCE_DOCUMENT_ONLY"


@pytest.mark.parametrize("args", [{"limit": True}, {"limit": 1001}, {"offset": -1},
                                 {"capability": "convert"}, {"query": "x"*201}])
def test_invalid_search_contract(args) -> None:
    assert_code("INVALID_QUERY", lambda: catalog().search(**args))


def test_invalid_queries_and_metadata() -> None:
    store = catalog()
    for max_points in (1, 1001, True, 3.5):
        assert_code("INVALID_QUERY", lambda max_points=max_points: store.preview(entry_id(store), max_points=max_points))
    assert_code("ENTRY_NOT_FOUND", lambda: store.detail("not-this-snapshot"))
    assert_code("INVALID_METADATA", lambda: parse_agf(SELLMEIER, manufacturer="", catalog_version="v1"))
    assert_code("INVALID_METADATA", lambda: parse_agf(SELLMEIER, manufacturer="vendor", catalog_version=""))
    assert_code("OPTICAL_VALIDATION_FAILED", lambda: prepare(store, entry_id(store), optical_set_id="bad/id"))
    assert_code("OPTICAL_VALIDATION_FAILED", lambda: prepare(store, entry_id(store), name=""))


def test_source_limits_and_no_silent_provenance_truncation(monkeypatch) -> None:
    from coat_sim.core import vendor_optical_catalog as module
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 32)
    assert_code("SOURCE_LIMIT", lambda: catalog())
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 16*1024*1024)
    monkeypatch.setattr(module, "MAX_ENTRIES", 1)
    assert_code("SOURCE_LIMIT", lambda: catalog(SELLMEIER + SCHOTT))
    monkeypatch.setattr(module, "MAX_ENTRIES", 10000)
    assert_code("SOURCE_LIMIT", lambda: catalog("CC " + "a"*65537 + "\n" + SELLMEIER))
    store = catalog(SELLMEIER + "GC " + "x"*21000 + "\n")
    assert_code("PROVENANCE_TOO_LARGE", lambda: prepare(store, entry_id(store)))
    assert len(store.detail(entry_id(store))["entry"]["raw_entry"]) > 21000


def test_numeric_cd_td_continuations_are_explicit_and_raw_lines_survive() -> None:
    original = SELLMEIER.replace("CD 1 .02 .1 .03 .2 100 0 0", "CD 1 .02 .1 .03\n.2 100\n0 0")
    original = original.replace("TD 1e-6 2e-8 3e-11 4e-7 5e-10 .2 22", "TD 1e-6 2e-8 3e-11 4e-7\n5e-10 .2 22")
    store = catalog(original)
    detail = store.detail(entry_id(store))["entry"]
    assert detail["raw_entry"] == original
    assert detail["reference_temperature_c"] == 22
    assert len(detail["fields"]["CD"][0]["continuation_lines"]) == 2
    assert prepare(store, entry_id(store))["optical_set"]["coefficients"] == [0, 1, .02, .1, .03, .2, 100]
    bad = catalog(original.replace(".2 100\n", ".2 unknown\n"))
    assert_code("INVALID_AGF_RECORD", lambda: prepare(bad, entry_id(bad)))


def test_nm_nd_mismatch_is_advisory_preserved_and_never_reconciles_values() -> None:
    store = catalog(SELLMEIER.replace("000000 1.5 50", "000000 2.0 50"))
    entry = store.detail(entry_id(store))["entry"]
    assert entry["nd_consistency"]["status"] == "warning"
    assert entry["data_quality"] == "source_inconsistent"
    assert store.search()["entries"][0]["data_quality"] == "source_inconsistent"
    assert store.preview(entry_id(store), max_points=2)["data_quality"] == "source_inconsistent"
    assert entry["nd_consistency"]["source_nm_nd"] == 2
    assert entry["nd_consistency"]["policy"] == "project_advisory_only_not_vendor_specification"
    assert entry["conversion"]["status"] == "supported"
    imported = prepare(store, entry_id(store))
    assert imported["provenance"]["nd_consistency"] == entry["nd_consistency"]
    assert imported["provenance"]["data_quality"] == "source_inconsistent"
    n, k = evaluate_optical_set(imported["optical_set"], .5875618, "um")
    assert n == entry["nd_consistency"]["calculated_nd"] and n != 2 and k is None
    assert any("NM nd" in w for w in imported["provenance"]["warnings"])


def test_nd_evaluation_failure_cannot_bypass_invalid_capability() -> None:
    # n-squared is positive at both LD endpoints but negative at the d line.
    store = catalog("NM SYNTH-C 1 0 1.5 50\nCD -.7004 1 .11916 0 0 0\nLD .4 2.5\n")
    row = store.search()["entries"][0]
    assert row["conversion"]["status"] == "invalid"
    assert_code("OPTICAL_DOMAIN_INVALID", lambda: prepare(store, row["entry_id"]))
    assert_code("OPTICAL_DOMAIN_INVALID", lambda: store.preview(row["entry_id"]))


def test_text_bom_preserved_and_invalid_path_is_stable_error() -> None:
    store = catalog("\ufeff" + SELLMEIER)
    assert store.detail(entry_id(store))["entry"]["raw_entry"].startswith("\ufeffNM")
    assert_code("INVALID_SOURCE", lambda: load_vendor_catalog("bad\0.agf", manufacturer="v", catalog_version="v1"))


@pytest.mark.parametrize("agf_number,coefficients,target_number,independent", [
    (3, [1.5, .001, .0001, .00001, .000001, .0000001], 7,
     lambda w: 1.5 + .001/(w*w-.028) + .0001/(w*w-.028)**2
               + .00001*w*w + .000001*w**4 + .0000001*w**6),
    (6, [1, .02, .1, .03, .2, 100, .3, 200], 2,
     lambda w: math.sqrt(1 + w*w/(w*w-.02) + .1*w*w/(w*w-.03)
                         + .2*w*w/(w*w-100) + .3*w*w/(w*w-200))),
    (12, [2.25, .01, .001, .0001, .00001, .000001, .0000001, .00000001], 3,
     lambda w: math.sqrt(2.25 + .01*w*w + .001/w**2 + .0001/w**4
                         + .00001/w**6 + .000001/w**8 + .0000001*w**4 + .00000001*w**6)),
    (13, [2.25, .01, .001, .0001, .00001, .000001, .0000001, .00000001, .000000001], 3,
     lambda w: math.sqrt(2.25 + .01*w*w + .001*w**4 + .0001/w**2
                         + .00001/w**4 + .000001/w**6 + .0000001/w**8
                         + .00000001/w**10 + .000000001/w**12)),
])
def test_additional_official_formulas_against_independent_multispectral_expressions(
        agf_number, coefficients, target_number, independent) -> None:
    # Synthetic sources retain a source-declared d-line value from a separate
    # expression; no real catalog or manufacturer coefficients are redistributed.
    nd = independent(.5875618)
    text = (f"NM SYNTH-{agf_number} {agf_number} 0 {nd:.14g} 50\n"
            + "CD " + " ".join(map(str, coefficients)) + " 0\nLD .4 2.5\n")
    store = catalog(text)
    row = store.search()["entries"][0]
    assert row["conversion"]["status"] == "supported"
    assert row["data_quality"] == "no_inconsistency_detected"
    record = prepare(store, row["entry_id"])["optical_set"]
    assert record["formula_number"] == target_number
    for w in (.4, .45, .5, .5875618, .65, .8, 1, 1.5, 2, 2.5):
        n, k = evaluate_optical_set(record, w, "um")
        assert n == pytest.approx(independent(w), rel=5e-15)
        assert k is None
    assert_code("UNSUPPORTED_COEFFICIENT_LAYOUT", lambda: prepare(catalog(text.replace(" 0\nLD", " 1\nLD")),
        entry_id(catalog(text.replace(" 0\nLD", " 1\nLD")))))


def test_herzberger_pole_uses_um_and_extended3_is_not_extended2() -> None:
    store = catalog("NM SYNTH-H 3 0 1.5 50\nCD 1.5 .001 .0001 0 0 0\nLD .1 .5\n")
    row = store.search()["entries"][0]
    assert row["conversion"]["code"] == "OPTICAL_VALIDATION_FAILED"
    assert_code("OPTICAL_VALIDATION_FAILED", lambda: prepare(store, row["entry_id"]))
    a2_only = catalog("NM SYNTH-E3 13 0 1.5 50\nCD 2.25 0 .1 0 0 0 0 0 0\nLD .4 2.5\n")
    result = prepare(a2_only, entry_id(a2_only))["optical_set"]
    assert evaluate_optical_set(result, 2, "um")[0] == pytest.approx(math.sqrt(2.25 + .1*16))


def test_import_requires_explicit_reference_air_approximation_and_persists_choice() -> None:
    store = catalog()
    key = entry_id(store)
    assert store.preview(key, max_points=2)["reference_index_medium"] == "relative_to_reference_air"
    assert store.search()["entries"][0]["conversion"]["requires_reference_air_approximation"]
    assert_code("REFERENCE_FRAME_CONFIRMATION_REQUIRED", lambda: store.prepare_import(key))
    assert_code("REFERENCE_FRAME_CONFIRMATION_REQUIRED", lambda: store.prepare_import(key, allow_reference_air_approximation=False))
    assert_code("INVALID_QUERY", lambda: store.prepare_import(key, allow_reference_air_approximation=1))
    result = store.prepare_import(key, allow_reference_air_approximation=True)
    assert result["provenance"]["reference_air_approximation"] is True
    assert result["provenance"]["reference_frame_policy"] == "explicit_user_choice_uncorrected_air_as_vacuum_approximation"
    persistent = json.loads(result["optical_set"]["notes"].split("\n", 1)[1])
    assert persistent["reference_air_approximation"] is True
    assert any(p["name"] == "reference_air_approximation" for p in result["optical_set"]["process_parameters"])
    assert not result["changes_active_selection"]


@pytest.mark.parametrize("x0,epsilon", [(.96, .16), (.95**2, 1e-10)])
def test_source_domain_gate_cannot_be_bypassed_with_two_preview_samples(x0: float, epsilon: float) -> None:
    # A negative pocket inside the valid endpoint range, including one much
    # narrower than the fixed sampling grid: the stationary root must catch it.
    store = catalog(f"NM INVALID-POCKET 1 0 1.5 50\nCD {-2*x0-epsilon} 1 {x0*x0} 0 0 0\nLD .4 2.5\n")
    row = store.search()["entries"][0]
    assert row["conversion"]["status"] == "invalid"
    assert_code("OPTICAL_DOMAIN_INVALID", lambda: prepare(store, row["entry_id"]))
    assert_code("OPTICAL_DOMAIN_INVALID", lambda: store.preview(row["entry_id"], max_points=2))


def test_unresolved_domain_roots_fail_closed(monkeypatch) -> None:
    import numpy as np
    from numpy.polynomial import Polynomial

    def fail_roots(*_args, **_kwargs):
        raise np.linalg.LinAlgError("synthetic root solver failure")

    monkeypatch.setattr(Polynomial, "roots", fail_roots)
    store = catalog(SCHOTT)
    row = store.search()["entries"][0]
    assert row["conversion"]["status"] == "invalid"
    assert_code("OPTICAL_DOMAIN_INVALID", lambda: prepare(store, row["entry_id"]))
