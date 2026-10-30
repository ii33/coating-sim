"""Prepare numerical-zero removal; the caller must rescore and verify optics."""
from copy import deepcopy
from dataclasses import replace
import math

import numpy as np

from .film_design import validate_design
from .film_synthesis_batch_needle import cleanup_batch_needle_design, _physical_rows, _surfaces


def numeric_zero_cleanup(design, frozen, phase_step_limits_nm, *, max_layer_thickness_nm=None,
                         selected_surfaces=("front",)):
    """Bound omitted phase by 32 double-precision eps at sampled optical inputs.

    phase_step_limits_nm binds |2*pi*q*Delta_d/lambda| <= pi/4 on the
    original target/audit grid and optical source knots. The minimum active
    phase scale is conservative for every removed layer. This is a numerical
    convention, never a manufacturing minimum or an optical acceptance rule.
    """
    sides = _surfaces(design, selected_surfaces)
    rows = [row for side in sides for row in design[f"{side}_layers"]]
    if not rows:
        return None
    threshold = min(phase_step_limits_nm[(r['material_id'], r['optical_set_id'])]
                    for r in rows) * (32 * np.finfo(float).eps) / (math.pi / 4)
    if (all(r['thickness']['unit'] == 'nm' for r in rows)
            and not any(0. < r['thickness']['value'] <= threshold for r in rows)):
        return None
    checked = validate_design(deepcopy(dict(design)))
    physical_by_side = {side: _physical_rows(checked, frozen, side=side)[1] for side in sides}
    physical = [d for side in sides for d in physical_by_side[side]]
    if not any(0. < d <= threshold for d in physical):
        return None
    cleaned = cleanup_batch_needle_design(checked, frozen, near_zero_nm=threshold, selected_surfaces=sides)
    if (max_layer_thickness_nm is not None and
            any(r['thickness']['value'] > max_layer_thickness_nm for side in sides for r in cleaned.design[f"{side}_layers"])):
        # Removing zero may expose identical neighbours whose merged physical
        # thickness would violate an explicit per-layer cap. Preserve their IDs.
        candidate = deepcopy(checked)
        for side in sides:
            candidate[f"{side}_layers"] = []
            for row, d in zip(checked[f"{side}_layers"], physical_by_side[side], strict=True):
                if d > threshold:
                    row = deepcopy(row)
                    row['thickness'] = dict(value=d, unit='nm')
                    candidate[f"{side}_layers"].append(row)
        if candidate.get('expression', {}).get('text'):
            candidate['expression']['in_sync'] = False
        cleaned = replace(cleaned, design=validate_design(candidate), merged_ids={})
    return cleaned, float(threshold)
