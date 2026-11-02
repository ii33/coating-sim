"""Real controller prepares both physical sides for a single joint repair."""
from copy import deepcopy

import pytest

pytest_plugins = ("tests.test_film_manufacturing_quick_controller",)

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.mark.parametrize("incidence", ["front", "back"])
def test_controller_prepares_both_surfaces_and_materials(harness, incidence):
    h = harness
    h.session.design['substrate']['sides'] = 'double'
    h.session.design['first_surface'] = incidence
    back = deepcopy(h.session.design['front_layers'][0])
    back['id'] = 'real-back-layer'
    h.session.design['back_layers'] = [back]
    h.session.design_revision += 1
    assert h.panel.prepare(), h.panel.error
    assert len(h.panel.layerRows) == 2
    assert h.panel.layerRows[0]['label'].startswith('正面')
    assert h.panel.layerRows[1]['label'].startswith('背面')
    for name, value in [('reference_nm', '110'), ('thickness_tolerance_nm', '1'),
                        ('optical_tolerance_percentage_points', '1')]:
        assert h.panel.setOption(name, value)
    assert h.panel.startAutomatic(), h.panel.error
    task = h.pool.tasks[-1]
    assert task.synthesis.selected_surfaces == ('front', 'back')
    assert task.source['first_surface'] == incidence
    assert task.options['comparison_settings']['first_surface'] == incidence
    assert not task.synthesis.environment['front_layers']
    assert not task.synthesis.environment['back_layers']
    assert len(task.source['front_layers']) == len(task.source['back_layers']) == 1
    assert {(r.material_id, r.optical_set_id) for r in task.synthesis.candidate_materials} == {
        (back['material_id'], back['optical_set_id'])}


def test_single_substrate_live_totals_exclude_inactive_back_draft(harness, monkeypatch):
    design = deepcopy(harness.session.design)
    design['substrate']['sides'] = 'single'
    back = deepcopy(design['front_layers'][0])
    back['id'] = 'inactive-back-draft'
    design['back_layers'] = [back]
    snapshot = {'design': design}
    monkeypatch.setattr(harness.panel, '_live_display_snapshots',
                        lambda: (snapshot, snapshot, None, 'source_unchanged', 0))
    totals = harness.panel.liveTotals
    assert totals['original_layers'] == totals['current_layers'] == 1
    assert totals['original_nm'] == totals['current_nm'] == design['front_layers'][0]['thickness']['value']
