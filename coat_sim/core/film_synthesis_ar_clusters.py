"""Real-material non-quarter-wave / clustered AR proposal and parameter maps.

All grids, layer counts, shares and exploration boxes are project conventions.
No response scoring, manufactured intermediate index or feasibility certificate
is provided. Fixed local-reference Re(q)*d budgets have units nm and include
all exported films; they are not a broadband integral or physical total thickness.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations, product
import math

import numpy as np

from .film_design import MAX_LAYERS
from .film_synthesis_absorption import assumes_unknown_k_zero, sample_synthesis_optical
from .film_synthesis_global import StructuralProposal
from .film_synthesis_matching import matching_objectives
from .optical_constants import UNITS_TO_UM, prepare_optical_set

_MODEL = 'role-normalized-relative-envelope-v1'
_LOG_MIN = math.log(float(np.nextafter(0., 1.)))
_LOG_MAX = math.log(float(np.finfo(float).max))
_LOG_SPAN = _LOG_MAX - _LOG_MIN


def _exploration_radius(base, epoch):
    # Saturation follows finite-log representability, not an iteration budget.
    return 2 * _LOG_SPAN if epoch + 1 >= 2 * _LOG_SPAN / base else base * (epoch + 1)


def _logsumexp(values):
    values = np.asarray(values, float)
    if not values.size or not np.all(np.isfinite(values)):
        raise ValueError('finite positive optical weights required')
    largest = float(np.max(values))
    return largest + math.log(math.fsum(float(v) for v in np.exp(values - largest)))


def _iter_pair_counts(maximum, layer_cap, *, uneven=False, third=False,
                      cancel_check=lambda: None):
    """Layer-cap-legal uniform counts or lazy bounded integer compositions."""
    capacity = max(0, (layer_cap - 2 - int(third)) // 2)
    if not uneven:
        for pairs in range(1, min(maximum, capacity) + 1):
            for count in (1, 2, 3):
                cancel_check()
                if pairs * count <= capacity:
                    yield (pairs,) * count
        return

    def compositions(prefix, remaining, slots):
        cancel_check()
        if not slots:
            if len(set(prefix)) > 1:
                yield prefix
            return
        for pairs in range(1, min(maximum, remaining - slots + 1) + 1):
            cancel_check()
            yield from compositions((*prefix, pairs), remaining - pairs, slots - 1)

    for count in (2, 3):
        if count <= capacity:
            yield from compositions((), capacity, count)



def _model(design, metadata):
    rows = design['front_layers']
    slots = metadata.get('layer_slots', ())
    clusters = metadata.get('clusters', ())
    edges = tuple(metadata.get('boundary_layer_indices', ()))
    if metadata.get('parameter_model') != _MODEL or not clusters or len(slots) != len(rows):
        raise ValueError('a matching AR cluster parameter model is required')
    groups: list[int] = []
    for cluster in clusters:
        high, low = tuple(cluster['high_indices']), tuple(cluster['low_indices'])
        if not high or len(high) != len(low):
            raise ValueError('each cluster requires equally many nonempty high/low indices')
        groups.extend((*high, *low))
    groups.extend(edges)
    if any(type(i) is not int for i in groups) or sorted(groups) != list(range(len(rows))):
        raise ValueError('AR cluster and boundary indices must partition all front layers')
    for row, slot in zip(rows, slots, strict=True):
        if (row['id'] != slot['id'] or (row['material_id'], row['optical_set_id']) != tuple(slot['material'])
                or row['thickness']['unit'] != 'nm' or not math.isfinite(row['thickness']['value'])
                or row['thickness']['value'] <= 0 or not math.isfinite(slot['q']) or slot['q'] <= 0):
            raise ValueError('AR cluster slots require matching IDs/materials and positive physical thickness/q')
    for cluster in clusters:
        for role, key in (('high', 'high_indices'), ('low', 'low_indices')):
            if any(tuple(slots[i]['material']) != tuple(metadata[role])
                   or slots[i]['q'] != cluster[f'{role}_q'] for i in cluster[key]):
                raise ValueError('cluster high/low roles must match their fixed material and q bindings')
    return rows, slots, clusters, edges


def _layout(clusters, edges):
    layout: list[tuple[str, int]] = []
    for index, cluster in enumerate(clusters):
        layout.extend((('log_optical_budget_nm', index), ('logit_high_share', index)))
        if len(cluster['high_indices']) >= 2:
            layout.append(('relative_envelope_slope', index))
        if len(cluster['high_indices']) >= 3:
            layout.append(('relative_envelope_curvature', index))
    layout.extend(('log_boundary_thickness_nm', index) for index in edges)
    return layout


def _current_parameters(rows, slots, clusters, edges):
    initial: list[float] = []
    for cluster in clusters:
        high, low = (_logsumexp([math.log(slots[i]['q']) + math.log(rows[i]['thickness']['value'])
            for i in cluster[key]]) for key in ('high_indices', 'low_indices'))
        initial.extend((float(np.logaddexp(high, low)), high - low))
        if len(cluster['high_indices']) >= 2:
            initial.append(0.)
        if len(cluster['high_indices']) >= 3:
            initial.append(0.)
    initial.extend(math.log(rows[i]['thickness']['value']) for i in edges)
    return np.asarray(initial, float)


def ar_cluster_parameters(design, metadata, *, epoch=0, minimum_nm=None, maximum_nm=None):
    """Current-seed identity point and finite exploratory rectangular log box.

    Independent per-cluster B/high-share and relative envelope increments retain
    any current per-role thickness differences. One/two pairs omit unidentifiable
    slope/curvature. Boundaries have independent absolute log physical thickness.

    Boxes intersect necessary B/boundary manufacturing bounds; internal coupled
    manufacturing constraints remain the caller's responsibility, with no clip.
    For extreme epochs the sum rB+rf+2*rs+rc is bounded by current layer log(d)
    distance to positive finite binary64 limits. This only protects representable
    mappings; it is neither a manufacturing constraint nor an iteration budget.
    """
    if type(epoch) is not int or epoch < 0:
        raise ValueError('AR cluster epoch must be a nonnegative integer')
    for value in (minimum_nm, maximum_nm):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError('physical layer bounds must be finite and positive')
    rows, slots, clusters, edges = _model(design, metadata)
    if any((minimum_nm is not None and row['thickness']['value'] < minimum_nm)
           or (maximum_nm is not None and row['thickness']['value'] > maximum_nm) for row in rows):
        raise ValueError('current cluster seed is outside explicit physical layer bounds')
    initial = _current_parameters(rows, slots, clusters, edges)
    layout = _layout(clusters, edges)
    radii = np.asarray([_exploration_radius(math.log(8.) if kind.startswith('log_') else
        math.log(4.) if kind == 'logit_high_share' else 1.5, epoch) for kind, _ in layout])
    for index, cluster in enumerate(clusters):
        positions = [i for i, (kind, ci) in enumerate(layout) if ci == index and kind != 'log_boundary_thickness_nm']
        logs = [math.log(rows[i]['thickness']['value']) for i in (*cluster['high_indices'], *cluster['low_indices'])]
        margin = max(0., min(min(v - _LOG_MIN, _LOG_MAX - v) for v in logs) - 1e-10)
        total = math.fsum(radii[i] * (2 if layout[i][0] == 'relative_envelope_slope' else 1) for i in positions)
        if total > margin:
            radii[positions] *= margin / total
    lower, upper = initial - radii, initial + radii
    for position, (kind, index) in enumerate(layout):
        if kind.startswith('log_'):
            lower[position] = max(lower[position], _LOG_MIN)
            upper[position] = min(upper[position], _LOG_MAX)
        elif kind == 'logit_high_share':
            lower[position] = max(lower[position], -_LOG_SPAN)
            upper[position] = min(upper[position], _LOG_SPAN)
        if kind == 'log_optical_budget_nm':
            cluster = clusters[index]
            log_sum_q = _logsumexp([math.log(slots[i]['q']) for i in (*cluster['high_indices'], *cluster['low_indices'])])
            if minimum_nm is not None:
                lower[position] = max(lower[position], min(initial[position], math.log(minimum_nm) + log_sum_q))
            if maximum_nm is not None:
                upper[position] = min(upper[position], max(initial[position], math.log(maximum_nm) + log_sum_q))
        elif kind == 'log_boundary_thickness_nm':
            if minimum_nm is not None:
                lower[position] = max(lower[position], math.log(minimum_nm))
            if maximum_nm is not None:
                upper[position] = min(upper[position], math.log(maximum_nm))
    if not np.all(np.isfinite([initial, lower, upper])) or np.any(lower > initial) or np.any(initial > upper):
        raise ValueError('finite exploratory bounds containing the current cluster seed required')
    return initial, lower, upper


def ar_cluster_design(design, point, metadata):
    """Map current real-layer templates with stable independent log shares.

    Fixed q/reference wavelengths never change. Logsumexp normalization preserves
    very small positive role budgets without computing 1-sigmoid(x), and no layer
    is clipped or merged. Initial parameters copy any valid current seed exactly.
    Outside representable domains ValueError identifies a candidate to skip.
    """
    rows, slots, clusters, edges = _model(design, metadata)
    initial = _current_parameters(rows, slots, clusters, edges)
    point = np.asarray(point, float)
    if point.shape != initial.shape or not np.all(np.isfinite(point)):
        raise ValueError('one finite parameter per AR cluster model coordinate required')
    trial = deepcopy(design)
    if np.array_equal(point, initial):
        return trial
    position = 0
    try:
        for cluster in clusters:
            log_budget, logit = (float(v) for v in point[position:position + 2])
            position += 2
            if not _LOG_MIN <= log_budget <= _LOG_MAX:
                raise ValueError('representable positive finite cluster budget required')
            tail = math.log1p(math.exp(-abs(logit)))
            log_shares = (-max(-logit, 0.) - tail, -max(logit, 0.) - tail)
            count = len(cluster['high_indices'])
            slope = float(point[position]) if count >= 2 else 0.
            position += count >= 2
            curvature = float(point[position]) if count >= 3 else 0.
            position += count >= 3
            t = np.linspace(-1., 1., count) if count > 1 else np.zeros(1)
            exponent = slope * t + curvature * (t * t - np.mean(t * t))
            for indices, log_share in zip((cluster['high_indices'], cluster['low_indices']), log_shares, strict=True):
                log_weights = np.asarray([math.log(slots[i]['q']) + math.log(rows[i]['thickness']['value']) for i in indices]) + exponent
                log_total = _logsumexp(log_weights)
                for index, log_weight in zip(indices, log_weights, strict=True):
                    log_d = log_budget + log_share + float(log_weight - log_total) - math.log(slots[index]['q'])
                    thickness = math.exp(log_d)
                    if not math.isfinite(thickness) or thickness <= 0:
                        raise ValueError('AR cluster map produced unrepresentable physical thickness')
                    trial['front_layers'][index]['thickness'] = dict(value=thickness, unit='nm')
        for index in edges:
            thickness = math.exp(float(point[position]))
            position += 1
            if not math.isfinite(thickness) or thickness <= 0:
                raise ValueError('AR boundary map produced unrepresentable physical thickness')
            trial['front_layers'][index]['thickness'] = dict(value=thickness, unit='nm')
    except OverflowError as exc:
        raise ValueError('AR cluster map produced unrepresentable physical thickness') from exc
    return trial


def iter_ar_cluster_proposals(request, *, epoch=0, cancel_check=lambda: None,
                              report=lambda event: None):
    """Lazy real-material alternating stacks with one/two/three AR clusters.

    The first sweep interleaves all materials, K, uniform per-cluster pair
    counts and full-film optical budgets. Later sweeps add uneven counts,
    high shares, physical envelopes, asymmetric thin edges and an optional
    distinct allowed material with lower phase index across checked source
    knots/full objective grids. Finite checks cannot certify continuous order.
    """
    if type(epoch) is not int or epoch < 0:
        raise ValueError('AR cluster epoch must be a nonnegative integer')
    objectives = matching_objectives(request)
    if not objectives or request.seed_designs:
        return
    cancel_check()
    environment = request.environment
    if (environment['front_layers'] or environment['back_layers']
            or environment['substrate']['sides'] != 'single' or environment['first_surface'] != 'front'):
        report(dict(stage='structure_skipped', family='ar_cluster', reason='bare single-sided front environment required'))
        return
    lo, hi = min(o.wavelength_min_nm for o in objectives), max(o.wavelength_max_nm for o in objectives)
    center = math.sqrt(lo * hi)
    angles = sorted({a for o in objectives for a in o.angles_deg})
    angle = (angles[0] + angles[-1]) / 2
    waves = {lo, center, hi, *(float(w) for o in objectives for w in np.linspace(
        o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples))}
    materials = {m['id']: m for m in request.frozen_material_snapshot['materials']}
    references = tuple(dict.fromkeys((m.material_id, m.optical_set_id) for m in request.candidate_materials))
    # Source knots supplement the complete requested grid when ordering an
    # optional third material. No extrapolation or continuous certificate.
    for ref in references:
        optical = next(o for o in materials[ref[0]]['optical_sets'] if o['id'] == ref[1])
        for table in (optical, optical.get('k_data') or {}):
            unit = table.get('wavelength_unit')
            factor = UNITS_TO_UM.get(unit) if isinstance(unit, str) else None
            if factor is not None:
                unit_nm = 1000 * factor
                waves.update(float(w) * unit_nm for w in table.get('wavelengths', ())
                             if lo <= float(w) * unit_nm <= hi)
    samplers, nk_cache, phase_cache = {}, {}, {}
    incident = environment['incident_medium']

    def nk(ref, wave):
        cancel_check()
        key = (ref, wave)
        if key not in nk_cache:
            if ref not in samplers:
                optical = next(o for o in materials[ref[0]]['optical_sets'] if o['id'] == ref[1])
                samplers[ref] = prepare_optical_set(optical, k_sampling_policy='source')
            nk_cache[key] = sample_synthesis_optical(samplers[ref], wave,
                assume_unknown_k_zero=assumes_unknown_k_zero(request))[:2]
        return nk_cache[key]

    def phase(ref, wave, angle_deg):
        key = (ref, wave, angle_deg)
        if key not in phase_cache:
            n, k = nk(ref, wave)
            if incident['kind'] == 'constant':
                n0 = incident['n']
            else:
                n0, k0 = nk((incident['material_id'], incident['optical_set_id']), wave)
                if k0 != 0:
                    raise ValueError('incident medium must have k=0')
            q = np.sqrt(complex(n, k)**2 - (n0 * math.sin(math.radians(angle_deg)))**2 + 0j)
            if not np.isfinite(q) or q.real <= 0:
                raise ValueError('positive propagating real phase thickness required')
            phase_cache[key] = q.real
        return phase_cache[key]

    pairs = []
    for first, second in combinations(references, 2):
        cancel_check()
        try:
            ratios = [phase(first, w, a) / phase(second, w, a) for w in sorted(waves) for a in angles]
            if min(ratios) > 1:
                high, low, contrast = first, second, min(ratios)
            elif max(ratios) < 1:
                high, low, contrast = second, first, 1 / max(ratios)
            else:
                report(dict(stage='structure_skipped', family='ar_cluster', reason='inconsistent phase-index order', materials=(first, second)))
                continue
            substrate = environment['substrate']
            for wave in sorted(waves):
                nk((substrate['material_id'], substrate['optical_set_id']), wave)
            thirds = []
            for third in references:
                if third in (high, low):
                    continue
                try:
                    if all(nk(third, w)[0] < nk(low, w)[0] and phase(third, w, a) < phase(low, w, a)
                           for w in sorted(waves) for a in angles):
                        thirds.append(third)
                except (ValueError, ArithmeticError) as exc:
                    report(dict(stage='structure_skipped', family='ar_cluster', reason=str(exc), third_material=third))
            pairs.append((contrast, high, low, tuple(thirds)))
        except (ValueError, ArithmeticError) as exc:
            report(dict(stage='structure_skipped', family='ar_cluster', reason=str(exc), materials=(first, second)))
    pairs.sort()
    if not pairs:
        return
    layer_cap = min(MAX_LAYERS, request.max_layers if request.max_layers is not None else MAX_LAYERS)
    max_pairs = min(1 + math.ceil(math.log2(hi / lo)) + epoch, max(0, (layer_cap - 2) // 2))
    if not max_pairs:
        report(dict(stage='structure_skipped', family='ar_cluster', reason='explicit max_layers or existing format MAX_LAYERS', layer_count=4))
        return
    minimum = request.minimum_nonzero_thickness_nm
    primary_edge = 'explicit_minimum' if minimum is not None else 'qw16'
    settings = [(False, .35, 'flat', primary_edge, False)]
    settings.extend((unequal, share, envelope, edge, third)
        for unequal, share, envelope, edge, third in product((False, True), (.35, .65, .5, .2, .8),
            ('flat', 'ramp', 'bowl'), (primary_edge, 'qw4', 'asymmetric_qw'), (False, True))
        if (unequal, share, envelope, edge, third) != settings[0])
    # Only binary64-representable positive finite phase budgets are scanned.
    # ldexp avoids materializing 2**power; integer range bounds stay lazy.
    _, center_exponent = math.frexp(center)
    budget_exponents = range(max(-3 - epoch, -1075 - center_exponent),
                             min(3 + epoch, 1025 - center_exponent))
    serial = 0
    for use_uneven, share, envelope, edge_variant, use_third in settings:
        for budget_exponent in budget_exponents:
            cancel_check()
            for pair_counts in _iter_pair_counts(max_pairs, layer_cap, uneven=use_uneven,
                    third=use_third, cancel_check=cancel_check):
                for first_role in ('L', 'H'):
                    for contrast, high, low, lower_thirds in pairs:
                        cancel_check()
                        options = lower_thirds if use_third else (None,)
                        for optional_third in options:
                            cancel_check()
                            count = 2 * sum(pair_counts) + 2 + (optional_third is not None)
                            if count > layer_cap:
                                report(dict(stage='structure_skipped', family='ar_cluster', reason='explicit max_layers or existing format MAX_LAYERS', layer_count=count))
                                continue
                            k = len(pair_counts)
                            local_waves = [lo * (hi / lo) ** ((i + .5) / k) for i in range(k)]
                            roles = {'H': high, 'L': low}
                            second_role = 'H' if first_role == 'L' else 'L'
                            substrate_ref, air_ref = roles[second_role], roles[first_role]
                            try:
                                q_edges = [phase(substrate_ref, center, angle), phase(air_ref, center, angle)]
                                edge_fractions = (.25, .125) if edge_variant == 'asymmetric_qw' else (.25, .25) if edge_variant == 'qw4' else (1 / 16, 1 / 16)
                                edge_ds = [minimum, minimum] if edge_variant == 'explicit_minimum' else [
                                    center * fraction / (4 * q) for q, fraction in zip(q_edges, edge_fractions, strict=True)]
                                edge_refs = [substrate_ref, air_ref]
                                if optional_third is not None:
                                    q3 = phase(optional_third, center, angle)
                                    q_edges.append(q3)
                                    edge_refs.append(optional_third)
                                    edge_ds.append(minimum if minimum is not None else center / (64 * q3))
                                edge_budget = math.fsum(q * d for q, d in zip(q_edges, edge_ds, strict=True))
                                total_budget = math.ldexp(center, budget_exponent)
                                core_budget = total_budget - edge_budget
                                if core_budget <= 0:
                                    report(dict(stage='structure_skipped', family='ar_cluster', reason='nonpositive core optical budget after explicit edge allocation'))
                                    continue
                                raw = [(substrate_ref, edge_ds[0], q_edges[0])]
                                clusters = []
                                for ci, (p, wave) in enumerate(zip(pair_counts, local_waves, strict=True)):
                                    qh, ql = phase(high, wave, angle), phase(low, wave, angle)
                                    t = np.linspace(-1., 1., p) if p > 1 else np.zeros(1)
                                    s = .75 * (-1 if ci % 2 else 1) if envelope == 'ramp' else 0.
                                    c = .75 if envelope == 'bowl' and p >= 3 else 0.
                                    weights = np.exp(s * t + c * (t * t - np.mean(t * t)))
                                    weights /= np.sum(weights)
                                    budget = core_budget * (p / sum(pair_counts))
                                    cluster = dict(high_indices=[], low_indices=[], reference_wavelength_nm=wave,
                                        high_q=qh, low_q=ql, pair_count=p)
                                    for weight in weights:
                                        for role in (first_role, second_role):
                                            ref, q, fraction = (high, qh, share) if role == 'H' else (low, ql, 1 - share)
                                            cluster['high_indices' if role == 'H' else 'low_indices'].append(len(raw))
                                            raw.append((ref, float(budget * fraction * weight / q), q))
                                    clusters.append(cluster)
                                edge_indices = [0]
                                for ref, d, q in zip(edge_refs[1:], edge_ds[1:], q_edges[1:], strict=True):
                                    edge_indices.append(len(raw))
                                    raw.append((ref, float(d), q))
                            except (ValueError, ArithmeticError) as exc:
                                report(dict(stage='structure_skipped', family='ar_cluster', reason=str(exc), high=high, low=low))
                                continue
                            ds = [d for _, d, _ in raw]
                            if (not all(math.isfinite(d) and d > 0 for d in ds)
                                    or minimum is not None and min(ds) < minimum
                                    or request.max_layer_thickness_nm is not None and max(ds) > request.max_layer_thickness_nm
                                    or request.max_total_thickness_nm is not None and math.fsum(ds) > request.max_total_thickness_nm):
                                report(dict(stage='structure_skipped', family='ar_cluster', reason='explicit thickness constraints', layer_count=count))
                                continue
                            serial += 1
                            design = deepcopy(environment)
                            design['front_layers'] = [dict(id=f'auto-ar-cluster-{epoch}-{serial}-{i}', material_id=ref[0],
                                optical_set_id=ref[1], thickness=dict(value=d, unit='nm'),
                                note='Automatic real-material AR optical-budget cluster proposal') for i, (ref, d, _) in enumerate(raw)]
                            slots = [dict(id=row['id'], material=(row['material_id'], row['optical_set_id']), q=q)
                                for row, (_, _, q) in zip(design['front_layers'], raw, strict=True)]
                            metadata = dict(family='ar_cluster', variant='non_qw' if k == 1 else 'clustered',
                                epoch=epoch, high=high, low=low, contrast=contrast, cluster_count=k,
                                pair_counts=tuple(pair_counts), third_material=optional_third, first_role=first_role,
                                boundary_variant=edge_variant, boundary_layer_indices=tuple(edge_indices),
                                boundary_roles=('substrate', 'incident', 'third_outer') if optional_third is not None else ('substrate', 'incident'),
                                clusters=clusters, layer_slots=slots, parameter_model=_MODEL,
                                parameter_coordinates=tuple(_layout(clusters, edge_indices)),
                                initial_envelope=envelope, initial_high_share=share,
                                proposal_total_phase_budget_nm=math.fsum(q * d for _, d, q in raw),
                                requested_total_phase_budget_nm=total_budget, core_phase_budget_nm=core_budget,
                                edge_phase_budget_nm=edge_budget, reference_wavelength_nm=center,
                                target_bands_nm=tuple((o.wavelength_min_nm, o.wavelength_max_nm) for o in objectives),
                                phase_angle_deg=angle, third_order_check='n_and_q_full_objective_grids_and_source_knots_finite_only',
                                layer_count=count, search_convention=True)
                            yield StructuralProposal(design, metadata)
