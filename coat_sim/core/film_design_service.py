"""Film-design operations shared by desktop, API and MCP; no Qt dependency."""
from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import json
import math
import os
import sqlite3
import struct
import sys
from pathlib import Path
import tempfile

import numpy as np

from .api import CoatService
from .design_files import validate_design_path
from .film_design import (new_design, validate_design, save_design, load_design,
                          expand_expression, physical_thickness_nm)
from .film_design_store import FilmDesignStore
from .film_expression_store import FilmExpressionTemplateStore
from .film_materials import FilmMaterialLibrary
from .film_optics import coherent_rta, incoherent_plate_rta
from .optical_constants import (evaluate_optical_set, evaluate_optical_set_with_details,
                                normalize_extrapolation_policy, prepare_optical_set)
from .atomic_io import replace_with_retry


_SIMULATION_MAX_CHUNK_SAMPLES = 4096
_SIMULATION_WORKING_SET_TARGET_BYTES = 32 * 1024 * 1024
_SIMULATION_WORK_BYTES_PER_MEDIUM_SAMPLE = 256


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('Film spectrum calculation cancelled')


def _validate_sample_count(low, high, count):
    if count > np.iinfo(np.intp).max:
        raise ValueError('采样点数超出当前运行时可寻址的数组长度')
    if count <= 1:
        return
    step = (high - low) / (count - 1)
    if not math.isfinite(step) or step <= 0:
        raise ValueError('采样间隔无法表示为有限正数')
    if low + step <= low or high - step >= high:
        raise ValueError('采样间隔低于该波长范围的浮点可分辨精度')


def _simulation_chunk_size(active_media_count):
    medium_count = max(1, int(active_media_count))
    estimated_bytes_per_sample = medium_count * _SIMULATION_WORK_BYTES_PER_MEDIUM_SAMPLE
    adaptive = _SIMULATION_WORKING_SET_TARGET_BYTES // estimated_bytes_per_sample
    return max(1, min(_SIMULATION_MAX_CHUNK_SAMPLES, int(adaptive)))


def estimate_film_simulation_resources(settings, design, *, extra_polarizations=0):
    """Estimate retained full-resolution output plus one bounded working chunk.

    This is a UI hint, not a calculation quota or a promise about total process
    peak memory. The retained-output estimate uses the current Python list and
    pointer sizes; chunk scratch uses a deliberately conservative project rule.
    """
    samples = int(settings['samples'])
    media_count = (len(design.get('front_layers', ())) +
                   len(design.get('back_layers', ())) + 3)
    extra_polarizations = max(0, int(extra_polarizations))
    float_list_count = 8 * (1 + extra_polarizations) + 3 * extra_polarizations
    bytes_per_list_value = sys.getsizeof(0.0) + struct.calcsize('P')
    retained_output_bytes = samples * float_list_count * bytes_per_list_value
    wavelength_array_bytes = samples * np.dtype(float).itemsize
    chunk_samples = _simulation_chunk_size(media_count)
    chunk_working_bytes = (chunk_samples * media_count *
                           _SIMULATION_WORK_BYTES_PER_MEDIUM_SAMPLE)
    return {
        'samples': samples,
        'active_medium_count': media_count,
        'chunk_samples': chunk_samples,
        'retained_output_bytes': retained_output_bytes,
        'wavelength_array_bytes': wavelength_array_bytes,
        'chunk_working_bytes': chunk_working_bytes,
        'estimated_bytes': retained_output_bytes + wavelength_array_bytes + chunk_working_bytes,
    }


def _finite(value, label):
    if isinstance(value, bool):
        raise ValueError(label + ' 必须为有限数值')
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(label + ' 必须为有限数值') from exc
    if not math.isfinite(number):
        raise ValueError(label + ' 必须为有限数值')
    return number


def _trapezoid(values, coordinates):
    values = np.asarray(values, dtype=float)
    coordinates = np.asarray(coordinates, dtype=float)
    return float(np.sum((values[:-1] + values[1:]) * np.diff(coordinates) * 0.5))


def standard_air_refractive_index(wavelength_vacuum_nm):
    """Edlen 1966 standard dry-air dispersion; input is vacuum wavelength in nm.

    Standard air is 15 C, 101325 Pa, dry, 300 ppm CO2. CIE tabulations use
    standard-air wavelength coordinates; the solver uses vacuum nm.
    """
    waves = np.asarray(wavelength_vacuum_nm, dtype=float)
    if np.any(~np.isfinite(waves)) or np.any((waves < 299.0) | (waves > 831.0)):
        raise ValueError('standard-air conversion is supported only over 300–830 nm')
    sigma = 1000.0 / waves
    refractivity = 1e-8 * (8342.13 + 2406030.0 / (130.0 - sigma**2) +
                           15997.0 / (38.9 - sigma**2))
    return 1.0 + refractivity


def standard_air_to_vacuum_nm(wavelength_air_nm):
    air = np.asarray(wavelength_air_nm, dtype=float)
    vacuum = air * 1.00028
    for _ in range(5):
        vacuum = air * standard_air_refractive_index(vacuum)
    return vacuum


def vacuum_to_standard_air_nm(wavelength_vacuum_nm):
    vacuum = np.asarray(wavelength_vacuum_nm, dtype=float)
    return vacuum / standard_air_refractive_index(vacuum)


def standard_air_density_jacobian(wavelength_vacuum_nm):
    """d(lambda_air)/d(lambda_vac) for spectral-density conversion."""
    waves = np.asarray(wavelength_vacuum_nm, dtype=float)
    sigma = 1000.0 / waves
    dn_dsigma = 1e-8 * (2406030.0 * 2.0 * sigma / (130.0 - sigma**2)**2 +
                        15997.0 * 2.0 * sigma / (38.9 - sigma**2)**2)
    dn_dlambda = dn_dsigma * (-1000.0 / waves**2)
    index = standard_air_refractive_index(waves)
    return (index - waves * dn_dlambda) / index**2


def simulation_settings(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError('计算设置必须为对象')
    allowed = {'wavelength_min_nm','wavelength_max_nm','samples','step_nm','angle_deg',
               'polarization','light_source','first_surface','extrapolation_policy'}
    if set(value) - allowed:
        raise ValueError('未知计算设置: ' + ', '.join(sorted(set(value)-allowed)))
    low = _finite(value.get('wavelength_min_nm',400), '波长下限')
    high = _finite(value.get('wavelength_max_nm',700), '波长上限')
    if not 0 < low or high < low:
        raise ValueError('波长须为正且上限不能小于下限 (nm)')
    single_wavelength = low == high
    count = value.get('samples')
    minimum_count = 1 if single_wavelength else 2
    if count is not None and (type(count) is not int or count < minimum_count):
        raise ValueError(f'采样点须为至少 {minimum_count} 的整数')
    target_step = value.get('step_nm')
    if target_step is not None:
        target_step = _finite(target_step, '波长采样间隔')
        if target_step <= 0:
            raise ValueError('波长采样间隔须为正数 (nm)')
        if single_wavelength:
            count = 1
        else:
            intervals = (high - low) / target_step
            if not math.isfinite(intervals):
                raise ValueError('step_nm 换算后的采样点数超出当前运行时可寻址的数组长度')
            count = max(2, int(math.floor(intervals + 0.5)) + 1)
    elif single_wavelength:
        count = 1
    elif count is None:
        count = 301
    if not minimum_count <= count:
        raise ValueError(f'采样点须为至少 {minimum_count} 的整数')
    _validate_sample_count(low, high, count)
    actual_step = 0.0 if count == 1 else (high - low) / (count - 1)
    angle = _finite(value.get('angle_deg',0),'入射角')
    if not 0 <= angle < 90:
        raise ValueError('入射角须为 [0,90) 度')
    polarization = value.get('polarization','unpolarized')
    if polarization not in ('s','p','unpolarized'):
        raise ValueError('偏振须为 s、p 或 unpolarized')
    first = value.get('first_surface','front')
    if first not in ('front','back'):
        raise ValueError('首入射面须为 front/back')
    extrapolation_policy = normalize_extrapolation_policy(value.get('extrapolation_policy','endpoint_hold'))
    source = value.get('light_source',{'kind':'flat'})
    if not isinstance(source, dict):
        raise ValueError('光源须为对象')
    kind = source.get('kind','flat')
    if kind == 'flat' and set(source) <= {'kind'}:
        source = {'kind':'flat'}
    elif kind == 'blackbody' and set(source) <= {'kind','temperature_k'}:
        temperature = _finite(source.get('temperature_k',5778),'黑体温度')
        if not 1 <= temperature <= 1e7:
            raise ValueError('黑体温度须为 1–1e7 K')
        source = {'kind':kind, 'temperature_k':temperature}
    elif kind == 'tabulated' and set(source) <= {
            'kind','wavelength_nm','intensity','source','wavelength_basis',
            'dataset_id','snapshot_date','data_license','source_page','doi','catalog_version','source_warnings'}:
        waves = source.get('wavelength_nm')
        intensity = source.get('intensity')
        if not isinstance(waves,list) or not isinstance(intensity,list) or not 2 <= len(waves) <= 100000 or len(waves)!=len(intensity):
            raise ValueError('invalid tabulated source arrays')
        waves = [_finite(x,'source wavelength') for x in waves]
        intensity = [_finite(x,'source intensity') for x in intensity]
        if waves[0]<=0 or any(b<=a for a,b in zip(waves,waves[1:])) or min(intensity)<0 or max(intensity)<=0:
            raise ValueError('invalid tabulated source data')
        basis = source.get('wavelength_basis', 'vacuum')
        if basis not in ('vacuum', 'standard_air'):
            raise ValueError('tabulated wavelength_basis must be vacuum or standard_air')
        coverage = ([low, high] if basis == 'vacuum' else
                    vacuum_to_standard_air_nm(np.asarray([low, high], dtype=float)).tolist())
        if coverage[0] < waves[0] or coverage[1] > waves[-1]:
            raise ValueError('calculation range exceeds tabulated source coverage; extrapolation is disabled')
        normalized_source = {'kind':kind,'wavelength_nm':waves,'intensity':intensity,
                             'source':str(source.get('source','')), 'wavelength_basis':basis}
        for metadata_key in ('dataset_id','snapshot_date','data_license','source_page','doi','catalog_version'):
            metadata_value = source.get(metadata_key)
            if metadata_value is not None:
                if not isinstance(metadata_value, str):
                    raise ValueError('tabulated source provenance fields must be strings')
                normalized_source[metadata_key] = metadata_value
        source_warnings = source.get('source_warnings', [])
        if not isinstance(source_warnings, (list, tuple)) or not all(isinstance(item, str) for item in source_warnings):
            raise ValueError('tabulated source_warnings must be a list of strings')
        if source_warnings: normalized_source['source_warnings'] = list(source_warnings)
        source = normalized_source
    else:
        raise ValueError('光源须为 flat、blackbody 或 tabulated；不接受未知字段')
    return dict(wavelength_min_nm=low,wavelength_max_nm=high,samples=count,step_nm=actual_step,
                angle_deg=angle,polarization=polarization,light_source=source,first_surface=first,
                extrapolation_policy=extrapolation_policy)


def _nm(value, unit):
    return value * (1000.0 if unit == "um" else 1.0)


def _outside_ranges(requested, domain):
    low, high = requested
    dlow, dhigh = domain
    ranges = []
    if low < dlow:
        ranges.append([low, min(high, dlow)])
    if high > dhigh:
        ranges.append([max(low, dhigh), high])
    return ranges


def _merge_used_interval(old, current):
    if current is None:
        return old
    low, high = current
    if old is None:
        return [float(low), float(high)]
    return [min(old[0], float(low)), max(old[1], float(high))]


def _pretty_ranges(ranges):
    return ", ".join(f"{low:g}–{high:g} nm" for low, high in ranges)

def _k_default_zero_intervals(requested, source_range, enabled):
    """Requested intervals with exact inclusive/exclusive source boundaries."""
    low, high = requested
    def interval(a,b,left=True,right=True,reason='outside_source_range'):
        return {'min_nm':float(a),'max_nm':float(b),'min_inclusive':left,
                'max_inclusive':right,'reason':reason}
    if not enabled: return [interval(low,high,reason='disabled')]
    if source_range is None: return [interval(low,high,reason='missing')]
    start,end=source_range; result=[]
    if low < start: result.append(interval(low,min(high,start),right=high < start))
    if high > end: result.append(interval(max(low,end),high,left=low > end))
    return result


def _effective_input_range(requested, source_range):
    """Requested wavelengths after clipping each channel to its declared domain."""
    if source_range is None:
        return None
    low, high = source_range
    return [min(max(requested[0], low), high), min(max(requested[1], low), high)]


def source_spectrum(wavelength_nm, source):
    """Relative spectral density per wavelength; peak normalized, never absolute power."""
    waves = np.asarray(wavelength_nm,dtype=float)
    if source['kind']=='flat':
        return np.ones_like(waves)
    if source['kind']=='tabulated':
        source_waves = waves
        if source.get('wavelength_basis', 'vacuum') == 'standard_air':
            source_waves = vacuum_to_standard_air_nm(waves)
        values = np.interp(source_waves,source['wavelength_nm'],source['intensity'])
        if source.get('wavelength_basis', 'vacuum') == 'standard_air':
            values *= standard_air_density_jacobian(waves)
    else:
        # Planck B_lambda, log form avoids exp overflow at low temperatures.
        exponent = 14387768.775039337 / (waves * source['temperature_k'])
        log_denominator = np.where(exponent>50, exponent,
                                   np.log(np.expm1(np.minimum(exponent,50))))
        log_values = -5*np.log(waves)-log_denominator
        values = np.exp(log_values-np.max(log_values))
    maximum = float(np.max(values))
    if maximum <= 0:
        raise ValueError('所选波段光源谱为零')
    return values / maximum


def active_material_references(design):
    """Inactive single-sided drafts never participate in material lookup."""
    references = [design['substrate']] + design['front_layers']
    keys = ['incident_medium']
    if design['substrate']['sides'] == 'double':
        references += design['back_layers']
        keys.append('exit_medium')
    references += [design[key] for key in keys if design[key]['kind'] == 'material']
    return references


def automatic_substrate_handling(design, maximum_wavelength_nm):
    thickness = design['substrate']['thickness']
    d = thickness['value'] * {'nm':1.0, 'um':1000.0, 'mm':1e6}[thickness['unit']]
    threshold = 10.0 * float(maximum_wavelength_nm)
    single = design['substrate']['sides'] == 'single'
    resolved = 'semi_infinite' if single else ('incoherent' if d > threshold else 'coherent')
    return {'policy':'automatic', 'resolved_mode':resolved, 'thickness_nm':d,
            'incoherent_threshold_nm':threshold, 'threshold_comparison':'strictly greater',
            'exit_medium_ignored':single, 'average_fringes_approximation':resolved == 'incoherent'}


class FilmDesignService:
    def __init__(self, root=None, *, restrict_to_root=True, plugins=None):
        self.paths = CoatService(root,restrict_to_root=restrict_to_root,plugins=plugins)
        self.root = self.paths.root
        self.materials = FilmMaterialLibrary(self.root)
        from .tfcalc_import import register_tfcalc_plugin, PLUGIN_NAME
        self.plugins = self.paths.plugins
        if not any(spec.name == PLUGIN_NAME for spec in self.plugins.list('film_importer')):
            register_tfcalc_plugin(self.plugins)
        self._store = None
        self._expression_store = None

    def read_tfcalc(self, path):
        from .tfcalc_import import read_tfcalc_file
        return read_tfcalc_file(self.paths._path(path), self.plugins)

    def build_tfcalc_import(self, document, options, base_design):
        from .tfcalc_import import build_tfcalc_design, PLUGIN_NAME
        if not self.plugins.is_enabled('film_importer', PLUGIN_NAME):
            raise PermissionError('TFCalc 导入插件已禁用')
        return build_tfcalc_design(document, options, base_design,
                                  self.material_options(purpose='film'))

    @property
    def store(self):
        if self._store is None:
            database = self.paths._path('data/film_design/library.sqlite3')
            self._store = FilmDesignStore(database)
        return self._store

    @property
    def expression_store(self):
        if self._expression_store is None:
            database = self.paths._path('data/film_design/library.sqlite3')
            self._expression_store = FilmExpressionTemplateStore(database)
        return self._expression_store

    def list_expression_templates(self):
        try:
            return self.expression_store.list()
        except sqlite3.Error as exc:
            raise OSError(f'Expression-template SQLite read failed: {exc}') from exc

    def save_expression_template(self, template):
        try:
            return self.expression_store.save(template)
        except sqlite3.Error as exc:
            raise OSError(f'Expression-template SQLite save failed: {exc}') from exc

    def load_expression_template(self, template_id):
        try:
            return self.expression_store.load(template_id)
        except sqlite3.Error as exc:
            raise OSError(f'Expression-template SQLite read failed: {exc}') from exc

    def list_saved_designs(self):
        try:
            return self.store.list()
        except sqlite3.Error as exc:
            raise OSError(f'膜系 SQLite 库读取失败：{exc}') from exc

    def save_saved_design(self, design):
        try:
            return self.store.save(design)
        except sqlite3.Error as exc:
            raise OSError(f'膜系 SQLite 库保存失败：{exc}') from exc

    def load_saved_design(self, design_id):
        try:
            return self.store.load(design_id)
        except sqlite3.Error as exc:
            raise OSError(f'膜系 SQLite 库读取失败：{exc}') from exc

    def material_options(self,purpose='film'):
        return self.materials.options(purpose)

    def material_options_many(self,purposes):
        return self.materials.options_many(purposes)

    def new_design(self):
        return new_design()

    def validate(self,design):
        return validate_design(design)

    def load(self,path):
        return load_design(self.paths._path(path))

    def save(self,path,design,*,overwrite=False):
        if type(overwrite) is not bool:
            raise ValueError('overwrite 必须为显式布尔值')
        target = self.paths._path(path)
        validate_design_path("film", target)
        if target.exists() and not overwrite:
            raise ValueError('文件已存在；需明确 overwrite=True')
        normalized = self.validate(design)
        save_design(target,normalized,overwrite=overwrite)
        return normalized

    def expand(self,expression,symbols):
        return expand_expression(expression,symbols)

    def freeze_design(self,design,settings=None):
        """Capture optical records for reproducible calls; this creates no queue job."""
        design=self.validate(design)
        if settings is not None and not isinstance(settings,dict):
            raise ValueError('计算设置必须为对象')
        settings={} if settings is None else dict(settings)
        settings.setdefault('first_surface',design.get('first_surface','front'))
        settings=simulation_settings(settings)
        references = active_material_references(design)
        catalog={row['id']:row for row in self.materials._records()}
        selected={}
        for ref in references:
            selected.setdefault(ref['material_id'],set()).add(ref['optical_set_id'])
        records=[]
        for identifier,ids in selected.items():
            if identifier not in catalog: raise ValueError('找不到材料: '+identifier)
            material=catalog[identifier]
            optical_sets=[deepcopy(row) for row in material['optical_sets'] if row['id'] in ids]
            if {row['id'] for row in optical_sets}!=ids: raise ValueError('材料光学常数组缺失: '+identifier)
            records.append({'id':identifier,'name':material['name'],'optical_sets':optical_sets,
                            'design_ready':material.get('design_ready'),'notes':material.get('notes','')})
        payload={'schema_version':1,'kind':'coat_sim.frozen_film_design','design':design,'settings':settings,'materials':records}
        identity=json.dumps(payload,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')
        payload['sha256']=hashlib.sha256(identity).hexdigest()
        return payload

    def simulate(self,design,settings=None,*,cancel_event=None):
        design = self.validate(design)
        if settings is None:
            settings={'first_surface':design.get('first_surface','front')}
        elif isinstance(settings,dict):
            settings=dict(settings)
            settings.setdefault('first_surface',design.get('first_surface','front'))
        settings = simulation_settings(settings)
        _check_cancelled(cancel_event)
        waves = np.linspace(settings['wavelength_min_nm'],settings['wavelength_max_nm'],settings['samples'])
        if waves.size > 1 and np.any(np.diff(waves) <= 0):
            raise ValueError('采样间隔低于该波长范围的浮点可分辨精度')
        chunk_size = _simulation_chunk_size(
            len(design['front_layers']) + len(design['back_layers']) + 3)
        references = active_material_references(design)
        if isinstance(self.materials, FilmMaterialLibrary):
            identifiers = {row['material_id'] for row in references}
            catalog_snapshot = {
                row['id']: row for row in
                self.materials._records(material_ids=identifiers)
            }
        elif hasattr(self.materials, '_records'):
            catalog_snapshot = {
                row['id']: row for row in self.materials._records()
            }
        else:
            catalog_snapshot = {}
            for identifier in {row['material_id'] for row in references}:
                catalog_snapshot[identifier] = self.materials.get(identifier)
        def chunks():
            for start in range(0, waves.size, chunk_size):
                _check_cancelled(cancel_event)
                yield self._simulate_chunk(
                    design, settings, waves[start:start + chunk_size],
                    cancel_event=cancel_event, include_reference_queries=(start == 0),
                    catalog_snapshot=catalog_snapshot)
            _check_cancelled(cancel_event)
        return _merge_simulation_chunks(chunks(), waves, settings,
                                        cancel_event=cancel_event)

    def _simulate_chunk(self,design,settings,waves,*,cancel_event=None,
                        include_reference_queries=True,catalog_snapshot=None):
        _check_cancelled(cancel_event)
        use_k = design.get('use_k', True)
        extrapolation_policy = settings['extrapolation_policy']
        snapshots = {}
        prepared = {}
        series_cache = {}
        series_query_counts = {}
        warnings = list(settings['light_source'].get('source_warnings', []))
        references = active_material_references(design)
        catalog = catalog_snapshot

        def evaluate(material_id,set_id,wave,purpose='spectrum',record_reference=True):
            _check_cancelled(cancel_event)
            key = (material_id,set_id)
            if key not in snapshots:
                if catalog is not None:
                    if material_id not in catalog: raise ValueError('找不到材料: '+str(material_id))
                    material=catalog[material_id]
                else:
                    material=self.materials.get(material_id)
                record = next((r for r in material['optical_sets'] if r['id']==set_id),None)
                if record is None:
                    raise ValueError(f'{material_id}: 找不到光学常数组 {set_id}')
                snapshots[key] = {'material_id':material_id,'material_name':material['name'],
                                  'optical_set':deepcopy(record),
                                  'design_ready':material.get('design_ready'),
                                  '_evaluation_trace':{'n_interpolation_range_nm':None,
                                      'k_interpolation_range_nm':None,
                                      'n_interpolated_queries':0,'k_interpolated_queries':0,
                                       'k_evaluated_input_range_nm':None,
                                       'k_source_queries':0,'k_default_zero_queries':0,
                                      'n_extrapolated_sides':set(),'k_extrapolated_sides':set(),
                                      'reference_queries':[]}}
                if material.get('design_ready') is False:
                    warnings.append(f'{material["name"]}: 条件资料/方法演示，未标为生产设计数据')
                if 'index_reference=relative_to_air' in record.get('notes',''):
                    warnings.append(f'{material["name"]}: 采用来源目录相对空气n；未做空气/真空折射率换算')
            snapshot = snapshots[key]
            try:
                if key not in prepared:
                    prepared[key] = prepare_optical_set(snapshot['optical_set'],
                        k_sampling_policy='source_only_zero_defaults')
                n,k,details = prepared[key](float(wave),'nm',
                    extrapolation_policy=extrapolation_policy,include_k=use_k)
                trace=snapshot['_evaluation_trace']
                if purpose == 'reference':
                    if record_reference:
                        trace['reference_queries'].append({
                            'wavelength_nm':float(wave),
                            'n_evaluation_wavelength_nm':details['n_evaluation_wavelength_nm'],
                            'n_extrapolated':details['n_extrapolation_side'] is not None,
                            'k_evaluation_wavelength_nm':details['k_evaluation_wavelength_nm'],
                            'k_extrapolated':details['k_extrapolation_side'] is not None,
                            'k_default_reason':details['k_default_reason'],
                        })
                else:
                    trace['n_interpolation_range_nm'] = _merge_used_interval(
                        trace['n_interpolation_range_nm'], details['n_interpolation_segment_nm'])
                    trace['k_interpolation_range_nm'] = _merge_used_interval(
                        trace['k_interpolation_range_nm'], details['k_interpolation_segment_nm'])
                    trace['n_interpolated_queries'] += details['n_interpolation_segment_nm'] is not None
                    trace['k_interpolated_queries'] += details['k_interpolation_segment_nm'] is not None
                    if details['k_evaluation_wavelength_nm'] is not None:
                        value = details['k_evaluation_wavelength_nm']
                        trace['k_evaluated_input_range_nm'] = _merge_used_interval(
                            trace['k_evaluated_input_range_nm'], [value,value])
                        trace['k_source_queries'] += 1
                    if details['k_default_reason'] is not None:
                        trace['k_default_zero_queries'] += 1
                    if details['n_extrapolation_side']:
                        trace['n_extrapolated_sides'].add(details['n_extrapolation_side'])
                    if details['k_extrapolation_side']:
                        trace['k_extrapolated_sides'].add(details['k_extrapolation_side'])
                if k is None:
                    raise ValueError('仿真 k 采样器未返回有限计算值')
                if not use_k:
                    return complex(n,0.0)
                return complex(n,k)
            except ValueError as exc:
                raise ValueError(f'{material_id}/{set_id} @ {wave:.12g} nm: {exc}') from exc

        def index_series(reference):
            _check_cancelled(cancel_event)
            key = (reference['material_id'], reference['optical_set_id'])
            if key not in series_cache:
                previous = snapshots.get(key, {}).get('_evaluation_trace', {})
                counters = ('n_interpolated_queries','k_interpolated_queries',
                            'k_source_queries','k_default_zero_queries')
                before = tuple(previous.get(name,0) for name in counters)
                first = evaluate(*key, float(waves[0]))
                sampler = prepared[key]
                if (waves.size > 1 and hasattr(sampler, 'evaluate_many') and
                        snapshots[key]['optical_set']['kind'] == 'tabulated'):
                    try:
                        remainder, summary = sampler.evaluate_many(
                            waves[1:], 'nm', extrapolation_policy=extrapolation_policy,
                            include_k=use_k)
                    except ValueError:
                        # Re-run only the exceptional path through the scalar
                        # evaluator so diagnostics keep the exact failing wavelength.
                        for wave in waves[1:]:
                            evaluate(*key, float(wave))
                        raise
                    if remainder is None:
                        remainder = np.asarray([evaluate(*key, float(wave)) for wave in waves[1:]], complex)
                    else:
                        trace = snapshots[key]['_evaluation_trace']
                        trace['n_interpolation_range_nm'] = _merge_used_interval(
                            trace['n_interpolation_range_nm'], summary['n_interpolation_range_nm'])
                        trace['k_interpolation_range_nm'] = _merge_used_interval(
                            trace['k_interpolation_range_nm'], summary['k_interpolation_range_nm'])
                        trace['n_interpolated_queries'] += summary['n_interpolated_queries']
                        trace['k_interpolated_queries'] += summary['k_interpolated_queries']
                        trace['k_evaluated_input_range_nm'] = _merge_used_interval(
                            trace['k_evaluated_input_range_nm'], summary['k_evaluated_input_range_nm'])
                        trace['k_source_queries'] += summary['k_source_queries']
                        trace['k_default_zero_queries'] += summary['k_default_zero_queries']
                        trace['n_extrapolated_sides'].update(summary['n_extrapolated_sides'])
                        trace['k_extrapolated_sides'].update(summary['k_extrapolated_sides'])
                    series_cache[key] = np.empty(waves.size, dtype=complex)
                    series_cache[key][0] = first
                    series_cache[key][1:] = remainder
                else:
                    series_cache[key] = np.asarray(
                        [first] + [evaluate(*key, float(wave)) for wave in waves[1:]], complex)
                trace = snapshots[key]['_evaluation_trace']
                series_query_counts[key] = {name:trace[name]-value for name,value in zip(counters,before)}
            else:
                # Keep the report's logical query counts per reference, not cache misses.
                trace = snapshots[key]['_evaluation_trace']
                for name,delta in series_query_counts[key].items(): trace[name] += delta
            return series_cache[key]

        def medium(reference):
            if reference.get('kind','constant')=='constant':
                return np.full(len(waves),reference['n'],dtype=complex)
            return index_series(reference)

        def layers(rows):
            columns = [index_series(layer) for layer in rows]
            indices = np.column_stack(columns) if columns else np.empty((len(waves),0),complex)
            thickness = []
            for layer in rows:
                n_reference = None
                if layer['thickness']['unit']=='qw':
                    n_reference = evaluate(layer['material_id'],layer['optical_set_id'],
                                           design['reference_wavelength_nm'],purpose='reference',
                                           record_reference=include_reference_queries).real
                thickness.append(physical_thickness_nm(layer,design['reference_wavelength_nm'],n_reference))
            return indices,thickness

        substrate = design['substrate']
        handling = automatic_substrate_handling(design, settings['wavelength_max_nm'])
        ns = index_series(substrate)
        ni = medium(design['incident_medium'])
        ne = medium(design['exit_medium']) if substrate['sides'] == 'double' else None
        if np.any(np.abs(ni.imag)>1e-14) or (ne is not None and np.any(np.abs(ne.imag)>1e-14)):
            raise ValueError('首版外部入射/出射介质须透明；不支持吸收外部端口')
        front = design['front_layers']
        back = design['back_layers'] if substrate['sides']=='double' else []
        if settings['first_surface']=='back':
            if substrate['sides']=='single':
                raise ValueError('半无限基片不具有可计算的背面入射')
            front,back = back,front
            ni,ne = ne,ni
        fi,fd = layers(list(reversed(front)))
        bi,bd = layers(back)
        angle,pola = settings['angle_deg'],settings['polarization']
        phase_summary = None
        if handling['resolved_mode']=='semi_infinite':
            response = coherent_rta(waves,np.column_stack([ni,fi,ns]),fd,angle,pola)
            definition = 'T为进入半无限基片的通量；A仅为膜层吸收，不含基片后续体吸收'
        else:
            thick = handling['thickness_nm']
            if handling['resolved_mode']=='incoherent':
                response = incoherent_plate_rta(waves,ni,ns,ne,fi,fd,bi,bd,thick,angle,pola)
                definition = '厚基片平均条纹近似，包含基片体吸收及多次背面反射；A为基片与两面膜层总吸收'
                warnings.append('基片厚度大于 10×最大真空波长：采用平均条纹工程近似；不代表真实相干性判定，激光或高分辨平行片测量不适用')
            else:
                response = coherent_rta(waves,np.column_stack([ni,fi,ns,bi,ne]),fd+[thick]+bd,angle,pola)
                definition = '完整相干有限基片；A包含膜层及基片体吸收；条纹依赖相干假设'
                beta = ni.real * math.sin(math.radians(angle))
                q = np.sqrt(ns * ns - beta * beta + 0j)
                phase = 4 * math.pi * q.real * thick / waves
                phase_steps = np.abs(np.diff(phase))
                maximum_step = float(np.max(phase_steps)) if phase_steps.size else 0.0
                handling['phase_sampling'] = {'maximum_round_trip_phase_step_rad':maximum_step,
                    'nyquist_necessary_condition_met':maximum_step <= math.pi,'convergence_verified':False}
                phase_summary = {'first_rad':float(phase[0]),'last_rad':float(phase[-1]),
                                 'maximum_internal_step_rad':maximum_step}
                if maximum_step > math.pi:
                    warnings.append('相干基片相邻波长点的往返相位变化超过 π，当前采样不足以解析条纹；请减小步长并检查收敛，未用插值补齐')
        # Source spectra are normalized over the complete requested band. The
        # caller recomputes them after chunks are merged, so a zero-only chunk
        # cannot be mistaken for a zero-only full-band source.
        spectral = np.ones(waves.size, dtype=float)
        result = {'schema_version':1,'kind':'coat_sim.film_spectrum','design_id':design['id'],
                  'wavelength_nm':waves.tolist(),'wavelength_basis':'vacuum',
                  'source_wavelength_basis':settings['light_source'].get('wavelength_basis','vacuum'),
                  'settings':settings,'definition':definition,'substrate_handling':handling,
                  'rta_units':'fraction (0–1); plot display uses percent',
                  'source_spectrum':spectral.tolist(),'source_units':'relative spectral density per wavelength; peak normalized',
                  'warnings':warnings,'provenance':[],'design':deepcopy(design)}
        if not use_k:
            warnings.append('已关闭 k：仅此计算将所选介质的消光系数按 0 处理，原始材料数据与来源快照保持不变')
        for key in ('R','T','A'):
            array = np.asarray(response[key],dtype=float)
            if not np.all(np.isfinite(array)):
                raise ValueError('计算产生非有限光谱，结果未发布')
            result[key]=array.tolist()
        for key,channel in [('R','reflected_spectrum'),('T','transmitted_spectrum'),('A','absorbed_spectrum')]:
            result[channel]=(np.asarray(result[key])*spectral).tolist()
        denominator = _trapezoid(spectral, waves)
        result['weighted_rta'] = ({key: float(_trapezoid(spectral * np.asarray(result[key]), waves) / denominator)
                                   for key in ('R','T','A')} if denominator > 0 else None)

        requested_range=[float(settings['wavelength_min_nm']),float(settings['wavelength_max_nm'])]
        for item in snapshots.values():
            record=item['optical_set']
            trace=item.pop('_evaluation_trace')
            item_warnings=[]
            if item.get('design_ready') is False:
                item_warnings.append('条件资料/方法演示，未标为生产设计数据')
            if 'index_reference=relative_to_air' in record.get('notes',''):
                item_warnings.append('来源目录的 n 相对空气；未做空气/真空折射率换算')
            n_domain=[_nm(record['wavelength_min'],record['wavelength_unit']),
                      _nm(record['wavelength_max'],record['wavelength_unit'])]
            if record['kind']=='tabulated' and record.get('k') is not None:
                k_domain=n_domain
                k_known=True
                k_method='piecewise_linear'
            elif record.get('k_data') is not None:
                kdata=record['k_data']
                k_domain=[_nm(kdata['wavelength_min'],kdata['wavelength_unit']),
                          _nm(kdata['wavelength_max'],kdata['wavelength_unit'])]
                k_known=True
                k_method='single_point' if len(kdata['wavelengths'])==1 else 'piecewise_linear'
            else:
                k_domain=None
                k_known=False
                k_method='unknown'
            n_method=('piecewise_linear' if record['kind']=='tabulated' else
                      (f"RII formula {record['formula_number']}" if record['kind']=='rii_formula' else record['kind']))
            n_external=_outside_ranges(requested_range,n_domain)
            k_defaults=_k_default_zero_intervals(requested_range,k_domain,use_k)
            evaluation={
                'requested_spectral_range_nm':requested_range,
                'extrapolation_policy':extrapolation_policy,
                'k_sampling_policy':'source_only_zero_defaults',
                'n':{'method':n_method,'source_range_nm':n_domain,
                     'effective_input_range_nm':_effective_input_range(requested_range,n_domain),
                     'interpolation_method':'linear' if record['kind']=='tabulated' else None,
                     'extrapolation_method':'endpoint_clamp' if extrapolation_policy=='endpoint_hold' else 'error',
                     'interpolated_segments_nm':trace['n_interpolation_range_nm'],
                     'interpolated_query_count':trace['n_interpolated_queries'],
                     'extrapolated_ranges_nm':n_external},
                'k':{'known':k_known,'enabled':use_k,'method':k_method,
                     'source':((record.get('k_data') or record).get('source','') if k_known else None),
                     'notes':((record.get('k_data') or record).get('notes','') if k_known else None),
                     'source_range_nm':k_domain,
                     'effective_input_range_nm':trace['k_evaluated_input_range_nm'],
                     'interpolation_method':'linear' if k_method=='piecewise_linear' and use_k else None,
                     'extrapolation_method':'none',
                      'calculation_policy':'source_only_zero_defaults',
                      'default_value':0.0,'default_zero_intervals':k_defaults,
                      'default_zero_query_count':trace['k_default_zero_queries'],
                      'source_evaluated_query_count':trace['k_source_queries'],
                      'boundary_rule':'inclusive source endpoints; 4-ulp source-unit rounding snap',
                     'interpolated_segments_nm':trace['k_interpolation_range_nm'] if use_k else None,
                     'interpolated_query_count':trace['k_interpolated_queries'] if use_k else 0,
                     'extrapolated_ranges_nm':[]},
                'thickness_reference_queries':trace['reference_queries'],
            }
            item['evaluation']=evaluation
            item['sha256']=hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
            if not use_k:
                item_warnings.append('本次计算显式关闭 k；原始材料数据保留不变')
            if n_external:
                warning=(f'{item["material_name"]}/{record["name"]}: n 数据有效波段 {n_domain[0]:g}–{n_domain[1]:g} nm；'
                         f'超出范围 {_pretty_ranges(n_external)}，按最近端点值外推')
                warnings.append(warning); item_warnings.append(warning)
            item['warnings']=item_warnings
            result['provenance'].append(item)
        result['_phase_summary'] = phase_summary
        return result

    def export_csv(self,path,result,*,overwrite=False):
        if type(overwrite) is not bool:
            raise ValueError('overwrite 必须为显式布尔值')
        target = self.paths._path(path)
        if target.suffix.lower()!='.csv':
            raise ValueError('光谱导出须为 .csv')
        if target.exists() and not overwrite:
            raise ValueError('文件已存在；需明确 overwrite=True')
        data_columns=['wavelength_nm','R','T','A','source_spectrum','reflected_spectrum','transmitted_spectrum','absorbed_spectrum']
        columns=data_columns+['evaluation_report_json','warnings_json']
        arrays=[result[key] for key in data_columns]
        evaluation_report=json.dumps({
            'settings':result.get('settings',{}),
            'units':{'wavelength_nm':'nm','R':'fraction (0–1)','T':'fraction (0–1)','A':'fraction (0–1)',
                     'spectra':result.get('source_units')},
            'substrate_handling':result.get('substrate_handling',{}),
            'materials':[{'material_id':item.get('material_id'),'material_name':item.get('material_name'),
                          'optical_set_id':item.get('optical_set',{}).get('id'),
                          'optical_set_name':item.get('optical_set',{}).get('name'),
                          'source':item.get('optical_set',{}).get('source'),
                          'notes':item.get('optical_set',{}).get('notes'),
                          'warnings':item.get('warnings',[]),
                          'evaluation':item.get('evaluation')} for item in result.get('provenance',[])],
        },ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':'))
        warnings_json=json.dumps(result.get('warnings',[]),ensure_ascii=False,allow_nan=False,separators=(',',':'))
        if len({len(a) for a in arrays})!=1:
            raise ValueError('光谱列长度不一致')
        target.parent.mkdir(parents=True,exist_ok=True)
        descriptor,temporary=tempfile.mkstemp(prefix='.film-spectrum-',suffix='.csv',dir=target.parent)
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.writer(stream)
                writer.writerow(columns)
                for index,row in enumerate(zip(*arrays)):
                    writer.writerow((*row,evaluation_report if index==0 else '',warnings_json if index==0 else ''))
                stream.flush(); os.fsync(stream.fileno())
            if overwrite:
                replace_with_retry(temporary,target)
            else:
                # Same-filesystem hard link publishes the complete file without
                # a check/create race or replacing a concurrently-created file.
                os.link(temporary,target)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return str(target)


def _merge_simulation_chunks(chunks, waves, settings, *, cancel_event=None):
    iterator = iter(chunks)
    try:
        result = next(iterator)
    except StopIteration as exc:
        raise RuntimeError('film simulation produced no wavelength chunks') from exc
    array_keys = ('R', 'T', 'A', 'source_spectrum', 'reflected_spectrum',
                  'transmitted_spectrum', 'absorbed_spectrum')
    provenance_by_key = {
        (item.get('material_id'), item.get('optical_set', {}).get('id')): item
        for item in result.get('provenance', ())
    }
    phase_summary = result.pop('_phase_summary', None)
    maximum_phase_step = (phase_summary or {}).get('maximum_internal_step_rad', 0.0)
    previous_phase_end = (phase_summary or {}).get('last_rad')
    warning_values = list(result.get('warnings', ()))
    _check_cancelled(cancel_event)
    for part in iterator:
        _check_cancelled(cancel_event)
        for key in array_keys:
            result[key].extend(part[key])
        current_phase = part.pop('_phase_summary', None)
        if current_phase is not None:
            maximum_phase_step = max(
                maximum_phase_step,
                current_phase['maximum_internal_step_rad'])
            if previous_phase_end is not None:
                maximum_phase_step = max(
                    maximum_phase_step,
                    abs(current_phase['first_rad'] - previous_phase_end))
            previous_phase_end = current_phase['last_rad']
        warning_values.extend(part.get('warnings', ()))
        for item in part.get('provenance', ()):
            _check_cancelled(cancel_event)
            key = (item.get('material_id'), item.get('optical_set', {}).get('id'))
            target = provenance_by_key.get(key)
            if target is None:
                target = deepcopy(item)
                result['provenance'].append(target)
                provenance_by_key[key] = target
                continue
            target_eval = target.get('evaluation', {})
            source_eval = item.get('evaluation', {})
            target_n, source_n = target_eval.get('n', {}), source_eval.get('n', {})
            target_n['interpolated_segments_nm'] = _merge_used_interval(
                target_n.get('interpolated_segments_nm'),
                source_n.get('interpolated_segments_nm'))
            target_n['interpolated_query_count'] = (
                target_n.get('interpolated_query_count', 0) +
                source_n.get('interpolated_query_count', 0))
            target_k, source_k = target_eval.get('k', {}), source_eval.get('k', {})
            for name in ('effective_input_range_nm', 'interpolated_segments_nm'):
                target_k[name] = _merge_used_interval(
                    target_k.get(name), source_k.get(name))
            for name in ('default_zero_query_count', 'source_evaluated_query_count',
                         'interpolated_query_count'):
                target_k[name] = target_k.get(name, 0) + source_k.get(name, 0)
            target['warnings'] = list(dict.fromkeys(
                list(target.get('warnings', ())) + list(item.get('warnings', ()))))
    _check_cancelled(cancel_event)
    result['wavelength_nm'] = waves.tolist()
    waves_spectrum = source_spectrum(waves, settings['light_source'])
    _check_cancelled(cancel_event)
    result['source_spectrum'] = waves_spectrum.tolist()
    for key, channel in (('R', 'reflected_spectrum'),
                         ('T', 'transmitted_spectrum'),
                         ('A', 'absorbed_spectrum')):
        _check_cancelled(cancel_event)
        result[channel] = (np.asarray(result[key], dtype=float) * waves_spectrum).tolist()
    _check_cancelled(cancel_event)
    denominator = _trapezoid(waves_spectrum, waves)
    result['weighted_rta'] = (
        {key: float(_trapezoid(waves_spectrum * np.asarray(result[key], dtype=float), waves) /
                    denominator) for key in ('R', 'T', 'A')}
        if denominator > 0 else None)
    result['warnings'] = list(dict.fromkeys(
        warning for warning in warning_values
        if '\u5f80\u8fd4\u76f8\u4f4d\u53d8\u5316\u8d85\u8fc7' not in warning))
    if 'phase_sampling' in result.get('substrate_handling', {}):
        phase_sampling = result['substrate_handling']['phase_sampling']
        phase_sampling['maximum_round_trip_phase_step_rad'] = float(maximum_phase_step)
        phase_sampling['nyquist_necessary_condition_met'] = maximum_phase_step <= math.pi
        if maximum_phase_step > math.pi:
            result['warnings'].append(
                '\u76f8\u5e72\u57fa\u7247\u76f8\u90bb\u6ce2\u957f\u70b9\u7684\u5f80\u8fd4\u76f8\u4f4d\u53d8\u5316\u8d85\u8fc7 \u03c0\uff0c\u5f53\u524d\u91c7\u6837\u4e0d\u8db3\u4ee5\u89e3\u6790\u6761\u7eb9\uff1b\u8bf7\u51cf\u5c0f\u6b65\u957f\u5e76\u68c0\u67e5\u6536\u655b\uff0c\u672a\u7528\u63d2\u503c\u8865\u9f50')
    _check_cancelled(cancel_event)
    return result


def simulate_frozen(root,payload,*,cancel_event=None):
    """Pure worker entry for immutable input data; no scheduler submission."""
    if not isinstance(payload,dict) or payload.get('schema_version')!=1 or payload.get('kind')!='coat_sim.frozen_film_design':
        raise ValueError('冻结膜系快照版本/类型无效')
    original={key:value for key,value in payload.items() if key!='sha256'}
    identity=hashlib.sha256(json.dumps(original,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
    if identity!=payload.get('sha256'): raise ValueError('冻结膜系快照摘要不匹配')
    rows=payload.get('materials')
    if not isinstance(rows,list) or any(not isinstance(row,dict) or not isinstance(row.get('id'),str) for row in rows):
        raise ValueError('冻结材料列表无效')
    records={row['id']:deepcopy(row) for row in rows}
    if len(records)!=len(rows): raise ValueError('冻结材料ID重复')
    class FrozenLibrary:
        def get(self,identifier):
            if identifier not in records: raise ValueError('冻结材料快照缺失: '+str(identifier))
            return deepcopy(records[identifier])
    service=FilmDesignService(root)
    service.materials=FrozenLibrary()
    result=service.simulate(payload['design'],payload['settings'],cancel_event=cancel_event)
    result['input_snapshot_sha256']=identity
    return result
