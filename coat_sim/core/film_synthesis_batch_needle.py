"""Analytic position scans and simultaneous mathematical needle construction.

All coordinates are physical nm in substrate-outward ``front_layers`` order.
Interior positions are fractions of the *original* host, not of a remainder
left by another insertion. A batch is rebuilt once from that original stack.
The Jacobian variable order is the resulting row order, never proposal order.

The derivative only screens infinitesimal directions. Finite joint responses
must be evaluated by the caller's real optical evaluator and jointly optimized;
this module never adds finite single-needle improvements to predict a batch.
Zero/near-zero mathematical seeds are independent of manufacturing limits.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Callable, Mapping, Sequence

import numpy as np
from numpy.typing import NDArray

from .film_design import MAX_LAYERS, validate_design
from .film_optimization_needle import FilmNeedleMaterialReference, _physical_layer_thickness
from .film_optimization_structure import _site_records, _stable_identifier


@dataclass(frozen=True)
class BatchNeedleProposal:
    site: Mapping[str, Any]
    reference: FilmNeedleMaterialReference
    sensitivity: float
    needle_id: str
    split_id: str
    refinement_level: int = 0
    parent_fingerprint: str = ""


@dataclass(frozen=True)
class BatchNeedleBuild:
    design: Mapping[str, Any]
    layer_ids: tuple[str, ...]
    needle_ids: tuple[str, ...]
    zero_thickness_layer_ids: tuple[str, ...]
    anchors: Mapping[str, Mapping[str, Any]]
    host_segments: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class BatchNeedleScan:
    proposals: tuple[BatchNeedleProposal, ...]
    selected: tuple[BatchNeedleProposal, ...]
    probe_count: int
    refined_probe_count: int
    skipped_probe_count: int = 0
    complete: bool = True
    stop_reason: str = "scan_complete"
    skipped_batch_count: int = 0
    topology_equivalent_skip_count: int = 0


@dataclass(frozen=True)
class BatchNeedleCleanup:
    design: Mapping[str, Any]
    removed_exact_zero_ids: tuple[str, ...]
    removed_near_zero_ids: tuple[str, ...]
    merged_ids: Mapping[str, str]
    algebraically_equivalent: bool
    requires_full_reevaluation: bool


def _nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite and nonnegative")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite and nonnegative") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _count(value: Any, name: str, *, zero: bool = False) -> int:
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError(f"{name} must be a {'nonnegative' if zero else 'positive'} integer")
    return value


def _physical_rows(design, frozen):
    if not isinstance(frozen, Mapping):
        raise ValueError("frozen optical data must be a mapping")
    checked = validate_design(deepcopy(dict(design)))
    physical = [float(_physical_layer_thickness(row, checked, frozen))
                for row in checked["front_layers"]]
    if any(not math.isfinite(value) or value < 0 for value in physical):
        raise ValueError("batch needle hosts must have finite nonnegative thicknesses")
    return checked, physical


def batch_needle_parent_fingerprint(design, frozen) -> str:
    """Bind construction anchors to IDs, physical geometry and actual optics.

    This is not a merit-cache key or snapshot-validation capability. Scans do
    not cache/reuse scores or derivatives; every invocation uses its current
    evaluator's complete target/grid. Frozen content, not a supplied digest,
    is hashed, so stale metadata cannot make changed optical arrays identical.
    """
    checked, physical = _physical_rows(design, frozen)
    identity = {
        "layers": [(row["id"], row["material_id"], row["optical_set_id"], value)
                   for row, value in zip(checked["front_layers"], physical, strict=True)],
        "substrate": checked["substrate"], "incident_medium": checked["incident_medium"],
        "use_k": checked["use_k"], "assume_lossless": checked.get("assume_lossless", False),
        "first_surface": checked["first_surface"],
        "exit_medium": checked["exit_medium"], "back_layers": checked["back_layers"],
        "reference_wavelength_nm": checked["reference_wavelength_nm"],
        "frozen_material_content": frozen.get("materials", frozen),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False,
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def _anchor_key(site):
    if site["kind"] == "interior":
        return ("interior", site["host_layer_id"], float(site["fraction"]))
    return ("interface", int(site["interface"]))


def _canonical_site(site, rows, physical):
    """Resolve original stable anchors and reject stale geometry, not remap it."""
    if not isinstance(site, Mapping):
        raise ValueError("needle site must be a mapping")
    prefix = [0.0]
    for value in physical:
        prefix.append(prefix[-1] + value)
    if site.get("kind") == "interface":
        interface = site.get("interface")
        if type(interface) is not int or not 0 <= interface <= len(rows):
            raise ValueError("needle interface must index the original stack")
        left = rows[interface - 1]["id"] if interface else None
        right = rows[interface]["id"] if interface < len(rows) else None
        if (("left_layer_id" in site and site["left_layer_id"] != left) or
                ("right_layer_id" in site and site["right_layer_id"] != right)):
            raise ValueError("needle interface has stale original neighbour IDs")
        return dict(kind="interface", interface=interface, host_index=None,
                    host_layer_id="", fraction=None, position_nm=prefix[interface],
                    left_layer_id=left, right_layer_id=right)
    if site.get("kind") != "interior":
        raise ValueError("needle site kind must be interface or interior")
    host = site.get("host_layer_id")
    index = next((i for i, row in enumerate(rows) if row["id"] == host), None)
    if index is None:
        raise ValueError("needle original host ID is absent")
    fraction = _nonnegative(site.get("fraction"), "needle fraction")
    if not 0 < fraction < 1:
        raise ValueError("interior needle fraction must be strictly between zero and one")
    if "host_index" in site and site["host_index"] != index:
        raise ValueError("needle original host index no longer matches its ID")
    if "host_thickness_nm" in site and not math.isclose(
            _nonnegative(site["host_thickness_nm"], "host_thickness_nm"), physical[index],
            rel_tol=1e-12, abs_tol=1e-10):
        raise ValueError("needle original host thickness changed since the scan")
    if physical[index] == 0:
        raise ValueError("a zero-thickness host has no interior interval")
    return dict(kind="interior", interface=index, host_index=index,
                host_layer_id=host, fraction=fraction, host_thickness_nm=physical[index],
                position_nm=prefix[index] + physical[index] * fraction)


def build_batch_needle_design(
    design: Mapping[str, Any], frozen: Mapping[str, Any],
    proposals: Sequence[BatchNeedleProposal], *,
    seed_thickness_nm: float | Mapping[str, float] = 0.0,
) -> BatchNeedleBuild:
    """Insert all needles in one reconstruction, conserving every original host.

    The first host segment retains the host ID. Later segments use generated
    split IDs. Original fractions and stable interface neighbours are returned
    in ``anchors``. A mapping seed is keyed by needle ID and must cover the
    proposals exactly. No manufacturing minimum or 70/104-layer rule is used.
    """
    candidate, physical = _physical_rows(design, frozen)
    rows = candidate["front_layers"]
    fingerprint = batch_needle_parent_fingerprint(candidate, frozen)
    if isinstance(proposals, (str, bytes)) or not isinstance(proposals, Sequence):
        raise ValueError("proposals must be a sequence of BatchNeedleProposal values")
    occupied = {row["id"] for side in ("front_layers", "back_layers")
                for row in candidate[side]}
    seen_anchors = set()
    interiors: dict[str, list[tuple[BatchNeedleProposal, dict[str, Any], float]]] = {}
    interfaces: dict[int, tuple[BatchNeedleProposal, dict[str, Any], float]] = {}
    anchors = {}
    needle_ids = set()
    if isinstance(seed_thickness_nm, Mapping):
        if set(seed_thickness_nm) != {p.needle_id for p in proposals
                                     if isinstance(p, BatchNeedleProposal)}:
            raise ValueError("seed mapping must cover exactly the batch needle IDs")
    else:
        seed_thickness_nm = _nonnegative(seed_thickness_nm, "seed_thickness_nm")
    for proposal in proposals:
        if not isinstance(proposal, BatchNeedleProposal):
            raise ValueError("proposals must contain BatchNeedleProposal values")
        if not isinstance(proposal.reference, FilmNeedleMaterialReference):
            raise ValueError("needle reference must be FilmNeedleMaterialReference")
        if any(not isinstance(value, str) or not value or len(value) > 128
               for value in (proposal.reference.material_id, proposal.reference.optical_set_id)):
            raise ValueError("needle material and optical-set IDs must be nonempty text")
        if proposal.parent_fingerprint and proposal.parent_fingerprint != fingerprint:
            raise ValueError("needle proposal belongs to a different original parent")
        for identifier in (proposal.needle_id, proposal.split_id):
            if (not isinstance(identifier, str) or not identifier or len(identifier) > 128
                    or identifier in occupied):
                raise ValueError("batch needle and split IDs must be unique, new nonempty IDs")
            occupied.add(identifier)
        site = _canonical_site(proposal.site, rows, physical)
        anchor = _anchor_key(site)
        if anchor in seen_anchors:
            raise ValueError("batch needles cannot occupy the same original position")
        seen_anchors.add(anchor)
        seed = _nonnegative(seed_thickness_nm[proposal.needle_id], "seed_thickness_nm") \
            if isinstance(seed_thickness_nm, Mapping) else seed_thickness_nm
        needle_ids.add(proposal.needle_id)
        anchors[proposal.needle_id] = {**site, "original_host_id": site["host_layer_id"],
                                      "original_fraction": site["fraction"],
                                      "original_position_nm": site["position_nm"]}
        item = (proposal, site, seed)
        if site["kind"] == "interior":
            interiors.setdefault(site["host_layer_id"], []).append(item)
        else:
            interfaces[site["interface"]] = item
    result_rows = []
    host_segments = {}

    def append_needle(item):
        proposal, _site, seed = item
        result_rows.append({"id": proposal.needle_id,
            "material_id": proposal.reference.material_id,
            "optical_set_id": proposal.reference.optical_set_id,
            "thickness": {"value": seed, "unit": "nm"},
            "note": "Mathematical simultaneous analytic needle; not a manufacturing prescription."})

    for index in range(len(rows) + 1):
        if index in interfaces:
            append_needle(interfaces[index])
        if index == len(rows):
            continue
        host = rows[index]
        insertions = sorted(interiors.get(host["id"], ()), key=lambda item: item[1]["fraction"])
        if not insertions:
            result_rows.append(deepcopy(host))
            host_segments[host["id"]] = (host["id"],)
            continue
        boundaries = [0.0, *(item[1]["fraction"] for item in insertions), 1.0]
        segment_ids = [host["id"], *(item[0].split_id for item in insertions)]
        allocated: list[float] = []
        for part, identifier in enumerate(segment_ids):
            thickness = (physical[index] - math.fsum(allocated) if part == len(segment_ids) - 1
                         else physical[index] * (boundaries[part + 1] - boundaries[part]))
            allocated.append(thickness)
            segment = deepcopy(host)
            segment["id"] = identifier
            segment["thickness"] = {"value": thickness, "unit": "nm"}
            result_rows.append(segment)
            if part < len(insertions):
                append_needle(insertions[part])
        host_segments[host["id"]] = tuple(segment_ids)
    candidate["front_layers"] = result_rows
    if candidate.get("expression", {}).get("text"):
        candidate["expression"]["in_sync"] = False
    candidate = validate_design(candidate)
    layer_ids = tuple(row["id"] for row in candidate["front_layers"])
    return BatchNeedleBuild(candidate, layer_ids,
        tuple(identifier for identifier in layer_ids if identifier in needle_ids),
        tuple(row["id"] for row in candidate["front_layers"]
              if row["thickness"]["unit"] == "nm" and row["thickness"]["value"] == 0),
        anchors, host_segments)


def scan_batch_needles(
    design: Mapping[str, Any], frozen: Mapping[str, Any],
    references: Sequence[FilmNeedleMaterialReference], *, evaluate: Callable[..., Mapping[str, Any]],
    position_samples: int = 3, valley_refinement_rounds: int = 1,
    max_needles: int = 1, max_layers: int | None = None, seed: int = 0,
    max_probes: int | None = None, genuine_only: bool = False,
    gradient: Callable[[Mapping[str, Any], Sequence[str]], Sequence[float]] | None = None,
    on_probe: Callable[[BatchNeedleProposal], None] | None = None,
    admit: Callable[[Mapping[str, Any]], bool] | None = None,
) -> BatchNeedleScan:
    """Scan every selected material/interface/interior and refine sampled valleys.

    ``evaluate(design, variable_ids=..., zero_ids=...)`` must return a complete
    physical analytic evaluation; exceptions (including budget/cancellation)
    propagate. ``gradient`` can supply an explicitly weighted guide derivative;
    by default the evaluator's smooth SSE derivative is used. This guide is
    only a ranking, never evidence of strict worst-case improvement.

    Each round bisects neighbours of sampled local minima independently for
    each material/host, including a one-sided valley at either interface. This
    is adaptive finite position exploration, not a continuous-optimality proof.
    ``admit`` is an optional resource admission test, not a hidden layer cap.
    ``max_probes`` is an explicit scheduling quantum; a truncated scan returns
    accumulated proposals with ``complete=False`` and ``stop_reason=stage_budget``.
    A different cycle seed rotates its starting material/position.
    ``genuine_only`` excludes same-source interior needles and interfaces
    matching either film neighbour: these are existing-layer thickness growth,
    not new topology. Such exclusions are reported separately, not as resource
    failures. The bulk substrate is not a film neighbour for this purpose.
    """
    position_samples = _count(position_samples, "position_samples", zero=True)
    valley_refinement_rounds = _count(valley_refinement_rounds, "valley_refinement_rounds", zero=True)
    max_needles = _count(max_needles, "max_needles")
    if max_probes is not None:
        max_probes = _count(max_probes, "max_probes")
    maximum = MAX_LAYERS if max_layers is None else min(MAX_LAYERS, _count(max_layers, "max_layers"))
    if type(genuine_only) is not bool:
        raise ValueError("genuine_only must be a boolean")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    parent, physical = _physical_rows(design, frozen)
    # The requested search limit applies to the front coating. The design
    # representation's independent global cap counts preserved back rows too,
    # even for a single-sided design whose back rows are currently inactive.
    maximum = min(maximum, MAX_LAYERS - len(parent["back_layers"]))
    if len(parent["front_layers"]) > maximum:
        raise ValueError("original parent exceeds the explicit layer limit")
    if (not isinstance(references, Sequence) or isinstance(references, (str, bytes))
            or not references or any(not isinstance(ref, FilmNeedleMaterialReference) for ref in references)):
        raise ValueError("references must contain candidate material optical-set references")
    pairs = [(ref.material_id, ref.optical_set_id) for ref in references]
    if any(not isinstance(value, str) or not value or len(value) > 128
           for pair in pairs for value in pair):
        raise ValueError("candidate material and optical-set IDs must be nonempty text")
    if len(set(pairs)) != len(pairs):
        raise ValueError("candidate material optical-set references must be unique")
    fingerprint = batch_needle_parent_fingerprint(parent, frozen)
    existing = {row["id"] for side in ("front_layers", "back_layers") for row in parent[side]}
    probes: dict[tuple[tuple[Any, ...], str, str], BatchNeedleProposal] = {}
    skipped = set()
    equivalent_skipped = set()
    initial_count = 0
    stage_budget_reached = False

    def probe(site, ref, level):
        nonlocal stage_budget_reached
        nonlocal initial_count
        key = (_anchor_key(site), ref.material_id, ref.optical_set_id)
        if key in probes or key in skipped or key in equivalent_skipped:
            return
        if genuine_only:
            pair = (ref.material_id, ref.optical_set_id)
            if site["kind"] == "interior":
                neighbours = (parent["front_layers"][site["host_index"]],)
            else:
                interface = site["interface"]
                neighbours = parent["front_layers"][max(0, interface - 1):interface + 1]
            if any((row["material_id"], row["optical_set_id"]) == pair for row in neighbours):
                equivalent_skipped.add(key)
                return
        if max_probes is not None and len(probes) >= max_probes:
            stage_budget_reached = True
            return
        ids = [fingerprint, seed, key]
        needle_id = _stable_identifier("batch-needle", ids, existing)
        split_id = _stable_identifier("batch-split", ids, existing | {needle_id})
        proposal = BatchNeedleProposal(deepcopy(site), ref, 0.0, needle_id, split_id, level, fingerprint)
        extra_rows = 2 if site["kind"] == "interior" else 1
        if len(parent["front_layers"]) + extra_rows > maximum:
            skipped.add(key)
            return
        build = build_batch_needle_design(parent, frozen, (proposal,))
        if admit is not None and not admit(build.design):
            skipped.add(key)
            return
        value = evaluate(build.design, variable_ids=build.layer_ids,
                         zero_ids=build.zero_thickness_layer_ids)
        if gradient is None:
            sensitivity = float(value["gradient_by_layer_id"][needle_id])
        else:
            guide: NDArray[np.float64] = np.asarray(gradient(value, build.layer_ids), dtype=float)
            if guide.shape != (len(build.layer_ids),) or not np.all(np.isfinite(guide)):
                raise ValueError("needle guide gradient must match the built layer order")
            sensitivity = float(guide[build.layer_ids.index(needle_id)])
        if not math.isfinite(sensitivity):
            raise ValueError("nonfinite analytic needle sensitivity")
        proposal = BatchNeedleProposal(deepcopy(site), ref, sensitivity, needle_id, split_id,
                                       level, fingerprint)
        probes[key] = proposal
        if level == 0:
            initial_count += 1
        if on_probe is not None:
            on_probe(proposal)

    sites = _site_records(parent, frozen, position_samples)
    task_count = len(sites) * len(references)
    # A scheduling quantum is not an exhaustive scan. Rotate its start using
    # the caller's cycle seed so successive rotations explore new positions.
    offset = (seed * (max_probes or 1)) % task_count
    # Reserve a quarter of a bounded scheduling quantum for real local valley
    # exploration, rather than letting a large coarse scan starve refinement.
    # This is a work-scheduling convention, not a physical search restriction.
    reserve = (max_probes // 4 if max_probes is not None and max_probes >= 8
               and valley_refinement_rounds and task_count > max_probes else 0)
    initial_limit = None if max_probes is None else max_probes - reserve
    next_ordinal = 0
    for ordinal in range(task_count):
        if initial_limit is not None and len(probes) >= initial_limit:
            next_ordinal = ordinal
            break
        task = (ordinal + offset) % task_count
        probe(sites[task // len(references)], references[task % len(references)], 0)
        next_ordinal = ordinal + 1
        if stage_budget_reached:
            break
    for level in range(1, valley_refinement_rounds + 1):
        if stage_budget_reached:
            break
        new_sites = []
        by_host_material: dict[tuple[str, tuple[str, str]], list[tuple[float, float]]] = {}
        for proposal in probes.values():
            site = proposal.site
            pair = (proposal.reference.material_id, proposal.reference.optical_set_id)
            if site["kind"] == "interior":
                by_host_material.setdefault((site["host_layer_id"], pair), []).append(
                    (float(site["fraction"]), proposal.sensitivity))
            else:
                interface = site["interface"]
                for index, fraction in ((interface - 1, 1.0), (interface, 0.0)):
                    if 0 <= index < len(parent["front_layers"]):
                        host_id = parent["front_layers"][index]["id"]
                        by_host_material.setdefault((host_id, pair), []).append(
                            (fraction, proposal.sensitivity))
        for host_index, host in enumerate(parent["front_layers"]):
            if physical[host_index] == 0:
                continue
            for ref in references:
                pair = (ref.material_id, ref.optical_set_id)
                samples = sorted(by_host_material.get((host["id"], pair), ()))
                if len(samples) < 2:
                    continue
                # Freeze each round's samples, so generation order cannot
                # accidentally turn one half-step into a deeper refinement.
                for index, (fraction, sensitivity) in enumerate(samples):
                    left = samples[index - 1][1] if index else math.inf
                    right = samples[index + 1][1] if index + 1 < len(samples) else math.inf
                    if sensitivity > left or sensitivity > right:
                        continue
                    if sensitivity == left == right:
                        continue
                    for neighbour in (index - 1, index + 1):
                        if not 0 <= neighbour < len(samples):
                            continue
                        midpoint = (fraction + samples[neighbour][0]) / 2
                        site = _canonical_site(dict(kind="interior", host_layer_id=host["id"],
                                                    fraction=midpoint), parent["front_layers"], physical)
                        new_sites.append((site, ref))
        before = len(probes)
        for site, ref in new_sites:
            probe(site, ref, level)
            if stage_budget_reached:
                break
        if len(probes) == before:
            break
    # If the sampled portion had no valleys, use its remaining quantum on new
    # coarse positions. Do not silently report this partial grid as exhaustive.
    for ordinal in range(next_ordinal, task_count):
        task = (ordinal + offset) % task_count
        probe(sites[task // len(references)], references[task % len(references)], 0)
        if stage_budget_reached:
            break
    ordered = tuple(sorted(probes.values(), key=lambda proposal: (
        proposal.sensitivity, proposal.site["position_nm"], proposal.reference.material_id,
        proposal.reference.optical_set_id, proposal.needle_id)))
    selected: list[BatchNeedleProposal] = []
    occupied_anchors = set()
    raw_rows = len(parent["front_layers"])
    skipped_batches = 0
    for proposal in ordered:
        if proposal.sensitivity >= 0 or len(selected) >= max_needles:
            break
        anchor = _anchor_key(proposal.site)
        if anchor in occupied_anchors:
            continue
        added = 2 if proposal.site["kind"] == "interior" else 1
        if raw_rows + added > maximum:
            continue
        batch = build_batch_needle_design(parent, frozen, (*selected, proposal))
        if admit is not None and not admit(batch.design):
            skipped_batches += 1
            continue
        selected.append(proposal)
        occupied_anchors.add(anchor)
        raw_rows += added
    # Layer-limit exclusions are part of an explicit restricted scan. Resource
    # skips make it incomplete; neither case implies needle optimality.
    stop_reason = ("stage_budget" if stage_budget_reached else
                   "restricted_or_resource_skips" if skipped else
                   "batch_resource_skips" if skipped_batches else "scan_complete")
    return BatchNeedleScan(ordered, tuple(selected), len(probes), len(probes) - initial_count,
                           len(skipped), not skipped and not stage_budget_reached and not skipped_batches,
                           stop_reason, skipped_batches, len(equivalent_skipped))


def cleanup_batch_needle_design(
    design: Mapping[str, Any], frozen: Mapping[str, Any], *, near_zero_nm: float = 0.0,
) -> BatchNeedleCleanup:
    """Remove exact zeros and merge only equal material AND optical-set films.

    The bulk substrate is never considered for merging. Positive near-zero
    deletion is opt-in mathematical simplification, NOT algebraic equivalence,
    and always requires a new full physical score before any acceptance.
    Manufacturing limits are deliberately absent from this API.
    """
    threshold = _nonnegative(near_zero_nm, "near_zero_nm")
    candidate, physical = _physical_rows(design, frozen)
    exact, near, merged = [], [], {}
    retained: list[dict[str, Any]] = []
    for row, thickness in zip(candidate["front_layers"], physical, strict=True):
        if thickness == 0:
            exact.append(row["id"])
            continue
        if thickness <= threshold:
            near.append(row["id"])
            continue
        current = deepcopy(row)
        current["thickness"] = {"value": thickness, "unit": "nm"}
        if (retained and (retained[-1]["material_id"], retained[-1]["optical_set_id"]) ==
                (current["material_id"], current["optical_set_id"])):
            merged[current["id"]] = retained[-1]["id"]
            retained[-1]["thickness"]["value"] = math.fsum(
                (retained[-1]["thickness"]["value"], thickness))
        else:
            retained.append(current)
    candidate["front_layers"] = retained
    if (exact or near or merged) and candidate.get("expression", {}).get("text"):
        candidate["expression"]["in_sync"] = False
    candidate = validate_design(candidate)
    return BatchNeedleCleanup(candidate, tuple(exact), tuple(near), merged,
                              not near, bool(near))
