"""Offline launch-entry contracts. No user desktop writes or application start."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.dependency_manager import launchers


class LaunchEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "中文 space & ! % ' project"
        self.root.mkdir()
        self.venv = self.root / '.venv'
        (self.venv / 'bin').mkdir(parents=True)
        (self.venv / 'Scripts').mkdir()
        (self.venv / 'pyvenv.cfg').write_text('include-system-site-packages = false\n')
        for path in (self.venv / 'bin/python', self.venv / 'Scripts/python.exe'):
            path.write_bytes(b'placeholder')
        (self.root / 'launcher.py').write_text('# Not run\n')
        icon = self.root / launchers.ICON_RELATIVE
        icon.parent.mkdir(parents=True)
        icon.write_bytes(b'original icon')
        self.probe = patch.object(launchers.manager, 'probe_python', return_value={
            'prefix': str(self.venv), 'base_prefix': sys.base_prefix})
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def plan(self, **kwargs):
        return launchers.plan_launch_entries(self.root, **kwargs)

    def test_requires_confirmation_and_no_overwrite(self):
        plan = self.plan()
        self.assertEqual(plan['status'], 'ready', plan)
        self.assertEqual(launchers.execute_launch_entries(plan)['status'], 'blocked')
        result = launchers.execute_launch_entries(plan, confirmed=True)
        self.assertEqual(result['status'], 'success', result)
        path = Path(result['files'][0]['path'])
        before = path.read_bytes()
        self.assertEqual(self.plan()['status'], 'blocked')
        self.assertEqual(launchers.execute_launch_entries(plan, confirmed=True)['status'], 'blocked')
        self.assertEqual(path.read_bytes(), before)

    def test_unrelated_file_preserved(self):
        path = self.root / ('Coating Simulator.cmd' if sys.platform == 'win32' else 'Coating Simulator.sh')
        path.write_text('unrelated')
        self.assertEqual(self.plan()['status'], 'blocked')
        self.assertEqual(path.read_text(), 'unrelated')
        self.assertEqual(self.plan(name='Other name')['status'], 'ready')

    def test_changed_plan_and_inputs_blocked(self):
        plan = self.plan()
        plan['script'] = 'malicious changed content'
        self.assertEqual(launchers.execute_launch_entries(plan, confirmed=True)['status'], 'blocked')
        plan = self.plan()
        (self.root / launchers.ICON_RELATIVE).write_bytes(b'changed icon')
        self.assertEqual(launchers.execute_launch_entries(plan, confirmed=True)['status'], 'blocked')

    def test_invalid_venv_and_names_blocked(self):
        for name in ('../bad', 'CON', 'bad%name', 'bad\nname', 'bad.'):
            self.assertEqual(self.plan(name=name)['status'], 'blocked', name)
        (self.venv / 'pyvenv.cfg').write_text('include-system-site-packages = true\n')
        self.assertEqual(self.plan()['status'], 'blocked')

    def test_wrong_python_prefix_blocked(self):
        with patch.object(launchers.manager, 'probe_python', return_value={'prefix': '/wrong', 'base_prefix': '/base'}):
            self.assertEqual(self.plan()['status'], 'blocked')

    def test_windows_shortcut_destinations_and_icon(self):
        dest = self.root / 'chosen shortcut folder'
        dest.mkdir()
        with patch.object(launchers.sys, 'platform', 'win32'), patch.object(launchers, '_known_folder', return_value=dest) as known:
            plan = self.plan(shortcut='desktop')
            self.assertEqual(plan['status'], 'ready', plan)
            known.assert_called_once_with('desktop')
            self.assertTrue(plan['files'][1]['path'].endswith('.lnk'))
            self.assertEqual(plan['icon'], str(self.root / launchers.ICON_RELATIVE))
            self.assertIn('DisableDelayedExpansion', plan['script'])
            self.assertIn('pushd "%~dp0"', plan['script'])
            self.assertNotIn(str(self.root), plan['script'])
            self.assertEqual(self.plan(shortcut='custom')['status'], 'blocked')
            self.assertEqual(self.plan(shortcut='custom', output_dir=dest)['status'], 'ready')

    def test_shortcut_failure_creates_nothing(self):
        dest = self.root / 'custom'
        dest.mkdir()
        with patch.object(launchers.sys, 'platform', 'win32'), patch.object(launchers, '_write_shortcut', side_effect=OSError('policy blocked')):
            plan = self.plan(shortcut='custom', output_dir=dest)
            result = launchers.execute_launch_entries(plan, confirmed=True)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['files'], [])
        self.assertFalse(Path(plan['files'][0]['path']).exists())

    @unittest.skipUnless(sys.platform == 'linux', 'Actual POSIX shell execution')
    def test_script_real_argument_and_cwd_round_trip_no_fallback(self):
        python = self.venv / 'bin/python'
        python.unlink()
        python.symlink_to(sys.executable)
        (self.root / 'launcher.py').write_text(
            'import json, os, sys\nprint(json.dumps([os.getcwd(),sys.argv[1:]],ensure_ascii=False))\n')
        plan = self.plan()
        result = launchers.execute_launch_entries(plan, confirmed=True)
        self.assertEqual(result['status'], 'success', result)
        script = result['files'][0]['path']
        args = ['空 格', 'a"b', "'", '$HOME; echo injected', 'a!%&b']
        run = subprocess.run([script, *args], cwd='/', capture_output=True, text=True,
                             env={**os.environ, 'PYTHONHOME': '/invalid'})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), [str(self.root), args])
        python.unlink()
        run = subprocess.run([script], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertIn('.venv is missing', run.stderr)

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows Shell Link acceptance')
    def test_native_windows_shell_link_fields(self):
        destination = self.root / 'custom output'
        destination.mkdir()
        plan = self.plan(shortcut='custom', output_dir=destination)
        result = launchers.execute_launch_entries(plan, confirmed=True)
        self.assertEqual(result['status'], 'success', result)
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = self.root / 'read-link.ps1'
        script.write_text(
            "param([string]$Link)\n"
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($Link)\n"
            "@{target=$s.TargetPath; arguments=$s.Arguments; cwd=$s.WorkingDirectory; icon=$s.IconLocation} | ConvertTo-Json -Compress\n",
            encoding='utf-8-sig')
        read = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-File', str(script),
                               '-Link', result['files'][1]['path']], capture_output=True, timeout=30)
        self.assertEqual(read.returncode, 0, read.stderr)
        # Windows PowerShell redirected output follows its console encoding.
        # Force UTF-8 for a separate run so Chinese-path assertions are reliable.
        text = script.read_text(encoding='utf-8-sig').replace(
            "param([string]$Link)\n", "param([string]$Link)\n[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n")
        script.write_text(text, encoding='utf-8-sig')
        read = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-File', str(script),
                               '-Link', result['files'][1]['path']], capture_output=True, timeout=30)
        self.assertEqual(read.returncode, 0, read.stderr)
        data = json.loads(read.stdout.decode('utf-8-sig'))
        self.assertEqual(Path(data['target']), Path(plan['python']))
        self.assertEqual(Path(data['cwd']), self.root)
        self.assertEqual(data['arguments'], subprocess.list2cmdline(['-E', str(self.root / 'launcher.py')]))
        self.assertEqual(data['icon'], plan['icon'] + ',0')

    def test_symlink_output_is_preserved(self):
        if os.name == 'nt':
            self.skipTest('No symlink privilege assumed')
        target = self.root / 'unrelated'
        target.write_text('keep')
        output = self.root / 'Coating Simulator.sh'
        output.symlink_to(target)
        self.assertEqual(self.plan()['status'], 'blocked')
        self.assertEqual(target.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
