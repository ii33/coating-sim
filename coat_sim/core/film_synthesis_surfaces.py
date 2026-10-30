"""Selection algebra only; never changes illumination or optical geometry.

Rows stay in their real substrate-outward side arrays. Flattening is exclusively
an ordered optimizer variable view, not a transfer-matrix stack construction.
"""
from copy import deepcopy
from collections.abc import Mapping


def selected_sides(request):
    raw = (request if isinstance(request, (tuple, list)) else
           request.get('selected_surfaces', ('front',)) if isinstance(request, Mapping) else
           getattr(request, 'selected_surfaces', ('front',)))
    if (not isinstance(raw, (tuple, list)) or not raw
            or any(side not in ('front', 'back') for side in raw)
            or len(set(raw)) != len(raw)):
        raise ValueError('selected_surfaces must be an ordered nonempty unique sequence of front/back')
    return tuple(side for side in ('front', 'back') if side in raw)


def selected_row_sites(request, design):
    return ((side, row) for side in selected_sides(request) for row in design[side + '_layers'])


def selected_rows(request, design):
    return [row for _side, row in selected_row_sites(request, design)]


def selected_layer_count(request, design):
    return sum(len(design[side + '_layers']) for side in selected_sides(request))


def set_selected_rows(request, design, rows):
    """Replace/filter existing active rows without crossing a substrate boundary.

    New topology must be introduced by side-aware construction operators, not
    by concatenating optimizer variables across the substrate.
    """
    ownership = {row['id']: side for side, row in selected_row_sites(request, design)}
    output: dict[str, list[dict]] = {side: [] for side in selected_sides(request)}
    for row in rows:
        if row['id'] not in ownership:
            raise ValueError('new synthesis rows require an explicit physical side')
        output[ownership[row['id']]].append(row)
    for side, side_rows in output.items():
        design[side + '_layers'] = side_rows


def synthesis_environment(request, design):
    result = deepcopy(design)
    for side in selected_sides(request):
        result[side + '_layers'] = []
    return result


def validate_candidate_environment(request, design):
    """Require the actual optical environment and byte-equivalent fixed rows."""
    reference = request.environment
    if 'back' in selected_sides(request) and reference['substrate']['sides'] != 'double':
        raise ValueError('back synthesis requires a double-sided substrate')
    keys = ('substrate', 'incident_medium', 'exit_medium', 'first_surface',
            'layer_order', 'reference_wavelength_nm', 'use_k', 'assume_lossless')
    if any(design.get(key) != reference.get(key) for key in keys):
        raise ValueError('synthesis candidate changed its physical environment')
    for side in ('front', 'back'):
        if side not in selected_sides(request) and design[side + '_layers'] != reference[side + '_layers']:
            raise ValueError('synthesis candidate changed an unselected surface stack')


def surface_interfaces(request, design):
    return ((side, interface) for side in selected_sides(request)
            for interface in range(len(design[side + '_layers']) + 1))


def geometry_template_request(request):
    """Single-front *unscored construction* input, never a physical candidate.

    Existing optical-thickness heuristics construct substrate-outward rows. For
    multiple selected faces their explicit layer/total caps are shared equally;
    subsequent real local/global operators may redistribute all active rows.
    This construction convention makes no separate-face objective assumption.
    """
    from dataclasses import replace
    sides = selected_sides(request)
    env = request.environment
    if (sides == ('front',) and env['substrate']['sides'] == 'single'
            and env['first_surface'] == 'front'):
        return request
    template = deepcopy(env)
    template['front_layers'] = []
    template['back_layers'] = []
    template['substrate']['sides'] = 'single'
    template['first_surface'] = 'front'
    if sides == ('back',):
        template['incident_medium'], template['exit_medium'] = deepcopy(env['exit_medium']), deepcopy(env['incident_medium'])
    updates = dict(environment=template, selected_surfaces=('front',))
    if request.max_layers is not None:
        updates['max_layers'] = request.max_layers // len(sides)
    if request.max_total_thickness_nm is not None:
        updates['max_total_thickness_nm'] = request.max_total_thickness_nm / len(sides)
    return replace(request, **updates)


def surface_template_eligibility(function):
    from functools import wraps
    @wraps(function)
    def eligible(request, *args, **kwargs):
        return function(geometry_template_request(request), *args, **kwargs)
    return eligible


def surface_template_proposals(function):
    """Lift unscored row templates into one actual full-coating candidate."""
    from functools import wraps
    from dataclasses import replace
    @wraps(function)
    def proposals(request, *args, **kwargs):
        template_request = geometry_template_request(request)
        if template_request is request:
            yield from function(request, *args, **kwargs)
            return
        sides = selected_sides(request)
        if template_request.max_layers == 0:
            return
        for proposal in function(template_request, *args, **kwargs):
            template_rows = proposal.design['front_layers']
            design = deepcopy(request.environment)
            metadata = deepcopy(proposal.provenance)
            count = len(template_rows)
            slots, clusters = [], []
            edges: list[int] = []
            groups: list[tuple[int, ...]] = []
            occupied = {row['id'] for side in ('front', 'back') for row in design[side + '_layers']}
            for index, side in enumerate(sides):
                rows = deepcopy(template_rows)
                for row in rows:
                    stem = row['id'][:110] + '-' + side
                    identifier, serial = stem, 0
                    while identifier in occupied:
                        serial += 1
                        identifier = stem + '-' + str(serial)
                    row['id'] = identifier
                    occupied.add(identifier)
                design[side + '_layers'] = rows
                for slot, row in zip(metadata.get('layer_slots', ()), rows, strict=False):
                    slots.append({**deepcopy(slot), 'id': row['id']})
                offset = index * count
                for cluster in metadata.get('clusters', ()):
                    lifted = deepcopy(cluster)
                    for key in ('high_indices', 'low_indices'):
                        lifted[key] = [i + offset for i in cluster[key]]
                    clusters.append(lifted)
                edges.extend(i + offset for i in metadata.get('boundary_layer_indices', ()))
                groups.extend(tuple(i + offset for i in group) for group in metadata.get('symmetry_groups', ()))
            metadata['selected_surfaces'] = sides
            metadata['template_construction_only'] = True
            metadata['layer_count'] = count * len(sides)
            metadata['template_layers_per_surface'] = count
            if slots:
                metadata.update(layer_slots=slots, clusters=clusters, boundary_layer_indices=tuple(edges))
                metadata['cluster_count'] = len(clusters)
                metadata['pair_counts'] = tuple(metadata['pair_counts']) * len(sides)
                from .film_synthesis_ar_clusters import _layout
                metadata['parameter_coordinates'] = tuple(_layout(clusters, edges))
                for key in ('proposal_total_phase_budget_nm', 'requested_total_phase_budget_nm',
                            'core_phase_budget_nm', 'edge_phase_budget_nm'):
                    if key in metadata:
                        metadata[key] *= len(sides)
            if groups:
                metadata['symmetry_groups'] = tuple(groups)
            yield replace(proposal, design=design, provenance=metadata)
    return proposals
