"""Reproducible CPU-only synthesis benchmark; no speedup claim without equivalence.

Run with .venv/Scripts/python.exe tools/benchmark_film_synthesis_parallel.py
 --workers 1,2,4,8 --repeats 2 --case ar --output NEW_DIRECTORY
Each run owns an isolated Python process; --timeout is per run, followed by
--cancel-grace seconds for cooperative shutdown. All numerical work uses the
current imported source. Synthetic k=0 fixtures make no real-material claim.
"""
from __future__ import annotations

import argparse
from dataclasses import fields, replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time
import traceback
from typing import Any
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def dump(path, payload):
    from tools.run_film_synthesis_acceptance import atomic_json
    atomic_json(Path(path), payload)


def semantic_result(result):
    """Exact prescription, ranking trace, scores and audit, excluding execution metadata."""
    keys = ('state', 'termination_reason', 'candidate_design', 'targets_met',
            'training_score', 'validation_score', 'validation_complete', 'cycles',
            'evaluations', 'validation_evaluations', 'failed_evaluations', 'trace',
            'audit_physical_evaluations', 'audit_wavelength_points')
    return {key: result.get(key) for key in keys}


def case_acceptance_passed(name, result, *, partial=False):
    """Fixed-cycle throughput exhaustion is valid evidence, not target attainment."""
    if partial:
        return False
    if name in {'ar', 'smoke'}:
        return result.get('state') == 'completed' and bool(result.get('targets_met'))
    if name in {'throughput', 'multilayer'}:
        return (result.get('state') == 'completed' or
                (result.get('state') == 'not_found' and
                 result.get('termination_reason') == 'cycle_limit'))
    return False


def build_case(name, workers, wavelength_samples=4001):
    from tools.run_film_synthesis_acceptance import synthetic_service, make_request
    from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
    from coat_sim.core.film_optimization import FilmOptimizationObjective
    service, environment, rows = synthetic_service()
    if name == 'smoke':
        objective = FilmOptimizationObjective(550, 550, 1, mode='range', upper_bound=.05)
        request = make_request(environment, [objective], ['match'], cycles=1)
        note = 'Bare substrate already meets R<=0.05; harness plumbing only, no parallel speed claim.'
    elif name == 'ar':
        objective = FilmOptimizationObjective(520, 580, 9, mode='range', upper_bound=.001)
        request = make_request(environment, [objective], ['match', 'low', 'high'], cycles=4)
        note = 'Known synthetic quarter-wave AR solution; stops on audited target or explicit four cycles.'
    else:
        # Bare-start one-cycle workload: all insertion positions/materials and
        # growth fractions are identical for every worker configuration.
        objective = FilmOptimizationObjective(450, 900, wavelength_samples, angles_deg=(0., 30., 60.),
            mode='range', lower_bound=.999999)
        request = make_request(environment, [objective], ['match', 'low', 'high'],
            cycles=3 if name == 'multilayer' else 1,
            growth=(.25, .5, .75, 1., 1.5, 2., 3., 4.))
        request = replace(request, position_samples=5)
        note = ('Synthetic broad-band multi-angle reflector candidate throughput, '
                f'{request.max_cycles} fixed maximum cycles from bare substrate; '
                'target gap is reported, exhaustion is not physical infeasibility.')
    supports_workers = 'cpu_workers' in {field.name for field in fields(FilmSynthesisRequest)}
    if supports_workers:
        request = replace(request, cpu_workers=workers)
    elif workers != 1:
        raise RuntimeError('Current source has no synthesis cpu_workers contract')
    return service, request, {'description': note, 'optical_rows': rows,
        'cpu_workers_contract_present': supports_workers, 'data_class': 'explicit_synthetic_lossless'}


def process_memory(root_pid):
    """Return live tree RSS and native per-process lifetime working-set peaks."""
    try:
        psutil = importlib.import_module('psutil')
        parent = psutil.Process(root_pid)
        processes = [parent] + parent.children(recursive=True)
        records = []
        for process in processes:
            try:
                info = process.memory_info()
                records.append({'pid': process.pid, 'rss': info.rss,
                    'peak_working_set': getattr(info, 'peak_wset', None)})
            except psutil.Error:
                pass
        return records, 'psutil process tree RSS'
    except ImportError:
        pass
    if os.name != 'nt':
        return [], 'unavailable: psutil absent; no cross-platform tree RSS fallback'
    import ctypes as C
    from ctypes import wintypes as W
    class Entry(C.Structure):
        _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD),
            ('th32DefaultHeapID', C.c_size_t), ('th32ModuleID', W.DWORD),
            ('cntThreads', W.DWORD), ('th32ParentProcessID', W.DWORD),
            ('pcPriClassBase', W.LONG), ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR * 260)]
    class Counters(C.Structure):
        _fields_ = [('cb', W.DWORD), ('PageFaultCount', W.DWORD)] + [
            (key, C.c_size_t) for key in ('PeakWorkingSetSize', 'WorkingSetSize',
            'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
    kernel = C.WinDLL('kernel32', use_last_error=True)
    psapi = C.WinDLL('psapi', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = W.HANDLE
    kernel.OpenProcess.restype = W.HANDLE
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [W.HANDLE, C.POINTER(Entry)]
    psapi.GetProcessMemoryInfo.argtypes = [W.HANDLE, C.POINTER(Counters), W.DWORD]
    snap = kernel.CreateToolhelp32Snapshot(2, 0)
    parents = {}
    entry = Entry()
    entry.dwSize = C.sizeof(entry)
    try:
        more = kernel.Process32FirstW(snap, C.byref(entry))
        while more:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            more = kernel.Process32NextW(snap, C.byref(entry))
    finally:
        kernel.CloseHandle(snap)
    owned = {root_pid}
    while True:
        expanded = owned | {pid for pid, parent in parents.items() if parent in owned}
        if expanded == owned:
            break
        owned = expanded
    records = []
    for pid in owned:
        handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
        if not handle:
            continue
        try:
            info = Counters()
            info.cb = C.sizeof(info)
            if psapi.GetProcessMemoryInfo(handle, C.byref(info), info.cb):
                records.append({'pid': pid, 'rss': info.WorkingSetSize,
                    'peak_working_set': info.PeakWorkingSetSize})
        finally:
            kernel.CloseHandle(handle)
    return records, 'Windows Toolhelp32 + GetProcessMemoryInfo'


def child(args):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'tools'))
    from tools.run_film_synthesis_acceptance import jsonable, sha256_json
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request, synthesize_film_design
    output = Path(args.child)
    cancel = threading.Event()
    stop = threading.Event()
    lock = threading.Lock()
    def event(kind, **data):
        with lock, (output / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'kind': kind, 'elapsed_s': time.perf_counter() - started,
                **jsonable(data)}, ensure_ascii=False) + '\n')
    started = time.perf_counter()
    dump(output / 'runtime.json', {'synthesis_pid': os.getpid(), 'parent_pid': os.getppid()})
    def watch():
        while not stop.wait(1.):
            if (output / 'cancel').exists():
                cancel.set()
            event('heartbeat', cancel_requested=cancel.is_set())
    monitor = threading.Thread(target=watch, daemon=True)
    monitor.start()
    try:
        service, request, fixture = build_case(args.case, args.child_workers, args.wavelength_samples)
        frozen = freeze_film_synthesis_request(service, request)
        canonical_request = jsonable(frozen)
        canonical_request.pop('cpu_workers', None)
        input_sha256 = sha256_json(canonical_request)
        dump(output / 'request.json', {'request': frozen, 'fixture': fixture,
            'request_sha256': sha256_json(frozen), 'worker_independent_input_sha256': input_sha256})
        ready = time.perf_counter()
        event('ready', initialization_s=ready - started)
        result = synthesize_film_design(service, frozen, cancel_event=cancel,
            progress_callback=lambda progress: event('progress', progress=progress),
            snapshot_callback=lambda design, training, validation, audit:
                dump(output / 'checkpoint.json', {'design': design, 'training_score': training,
                    'validation_score': validation, 'audit': audit}))
        payload = jsonable(result)
        dump(output / 'result.json', {'result': payload, 'initialization_s': ready - started,
            'worker_independent_input_sha256': input_sha256,
            'synthesis_wall_s': time.perf_counter() - ready,
            'semantic_sha256': sha256_json(semantic_result(payload))})
        event('terminal', state=result.state, targets_met=result.targets_met)
    except BaseException:
        dump(output / 'error.json', {'traceback': traceback.format_exc()})
        raise
    finally:
        stop.set()
        monitor.join(timeout=2.)


def run_one(args, directory, workers):
    directory.mkdir()
    environment = dict(os.environ)
    for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        environment[key] = '1'
    environment['COAT_SIM_FILM_BACKEND'] = 'cpu'
    command = [sys.executable, str(Path(__file__).resolve()), '--child', str(directory),
               '--child-workers', str(workers), '--case', args.case,
               '--wavelength-samples', str(args.wavelength_samples)]
    started = time.perf_counter()
    native_peaks: dict[int, int] = {}
    peak_parent = peak_children = peak_total = peak_launcher = 0
    synthesis_pid = None
    cancellation = None
    cancel_at = None
    memory_method = None
    with (directory / 'stdout.log').open('w', encoding='utf-8') as stdout, (directory / 'stderr.log').open('w', encoding='utf-8') as stderr:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=stdout, stderr=stderr)
        dump(directory / 'owner.json', {'pid': process.pid, 'command': command})
        while process.poll() is None:
            elapsed = time.perf_counter() - started
            records, memory_method = process_memory(process.pid)
            if synthesis_pid is None and (directory / 'runtime.json').exists():
                synthesis_pid = json.loads((directory / 'runtime.json').read_text(encoding='utf-8'))['synthesis_pid']
            parent = sum(row['rss'] for row in records if row['pid'] == synthesis_pid)
            children = (sum(row['rss'] for row in records if row['pid'] not in {process.pid, synthesis_pid})
                        if synthesis_pid is not None else 0)
            launcher = sum(row['rss'] for row in records if row['pid'] == process.pid and row['pid'] != synthesis_pid)
            peak_parent = max(peak_parent, parent)
            peak_children = max(peak_children, children)
            peak_launcher = max(peak_launcher, launcher)
            peak_total = max(peak_total, sum(row['rss'] for row in records))
            for row in records:
                native_peaks[row['pid']] = max(native_peaks.get(row['pid'], 0), row['peak_working_set'] or 0)
            with (directory / 'memory.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'elapsed_s': elapsed, 'processes': records}) + '\n')
            external_cancel = args.cancel_file and Path(args.cancel_file).exists()
            if cancellation is None and (external_cancel or elapsed >= args.timeout):
                cancellation = 'cancel_file' if external_cancel else 'timeout'
                cancel_at = elapsed
                (directory / 'cancel').touch()
            if cancel_at is not None and elapsed - cancel_at >= args.cancel_grace:
                # Only the independently launched owned process tree is terminated.
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                else:
                    process.kill()
                break
            try:
                time.sleep(args.sample_interval)
            except KeyboardInterrupt:
                cancellation = 'keyboard_interrupt'
                cancel_at = time.perf_counter() - started
                (directory / 'cancel').touch()
        process.wait()
    wall = time.perf_counter() - started
    payload = json.loads((directory / 'result.json').read_text(encoding='utf-8')) if (directory / 'result.json').exists() else None
    result = payload['result'] if payload else {}
    summary = {'workers': workers, 'wall_s': wall, 'returncode': process.returncode,
        'interruption': cancellation, 'partial': payload is None or result.get('state') == 'cancelled',
        'synthesis_wall_s': payload.get('synthesis_wall_s') if payload else None,
        'initialization_s': payload.get('initialization_s') if payload else None,
        'launch_import_shutdown_and_monitor_overhead_s': wall - payload['synthesis_wall_s'] - payload['initialization_s'] if payload else None,
        'memory': {'method': memory_method, 'sample_interval_s': args.sample_interval,
            'launch_root_pid': process.pid, 'synthesis_parent_pid': synthesis_pid,
            'sampled_peak_launcher_rss_bytes': peak_launcher,
            'sampled_peak_parent_rss_bytes': peak_parent, 'sampled_peak_child_sum_rss_bytes': peak_children,
            'sampled_peak_tree_rss_bytes': peak_total, 'native_lifetime_peak_working_set_by_pid': native_peaks,
            'note': 'Tree peaks are sampled approximations. Per-process lifetime peaks are not simultaneous and must not be summed.'},
        'semantic_sha256': payload.get('semantic_sha256') if payload else None,
        'worker_independent_input_sha256': payload.get('worker_independent_input_sha256') if payload else None,
        'state': result.get('state'), 'targets_met': result.get('targets_met'),
        'case_acceptance_passed': case_acceptance_passed(args.case, result,
            partial=payload is None or cancellation is not None),
        'termination_reason': result.get('termination_reason'),
        'validation_max_error': (result.get('validation_score') or {}).get('max_error'),
        'evaluations': result.get('evaluations'), 'performance': result.get('performance'),
        'evidence_directory': str(directory)}
    dump(directory / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', default='1,2,4,8')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--case', choices=('smoke', 'ar', 'throughput', 'multilayer'), default='ar')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=float, default=3600.)
    parser.add_argument('--cancel-grace', type=float, default=30.)
    parser.add_argument('--cancel-file', type=Path)
    parser.add_argument('--sample-interval', type=float, default=.25)
    parser.add_argument('--wavelength-samples', type=int, default=4001,
                        help='Training grid for throughput/multilayer only; identical for all worker counts')
    parser.add_argument('--child', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--child-workers', type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / 'tools'))
    if args.child:
        child(args)
        return 0
    workers = [int(value) for value in args.workers.split(',')]
    if not args.output or args.repeats < 1 or not workers or min(workers) < 0 or args.timeout <= 0 or args.sample_interval <= 0 or args.cancel_grace < 0 or args.wavelength_samples < 1:
        parser.error('Require new --output, positive repeats/timeout/sample interval and nonnegative workers/grace')
    if workers[0] != 1:
        parser.error('--workers must start with 1: current-source serial baseline is mandatory')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    source_hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / 'coat_sim').rglob('*.py'))}
    source_hashes['tools/benchmark_film_synthesis_parallel.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    source_hashes['tools/run_film_synthesis_acceptance.py'] = hashlib.sha256((ROOT / 'tools' / 'run_film_synthesis_acceptance.py').read_bytes()).hexdigest()
    revision_command = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, capture_output=True)
    revision = revision_command.stdout.strip()
    dump(args.output / 'manifest.json', {'schema_version': 1, 'git_head': revision,
        'git_head_error': revision_command.stderr.strip() if revision_command.returncode else None,
        'source_sha256': source_hashes, 'python': sys.version, 'platform': platform.platform(),
        'processor': platform.processor(), 'logical_cpu_count': os.cpu_count(), 'arguments': vars(args),
        'comparison': 'Exact semantic equality required before reporting wall speedup; identical fixed case and unlimited evaluation budget.',
        'thread_environment': 'OMP/OPENBLAS/MKL/NUMEXPR_NUM_THREADS=1; CPU backend; no GPU',
        'stop_conditions': 'Core audited target / fixed max_cycles; external cancel-file or per-run timeout; forced owned tree kill after grace.'})
    runs = []
    try:
        for repeat in range(args.repeats):
            baseline: dict[str, Any] | None = None
            for count in workers:
                if args.cancel_file and args.cancel_file.exists():
                    break
                row = run_one(args, args.output / f'repeat-{repeat + 1}-workers-{count}', count)
                if count == 1:
                    baseline = row
                equivalent = bool(baseline and baseline['semantic_sha256'] and not baseline['partial'] and
                    not row['partial'] and row['case_acceptance_passed'] and baseline['case_acceptance_passed'] and
                    row['semantic_sha256'] == baseline['semantic_sha256'] and
                    row['worker_independent_input_sha256'] == baseline['worker_independent_input_sha256'])
                row['equivalent_to_serial'] = equivalent
                row['wall_speedup'] = baseline['wall_s'] / row['wall_s'] if equivalent and baseline is not None else None
                runs.append(row)
                dump(args.output / 'report.json', {'runs': runs, 'partial': True})
                print(json.dumps({key: row[key] for key in ('workers', 'wall_s', 'state', 'targets_met', 'equivalent_to_serial', 'wall_speedup')}), flush=True)
                if row['interruption']:
                    break
            if runs and runs[-1]['interruption']:
                break
    finally:
        changed_sources = [name for name, digest in source_hashes.items()
            if not (ROOT / name).exists() or hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
        if changed_sources:
            for row in runs:
                row['equivalent_to_serial'] = False
                row['wall_speedup'] = None
        dump(args.output / 'report.json', {'runs': runs, 'source_changed_during_run': changed_sources,
            'partial': any(row['partial'] for row in runs) or len(runs) != len(workers) * args.repeats,
            'median_wall_s_by_workers': {str(count): statistics.median([row['wall_s'] for row in runs if row['workers'] == count]) for count in workers if any(row['workers'] == count for row in runs)}})
        suite = ET.Element('testsuite', name='synthesis_parallel_equivalence', tests=str(len(runs)))
        for row in runs:
            case = ET.SubElement(suite, 'testcase', name=Path(row['evidence_directory']).name, time=str(row['wall_s']))
            if changed_sources or not row['equivalent_to_serial'] or row['returncode']:
                ET.SubElement(case, 'failure', message='Incomplete run or semantic mismatch').text = json.dumps(row)
        ET.ElementTree(suite).write(args.output / 'junit.xml', encoding='utf-8', xml_declaration=True)
    return 0 if not changed_sources and runs and len(runs) == len(workers) * args.repeats and all(row['equivalent_to_serial'] and not row['returncode'] for row in runs) else 1


if __name__ == '__main__':
    raise SystemExit(main())
