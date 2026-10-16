#!/usr/bin/env python3
"""Real frozen-asset CUDA appearance worker smoke, using isolated source/runtime.

This never changes the source project or its large assets. It requires the
original saved .coatasset whose SHA is bound into the supplied prior job.
Explicit --width/--height/--spp are smoke overrides, never silent reductions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import uuid

from coat_sim.appearance.gpu import FATAL_COUNTERS, json_hash
from coat_sim.ui.appearance_worker import (
    _appearance_context_from_asset, _atomic_json, _build_job_from_verified_asset,
    _canonical, run,
)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--source-job', type=Path, required=True,
                        help='actual prior frozen HR54 schema-1 input-job.json')
    parser.add_argument('--source-asset', type=Path,
                        help='original saved .coatasset, including outside project data; SHA must match source job')
    parser.add_argument('--output-parent', type=Path, required=True,
                        help='new disposable smoke outputs are created here')
    parser.add_argument('--width', type=int, default=64)
    parser.add_argument('--height', type=int, default=48)
    parser.add_argument('--spp', type=int, default=4)
    parser.add_argument('--precision', choices=('mixed', 'float32'), default='mixed')
    parser.add_argument('--cancel-test', action='store_true',
                        help='run a second child, request cancel at first GPU wavelength, and reject partial output')
    args = parser.parse_args()
    root = args.project_root.resolve(strict=True)
    source = json.loads(args.source_job.read_text(encoding='utf-8'))
    if source.get('schema_version') != 1 or source.get('substrate', {}).get('outline_kind') != 'circle':
        parser.error('source job must be the actual flat circular schema-1 case')
    expected_asset_sha = source.get('provenance', {}).get('process_asset_sha256')
    if not isinstance(expected_asset_sha, str) or len(expected_asset_sha) != 64:
        parser.error('source job does not bind a process asset SHA-256')
    if args.source_asset is None:
        matches = [p for p in (root / 'data').rglob('*.coatasset')
                   if p.is_file() and _hash_file(p) == expected_asset_sha]
        if len(matches) != 1:
            parser.error(f'expected exactly one real .coatasset with SHA {expected_asset_sha}; found {len(matches)}')
        asset = matches[0]
    else:
        asset = args.source_asset.resolve(strict=True)
    if _hash_file(asset) != expected_asset_sha:
        parser.error('original asset must match the source-job SHA-256')
    if min(args.width, args.height, args.spp) < 1:
        parser.error('smoke width, height and spp must be positive')
    inputs = {key: source[key] for key in ('wavelength_nm', 'camera', 'area_source',
                                          'environment_spectrum', 'resolution')}
    inputs['resolution'] = {**inputs['resolution'], 'width': args.width,
                            'height': args.height, 'spp': args.spp}
    if source['substrate'].get('circle_boundary_sampling'):
        inputs['circle_boundary_sampling'] = source['substrate']['circle_boundary_sampling']
    provenance = source['substrate'].get('solid_thickness_provenance')
    request = {'asset_path': str(asset), 'asset_sha256': expected_asset_sha,
               'render_inputs': inputs, 'backend': 'gpu_cuda',
               'precision': args.precision, 'display_exposure': .012,
               'geometry_thickness_mode': ('explicit_override' if provenance == 'explicit_geometry_input'
                                           else 'film_design_snapshot')}
    if provenance == 'explicit_geometry_input':
        request['geometry_thickness_mm'] = source['substrate']['solid_thickness_mm']
    context = _appearance_context_from_asset(asset, root, expected_asset_sha)
    job = _build_job_from_verified_asset(
        context['process_profile'], str(asset), str(root), inputs['wavelength_nm'], inputs,
        expected_sha256=expected_asset_sha,
        geometry_thickness_mm=request.get('geometry_thickness_mm'))
    if (job['substrate'] != source['substrate']
            or job['front_layers'] != source['front_layers']
            or job.get('provenance', {}).get('process_asset_sha256') != expected_asset_sha):
        raise RuntimeError('frozen asset job differs from prior actual source geometry/material/layer fields')
    if len(job['wavelength_nm']) != 86 or job['substrate']['radius_mm'] != 150.0 or job['substrate']['solid_thickness_mm'] != 12.0:
        raise RuntimeError('frozen source is not the expected 86-wave, 300 mm diameter, 12 mm HR54 case')
    args.output_parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='gpu-product-worker-', dir=args.output_parent))
    job_id = uuid.uuid4().hex
    worker_input = {'schema': 'coat_sim.appearance_worker_input', 'version': 1,
                    'job_id': job_id,
                    'input_sha256': hashlib.sha256(_canonical(request)).hexdigest(),
                    'context_sha256': hashlib.sha256(_canonical(context)).hexdigest(),
                    'context': context, 'render_inputs': inputs,
                    'backend': 'gpu_cuda', 'precision': args.precision,
                    'geometry_thickness_mode': request['geometry_thickness_mode'],
                    'geometry_thickness_mm': request.get('geometry_thickness_mm')}
    _atomic_json(directory / 'worker_input.json', worker_input)
    code = run(directory / 'worker_input.json', directory, directory / 'cancel.request')
    if code != 0:
        raise RuntimeError(f'integrated worker failed code={code}; job_dir={directory}')
    completion = json.loads((directory / 'appearance_result.json').read_text(encoding='utf-8'))
    manifest = json.loads((directory / 'render' / 'manifest.json').read_text(encoding='utf-8'))
    if (completion['backend'] != 'gpu_cuda' or completion['precision'] != args.precision
            or manifest['input_sha256'] != json_hash(job)
            or manifest['status'] != 'rendered_unqualified'
            or manifest.get('runtime', {}).get('variant') != 'cuda_ad_rgb'
            or len(manifest['per_wavelength']) != 86
            or any(not row['diagnostics']['pass'] or
                   any(row['diagnostics'][key] != 0 for key in FATAL_COUNTERS)
                   for row in manifest['per_wavelength'])):
        raise RuntimeError('integrated CUDA worker output failed smoke verification')
    print(json.dumps({'status': 'integrated_worker_pass', 'job_dir': str(directory),
                      'input_sha256': manifest['input_sha256'],
                      'asset_sha256': expected_asset_sha,
                      'backend': completion['backend'], 'precision': completion['precision'],
                      'width': args.width, 'height': args.height, 'spp': args.spp,
                      'wavelengths': 86, 'fatal_total': 0}, ensure_ascii=False))
    if args.cancel_test:
        cancel_dir = Path(tempfile.mkdtemp(prefix='gpu-product-cancel-', dir=args.output_parent))
        cancel_request = {**worker_input, 'job_id': uuid.uuid4().hex}
        _atomic_json(cancel_dir / 'worker_input.json', cancel_request)
        marker = cancel_dir / 'cancel.request'
        command = [sys.executable, '-s', '-P', '-m', 'coat_sim.ui.appearance_worker',
                   '--input', str(cancel_dir / 'worker_input.json'),
                   '--job-dir', str(cancel_dir), '--cancel-marker', str(marker)]
        child = subprocess.Popen(command, cwd=str(root), env=dict(os.environ),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
        lines: queue.Queue[str | None] = queue.Queue()

        def collect():
            assert child.stdout is not None
            for line in child.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=collect, daemon=True).start()
        reached_first_wave = False
        try:
            while not reached_first_wave:
                try:
                    line = lines.get(timeout=120)
                except queue.Empty as exc:
                    raise RuntimeError('cancel child never reached first wavelength status') from exc
                if line is None:
                    raise RuntimeError(f'cancel child exited early with code {child.poll()}')
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                reached_first_wave = (event.get('stage') == 'rendering'
                                      and event.get('completed_wavelengths') == 0)
            marker.write_text('cancel\n', encoding='ascii')
            forced = False
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
                forced = True
            if (cancel_dir / 'appearance_result.json').exists():
                raise RuntimeError('cancelled CUDA worker published an invalid completion wrapper')
            print(json.dumps({'status': 'integrated_cancel_pass',
                              'job_dir': str(cancel_dir), 'forced_stop': forced,
                              'worker_exit_code': child.returncode,
                              'completion_published': False}, ensure_ascii=False))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
