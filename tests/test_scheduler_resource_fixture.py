"""Fail-closed safety tests for the fixed-choice resource worker fixture."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from coat_sim.scheduler.jobs import JobSpec
from tests.scheduler_queue_support import SchedulerHarness
from tests.scheduler_resource_worker import main, validate_bounded_spec


def bounded_spec():
    return JobSpec('process_recipe', {
        'settings': {'grid_size': 21, 'time_steps': 12},
        'backend': 'CPU', 'algorithm': 'fast',
    }, {})


class ResourceFixtureSafetyTests(unittest.TestCase):
    def test_validator_accepts_only_fixed_bounded_compute_parameters(self):
        spec = bounded_spec()
        self.assertEqual(validate_bounded_spec(spec), spec.parameters['settings'])
        variants = []
        for key, values in (
                ('grid_size', (100000, 23, True, 21.0, '21', None)),
                ('time_steps', (100000, 13, True, 12.0, '12', None))):
            for value in values:
                candidate = deepcopy(spec)
                candidate.parameters['settings'][key] = value
                variants.append(candidate)
        for key, value in (('backend', 'GPU'), ('algorithm', 'reference')):
            candidate = deepcopy(spec)
            candidate.parameters[key] = value
            variants.append(candidate)
        variants.append(JobSpec('probe', spec.parameters, {}))
        variants.append(JobSpec('process_recipe', {'settings': {}}, {}))
        for candidate in variants:
            with self.subTest(parameters=candidate.parameters, executor=candidate.executor):
                with self.assertRaisesRegex(ValueError, 'bounded resource fixture requires'):
                    validate_bounded_spec(candidate)

    def test_unknown_failure_choice_cannot_change_launch_route(self):
        from coat_sim.scheduler import daemon
        with tempfile.TemporaryDirectory() as folder:
            harness = SchedulerHarness(Path(folder))
            try:
                original = daemon.module_command
                with self.assertRaisesRegex(ValueError, 'failure'):
                    with harness.resource_failure_workers('arbitrary-code'):
                        self.fail('unrecognized failure was admitted')
                self.assertIs(daemon.module_command, original)
            finally:
                harness.close()

    def test_launch_route_and_child_reject_root_escape_or_unsafe_job_identity(self):
        from coat_sim.scheduler import daemon
        from tests.scheduler_resource_worker import _validated_child_spec
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            harness = SchedulerHarness(root)
            try:
                with (harness.resource_failure_workers('memory') as processes,
                      patch.object(Path, 'read_text',
                                   side_effect=AssertionError('escaped fixture file read')) as read):
                    with self.assertRaisesRegex(ValueError, 'isolated scheduler root'):
                        daemon.module_command('coat_sim.scheduler', '--root', str(root / 'outside'),
                                              'worker', 'safe', str(os.getpid()), 'owner')
                    for job_id in ('.', '..', '../outside', 'nested/job'):
                        with self.subTest(job_id=job_id):
                            with self.assertRaisesRegex(ValueError, 'safe job id'):
                                daemon.module_command('coat_sim.scheduler', '--root', str(root),
                                                      'worker', job_id, str(os.getpid()), 'owner')
                            with self.assertRaisesRegex(ValueError, 'bounded resource fixture requires'):
                                _validated_child_spec(root, job_id)
                    read.assert_not_called()
                self.assertEqual(processes, [])
            finally:
                harness.close()

    def test_changed_scheduler_worker_command_shape_fails_closed(self):
        from coat_sim.scheduler import daemon
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            harness = SchedulerHarness(root)
            try:
                with (harness.resource_failure_workers('memory') as processes,
                      patch.object(Path, 'read_text',
                                   side_effect=AssertionError('malformed command read spec')) as read):
                    malformed = (
                        ('worker', 'job', str(os.getpid()), 'owner'),
                        ('--root', str(root), 'worker', 'job', str(os.getpid())),
                        ('--root', str(root), 'worker', 'job', str(os.getpid()), 'owner', '--extra'),
                    )
                    for arguments in malformed:
                        with self.subTest(arguments=arguments):
                            with self.assertRaisesRegex(ValueError, 'known worker launch contract'):
                                daemon.module_command('coat_sim.scheduler', *arguments)
                    read.assert_not_called()
                self.assertEqual(processes, [])
            finally:
                harness.close()

    def test_oversized_queued_spec_is_rejected_before_popen(self):
        from coat_sim.scheduler import daemon
        with tempfile.TemporaryDirectory() as folder:
            harness = SchedulerHarness(Path(folder))
            try:
                spec = bounded_spec()
                # This is scalar validation only; Popen must remain untouched.
                spec.parameters['settings']['grid_size'] = 100000
                job = harness.client().submit(spec.to_dict())
                with (patch.object(daemon.subprocess, 'Popen',
                                   side_effect=AssertionError('unsafe worker launched')) as launch,
                      harness.resource_failure_workers('memory') as processes):
                    harness.tick()
                launch.assert_not_called()
                self.assertEqual(processes, [])
                row = harness.client().status(job['job_id'])
                self.assertEqual(row['state'], 'failed', row)
                self.assertEqual(row['error_code'], 'worker_start_failed', row)
                self.assertIn('bounded resource fixture requires', row['error'])
                self.assertIsNone(harness.scheduler.active)
            finally:
                harness.close()

    def test_child_guard_rejects_oversized_frozen_spec_before_worker_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / 'artifacts/runtime/scheduler/jobs/unsafe'
            directory.mkdir(parents=True)
            spec = bounded_spec()
            spec.parameters['settings']['grid_size'] = 100000
            (directory / 'spec.json').write_text(json.dumps(spec.to_dict()))
            argv = ['resource-worker', '--failure', 'memory', '--root', str(root),
                    'worker', 'unsafe', str(os.getpid()), 'test-owner']
            with (patch.object(sys, 'argv', argv),
                  patch('coat_sim.scheduler.worker.run',
                        side_effect=AssertionError('unsafe compute entered')) as run):
                with self.assertRaisesRegex(ValueError, 'bounded resource fixture requires'):
                    main()
            run.assert_not_called()
            self.assertFalse((directory / 'resource-failure-fixture.json').exists())

    def test_context_reaps_launched_child_and_restores_routes_after_assertion(self):
        from coat_sim.scheduler import daemon
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            harness = SchedulerHarness(root)
            try:
                directory = root / 'artifacts/runtime/scheduler/jobs/cleanup'
                directory.mkdir(parents=True)
                (directory / 'spec.json').write_text(json.dumps(bounded_spec().to_dict()))
                original_command, original_popen = daemon.module_command, daemon.subprocess.Popen
                with self.assertRaisesRegex(AssertionError, 'bounded assertion failure'):
                    with harness.resource_failure_workers('memory') as processes:
                        command = daemon.module_command(
                            'coat_sim.scheduler', '--root', str(root), 'worker',
                            'cleanup', str(os.getpid()), harness.scheduler.owner)
                        # No launch.json: unchanged worker waits for its durable
                        # lease rather than entering a simulation algorithm.
                        child = daemon.subprocess.Popen(
                            command, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        self.assertIsNone(child.poll())
                        raise AssertionError('bounded assertion failure')
                self.assertEqual(processes, [child])
                self.assertIsNotNone(child.poll())
                self.assertTrue(child.stdout.closed)
                self.assertTrue(child.stderr.closed)
                self.assertIs(daemon.module_command, original_command)
                self.assertIs(daemon.subprocess.Popen, original_popen)
                self.assertFalse((directory / 'resource-failure-fixture.json').exists())
            finally:
                harness.close()
