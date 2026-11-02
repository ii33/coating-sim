"""Two-sided repair contracts: frozen materials, side lineage and display."""
from copy import deepcopy
from dataclasses import replace

import pytest

from coat_sim.core import film_automatic_repair as repair
from coat_sim.core import film_automatic_repair_jobs as jobs
from coat_sim.ui.film_manufacturing_quick_adapter import (
    comparison_rows, layer_change_rows, layer_total_nm, snapshot_text,
)
from tests.test_film_automatic_repair_double_substrate import _problem, _make

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('incidence', ['front', 'back'])
def test_two_sided_freeze_closes_materials_and_preserves_inputs(live, incidence):
    service, synthesis, source = _problem()
    source['first_surface'] = incidence
    synthesis = replace(synthesis, environment={**deepcopy(synthesis.environment), 'first_surface': incidence},
                        candidate_materials=(synthesis.candidate_materials[0],))
    source['back_layers'][0]['thickness'] = {'value': .2, 'unit': 'qw'}
    original = deepcopy(source)
    payload = _make(service, synthesis, source, live=live)
    frozen, normalized = jobs.request_from_payload(payload)
    assert source == original
    assert normalized['first_surface'] == incidence
    assert normalized['back_layers'][0]['thickness']['unit'] == 'nm'
    assert normalized['back_layers'][0]['thickness']['value'] > 0
    assert ('low', 'nk') in {(r.material_id, r.optical_set_id) for r in frozen.candidate_materials}
    assert {'high', 'low'} <= {m['id'] for m in frozen.frozen_material_snapshot['materials']}
    assert frozen.selected_surfaces == ('front', 'back')


def test_two_sided_lineage_is_strictly_bound_to_side_and_material():
    _service, _synthesis, source = _problem()
    layers = repair.live_layers(source, {r['id']: [r['id']] for r in repair.repair_rows(source)})
    jobs._validate_live_layers(layers, source, source)
    assert [r['side'] for r in layers] == ['front', 'front', 'back']
    bad = deepcopy(layers)
    bad[-1]['side'] = 'front'
    with pytest.raises(ValueError):
        jobs._validate_live_layers(bad, source, source)
    bad = deepcopy(layers)
    bad[-1]['origins'] = [source['front_layers'][-1]['id']]
    with pytest.raises(ValueError):
        jobs._validate_live_layers(bad, source, source)
    bad = deepcopy(layers)
    bad[-1]['origins'] = []
    with pytest.raises(ValueError):
        jobs._validate_live_layers(bad, source, source)
    moved = deepcopy(source)
    moved['front_layers'].append(moved['back_layers'].pop())
    forged_layers = repair.live_layers(moved, {r['id']: [] for r in repair.repair_rows(moved)})
    with pytest.raises(ValueError):
        jobs._validate_live_layers(forged_layers, moved, source)
    duplicated = deepcopy(source)
    extra = deepcopy(duplicated['front_layers'][-1])
    extra['id'] = 'inserted'
    duplicated['front_layers'].append(extra)
    forged_layers = repair.live_layers(duplicated, {r['id']: [r['id']] for r in repair.repair_rows(source)})
    forged_layers[2]['origins'] = [source['front_layers'][-1]['id']]
    with pytest.raises(ValueError, match='duplicated across rows'):
        jobs._validate_live_layers(forged_layers, duplicated, source)


def test_design_fallback_display_includes_both_sides_and_total():
    _service, _synthesis, source = _problem()
    snapshot = {'design': source}
    rows = layer_change_rows(snapshot, snapshot, None)
    assert len(rows) == 3
    assert rows[-1]['label'].startswith('背面 · 原第1层')
    assert layer_total_nm(snapshot) == pytest.approx(40.9)
    details = snapshot_text(snapshot, [])
    assert '层数 3' in details and '最薄 0.4' in details and '总厚度 40.9' in details
    payload = {'schema_version': 3, 'request': {'synthesis': {'request': {'objectives': []}},
               'original_targets_present': False}, 'result': {'source': snapshot}}
    details = comparison_rows(payload)[0]['details']
    assert '层数 3' in details and '总厚 40.9' in details


def test_live_job_double_sided_result_and_progress_validate_strictly(tmp_path):
    from threading import Event, Timer

    service, synthesis, source = _problem()
    payload = _make(service, synthesis, source, live=True)
    job = jobs.build_job_payload(service, payload)
    stop = Event()
    timer = Timer(20., stop.set)
    frames = []
    def report(frame):
        frames.append(deepcopy(frame))
        if frame.get('champion'):
            stop.set()
    timer.start()
    try:
        result = jobs.execute_job({'payload': job}, stop, report, tmp_path / 'checkpoint.json',
                                  lambda _path, _value: None)
    finally:
        timer.cancel()
    assert result['result']['source'] is not None
    assert result['result']['best'] is not None
    jobs.validate_result_payload(result, expected_job=job, replay=True)
    for frame in frames:
        jobs.validate_live_progress(frame, payload)
    forged = deepcopy(result)
    forged['result']['best']['layers'][-1]['side'] = 'front'
    with pytest.raises(ValueError):
        jobs.validate_result_payload(forged, expected_job=job, replay=False)


def test_v2_job_double_sided_result_strictly_rejects_physical_tampering(tmp_path):
    from threading import Event, Timer

    service, synthesis, source = _problem()
    payload = _make(service, synthesis, source)
    job = jobs.build_job_payload(service, payload)
    stop = Event()
    timer = Timer(20., stop.set)
    def report(frame):
        if frame.get('stage') == 'repair_audited_best':
            stop.set()
    timer.start()
    try:
        result = jobs.execute_job({'payload': job}, stop, report, tmp_path / 'checkpoint.json',
                                  lambda _path, _value: None)
    finally:
        timer.cancel()
    assert result['result']['source'] is not None
    assert result['result']['best'] is not None
    jobs.validate_result_payload(result, expected_job=job, replay=False)
    forged = deepcopy(result)
    forged['result']['best']['design']['back_layers'][0]['material_id'] = 'unauthorized'
    with pytest.raises(ValueError):
        jobs.validate_result_payload(forged, expected_job=job, replay=False)


def test_v2_rejects_original_id_moved_between_sides(tmp_path):
    from threading import Event
    service, synthesis, source = _problem()
    payload = _make(service, synthesis, source)
    job = jobs.build_job_payload(service, payload)
    stop = Event()
    def report(frame):
        if frame.get('stage') == 'repair_audited_best':
            stop.set()
    result = jobs.execute_job({'payload': job}, stop, report, tmp_path / 'checkpoint.json',
                              lambda _path, _value: None)
    bad = deepcopy(result)
    candidate = bad['result']['best']['design']
    candidate['front_layers'].append(candidate['back_layers'].pop())
    with pytest.raises(ValueError, match='moved an original layer'):
        jobs.validate_result_payload(bad, expected_job=job, replay=False)


def test_v2_rejects_original_id_relabelled_with_other_frozen_material(tmp_path):
    from threading import Event
    service, synthesis, source = _problem()
    payload = _make(service, synthesis, source)
    job = jobs.build_job_payload(service, payload)
    stop = Event()
    def report(frame):
        if frame.get('stage') == 'repair_audited_best':
            stop.set()
    result = jobs.execute_job({'payload': job}, stop, report, tmp_path / 'checkpoint.json',
                              lambda _path, _value: None)
    bad = deepcopy(result)
    candidate = bad['result']['best']['design']
    candidate['back_layers'][0]['material_id'] = source['front_layers'][0]['material_id']
    with pytest.raises(ValueError, match='changed an original layer material'):
        jobs.validate_result_payload(bad, expected_job=job, replay=False)
