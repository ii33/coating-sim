"""Unscored, edge-anchored two-substack HR starting proposals.

Pair counts, fractions, ordering and the lossless stopband-width proxy are
project conventions, not feasibility certificates for dispersive/absorbing
stacks. Frozen source k is retained. Parent scoring and audit remain decisive.
"""
from __future__ import annotations

import cmath
from copy import deepcopy
from itertools import combinations
import math
from typing import TYPE_CHECKING, Any, Callable, Generator

from .film_design import MAX_LAYERS
from .film_synthesis_absorption import assumes_unknown_k_zero, sample_synthesis_optical
from .film_synthesis_global import StructuralProposal
from .optical_constants import prepare_optical_set

if TYPE_CHECKING:
    from .film_auto_synthesis import FilmSynthesisRequest

FRACTIONS = (1 / 3, 1 / 4, 1 / 2)


class _InvalidPrior(Exception):
    pass


def pure_hr_prior_eligible(request: FilmSynthesisRequest) -> bool:
    """Require a bare, bounded, seed-free, single-sided pure HR request."""
    environment = request.environment
    return bool(request.objectives and not request.seed_designs
        and len(request.candidate_materials) >= 2
        and type(request.max_layers) is int and min(request.max_layers, MAX_LAYERS) >= 4
        and request.frozen_material_snapshot is not None
        and environment["layer_order"] == "substrate_outward"
        and not environment["front_layers"] and not environment["back_layers"]
        and environment["substrate"]["sides"] == "single"
        and environment["first_surface"] == "front"
        and all(o.pointwise.enabled and o.metric == "R" and o.pointwise.mode == "range" and o.weight > 0
                and o.pointwise.lower_bound is not None and o.pointwise.lower_bound >= .5
                and (o.pointwise.upper_bound is None or o.pointwise.upper_bound >= 1.)
                for o in request.objectives))


def iter_dual_substack_hr_proposals(request: FilmSynthesisRequest, *, epoch: int = 0,
        cancel_check: Callable[[], None] = lambda: None,
        report: Callable[[dict[str, Any]], None] = lambda event: None) -> Generator[StructuralProposal, None, None]:
    """Yield incident-side low/high-center stacks, HL before LH per fraction.

    Uses only the explicit layer boundary; ``epoch`` records scheduling identity
    and does not silently increase layers or introduce a default 512-layer scan.
    All cancellation callbacks stay outside numerical-error conversion blocks.
    Only this module's marked invalid-physics errors are caught and reported.
    Both material references and thicknesses are reversed together when writing
    the production substrate-outward layer array.
    """
    cancel_check()
    if type(epoch) is not int or epoch < 0:
        raise ValueError("HR prior epoch must be a nonnegative integer")
    if not pure_hr_prior_eligible(request):
        return
    assert request.max_layers is not None and request.frozen_material_snapshot is not None
    pairs = min(request.max_layers, MAX_LAYERS) // 2
    split = ((pairs + 1) // 2, pairs // 2)
    lo = min(o.wavelength_min_nm for o in request.objectives)
    hi = max(o.wavelength_max_nm for o in request.objectives)
    waves = (lo, math.sqrt(lo) * math.sqrt(hi), hi)
    conditions = sorted({(angle, pol) for o in request.objectives for angle in o.angles_deg
        for pol in (("s", "p") if o.polarization == "unpolarized" else (o.polarization,))})
    angles = [angle for angle, _ in conditions]
    phase_angle = (min(angles) + max(angles)) / 2
    references = tuple(dict.fromkeys((r.material_id, r.optical_set_id) for r in request.candidate_materials))
    materials = {row["id"]: row for row in request.frozen_material_snapshot["materials"]}
    samplers: dict[tuple[str, str], Any] = {}
    optical_rows: dict[tuple[str, str], dict[str, Any]] = {}
    phase_cache: dict[tuple[tuple[str, str], float, float], tuple[complex, complex]] = {}
    assumption = assumes_unknown_k_zero(request)
    incident = request.environment["incident_medium"]

    def skipped(reason, **details):
        report(dict(stage="structure_skipped", family="dual_substack_hr", reason=reason, **details))

    def sample(pair, wave):
        cancel_check()
        if pair not in samplers:
            try:
                optical = next(row for row in materials[pair[0]]["optical_sets"] if row["id"] == pair[1])
                samplers[pair] = prepare_optical_set(optical, k_sampling_policy="source")
                optical_rows[pair] = optical
            except (KeyError, StopIteration, ValueError, ArithmeticError) as exc:
                raise _InvalidPrior(str(exc)) from exc
        # Cancellation above cannot be mistaken for an invalid source exception.
        try:
            n, k, _ = sample_synthesis_optical(samplers[pair], wave, assume_unknown_k_zero=assumption)
        except (ValueError, ArithmeticError) as exc:
            raise _InvalidPrior(str(exc)) from exc
        return complex(n, k)

    def phase(pair, wave, angle):
        cancel_check()
        key = (pair, wave, angle)
        if key in phase_cache:
            return phase_cache[key]
        index = sample(pair, wave)
        if incident["kind"] == "constant":
            n0 = complex(incident["n"], incident.get("k", 0.))
        else:
            n0 = sample((incident["material_id"], incident["optical_set_id"]), wave)
        if not math.isfinite(n0.real) or n0.real <= 0 or n0.imag != 0:
            raise _InvalidPrior("production forward path requires a real propagating incident medium")
        try:
            q = cmath.sqrt(index * index - (n0 * math.sin(math.radians(angle))) ** 2)
        except ArithmeticError as exc:
            raise _InvalidPrior(str(exc)) from exc
        if q.imag < 0 or (q.imag == 0 and q.real < 0):
            q = -q
        if not math.isfinite(q.real) or not math.isfinite(q.imag) or q.real <= 0:
            raise _InvalidPrior("positive finite passive longitudinal phase is required")
        phase_cache[key] = (index, q)
        return index, q

    def admittance(pair, wave, angle, polarization):
        index, q = phase(pair, wave, angle)
        try:
            value = q if polarization == "s" else index * index / q
        except ArithmeticError as exc:
            raise _InvalidPrior(str(exc)) from exc
        if not math.isfinite(value.real) or not math.isfinite(value.imag) or value.real <= 0:
            raise _InvalidPrior("positive finite real optical admittance is required")
        return value.real

    def sources(high: tuple[str, str], low: tuple[str, str]):
        result = []
        source_pairs: list[tuple[str, str]] = [high, low]
        if incident["kind"] != "constant":
            source_pairs.append((incident["material_id"], incident["optical_set_id"]))
        for pair in dict.fromkeys(source_pairs):
            row = optical_rows[pair]
            source: dict[str, Any] = dict(material_id=pair[0], optical_set_id=pair[1])
            source.update({key: deepcopy(row[key])
                for key in ("source", "source_url", "hash", "source_hash", "sha256") if key in row})
            result.append(source)
        return result

    serial = 0
    contrast_cache = {}
    for fraction in FRACTIONS:
        cancel_check()
        for first, second in combinations(references, 2):
            cancel_check()
            key = (first, second)
            try:
                if key not in contrast_cache:
                    raw = []
                    for wave in waves:
                        for angle, pol in conditions:
                            cancel_check()
                            a = admittance(first, wave, angle, pol)
                            b = admittance(second, wave, angle, pol)
                            ratio = a / b
                            if not math.isfinite(ratio) or ratio <= 0:
                                raise _InvalidPrior("nonfinite or nonpositive witness contrast")
                            raw.append((wave, angle, pol, a, b, ratio))
                    if min(row[5] for row in raw) > 1:
                        high, low, reverse_pair = first, second, False
                    elif max(row[5] for row in raw) < 1:
                        high, low, reverse_pair = second, first, True
                    else:
                        raise _InvalidPrior("no consistently ordered s/p admittance contrast at all witnesses")
                    witnesses = [dict(wavelength_nm=w, angle_deg=a, polarization=p,
                        high_admittance_real=(yb if reverse_pair else ya),
                        low_admittance_real=(ya if reverse_pair else yb),
                        ratio=(1 / r if reverse_pair else r)) for w, a, p, ya, yb, r in raw]
                    contrast = min(row["ratio"] for row in witnesses)
                    if not math.isfinite(contrast) or contrast <= 1:
                        raise _InvalidPrior("finite witness contrast greater than one is required")
                    delta = 4 / math.pi * math.asin((contrast - 1) / (contrast + 1))
                    if not math.isfinite(delta) or delta <= 0:
                        raise _InvalidPrior("positive finite stopband proxy is required")
                    contrast_cache[key] = (high, low, contrast, delta, witnesses)
                high, low, contrast, delta, witnesses = contrast_cache[key]
                centers = (lo * (1 + fraction * delta), hi / (1 + fraction * delta))
                if not all(math.isfinite(c) and c > 0 for c in centers) or centers[0] >= centers[1]:
                    raise _InvalidPrior("edge-anchored centers cross or coincide")
                for reverse in (False, True):
                    cancel_check()
                    layer_refs, thicknesses = [], []
                    order = (low, high) if reverse else (high, low)
                    for center, count in zip(centers, split, strict=True):
                        for _ in range(count):
                            for pair in order:
                                cancel_check()
                                _, q = phase(pair, center, phase_angle)
                                thickness = center / (4 * q.real)
                                if not math.isfinite(thickness) or thickness <= 0:
                                    raise _InvalidPrior("positive finite physical thickness is required")
                                layer_refs.append(pair)
                                thicknesses.append(thickness)
                    total_thickness = sum(thicknesses)
                    if not math.isfinite(total_thickness):
                        raise _InvalidPrior("finite total physical thickness is required")
                    if ((request.minimum_nonzero_thickness_nm is not None
                            and min(thicknesses) < request.minimum_nonzero_thickness_nm)
                            or (request.max_layer_thickness_nm is not None
                                and max(thicknesses) > request.max_layer_thickness_nm)
                            or (request.max_total_thickness_nm is not None
                                and total_thickness > request.max_total_thickness_nm)):
                        skipped("explicit physical thickness constraints", fraction=fraction,
                            high=high, low=low, reverse=reverse)
                        continue
                    serial += 1
                    design = deepcopy(dict(request.environment))
                    stored_layers = list(reversed(list(zip(layer_refs, thicknesses, strict=True))))
                    design["front_layers"] = [dict(id=f"auto-hr-dual-{serial}-{i}",
                        material_id=pair[0], optical_set_id=pair[1], thickness=dict(value=d, unit="nm"),
                        note="Project edge-anchored dual-substack HR prior")
                        for i, (pair, d) in enumerate(stored_layers)]
                    yield StructuralProposal(design, dict(family="dual_substack_hr", variant="edge_anchored",
                        epoch=epoch, high=high, low=low, pairs=pairs, pair_counts=split,
                        layer_count=len(thicknesses), contrast=contrast, fractional_stopband=delta,
                        fraction=fraction, centers_nm=centers, reverse=reverse,
                        material_order="LH" if reverse else "HL", center_order="low_then_high",
                        order_scope="incident_to_substrate", stored_material_order="HL" if reverse else "LH",
                        storage_order="substrate_outward",
                        phase_angle_deg=phase_angle, witness_conditions=deepcopy(witnesses),
                        optical_sources=sources(high, low), search_convention=True,
                        source_scope="frozen optical source IDs only; dual-stack layout and width proxy are project conventions",
                        incident_scope="real propagating incident medium; full passive film n+ik retained"))
            except _InvalidPrior as exc:
                skipped(str(exc), high=first, low=second, fraction=fraction)
