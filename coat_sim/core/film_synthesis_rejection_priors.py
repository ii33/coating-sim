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


REJECTION_PROPOSAL_POLICY = 'normal_incidence_narrow_rejection_v1'


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
            for duty_factor, count_factor, reverse in product((1., .85, 1.15), (1., 1.5), (False, True)):
                cancel_check()
                cells = []
                for w, center, nk, high, low, eta, duty, target in (reversed(specs) if reverse else specs):
                    f = min(.5, duty * duty_factor) if duty < .5 else duty
                    attenuation = math.acosh(1 + (eta - 1) * math.sin(math.pi * f) ** 2)
                    if attenuation <= 0:
                        raise ValueError('rejection prior has zero cell attenuation')
                    count = math.ceil(count_factor * math.atanh(math.sqrt(target)) / attenuation) + epoch
                    cells.append((w, center, nk, high, low, f, count))
                if 2 * sum(c[-1] for c in cells) > cap:
                    report(dict(stage='structure_skipped', family='narrow_rejection',
                                reason='explicit or schema layer limit'))
                    continue
                rows: list[dict] = []
                metadata = []
                for w, center, nk, high, low, f, count in cells:
                    metadata.append(dict(w, center_nm=center, high=high, low=low,
                        high_phase_fraction=f, pairs=count, source_nk_at_center={
                            'high': nk[high], 'low': nk[low]}))
                    for _ in range(count):
                        cancel_check()
                        for ref, fraction in ((high, f), (low, 1 - f)):
                            rows.append(dict(id=f'narrow-rejection-{serial}-{len(rows)}',
                                material_id=ref[0], optical_set_id=ref[1],
                                thickness=dict(value=center * fraction / (2 * nk[ref][0]), unit='nm'),
                                note='Target-derived narrow rejection starting structure'))
                serial += 1
                thicknesses = [r['thickness']['value'] for r in rows]
                if ((request.max_total_thickness_nm is not None and sum(thicknesses) > request.max_total_thickness_nm)
                        or (request.max_layer_thickness_nm is not None and max(thicknesses) > request.max_layer_thickness_nm)
                        or (request.minimum_nonzero_thickness_nm is not None and min(thicknesses) < request.minimum_nonzero_thickness_nm)):
                    report(dict(stage='structure_skipped', family='narrow_rejection', reason='explicit thickness constraint'))
                    continue
                design = deepcopy(env)
                design['front_layers'] = list(reversed(rows))
                yield StructuralProposal(design, dict(family='narrow_rejection', windows=metadata,
                    epoch=epoch, layer_count=len(rows), search_convention=True,
                    duty_factor=duty_factor, pair_count_factor=count_factor,
                    reverse_window_order=reverse, acceptance_equivalence=False,
                    construction_scope='normal-incidence binary phase cells; original complex optics required'))
        except (ValueError, ArithmeticError) as exc:
            report(dict(stage='structure_skipped', family='narrow_rejection', reason=str(exc)))
