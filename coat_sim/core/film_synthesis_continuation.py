"""Small durable continuation contract for the existing hybrid search.

Deterministic source streams resume by cursor without repeating physical work.
The existing archive supplies real designs, freshly scored on resume. No saved
score or audit is accepted as new evidence. Local solvers restart at phase
boundaries; the structural frontier, pending proposals and rotation seed persist.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

from .film_synthesis_archive import physical_design_hash
from .film_synthesis_surfaces import selected_rows, selected_sides, validate_candidate_environment

SEARCH_STATE_KIND = 'coat_sim.film_synthesis_search_state'
SEARCH_STATE_VERSION = 1
MAX_SEARCH_STATE_BYTES = 64 * 1024 * 1024
SOURCE_NAMES = ('user_seed', 'homogeneous', 'multispectral', 'reflector',
                'matching', 'ar_cluster', 'bandpass', 'dual_hr')


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False, ensure_ascii=False).encode('utf-8')
    # The existing atomic writers use JSON's default separator spaces. This
    # conservative bound includes those spaces plus the sealed digest field;
    # punctuation inside strings only makes the admission more conservative.
    output_bound = len(encoded) + encoded.count(b',') + encoded.count(b':')
    if output_bound > MAX_SEARCH_STATE_BYTES - 128:
        raise ValueError('search checkpoint exceeds 64 MiB JSON transport limit; previous file is retained')
    return hashlib.sha256(encoded).hexdigest()


def load_search_state(path):
    """Bounded JSON only, using the scheduler's existing bounded-reader pattern."""
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_SEARCH_STATE_BYTES + 1)
    if len(data) > MAX_SEARCH_STATE_BYTES:
        raise ValueError('search checkpoint exceeds 64 MiB JSON transport limit')
    try:
        return json.loads(data.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('search checkpoint JSON is incomplete or invalid; original file was not changed') from exc


def search_fingerprint(request):
    """Frozen physics/search semantics; editor text, seeds and per-run budgets excluded."""
    data = asdict(request)
    data["selected_surfaces"] = selected_sides(request)
    for name in ('search_checkpoint', 'seed_designs', 'warm_start_seed_index', 'design_revision',
                 'max_cycles', 'max_evaluations', 'max_seconds', 'cpu_workers',
                 'forward_backend', 'forward_device', 'resource_guard_resolution',
                 'max_working_set_bytes', 'enable_thickness_reduction', 'enable_local_margin_refinement'):
        data.pop(name, None)
    data.pop('parallel_batch_size', None)
    if tuple(data.get('selected_surfaces', ('front',))) == ('front',):
        data.pop('selected_surfaces', None)
    data['environment'] = physical_design_hash(request.environment)
    if request.environment['substrate']['sides'] == 'double':
        data['fixed_surface_stacks'] = {side: deepcopy(request.environment[side + '_layers'])
            for side in ('front', 'back') if side not in selected_sides(request)}
    snapshot = request.frozen_material_snapshot or {}
    data['frozen_material_snapshot'] = snapshot.get('materials')
    return _digest(data)


def seal_search_state(state):
    value = deepcopy(state)
    value.pop('sha256', None)
    value['sha256'] = _digest(value)
    return value


def validate_search_state(request, supplied):
    if not isinstance(supplied, dict):
        raise ValueError('search checkpoint must be a JSON object')
    state = deepcopy(supplied)
    if (state.get('kind') != SEARCH_STATE_KIND or type(state.get('version')) is not int
            or state.get('version') != SEARCH_STATE_VERSION):
        raise ValueError('unsupported search checkpoint kind/version; original state retained; explicitly rebuild')
    required = {'kind', 'version', 'fingerprint', 'cursors', 'source_cursor', 'rotations',
                'tried', 'pending', 'pool', 'feedback', 'archive_rotation', 'archive_total_admitted', 'sha256'}
    if required - set(state) or set(state) - required - {'active_sources', 'random_state'}:
        raise ValueError('incomplete or unknown search checkpoint fields; original state retained')
    digest = state.pop('sha256', None)
    if (state.get('kind') != SEARCH_STATE_KIND or state.get('version') != SEARCH_STATE_VERSION
            or digest != _digest(state)):
        raise ValueError('unsupported or damaged search checkpoint')
    if state.get('fingerprint') != search_fingerprint(request):
        raise ValueError('search checkpoint target/material/constraint/search fingerprint changed; '
                         'explicitly rebuild with search_checkpoint=None; old scores are never reused')
    if request.search_strategy != 'hybrid':
        raise ValueError('structural continuation requires hybrid search')
    cursors = state.get('cursors')
    if (not isinstance(cursors, dict) or set(cursors) != set(SOURCE_NAMES)
            or any(type(v) is not int or v < 0 for v in cursors.values())
            or any(type(state.get(k)) is not int or state[k] < 0
                   for k in ('source_cursor', 'rotations', 'archive_rotation', 'archive_total_admitted'))):
        raise ValueError('invalid search checkpoint cursors')
    if (not isinstance(state.get('tried'), list)
            or any(not isinstance(s, str) or len(s) != 64 or any(c not in '0123456789abcdef' for c in s) for s in state['tried'])
            or not isinstance(state.get('pending'), list) or not isinstance(state.get('pool'), list)):
        raise ValueError('invalid search checkpoint candidate state')
    active = state.get('active_sources', list(SOURCE_NAMES))
    if (not isinstance(active, list) or any(not isinstance(n, str) for n in active)
            or len(active) != len(set(active)) or any(n not in SOURCE_NAMES for n in active)):
        raise ValueError('invalid checkpoint active source streams')
    if active != [n for n in SOURCE_NAMES if n in active]:
        raise ValueError('checkpoint active source ordering changed')
    if len(state['pool']) > request.archive_capacity + 1:
        raise ValueError('checkpoint design pool exceeds frozen archive plus current seed capacity')
    if 'random_state' in state:
        import numpy as np
        from .film_synthesis_randomization import ALGORITHM_VERSION
        rng = state['random_state']
        if (not isinstance(rng, dict) or rng.get('root_seed') != request.structure_search_seed
                or rng.get('next_rotation_seed') != request.structure_search_seed + state['rotations'] + 1
                or rng.get('template_algorithm') != ALGORITHM_VERSION or rng.get('numpy_version') != np.__version__):
            raise ValueError('incompatible checkpoint random stream algorithm/seed; original state retained')
    from .film_auto_synthesis import _feasible
    from .film_design import validate_design
    available = {(m['id'], o['id']) for m in request.frozen_material_snapshot['materials'] for o in m['optical_sets']}
    for record in [*state['pending'], *state['pool']]:
        if (not isinstance(record, dict) or not isinstance(record.get('provenance'), dict)
                or 'design' not in record):
            raise ValueError('invalid checkpoint candidate record')
        design = validate_design(record['design'])
        if any((r['material_id'], r['optical_set_id']) not in available
               or r['thickness']['unit'] != 'nm' for r in selected_rows(request, design)):
            raise ValueError('checkpoint candidate is outside frozen material/nm closure')
        if not _feasible(request, design):
            raise ValueError('search checkpoint candidate violates frozen constraints')
        validate_candidate_environment(request, design)
    for record in state['pool']:
        if (type(record.get('insertion_index')) is not int or record['insertion_index'] < 1
                or not isinstance(record.get('operator'), str)
                or (record.get('parent_hash') is not None and not isinstance(record['parent_hash'], str))):
            raise ValueError('invalid checkpoint archive identity')
    from .film_synthesis_feedback import SparseFeedbackRegistry
    SparseFeedbackRegistry(request).restore_state(state['feedback'])
    state['sha256'] = digest
    return state


def continue_synthesis_request(request, checkpoint, *, current_design=None, **budget):
    """Minimal UI/CLI interface. Viewing/saving leaves checkpoint identity intact.

    An edited current design becomes the first freshly evaluated seed; compatible
    source cursors and archive designs survive. Changed physics requires an
    explicit rebuild, never a silent reuse of old scores. New queue limits apply
    only to this invocation. No editor session is mutated by this helper.
    """
    if set(budget) - {'max_cycles', 'max_evaluations', 'max_seconds', 'design_revision'}:
        raise ValueError('continuation accepts only per-run budgets and design_revision')
    validate_search_state(request, checkpoint)
    seeds = request.seed_designs
    seed_index = request.warm_start_seed_index
    if current_design is not None:
        seeds = (deepcopy(current_design), *seeds)
        seed_index = 0
    updated = replace(request, search_checkpoint=deepcopy(checkpoint), seed_designs=seeds,
                      warm_start_seed_index=seed_index, **budget)
    from .film_auto_synthesis import freeze_film_synthesis_request
    updated = freeze_film_synthesis_request(None, updated)
    validate_search_state(updated, checkpoint)
    return updated
