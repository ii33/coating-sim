"""Target-derived narrow rejection starting stacks at normal incidence.

Binary optical duty cycles reduce the Bragg gap compared with quarter-wave
pairs. Bandwidth and cell attenuation below are seed-construction heuristics,
not feasibility bounds. The parent owns full complex optics and original-target
optimization, resource admission, cancellation and independent audit.
"""
from copy import deepcopy
from itertools import combinations, product
import math

from .film_design import MAX_LAYERS
from .film_synthesis_absorption import assumes_unknown_k_zero, sample_synthesis_optical
from .optical_constants import prepare_optical_set


REJECTION_PROPOSAL_POLICY = 'normal_incidence_phase_diverse_rejection_v4'


def rejection_prior_applicable(request):
    """Potential stream eligibility before surface lifting; no optical work.

    Environment/physical limits are checked by the template and parent. A
    conservative policy tag must also cover back/double-surface adapters.
    """
    from .film_synthesis_multispectral_priors import merged_hr_windows
    if any(a != 0 for o in request.objectives for a in o.angles_deg):
        return False
    passes = tuple(o for o in request.objectives if o.metric == 'T' and o.weight > 0
        and o.pointwise.enabled and o.pointwise.mode == 'range'
        and o.pointwise.lower_bound is not None and o.pointwise.lower_bound > .5)
    return any(any(o.wavelength_max_nm < w['wavelength_min_nm'] for o in passes)
        and any(o.wavelength_min_nm > w['wavelength_max_nm'] for o in passes)
        for w in merged_hr_windows(request))


def iter_rejection_proposals(request, *, epoch=0, cancel_check=lambda: None,
                             report=lambda event: None):
    """Called inside the reflector's surface-template adapter, never scored here."""
    from .film_synthesis_global import StructuralProposal
    from .film_synthesis_multispectral_priors import merged_hr_windows
    if type(epoch) is not int or epoch < 0:
        raise ValueError('rejection epoch must be a nonnegative integer')
    env = request.environment
    if (request.seed_designs or env['front_layers'] or env['back_layers']
            or env['substrate']['sides'] != 'single' or env['first_surface'] != 'front'
            or any(a != 0 for o in request.objectives for a in o.angles_deg)):
        return
    passes = tuple(o for o in request.objectives if o.metric == 'T' and o.weight > 0
        and o.pointwise.enabled and o.pointwise.mode == 'range'
        and o.pointwise.lower_bound is not None and o.pointwise.lower_bound > .5)
    windows = merged_hr_windows(request)
    if not passes or not windows:
        return
    brackets = []
    for w in windows:
        left = [o.wavelength_max_nm for o in passes if o.wavelength_max_nm < w['wavelength_min_nm']]
        right = [o.wavelength_min_nm for o in passes if o.wavelength_min_nm > w['wavelength_max_nm']]
        brackets.append((max(left), min(right)) if left and right else None)
    if not any(brackets):
        return
    cap = min(MAX_LAYERS, request.max_layers if request.max_layers is not None else MAX_LAYERS)
    materials = {m['id']: m for m in request.frozen_material_snapshot['materials']}
    refs = tuple(dict.fromkeys((r.material_id, r.optical_set_id) for r in request.candidate_materials))
    samplers = {}

    def sample(ref, wave):
        cancel_check()
        if ref not in samplers:
            optical = next(o for o in materials[ref[0]]['optical_sets'] if o['id'] == ref[1])
            samplers[ref] = prepare_optical_set(optical, k_sampling_policy='source')
        return sample_synthesis_optical(samplers[ref], wave,
            assume_unknown_k_zero=assumes_unknown_k_zero(request))[:2]

    serial = 0
    for pair in combinations(refs, 2):
        cancel_check()
        try:
            for o in request.objectives:
                for wave in (o.wavelength_min_nm, o.wavelength_max_nm):
                    for ref in pair:
                        sample(ref, wave)
            specs = []
            for w, bracket in zip(windows, brackets, strict=True):
                center = math.sqrt(w['wavelength_min_nm']) * math.sqrt(w['wavelength_max_nm'])
                nk = {r: sample(r, center) for r in pair}
                high, low = sorted(pair, key=lambda r: nk[r][0], reverse=True)
                if not all(math.isfinite(nk[r][0]) and nk[r][0] > 0 for r in pair):
                    raise ValueError('rejection prior needs positive finite phase indices')
                ratio = nk[high][0] / nk[low][0]
                eta = (ratio + 1 / ratio) / 2
                if eta <= 1:
                    raise ValueError('rejection prior needs nonzero index contrast')
                duty = .5
                if bracket is not None:
                    a = (bracket[0] + w['wavelength_min_nm']) / 2
                    b = (bracket[1] + w['wavelength_max_nm']) / 2
                    kappa = math.pi * (b - a) / center / 2
                    duty = math.asin(math.sqrt(min(1., (math.cosh(kappa) - 1) / (eta - 1)))) / math.pi
                floor = max(request.objectives[i].pointwise.lower_bound for i in w['objective_indices'])
                target = min(1 - (1 - floor) * .1, math.nextafter(1., 0.))
                specs.append((w, center, nk, high, low, eta, duty, target))
            # Ordering is only a bulk attenuation heuristic, not a field-loss
            # bound or a feasibility claim. All candidates retain original n/k.
            pass_samples = sorted({wave for o in passes for wave in
                (o.wavelength_min_nm, (o.wavelength_min_nm + o.wavelength_max_nm) / 2,
                 o.wavelength_max_nm)})
            loss_rates = {ref: max(float(sample(ref, wave)[1]) / wave
                                  for wave in pass_samples) for ref in pair}
            cap_wave = math.sqrt(min(o.wavelength_min_nm for o in passes)
                                 * max(o.wavelength_max_nm for o in passes))
            cap_info = None
            try:
                cap_n = {ref: sample(ref, cap_wave)[0] for ref in pair}
                cap_ref = min(pair, key=lambda ref: cap_n[ref])
                if math.isfinite(cap_n[cap_ref]) and cap_n[cap_ref] > 0:
                    cap_info = cap_ref, cap_wave / (4 * cap_n[cap_ref])
            except (ValueError, ArithmeticError):
                # An optional cap reference in a gap must not suppress a valid
                # uncapped seed whose target domains are all supported.
                pass

            def phase_options(spec, duty_factor, pass_samples=tuple(pass_samples)):
                _w, center, nk, high, low, eta, duty, _target = spec
                f = min(.5, duty * duty_factor) if duty < .5 else duty
                values = [(f, 'narrow' if f < .5 else 'quarter_wave')]
                if f < .5:
                    values.append((1 - f, 'complementary_narrow'))
                else:
                    # Inspect actual dispersive phase at pass-window samples.
                    # Rational and dispersion-corrected fractions supply other
                    # phase basins when a periodic seed puts a harmonic in a pass.
                    for wave in pass_samples:
                        nh, _ = sample(high, wave)
                        nl, _ = sample(low, wave)
                        if nh <= 0 or nl <= 0:
                            continue
                        ah = math.pi * center / wave * .5 * nh / nk[high][0]
                        al = math.pi * center / wave * .5 * nl / nk[low][0]
                        contrast = (nh / nl + nl / nh) / 2
                        cell_trace = math.cos(ah) * math.cos(al) - contrast * math.sin(ah) * math.sin(al)
                        order = int(round((ah + al) / math.pi))
                        if abs(cell_trace) <= 1 or order < 2:
                            continue
                        for numerator in sorted({1, order - 1, order // 2, (order + 1) // 2}):
                            if not 0 < numerator < order:
                                continue
                            values.extend(((numerator / order, 'harmonic_phase'),
                                (numerator * wave * nk[high][0] / (center * nh), 'harmonic_phase'),
                                (1 - numerator * wave * nk[low][0] / (center * nl), 'harmonic_phase')))
                unique: dict[float, tuple[float, str]] = {}
                for fraction, mode in values:
                    if math.isfinite(fraction) and 0 < fraction < 1:
                        unique.setdefault(round(fraction, 12), (fraction, mode))
                return list(unique.values())

            for duty_factor, count_factor, reverse, depth_fraction in product(
                    (1., .85, 1.15), (1., 1.5), (False, True), (1., .1)):
                cancel_check()
                choices = []
                for spec in (list(reversed(specs)) if reverse else specs):
                    w, center, nk, high, low, eta, _duty, _target = spec
                    floor = max(request.objectives[i].pointwise.lower_bound for i in w['objective_indices'])
                    target = min(1 - (1 - floor) * depth_fraction, math.nextafter(1., 0.))
                    alternatives = []
                    for f, mode in phase_options(spec, duty_factor):
                        attenuation = math.acosh(1 + (eta - 1) * math.sin(math.pi * f) ** 2)
                        if attenuation <= 0:
                            continue
                        count = math.ceil(count_factor * math.atanh(math.sqrt(target)) / attenuation) + epoch
                        if 2 * max(1, count - 1) > cap:
                            continue
                        loss_proxy = count * center / 2 * (loss_rates[high] * f / nk[high][0]
                            + loss_rates[low] * (1 - f) / nk[low][0])
                        alternatives.append((w, center, nk, high, low, f, count, mode, loss_proxy))
                    alternatives.sort(key=lambda c: (c[8], c[6], c[5]))
                    # A bounded menu is a construction-work convention, never
                    # a manufacturing limit. Preserve the original phase option.
                    selected = alternatives[:8]
                    original = next((c for c in alternatives if c[7] in ('narrow', 'quarter_wave')), None)
                    if original is not None and original not in selected:
                        selected = selected[:7] + [original]
                    choices.append(selected)
                if (any(not options for options in choices)
                        or 2 * sum(max(1, min(c[6] for c in options) - 1) for options in choices) > cap):
                    report(dict(stage='structure_skipped', family='narrow_rejection',
                                reason='explicit or schema layer limit'))
                    continue
                def boundary_count_variants(choices=tuple(tuple(options) for options in choices)):
                    # The cell attenuation estimate omits finite-stack boundary
                    # admittances and coupling between windows. Test adjacent
                    # integer counts as separate real starts, not lower bounds.
                    # Preserve breadth of the nominal phase menu before
                    # neighboring counts consume the finite screening prefix.
                    for delta in (0, -1, 1):
                        for nominal_cells in product(*choices):
                            if delta == 0:
                                yield nominal_cells, None
                                continue
                            for window_index, original in enumerate(nominal_cells):
                                count = original[6] + delta
                                if count < 1:
                                    continue
                                changed = list(original)
                                changed[6] = count
                                changed[8] = original[8] * count / original[6]
                                cells = list(nominal_cells)
                                cells[window_index] = tuple(changed)
                                yield tuple(cells), dict(window_index=window_index, pair_delta=delta,
                                                        nominal_pairs=original[6])

                for cells, pair_adjustment in boundary_count_variants():
                    cancel_check()
                    if 2 * sum(c[6] for c in cells) > cap:
                        continue
                    for add_cap in ((False, True) if cap_info is not None else (False,)):
                        rows: list[dict] = []
                        metadata = []
                        for w, center, nk, high, low, f, count, mode, loss_proxy in cells:
                            metadata.append(dict(deepcopy(w), center_nm=center, high=high, low=low,
                                high_phase_fraction=f, phase_family=mode, pairs=count,
                                bulk_attenuation_proxy=loss_proxy,
                                source_nk_at_center={'high': nk[high], 'low': nk[low]}))
                            for _ in range(count):
                                cancel_check()
                                for ref, fraction in ((high, f), (low, 1 - f)):
                                    rows.append(dict(id=f'narrow-rejection-{serial}-{len(rows)}',
                                        material_id=ref[0], optical_set_id=ref[1],
                                        thickness=dict(value=center * fraction / (2 * nk[ref][0]), unit='nm'),
                                        note='Target-derived phase-diverse rejection starting structure'))
                        if add_cap:
                            assert cap_info is not None
                            ref, thickness = cap_info
                            rows.insert(0, dict(id=f'narrow-rejection-{serial}-cap',
                                material_id=ref[0], optical_set_id=ref[1],
                                thickness=dict(value=thickness, unit='nm'),
                                note='Pass-range quarter-wave matching-cap seed'))
                        serial += 1
                        thicknesses = [r['thickness']['value'] for r in rows]
                        if (len(rows) > cap
                                or (request.max_total_thickness_nm is not None and sum(thicknesses) > request.max_total_thickness_nm)
                                or (request.max_layer_thickness_nm is not None and max(thicknesses) > request.max_layer_thickness_nm)
                                or (request.minimum_nonzero_thickness_nm is not None and min(thicknesses) < request.minimum_nonzero_thickness_nm)):
                            report(dict(stage='structure_skipped', family='narrow_rejection', reason='explicit thickness/layer constraint'))
                            continue
                        modes = {c[7] for c in cells}
                        family = ('narrow_rejection_harmonic' if 'harmonic_phase' in modes else
                            'narrow_rejection_complement' if 'complementary_narrow' in modes else 'narrow_rejection')
                        design = deepcopy(env)
                        design['front_layers'] = list(reversed(rows))
                        yield StructuralProposal(design, dict(family=family, windows=metadata,
                            epoch=epoch, layer_count=len(rows), search_convention=True,
                            duty_factor=duty_factor, pair_count_factor=count_factor,
                            reflection_depth_fraction=depth_fraction,
                            boundary_pair_adjustment=deepcopy(pair_adjustment),
                            matching_cap=add_cap, cap_reference_nm=cap_wave if add_cap else None,
                            reverse_window_order=reverse, acceptance_equivalence=False,
                            construction_scope='normal-incidence phase-diverse binary cells; original complex optics required'))
        except (ValueError, ArithmeticError) as exc:
            report(dict(stage='structure_skipped', family='narrow_rejection', reason=str(exc)))
