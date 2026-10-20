"""Pure, inspectable physical thin-film starting structures, never solved designs.

All sections, layer exports and candidate designs run from substrate to incident
medium. Thicknesses are physical nm; real source n/k stay in the frozen library.
See docs/film-physical-structures-contract.md and docs/algorithms.html.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import MISSING, asdict, dataclass, fields
import hashlib
import json
import math
from typing import Any, Mapping

from .film_design import MAX_LAYERS, validate_design
from .film_optimization import FilmOptimizationObjective
from .optical_constants import prepare_optical_set
from .film_synthesis_absorption import annotate_absorption_assumption, design_absorption_assumption


@dataclass(frozen=True)
class FilmStructureMaterialBinding:
    role: str
    material_id: str
    optical_set_id: str


@dataclass(frozen=True)
class FilmOpticalLayerSpec:
    role: str
    wavelength_nm: float
    optical_thickness_waves: float = .25
    thickness_nm: float | None = None


@dataclass(frozen=True)
class FilmBraggStackSection:
    wavelength_nm: float
    pairs: int = 8
    high_role: str = 'H'
    low_role: str = 'L'
    first_role: str = 'L'
    high_low_optical_ratio: float = 1.
    thickness_scale: float = 1.
    terminal_role: str | None = None
    terminal_optical_thickness_waves: float = .25


@dataclass(frozen=True)
class FilmChirpedSection:
    start_wavelength_nm: float
    end_wavelength_nm: float
    pairs: int = 16
    high_role: str = 'H'
    low_role: str = 'L'
    first_role: str = 'L'
    ratio_start: float = 1.
    ratio_end: float = 1.
    spacing: str = 'linear'
    thickness_scale: float = 1.


@dataclass(frozen=True)
class FilmDigitalRugateSection:
    wavelength_nm: float
    period_nm: float
    periods: int = 8
    samples_per_period: int = 16
    mean_n: float | None = None
    modulation_n: float | None = None
    phase_rad: float = 0.
    apodization: str = 'hann'
    high_role: str = 'H'
    low_role: str = 'L'


@dataclass(frozen=True)
class FilmCoupledCavitySection:
    substrate_mirror: FilmBraggStackSection
    cavities: tuple[FilmOpticalLayerSpec, ...]
    coupling_mirrors: tuple[FilmBraggStackSection, ...]
    incident_mirror: FilmBraggStackSection


FilmStructureSection = (FilmOpticalLayerSpec | FilmBraggStackSection |
                        FilmChirpedSection | FilmDigitalRugateSection |
                        FilmCoupledCavitySection)


@dataclass(frozen=True)
class FilmStructureSpec:
    mechanism: str
    bindings: tuple[FilmStructureMaterialBinding, ...]
    sections: tuple[FilmStructureSection, ...]
    name: str = 'physical structure'
    validation_wavelengths_nm: tuple[float, ...] = ()


@dataclass(frozen=True)
class FilmPhysicalStructureResult:
    candidate_design: Mapping[str, Any]
    metadata: Mapping[str, Any]


_TYPES = {'layer': FilmOpticalLayerSpec, 'bragg': FilmBraggStackSection,
          'chirped': FilmChirpedSection, 'digital_rugate': FilmDigitalRugateSection,
          'coupled_cavity': FilmCoupledCavitySection}
_MECHANISMS = {'multistack', 'coupled_cavity', 'chirped', 'digital_rugate', 'composite'}
_REFERENCES = {
    'multistack': ('https://doi.org/10.1364/AO.32.002969',),
    'coupled_cavity': ('https://doi.org/10.1364/OE.9.000652',),
    'chirped': ('https://doi.org/10.1364/OL.22.000831',),
    'digital_rugate': ('https://doi.org/10.1364/AO.24.000457',
                       'https://doi.org/10.1364/AO.28.005091'),
}
RESEARCH_REF = '01980d6e6a9b57d5de2c5aa989394d0b368a67c6'
RESEARCH_URL = ('https://github.com/ii33/coating-research-library/blob/' + RESEARCH_REF +
                '/manual_studies/2026-10-19_multispectral_ir_structures/research.md')


def physical_structure_catalog() -> list[dict[str, Any]]:
    """Describe mechanisms and parameters; this is not a performance catalog."""
    parameters = {key: [{'name': field.name, 'required': field.default is MISSING,
                         **({} if field.default is MISSING else {'default': field.default})}
                        for field in fields(cls)] for key,cls in _TYPES.items()}
    return [{'mechanism': key, 'section_types': list(_TYPES),
             'section_parameters': deepcopy(parameters),
             'layer_order': 'substrate_outward', 'references': list(_REFERENCES.get(key, ())),
             'research_url': RESEARCH_URL, 'status': 'mechanism_seed_not_paper_reproduction',
             'supported_acceptance_metrics': ['R', 'T'],
             'unsupported_acceptance_metrics': ['phase', 'GD', 'GDD']}
            for key in sorted(_MECHANISMS)]


def _object(raw, cls):
    if not isinstance(raw, Mapping) or set(raw) - {f.name for f in fields(cls)}:
        raise ValueError(f'{cls.__name__} requires an object with known fields')
    try:
        return cls(**raw)
    except TypeError as exc:
        raise ValueError(f'invalid {cls.__name__}: {exc}') from exc


def _section_from_mapping(raw):
    if not isinstance(raw, Mapping) or not isinstance(raw.get('type'), str) or raw['type'] not in _TYPES:
        raise ValueError('section requires a known type')
    data = dict(raw)
    cls = _TYPES[data.pop('type')]
    if cls is FilmCoupledCavitySection:
        for name in ('substrate_mirror', 'incident_mirror'):
            if name not in data:
                raise ValueError(f'coupled cavity requires {name}')
            data[name] = _section_from_mapping(data[name])
        for name in ('cavities', 'coupling_mirrors'):
            if not isinstance(data.get(name), (list, tuple)):
                raise ValueError(f'coupled cavity requires {name} array')
            data[name] = tuple(_section_from_mapping(v) for v in data[name])
    return _object(data, cls)


def structure_spec_from_mapping(raw: Mapping[str, Any]) -> FilmStructureSpec:
    """Parse JSON-compatible public parameters, rejecting silently ignored keys."""
    if not isinstance(raw, Mapping):
        raise ValueError('structure spec must be an object')
    data = dict(raw)
    for name in ('bindings', 'sections'):
        if not isinstance(data.get(name), (tuple, list)):
            raise ValueError(f'{name} must be an array')
    data['bindings'] = tuple(_object(v, FilmStructureMaterialBinding) for v in data['bindings'])
    data['sections'] = tuple(_section_from_mapping(v) for v in data['sections'])
    if 'validation_wavelengths_nm' in data:
        if not isinstance(data['validation_wavelengths_nm'], (tuple, list)):
            raise ValueError('validation_wavelengths_nm must be an array')
        data['validation_wavelengths_nm'] = tuple(data['validation_wavelengths_nm'])
    return _object(data, FilmStructureSpec)


def _section_to_mapping(section):
    kind = next((key for key, cls in _TYPES.items() if type(section) is cls), None)
    if kind is None:
        raise ValueError('unknown physical structure section')
    data = asdict(section)
    if isinstance(section, FilmCoupledCavitySection):
        data['substrate_mirror'] = _section_to_mapping(section.substrate_mirror)
        data['incident_mirror'] = _section_to_mapping(section.incident_mirror)
        data['cavities'] = [_section_to_mapping(v) for v in section.cavities]
        data['coupling_mirrors'] = [_section_to_mapping(v) for v in section.coupling_mirrors]
    return {'type': kind, **data}


def structure_spec_to_mapping(spec: FilmStructureSpec) -> dict[str, Any]:
    if not isinstance(spec, FilmStructureSpec):
        raise ValueError('spec must be FilmStructureSpec')
    return {'mechanism': spec.mechanism, 'name': spec.name,
            'bindings': [asdict(v) for v in spec.bindings],
            'sections': [_section_to_mapping(v) for v in spec.sections],
            'validation_wavelengths_nm': list(spec.validation_wavelengths_nm)}


def _number(value, label, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label} must be a finite number')
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f'{label} must be finite' + (' and positive' if positive else ''))
    return result


def _count(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{label} must be an integer >= {minimum}')
    return value


class _Builder:
    def __init__(self, spec, snapshot, *, assume_unknown_k_zero=False):
        self.assume_unknown_k_zero = assume_unknown_k_zero
        if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get('materials'), list):
            raise ValueError('frozen_material_snapshot requires a materials array')
        self.bindings: dict[str, FilmStructureMaterialBinding] = {}
        self.evaluators = {}
        self.sources = {}
        self.materials: dict[str, Mapping[str, Any]] = {}
        for row in deepcopy(snapshot['materials']):
            if not isinstance(row, Mapping) or not isinstance(row.get('id'), str):
                raise ValueError('invalid frozen material record')
            if row['id'] in self.materials:
                raise ValueError('duplicate frozen material id')
            self.materials[row['id']] = row
        for binding in spec.bindings:
            if not isinstance(binding, FilmStructureMaterialBinding):
                raise ValueError('bindings require FilmStructureMaterialBinding')
            for key in ('role', 'material_id', 'optical_set_id'):
                value = getattr(binding, key)
                if not isinstance(value, str) or not value or len(value) > 128:
                    raise ValueError(f'invalid binding {key}')
            if binding.role in self.bindings:
                raise ValueError('duplicate material role')
            self.bindings[binding.role] = binding
            self._evaluator(binding.material_id, binding.optical_set_id)
        self.rows = []
        self.sections = []
        self.waves = set()
        self.material_samples = {}

    def _evaluator(self, material_id, set_id):
        key = (material_id, set_id)
        if key not in self.evaluators:
            material = self.materials.get(material_id)
            if material is None or material.get('design_ready') is False:
                raise ValueError(f'missing or unready frozen material {material_id}')
            optical_sets = material.get('optical_sets', [])
            if not isinstance(optical_sets, (tuple, list)) or any(not isinstance(v, Mapping) for v in optical_sets):
                raise ValueError('frozen optical_sets must contain objects')
            sets = [v for v in optical_sets if v.get('id') == set_id]
            if len(sets) != 1:
                raise ValueError(f'frozen optical set must be unique: {material_id}/{set_id}')
            self.evaluators[key] = prepare_optical_set(sets[0], k_sampling_policy='source')
            self.sources[key] = {name: deepcopy(sets[0][name]) for name in
                                ('source', 'doi', 'source_page', 'data_license', 'notes', 'k_data')
                                if name in sets[0]}
        return self.evaluators[key]

    def sample(self, material_id, set_id, wave):
        n, k, details = self._evaluator(material_id, set_id)(
            wave, 'nm', extrapolation_policy='error', include_k=True)
        assumed = not details.get('k_known')
        if assumed and self.assume_unknown_k_zero:
            k = 0.0
        elif assumed or k is None:
            raise ValueError(f'unknown k at {wave:g} nm: {material_id}/{set_id}')
        n = _number(n, 'source n')
        k = _number(k, 'source k', positive=False)
        if k < 0:
            raise ValueError('source k must be passive (nonnegative)')
        self.material_samples[(material_id, set_id, wave)] = {
            'material_id': material_id, 'optical_set_id': set_id,
            'wavelength_nm': wave, 'n': n, 'k': k,
            'material_name': self.materials[material_id].get('name', material_id),
            'n_source_range_nm': deepcopy(details.get('n_source_range_nm')),
            'k_source_range_nm': deepcopy(details.get('k_source_range_nm')),
            'source_metadata': deepcopy(self.sources[(material_id, set_id)])}
        if assumed:
            self.material_samples[(material_id, set_id, wave)].update(
                k_source_known=False, k_assumed_zero=True)
        return n, k

    def role(self, role, wave):
        if not isinstance(role, str) or role not in self.bindings:
            raise ValueError(f'unbound material role {role}')
        binding = self.bindings[role]
        self.waves.add(wave)
        return self.sample(binding.material_id, binding.optical_set_id, wave)

    def append(self, role, wave, thickness, path, **details):
        _number(thickness, 'physical layer thickness')
        n, k = self.role(role, wave)
        if len(self.rows) >= MAX_LAYERS:
            raise ValueError(f'physical structure exceeds design schema limit {MAX_LAYERS}')
        binding = self.bindings[role]
        self.rows.append({'sequence': len(self.rows) + 1, 'role': role,
            'material_id': binding.material_id, 'optical_set_id': binding.optical_set_id,
            'physical_thickness_nm': thickness, 'wavelength_nm': wave,
            'n_at_wavelength': n, 'k_at_wavelength': k, 'section': path, **details})

    def layer(self, section, path):
        wave = _number(section.wavelength_nm, 'wavelength_nm')
        waves = _number(section.optical_thickness_waves, 'optical_thickness_waves')
        n, _ = self.role(section.role, wave)
        thickness = waves * wave / n if section.thickness_nm is None else _number(section.thickness_nm, 'thickness_nm')
        self.append(section.role, wave, thickness, path,
                    optical_thickness_waves=n * thickness / wave)

    def pair(self, section, wave, ratio, path, pair_index, scale):
        n_h, _ = self.role(section.high_role, wave)
        n_l, _ = self.role(section.low_role, wave)
        if section.high_role == section.low_role or n_h <= n_l:
            raise ValueError(f'Bragg/rugate requires nH > nL at {wave:g} nm')
        if section.first_role not in (section.high_role, section.low_role):
            raise ValueError('first_role must be high_role or low_role')
        high_fraction = ratio / (1. + ratio)
        values = {section.high_role: wave * scale * high_fraction / (2 * n_h),
                  section.low_role: wave * scale * (1. - high_fraction) / (2 * n_l)}
        second = section.low_role if section.first_role == section.high_role else section.high_role
        for role in (section.first_role, second):
            self.append(role, wave, values[role], path, pair_index=pair_index,
                        local_bragg_wavelength_nm=wave, high_low_optical_ratio=ratio,
                        thickness_scale=scale)

    def section(self, section, path):
        first = len(self.rows)
        if isinstance(section, FilmOpticalLayerSpec):
            self.layer(section, path)
        elif isinstance(section, FilmBraggStackSection):
            wave = _number(section.wavelength_nm, 'wavelength_nm')
            ratio = _number(section.high_low_optical_ratio, 'high_low_optical_ratio')
            scale = _number(section.thickness_scale, 'thickness_scale')
            for i in range(_count(section.pairs, 'pairs')):
                self.pair(section, wave, ratio, path, i, scale)
            if section.terminal_role is not None:
                if section.terminal_role not in (section.high_role, section.low_role):
                    raise ValueError('terminal_role must be high_role or low_role')
                self.layer(FilmOpticalLayerSpec(section.terminal_role, wave,
                    section.terminal_optical_thickness_waves), path+'.terminal')
        elif isinstance(section, FilmChirpedSection):
            start = _number(section.start_wavelength_nm, 'start_wavelength_nm')
            end = _number(section.end_wavelength_nm, 'end_wavelength_nm')
            pairs = _count(section.pairs, 'chirped pairs', 2)
            r0 = _number(section.ratio_start, 'ratio_start')
            r1 = _number(section.ratio_end, 'ratio_end')
            scale = _number(section.thickness_scale, 'thickness_scale')
            if section.spacing not in ('linear', 'log'):
                raise ValueError('chirped spacing must be linear or log')
            for i in range(pairs):
                t = i / (pairs - 1)
                wave = start + (end - start) * t if section.spacing == 'linear' else math.exp(math.log(start) * (1-t) + math.log(end) * t)
                self.pair(section, wave, r0 + (r1-r0)*t, path, i, scale)
        elif isinstance(section, FilmDigitalRugateSection):
            self.rugate(section, path)
        elif isinstance(section, FilmCoupledCavitySection):
            if not isinstance(section.cavities, (tuple, list)) or not section.cavities:
                raise ValueError('coupled cavity needs at least one cavity')
            if not isinstance(section.coupling_mirrors, (tuple, list)) or len(section.coupling_mirrors) != len(section.cavities)-1:
                raise ValueError('N cavities require N-1 coupling mirrors')
            mirrors = (section.substrate_mirror, *section.coupling_mirrors, section.incident_mirror)
            if any(not isinstance(v, FilmBraggStackSection) for v in mirrors) or any(not isinstance(v, FilmOpticalLayerSpec) for v in section.cavities):
                raise ValueError('cavity mirrors must be Bragg sections and cavities explicit layers')
            self.section(section.substrate_mirror, path+'.substrate_mirror')
            for i, cavity in enumerate(section.cavities):
                self.section(cavity, path+f'.cavities[{i}]')
                if i < len(section.coupling_mirrors):
                    self.section(section.coupling_mirrors[i], path+f'.coupling_mirrors[{i}]')
            self.section(section.incident_mirror, path+'.incident_mirror')
        else:
            raise ValueError('unknown physical structure section')
        self.sections.append({'path': path, 'first_sequence': first+1,
                              'last_sequence': len(self.rows), 'parameters': _section_to_mapping(section)})

    def rugate(self, section, path):
        wave = _number(section.wavelength_nm, 'wavelength_nm')
        period = _number(section.period_nm, 'period_nm')
        periods = _count(section.periods, 'periods')
        samples = _count(section.samples_per_period, 'samples_per_period', 2)
        n_h, _ = self.role(section.high_role, wave)
        n_l, _ = self.role(section.low_role, wave)
        if section.high_role == section.low_role or n_h <= n_l:
            raise ValueError('digital rugate requires nH > nL')
        mean = .5*(n_h+n_l) if section.mean_n is None else _number(section.mean_n, 'mean_n')
        amplitude = .45*(n_h-n_l) if section.modulation_n is None else _number(section.modulation_n, 'modulation_n', positive=False)
        phase = _number(section.phase_rad, 'phase_rad', positive=False)
        if amplitude < 0 or mean-amplitude < n_l or mean+amplitude > n_h:
            raise ValueError('rugate profile must stay within the selected real index range')
        if section.apodization not in ('hann', 'none'):
            raise ValueError('rugate apodization must be hann or none')
        count = periods*samples
        dz = period/samples
        for i in range(count):
            t = (i+.5)/count
            envelope = math.sin(math.pi*t)**2 if section.apodization == 'hann' else 1.
            target = mean + amplitude*envelope*math.sin(2*math.pi*(i+.5)/samples+phase)
            fraction = (target*target-n_l*n_l)/(n_h*n_h-n_l*n_l)
            # Symmetric digital cell: real H(f dz/2), L((1-f)dz), H(f dz/2).
            for role, thickness in ((section.high_role, fraction*dz/2),
                                    (section.low_role, (1-fraction)*dz),
                                    (section.high_role, fraction*dz/2)):
                if thickness > 0:
                    self.append(role, wave, thickness, path, cell_index=i,
                                cell_target_n=target, high_fill_fraction=fraction,
                                cell_thickness_nm=dz)


def generate_physical_structure(environment: Mapping[str, Any], spec: FilmStructureSpec,
                                frozen_material_snapshot: Mapping[str, Any], *, assume_unknown_k_zero: bool = False) -> FilmPhysicalStructureResult:
    """Expand a mechanism into a validated, copied candidate and complete layer table.

    No search, live material lookup, write-back, phase acceptance or extrapolation
    is performed. Unknown-k substitution requires the explicit keyword and the
    matching saved environment flag; strict is the default. These are seeds, not claims
    of literature reproduction or manufacturability.
    """
    if not isinstance(spec, FilmStructureSpec) or not isinstance(spec.mechanism, str) or spec.mechanism not in _MECHANISMS:
        raise ValueError('known FilmStructureSpec mechanism required')
    if not isinstance(spec.sections, (tuple, list)) or not spec.sections:
        raise ValueError('physical structure requires explicit sections')
    if not isinstance(spec.bindings, (tuple, list)) or not spec.bindings:
        raise ValueError('physical structure requires explicit material bindings')
    if not isinstance(spec.name, str) or not spec.name or len(spec.name) > 120:
        raise ValueError('structure name must contain 1 to 120 characters')
    required = {'multistack': FilmBraggStackSection, 'coupled_cavity': FilmCoupledCavitySection,
                'chirped': FilmChirpedSection, 'digital_rugate': FilmDigitalRugateSection}
    if spec.mechanism in required and not any(isinstance(s, required[spec.mechanism]) for s in spec.sections):
        raise ValueError('mechanism requires a corresponding explicit section')
    design = validate_design(deepcopy(environment))
    if design['front_layers'] or design['back_layers']:
        raise ValueError('physical construction requires a bare environment')
    if design['substrate']['sides'] != 'single' or design['first_surface'] != 'front':
        raise ValueError('physical construction supports single-sided front incidence')
    if type(assume_unknown_k_zero) is not bool:
        raise ValueError('assume_unknown_k_zero must be an explicit boolean')
    if not design['use_k']:
        raise ValueError('physical construction requires use_k=True')
    if design['assume_lossless'] != assume_unknown_k_zero:
        raise ValueError('physical construction assume_lossless must match explicit assume_unknown_k_zero policy')
    builder = _Builder(spec, frozen_material_snapshot,
                       assume_unknown_k_zero=assume_unknown_k_zero)
    for i, section in enumerate(spec.sections):
        builder.section(section, f'sections[{i}]')
    if not isinstance(spec.validation_wavelengths_nm, (tuple, list)):
        raise ValueError('validation_wavelengths_nm must be a sequence')
    builder.waves.update(_number(v, 'validation wavelength') for v in spec.validation_wavelengths_nm)
    # A later evaluator must still check its full objective grid; no continuous
    # band coverage is inferred from these construction/validation samples.
    used = {(r['material_id'], r['optical_set_id']) for r in builder.rows}
    substrate = design['substrate']
    used.add((substrate['material_id'], substrate['optical_set_id']))
    incident = design['incident_medium']
    if incident['kind'] == 'material':
        used.add((incident['material_id'], incident['optical_set_id']))
    for wave in sorted(builder.waves):
        for material, optical in sorted(used):
            _, k = builder.sample(material, optical, wave)
            if incident['kind'] == 'material' and (material, optical) == (incident['material_id'], incident['optical_set_id']) and k != 0:
                raise ValueError('incident medium must have k=0')
    parameters = structure_spec_to_mapping(spec)
    material_bytes = json.dumps(frozen_material_snapshot['materials'], sort_keys=True,
                               ensure_ascii=False, allow_nan=False).encode('utf-8')
    material_digest = hashlib.sha256(material_bytes).hexdigest()
    identity = hashlib.sha256(json.dumps({'spec': parameters, 'environment': design,
        'materials_sha256': material_digest}, sort_keys=True, ensure_ascii=False,
        allow_nan=False).encode('utf-8')).hexdigest()
    design['id'] = 'physical-'+identity[:32]
    design['name'] = spec.name
    design['front_layers'] = [{'id': f'physical-{identity[:16]}-{r["sequence"]}',
        'material_id': r['material_id'], 'optical_set_id': r['optical_set_id'],
        'thickness': {'value': r['physical_thickness_nm'], 'unit': 'nm'},
        'note': f'{spec.mechanism}; {r["section"]}; role={r["role"]}'} for r in builder.rows]
    design['expression'] = {'text': '', 'symbols': {}, 'in_sync': False}
    design['note'] = f'Physical construction seed: {spec.mechanism}; no performance or reproduction claim.'
    metadata = {'schema_version': 1, 'kind': 'coat_sim.physical_structure',
        'mechanism': spec.mechanism, 'parameters': parameters, 'layer_order': 'substrate_outward',
        'layers': builder.rows, 'sections': builder.sections,
        'layer_count': len(builder.rows),
        'total_thickness_nm': math.fsum(r['physical_thickness_nm'] for r in builder.rows),
        'material_samples': [builder.material_samples[k] for k in sorted(builder.material_samples)],
        'checked_wavelengths_nm': sorted(builder.waves), 'materials_sha256': material_digest,
        'references': sorted({url for urls in _REFERENCES.values() for url in urls}),
        'research_url': RESEARCH_URL, 'research_ref': RESEARCH_REF,
        'status': 'mechanism_seed_not_paper_reproduction',
        'supported_acceptance_metrics': ['R', 'T'],
        'unsupported_acceptance_metrics': ['phase', 'GD', 'GDD'],
        'approximations': [
            'Normal-incidence optical thickness construction; evaluate full angular R/T separately.',
            'Rugate fill uses real epsilon interpolation at the reference wavelength only; all exported films retain real source n/k.',
            'Digital cells approximate a scalar profile only in the deeply subwavelength, normal-incidence, weak-loss limit; no mixed n/k is invented.',
            'Default counts, chirp interpolation, Hann envelope and 0.45 index contrast are project conventions, not validated fabrication rules.',
            'No phase/GD/GDD acceptance, paper-exact reproduction or manufacturing guarantee.']}
    if assume_unknown_k_zero:
        assumption = design_absorption_assumption(design, frozen_material_snapshot)
        metadata['absorption_assumption'] = assumption
        design = annotate_absorption_assumption(design, assumption)
    return FilmPhysicalStructureResult(validate_design(design), deepcopy(metadata))


def dual_window_ar_objectives(*, max_reflectance: float, samples_per_window: int = 41,
                              angles_deg=(0.,), polarization='unpolarized') -> tuple[FilmOptimizationObjective, ...]:
    """3–5 and 8–12 um R ceilings; the intervening gap is unconstrained."""
    ceiling = _fraction(max_reflectance, 'max_reflectance')
    samples = _count(samples_per_window, 'samples_per_window', 2)
    return tuple(FilmOptimizationObjective(lo, hi, samples, tuple(angles_deg), polarization,
        metric='R', mode='range', upper_bound=ceiling) for lo, hi in ((3000., 5000.), (8000., 12000.)))


def dual_passband_objectives(*, min_transmittance: float, max_stop_transmittance: float,
                            stopbands_nm: tuple[tuple[float, float], ...],
                            samples_per_band: int = 41, angles_deg=(0.,),
                            polarization='unpolarized') -> tuple[FilmOptimizationObjective, ...]:
    """Two IR T floors plus separately explicit gap/outer T ceilings."""
    floor = _fraction(min_transmittance, 'min_transmittance')
    ceiling = _fraction(max_stop_transmittance, 'max_stop_transmittance')
    samples = _count(samples_per_band, 'samples_per_band', 2)
    if not isinstance(stopbands_nm, (tuple, list)) or not stopbands_nm:
        raise ValueError('dual passband requires explicit stopbands')
    bands = ((3000., 5000.), (8000., 12000.))
    result = [FilmOptimizationObjective(lo, hi, samples, tuple(angles_deg), polarization,
        metric='T', mode='range', lower_bound=floor) for lo, hi in bands]
    for band in stopbands_nm:
        if not isinstance(band, (tuple, list)) or len(band) != 2:
            raise ValueError('stopband must contain two endpoints')
        lo, hi = (_number(v, 'stopband endpoint') for v in band)
        if lo >= hi or any(max(lo,a) <= min(hi,b) for a,b in bands):
            raise ValueError('stopband must be ordered and disjoint from passbands')
        result.append(FilmOptimizationObjective(lo, hi, samples, tuple(angles_deg), polarization,
            metric='T', mode='range', upper_bound=ceiling))
    return tuple(result)


def six_line_ar_objectives(*, max_reflectance: float,
                          wavelengths_nm=(532., 1064., 2100., 3900., 4800., 9500.),
                          angles_deg=(0.,), polarization='unpolarized') -> tuple[FilmOptimizationObjective, ...]:
    """Six independent R points; this does not constrain any interval between them."""
    ceiling = _fraction(max_reflectance, 'max_reflectance')
    if not isinstance(wavelengths_nm, (tuple, list)) or len(wavelengths_nm) != 6:
        raise ValueError('six-line target requires six distinct wavelengths')
    waves = tuple(_number(v, 'line wavelength') for v in wavelengths_nm)
    if len(set(waves)) != 6:
        raise ValueError('six-line target wavelengths must be distinct')
    return tuple(FilmOptimizationObjective(w, w, 1, tuple(angles_deg), polarization,
        metric='R', mode='range', upper_bound=ceiling) for w in waves)


def _fraction(value, label):
    value = _number(value, label, positive=False)
    if not 0 <= value <= 1:
        raise ValueError(f'{label} must be within [0,1]')
    return value
