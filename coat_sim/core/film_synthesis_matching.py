"""Unscored thin buried-layer / outer-layer AR matching starting structures.

Every count, phase fraction and ordering here is a project search convention,
not a proven performance, fabrication or feasibility rule. The caller owns
physical evaluation, acceptance, audit, cancellation lifetime and work budgets.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations, product
import math

import numpy as np

from .film_design import MAX_LAYERS
from .film_synthesis_absorption import assumes_unknown_k_zero, sample_synthesis_optical
from .film_synthesis_global import StructuralProposal
from .optical_constants import prepare_optical_set


def matching_objectives(request):
    """Pure R ceilings and low R values; value <= .5 is a project convention."""
    return tuple(o for o in request.objectives if o.metric == 'R' and o.weight > 0
        and o.pointwise.enabled and not o.mean.enabled
        and ((o.pointwise.mode == 'range' and o.pointwise.upper_bound is not None and 0 <= o.pointwise.upper_bound < 1
              and (o.pointwise.lower_bound is None or o.pointwise.lower_bound <= 0))
             or (o.pointwise.mode == 'value' and o.pointwise.target_value is not None and 0 <= o.pointwise.target_value <= .5)))


def iter_matching_proposals(request, *, epoch=0, cancel_check=lambda: None,
                            report=lambda event: None):
    """Yield substrate-outward (LH)^m L seeds with real physical nm layers.

    L/H identify the ordered real phase-index pair, not fabricated intermediate
    optical constants. Either starting role and direction is proposed. Buried
    layers use quarter-wave fractions or exactly the user-specified minimum;
    the terminal layer is O(quarter-wave). Optional geometric band sections
    vary buried-layer reference wavelengths. No optical response is evaluated.

    The first sweep covers every material pair, pair count, representative
    wavelength and orientation with terminal quarter-wave thickness and the
    explicit minimum (or QW/16 when no minimum was supplied). Later sweeps fill
    the phase/profile grid. Epoch expands counts and grids, never a total budget.
    """
    if type(epoch) is not int or epoch < 0:
        raise ValueError('matching epoch must be a nonnegative integer')
    objectives = matching_objectives(request)
    if not objectives or request.seed_designs:
        return
    cancel_check()
    environment = request.environment
    if (environment['front_layers'] or environment['back_layers']
            or environment['substrate']['sides'] != 'single'
            or environment['first_surface'] != 'front'):
        report(dict(stage='structure_skipped', family='matching',
                    reason='matching construction requires bare single-sided front environment'))
        return
    lo = min(o.wavelength_min_nm for o in objectives)
    hi = max(o.wavelength_max_nm for o in objectives)
    representatives = tuple(sorted({lo, hi, math.sqrt(lo * hi), (lo + hi) / 2}))
    waves = sorted({*representatives, *(w for o in objectives for w in (o.wavelength_min_nm, o.wavelength_max_nm))})
    angles = sorted({a for o in objectives for a in o.angles_deg})
    angle = (angles[0] + angles[-1]) / 2
    materials = {m['id']: m for m in request.frozen_material_snapshot['materials']}
    samplers, nk_cache, phase_cache = {}, {}, {}
    assumption = assumes_unknown_k_zero(request)
    incident = environment['incident_medium']

    def nk(pair, wave):
        cancel_check()
        key = (pair, wave)
        if key not in nk_cache:
            if pair not in samplers:
                optical = next(o for o in materials[pair[0]]['optical_sets'] if o['id'] == pair[1])
                samplers[pair] = prepare_optical_set(optical, k_sampling_policy='source')
            nk_cache[key] = sample_synthesis_optical(samplers[pair], wave,
                assume_unknown_k_zero=assumption)[:2]
        return nk_cache[key]

    def phase(pair, wave, angle_deg):
        key = (pair, wave, angle_deg)
        if key not in phase_cache:
            n, k = nk(pair, wave)
            if incident['kind'] == 'constant':
                n0 = incident['n']
            else:
                n0, k0 = nk((incident['material_id'], incident['optical_set_id']), wave)
                if k0 != 0:
                    raise ValueError('incident medium must have k=0')
            q = np.sqrt(complex(n, k)**2 - (n0 * math.sin(math.radians(angle_deg)))**2 + 0j)
            if not np.isfinite(q) or q.real <= 0:
                raise ValueError('automatic matching needs positive propagating real phase thickness')
            phase_cache[key] = q.real
        return phase_cache[key]

    references = tuple(dict.fromkeys((m.material_id, m.optical_set_id) for m in request.candidate_materials))
    pairs = []
    for first, second in combinations(references, 2):
        cancel_check()
        try:
            ratios = [phase(first, w, a) / phase(second, w, a) for w in waves for a in angles]
            if min(ratios) > 1:
                high, low, contrast = first, second, min(ratios)
            elif max(ratios) < 1:
                high, low, contrast = second, first, 1 / max(ratios)
            else:
                report(dict(stage='structure_skipped', family='matching',
                    reason='material pair has no consistent positive phase contrast', materials=(first, second)))
                continue
            substrate = environment['substrate']
            for wave in waves:
                nk((substrate['material_id'], substrate['optical_set_id']), wave)
            pairs.append((contrast, high, low))
        except (ValueError, ArithmeticError) as exc:
            report(dict(stage='structure_skipped', family='matching', reason=str(exc), materials=(first, second)))
    pairs.sort()  # Lower contrast is an ordering convention, not a performance score.
    if not pairs:
        return
    layer_cap = min(MAX_LAYERS, request.max_layers if request.max_layers is not None else MAX_LAYERS)
    estimated_pairs = max(1, 1 + math.ceil(math.log2(hi / lo)))
    maximum_pairs = min(estimated_pairs + epoch, max(0, (layer_cap - 1) // 2))
    if not maximum_pairs:
        report(dict(stage='structure_skipped', family='matching',
                    reason='explicit max_layers or existing format MAX_LAYERS', layer_count=3))
        return
    minimum = request.minimum_nonzero_thickness_nm
    primary = ('explicit_minimum', None) if minimum is not None else ('quarter_fraction', 1 / 16)
    fractions = (1 / 4, 1 / 2, 1., 1 / 8, 1 / 16, *(2. ** -i for i in range(5, 6 + epoch)))
    thin_modes = (primary, *(( 'quarter_fraction', fraction) for fraction in fractions))
    # Configuration-major sweeps round-robin materials and m before exhausting
    # any one material family's detailed grid; no first-family budget is implied.
    configurations = [(primary, 1., 'constant')]
    configurations.extend((thin, terminal, spacing)
        for thin, terminal, spacing in product(dict.fromkeys(thin_modes),
            (1., .5, *(1.5 + .5 * i for i in range(epoch + 1))), ('constant', 'geometric_band'))
        if (thin, terminal, spacing) != configurations[0])
    serial = 0
    for (thin_mode, fraction), terminal_quarters, spacing in configurations:
        for count, wave, first_role, reverse in product(range(1, maximum_pairs + 1), representatives,
                                                       ('L', 'H'), (False, True)):
            if spacing == 'geometric_band' and count == 1:
                continue  # These one-section references already occur in constant mode.
            for contrast, high, low in pairs:
                cancel_check()
                roles = {'H': high, 'L': low}
                second_role = 'H' if first_role == 'L' else 'L'
                centers = [wave] * count if spacing == 'constant' else np.geomspace(lo, hi, count)
                rows = []
                try:
                    for center in centers:
                        for role in (first_role, second_role):
                            pair = roles[role]
                            # Phase is still sampled for explicit physical-minimum
                            # layers, preserving source/domain/unknown-k contracts.
                            quarter = float(center) / (4 * phase(pair, float(center), angle))
                            thickness = minimum if thin_mode == 'explicit_minimum' else quarter * fraction
                            rows.append((pair, float(thickness)))
                    terminal_pair = roles[first_role]
                    rows.append((terminal_pair, terminal_quarters * wave /
                                 (4 * phase(terminal_pair, wave, angle))))
                except (ValueError, ArithmeticError) as exc:
                    report(dict(stage='structure_skipped', family='matching', reason=str(exc),
                                high=high, low=low, pairs=count, spacing=spacing))
                    continue
                if reverse:
                    rows.reverse()
                ds = [d for _, d in rows]
                if (not all(math.isfinite(d) and d > 0 for d in ds)
                        or request.minimum_nonzero_thickness_nm is not None and min(ds) < request.minimum_nonzero_thickness_nm
                        or request.max_layer_thickness_nm is not None and max(ds) > request.max_layer_thickness_nm
                        or request.max_total_thickness_nm is not None and math.fsum(ds) > request.max_total_thickness_nm):
                    report(dict(stage='structure_skipped', family='matching',
                        reason='explicit thickness constraints', high=high, low=low, pairs=count,
                        layer_count=len(rows), thin_mode=thin_mode, thin_quarter_fraction=fraction))
                    continue
                serial += 1
                design = deepcopy(environment)
                design['front_layers'] = [dict(id=f'auto-match-{epoch}-{serial}-{index}',
                    material_id=pair[0], optical_set_id=pair[1], thickness=dict(value=d, unit='nm'),
                    note='Automatic AR buried-layer and outer-layer matching proposal')
                    for index, (pair, d) in enumerate(rows)]
                yield StructuralProposal(design, dict(family='matching',
                    variant='quarter_wave_matching' if thin_mode == 'quarter_fraction' and fraction == 1.
                    else 'graded_matching' if spacing == 'geometric_band' else 'thin_matching',
                    epoch=epoch, high=high, low=low, pairs=count, estimated_pairs=estimated_pairs,
                    contrast=contrast, first_role=first_role, reverse=reverse, spacing=spacing,
                    thin_mode=thin_mode, thin_quarter_fraction=fraction,
                    thin_physical_nm=minimum if thin_mode == 'explicit_minimum' else None,
                    terminal_quarters=terminal_quarters, reference_wavelength_nm=wave,
                    section_wavelengths_nm=tuple(float(w) for w in centers),
                    target_bands_nm=tuple((o.wavelength_min_nm, o.wavelength_max_nm) for o in objectives),
                    phase_angle_deg=angle, layer_count=len(rows), search_convention=True))
