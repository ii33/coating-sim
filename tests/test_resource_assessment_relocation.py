"""Contracts for resource assessment's independent, read-only physical draft."""
from copy import deepcopy
import threading
from typing import Any

import pytest

from coat_sim.ui import film_resource_estimate_controller as module
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import saved_configuration


def test_selected_substrate_is_frozen_for_environment_and_candidate(tmp_path):
    service, _library, design = fixture(tmp_path)
    config = saved_configuration(service, design)
    config['synthesis']['substrate_reference'] = {'material_id': 'old', 'optical_set_id': 'old-set'}
    design['workflow_settings'] = {'optimization': config}
    before = deepcopy(design)
    selected = {'material_id': 'new-glass', 'optical_set_id': 'new-set'}
    request = module._prepare_request(design, 19, 8, threading.Event(),
                                      [{'material_id': 'acceptance-film', 'optical_set_id': 'film-set'}], selected)
    assert request.candidate_designs[0]['substrate']['material_id'] == 'new-glass'
    assert request.candidate_designs[0]['substrate']['optical_set_id'] == 'new-set'
    assert request.synthesis_request.environment['substrate']['material_id'] == 'new-glass'
    assert request.synthesis_request.environment['substrate']['optical_set_id'] == 'new-set'
    assert design == before


def test_missing_substrate_reference_rejected(tmp_path):
    service, _library, design = fixture(tmp_path)
    design['workflow_settings'] = {'optimization': saved_configuration(service, design)}
    with pytest.raises(ValueError, match='基片'):
        module._prepare_request(design, 19, 8, threading.Event(),
                                [{'material_id': 'acceptance-film', 'optical_set_id': 'film-set'}],
                                {'material_id': 'new-glass', 'optical_set_id': ''})


def test_failed_audit_does_not_claim_target_failure():
    summary = module._format_summary({'state': 'completed', 'candidate_reports': [{
        'state': 'error', 'message': 'source unavailable', 'targets_met': False,
        'layer_count': 0, 'total_thickness_nm': 0,
        'audit': {'objective_status': [{'metric': 'R', 'pointwise': {'enabled': True,
                    'targets_met': False, 'worst_actual': .04}}]},
    }]})
    assert '未取得完整审计' in summary
    assert '目标未通过' not in summary and '未达标' not in summary
    assert '4%' not in summary


def test_audited_actual_percent_and_unknown_witness_are_not_conflated():
    summary = module._format_summary({'state': 'completed', 'candidate_reports': [{
        'state': 'audited', 'targets_met': False, 'layer_count': 0, 'total_thickness_nm': 0,
        'audit': {'objective_status': [{'objective_index': 0, 'metric': 'R',
                  'wavelength_min_nm': 500., 'wavelength_max_nm': 500.,
                  'pointwise': {'enabled': True, 'mode': 'range', 'worst_actual': .04,
                                'lower_bound': 0., 'upper_bound': .001,
                                'worst_angle_deg': 0., 'worst_wavelength_nm': 500.,
                                'targets_met': False}}]}}]})
    assert '4%' in summary and '0.1%' in summary and '未达标' in summary
    assert '不等同全谱极值' in summary and '可能只是代表点' in summary
    assert '这不代表不存在可行方案' in summary
    assert '本次未进行快速预估' in summary


def test_probe_budget_and_seeds_are_frozen_in_request(tmp_path):
    service, _library, design = fixture(tmp_path)
    design['workflow_settings'] = {'optimization': saved_configuration(service, design)}
    before = deepcopy(design)
    request = module._prepare_request(
        design, 19, 8, threading.Event(),
        [{'material_id': 'acceptance-film', 'optical_set_id': 'film-set'}],
        {'material_id': 'acceptance-glass', 'optical_set_id': 'glass-set'},
        probe_max_evaluations=7, probe_seeds=(2, 3), audit_candidates=True,
    )
    assert request.probe_max_evaluations == 7
    assert request.probe_seeds == (2, 3)
    assert request.audit_candidates is True
    assert design == before


def test_probe_summary_reports_real_effort_and_incomplete_counters():
    summary = module._format_summary({'state': 'completed', 'work': {
        'probe_outcomes': [{'seed': 2, 'state': 'completed', 'evaluations': 7, 'logical_evaluations_observed': 6,
                            'requested_max_evaluations': 9, 'physical_evaluations': 5,
                            'candidates_retained': 1, 'evaluation_window_exceeded': True}],
        'physical_evaluations': 6, 'wavelength_points': 60,
    }})
    assert '种子 2' in summary and '7 / 9' in summary
    assert '物理光谱计算 5 次' in summary and '全部物理光谱计算 6 次' in summary
    assert '不代表搜索穷尽' in summary
    assert '超出请求窗口' in summary
    incomplete = module._format_summary({'state': 'completed', 'work': {
        'probe_outcomes': [{'seed': 3, 'state': 'failed'}], 'actual_work_complete': False,
    }})
    assert '工作量统计不完整' in incomplete
    assert '逻辑评价 0 /' not in incomplete and '物理光谱计算 0 次' not in incomplete


def test_cancelled_probe_shows_recorded_work_without_claiming_completion():
    summary = module._format_summary({'state': 'cancelled', 'work': {
        'probe_outcomes': [{'seed': 1, 'state': 'cancelled',
                            'logical_evaluations_observed': 4,
                            'requested_max_evaluations': 256,
                            'physical_evaluations': 3, 'candidates_retained': 0}],
        'actual_work_complete': False,
    }})
    assert '未完成' in summary or '取消' in summary
    assert '4 / 256' in summary and '物理光谱计算 3 次' in summary
    assert '工作量可能不完整' in summary
    assert '不存在可行方案' not in summary or '不代表' in summary


@pytest.mark.parametrize(('conditional', 'evidence', 'expected'), [
    (True, {'conditional_on_assumption': True}, '依赖未知 k 按零假设'),
    (False, {'conditional_on_assumption': False}, '未使用未知 k 按零假设'),
    (None, {}, '吸收假设状态未知'),
    (False, {'conditional_on_assumption': True}, '吸收假设状态未知'),
])
def test_near_best_is_audited_admissible_and_kept_alongside_witness(conditional, evidence, expected):
    best = {'source': 'probe', 'state': 'audited', 'targets_met': False,
            'within_constraints': True, 'within_material_pool': True,
            'max_normalized_error': .12, 'layer_count': 2, 'total_thickness_nm': 120.,
            'conditional_on_assumption': conditional, 'absorption_assumption': evidence,
            'audit': {'objective_status': [{'objective_index': 0, 'metric': 'R',
                'pointwise': {'enabled': True, 'worst_actual': .04,
                    'worst_angle_deg': 0., 'worst_wavelength_nm': 500., 'targets_met': False}}]}}
    excluded = {**best, 'max_normalized_error': .001, 'layer_count': 99, 'within_constraints': False}
    unaudited = {**best, 'max_normalized_error': .0001, 'layer_count': 88, 'state': 'not_audited'}
    summary = module._format_summary({'state': 'completed',
        'candidate_reports': [best, excluded, unaudited],
        'recommended_budget': [{'layer_count': 3, 'total_thickness_nm': 130.,
                                'conditional_on_assumption': False,
                                'absorption_assumption': {'conditional_on_assumption': False}}]})
    assert '有限采样审计见证' in summary and '最接近探测候选（所选膜面合计）：2 层 / 120' in summary
    assert expected in summary and '4%' in summary and '未达标，不是可行见证' in summary
    assert '最接近探测候选（所选膜面合计）：99 层' not in summary and '最接近探测候选（所选膜面合计）：88 层' not in summary


def test_selected_face_total_separates_fixed_unselected_surface_with_unknowns():
    row: dict[str, Any] = {'source': 'probe', 'state': 'audited', 'targets_met': False,
           'within_constraints': True, 'within_material_pool': True,
           'max_normalized_error': .1, 'layer_count': 2, 'total_thickness_nm': 120.,
           'surfaces': {'front': {'active': True, 'selected': True, 'layer_count': 2, 'total_thickness_nm': 120.},
                        'back': {'active': True, 'selected': False, 'layer_count': 3, 'total_thickness_nm': 450.}}}
    summary = module._format_summary({'state': 'completed', 'selected_surfaces': ['front'], 'candidate_reports': [row]})
    assert '所选膜面合计' in summary and '2 层 / 120.0 nm' in summary
    assert '未选固定面：背面 3 层 / 450 nm' in summary
    row['surfaces']['back'].update(layer_count=None, total_thickness_nm=None)
    summary = module._format_summary({'state': 'completed', 'selected_surfaces': ['front'], 'candidate_reports': [row]})
    assert '未选固定面：背面 未知 层 / 厚度未统计' in summary
    assert '背面 0 层' not in summary
    row['surfaces']['back']['active'] = False
    summary = module._format_summary({'state': 'completed', 'selected_surfaces': ['front'], 'candidate_reports': [row]})
    assert '未选固定面：背面' not in summary
    summary = module._format_summary({'state': 'completed', 'candidate_reports': [row]})
    assert '选面信息未知，固定面未列出' in summary
