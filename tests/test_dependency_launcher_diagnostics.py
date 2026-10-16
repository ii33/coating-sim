"""Offline diagnostics for stock Windows shortcut creation failures."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.dependency_manager import launchers


class ShortcutDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        powershell = self.root / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        powershell.parent.mkdir(parents=True)
        powershell.write_bytes(b'placeholder; never executed')
        environment = patch.dict(launchers.os.environ, {'SystemRoot': str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.root / 'output.lnk'
        self.plan = {'root': str(self.root), 'python': str(self.root / 'python.exe'),
                     'icon': str(self.root / 'launcher.ico')}

    def shortcut_failure(self, *, returncode=1, stderr=b'', stdout=b'', exception=None):
        result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with patch.object(launchers.subprocess, 'run', return_value=result,
                          side_effect=exception) as run:
            with self.assertRaises(OSError) as caught:
                launchers._write_shortcut(self.path, self.plan)
        args, kwargs = run.call_args
        self.assertNotIn('-ExecutionPolicy', args[0])
        self.assertEqual(kwargs, {'capture_output': True, 'timeout': 30, 'shell': False})
        self.assertFalse((self.root / 'Coating Simulator.cmd').exists())
        return caught.exception

    def test_failure_reports_actual_exit_and_both_streams(self):
        error = self.shortcut_failure(returncode=5, stderr=b'CreateShortcut: access denied',
                          stdout=b'COM diagnostic')
        self.assertIn('exit code: 5', str(error))
        self.assertIn('stderr: CreateShortcut: access denied', str(error))
        self.assertIn('stdout: COM diagnostic', str(error))
        self.assertNotIn('script policy', str(error))

    def test_success_exit_without_artifact_is_not_success(self):
        error = self.shortcut_failure(returncode=0)
        self.assertIn('no shortcut file was created', str(error))
        self.assertIn('exit code: 0', str(error))
        self.assertIn('stderr: [empty]', str(error))

    def test_short_artifact_is_not_success(self):
        self.path.write_bytes(b'bad')
        error = self.shortcut_failure(returncode=0)
        self.assertIn('smaller than 76 bytes', str(error))
        self.assertEqual(self.path.read_bytes(), b'bad')

    def test_nonzero_exit_even_with_artifact_fails(self):
        self.path.write_bytes(b'x' * 76)
        self.assertIn('nonzero exit status', str(self.shortcut_failure()))

    def test_valid_artifact_keeps_existing_success_path(self):
        self.path.write_bytes(b'x' * 76)
        with patch.object(launchers.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], 0, b'', b'')):
            self.assertIsNone(launchers._write_shortcut(self.path, self.plan))

    def test_native_writer_uses_unicode_com_and_json_data(self):
        root = self.root / "中文 space & ! % ' project"
        plan = {'root': str(root), 'python': str(root / '.venv/Scripts/python.exe'),
                'icon': str(root / 'launcher.ico')}

        def run(command, **kwargs):
            script = Path(command[command.index('-File') + 1]).read_text(encoding='utf-8-sig')
            spec = json.loads(Path(command[command.index('-Spec') + 1]).read_text(encoding='utf-8'))
            self.assertIn('000214F9-0000-0000-C000-000000000046', script)
            self.assertIn('UnmanagedType.LPWStr', script)
            self.assertIn('((IPersistFile)instance).Save(path, true)', script)
            self.assertNotIn('WScript.Shell', script)
            self.assertNotIn(str(root), script)
            self.assertEqual(spec['python'], plan['python'])
            self.assertEqual(spec['root'], plan['root'])
            self.assertEqual(spec['icon'], plan['icon'])
            self.assertEqual(spec['arguments'], subprocess.list2cmdline(['-E', str(root / 'launcher.py')]))
            self.path.write_bytes(b'x' * 76)
            return subprocess.CompletedProcess(command, 0, b'', b'')

        with patch.object(launchers.subprocess, 'run', side_effect=run):
            launchers._write_shortcut(self.path, plan)

    def test_timeout_retains_cause_and_partial_output(self):
        original = subprocess.TimeoutExpired(['private command'], 30,
                                            output=b'partial stdout', stderr=b'partial stderr')
        error = self.shortcut_failure(exception=original)
        self.assertIs(error.__cause__, original)
        self.assertIn('timed out after 30 seconds', str(error))
        self.assertIn('exit code: unavailable', str(error))
        self.assertIn('partial stderr', str(error))
        self.assertIn('partial stdout', str(error))
        self.assertNotIn('private command', str(error))

    def test_start_failure_retains_cause_and_redacts_message(self):
        original = PermissionError('Access denied password=hunter2')
        error = self.shortcut_failure(exception=original)
        self.assertIs(error.__cause__, original)
        self.assertIn('PowerShell could not start', str(error))
        self.assertIn('Access denied', str(error))
        self.assertNotIn('hunter2', str(error))

    def test_output_redacted_before_bounded_display(self):
        output = ('https://alice:pass@example.org/path?private=hidden '
                  'Authorization: Bearer bearer-secret password="multiple words secret" '
                  'api_key=key-secret token=' + 's' * 2000 + ' trailing')
        error = str(self.shortcut_failure(stderr=output.encode(), stdout=b'x' * 5000))
        for secret in ('alice', 'pass@', 'hidden', 'bearer-secret', 'multiple words secret',
                       'key-secret', 'ssssssss'):
            self.assertNotIn(secret, error)
        self.assertIn('[REDACTED]', error)
        self.assertIn('[truncated]', error)
        self.assertLess(len(error), 3300)

    def test_bad_bytes_controls_and_malformed_url_are_safe(self):
        error = str(self.shortcut_failure(stderr=b'invalid \xff byte\x1b[31m'))
        self.assertIn('invalid', error)
        self.assertNotIn('\x1b', error)
        error = str(self.shortcut_failure(stderr=b'https://[malformed password=hidden'))
        self.assertIn('output omitted', error)
        self.assertNotIn('hidden', error)


if __name__ == '__main__':
    unittest.main()
