"""Isolated, quota-admitted extra thickness starts; no physical evaluations.

All perturbation constants are project conventions, not optical guarantees.
Callers retain the base and own candidate/evaluation/resource/time admission.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any, Callable, Mapping

import numpy as np

from .film_synthesis_archive import physical_design_hash
from .film_synthesis_surfaces import selected_rows, selected_sides


ALGORITHM_VERSION = "template-thickness-ar1-v1"
MAX_DRAW_ATTEMPTS = 3


def validate_template_seed(seed: int | None, *, enabled: bool) -> None:
    if type(enabled) is not bool:
        raise ValueError("randomize_template_search must be boolean")
    if enabled and seed is None:
        return
    if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("structure_search_seed must be an integer in [0, 4294967295] (or None for enabled auto)")


def template_randomization_metadata(seed: int | None, *, provenance: str) -> dict[str, Any]:
    return dict(algorithm_version=ALGORITHM_VERSION, bit_generator="PCG64",
                numpy_version=np.__version__, effective_seed=seed, seed_provenance=provenance,
                ar1_correlation=.85, correlated_sigma=.12, independent_sigma=.06,
                log_delta_limit=.35, maximum_draw_attempts=MAX_DRAW_ATTEMPTS)


def template_random_stream(root_seed: int, physical_hash: str, *, stage: str,
                           restart_index: int = 0) -> np.random.Generator:
    """SHA256 domain-separated PCG64, independent of IDs/order/workers/PID."""
    validate_template_seed(root_seed, enabled=True)
    if root_seed is None:
        raise ValueError("template randomization requires a frozen explicit seed")
    payload = [ALGORITHM_VERSION, root_seed, physical_hash, stage, restart_index]
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False).encode("ascii")).digest()
    return np.random.Generator(np.random.PCG64(int.from_bytes(digest, "big")))


def randomized_template_start(design: Mapping[str, Any], *, root_seed: int,
        stage: str, minimum_nm: float | None = None, maximum_nm: float | None = None,
        total_maximum_nm: float | None = None, optimizable_mask: tuple[bool, ...] | None = None,
        feasible: Callable[[Mapping[str, Any]], bool] = lambda design: True,
        cancel_check: Callable[[], None] = lambda: None,
        seen_hashes: set[str] | None = None,
        restart_index: int = 0, selected_surfaces=("front",)) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Try at most three draws, retrying only illegal or duplicate geometry.

    Positive nm variables follow stationary AR(1) in their stack order. Zero
    layers and fixed intervals are untouched. An optional mask is an explicit
    caller variable selection, never a design-schema lock field. No clipping
    to physical bounds, objective scoring, merit filtering or entropy occurs.
    """
    cancel_check()
    selection = selected_sides(selected_surfaces)
    rows = selected_rows(selection, design)
    if optimizable_mask is not None and len(optimizable_mask) != len(rows):
        raise ValueError("optimizable mask must match selected layers")
    lower = minimum_nm or 0.
    movable = [index for index, row in enumerate(rows)
        if row["thickness"]["unit"] == "nm"
        and math.isfinite(row["thickness"]["value"]) and row["thickness"]["value"] > 0
        and (maximum_nm is None or lower < maximum_nm)
        and (optimizable_mask is None or optimizable_mask[index])]
    parent_hash = physical_design_hash(design)
    record: dict[str, Any] = dict(stage=stage, restart_index=restart_index,
        parent_physical_hash=parent_hash, attempts=0, variable_indices=movable,
        status="skipped", reason="no_optimizable_positive_thickness_layers")
    if not movable:
        return None, record
    random_stage = stage if selection == ("front",) else stage + ":surfaces=" + ",".join(selection)
    random = template_random_stream(root_seed, parent_hash, stage=random_stage, restart_index=restart_index)
    seen = (seen_hashes or set()) | {parent_hash}
    for attempt in range(MAX_DRAW_ATTEMPTS):
        cancel_check()
        record["attempts"] = attempt + 1
        innovation = random.standard_normal(len(movable))
        independent = random.standard_normal(len(movable))
        correlated = np.empty(len(movable))
        correlated[0] = innovation[0]
        for index in range(1, len(movable)):
            correlated[index] = .85 * correlated[index - 1] + math.sqrt(1 - .85**2) * innovation[index]
        delta = np.clip(.12 * correlated + .06 * independent, -.35, .35)
        candidate = deepcopy(dict(design))
        for index, shift in zip(movable, delta, strict=True):
            selected_rows(selection, candidate)[index]["thickness"]["value"] *= math.exp(float(shift))
        thicknesses = [row["thickness"]["value"] for row in selected_rows(selection, candidate)]
        legal = (all(math.isfinite(value) and value >= 0
                     and (value == 0 or value >= lower)
                     and (maximum_nm is None or value <= maximum_nm) for value in thicknesses)
                 and (total_maximum_nm is None or sum(thicknesses) <= total_maximum_nm)
                 and feasible(candidate))
        if not legal:
            record["reason"] = "invalid_geometry_after_draw"
            continue
        signature = physical_design_hash(candidate)
        if signature in seen:
            record["reason"] = "duplicate_geometry_after_draw"
            continue
        record.update(status="constructed", reason="legal_extra_start",
            result_physical_hash=signature,
            start_thickness_nm=thicknesses, log_deltas=delta.tolist())
        return candidate, record
    record["reason"] = "three_invalid_or_duplicate_draws_base_retained"
    return None, record
