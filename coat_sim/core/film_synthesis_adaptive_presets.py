"""Parameterized apodized mirrors and unequal-mirror cavity proposals.

These are finite, target-derived search conventions, not performance bounds.
Construction never scores, changes source optics, or relaxes original targets.
The hybrid caller owns budgets, full original scoring and independent audit.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math

from .film_design import MAX_LAYERS
from .film_synthesis_global import (
    StructuralProposal, iter_bandpass_proposals, iter_reflector_proposals,
    reflector_objectives,
)
from .film_synthesis_surfaces import surface_template_proposals

ADAPTIVE_PRESET_POLICY = 'apodized_phase_and_unequal_mirror_v1'


def _admissible(request, rows):
    """Final real geometry must satisfy every explicit construction limit."""
    if not rows or len(rows) > min(MAX_LAYERS, MAX_LAYERS if request.max_layers is None else request.max_layers):
        return False
    ds = [float(row['thickness']['value']) for row in rows]
    return (all(math.isfinite(d) and d > 0 for d in ds)
        and (request.minimum_nonzero_thickness_nm is None or min(ds) >= request.minimum_nonzero_thickness_nm)
        and (request.max_layer_thickness_nm is None or max(ds) <= request.max_layer_thickness_nm)
        and (request.max_total_thickness_nm is None or math.fsum(ds) <= request.max_total_thickness_nm))


@surface_template_proposals
def iter_apodized_reflector_proposals(request, *, epoch=0,
        cancel_check=lambda: None, report=lambda event: None):
    """Smoothly vary QW pair phase duty, preserving each local pair's phase sum.

    Only the generic QW reflector generator supplies the basis. Removing pass
    windows here avoids enumerating its separate narrow-rejection family; this
    is an unscored construction view, never a replacement acceptance request.
    Actual n/k and angular phase were already used by that generator. With its
    quarter-wave pair, dH'=2*f*dH and dL'=2*(1-f)*dL preserve Re(qH)*dH+Re(qL)*dL, not the absorption optical depth.
    """
    if type(epoch) is not int or epoch < 0:
        raise ValueError('adaptive preset epoch must be a nonnegative integer')
    objectives = reflector_objectives(request)
    if not objectives or request.seed_designs:
        return
    basis_request = replace(request, objectives=objectives)
    source = iter_reflector_proposals(basis_request, epoch=epoch,
        cancel_check=cancel_check, report=report)
    try:
        for proposal in source:
            cancel_check()
            if proposal.provenance.get('family') not in ('periodic', 'chirped', 'multistack'):
                continue
            basis = proposal.design['front_layers']
            pairs = len(basis) // 2
            if pairs < 2 or len(basis) != 2 * pairs:
                continue
            high = tuple(proposal.provenance['high'])
            for edge_duty in (.1, .2):
                for inverted in (False, True):
                    design = deepcopy(proposal.design)
                    for index in range(pairs):
                        cancel_check()
                        z = index / (pairs - 1)
                        duty = edge_duty + (.5 - edge_duty) * math.sin(math.pi * z) ** 2
                        if inverted:
                            duty = 1 - duty
                        for row in design['front_layers'][2 * index:2 * index + 2]:
                            is_high = (row['material_id'], row['optical_set_id']) == high
                            row['thickness']['value'] *= 2 * (duty if is_high else 1 - duty)
                    rows = design['front_layers']
                    if not _admissible(request, rows):
                        report(dict(stage='structure_skipped', family='apodized_reflector',
                            reason='explicit thickness/layer constraints'))
                        continue
                    metadata = dict(deepcopy(proposal.provenance),
                        family='apodized_reflector', base_family=proposal.provenance['family'],
                        edge_high_phase_fraction=edge_duty, inverted_phase_duty=inverted,
                        phase_envelope='edge + (0.5-edge)*sin(pi*j/(pairs-1))**2',
                        construction_policy=ADAPTIVE_PRESET_POLICY,
                        acceptance_equivalence=False, search_convention=True)
                    yield StructuralProposal(design, metadata)
    finally:
        source.close()


@surface_template_proposals
def iter_asymmetric_cavity_proposals(request, *, epoch=0,
        cancel_check=lambda: None, report=lambda event: None):
    """Remove one outer mirror pair on either side of a coupled-cavity seed.

    Both exterior mirror choices are proposed, not chosen by a lossless mirror
    formula. Cavity phases and source optical constants remain unchanged. The
    reduced mirror can alter impedance coupling; improvement is not assumed.
    At least one pair remains. No inherited symmetry constraint is permitted.
    """
    if type(epoch) is not int or epoch < 0:
        raise ValueError('adaptive preset epoch must be a nonnegative integer')
    if request.seed_designs:
        return
    source = iter_bandpass_proposals(request, epoch=epoch,
        cancel_check=cancel_check, report=report)
    try:
        for proposal in source:
            cancel_check()
            pairs = int(proposal.provenance['mirror_pairs'])
            if pairs < 2:
                continue
            pad = int(bool(proposal.provenance['matching_quarters']))
            for reduced_side in ('substrate', 'incident'):
                cancel_check()
                design = deepcopy(proposal.design)
                rows = design['front_layers']
                start = pad if reduced_side == 'substrate' else len(rows) - pad - 2
                del rows[start:start + 2]
                if not _admissible(request, rows):
                    report(dict(stage='structure_skipped', family='asymmetric_cavity',
                        reason='explicit thickness/layer constraints'))
                    continue
                metadata = deepcopy(proposal.provenance)
                metadata.pop('symmetry_groups', None)
                metadata.update(family='asymmetric_cavity', base_family='coupled_cavity',
                    substrate_mirror_pairs=pairs - int(reduced_side == 'substrate'),
                    incident_mirror_pairs=pairs - int(reduced_side == 'incident'),
                    reduced_mirror_side=reduced_side,
                    construction_policy=ADAPTIVE_PRESET_POLICY,
                    acceptance_equivalence=False, search_convention=True)
                # mirror_pairs described the symmetric source, not the output.
                metadata['source_mirror_pairs'] = metadata.pop('mirror_pairs')
                yield StructuralProposal(design, metadata)
    finally:
        source.close()
