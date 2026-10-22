"""Public source-model jobs freeze the user's four windows without answer seeds."""
import json
import math
from pathlib import Path

import pytest

from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload
from coat_sim.core.film_synthesis_jobs import build_job_payload, canonical_parameters, deserialize_job_payload

pytestmark = pytest.mark.algorithm
ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / 'benchmarks/film_synthesis/20261022/multispectral-v1/four-window-models'


@pytest.mark.parametrize('branch', ['author_export', 'supplier_ybf3'])
def test_four_window_jobs_keep_original_nk_and_strict_upper_boundary(branch):
    payload = json.loads((MODEL_DIR / (branch+'-request.json')).read_text(encoding='utf8'))
    _, request, structures = film_synthesis_request_from_payload(payload)
    assert not structures and not request.seed_designs
    assert not request.environment['front_layers'] and not request.environment['back_layers']
    assert request.environment['substrate']['sides'] == 'single'
    from coat_sim.core.film_design_service import automatic_substrate_handling
    handling = automatic_substrate_handling(request.environment, 12000.)
    assert request.environment['substrate']['mode'] == 'auto'
    assert handling['resolved_mode'] == 'semi_infinite' and handling['exit_medium_ignored']
    assert 'zns_bulk' in request.environment['substrate']['material_id']
    assert request.environment['use_k'] and not request.environment['assume_lossless']
    assert [(o.wavelength_min_nm,o.wavelength_max_nm,o.wavelength_samples) for o in request.objectives] == [
        (580.,920.,341),(1044.,1084.,41),(3000.,5000.,2001),(7000.,12000.,5001)]
    assert all(o.angles_deg == tuple(range(39,52)) and o.polarization == 'unpolarized'
               for o in request.objectives)
    assert [o.lower_bound for o in request.objectives[:2]] == [.99,.99]
    assert all(o.upper_bound == math.nextafter(.03,-math.inf) < .03 for o in request.objectives[2:])
    assert request.acceptable_normalized_error == 0
    assert request.max_seconds == 300 and request.max_evaluations == 6000
    assert request.max_layers == 64  # Explicit exploratory protocol, not a default/user restriction.
    snapshot = request.frozen_material_snapshot
    assert snapshot is not None
    assert all(o['k'] is not None for m in snapshot['materials'] for o in m['optical_sets'])
    job = build_job_payload(canonical_parameters(None, {'payload':payload}))
    _, decoded, queued_structures = deserialize_job_payload(job)
    assert not queued_structures and decoded == request


def test_public_runner_verifies_base_source_and_reserves_only_new_temp_directory(tmp_path):
    from tools.run_film_synthesis_multispectral import _load_inputs, _reserve_output, PROTOCOL_RELATIVE
    _, _, _, case, rows, sha = _load_inputs(ROOT / PROTOCOL_RELATIVE, 'three_ar_hr_ar', 0)
    assert sha == 'f9865f384892b80688a5493e4310f128b102bbbf64fecbe62ed29d0f4f3214d3'
    assert rows and case['max_seconds'] == 90
    _reserve_output(tmp_path / 'new')
    with pytest.raises(FileExistsError):
        _reserve_output(tmp_path / 'new')
    with pytest.raises(ValueError, match='Temp'):
        _reserve_output(ROOT / 'do-not-create')
