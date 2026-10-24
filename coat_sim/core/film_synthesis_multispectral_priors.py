"""Source-derived separated HR-window proposals; no scoring or solver owner.

Stopband widths, pair estimates, allocation and optional half-quarter-wave
matching/connection layers are project conventions, never feasibility claims.
All original R/T conditions require parent coherent scoring and independent audit.
"""
from __future__ import annotations

import cmath
from copy import deepcopy
from itertools import combinations, product
import math
from typing import Any

from .film_design import MAX_LAYERS
from .film_synthesis_absorption import assumes_unknown_k_zero, sample_synthesis_optical
from .film_synthesis_global import StructuralProposal
from .optical_constants import prepare_optical_set


class _InvalidPrior(Exception):
    pass


def _hr_objectives(request):
    return tuple((i, o) for i, o in enumerate(request.objectives)
        if o.pointwise.enabled and o.metric == "R" and o.pointwise.mode == "range" and o.weight > 0
        and o.pointwise.lower_bound is not None and o.pointwise.lower_bound >= .5)


def merged_hr_windows(request):
    """Merge only overlapping/touching HR intervals, retaining original indices."""
    windows: list[dict[str, Any]] = []
    for index, objective in sorted(_hr_objectives(request), key=lambda row:
            (row[1].wavelength_min_nm, row[1].wavelength_max_nm, row[0])):
        lo, hi = objective.wavelength_min_nm, objective.wavelength_max_nm
        if windows and lo <= windows[-1]["wavelength_max_nm"]:
            windows[-1]["wavelength_max_nm"] = max(hi, windows[-1]["wavelength_max_nm"])
            windows[-1]["objective_indices"].append(index)
        else:
            windows.append(dict(wavelength_min_nm=lo, wavelength_max_nm=hi,
                                objective_indices=[index]))
    return tuple(windows)


def multispectral_prior_eligible(request):
    """Bare front-side requests with separated HR windows or mixed constraints."""
    env = request.environment
    hr = _hr_objectives(request)
    if not hr:
        return False
    pure = len(hr) == len(request.objectives) and all(
        o.pointwise.upper_bound is None or o.pointwise.upper_bound >= 1. for _, o in hr)
    return bool(not request.seed_designs and len(request.candidate_materials) >= 2
        and (request.max_layers is None or (type(request.max_layers) is int and request.max_layers >= 2))
        and request.frozen_material_snapshot is not None
        and env["layer_order"] == "substrate_outward"
        and not env["front_layers"] and not env["back_layers"]
        and env["substrate"]["sides"] == "single" and env["first_surface"] == "front"
        and (len(merged_hr_windows(request)) >= 2 or not pure))


def _allocate(need, budget, cancel_check=lambda: None):
    """One pair per center, then deterministic proportional-deficit allocation."""
    if budget < len(need):
        return None
    counts = [1] * len(need)
    # Allocate every reserved pair, including an explicit layer-boundary variant.
    for _ in range(budget - len(need)):
        cancel_check()
        index = max(range(len(need)), key=lambda i: (need[i] / counts[i], -i))
        counts[index] += 1
    return counts


def iter_multispectral_hr_proposals(request, *, epoch=0,
        cancel_check=lambda: None, report=lambda event: None):
    """Yield a finite source-derived family, stored substrate outward in nm.

    Half, three-quarter and full estimated demand schedule a finite ascending
    pair-budget ladder; epochs also add one pair per center to full demand.
    MAX_LAYERS remains the existing format ceiling. Explicit caps additionally
    include their available pair boundary after optional layer reservations.
    Optional
    matching/connection layers reserve their slots before allocating mirror pairs.
    """
    cancel_check()
    if type(epoch) is not int or epoch < 0:
        raise ValueError("multispectral epoch must be a nonnegative integer")
    if not multispectral_prior_eligible(request):
        return
    windows = merged_hr_windows(request)
    cap = min(MAX_LAYERS, request.max_layers if request.max_layers is not None else MAX_LAYERS)
    materials = {row["id"]: row for row in request.frozen_material_snapshot["materials"]}
    references = tuple(dict.fromkeys((r.material_id, r.optical_set_id) for r in request.candidate_materials))
    samplers: dict[tuple[str, str], Any] = {}
    sources: dict[tuple[str, str], dict[str, Any]] = {}
    phase_cache: dict[tuple[tuple[str, str], float, float], tuple[complex, complex]] = {}
    assumption = assumes_unknown_k_zero(request)
    incident = request.environment["incident_medium"]

    def skipped(reason, **details):
        report(dict(stage="structure_skipped", family="multispectral_hr", reason=reason, **details))

    def sample(pair, wave):
        cancel_check()
        try:
            if pair not in samplers:
                optical = next(row for row in materials[pair[0]]["optical_sets"] if row["id"] == pair[1])
                samplers[pair] = prepare_optical_set(optical, k_sampling_policy="source")
                sources[pair] = dict(material_id=pair[0], optical_set_id=pair[1], **{
                    key: deepcopy(optical[key]) for key in
                    ("source", "source_url", "hash", "source_hash", "sha256") if key in optical})
            n, k, _ = sample_synthesis_optical(samplers[pair], wave, assume_unknown_k_zero=assumption)
        except (KeyError, StopIteration, ValueError, ArithmeticError) as exc:
            raise _InvalidPrior(str(exc)) from exc
        return complex(n, k)

    def phase(pair, wave, angle):
        cancel_check()
        key = pair, wave, angle
        if key in phase_cache:
            return phase_cache[key]
        index = sample(pair, wave)
        n0 = (complex(incident["n"], incident.get("k", 0.)) if incident["kind"] == "constant"
              else sample((incident["material_id"], incident["optical_set_id"]), wave))
        if not math.isfinite(n0.real) or n0.real <= 0 or n0.imag != 0:
            raise _InvalidPrior("real propagating incident medium required")
        try:
            q = cmath.sqrt(index * index - (n0 * math.sin(math.radians(angle))) ** 2)
        except ArithmeticError as exc:
            raise _InvalidPrior(str(exc)) from exc
        if q.imag < 0 or (q.imag == 0 and q.real < 0):
            q = -q
        if not math.isfinite(q.real) or not math.isfinite(q.imag) or q.real <= 0:
            raise _InvalidPrior("positive finite passive longitudinal phase required")
        phase_cache[key] = index, q
        return index, q

    def admittance(pair, wave, angle, pol):
        index, q = phase(pair, wave, angle)
        value = q if pol == "s" else index * index / q
        if not math.isfinite(value.real) or not math.isfinite(value.imag) or value.real <= 0:
            raise _InvalidPrior("positive finite real admittance required")
        return value.real

    serial = 0
    for first, second in combinations(references, 2):
        cancel_check()
        try:
            # A candidate material must cover every original target, including T.
            # No sampling is introduced in untargeted gaps between HR windows.
            for objective in request.objectives:
                for wave in {objective.wavelength_min_nm, objective.wavelength_max_nm}:
                    for pair in (first, second):
                        sample(pair, wave)
            window_specs: list[dict[str, Any]] = []
            witnesses: list[list[dict[str, Any]]] = []
            for window in windows:
                cancel_check()
                lo, hi = window["wavelength_min_nm"], window["wavelength_max_nm"]
                objectives = [request.objectives[i] for i in window["objective_indices"]]
                conditions = sorted({(a, p) for o in objectives for a in o.angles_deg
                    for p in (("s", "p") if o.polarization == "unpolarized" else (o.polarization,))})
                raw = []
                for wave in sorted({lo, math.sqrt(lo) * math.sqrt(hi), hi}):
                    for angle, pol in conditions:
                        cancel_check()
                        ratio = admittance(first, wave, angle, pol) / admittance(second, wave, angle, pol)
                        if not math.isfinite(ratio) or ratio <= 0:
                            raise _InvalidPrior("positive finite witness contrast required")
                        raw.append(dict(wavelength_nm=wave, angle_deg=angle, polarization=pol, ratio=ratio))
                sign = 1 if min(row["ratio"] for row in raw) > 1 else (
                    -1 if max(row["ratio"] for row in raw) < 1 else 0)
                if not sign:
                    raise _InvalidPrior("no consistently ordered s/p admittance contrast within HR window")
                ratio = min(row["ratio"] if sign == 1 else 1 / row["ratio"] for row in raw)
                delta = 4 / math.pi * math.asin((ratio - 1) / (ratio + 1))
                # 3/4 proxy-width overlap in logarithmic wavelength coordinates.
                log_step = math.log1p(.75 * delta)
                center_count = max(1, math.ceil(math.log(hi / lo) / log_step))
                if center_count > cap // 2:
                    raise _InvalidPrior("layer budget cannot cover derived HR centers")
                centers = [lo if lo == hi else math.exp(math.log(lo) + (i + .5) * math.log(hi / lo) / center_count)
                           for i in range(center_count)]
                target = min(max(o.pointwise.lower_bound for o in objectives), math.nextafter(1., 0.))
                estimated = max(1, math.ceil(math.log((1 + math.sqrt(target)) /
                    (1 - math.sqrt(target))) / (2 * math.log(ratio))))
                window_specs.append(dict(deepcopy(window), centers_nm=centers,
                    contrast=ratio, fractional_stopband=delta, estimated_pairs_per_center=estimated,
                    target_lower_bound=max(o.pointwise.lower_bound for o in objectives),
                    phase_angle_deg=(conditions[0][0] + conditions[-1][0]) / 2,
                    high=first if sign == 1 else second, low=second if sign == 1 else first))
                witnesses.append([dict(row, ratio=(row["ratio"] if sign == 1 else 1 / row["ratio"]))
                                  for row in raw])
            center_total = sum(len(w["centers_nm"]) for w in window_specs)
            if center_total > cap // 2:
                raise _InvalidPrior("layer budget cannot cover all separated HR windows")
            # A common ordering is unnecessary: each source-derived window owns
            # its local high/low admittance order; parent coherence resolves joins.
            templates = ((False, False), (True, False)) if len(window_specs) == 1 else (
                (False, False), (True, False), (False, True), (True, True))
            for ends, joins in templates:
                cancel_check()
                end_layers = 2 * int(ends)
                connection_layers = (len(window_specs) - 1) * int(joins)
                extras = end_layers + connection_layers
                pair_budget = (cap - extras) // 2
                need = [w["estimated_pairs_per_center"] for w in window_specs for _ in w["centers_nm"]]
                estimated_demand = sum(need)
                demand = estimated_demand + epoch * center_total
                # Conservative demand fractions are scheduling conventions. The
                # threshold proxy is not a minimum feasible layer prescription.
                # Reserve every optional layer before clipping mirror budgets.
                raw_budgets = {(estimated_demand + 1) // 2,
                               (3 * estimated_demand + 3) // 4,
                               estimated_demand, demand}
                if request.max_layers is not None:
                    raw_budgets.add(pair_budget)
                budgets = sorted({min(pair_budget, max(center_total, value)) for value in raw_budgets})
                if pair_budget < center_total:
                    skipped("layer budget cannot reserve matching/connection layers", extra_layers=extras)
                    continue
                for budget, descending, reverse_material in product(budgets, (False, True), (False, True)):
                    cancel_check()
                    counts = _allocate(need, budget, cancel_check)
                    if counts is None:
                        skipped("layer budget cannot cover all derived HR centers", pair_budget=budget)
                        continue
                    offset = 0
                    specs: list[dict[str, Any]] = []
                    for window in window_specs:
                        size = len(window["centers_nm"])
                        specs.append(dict(deepcopy(window), pair_counts=counts[offset:offset + size]))
                        offset += size
                    ordered = list(reversed(specs)) if descending else specs
                    incident_layers = []
                    for wi, window in enumerate(ordered):
                        center_counts = list(zip(window["centers_nm"], window["pair_counts"], strict=True))
                        if descending:
                            center_counts.reverse()
                        high, low = window["high"], window["low"]
                        order = (low, high) if reverse_material else (high, low)
                        angle = window["phase_angle_deg"]
                        def layer(pair, center, fraction=1., phase_angle=angle):
                            _, q = phase(pair, center, phase_angle)
                            thickness = fraction * center / (4 * q.real)
                            if not math.isfinite(thickness) or thickness <= 0:
                                raise _InvalidPrior("positive finite physical thickness required")
                            return pair, thickness
                        if wi == 0 and ends:
                            incident_layers.append(layer(low, center_counts[0][0], .5))
                        if wi > 0 and joins:
                            incident_layers.append(layer(low, center_counts[0][0], .5))
                        for center, count in center_counts:
                            for _ in range(count):
                                for pair in order:
                                    cancel_check()
                                    incident_layers.append(layer(pair, center))
                        if wi == len(ordered) - 1 and ends:
                            incident_layers.append(layer(low, center_counts[-1][0], .5))
                    thicknesses = [d for _, d in incident_layers]
                    if (len(thicknesses) > cap
                        or (request.minimum_nonzero_thickness_nm is not None and min(thicknesses) < request.minimum_nonzero_thickness_nm)
                        or (request.max_layer_thickness_nm is not None and max(thicknesses) > request.max_layer_thickness_nm)
                        or not math.isfinite(sum(thicknesses))
                        or (request.max_total_thickness_nm is not None and sum(thicknesses) > request.max_total_thickness_nm)):
                        skipped("explicit physical thickness constraints", pair_budget=budget, extra_layers=extras)
                        continue
                    serial += 1
                    design = deepcopy(dict(request.environment))
                    design["front_layers"] = [dict(id=f"auto-multi-hr-{epoch}-{serial}-{i}",
                        material_id=pair[0], optical_set_id=pair[1], thickness=dict(value=d, unit="nm"),
                        note="Project separated-window source-derived HR proposal")
                        for i, (pair, d) in enumerate(reversed(incident_layers))]
                    yield StructuralProposal(design, dict(family="multispectral_hr", variant="separated_windows",
                        epoch=epoch, windows=deepcopy(specs), layer_count=len(thicknesses),
                        pair_budget=budget, candidate_pair_budget=budget,
                        estimated_pair_demand=estimated_demand, epoch_pair_demand=demand,
                        demand_ladder_fractions=["1/2", "3/4", "1"],
                        available_pair_budget=pair_budget, layer_cap=cap,
                        explicit_max_layers=request.max_layers,
                        explicit_layer_boundary=(request.max_layers is not None and budget == pair_budget),
                        reserved_end_layers=end_layers, reserved_connection_layers=connection_layers,
                        extra_layers=extras, matching_end_layers=ends,
                        connection_layers=joins, center_order="descending" if descending else "ascending",
                        reverse_material_order=reverse_material, storage_order="substrate_outward",
                        order_scope="incident_to_substrate", witness_conditions=deepcopy(witnesses),
                        optical_sources=[deepcopy(sources[p]) for p in dict.fromkeys((first, second,
                            *(((incident["material_id"], incident["optical_set_id"]),) if incident["kind"] != "constant" else ())))],
                        search_convention=True, assume_unknown_k_zero=assumption,
                        allocation_limited=budget < sum(need), original_objective_count=len(request.objectives),
                        assumptions="Project lossless-width/threshold proxies with source complex n+ik; optional half-quarter-wave joins; no feasibility claim; all original R/T conditions require coherent parent scoring and independent audit"))
        except _InvalidPrior as exc:
            skipped(str(exc), first=first, second=second)
