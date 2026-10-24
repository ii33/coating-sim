"""Public source-model jobs freeze the user's four windows without answer seeds."""
import json
import math
from pathlib import Path

import pytest

from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload
from coat_sim.core.film_synthesis_jobs import canonical_parameters

pytestmark = pytest.mark.algorithm
ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / 'benchmarks/film_synthesis/20261022/multispectral-v1/four-window-models'


@pytest.mark.parametrize('branch', ['author_export', 'supplier_ybf3'])
def test_historical_four_window_evidence_is_unchanged_and_old_jobs_are_rejected(branch):
    payload = json.loads((MODEL_DIR / (branch+'-request.json')).read_text(encoding='utf8'))
    # These public scientific inputs retain their original schema and values.
    # Current requests must be authored explicitly; no migration is performed.
    assert payload['schema_version'] == 1
    request = payload['request']
    assert not payload['physical_structures'] and not request['seed_designs']
    environment = request['environment']
    assert not environment['front_layers'] and not environment['back_layers']
    assert environment['substrate']['sides'] == 'single'
    from coat_sim.core.film_design_service import automatic_substrate_handling
    handling = automatic_substrate_handling(environment, 12000.)
    assert environment['substrate']['mode'] == 'auto'
    assert handling['resolved_mode'] == 'semi_infinite' and handling['exit_medium_ignored']
    assert 'zns_bulk' in environment['substrate']['material_id']
    assert environment['use_k'] and not environment['assume_lossless']
    objectives = request['objectives']
    assert [(o['wavelength_min_nm'],o['wavelength_max_nm'],o['wavelength_samples']) for o in objectives] == [
        (580.,920.,341),(1044.,1084.,41),(3000.,5000.,2001),(7000.,12000.,5001)]
    assert all(o['angles_deg'] == list(range(39,52)) and o['polarization'] == 'unpolarized'
               for o in objectives)
    assert [o['lower_bound'] for o in objectives[:2]] == [.99,.99]
    assert all(o['upper_bound'] == math.nextafter(.03,-math.inf) < .03 for o in objectives[2:])
    assert request['acceptable_normalized_error'] == 0
    assert request['max_seconds'] == 300 and request['max_evaluations'] == 6000
    assert request['max_layers'] == 64  # Original exploratory protocol.
    snapshot = request['frozen_material_snapshot']
    assert snapshot is not None
    assert all(o['k'] is not None for m in snapshot['materials'] for o in m['optical_sets'])
    with pytest.raises(ValueError, match='unsupported synthesis request schema_version'):
        film_synthesis_request_from_payload(payload)
    with pytest.raises(ValueError, match='unsupported synthesis request schema_version'):
        canonical_parameters(None, {'payload':payload})


def test_public_runner_verifies_base_source_and_reserves_only_new_temp_directory(tmp_path, monkeypatch):
    from tools.run_film_synthesis_multispectral import _load_inputs, _reserve_output, PROTOCOL_RELATIVE
    monkeypatch.setattr('tools.run_film_synthesis_multispectral.tempfile.gettempdir', lambda: str(tmp_path))
    _, _, _, case, rows, sha = _load_inputs(ROOT / PROTOCOL_RELATIVE, 'three_ar_hr_ar', 0)
    assert sha == 'f9865f384892b80688a5493e4310f128b102bbbf64fecbe62ed29d0f4f3214d3'
    assert rows and case['max_seconds'] == 90
    _reserve_output(tmp_path / 'new')
    with pytest.raises(FileExistsError):
        _reserve_output(tmp_path / 'new')
    with pytest.raises(ValueError, match='Temp'):
        _reserve_output(ROOT / 'do-not-create')
