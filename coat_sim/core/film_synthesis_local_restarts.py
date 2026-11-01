"""Deterministic physical-thickness basin starts, never acceptance evidence.

Only selected rows change. Explicit manufacturing constraints remain owned by
parent admission; rejected starts never relax those constraints. The sequence
uses the persisted global rotation, not wall time or a case-specific seed.
"""
from copy import deepcopy
import hashlib

import numpy as np

from .film_synthesis_surfaces import selected_rows

LOCAL_RESTART_POLICY = 'selected_thickness_lognormal_v1'


def relative_basin_start(request, design, *, rotation):
    if type(rotation) is not int or rotation < 1:
        raise ValueError('restart rotation must be a positive integer')
    from .film_synthesis_archive import physical_design_hash
    signature = physical_design_hash(design)
    payload = f'{LOCAL_RESTART_POLICY}:{request.structure_search_seed}:{rotation}:{signature}'
    seed = int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], 'little')
    sigma = (.03, .08, .18)[(rotation - 1) % 3]
    rng = np.random.default_rng(seed)
    trial = deepcopy(design)
    rows = selected_rows(request, trial)
    for row, delta in zip(rows, rng.normal(0., sigma, len(rows)), strict=True):
        thickness = row['thickness']['value'] * float(np.exp(delta))
        if request.max_layer_thickness_nm is not None:
            thickness = min(thickness, request.max_layer_thickness_nm)
        if thickness > 0 and request.minimum_nonzero_thickness_nm is not None:
            thickness = max(thickness, request.minimum_nonzero_thickness_nm)
        row['thickness']['value'] = thickness
    return trial, dict(policy=LOCAL_RESTART_POLICY, rotation=rotation,
        relative_log_sigma=sigma, seed=seed, parent_physical_hash=signature,
        original_constraints_unchanged=True)


def refine_basin_within_quantum(request, initial, *, refine, cancel_check):
    """Continue improving iteration-limited solves within the parent's slice.

    A solver's 120-iteration work chunk is not convergence. Do not abandon a
    new basin just because its first chunk is worse than an older champion.
    The parent callback owns all evaluation/time/resource/cancellation limits.
    No extra evaluation or score transformation happens in this scheduler.
    """
    current = initial
    attempts = []
    while True:
        cancel_check()
        before = float(current['score']['max_error'])
        current, details = refine(current)
        attempts.append(details)
        improvement = before - float(current['score']['max_error'])
        if (details['termination'] != 'iteration_limit'
                or improvement <= request.relative_improvement_tolerance * max(abs(before), 1e-12)):
            break
    return current, dict(details, basin_attempts=attempts,
        basin_policy='continue_improving_iteration_chunks_within_parent_quantum')
