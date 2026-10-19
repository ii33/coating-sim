from copy import deepcopy

from coat_sim.core.assistant_material_quality import validate_assistant_material_quality


SOURCE = "https://example.org/optical/reference-table"


def table(k=None):
    return {"id": "sample", "name": "sample", "kind": "tabulated",
            "source": SOURCE, "notes": "bulk sample at 20 C",
            "wavelength_unit": "nm", "wavelength_min": 400, "wavelength_max": 700,
            "wavelengths": [400, 550, 700], "n": [1.5, 1.49, 1.48], "k": k}


def formula():
    return {"id": "sample", "name": "sample", "kind": "cauchy",
            "source": SOURCE, "notes": "bulk sample at 20 C",
            "wavelength_unit": "um", "wavelength_min": 0.4, "wavelength_max": 0.7,
            "parameters": {"A": 1.45, "B": 0.01, "C": 0.0}}


def check(record, origin="assistant_reference", **kwargs):
    if origin == "assistant_reference" and "conditions" not in kwargs:
        kwargs["conditions"] = "bulk sample at 20 C"
    return validate_assistant_material_quality(
        {"optical_constants": [record]}, origin=origin,
        evidence={"sample": kwargs})


def test_reference_formula_and_unknown_k():
    report = check(formula(), k_status="unknown")
    assert report["valid"]
    assert report["datasets"][0]["k_status"] == "unknown"
    assert not report["source_verified_externally"]


def test_complete_table_and_partial_table():
    assert check(table(), k_status="unknown", reference_row_count=3)["valid"]
    report = check(table(), k_status="unknown", reference_row_count=20)
    assert not report["valid"]
    assert "3 rows" in str(report["errors"])


def test_missing_source_range_and_conditions():
    record = table()
    record["source"] = "undocumented"
    record["notes"] = ""
    report = check(record, k_status="unknown", reference_row_count=3)
    assert not report["valid"]
    assert any("URL or DOI" in error for error in report["errors"])
    assert any("conditions" in error for error in report["errors"])
    record = table()
    del record["wavelength_unit"]
    try:
        check(record, k_status="unknown", reference_row_count=3)
    except ValueError:
        pass
    else:
        raise AssertionError("missing wavelength unit must fail schema validation")
    record = table()
    del record["wavelength_max"]
    try:
        check(record, k_status="unknown", reference_row_count=3)
    except ValueError:
        pass
    else:
        raise AssertionError("missing validity range must fail schema validation")


def test_unknown_k_cannot_be_written_as_zero():
    report = check(table([0, 0, 0]), k_status="unknown", reference_row_count=3)
    assert not report["valid"]
    assert "unknown k must stay null" in str(report["errors"])


def test_explicit_sparse_user_measurement_is_allowed():
    record = table()
    record["wavelengths"] = [400, 700]
    record["n"] = [1.5, 1.48]
    record["source"] = "User supplied measurements"
    record["notes"] = "sparse measured points"
    report = check(record, origin="user_measurement", k_status="unknown")
    assert report["valid"]
    assert report["datasets"][0]["data_character"] == "user_sparse_measurement"


def test_incomplete_formula_coefficients_fail():
    record = deepcopy(formula())
    del record["parameters"]["C"]
    try:
        check(record, k_status="unknown")
    except ValueError:
        pass
    else:
        raise AssertionError("incomplete dispersion coefficients must fail")
