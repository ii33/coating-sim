"""Offline launch-entry contracts. No user desktop writes or application start."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.dependency_manager import launchers


_SHORTCUT_DIAGNOSTIC_SCRIPT = r'''param([string]$Spec)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$s = Get-Content -LiteralPath $Spec -Raw -Encoding UTF8 | ConvertFrom-Json
$rows = @()
foreach ($case in $s.cases) {
    $stage = 'CreateShortcut'
    $row = @{name=$case.name; length=$case.target.Length;
             exists=[System.IO.File]::Exists($case.target)}
    try {
        $w = New-Object -ComObject WScript.Shell
        $link = $w.CreateShortcut($case.link)
        $stage = 'TargetPath'
        $link.TargetPath = $case.target
        $stage = 'Save'
        $link.Save()
        $stage = 'Readback'
        $read = $w.CreateShortcut($case.link)
        $row.targetMatches = ($read.TargetPath -ceq $case.target)
        $row.readback = $read.TargetPath
        $row.success = $true
    } catch {
        $row.success = $false
        $row.stage = $stage
        $e = $_.Exception
        $row.exception = $e.GetType().FullName
        $row.hresult = ('0x{0:X8}' -f $e.HResult)
        while ($e.InnerException) { $e = $e.InnerException }
        $row.innerException = $e.GetType().FullName
        $row.innerHresult = ('0x{0:X8}' -f $e.HResult)
        $row.message = $e.Message.Substring(0, [Math]::Min(400, $e.Message.Length))
    }
    $rows += $row
}
@{os=[Environment]::OSVersion.VersionString; powershell=$PSVersionTable.PSVersion.ToString();
  process64=[Environment]::Is64BitProcess; os64=[Environment]::Is64BitOperatingSystem;
  acp=[Text.Encoding]::Default.CodePage; culture=[Globalization.CultureInfo]::CurrentCulture.Name;
  cases=$rows} | ConvertTo-Json -Depth 5 -Compress
'''


def _native_failure_diagnostic(plan, temporary):
    """Failure-only evidence, restricted to disposable fixtures; no target runs."""
    target = str(plan.get('python', ''))
    evidence = {'python_repr': repr(target), 'python_length': len(target),
                'python_exists': Path(target).is_file()}
    try:
        base = Path(temporary) / 'shortcut-diagnostics'
        base.mkdir()
        cases = [{'name': 'original', 'target': target, 'link': str(base / 'original.lnk')}]
        # Preserve the original spelling in the comparison, without normalizing
        # the production target or assuming an extended-prefix root cause.
        if target and not target.startswith('\\\\?\\'):
            extended = ('\\\\?\\UNC\\' + target[2:] if target.startswith('\\\\')
                        else '\\\\?\\' + target)
            cases.append({'name': 'original-extended', 'target': extended,
                          'link': str(base / 'original-extended.lnk')})
        for name, folder in (('ascii', 'ascii'), ('chinese', '中文'),
                             ('punctuation', "space & ! % ' project"),
                             ('full', "中文 space & ! % ' project")):
            directory = base / folder / '.venv' / 'Scripts'
            directory.mkdir(parents=True)
            for kind in ('placeholder', 'real'):
                python = directory / (kind + '.exe')
                if kind == 'placeholder':
                    python.write_bytes(b'placeholder')
                else:
                    shutil.copyfile(sys.executable, python)
                label = name + '-' + kind
                cases.append({'name': label, 'target': str(python),
                              'link': str(base / (label + '.lnk'))})
        spec, script = base / 'spec.json', base / 'probe.ps1'
        spec.write_text(json.dumps({'cases': cases}), encoding='utf-8')
        script.write_text(_SHORTCUT_DIAGNOSTIC_SCRIPT, encoding='utf-8-sig')
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        run = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-File',
                              str(script), '-Spec', str(spec)], capture_output=True,
                             timeout=30, shell=False)
        evidence['diagnostic_exit'] = run.returncode
        # Fixed matrix, bounded report, and existing redaction; never dump env.
        for label, output in (('stdout', run.stdout), ('stderr', run.stderr)):
            text = output.decode('utf-8-sig', errors='replace')
            evidence[label] = launchers.redact(text)[:20000 if label == 'stdout' else 1500]
    except Exception as exc:
        # Diagnostics must never replace the original acceptance-test failure.
        evidence['diagnostic_error'] = launchers._shortcut_output(str(exc))
    return evidence


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
        diagnostic = (_native_failure_diagnostic(plan, self.temp.name)
                      if result['status'] != 'success' else None)
        self.assertEqual(result['status'], 'success', {'result': result, 'diagnostic': diagnostic})
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


class ShortcutFailureDiagnosticTests(unittest.TestCase):
    def test_matrix_is_local_bounded_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'test-interpreter.exe'
            executable.write_bytes(b'real interpreter stand-in; never run')
            target = r"C:\fixture\中文 space & ! % ' project\.venv\Scripts\python.exe"
            completed = subprocess.CompletedProcess([], 0, b'x' * 25000, b'password=hidden')
            with patch.dict(os.environ, {'SystemRoot': temporary}), \
                    patch.object(sys, 'executable', str(executable)), \
                    patch.object(subprocess, 'run', return_value=completed) as run:
                evidence = _native_failure_diagnostic({'python': target}, temporary)
            spec = json.loads((Path(temporary) / 'shortcut-diagnostics/spec.json').read_text())
            self.assertEqual(len(spec['cases']), 10)
            self.assertEqual(spec['cases'][0]['target'], target)
            self.assertEqual(spec['cases'][1]['target'], '\\\\?\\' + target)
            for case in spec['cases']:
                self.assertTrue(Path(case['link']).is_relative_to(temporary))
            self.assertEqual(evidence['python_repr'], repr(target))
            self.assertEqual(len(evidence['stdout']), 20000)
            self.assertNotIn('hidden', evidence['stderr'])
            self.assertEqual(run.call_args.kwargs,
                             {'capture_output': True, 'timeout': 30, 'shell': False})
            self.assertNotIn('-ExecutionPolicy', run.call_args.args[0])

    def test_diagnostic_error_is_returned_not_raised(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(shutil, 'copyfile', side_effect=OSError('password=hidden')):
                evidence = _native_failure_diagnostic({'python': 'fixture.exe'}, temporary)
            self.assertIn('diagnostic_error', evidence)
            self.assertNotIn('hidden', evidence['diagnostic_error'])


if __name__ == '__main__':
    unittest.main()
