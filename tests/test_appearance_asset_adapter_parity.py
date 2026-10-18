"""Offline real-package regression for the single headless appearance adapter."""
from copy import deepcopy
import hashlib
import json
from threading import Event
from unittest.mock import patch
import zipfile

import pytest

from coat_sim.appearance import process_adapter
from coat_sim.core.appearance_compute import _normalize_public_request
from coat_sim.core.appearance_freeze_worker import build_frozen_envelope
from coat_sim.core import optical_constants
from coat_sim.ui import appearance_worker
from tests.test_appearance_compute_entrypoints import _render_inputs, _write_synthetic_v7_asset


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_member(path, name, change):
    """Change a synthetic package and refresh its genuine member integrity index."""
    with zipfile.ZipFile(path) as archive:
        members = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    data = json.loads(members[name])
    change(data)
    members[name] = json.dumps(data).encode()
    manifest = json.loads(members['manifest.json'])
    row = next(row for row in manifest['entries'] if row['path'] == name)
    row.update(size=len(members[name]), sha256=hashlib.sha256(members[name]).hexdigest())
    members['manifest.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for member, content in members.items():
            archive.writestr(member, content)


def _calls(root, asset, profile, digest, inputs, cancel=None):
    common = dict(expected_sha256=digest, geometry_thickness_mm=None, cancel_event=cancel)
    request = _normalize_public_request(root, {
        'asset_path': str(asset), 'asset_sha256': digest, 'render_inputs': inputs}, verify_asset=False)
    return [
        lambda: process_adapter.build_job_from_asset(profile, asset, root, inputs['wavelength_nm'], inputs, **common),
        lambda: appearance_worker._build_job_from_verified_asset(profile, str(asset), str(root), inputs['wavelength_nm'], inputs, **common),
        lambda: build_frozen_envelope(root, request, cancel_event=cancel)['job'],
    ]


@pytest.mark.parametrize('circle', [False, True])
def test_real_asset_job_parity_and_no_global_evaluator_patch(tmp_path, circle):
    asset = _write_synthetic_v7_asset(tmp_path, circle=circle)
    digest = _digest(asset)
    context = process_adapter.appearance_context_from_asset(asset, tmp_path, digest)
    assert appearance_worker._appearance_context_from_asset(asset, tmp_path, digest) == context
    inputs = _render_inputs()
    if circle:
        inputs['circle_boundary_sampling'] = 'captured_node_mean_with_local_nearest_fallback'
    original = deepcopy((context, inputs))
    evaluator = optical_constants.evaluate_optical_set
    scalar_calls = []

    def scalar_only(record, wavelength, unit, **kwargs):
        assert optical_constants.evaluate_optical_set is scalar_only
        assert isinstance(wavelength, float)
        scalar_calls.append(wavelength)
        return evaluator(record, wavelength, unit, **kwargs)

    with patch.object(optical_constants, 'evaluate_optical_set', scalar_only):
        calls = _calls(tmp_path, asset, context['process_profile'], digest, inputs)
        jobs = [call() for call in calls[:2]]
        with patch.object(appearance_worker, '_build_job_from_verified_asset', side_effect=AssertionError('freeze must not depend on UI')), \
                patch.object(appearance_worker, '_appearance_context_from_asset', side_effect=AssertionError('freeze must not depend on UI')):
            jobs.append(calls[2]())
    assert scalar_calls
    assert jobs[0] == jobs[1] == jobs[2]
    assert jobs[0]['substrate']['bounds_mm'] == [-1.0, 1.0, -1.0, 1.0]
    assert jobs[0]['substrate']['outline_kind'] == ('circle' if circle else 'rectangle')
    assert jobs[0]['provenance']['process_asset_sha256'] == digest
    assert jobs[0]['substrate']['solid_thickness_mm'] == 1.0
    assert jobs[0]['substrate']['material']['n'] == [1.5, 1.5]
    layer = jobs[0]['front_layers'][0]
    assert layer['material']['n'] == [1.9, 1.9]
    assert layer['material']['k'] == [0.0, 0.0]
    assert layer['thickness_field']['thickness_nm'] == (
        [[None, 100.0, None], [100.0, 100.0, 100.0], [None, 100.0, None]]
        if circle else [[100.0, 100.0], [100.0, 100.0]])
    assert (context, inputs) == original
    assert optical_constants.evaluate_optical_set is evaluator


@pytest.mark.parametrize('fault', ['stale_hash', 'missing_manifest', 'invalid_manifest', 'missing_substrate'])
def test_real_asset_failure_parity(tmp_path, fault):
    asset = _write_synthetic_v7_asset(tmp_path)
    digest = _digest(asset)
    profile = process_adapter.appearance_context_from_asset(asset, tmp_path, digest)['process_profile']
    if fault == 'stale_hash':
        digest = '0' * 64
    elif fault == 'missing_manifest':
        _rewrite_member(asset, 'result-metadata.json', lambda data: data.pop('layer_field_manifest'))
        digest = _digest(asset)
    elif fault == 'invalid_manifest':
        _rewrite_member(asset, 'result-metadata.json', lambda data: data['layer_field_manifest'].update(field_units='mm'))
        digest = _digest(asset)
    else:
        _rewrite_member(asset, 'configuration.json', lambda data: data['settings'].pop('substrate_profile'))
        digest = _digest(asset)
    errors = []
    for call in _calls(tmp_path, asset, profile, digest, _render_inputs()):
        with pytest.raises(ValueError) as exc:
            call()
        errors.append((type(exc.value), str(exc.value)))
    # Freeze resolves context first, so missing substrate has a context-specific
    # diagnostic; stale hashes are rejected at public request normalization.
    # All paths fail closed; UI and domain have the exact contract.
    assert errors[0] == errors[1]
    if fault not in ('missing_substrate', 'stale_hash'):
        assert errors[0] == errors[2]


def test_precancelled_asset_rejected_by_all_entrypoints(tmp_path):
    asset = _write_synthetic_v7_asset(tmp_path)
    digest = _digest(asset)
    profile = process_adapter.appearance_context_from_asset(asset, tmp_path, digest)['process_profile']
    cancel = Event()
    cancel.set()
    for call in _calls(tmp_path, asset, profile, digest, _render_inputs(), cancel):
        with pytest.raises(InterruptedError):
            call()


def test_progress_and_cancellation_between_adapter_stages(tmp_path):
    asset = _write_synthetic_v7_asset(tmp_path)
    digest = _digest(asset)
    context = process_adapter.appearance_context_from_asset(asset, tmp_path, digest)
    cancel = Event()
    stages = []

    def progress(stage, message):
        stages.append(stage)
        assert message
        if stage == 'film_adapter':
            cancel.set()

    inputs = _render_inputs()
    with patch.object(process_adapter, 'build_job_from_v7', side_effect=AssertionError('must cancel before conversion')):
        with pytest.raises(InterruptedError):
            process_adapter.build_job_from_asset(
                context['process_profile'], asset, tmp_path, inputs['wavelength_nm'], inputs,
                expected_sha256=digest, cancel_event=cancel, progress_callback=progress)
    assert stages == ['field_load', 'asset_read', 'film_adapter']
