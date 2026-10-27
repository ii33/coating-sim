"""Common table drafts expand to the existing physical per-layer protocol."""
from copy import deepcopy
from dataclasses import replace
import json
from typing import cast

import pytest
from PySide6.QtTest import QSignalSpy

from coat_sim.core.film_thickness_error_table import _physical_parameters, freeze_film_thickness_error_table_request
from coat_sim.ui.film_sensitivity_quick_controller import FilmSensitivityQuickController
from tests.test_film_optimization_needle import _optical
from tests.test_film_sensitivity_workflows_gui import app as app, harness as harness

pytestmark = pytest.mark.qt


def _rows(panel: FilmSensitivityQuickController) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], panel.property("thicknessErrorRows"))


def _defaults(panel: FilmSensitivityQuickController, **values: str) -> None:
    for field, value in values.items():
        assert panel.setTableDefault(field, value)


def _request(case):
    return case.panel._build_thickness_error_request(
        case.owner._build_request(require_variables=False, fixed_topology=False))


def test_common_parameters_apply_to_all_and_match_existing_explicit_protocol(harness):
    panel = harness.panel
    assert all(row["enabled"] and not row["override"] for row in _rows(panel))
    _defaults(panel, bias_text="-2.5", bias_unit="relative_percent", uncertainty_kind="bounded",
              uncertainty_text="1", uncertainty_unit="absolute_nm")
    batch = _request(harness)
    assert all(row.bias == -.025 and row.bias_unit == "relative_fraction" for row in batch.rows)
    assert all(row.uncertainty_magnitude == 1. and row.uncertainty_kind == "bounded" for row in batch.rows)
    explicit = FilmSensitivityQuickController(harness.owner)
    try:
        assert explicit.prepare()
        for row in _rows(explicit):
            key = str(row["layer_id"])
            assert explicit.setTableDraft(key, "bias", "-2.5")
            assert explicit.setTableUnit(key, "bias", "relative_percent")
            assert explicit.setTableUncertaintyKind(key, "bounded")
            assert explicit.setTableDraft(key, "uncertainty_magnitude", "1")
            assert explicit.setTableUnit(key, "uncertainty", "absolute_nm")
        base = harness.owner._build_request(require_variables=False, fixed_topology=False)
        assert batch.rows == explicit._build_thickness_error_request(base).rows
    finally:
        explicit.shutdown(1000)


def test_common_updates_keep_exceptions_and_exclusions_and_reset_restores_inheritance(harness):
    panel = harness.panel
    first, second = (str(row["layer_id"]) for row in _rows(panel))
    _defaults(panel, bias_text="1", bias_unit="absolute_nm")
    assert panel.setTableDraft(first, "bias", "-4")
    assert panel.setTableLayerEnabled(second, False)
    _defaults(panel, bias_text="2", bias_unit="relative_percent")
    a, b = _rows(panel)
    assert a["override"] and a["bias_text"] == "-4" and a["bias_unit"] == "absolute_nm"
    assert not b["override"] and not b["enabled"] and b["bias_text"] == "2"
    assert panel.setTableLayerOverride(first, False)
    a, b = _rows(panel)
    assert not a["override"] and a["bias_text"] == "2" and a["bias_unit"] == "relative_percent"
    assert not b["enabled"]


def test_save_reload_draft_roundtrip_uses_explicit_edit_notification_without_load_echo(harness, tmp_path):
    panel = harness.panel
    writes: list[dict[str, object]] = []
    panel.tableConfigurationEdited.connect(
        lambda: writes.append(json.loads(json.dumps(panel.exportTableConfiguration()))))
    _defaults(panel, bias_text="-1e", bias_unit="absolute_nm")
    first, second = (str(row["layer_id"]) for row in _rows(panel))
    assert panel.setTableDraft(first, "bias", "0")
    assert panel.setTableLayerEnabled(second, False)
    path = tmp_path / "table-draft.json"
    path.write_text(json.dumps(writes[-1]), encoding="utf8")
    restored = FilmSensitivityQuickController(harness.owner)
    try:
        edits = QSignalSpy(restored.tableConfigurationEdited)
        saved = json.loads(path.read_text(encoding="utf8"))
        assert restored.loadTableConfiguration(saved)
        assert restored.exportTableConfiguration() == saved
        assert edits.count() == 0
        assert restored.prepare()
        a, b = _rows(restored)
        assert a["override"] and a["bias_text"] == "0"
        assert not b["override"] and not b["enabled"] and b["bias_text"] == "-1e"
        assert edits.count() == 0
    finally:
        restored.shutdown(1000)


def test_topology_reorder_add_delete_preserves_only_matching_stable_layer_ids(harness):
    panel = harness.panel
    first, second = (str(row["layer_id"]) for row in _rows(panel))
    _defaults(panel, bias_text="2", bias_unit="absolute_nm")
    assert panel.setTableDraft(first, "bias", "5")
    assert panel.setTableLayerEnabled(second, False)
    new = deepcopy(harness.session.design["front_layers"][0])
    new["id"] = "new-batch-layer"
    harness.session.design["front_layers"] = [new, *reversed(harness.session.design["front_layers"])]
    harness.session.design_revision += 1
    harness.owner.refreshDesign()
    assert panel.prepare()
    by_id = {row["layer_id"]: row for row in _rows(panel)}
    assert by_id[first]["override"] and by_id[first]["bias_text"] == "5"
    assert not by_id[second]["enabled"]
    assert by_id["new-batch-layer"]["enabled"] and not by_id["new-batch-layer"]["override"]
    assert by_id["new-batch-layer"]["bias_text"] == "2"
    harness.session.design["front_layers"] = [new]
    harness.session.design_revision += 1
    harness.owner.refreshDesign()
    assert panel.prepare()
    assert [row["layer_id"] for row in _rows(panel)] == ["new-batch-layer"]
    assert [row["layer_id"] for row in cast(list[dict[str, object]], panel.exportTableConfiguration()["layers"])] == ["new-batch-layer"]


def test_relative_percent_recomputes_from_current_frozen_qw_material_and_thickness(harness):
    panel = harness.panel
    record = harness.library.read("acceptance-film")["record"]
    record["optical_constants"].append(_optical("batch-higher-index", 2.0))
    harness.library.write(record, update=True)
    _defaults(panel, bias_text="-2", bias_unit="relative_percent", uncertainty_kind="bounded",
              uncertainty_text="1", uncertainty_unit="relative_percent")
    layer = harness.session.design["front_layers"][0]
    layer["thickness"] = {"value": 1., "unit": "qw"}
    harness.session.design_revision += 1
    harness.owner.refreshDesign()
    assert panel.prepare()
    before = _physical_parameters(freeze_film_thickness_error_table_request(harness.service, _request(harness)))[0]
    assert before[2] == pytest.approx(-.02 * before[1])
    layer["optical_set_id"] = "batch-higher-index"
    layer["thickness"]["value"] = 2.
    harness.session.design_revision += 1
    harness.owner.refreshDesign()
    assert panel.prepare()
    after = _physical_parameters(freeze_film_thickness_error_table_request(harness.service, _request(harness)))[0]
    assert after[1] == pytest.approx(2 * 550. / (4 * 2.0))
    assert after[1] != before[1]
    assert after[2] == pytest.approx(-.02 * after[1])
    assert after[3] == pytest.approx(.01 * after[1])


def test_batch_stddev_does_not_invent_independence_and_retains_invalidated_matrix_identity(harness):
    panel = harness.panel
    _defaults(panel, uncertainty_text="1", uncertainty_unit="absolute_nm", uncertainty_kind="stddev")
    unknown = _request(harness)
    assert unknown.correlation_matrix is None
    text = "[[1,0.25],[0.25,1]]"
    assert panel.setTableCorrelationDraft(text)
    _defaults(panel, uncertainty_text="2")
    assert panel.tableError == ""
    assert _request(harness).correlation_matrix is not None
    _defaults(panel, uncertainty_kind="bounded")
    assert "rho 矩阵身份已失效" in str(panel.property("tableError"))
    assert panel.property("tableCorrelationJson") == text
    saved = panel.exportTableConfiguration()
    assert saved["correlation_invalidated"] is True
    restored = FilmSensitivityQuickController(harness.owner)
    try:
        assert restored.loadTableConfiguration(saved)
        assert restored.prepare()
        assert "rho 矩阵身份已失效" in str(restored.property("tableError"))
    finally:
        restored.shutdown(1000)


@pytest.mark.parametrize("mutation", ["version", "unknown", "unit", "duplicate", "flag"])
def test_invalid_table_configuration_is_atomic_and_does_not_emit_edit(harness, mutation):
    panel = harness.panel
    before = panel.exportTableConfiguration()
    bad = deepcopy(before)
    if mutation == "version":
        bad["version"] = 2
    elif mutation == "unknown":
        bad["unexpected"] = True
    elif mutation == "unit":
        cast(dict[str, str], bad["defaults"])["bias_unit"] = "percent_fraction"
    elif mutation == "duplicate":
        layers = cast(list[dict[str, object]], bad["layers"])
        layers.append(deepcopy(layers[0]))
    else:
        bad["include_finite_bias"] = 1
    edits = QSignalSpy(panel.tableConfigurationEdited)
    assert not panel.loadTableConfiguration(bad)
    assert panel.exportTableConfiguration() == before
    assert edits.count() == 0


def test_busy_batch_edits_are_rejected_without_changing_the_frozen_input(harness):
    panel = harness.panel
    before = panel.exportTableConfiguration()
    panel._busy = True
    try:
        assert not panel.setTableDefault("bias_text", "3")
        assert not panel.setTableLayerOverride(str(_rows(panel)[0]["layer_id"]), True)
        assert not panel.loadTableConfiguration({})
        assert panel.exportTableConfiguration() == before
    finally:
        panel._busy = False


def test_persisted_ui_metadata_does_not_change_the_prepared_physical_source(harness):
    panel = harness.panel
    _defaults(panel, bias_text="0", bias_unit="absolute_nm")
    base = harness.owner._build_request(require_variables=False, fixed_topology=False)
    design = deepcopy(dict(base.design_snapshot))
    design["workflow_settings"] = {"optimization": {"sensitivity_table": panel.exportTableConfiguration()}}
    assert panel._request_signature(replace(base, design_snapshot=design)) == panel._request_signature(base)
    harness.session.design["workflow_settings"] = deepcopy(design["workflow_settings"])
    harness.owner.refreshDesign()
    rebuilt = harness.owner._build_request(require_variables=False, fixed_topology=False)
    assert panel._request_signature(rebuilt) == panel._request_signature(base)
    design["front_layers"][0]["thickness"]["value"] += 1.
    assert panel._request_signature(replace(base, design_snapshot=design)) != panel._request_signature(base)
