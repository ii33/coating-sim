"""Automatic reflector proposals and bounded DE proposals.

All heuristics here are project search conventions, not feasibility certificates
or manufacturing limits. The caller owns physical scoring, budgets and audit.
"""
from copy import deepcopy
from dataclasses import dataclass
import math
from itertools import product
import numpy as np
from numpy.typing import NDArray
from .film_design import MAX_LAYERS
from .optical_constants import prepare_optical_set
from .film_synthesis_absorption import sample_synthesis_optical, assumes_unknown_k_zero

@dataclass(frozen=True)
class StructuralProposal:
    design: dict
    provenance: dict


def reflector_objectives(request):
    """Select positive-weight reflectance objectives asking positive reflectance."""
    return tuple(o for o in request.objectives if o.metric == 'R' and o.weight > 0
                 and ((o.mode == 'range' and o.lower_bound is not None and o.lower_bound > 0)
                      or (o.mode == 'value' and o.target_value is not None and o.target_value > 0)))


def iter_reflector_proposals(request, *, epoch=0, cancel_check=lambda: None, report=lambda event: None):
    objectives = reflector_objectives(request)
    if not objectives or request.seed_designs:
        return
    materials = {m['id']: m for m in request.frozen_material_snapshot['materials']}
    samples = {}
    for ref in request.candidate_materials:
        optical = next(o for o in materials[ref.material_id]['optical_sets'] if o['id'] == ref.optical_set_id)
        samples[(ref.material_id, ref.optical_set_id)] = prepare_optical_set(optical, k_sampling_policy='source')
    lo = min(o.wavelength_min_nm for o in objectives)
    hi = max(o.wavelength_max_nm for o in objectives)
    center = math.sqrt(lo * hi)
    waves = (lo, center, hi)
    angles = sorted({a for o in objectives for a in o.angles_deg})
    target = max(float(o.lower_bound if o.mode == 'range' else o.target_value) for o in objectives)
    target = min(target, 1 - np.finfo(float).eps)
    incident = request.environment['incident_medium']
    if incident['kind'] == 'constant':
        incident_sample = None
    else:
        optical = next(o for o in materials[incident['material_id']]['optical_sets'] if o['id'] == incident['optical_set_id'])
        incident_sample = prepare_optical_set(optical, k_sampling_policy='source')
    def phase(pair, wave, angle):
        cancel_check()
        n, k, _ = sample_synthesis_optical(samples[pair], wave, assume_unknown_k_zero=assumes_unknown_k_zero(request))
        n0 = incident['n'] if incident_sample is None else sample_synthesis_optical(incident_sample, wave, assume_unknown_k_zero=assumes_unknown_k_zero(request))[0]
        q = np.sqrt(complex(n, k)**2 - (n0 * math.sin(math.radians(angle)))**2 + 0j)
        if not np.isfinite(q) or q.real <= 0:
            raise ValueError('automatic reflector needs positive propagating real phase thickness')
        return q
    pairs = list(samples)
    serial = 0
    for high in pairs:
        for low in pairs:
            if high == low:
                continue
            cancel_check()
            try:
                ratios = [phase(high, w, a).real / phase(low, w, a).real for w in waves for a in angles]
                contrast = min(ratios)
                if contrast <= 1:
                    report(dict(stage='structure_skipped', reason='ordered pair has no consistent positive phase contrast', high=high, low=low))
                    continue
                # Center reflectance estimate is only a proposal pair count.
                count = max(1, math.ceil(math.log((1 + math.sqrt(target)) / (1 - math.sqrt(target))) / (2 * math.log(contrast))))
                # Wider bands require overlapping local stopbands. This
                # count estimate only schedules candidates; absorption and
                # all requested conditions are evaluated without approximation.
                fractional_stopband = 4 / math.pi * math.asin((contrast - 1) / (contrast + 1))
                bandwidth_factor = max(1, math.ceil(((hi - lo) / center) / fractional_stopband))
                pair_counts = [count * (multiplier + epoch)
                               for multiplier in sorted({1, 2, 2 * bandwidth_factor})]
                # An explicit layer boundary is a useful additional topology,
                # not a request to enumerate all counts or the format ceiling.
                if request.max_layers is not None:
                    boundary_pairs = min(request.max_layers, MAX_LAYERS) // 2
                    if boundary_pairs > 0 and boundary_pairs not in pair_counts:
                        pair_counts.append(boundary_pairs)
                for count_now in pair_counts:
                    if 2 * count_now > (request.max_layers if request.max_layers is not None else MAX_LAYERS):
                        report(dict(stage='structure_skipped', reason='explicit max_layers or existing format MAX_LAYERS', layer_count=2 * count_now))
                        continue
                    variants = [('periodic', 'constant', 0., 0.), ('multistack', 'two_section', 0., 0.)]
                    pads = (0., fractional_stopband / 6, fractional_stopband / 4)
                    variants.extend(('chirped', spacing, low_pad, high_pad) for spacing in ('linear', 'geometric')
                                    for low_pad, high_pad in product(pads, pads))
                    for family, spacing, low_pad, high_pad in variants:
                        for reverse, direction in product((False, True), (1, -1) if family != "periodic" else (1,)):
                            cancel_check()
                            centers: list[float] | NDArray[np.float64]
                            if family == 'periodic':
                                centers = [center] * count_now
                            elif family == 'chirped':
                                grid = np.linspace if spacing == 'linear' else np.geomspace
                                centers = grid(lo / (1 + low_pad), hi * (1 + high_pad), count_now)
                            else:
                                centers = [lo if i < count_now / 2 else hi for i in range(count_now)]
                            if direction < 0:
                                centers = list(reversed(centers))
                            try:
                                ds = []
                                refs = []
                                for wave in centers:
                                    for pair in ((low, high) if reverse else (high, low)):
                                        # Midpoint of requested angle extremes; all angles are rescored.
                                        q = phase(pair, float(wave), (angles[0] + angles[-1]) / 2)
                                        ds.append(float(wave) / (4 * q.real))
                                        refs.append(pair)
                            except (ValueError, ArithmeticError) as exc:
                                report(dict(stage='structure_skipped', reason=str(exc), high=high, low=low,
                                            family=family, spacing=spacing, low_pad_fraction=low_pad,
                                            high_pad_fraction=high_pad, reverse=reverse, wavelength_direction=direction))
                                continue
                            if len(ds) > (request.max_layers if request.max_layers is not None else MAX_LAYERS):
                                report(dict(stage='structure_skipped', reason='explicit max_layers or existing format MAX_LAYERS', layer_count=len(ds)))
                                continue
                            if (request.max_total_thickness_nm is not None and sum(ds) > request.max_total_thickness_nm
                                or request.max_layer_thickness_nm is not None and max(ds) > request.max_layer_thickness_nm
                                or request.minimum_nonzero_thickness_nm is not None and min(ds) < request.minimum_nonzero_thickness_nm):
                                report(dict(stage='structure_skipped', reason='explicit thickness constraints', layer_count=len(ds)))
                                continue
                            serial += 1
                            design = deepcopy(request.environment)
                            design['front_layers'] = [dict(id=f'auto-hr-{serial}-{i}', material_id=pair[0], optical_set_id=pair[1], thickness=dict(value=d, unit='nm'), note='Automatic reflector structural proposal') for i, (pair, d) in enumerate(zip(refs, ds, strict=True))]
                            yield StructuralProposal(design, dict(family=family, variant=family, spacing=spacing, low_pad_fraction=low_pad, high_pad_fraction=high_pad, epoch=epoch, high=high, low=low, pairs=count_now, estimated_pairs=count, bandwidth_factor=bandwidth_factor, contrast=contrast, reverse=reverse, wavelength_direction=direction, phase_angle_deg=(angles[0]+angles[-1])/2, search_convention=True))
            except (ValueError, ArithmeticError) as exc:
                report(dict(stage='structure_skipped', reason=str(exc), high=high, low=low))



def bandpass_objectives(request):
    """Return T floors bracketed by disjoint positive R floors, without scoring."""
    def positive_floor(objective):
        value = (objective.lower_bound if objective.mode == 'range'
                 else objective.target_value if objective.mode == 'value' else None)
        return objective.weight > 0 and value is not None and value > 0

    stops = reflector_objectives(request)
    result = []
    for passed in request.objectives:
        if passed.metric != 'T' or not positive_floor(passed):
            continue
        lower = tuple(o for o in stops if o.wavelength_max_nm < passed.wavelength_min_nm)
        upper = tuple(o for o in stops if o.wavelength_min_nm > passed.wavelength_max_nm)
        if lower and upper:
            result.append((passed, lower, upper))
    return tuple(result)



def _bandpass_phase_orders(epoch, *, cancel_check=lambda: None):
    """Coarse-to-fine finite grid ordering; no physical merit is inferred.

    The first up to 24 grid points maximize distance from the preceding points
    in the normalized three-dimensional order grid. Lexicographic tie-breaking
    makes the prefix reproducible. Remaining Cartesian points follow once each;
    the prefix size is a scheduling convention, never a candidate/work budget.
    """
    if type(epoch) is not int or epoch < 0:
        raise ValueError('bandpass epoch must be a nonnegative integer')
    cancel_check()
    grid = list(product(range(1, 9 + 8 * epoch), range(1, 5 + 4 * epoch),
                        range(2, 5 + epoch)))
    points = np.asarray(grid, float)
    span = np.ptp(points, axis=0)
    points = (points - points[0]) / np.where(span > 0, span, 1.)
    distances = np.full(len(grid), np.inf)
    selected = np.zeros(len(grid), dtype=bool)
    index = 0
    for _ in range(min(24, len(grid))):
        cancel_check()
        selected[index] = True
        yield grid[index]
        distances = np.minimum(distances, np.sum((points - points[index]) ** 2, axis=1))
        distances[selected] = -1.
        index = int(np.argmax(distances))
    for index, orders in enumerate(grid):
        cancel_check()
        if not selected[index]:
            yield orders


def iter_bandpass_proposals(request, *, epoch=0, cancel_check=lambda: None,
                           report=lambda event: None):
    """Lazy symmetric mirror/cavity/mirror seeds, never physical evaluations.

    Orders, matching fractions and pair-count estimates are project search
    conventions. Full dispersive complex source n/k determine phase thickness;
    the caller must score every condition and enforce its own search budget.
    Layer order is substrate outward, physical thickness in nm.
    """
    if type(epoch) is not int or epoch < 0:
        raise ValueError('bandpass epoch must be a nonnegative integer')
    objectives = bandpass_objectives(request)
    if not objectives or request.seed_designs:
        return
    cancel_check()
    environment = request.environment
    if (environment['front_layers'] or environment['back_layers']
            or environment['substrate']['sides'] != 'single'
            or environment['first_surface'] != 'front'):
        report(dict(stage='structure_skipped', family='coupled_cavity',
                    reason='bandpass construction requires bare single-sided front environment'))
        return
    materials = {m['id']: m for m in request.frozen_material_snapshot['materials']}
    samples = {}

    def sampler(pair):
        if pair not in samples:
            optical = next(o for o in materials[pair[0]]['optical_sets'] if o['id'] == pair[1])
            samples[pair] = prepare_optical_set(optical, k_sampling_policy='source')
        return samples[pair]

    def nk(pair, wave):
        cancel_check()
        return sample_synthesis_optical(sampler(pair), wave,
            assume_unknown_k_zero=assumes_unknown_k_zero(request))[:2]

    incident = environment['incident_medium']

    def phase(pair, wave, angle):
        n, k = nk(pair, wave)
        if incident['kind'] == 'constant':
            n0 = incident['n']
        else:
            n0, k0 = nk((incident['material_id'], incident['optical_set_id']), wave)
            if k0 != 0:
                raise ValueError('incident medium must have k=0')
        q = np.sqrt(complex(n, k)**2 - (n0 * math.sin(math.radians(angle)))**2 + 0j)
        if not np.isfinite(q) or q.real <= 0:
            raise ValueError('automatic bandpass needs positive propagating real phase thickness')
        return q.real

    refs = [(ref.material_id, ref.optical_set_id) for ref in request.candidate_materials]
    layer_cap = min(MAX_LAYERS, request.max_layers if request.max_layers is not None else MAX_LAYERS)
    serial = 0
    seen_bands = set()
    for passed, lower_stops, upper_stops in objectives:
        cancel_check()
        relevant = (passed, *lower_stops, *upper_stops)
        center = math.sqrt(passed.wavelength_min_nm * passed.wavelength_max_nm)
        width = passed.wavelength_max_nm - passed.wavelength_min_nm
        waves = sorted({center, *(w for o in relevant for w in (o.wavelength_min_nm, o.wavelength_max_nm))})
        angles = sorted({angle for o in relevant for angle in o.angles_deg})
        angle = (angles[0] + angles[-1]) / 2
        stops = (*lower_stops, *upper_stops)
        band_key = (passed.wavelength_min_nm, passed.wavelength_max_nm,
                    tuple(sorted({(o.wavelength_min_nm, o.wavelength_max_nm) for o in stops})),
                    tuple(angles))
        if band_key in seen_bands:
            continue
        seen_bands.add(band_key)
        target = max(float(o.lower_bound if o.mode == 'range' else o.target_value) for o in stops)
        # Small-loss, first-order Fabry-Perot linewidth estimate schedules mirror
        # counts only. It cannot assert a lossy/angular structure meets any bound.
        linewidth_reflectance = 1 - math.pi * width / center
        target = min(max(target, linewidth_reflectance), 1 - np.finfo(float).eps)
        for high, low in product(refs, repeat=2):
            if high == low:
                continue
            cancel_check()
            try:
                contrast = min(phase(high, w, a) / phase(low, w, a) for w in waves for a in angles)
                if contrast <= 1:
                    report(dict(stage='structure_skipped', family='coupled_cavity',
                                reason='ordered pair has no consistent positive phase contrast', high=high, low=low))
                    continue
                substrate = environment['substrate']
                for wave in waves:
                    nk((substrate['material_id'], substrate['optical_set_id']), wave)
                quarter = {ref: center / (4 * phase(ref, center, angle)) for ref in (high, low)}
            except (ValueError, ArithmeticError) as exc:
                report(dict(stage='structure_skipped', family='coupled_cavity', reason=str(exc), high=high, low=low))
                continue
            estimated = max(1, math.ceil(math.log((1 + math.sqrt(target)) /
                (1 - math.sqrt(target))) / (2 * math.log(contrast))))
            templates = []
            for pairs in range(1, min(estimated + epoch, max(0, (layer_cap - 1) // 4)) + 1):
                for cavities in range(1, 4 + epoch):
                    cancel_check()
                    # Weaker internal mirrors couple adjacent resonators. Both
                    # one-pair and up-to-external-count variants remain explicit.
                    for coupling in sorted({1, max(1, pairs - 1)}):
                        if cavities == 1 and coupling != 1:
                            continue
                        for matching in (0., .5, 1.):
                            count = 4 * pairs + cavities + (cavities - 1) * (2 * coupling + 1) + 2 * bool(matching)
                            if count > layer_cap:
                                report(dict(stage='structure_skipped', family='coupled_cavity',
                                    reason='explicit max_layers or existing format MAX_LAYERS', layer_count=count))
                                continue
                            templates.append((pairs, cavities, coupling, matching))
            if not templates:
                report(dict(stage='structure_skipped', family='coupled_cavity',
                    reason='explicit max_layers or existing format MAX_LAYERS', layer_count=5))
            # Round-robin topology coverage at each phase order. Epochs expand
            # the family rather than imposing a hidden lifetime search budget.
            for order_h, order_l, cavity_order in _bandpass_phase_orders(
                    epoch, cancel_check=cancel_check):
                for pairs, cavities, coupling, matching in templates:
                    for reverse in (False, True):
                        cancel_check()
                        outer, inner = (low, high) if reverse else (high, low)
                        orders = {high: order_h, low: order_l}
                        left = [(outer, quarter[outer]), (inner, quarter[inner])] * pairs
                        left[-2:] = [(ref, quarter[ref] * orders[ref]) for ref in (outer, inner)]
                        core = [(outer, quarter[outer] * cavity_order)]
                        coupling_rows = ([(inner, quarter[inner]), (outer, quarter[outer])] * coupling
                                         + [(inner, quarter[inner])])
                        for _ in range(cavities - 1):
                            core.extend(coupling_rows)
                            core.append((outer, quarter[outer] * cavity_order))
                        rows = left + core + list(reversed(left))
                        if matching:
                            match = (inner, quarter[inner] * matching)
                            rows = [match, *rows, match]
                        ds = [d for _, d in rows]
                        if (not all(math.isfinite(d) and d > 0 for d in ds)
                                or request.max_total_thickness_nm is not None and math.fsum(ds) > request.max_total_thickness_nm
                                or request.max_layer_thickness_nm is not None and max(ds) > request.max_layer_thickness_nm
                                or request.minimum_nonzero_thickness_nm is not None and min(ds) < request.minimum_nonzero_thickness_nm):
                            report(dict(stage='structure_skipped', family='coupled_cavity',
                                reason='explicit thickness constraints', layer_count=len(rows)))
                            continue
                        serial += 1
                        design = deepcopy(environment)
                        design['front_layers'] = [dict(id=f'auto-bpf-{epoch}-{serial}-{i}',
                            material_id=ref[0], optical_set_id=ref[1], thickness=dict(value=d, unit='nm'),
                            note='Automatic symmetric bandpass structural proposal')
                            for i, (ref, d) in enumerate(rows)]
                        groups = tuple((i, len(rows) - 1 - i) if i != len(rows) - 1 - i else (i,)
                                       for i in range((len(rows) + 1) // 2))
                        yield StructuralProposal(design, dict(family='coupled_cavity',
                            variant='matched_cavity' if matching else 'single_cavity' if cavities == 1 else 'coupled_cavity',
                            epoch=epoch, high=high, low=low, mirror_pairs=pairs,
                            estimated_pairs=estimated, contrast=contrast, cavity_count=cavities,
                            cavity_material=outer, cavity_quarters=cavity_order,
                            coupling_pairs=coupling, matching_quarters=matching,
                            inner_phase_orders=dict(high=order_h, low=order_l), reverse=reverse,
                            passband_nm=(passed.wavelength_min_nm, passed.wavelength_max_nm),
                            passband_width_nm=width, stopbands_nm=tuple((o.wavelength_min_nm, o.wavelength_max_nm) for o in stops),
                            phase_angle_deg=angle, symmetry_groups=groups, search_convention=True))


def _symmetry_groups(design, symmetry_groups):
    rows = design['front_layers']
    groups = tuple(tuple(group) for group in symmetry_groups)
    indices = [index for group in groups for index in group]
    if (not groups or any(not group for group in groups)
            or any(type(index) is not int for index in indices)
            or sorted(indices) != list(range(len(rows)))):
        raise ValueError('symmetry groups must partition all front layer indices')
    for group in groups:
        first = rows[group[0]]
        for index in group:
            row = rows[index]
            if ((row['material_id'], row['optical_set_id']) != (first['material_id'], first['optical_set_id'])
                    or row['thickness']['unit'] != 'nm'
                    or not math.isfinite(row['thickness']['value']) or row['thickness']['value'] <= 0
                    or not math.isclose(row['thickness']['value'], first['thickness']['value'], rel_tol=1e-12)):
                raise ValueError('symmetry group requires matching materials and positive equal nm thicknesses')
    return groups


def symmetric_thickness_parameters(design, symmetry_groups, *, epoch=0,
                                   minimum_nm=None, maximum_nm=None):
    """Absolute log(nm) group parameters and a finite exploratory box.

    Each group is one independent variable, initially within a factor eight
    of its seed thickness; epoch expands that exploratory log radius. This is
    a project search convention, not a manufacturing bound. Per-layer bounds,
    when provided, intersect the box; total-thickness constraints remain the
    caller's responsibility because a rectangular box cannot express them.
    """
    if type(epoch) is not int or epoch < 0:
        raise ValueError('symmetry epoch must be a nonnegative integer')
    groups = _symmetry_groups(design, symmetry_groups)
    for value in (minimum_nm, maximum_nm):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError('physical thickness bounds must be finite and positive')
    initial = np.log([design['front_layers'][group[0]]['thickness']['value'] for group in groups])
    radius = math.log(8.) * (epoch + 1)
    lower = np.maximum(initial - radius, math.log(np.finfo(float).tiny))
    upper = np.minimum(initial + radius, math.log(np.finfo(float).max) - 1)
    if minimum_nm is not None:
        lower = np.maximum(lower, math.log(minimum_nm))
    if maximum_nm is not None:
        upper = np.minimum(upper, math.log(maximum_nm))
    if np.any(lower > initial) or np.any(initial > upper):
        raise ValueError('initial symmetry thickness lies outside physical bounds')
    return initial, lower, upper


def symmetric_thickness_design(design, parameters, *, symmetry_groups):
    """Map independent absolute log(nm) thicknesses without changing layer IDs."""
    groups = _symmetry_groups(design, symmetry_groups)
    parameters = np.asarray(parameters, float)
    if parameters.shape != (len(groups),) or not np.all(np.isfinite(parameters)):
        raise ValueError('one finite log thickness per symmetry group required')
    trial = deepcopy(design)
    for group, value in zip(groups, parameters, strict=True):
        try:
            thickness = math.exp(float(value))
        except OverflowError as exc:
            raise ValueError('symmetry transform produced invalid physical thickness') from exc
        if not math.isfinite(thickness) or thickness <= 0:
            raise ValueError('symmetry transform produced invalid physical thickness')
        for index in group:
            trial['front_layers'][index]['thickness'] = dict(value=thickness, unit='nm')
    return trial


def structured_thickness_design(design, parameters, *, high_pair=None):
    """Three log parameters: uniform scale, pair ramp, and H/L ratio.

    d_i' = d_i exp(a + b t_pair + c s_i), with t_pair in [-1,1] and
    s_i=+1 for the declared high material, -1 for the low material. This
    transforms real physical thicknesses; it fabricates no optical constants.
    """
    parameters = np.asarray(parameters, float)
    if parameters.shape != (3,) or not np.all(np.isfinite(parameters)):
        raise ValueError('three finite structure parameters required')
    trial = deepcopy(design)
    rows = trial['front_layers']
    if not rows:
        raise ValueError('structure transform requires at least one layer')
    if high_pair is None:
        high_pair = (rows[0]['material_id'], rows[0]['optical_set_id'])
    coordinates = np.linspace(-1., 1., max(1, (len(rows)+1)//2))
    a, b, c = parameters
    for i, row in enumerate(rows):
        sign = 1 if (row['material_id'], row['optical_set_id']) == tuple(high_pair) else -1
        thickness = row['thickness']['value'] * math.exp(a + b * coordinates[i//2] + c * sign)
        if not math.isfinite(thickness) or thickness <= 0:
            raise ValueError('structure transform produced invalid physical thickness')
        row['thickness'] = dict(value=float(thickness), unit='nm')
    return trial


class DifferentialEvolutionProposals:
    """Generational best1/bin; scoring and population acceptance stay ordered.

    Bounds are an explicitly reported exploratory box, never manufacturing
    bounds. Each generation must receive one scalar per proposed individual.
    """
    def __init__(self, initial, lower, upper, *, seed=0, population_size=12):
        self.rng = np.random.default_rng(seed)
        self.lower, self.upper = np.asarray(lower, float), np.asarray(upper, float)
        initial = np.asarray(initial, float)
        if initial.ndim != 1 or not initial.size or self.lower.shape != initial.shape or self.upper.shape != initial.shape or not np.all(np.isfinite([initial,self.lower,self.upper])) or np.any(self.lower > self.upper) or np.any(self.lower > initial) or np.any(initial > self.upper):
            raise ValueError('finite ordered DE bounds containing the initial point required')
        self.population = self.rng.uniform(self.lower, self.upper, (max(5, population_size), len(initial)))
        # Preserve the optical prior while retaining genuinely global samples.
        # Half the population explores the whole reported box; half perturbs
        # the prior within 10% of box width (project initialization convention).
        nearby = max(1, len(self.population) // 2)
        self.population[:nearby] = np.clip(initial + self.rng.normal(0, .1, (nearby, len(initial))) * (self.upper-self.lower), self.lower, self.upper)
        self.population[0] = initial
        self.losses = None
        self.pending = self.population.copy()
        self.generations = 0

    def proposals(self):
        if self.losses is None:
            return self.pending.copy()
        best = self.population[int(np.argmin(self.losses))]
        trials = []
        for i, current in enumerate(self.population):
            choices = [j for j in range(len(self.population)) if j != i]
            a, b = self.rng.choice(choices, 2, replace=False)
            mutant = np.clip(best + self.rng.uniform(.5, 1.) * (self.population[a] - self.population[b]), self.lower, self.upper)
            cross = self.rng.random(len(current)) < .7
            cross[self.rng.integers(len(current))] = True
            trials.append(np.where(cross, mutant, current))
        self.pending = np.asarray(trials)
        return self.pending.copy()

    def accept(self, losses):
        losses = np.asarray(losses, float)
        if losses.shape != (len(self.population),) or np.any(np.isnan(losses)):
            raise ValueError('one non-NaN scalar loss per DE proposal required')
        if self.losses is None:
            self.losses = losses.copy()
        else:
            accepted = losses <= self.losses
            self.population[accepted] = self.pending[accepted]
            self.losses[accepted] = losses[accepted]
            self.generations += 1
