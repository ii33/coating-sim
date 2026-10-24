"""Independent loopback service and client roots; only owned processes."""
from __future__ import annotations
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from coat_sim.scheduler.remote_server import BlobStore, RemoteComputeService, ServiceError

REPO = Path(__file__).resolve().parents[1]
_CHILD = '''import sys, threading, faulthandler, time, json, os
from pathlib import Path
started = time.monotonic()
faulthandler.dump_traceback_later(15, repeat=True)
from coat_sim.scheduler.remote_server import RemoteComputeService
service = RemoteComputeService(sys.argv[1], host="127.0.0.1", port=int(sys.argv[2]),
    insecure_loopback=sys.argv[3] == "loopback", allow_probe=sys.argv[3] == "loopback",
    allow_insecure_http=sys.argv[3] == "authenticated-http",
    token="ephemeral-test-operator-token")
assert not any(k.startswith(("PySide", "PyQt", "coat_sim.ui")) for k in sys.modules)
Path(sys.argv[1], 'startup-' + sys.argv[4] + '.json').write_text(json.dumps({
    'nonce': sys.argv[4], 'pid': os.getpid(), 'instance': service.scheduler.owner}), encoding='utf-8')
print(json.dumps({"stage":"startup_dependencies_ready", "elapsed_s":time.monotonic()-started,
                  "gui_modules_loaded":False}), flush=True)
def watch():
    sys.stdin.readline()
    service.scheduler.stopping.set()
threading.Thread(target=watch, daemon=True).start()
service.run()
'''


def _owned_endpoint(endpoint, startup, nonce):
    """Bind readiness to this child even when a Windows venv redirects Python."""
    return (startup.get('nonce') == nonce
            and type(startup.get('pid')) is int and startup['pid'] > 0
            and endpoint.get('pid') == startup['pid']
            and isinstance(startup.get('instance'), str) and bool(startup['instance'])
            and endpoint.get('instance') == startup['instance'])


@pytest.mark.parametrize('override', [
    {'nonce': 'old-launch'}, {'pid': 11}, {'instance': 'old-instance'},
    {'pid': True}, {'pid': 0}, {'instance': ''},
])
def test_owned_endpoint_rejects_unrelated_startup_records(override):
    endpoint = {'pid': 22, 'instance': 'this-instance'}
    startup = {'pid': 22, 'instance': 'this-instance', 'nonce': 'this-launch'}
    assert _owned_endpoint(endpoint, startup, 'this-launch')
    assert not _owned_endpoint(endpoint, startup | override, 'this-launch')


class OwnedService:
    def __init__(self, root, port=0, *, authenticated_http=False):
        self.root = root
        self.authenticated_http = authenticated_http
        root.mkdir(parents=True, exist_ok=True)
        self.log = (root / 'service-test.log').open('ab')
        env = dict(os.environ)
        env['TEMP'] = env['TMP'] = str(root)
        env['PYTHONUNBUFFERED'] = '1'
        env['PYTHONPATH'] = str(REPO)
        mode = 'authenticated-http' if authenticated_http else 'loopback'
        nonce = uuid4().hex
        startup_path = root / ('startup-' + nonce + '.json')
        self.process = subprocess.Popen([sys.executable, '-c', _CHILD, str(root), str(port), mode, nonce],
                                        cwd=root, stdin=subprocess.PIPE, stdout=self.log,
                                        stderr=self.log, env=env)
        self.record('process_created', pid=self.process.pid)
        endpoint = root / 'artifacts/runtime/scheduler/remote-endpoint.json'
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.close()
                raise AssertionError((root / 'service-test.log').read_text(errors='replace'))
            try:
                value = json.loads(endpoint.read_text(encoding='utf-8'))
                startup = json.loads(startup_path.read_text(encoding='utf-8'))
                if _owned_endpoint(value, startup, nonce):
                    self.pid = value['pid']
                    self.url = value['base_url']
                    self.instance = value['instance']
                    self.port = int(self.url.rsplit(':', 1)[1])
                    self.record('endpoint_ready', pid=value['pid'], launcher_pid=self.process.pid,
                                instance=value['instance'])
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.05)
        self.close()
        raise AssertionError('owned service start timeout')

    def record(self, stage, **values):
        record = {'stage': stage, 'time_utc': datetime.now(timezone.utc).isoformat(), **values}
        with (self.root / 'request-evidence.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps(record, ensure_ascii=False)+'\n')

    def close(self):
        self.record('shutdown_requested', pid=self.process.pid,
                    instance=getattr(self, 'instance', None))
        if self.process.poll() is None:
            try:
                self.process.stdin.write(b'stop\n')
                self.process.stdin.flush()
                self.process.wait(timeout=40)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=10)
        self.process.stdin.close()
        self.record('process_exited', pid=self.process.pid, exit_code=self.process.returncode)
        self.log.close()


@contextmanager
def running(root, port=0, *, authenticated_http=False):
    service = OwnedService(root, port, authenticated_http=authenticated_http)
    try:
        yield service
    finally:
        service.close()


def client(service, root, *, token='ephemeral-test-operator-token'):
    from coat_sim.scheduler.remote_client import make_remote_client
    cli = make_remote_client(service.url, root, token=token,
        allow_insecure_http=service.authenticated_http,
        allow_loopback_http=not service.authenticated_http)
    original = cli.transport._open
    def traced(request, **options):
        route = urlsplit(request.full_url).path
        service.record('http_request_start', method=request.get_method(), route=route)
        try:
            response = original(request, **options)
            service.record('http_response', method=request.get_method(), route=route,
                           status=response.status)
            return response
        except BaseException as exc:
            import traceback
            trace = traceback.format_exc().replace('ephemeral-test-operator-token', '<redacted>')
            service.record('http_request_error', method=request.get_method(), route=route,
                           code=getattr(exc, 'code', None), traceback=trace)
            raise
    cli.transport._open = traced
    cli._evidence_service = service
    return cli


def probe(delay=0, size=0):
    return {'protocol_version': 1, 'executor': 'probe',
            'parameters': {'delay_s': delay, 'payload_bytes': size}, 'inputs': {}}


def wait_state(cli, identifier, states, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cli.status(identifier)
        if value['state'] in states:
            cli._evidence_service.record('job_state_verified', job_id=identifier,
                state=value['state'], error_code=value.get('error_code'),
                artifacts=(value.get('result') or {}).get('result_artifacts', []))
            return value
        time.sleep(.05)
    raise AssertionError(f'{identifier}: {value}')


def ray_request():
    return {'schema': 'coat_sim.geometric_raytrace', 'schema_version': 1,
            'units': {'length': 'mm', 'wavelength': 'nm', 'index': 'n+i*k'},
            'polarization': 'unpolarized',
            'samples': [{'sample_id': 'r', 'origin_xz_mm': [0., 0.],
                         'direction_xz': [0., 1.], 'weight': 1.}],
            'spectrum': [{'wavelength_nm': 550., 'weight': 1.}],
            'surfaces': [{'surface_id': 's', 'geometry': {'type': 'plane', 'z_mm': 1.},
                          'aperture_radius_mm': 5., 'medium_before': [{'n': 1., 'k': 0.}],
                          'medium_after': [{'n': 1.5, 'k': 0.}]}]}


def frozen_process(root):
    from coat_sim.core.api import CoatService
    from coat_sim.scheduler.jobs import freeze_spec
    documents = {
        'source.csrc': {'schema_version': 2, 'kind': 'point', 'device_type': 'custom', 'source_exponent': 1.},
        'substrate.csub': {'schema_version': 1, 'name': 'Synthetic substrate',
                           'outline': {'kind': 'circle', 'radius_mm': 10.}, 'surface': {'kind': 'flat'}},
        'machine.cmac': {'schema_version': 5, 'carrier_x_mm': 0., 'carrier_y_mm': 0.,
                         'planet_offset_mm': 0., 'planet_tilt_deg': 0., 'substrate_z_mm': 100.,
                         'sun_teeth': 120, 'planet_teeth': 36, 'gear_module_mm': 0.,
                         'sources': [{'file': 'source.csrc', 'x_mm': 0., 'y_mm': 0., 'z_mm': 0.}],
                         'masks': [], 'monitors': []},
        'recipe.cproc': {'schema_version': 6, 'name': 'Synthetic remote recipe',
                         'machine_file': 'machine.cmac', 'substrate_file': 'substrate.csub',
                         'steps': [{'source_slot': 1, 'target_nm': .05, 'rate_nm_s': 1., 'target_mode': 'mean'}]},
    }
    root.mkdir(parents=True, exist_ok=True)
    for name, data in documents.items():
        (root / name).write_text(json.dumps(data), encoding='utf-8')
    service = CoatService(root)
    spec = freeze_spec(service, 'process_recipe', {'process_path': 'recipe.cproc', 'backend': 'CPU',
        'settings': {'grid_size': 21, 'duration_s': 1., 'time_steps': 20}, 'include_layer_fields': True})
    # Freeze is complete: the service machine must not read these original files.
    for name in documents:
        (root / name).unlink()
    return spec


def frozen_film(root):
    from copy import deepcopy
    from coat_sim.core.film_design_service import FilmDesignService
    from coat_sim.core.film_optimization import (FilmOptimizationRequest,
        FilmOptimizationObjective, FilmThicknessVariable)
    from coat_sim.core.film_optimization_job import freeze_job_payload
    from coat_sim.core.materials import default_material_profile
    rows = []
    for identifier, category, n in [('glass', 'substrate', 1.5), ('film', 'film', 1.23)]:
        row = default_material_profile('Synthetic '+identifier, id=identifier)
        row.update(category=category, optical_sets=[{'id': identifier+'-set', 'name': identifier,
            'kind': 'tabulated', 'notes': 'Synthetic remote fixture', 'source': 'test only',
            'wavelength_unit': 'nm', 'wavelength_min': 500., 'wavelength_max': 600.,
            'wavelengths': [500., 600.], 'n': [n, n], 'k': [0., 0.]}])
        rows.append(row)
    class SyntheticMaterials:
        def _records(self, material_ids=None):
            return deepcopy(rows)
        def get(self, identifier):
            return deepcopy(next(row for row in rows if row['id'] == identifier))
    service = FilmDesignService(root)
    service.materials = SyntheticMaterials()
    design = service.new_design()
    design['substrate'].update(material_id='glass', optical_set_id='glass-set')
    design['front_layers'] = [{'id': 'layer', 'material_id': 'film', 'optical_set_id': 'film-set',
                              'thickness': {'value': 100., 'unit': 'nm'}, 'note': 'synthetic'}]
    request = FilmOptimizationRequest(design_snapshot=design, design_revision=1,
        variables=(FilmThicknessVariable('front', 'layer', 80., 120.),),
        objectives=(FilmOptimizationObjective(500., 600., 3, pointwise=FilmObjectiveConstraint(mode="value", target_value=.1, lower_bound=None, upper_bound=None), angles_deg=(0.,)),),
        max_iterations=1, max_evaluations=4)
    payload = freeze_job_payload(service, request)
    return {'protocol_version': 1, 'executor': 'film_optimization', 'parameters': {'payload': payload}, 'inputs': {}}


def test_configuration_fails_closed_before_listening(tmp_path):
    with pytest.raises(ServiceError, match='tls_and_authentication_required'):
        RemoteComputeService(tmp_path, host='0.0.0.0')
    with pytest.raises(ServiceError, match='insecure_listening_requires_loopback'):
        RemoteComputeService(tmp_path, host='0.0.0.0', insecure_loopback=True)
    with pytest.raises(ServiceError, match='probe_requires'):
        RemoteComputeService(tmp_path, allow_probe=True)
    assert not (tmp_path / 'artifacts').exists()


def test_blob_integrity_quota_duplicate_and_partial_cleanup(tmp_path):
    store = BlobStore(tmp_path, 4, 4)
    digest = hashlib.sha256(b'abcd').hexdigest()
    assert store.receive(digest, 4, io.BytesIO(b'abcd')) == {'sha256': digest, 'bytes': 4}
    # A complete store must still accept idempotent upload retry.
    store.receive(digest, 4, io.BytesIO(b'abcd'))
    other = hashlib.sha256(b'abc').hexdigest()
    with pytest.raises(ServiceError, match='blob_quota_exceeded'):
        store.receive(other, 3, io.BytesIO(b'abc'))
    store2 = BlobStore(tmp_path / 'second', 4, 4)
    with pytest.raises(ServiceError, match='incomplete_upload'):
        store2.receive(digest, 4, io.BytesIO(b'ab'))
    with pytest.raises(ServiceError, match='blob_digest_mismatch'):
        store2.receive(digest, 4, io.BytesIO(b'zzzz'))
    assert list(store2.directory.iterdir()) == []
    assert store2.reserved == 0


def test_real_remote_disconnect_cancel_integrity_and_auth(tmp_path):
    with running(tmp_path / 'server') as service:
        cli = client(service, tmp_path / 'client-a')
        health = cli.ensure_started()
        assert health['pid'] == service.pid
        assert health['instance'] == service.instance
        assert 'python_script' not in cli.capabilities()['executors']
        denied = client(service, tmp_path / 'denied', token=None)
        with pytest.raises(ValueError, match='authentication'):
            denied.snapshot()
        job = cli.submit(probe(.2, 8*1024*1024), job_id='large')
        duplicate = cli.submit(probe(.2, 8*1024*1024), job_id='large')
        assert duplicate['job_id'] == job['job_id']
        with pytest.raises(ValueError, match='request_id_conflict'):
            cli.submit(probe(.2, 1), job_id='large')
        del cli
        resumed = client(service, tmp_path / 'client-b')
        final = resumed.wait_for_terminal('large', wait_s=.2)
        assert final['state'] == 'succeeded', final
        result = resumed.result('large')
        item = result['result_artifacts'][0]
        path = resumed.materialize_artifact(item['artifact_id'], item)
        assert path.is_relative_to(tmp_path / 'client-b')
        assert path.stat().st_size == 8*1024*1024
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
        service.record('artifact_verified', job_id='large', bytes=path.stat().st_size,
                       sha256=hashlib.sha256(path.read_bytes()).hexdigest(), artifact_id=item['artifact_id'])
        path.write_bytes(b'y' + path.read_bytes()[1:])
        repaired = resumed.materialize_artifact(item['artifact_id'], item)
        assert hashlib.sha256(repaired.read_bytes()).hexdigest() == item['sha256']
        active = resumed.submit(probe(20), job_id='active')
        wait_state(resumed, 'active', {'running'})
        queued = resumed.submit(probe(), job_id='queued')
        assert queued['state'] == 'queued'
        assert resumed.cancel('queued')['state'] == 'cancelled'
        assert resumed.cancel('active')['cancel_requested']
        assert wait_state(resumed, 'active', {'cancelled'})['state'] == 'cancelled'
        assert resumed.cancel('active')['state'] == 'cancelled'
        with pytest.raises(ValueError, match='disabled'):
            resumed.stop()
        text = json.dumps(resumed.snapshot())
        assert str(service.root) not in text
        assert not (tmp_path / 'client-b/artifacts/runtime/compute-tasks.sqlite3').exists()
        assert not (tmp_path / 'client-b/artifacts/runtime/scheduler/endpoint.json').exists()


def test_real_restart_preserves_queue_and_resets_events(tmp_path):
    service_root = tmp_path / 'server'
    with running(service_root) as service:
        cli = client(service, tmp_path / 'client-a')
        cli.submit(probe(20), job_id='interrupted')
        wait_state(cli, 'interrupted', {'running'})
        cli.submit(probe(.01), job_id='survives')
        cursor = cli.events()['sequence']
        port, previous = service.port, service.instance
    with running(service_root, port=port) as service:
        cli = client(service, tmp_path / 'client-b')
        assert cli.ensure_started()['instance'] != previous
        assert cli.status('interrupted')['state'] == 'failed'
        assert cli.status('interrupted')['error_code'] == 'scheduler_interrupted'
        assert cli.events(after_sequence=cursor)['reset']
        assert wait_state(cli, 'survives', {'succeeded', 'failed'})['state'] == 'succeeded'
        with sqlite3.connect(service_root / 'artifacts/runtime/compute-tasks.sqlite3') as db:
            assert db.execute('SELECT COUNT(*) FROM scheduler_specs').fetchone()[0] == 2
            assert db.execute('SELECT COUNT(*) FROM scheduler_events').fetchone()[0] > 0


def test_real_geometric_job_and_schema_checked_result(tmp_path):
    from coat_sim.scheduler.jobs import freeze_spec
    from coat_sim.core.geometric_raytrace_result import validate_geometric_raytrace_result
    with running(tmp_path / 'server') as service:
        cli = client(service, tmp_path / 'cache')
        spec = freeze_spec(None, 'geometric_raytrace', {'request': ray_request()})
        cli.submit(spec, job_id='ray')
        final = wait_state(cli, 'ray', {'succeeded', 'failed'})
        assert final['state'] == 'succeeded', final
        result = cli.result('ray')
        item = result['result_artifacts'][0]
        path = cli.materialize_artifact(item['artifact_id'], item)
        decoded = json.loads(path.read_bytes())
        validate_geometric_raytrace_result(decoded)
        assert decoded['spectra'][0]['reflectance'] == pytest.approx(.04)
        assert decoded['spectra'][0]['transmittance'] == pytest.approx(.96)


def test_real_authenticated_http_requires_both_opt_in_and_bearer(tmp_path):
    from coat_sim.scheduler.jobs import freeze_spec
    from coat_sim.scheduler.remote_client import RemoteTransport, RemoteServiceError
    from coat_sim.core.geometric_raytrace_result import validate_geometric_raytrace_result
    with running(tmp_path / 'server', authenticated_http=True) as service:
        with pytest.raises(ValueError, match='insecure_remote_http_forbidden'):
            RemoteTransport(service.url, token='ephemeral-test-operator-token')
        cli = client(service, tmp_path / 'cache')
        assert cli.ensure_started()['ready']
        capabilities = cli.transport.capabilities()
        assert capabilities['authentication'] == 'bearer_token'
        assert 'probe' not in capabilities['executors']
        unauthorized = client(service, tmp_path / 'wrong-token-cache', token='incorrect-test-token')
        with pytest.raises(RemoteServiceError) as error:
            unauthorized.ensure_started()
        assert error.value.http_status == 401
        assert error.value.code == 'authentication_required'
        # Bypass constructor policy deliberately to verify server-side missing auth.
        without_token = client(service, tmp_path / 'no-token-cache')
        without_token.transport.token = None
        with pytest.raises(RemoteServiceError) as error:
            without_token.ensure_started()
        assert error.value.http_status == 401
        spec = freeze_spec(None, 'geometric_raytrace', {'request': ray_request()})
        cli.submit(spec, job_id='authenticated-http-ray')
        final = wait_state(cli, 'authenticated-http-ray', {'succeeded', 'failed'})
        assert final['state'] == 'succeeded', final
        item = cli.result('authenticated-http-ray')['result_artifacts'][0]
        path = cli.materialize_artifact(item['artifact_id'], item)
        assert path.stat().st_size == item['bytes']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
        decoded = json.loads(path.read_bytes())
        validate_geometric_raytrace_result(decoded)
        assert decoded['spectra'][0]['reflectance'] == pytest.approx(.04)
        service.record('authenticated_http_verified', job_id='authenticated-http-ray',
                       artifact_sha256=item['sha256'], authentication='bearer_token')
    log = (tmp_path / 'server/service-test.log').read_text(encoding='utf-8', errors='replace')
    assert 'Bearer credentials and request data are unencrypted' in log
    assert 'ephemeral-test-operator-token' not in log


def test_real_portable_deposition_bundle_and_film_optimizer(tmp_path):
    from coat_sim.scheduler.jobs import freeze_spec
    from coat_sim.core.geometric_raytrace_result import validate_geometric_raytrace_result
    spec = frozen_process(tmp_path / 'client-inputs')
    film = frozen_film(tmp_path / 'film-inputs')
    with running(tmp_path / 'server-a') as service:
        cli = client(service, tmp_path / 'cache-a')
        cli.submit(spec, job_id='deposition')
        final = wait_state(cli, 'deposition', {'succeeded', 'failed'})
        assert final['state'] == 'succeeded', final
        bundle = next(item for item in cli.result('deposition')['result_artifacts'] if item['role'] == 'bundle')
        asset = cli.materialize_artifact(bundle['artifact_id'], bundle)
        from coat_sim.core.simulation_assets import read_simulation_asset
        asset_info = read_simulation_asset(asset, detail='full', include_arrays=True)
        step_id = asset_info['result_metadata']['layer_field_manifest']['fields'][0]['step_id']
        arrays = asset_info['arrays']
        x_index = next(i for i, x in enumerate(arrays['x']) if x == 0.)
        y_index = next(i for i, y in enumerate(arrays['y']) if y == 0.)
        expected_thickness = float(arrays['layer_fields'][step_id][y_index, x_index])
        assert expected_thickness > 0
    # Upload downloaded input to another service root; no shared filesystem IDs.
    with running(tmp_path / 'server-b') as service:
        cli = client(service, tmp_path / 'cache-b')
        descriptor = cli.transport.register_deposition_asset(asset)
        reference = {key: descriptor[key] for key in ('artifact_id', 'sha256')}
        request = ray_request()
        request['surfaces'][0]['coating'] = {
            'indices': [[{'n': 1., 'k': 0.}, {'n': 2., 'k': 0.}, {'n': 1.5, 'k': 0.}]],
            'layers': [{'layer_id': 'layer', 'deposition_step_id': step_id,
                        'global_to_grid_xy': {'rotation_rad': 0., 'translation_xy_mm': [0., 0.]}}]}
        cli.submit(freeze_spec(None, 'geometric_raytrace', {'request': request, 'deposition_artifact': reference}), job_id='ray-from-upload')
        final = wait_state(cli, 'ray-from-upload', {'succeeded', 'failed'})
        assert final['state'] == 'succeeded', final
        assert 'server-b' not in json.dumps(final)
        item = cli.result('ray-from-upload')['result_artifacts'][0]
        path = cli.materialize_artifact(item['artifact_id'], item)
        decoded = json.loads(path.read_bytes())
        validate_geometric_raytrace_result(decoded)
        event = decoded['paths'][0]['events'][0]
        assert event['sampled_layer_thickness_nm'] == pytest.approx([expected_thickness])
        assert decoded['spectra'][0]['missed_weight'] == 0
        from coat_sim.core.film_optics import coherent_rta
        expected = coherent_rta(550., [1.+0j, 2.+0j, 1.5+0j], [expected_thickness])
        for key, optical_key in zip(('reflectance', 'transmittance', 'absorptance'), ('R', 'T', 'A')):
            assert decoded['spectra'][0][key] == pytest.approx(expected[optical_key])
        service.record('portable_deposition_ray_verified', job_id='ray-from-upload',
            deposition_step_id=step_id, thickness_nm=expected_thickness,
            artifact_id=item['artifact_id'], result_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        cli.submit(film, job_id='film')
        final = wait_state(cli, 'film', {'succeeded', 'failed'})
        assert final['state'] == 'succeeded', final
        result = cli.result('film')
        assert result['kind'] == 'coat_sim.film_optimization_result'
        assert result['candidate']['quality']['value'] >= 0


def test_verified_cancelled_film_candidate_retains_remote_result_after_restart(tmp_path, monkeypatch):
    from threading import Event
    from coat_sim.core.film_design_service import FilmDesignService
    from coat_sim.scheduler.worker import _film_optimization
    from coat_sim.scheduler.jobs import canonical
    from coat_sim.scheduler.io import write_text
    from coat_sim.scheduler import result_capability
    payload = frozen_film(tmp_path / 'synthetic')['parameters']['payload']
    cancel = Event()
    def after_candidate(progress):
        if progress.get('status') == 'running' and progress.get('evaluations') == 1:
            cancel.set()
    service = RemoteComputeService(tmp_path / 'server', insecure_loopback=True)
    try:
        spec = {'protocol_version': 1, 'executor': 'film_optimization', 'parameters': {'payload': payload}, 'inputs': {}}
        data = canonical(spec).encode()
        blob = service.blobs.receive(hashlib.sha256(data).hexdigest(), len(data), io.BytesIO(data))
        service._submit({'api_version': 1, 'executor_schema_version': 1, 'job_id': 'candidate', 'spec_blob': blob})
        checkpoint = service.scheduler.runtime / 'jobs/candidate/best-candidate.json'
        result = _film_optimization(FilmDesignService(tmp_path / 'worker'), {'payload': payload},
            cancel, after_candidate, checkpoint)
        assert result['state'] == 'cancelled'
        assert result['partial_result']['candidate_verified'] is True
        checkpoint_bytes = checkpoint.read_bytes()
        persisted = json.loads(checkpoint_bytes)['result']
        assert persisted['candidate'] == result['candidate']
        def no_replay(*_args, **_kwargs):
            pytest.fail('active cancellation and stored terminal-result reads must not replay a checkpoint')

        monkeypatch.setattr(result_capability, 'load_best_candidate_checkpoint', no_replay)
        monkeypatch.setattr(result_capability, 'validate_film_optimization_result', no_replay)
        # The real current worker reply is authoritative. A valid historical
        # checkpoint alone must not manufacture an active-cancel result.
        unavailable = service.scheduler._cancelled_outcome('candidate', {})
        assert unavailable['result'] is None
        assert 'without cancellation replay' in unavailable['result_unavailable_reason']
        previous = {'state': 'cancelled', 'result': result}
        assert service.scheduler._cancelled_outcome('candidate', previous)['result'] is None
        # This fixture runs the worker directly; reproduce start_job's exact
        # frozen-spec write before accepting its bound terminal reply.
        write_text(checkpoint.parent / 'spec.json', canonical(spec))
        assert result_capability.is_film_optimization_result(result)
        assert service.scheduler._bound_film_reply('candidate', result)
        foreign = json.loads(json.dumps(result))
        foreign['request']['design_revision'] += 1
        foreign['candidate']['design_revision'] += 1
        assert result_capability.is_film_optimization_result(foreign)
        assert service.scheduler._cancelled_outcome('candidate',
            {'state': 'cancelled', 'result': foreign})['result'] is None
        outcome = service.scheduler._cancelled_outcome('candidate', previous)
        expected = json.loads(json.dumps(result))
        marker = {'source': 'worker_completed_reply', 'replay_performed': False, 'candidate_available': True}
        expected.setdefault('execution', {})['cancel_snapshot'] = marker
        assert canonical(outcome['result']) == canonical(expected)
        assert outcome['result']['candidate'] == persisted['candidate']
        assert checkpoint.read_bytes() == checkpoint_bytes
        service.scheduler._commit('transition', 'candidate', **outcome)
        with service.scheduler.store.queue.connection() as db:
            stored = db.execute('SELECT payload FROM queue_jobs WHERE job_id=?', ('candidate',)).fetchone()
        assert stored is not None
        stored_job = json.loads(stored['payload'])
        assert stored_job['state'] == 'cancelled'
        assert canonical(stored_job['result']) == canonical(outcome['result'])
    finally:
        service.scheduler.guard.close()
        service.scheduler.lock.close()
    restored = RemoteComputeService(tmp_path / 'server', insecure_loopback=True)
    try:
        reply = restored._rpc({'api_version': 1, 'action': 'result', 'arguments': {'job_id': 'candidate'}})
        assert reply['candidate'] == result['candidate']
        assert reply['partial_result']['candidate_verified'] is True
        assert reply['execution']['cancel_snapshot'] == marker
        assert canonical(reply) == canonical(expected)
    finally:
        restored.scheduler.guard.close()
        restored.scheduler.lock.close()
