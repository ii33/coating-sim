"""Test-only commit barrier around the unchanged real synthesis worker.

No synthetic scores or solver changes: pause after a complete improving training
candidate is committed, and release only when the real queue cancel event is set.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import time
from unittest.mock import patch


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--gate-mode', choices=('improving_complete', 'any_complete'),
                        default='improving_complete')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('command', choices=('worker',))
    parser.add_argument('job_id')
    parser.add_argument('owner_pid', type=int)
    parser.add_argument('owner_instance')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not args.job_id or args.job_id in ('.', '..') or Path(args.job_id).name != args.job_id:
        raise ValueError('cancel fixture requires one task-local job id')
    directory = root / 'artifacts/runtime/scheduler/jobs' / args.job_id

    from coat_sim.core import film_auto_synthesis as synthesis
    from coat_sim.core import film_candidate_display as display
    from coat_sim.core import film_synthesis_audit as audit
    from coat_sim.core import film_synthesis_jobs as jobs
    from coat_sim.core.film_synthesis_archive import physical_design_hash
    from coat_sim.scheduler.io import write_json
    from coat_sim.scheduler.jobs import JobSpec
    from coat_sim.scheduler.worker import FileCancelEvent, run

    frozen = JobSpec.parse_frozen_json((directory / 'spec.json').read_text(encoding='utf-8'))
    if frozen.executor != 'film_synthesis':
        raise ValueError('cancel fixture requires a real frozen synthesis job')
    cancel = FileCancelEvent(root, args.job_id, args.owner_pid, args.owner_instance)
    state = dict(gated=False, cancel_seen=False, audits_after_cancel=0, forwards_after_cancel=0)
    original_record = display.record_candidate
    original_synthesize = jobs.synthesize_film_design
    original_audit = audit.audit_frozen_synthesis_candidate
    original_forward = synthesis.evaluate_frozen_structure_candidate

    def checked_audit(*values, **kwargs):
        if cancel.is_set():
            state['audits_after_cancel'] += 1
            raise AssertionError('cancel must not launch a new audit')
        return original_audit(*values, **kwargs)

    def checked_forward(*values, **kwargs):
        if cancel.is_set():
            state['forwards_after_cancel'] += 1
            raise AssertionError('cancel must not launch a new forward evaluation')
        return original_forward(*values, **kwargs)

    def checkpoint_replay(*_args, **_kwargs):
        raise AssertionError('current worker cancellation must not replay an old checkpoint')

    def synthesize_with_observer(service, request, **kwargs):
        nonlocal cancel
        cancel = kwargs['cancel_event']
        # A public observation callback provides the already committed complete
        # candidate, even if optional desktop live-display transport is disabled.
        kwargs['candidate_observer'] = display.CandidateDisplayObserver(
            context_id='test-complete-cancel-boundary', objectives=request.objectives,
            control_provider=lambda: {}, publish_callback=lambda _value: None)
        return original_synthesize(service, request, **kwargs)

    def record_and_gate(observer, design, **kwargs):
        original_record(observer, design, **kwargs)
        if state['gated'] or not design['front_layers'] or kwargs.get('scope') != 'training':
            return
        checkpoint_path = directory / 'best-candidate.json'
        if not checkpoint_path.is_file():
            return
        checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
        if checkpoint.get('checkpoint_state') != 'audited':
            return
        if (args.gate_mode == 'improving_complete'
                and kwargs['max_normalized_error'] >= checkpoint['training_score']['max_error']):
            return
        state['gated'] = True
        ready = jobs._json_snapshot(dict(
            source='complete_training_commit_observer', candidate_design=deepcopy(design),
            physical_hash=physical_design_hash(design), evaluations=kwargs['evaluations'],
            max_error=kwargs['max_normalized_error'], weighted_rms_error=kwargs['weighted_rms_error'],
            errors=kwargs['condition_rows'], coverage=kwargs['raw_coverage'],
            checkpoint_hash=physical_design_hash(checkpoint['design'])))
        write_json(directory / 'cancel-boundary-ready.json', ready)
        deadline = time.monotonic() + 30
        while not cancel.is_set():
            if time.monotonic() >= deadline:
                raise TimeoutError('test commit barrier did not receive queue cancellation')
            time.sleep(.01)
        state['cancel_seen'] = True
        write_json(directory / 'cancel-boundary-observed.json', state)

    try:
        with (patch.object(display, 'record_candidate', record_and_gate),
              patch.object(jobs, 'synthesize_film_design', synthesize_with_observer),
              patch.object(audit, 'audit_frozen_synthesis_candidate', checked_audit),
              patch.object(synthesis, 'evaluate_frozen_structure_candidate', checked_forward),
              patch.object(jobs, 'validate_checkpoint', checkpoint_replay)):
            return run(root, args.job_id, args.owner_pid, args.owner_instance)
    finally:
        write_json(directory / 'cancel-boundary-final.json', state)


if __name__ == '__main__':
    raise SystemExit(main())
