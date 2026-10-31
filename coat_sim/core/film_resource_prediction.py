"""Online empirical resource prediction. No synthesis, optimizer or audit fallback.

A model must carry an independently validated domain and reference-flow semantics.
Missing calibration is not repaired by performing inverse design in this function.
"""
from __future__ import annotations

from dataclasses import replace
import json
import hashlib
import math
from pathlib import Path
import time
from typing import Any, Callable

import numpy as np

from .film_optimization_structure import _StructureCancelled
from .film_auto_synthesis import _validated as _validate_shared_input
from .optical_constants import prepare_optical_set
from .film_synthesis_absorption import sample_synthesis_optical
from .film_resource_estimation import (
    FilmResourceEstimateRequest, FilmResourceEstimateResult, FilmResourceEstimateProgress,
    freeze_resource_estimate_request, resource_estimate_source_digest,
)

_MODEL_PATH = Path(__file__).with_name('resources') / 'film_resource_prediction_v1.json'


class _OutsideDomain(ValueError):
    pass


def _features(frozen, *, real_domain=None):
    """Strict first-model domain; never silently flatten real dispersion/absorption."""
    base = frozen.synthesis_request
    env = base.environment
    if (base.selected_surfaces != ('front',) or env['substrate']['sides'] != 'single' or
            env.get('first_surface', 'front') != 'front'):
        raise _OutsideDomain('首批模型仅支持单前面、半无限基片；双面或整片光路尚未校准。')
    if base.minimum_nonzero_thickness_nm not in (None, 0) or base.max_layer_thickness_nm is not None:
        raise _OutsideDomain('首批模型未覆盖显式单层厚度制造限制，不能忽略这些限制后套用。')
    if len(base.objectives) != 1 or len(base.candidate_materials) != 2:
        raise _OutsideDomain('首批模型仅支持两个候选材料、单一波段的逐点减反或高反目标。')
    objective = base.objectives[0]
    criterion = objective.pointwise
    if (objective.metric != 'R' or objective.mean.enabled or not criterion.enabled or
            criterion.mode != 'range' or any(angle != 0 for angle in objective.angles_deg)):
        raise _OutsideDomain('首批模型仅支持正入射逐点 R 区间；不以均值或正入射近似其他指标。')
    if criterion.strict_lower or criterion.strict_upper:
        raise _OutsideDomain('首批模型尚未单独校准严格不等式目标。')
    if criterion.lower_bound in (None, 0.) and criterion.upper_bound is not None:
        family, target = 'AR', float(criterion.upper_bound)
    elif criterion.upper_bound in (None, 1.) and criterion.lower_bound is not None:
        family, target = 'HR', float(criterion.lower_bound)
    else:
        raise _OutsideDomain('首批模型未覆盖双侧反射区间或等值目标。')
    lo, hi = objective.wavelength_min_nm, objective.wavelength_max_nm
    if not 0 < lo < hi or not 0 < target < 1:
        raise _OutsideDomain('首批模型需要正带宽和介于0与1之间的指标。')
    optics = {(m['id'], o['id']): o for m in base.frozen_material_snapshot['materials']
              for o in m['optical_sets']}

    if family == 'AR' and real_domain is not None:
        real = _real_source_features(base, optics, lo, hi, target, real_domain)
        if real is not None:
            return family, real[0], (math.sqrt(lo) * math.sqrt(hi)), real[1]

    def constant(reference):
        if reference.get('kind') == 'constant':
            return float(reference['n'])
        optical = optics[reference['material_id'], reference['optical_set_id']]
        if optical.get('kind') != 'tabulated':
            raise _OutsideDomain('该膜料色散公式不在已核验真实源域内，也不属于常数折射率模型范围。')
        values, losses = optical.get('n'), optical.get('k')
        if not values or any(n != values[0] for n in values):
            raise _OutsideDomain('该材料的完整 n/k 数据曲线尚未注册验证；不会静默改用常数折射率近似。')
        if losses is None or any(k != 0 for k in losses):
            raise _OutsideDomain('首批模型仅校准已知 k=0；吸收或未知 k 任务尚未覆盖。')
        return float(values[0])

    if constant(env['incident_medium']) != 1.:
        raise _OutsideDomain('首批模型仅校准折射率为1的入射介质。')
    nS = constant(env['substrate'])
    nL, nH = sorted(constant(dict(material_id=r.material_id, optical_set_id=r.optical_set_id))
                    for r in base.candidate_materials)
    if not 0 < nL < nH:
        raise _OutsideDomain('候选材料必须有不同的正实折射率。')
    return family, np.array([nL, nH, nS, math.log(hi / lo),
                            -math.log(target if family == 'AR' else 1-target)]), (math.sqrt(lo) * math.sqrt(hi)), dict(domain='constant_index', conditional_on_assumption=False)


def optical_prediction_fingerprint(optical):
    # Numerical source identity, independent of user labels or private notes.
    fields = ('kind', 'wavelength_unit', 'wavelength_min', 'wavelength_max',
              'wavelengths', 'n', 'k', 'parameters')
    data = {key: optical.get(key) for key in fields}
    # Independent extinction tables and RII coefficients are also numerical
    # source data. Hash the entire table, not only feature-sampling locations.
    for key in ('formula_number', 'coefficients'):
        if optical.get(key) is not None:
            data[key] = optical[key]
    if optical.get('k_data') is not None:
        kd = optical['k_data']
        data['k_data'] = {key: kd.get(key) for key in (
            'wavelength_unit', 'wavelength_min', 'wavelength_max', 'wavelengths', 'k')}
    return hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _real_source_features(base, optics, lo, hi, target, domain):
    if domain.get('validation_status') != 'passed':
        return None
    env = base.environment
    incident = env['incident_medium']
    if incident.get('kind') == 'constant':
        if incident.get('n') != 1.:
            return None
    else:
        air = optics[incident['material_id'], incident['optical_set_id']]
        if (air.get('kind') != 'tabulated' or not air.get('n') or not air.get('k') or
                any(n != 1. for n in air['n']) or any(k != 0. for k in air['k'])):
            return None
    refs = [dict(material_id=r.material_id, optical_set_id=r.optical_set_id)
            for r in base.candidate_materials] + [env['substrate']]
    if any(r.get('kind') == 'constant' for r in refs):
        return None
    records = [optics[r['material_id'], r['optical_set_id']] for r in refs]
    signatures = [optical_prediction_fingerprint(record) for record in records]
    if any(sig in domain.get('excluded_low_source_fingerprints', []) for sig in signatures[:2]):
        raise _OutsideDomain('该块材色散公式作为膜料时，预测区间尚未达到验收要求，暂不列入快速预估支持域。')
    films = domain['low_source_fingerprints'] + domain['high_source_fingerprints']
    if any(sig not in films for sig in signatures[:2]) or signatures[2] not in domain['substrate_source_fingerprints']:
        return None
    if not domain['wavelength_min_nm'] <= lo < hi <= domain['wavelength_max_nm']:
        raise _OutsideDomain('已注册真实材料模型只验证了400–700 nm内的单窗口。')
    if hi/lo < domain['bandwidth_ratio_min']-1e-12 or hi/lo > domain['bandwidth_ratio_max']+1e-12:
        raise _OutsideDomain('当前相对带宽超出真实材料扩展域。')
    center = (math.sqrt(lo) * math.sqrt(hi))
    waves = np.unique(np.r_[np.linspace(lo, hi, 9), center])
    values = []
    assumption = False
    for record in records:
        sampler = prepare_optical_set(record, k_sampling_policy='source')
        row = [sample_synthesis_optical(sampler, float(wave),
            assume_unknown_k_zero=env['assume_lossless']) for wave in waves]
        values.append(np.array([(n, k) for n, k, _ in row]))
        assumption |= any(not details.get('k_known') for _, _, details in row)
    middle = int(np.where(waves == center)[0][0])
    order = sorted(range(2), key=lambda i: values[i][middle, 0])
    low, high = order
    if (signatures[low] not in domain['low_source_fingerprints'] or
            signatures[high] not in domain['high_source_fingerprints']):
        raise _OutsideDomain('真实材料扩展域需要一个已注册低折射率源和一个已注册高折射率源。')
    if values[low][:, 1].max() > domain['known_low_k_max'] or values[high][:, 1].max() > domain['known_high_k_max']:
        raise _OutsideDomain('已知吸收超出模型验证范围；没有将 k 改为零。')
    if np.any(values[2][:, 1] != 0):
        raise _OutsideDomain('当前基片吸收未在该真实材料扩展域中验证。')
    x = np.array([values[low][middle, 0], values[high][middle, 0], values[2][middle, 0],
                  math.log(hi/lo), -math.log(target)])
    details = dict(domain='registered_real_nk', conditional_on_assumption=bool(assumption),
        feature_source_samples=len(waves)*3, source_fingerprints=signatures,
        n_ranges=[[float(v[:, 0].min()), float(v[:, 0].max())] for v in values],
        k_ranges=[[float(v[:, 1].min()), float(v[:, 1].max())] for v in values],
        known_k_preserved=True)
    return x, details


def _basis(features):
    x = np.asarray(features, dtype=float)
    q = np.array([math.log(x[0]), math.log(x[1]/x[0]), math.log(x[2]), x[3], x[4]])
    return np.r_[q, [q[i]*q[j] for i in range(len(q)) for j in range(i, len(q))], x[3]/q[1]]


def _uncertainty_scales(features, model):
    if 'uncertainty_features' not in model:
        return np.ones(2)
    x = np.asarray(features)
    bank = np.asarray(model['uncertainty_features'], dtype=float)
    errors = np.asarray(model['uncertainty_abs_errors'], dtype=float)
    if bank.ndim != 2 or bank.shape[1] != len(x) or errors.shape != (len(bank), 2):
        raise ValueError('invalid empirical uncertainty matrix')
    d = np.sum(((bank-x)/np.asarray(model['feature_scales']))**2, axis=1)
    ids = np.argsort(d)[:int(model.get('uncertainty_neighbors', 8))]
    if not len(ids) or np.any(errors < 0) or not np.all(np.isfinite(errors)):
        raise ValueError('invalid empirical uncertainty residuals')
    return np.maximum(np.quantile(errors[ids], .8, axis=0), .05)


def _predict_model(features, model):
    """Fixed quadratic ridge formula; no fitting or design search during a query."""
    x = np.asarray(features, dtype=float)
    low, high = np.asarray(model['feature_min']), np.asarray(model['feature_max'])
    if np.any(x < low-1e-12) or np.any(x > high+1e-12):
        raise _OutsideDomain('输入超出该模型的独立验证范围，未做外推。')
    train = np.asarray(model['features'], dtype=float)
    paired = np.asarray(model['paired_log_resources'], dtype=float)
    scales = np.asarray(model['feature_scales'], dtype=float)
    if train.ndim != 2 or train.shape[1] != x.size or paired.shape != (len(train), 2):
        raise ValueError('prediction model matrix shape mismatch')
    if np.any(scales <= 0) or not np.all(np.isfinite(train)) or not np.all(np.isfinite(paired)):
        raise ValueError('prediction model contains invalid numerical data')
    distance = np.sum(((train-x)/scales)**2, axis=1)
    nearest = np.argsort(distance)[:int(model['neighbors'])]
    if not len(nearest) or math.sqrt(float(distance[nearest[0]])) > model['max_neighbor_distance']:
        raise _OutsideDomain('邻近校准资料不足；不能以远处材料案例代替本次预估。')
    if model.get('regressor') != 'ridge':
        raise ValueError('unsupported prediction regressor; only validated fixed ridge formulas are admitted')
    basis = _basis(x)
    mean, scale = np.asarray(model['basis_mean']), np.asarray(model['basis_scale'])
    coefficients, intercept = np.asarray(model['coefficients']), np.asarray(model['intercept'])
    if (mean.shape != basis.shape or scale.shape != basis.shape or
            coefficients.shape != (len(basis), 2) or intercept.shape != (2,) or
            not all(np.all(np.isfinite(v)) for v in (mean, scale, coefficients, intercept)) or
            np.any(scale <= 0)):
        raise ValueError('invalid fixed prediction formula parameters')
    center = ((basis-mean)/scale) @ coefficients + intercept
    radius = float(model['joint_log_radius'])
    if not math.isfinite(radius) or radius < 0:
        raise ValueError('invalid joint calibration radius')
    return center, radius * _uncertainty_scales(x, model)


def freeze_film_prediction_request(service: Any, request: FilmResourceEstimateRequest, *,
                                   cancel_event: Any = None) -> FilmResourceEstimateRequest:
    """Validate original semantics; freeze sources on a fixed nine-point feature grid.

    This invokes only shared input validation/freezing, never synthesis. Original
    objectives are restored verbatim in the frozen input/source identity. A large
    optimization sampling count must not turn prediction into a dense source scan.
    """
    # Existing candidate/seed stacks are not inputs to a from-scratch resource
    # forecast. They belong to the separate audit/verification entry.
    original = _validate_shared_input(replace(request.synthesis_request,
        seed_designs=(), search_checkpoint=None, warm_start_seed_index=None))
    sparse = replace(original, objectives=tuple(replace(o, wavelength_samples=5)
        for o in original.objectives), validation_samples_multiplier=2)
    frozen = freeze_resource_estimate_request(service, replace(request,
        synthesis_request=sparse, candidate_designs=(), audit_candidates=False, probe_max_evaluations=0),
        cancel_event=cancel_event)
    return replace(frozen, synthesis_request=replace(frozen.synthesis_request,
        objectives=original.objectives, validation_samples_multiplier=original.validation_samples_multiplier))


def predict_film_resources(service: Any, request: FilmResourceEstimateRequest, *, cancel_event: Any = None,
                           progress_callback: Callable[[FilmResourceEstimateProgress], None] | None = None) -> FilmResourceEstimateResult:
    """Validate/freeze inputs and infer from an offline model, without optical solves."""
    started = time.perf_counter()
    source = ''
    input_digest = ''
    sides: tuple[str, ...] = ()
    work: dict[str, Any] = dict(assessment_mode='quick_prediction', physical_evaluations=0,
                probe_runs=0, audits_started=0, elapsed_seconds=0.)

    def result(state: str, estimate_state: str, message: str,
               prediction: dict[str, Any] | None = None, input_status: str = 'ready') -> FilmResourceEstimateResult:
        work['elapsed_seconds'] = time.perf_counter()-started
        return FilmResourceEstimateResult(state=state, input_status=input_status, message=message,
            source_digest=source, input_digest=input_digest, selected_surfaces=sides, work=dict(work),
            estimate_state=estimate_state, prediction=prediction)

    if not isinstance(request, FilmResourceEstimateRequest):
        return result('failed', 'not_calibrated', 'request must be FilmResourceEstimateRequest', input_status='invalid')
    if cancel_event is not None and cancel_event.is_set():
        return result('cancelled', 'not_calibrated', '快速预估已取消。')
    try:
        work['source_freeze_attempts'] = 1
        frozen = freeze_film_prediction_request(service, request, cancel_event=cancel_event)
        work['source_freeze_passes'] = 1
        source = resource_estimate_source_digest(frozen)
        sides = frozen.synthesis_request.selected_surfaces
        if not _MODEL_PATH.is_file():
            return result('completed', 'not_calibrated', '离线模型尚未通过独立校准；本次未产生预测。')
        raw_model = _MODEL_PATH.read_bytes()
        work['model_sha256'] = hashlib.sha256(raw_model).hexdigest()
        input_digest = hashlib.sha256(json.dumps(dict(source_digest=source,
            model_sha256=work['model_sha256'], protocol='fixed9_joint_prediction_v1'),
            sort_keys=True).encode()).hexdigest()
        package = json.loads(raw_model)
        if not isinstance(package, dict):
            raise ValueError('prediction model package must be an object')
        if package.get('schema_version') != 1 or package.get('validation_status') != 'passed':
            return result('completed', 'not_calibrated', '离线模型未通过独立验证；本次未产生预测。')
        family, features, reference_wave, domain_details = _features(frozen, real_domain=package.get('real_ar_domain'))
        work['feature_domain'] = domain_details
        if cancel_event is not None and cancel_event.is_set():
            return result('cancelled', 'not_calibrated', '快速预估已取消。')
        if family not in package['families']:
            raise _OutsideDomain('该目标族尚无通过独立验证的预测模型。')
        model = package['families'][family]
        if domain_details['domain'] == 'registered_real_nk':
            model = dict(model, feature_min=package['real_ar_domain']['feature_min'],
                feature_max=package['real_ar_domain']['feature_max'],
                coverage_note=package['real_ar_domain']['coverage_note'],
                reference_profile=package['real_ar_domain']['reference_profile'])
        center, radius = _predict_model(features, model)
        count = max(1, round(math.expm1(float(center[0]))))
        thickness = reference_wave * math.exp(float(center[1]))
        prediction = dict(model_id=package['model_id'], model_label=package.get('model_label'),
            reference_profile_label=model.get('reference_profile_label'), reference_profile=model.get('reference_profile', package['reference_profile']),
            layer_count=count, total_thickness_nm=thickness,
            joint_interval=dict(layer_min=max(0, math.floor(math.expm1(float(center[0])-float(radius[0])))),
                layer_max=math.ceil(math.expm1(float(center[0])+float(radius[0]))),
                thickness_min_nm=reference_wave*math.exp(float(center[1])-float(radius[1])),
                thickness_max_nm=reference_wave*math.exp(float(center[1])+float(radius[1]))),
            coverage_note=model['coverage_note'], applicability_note=package['applicability_note'],
            conditional_on_assumption=domain_details['conditional_on_assumption'],
            known_k_preserved=True, feature_domain=domain_details['domain'],
            inference_kind='fixed_quadratic_ridge',
            uncertainty_kind='offline_calibrated_local_residual_lookup',
            claim_level='reference_process_empirical_prediction_not_physical_minimum',
            joint_region_note='同一联合模型的误差区域外接区间；不是其中每个角点都对应实际膜系。')
        if not all(math.isfinite(float(v)) and v > 0 for v in (thickness,
                prediction['joint_interval']['thickness_min_nm'],
                prediction['joint_interval']['thickness_max_nm'])):
            raise ValueError('resource prediction exceeded finite numerical range')
        if cancel_event is not None and cancel_event.is_set():
            return result('cancelled', 'not_calibrated', '快速预估已取消。')
        return result('completed', 'estimated', '经验资源预估完成；未调用合成、优化或候选审计。', prediction)
    except _StructureCancelled:
        return result('cancelled', 'not_calibrated', '快速预估已取消。')
    except _OutsideDomain as exc:
        return result('completed', 'out_of_domain', str(exc))
    except (ValueError, TypeError, KeyError, ArithmeticError, OSError) as exc:
        return result('failed', 'not_calibrated', str(exc), input_status='ready' if source else 'invalid')
