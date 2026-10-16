#!/usr/bin/env python3
"""Isolated real CUDA shared-queue smoke; never mutates the source asset/repo."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

from coat_sim.appearance.gpu import FATAL_COUNTERS
from coat_sim.core.appearance_freeze_worker import build_frozen_envelope
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.scheduler.jobs import JobSpec


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True,
                        help='new isolated root containing only a private data/asset copy and private scheduler')
    parser.add_argument('--source-job', type=Path, required=True)
    parser.add_argument('--source-asset', type=Path, required=True)
    parser.add_argument('--runtime-python', type=Path, required=True,
                        help='existing installed Mitsuba Python, also exposed at root/build/mitsuba_appearance_env')
    parser.add_argument('--width', type=int, default=64)
    parser.add_argument('--height', type=int, default=48)
    parser.add_argument('--spp', type=int, default=4)
    parser.add_argument('--precision', choices=('mixed', 'float32'), default='mixed')
    args = parser.parse_args()
    root = args.project_root.resolve()
    source_asset = args.source_asset.resolve(strict=True)
    runtime_python = args.runtime_python.resolve(strict=True)
    if root.exists() and any(root.iterdir()):
        # Allow only a prepared runtime link; never mix with old queue/data.
        if any(path.name not in {'build'} for path in root.iterdir()):
            parser.error('isolated project root is not empty before this smoke')
    root.mkdir(parents=True, exist_ok=True)
    installed = root / 'build' / 'mitsuba_appearance_env' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if not installed.is_file() or installed.resolve(strict=True) != runtime_python:
        parser.error('prepare a reversible runtime junction at project-root/build/mitsuba_appearance_env pointing to the existing installed environment; refusing implicit interpreter fallback')
    if min(args.width, args.height, args.spp) < 1:
        parser.error('width, height and spp must be positive')
    source = json.loads(args.source_job.read_text(encoding='utf-8'))
    expected = source.get('provenance', {}).get('process_asset_sha256')
    if not isinstance(expected, str) or sha(source_asset) != expected:
        parser.error('source asset does not match frozen source-job SHA-256')
    asset = root / 'data' / 'assets' / 'hr54-source.coatasset'
    asset.parent.mkdir(parents=True, exist_ok=True)
    with source_asset.open('rb') as src, asset.open('xb') as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    if sha(asset) != expected:
        raise RuntimeError('private source asset copy changed while copying')
    inputs = {key: source[key] for key in ('wavelength_nm', 'camera', 'area_source',
                                          'environment_spectrum', 'resolution')}
    inputs['resolution'] = {**inputs['resolution'], 'width': args.width,
                            'height': args.height, 'spp': args.spp}
    if source['substrate'].get('circle_boundary_sampling'):
        inputs['circle_boundary_sampling'] = source['substrate']['circle_boundary_sampling']
    provenance = source['substrate'].get('solid_thickness_provenance')
    request = {'asset_path': str(asset), 'asset_sha256': expected,
               'render_inputs': inputs, 'backend': 'gpu_cuda',
               'precision': args.precision, 'display_exposure': .012,
               'geometry_thickness_mode': ('explicit_override' if provenance == 'explicit_geometry_input'
                                           else 'film_design_snapshot')}
    if provenance == 'explicit_geometry_input':
        request['geometry_thickness_mm'] = source['substrate']['solid_thickness_mm']
    envelope = build_frozen_envelope(root, request)
    job = envelope['job']
    if (job['substrate'] != source['substrate']
            or job['front_layers'] != source['front_layers']
            or len(job['wavelength_nm']) != 86 or job['substrate']['radius_mm'] != 150
            or job['substrate']['solid_thickness_mm'] != 12):
        raise RuntimeError('private queue frozen job differs from actual HR54 substrate/fields')
    client = SchedulerClient(root)
    spec = JobSpec('appearance_render', {'frozen': envelope}, {}).to_dict()
    submitted = client.submit(spec)
    job_id = submitted['job_id']
    print(json.dumps({'status': 'submitted', 'job_id': job_id, 'root': str(root)}, ensure_ascii=False), flush=True)
    status = client.wait_for_terminal(job_id, on_update=lambda row: print(json.dumps({
        'status': 'queue_progress', 'job_id': job_id, 'state': row.get('state'),
        'progress': row.get('progress')}, ensure_ascii=False), flush=True))
    if status.get('state') != 'succeeded':
        raise RuntimeError(f'private GPU queue job ended {status.get("state")}; inspect {root}/artifacts/runtime/scheduler/jobs/{job_id}/worker.log')
    result = client.result(job_id)
    manifest_path = root / 'data' / 'tmp' / 'appearance' / job_id / 'render' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (result.get('backend') != 'gpu_cuda' or result.get('precision') != args.precision
            or result.get('render_status') != 'rendered_unqualified'
            or manifest.get('runtime', {}).get('variant') != 'cuda_ad_rgb'
            or len(manifest.get('per_wavelength', [])) != 86
            or any(not row['diagnostics']['pass'] or any(row['diagnostics'][key] != 0 for key in FATAL_COUNTERS)
                   for row in manifest['per_wavelength'])):
        raise RuntimeError('private shared-queue result failed CUDA/diagnostic verification')
    print(json.dumps({'status': 'integrated_queue_pass', 'job_id': job_id,
                      'root': str(root), 'asset_sha256': expected,
                      'backend': result['backend'], 'precision': result['precision'],
                      'width': args.width, 'height': args.height, 'spp': args.spp,
                      'wavelengths': 86, 'fatal_total': 0}, ensure_ascii=False), flush=True)
    client.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
