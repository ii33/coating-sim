"""Synthetic regressions for the bounded, explicit TFCalc text importer.

These fixtures are invented for the importer contract. They are not copied
from a user's coating file or a vendor sample.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from coat_sim.core.film_design import MAX_LAYERS, new_design
from coat_sim.core.tfcalc_import import (
    TfcalcDocument,
    build_tfcalc_design,
    parse_tfcalc_text,
    read_tfcalc_file,
    register_tfcalc_plugin,
)
from coat_sim.core.plugins import PluginRegistry


def synthetic_tfd() -> str:
    return "\n".join((
        "VERSION*1*",
        "LAYERS*2*",
        "LAYER*1*H*1.5*100*fixture-tail*keep*",
        "LAYER*2*L*1.6*200*",
        "FORMULA*(HL)^1*",
        "ENVIRON*AIR*1.0*SUBSTRATE*4.0*",
        "FLAG*fixture-only-flag*",
        "LAYERS2*2*",
        "LAYER2*1*B*2.5*250*",
        "LAYER2*2*C*3.5*350*",
        "PLOT*fixture-only-plot*",
        "EOF*",
    )) + "\n"


def material_options():
    return [
        {"id": "mat-H", "optical_sets": [{"id": "set-H"}]},
        {"id": "mat-L", "optical_sets": [{"id": "set-L"}]},
        {"id": "mat-B", "optical_sets": [{"id": "set-B"}]},
        {"id": "mat-C", "optical_sets": [{"id": "set-C"}]},
        {"id": "mat-other", "optical_sets": [{"id": "set-other"}]},
    ]


def mapping_for(*aliases):
    return {
        alias: {"material_id": "mat-" + alias, "optical_set_id": "set-" + alias}
        for alias in aliases
    }


def base_design():
    design = new_design("synthetic base", "synthetic-base")
    design["substrate"]["sides"] = "double"
    design["front_layers"] = [{
        "id": "preserved-front", "material_id": "existing-front",
        "optical_set_id": "existing-front-nk",
        "thickness": {"value": 55, "unit": "nm"}, "note": "keep front",
    }]
    design["back_layers"] = [{
        "id": "preserved-back", "material_id": "existing-back",
        "optical_set_id": "existing-back-nk",
        "thickness": {"value": 75, "unit": "nm"}, "note": "keep back",
    }]
    design["note"] = "base note"
    design["workflow_settings"] = {"simulation": {"samples": 19}}
    return design


def options(**overrides):
    result = {
        "source_table": "primary",
        "thickness_field": 4,
        "thickness_unit": "nm",
        "source_order": "substrate_outward",
        "target_side": "front",
        "reference_wavelength_nm": 1000,
        "preserve_environment_confirmed": True,
        "mapping": mapping_for("H", "L"),
    }
    result.update(overrides)
    return result


def test_physical_unit_conversion_and_total_overflow_are_rejected():
    document = parse_tfcalc_text("VERSION*1*\nLAYERS*2*\nLAYER*1*H*1*1e308*\nLAYER*2*L*1*1e308*\nEOF*\n")
    for unit in ("nm", "um"):
        with pytest.raises(ValueError, match="有限数值"):
            build_tfcalc_design(document, options(thickness_unit=unit), base_design(), material_options())


@pytest.mark.parametrize("side", ["front", "back"])
def test_import_of_either_face_preserves_existing_expression_draft_and_marks_it_out_of_sync(side):
    base = base_design()
    base["expression"] = {"text": "A(HL)^2", "in_sync": True, "symbols": {
        "A": {"material_id": "mat-H", "optical_set_id": "set-H",
              "thickness": {"value": 1, "unit": "qw"}, "note": "original expression draft"}}}
    before = deepcopy(base)
    candidate = build_tfcalc_design(parse_tfcalc_text(synthetic_tfd()), options(target_side=side),
                                     base, material_options())
    assert candidate["expression"]["text"] == before["expression"]["text"]
    assert candidate["expression"]["symbols"] == before["expression"]["symbols"]
    assert candidate["expression"]["in_sync"] is False
    other_side = "back" if side == "front" else "front"
    assert candidate[other_side + "_layers"] == before[other_side + "_layers"]
    assert base == before


def test_parse_returns_immutable_primary_and_secondary_tables_and_reports_unmapped_data():
    document = parse_tfcalc_text(synthetic_tfd())

    assert isinstance(document, TfcalcDocument)
    assert document.version == 1
    assert [(row.number, row.material, row.values) for row in document.layers] == [
        (1, "H", (1.5, 100.0)), (2, "L", (1.6, 200.0)),
    ]
    assert document.layers[0].extra_fields == ("fixture-tail", "keep")
    assert document.layers[0].line == 3
    assert [(row.number, row.material, row.values) for row in document.secondary_layers] == [
        (1, "B", (2.5, 250.0)), (2, "C", (3.5, 350.0)),
    ]
    assert document.formulas == ("FORMULA*(HL)^1*",)
    assert document.environment_records == (("ENVIRON", "AIR", "1.0", "SUBSTRATE", "4.0"),)
    report = "\n".join(document.report)
    assert "尾部字段" in report
    assert "环境记录" in report
    assert "FLAG×1" in report and "PLOT×1" in report
    for attribute, replacement in (("number", 9), ("material", "mutable alias")):
        with pytest.raises((AttributeError, TypeError)):
            setattr(document.layers[0], attribute, replacement)


def test_explicit_registry_registration_and_bounded_file_read(tmp_path):
    registry = PluginRegistry()
    register_tfcalc_plugin(registry)
    plugin = registry.get("film_importer", "tfcalc_plaintext")
    assert plugin.api_version == 1
    assert "明文" in plugin.description

    source = tmp_path / "synthetic.tfd"
    original = synthetic_tfd().encode("utf-8")
    source.write_bytes(original)
    before = source.read_bytes()
    parsed = read_tfcalc_file(source, registry)
    assert parsed == parse_tfcalc_text(original.decode("utf-8"))
    assert source.read_bytes() == before

    registry.set_enabled("film_importer", "tfcalc_plaintext", False)
    with pytest.raises(PermissionError):
        read_tfcalc_file(source, registry)


def test_file_reader_rejects_wrong_suffix_and_non_utf8_bytes(tmp_path):
    registry = PluginRegistry()
    register_tfcalc_plugin(registry)
    wrong_suffix = tmp_path / "synthetic.txt"
    wrong_suffix.write_bytes(synthetic_tfd().encode("utf-8"))
    with pytest.raises(ValueError, match=".tfd"):
        read_tfcalc_file(wrong_suffix, registry)

    invalid_encoding = tmp_path / "synthetic.tfd"
    invalid_encoding.write_bytes(b"VERSION*1*\n\xff")
    with pytest.raises(ValueError, match="UTF-8/ASCII"):
        read_tfcalc_file(invalid_encoding, registry)


@pytest.mark.parametrize("text", [
    "VERSION*2*\nLAYERS*0*\nEOF*\n",  # unsupported version
    "VERSION*1*\nLAYERS*0*\n",  # truncated, no EOF
    "VERSION*1*\nVERSION*1*\nLAYERS*0*\nEOF*\n",  # repeated critical record
    "VERSION*1*\nLAYERS*2*\nLAYER*1*H*1*2*\nEOF*\n",  # count mismatch
    "VERSION*1*\nLAYERS*2*\nLAYER*1*H*1*2*\nLAYER*3*H*1*2*\nEOF*\n",  # gap
    "VERSION*1*\nLAYERS*1*\nLAYER*1*H*nan*2*\nEOF*\n",  # NaN
    "VERSION*1*\nLAYERS*1*\nLAYER*1*H*1*inf*\nEOF*\n",  # infinity
    "VERSION*1*\nLAYERS*1*\nLAYER*1*H*0*2*\nEOF*\n",  # zero candidate
    "VERSION*1*\nLAYERS*1*\nLAYER*1*H*1*-2*\nEOF*\n",  # negative candidate
    f"VERSION*1*\nLAYERS*{MAX_LAYERS + 1}*\nEOF*\n",  # declared layer cap
    "VERSION*1*\nLAYERS*0*\nEOF*\nEOF*\n",  # repeated EOF
])
def test_parser_rejects_unsupported_truncated_or_malformed_inputs(text):
    with pytest.raises((ValueError, TypeError)):
        parse_tfcalc_text(text)


@pytest.mark.parametrize("text", [
    "VERSION*1*\nLAYERS*1*\nLAYER*1*H*1*2*",  # final record missing terminator
    "VERSION*1*\nLAYERS*1*\nLAYER*1*H*1*2*\nEOF*\n\x00",
    " " * (2 * 1024 * 1024 + 1),
], ids=["unterminated-record", "binary-content", "oversized-whitespace"])
def test_parser_rejects_unterminated_binary_or_oversized_input(text):
    with pytest.raises((ValueError, TypeError)):
        parse_tfcalc_text(text)


def test_build_maps_only_explicit_primary_table_and_preserves_unselected_state():
    document = parse_tfcalc_text(synthetic_tfd())
    original = base_design()
    before = deepcopy(original)

    candidate = build_tfcalc_design(
        document, options(), original, material_options(),
    )

    assert original == before
    assert [row["material_id"] for row in candidate["front_layers"]] == ["mat-H", "mat-L"]
    assert [row["optical_set_id"] for row in candidate["front_layers"]] == ["set-H", "set-L"]
    assert [row["thickness"] for row in candidate["front_layers"]] == [
        {"value": 100.0, "unit": "nm"}, {"value": 200.0, "unit": "nm"},
    ]
    assert len({row["id"] for row in candidate["front_layers"]}) == 2
    assert candidate["back_layers"] == original["back_layers"]
    assert candidate["substrate"] == original["substrate"]
    assert candidate["incident_medium"] == original["incident_medium"]
    assert candidate["workflow_settings"] == original["workflow_settings"]
    assert candidate["reference_wavelength_nm"] == 1000
    assert candidate["expression"] == {"text": "", "symbols": {}, "in_sync": False}
    assert "FORMULA*(HL)^1*" in candidate["note"]


def test_build_selects_secondary_table_field_unit_order_and_back_side_explicitly():
    document = parse_tfcalc_text(synthetic_tfd())
    original = base_design()
    before = deepcopy(original)
    selected = options(
        source_table="secondary",
        thickness_field=3,
        thickness_unit="um",
        source_order="incident_first",
        target_side="back",
        mapping=mapping_for("B", "C"),
    )

    candidate = build_tfcalc_design(document, selected, original, material_options())

    assert original == before
    assert candidate["front_layers"] == original["front_layers"]
    assert [row["material_id"] for row in candidate["back_layers"]] == ["mat-C", "mat-B"]
    assert [row["thickness"] for row in candidate["back_layers"]] == [
        {"value": 3.5, "unit": "um"}, {"value": 2.5, "unit": "um"},
    ]


@pytest.mark.parametrize("changed", [
    {"source_table": "automatic"},
    {"thickness_field": 2},
    {"thickness_unit": "qw"},
    {"source_order": "automatic"},
    {"target_side": "both"},
    {"preserve_environment_confirmed": False},
    {"reference_wavelength_nm": 0},
    {"unexpected": True},
    {"mapping": {"H": {"material_id": "mat-H", "optical_set_id": "set-H"}}},
    {"mapping": {"H": {"material_id": "mat-H", "optical_set_id": "set-L"},
                  "L": {"material_id": "mat-L", "optical_set_id": "set-L"}}},
    {"mapping": {"H": {"material_id": "mat-other", "optical_set_id": "set-H"},
                  "L": {"material_id": "mat-L", "optical_set_id": "set-L"}}},
])
def test_build_requires_explicit_choices_and_material_owned_optical_sets(changed):
    document = parse_tfcalc_text(synthetic_tfd())
    with pytest.raises(ValueError):
        build_tfcalc_design(document, options(**changed), base_design(), material_options())


def test_back_import_requires_double_sided_substrate_and_empty_table_is_rejected():
    document = parse_tfcalc_text("VERSION*1*\nLAYERS*1*\nLAYER*1*H*1*2*\nEOF*\n")
    original = new_design("single side", "single-side")
    selected = options(target_side="back")
    with pytest.raises(ValueError, match="双面"):
        build_tfcalc_design(document, selected, original, material_options())

    empty_secondary = parse_tfcalc_text(
        "VERSION*1*\nLAYERS*1*\nLAYER*1*H*1*2*\nLAYERS2*0*\nEOF*\n",
    )
    double_sided = base_design()
    with pytest.raises(ValueError, match="为空"):
        build_tfcalc_design(
            empty_secondary, options(source_table="secondary", target_side="back",
                                     mapping={}), double_sided, material_options(),
        )
