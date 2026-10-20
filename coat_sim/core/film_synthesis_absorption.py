"""Explicit synthesis-only treatment of absent extinction data.

Raw optical records are never patched. Known k and every source wavelength
boundary remain authoritative; only a completely absent k source can use zero.
"""
from __future__ import annotations

from typing import Any, Mapping
import math


def assumes_unknown_k_zero(request: Any) -> bool:
    value = request.environment.get('assume_lossless', False)
    if type(value) is not bool:
        raise ValueError('environment.assume_lossless must be an explicit boolean')
    return value


def sample_synthesis_optical(sampler: Any, wavelength_nm: float, *,
                             assume_unknown_k_zero: bool) -> tuple[float, float, Mapping[str, Any]]:
    """Read strict source domains, substituting only a missing k source."""
    if type(assume_unknown_k_zero) is not bool:
        raise ValueError('unknown-k assumption must be an explicit boolean')
    n, k, details = sampler(wavelength_nm, 'nm', extrapolation_policy='error', include_k=True)
    if not math.isfinite(float(n)) or float(n) <= 0:
        raise ValueError('finite positive source n is required')
    if not details.get('k_known'):
        if not assume_unknown_k_zero:
            raise ValueError('explicit finite passive n/k source data required; unknown k cannot be zero without assume_lossless=True')
        k = 0.0
    if k is None or not math.isfinite(float(k)) or float(k) < 0:
        raise ValueError('explicit finite passive source k is required')
    return float(n), float(k), details


def synthesis_absorption_assumption(request: Any, design: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Derive review facts from the frozen request, never from a live catalog."""
    from .film_design_service import active_material_references
    enabled = assumes_unknown_k_zero(request)
    references = {(row['material_id'], row['optical_set_id'])
                  for row in active_material_references(request.environment if design is None else design)}
    if design is None:
        references.update((row.material_id, row.optical_set_id) for row in request.candidate_materials)
        references.update((row['material_id'], row['optical_set_id'])
            for seed in request.seed_designs for row in active_material_references(seed))
    return _assumption_for_references(enabled, request.frozen_material_snapshot or {}, references,
                                      active_only=design is not None)


def design_absorption_assumption(design: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    from .film_design_service import active_material_references
    enabled = design.get('assume_lossless', False)
    if type(enabled) is not bool:
        raise ValueError('design.assume_lossless must be an explicit boolean')
    refs = {(row['material_id'], row['optical_set_id']) for row in active_material_references(design)}
    return _assumption_for_references(enabled, snapshot, refs, active_only=True)


def _assumption_for_references(enabled, snapshot, references, *, active_only):
    materials = {row['id']: row for row in snapshot.get('materials', ())}
    affected = []
    if enabled:
        for material_id, optical_set_id in sorted(references):
            material = materials.get(material_id, {})
            optical = next((row for row in material.get('optical_sets', ())
                            if row.get('id') == optical_set_id), None)
            if optical is None:
                continue  # Normal freeze/audit validation reports missing data.
            known = ((optical.get('kind') == 'tabulated' and optical.get('k') is not None)
                     or optical.get('k_data') is not None)
            if not known:
                affected.append({'material_id': material_id, 'optical_set_id': optical_set_id,
                                 'reason': 'no_k_source', 'assumed_k': 0.0})
    return {'assume_unknown_k_zero': enabled, 'policy': 'unknown_k_zero' if enabled else 'strict',
            'scope': 'candidate_active_materials' if active_only else 'request_material_pool',
            'affected_materials': affected,
            'used_affected_materials': affected if active_only else [],
            'conditional_on_assumption': bool(affected), 'known_k_preserved': True,
            'source_records_unchanged': True, 'wavelength_extrapolation': 'error',
            'measured_absorption_validated': False,
            'claim': ('Finite sampled R/T model evidence conditional on explicit unknown-k=0; '
                      'not measured absorption or a continuous certificate.' if affected else
                      'Finite sampled R/T model evidence using source k; not an experimental certificate.')}


def annotate_absorption_assumption(design: Mapping[str, Any],
                                   assumption: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a human-readable assumption without truncating user text.

    Only active references are annotated. The existing optional summary is the
    overflow location when the 20,000-character note cannot contain the marker.
    """
    from copy import deepcopy
    from .film_design import validate_design

    annotated = validate_design(deepcopy(dict(design)))
    enabled = assumption['assume_unknown_k_zero']
    if type(enabled) is not bool or annotated['assume_lossless'] != enabled:
        raise ValueError('candidate assume_lossless must match the absorption policy')
    if not annotated['use_k']:
        raise ValueError('absorption assumptions require use_k=True to preserve known k')
    references = sorted({(row['material_id'], row['optical_set_id'])
                         for row in assumption['used_affected_materials']})
    if not references:
        return annotated
    if not enabled:
        raise ValueError('strict candidates cannot contain unknown-k assumptions')
    marker = ('显式无损假设（仅未知 k 按 0 计算）：'
              + ', '.join(material + '/' + optical for material, optical in references)
              + '；保留已知 k；此结果不是实测吸收验证。')
    if marker in annotated['note'] or marker in annotated.get('summary', ''):
        return annotated
    for field in ('note', 'summary'):
        original = annotated.get(field, '')
        combined = original + ('\n' if original else '') + marker
        if len(combined) <= 20000:
            annotated[field] = combined
            return validate_design(annotated)
    raise ValueError('unknown-k assumption annotation cannot fit in note or summary; '
                     'shorten either field explicitly (existing text was not truncated)')


def annotate_synthesis_design(request: Any, design: Mapping[str, Any]) -> dict[str, Any]:
    """Return a policy-consistent, annotated candidate using frozen active refs."""
    return annotate_absorption_assumption(design, synthesis_absorption_assumption(request, design))
