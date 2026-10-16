"""Offline source launcher contracts; PowerShell harness never installs/downloads."""

import json
import locale
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/dependency_manager/bootstrap_windows.ps1"
CMD = ROOT / "dependency_manager.cmd"
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)


def _native_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8-sig")
    except UnicodeDecodeError:
        return value.decode(locale.getencoding(), errors="replace")


class BootstrapSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = SCRIPT.read_text(encoding="utf-8")

    def test_install_bat_preserves_percent_paths_without_call_expansion(self):
        source = (ROOT / "install.bat").read_text(encoding="utf-8")
        self.assertIn("setlocal DisableDelayedExpansion", source)
        self.assertIn('"%~dp0dependency_manager.cmd" %*', source)
        self.assertNotRegex(source, r"(?im)^\s*call\s")
        self.assertNotIn("-ExecutionPolicy", source)

    def test_cmd_uses_native_powershell_without_policy_override(self):
        text = CMD.read_text(encoding="utf-8")
        self.assertIn("Sysnative\\WindowsPowerShell", text)
        self.assertIn('-NoProfile -File "%~dp0tools\\dependency_manager\\bootstrap_windows.ps1" %*', text)
        self.assertIn("exit /b %COAT_EXIT%", text)
        self.assertIn("approved signed script", text)
        self.assertNotRegex(text + self.source, r"(?i)-ExecutionPolicy|Set-ExecutionPolicy|ScriptBlock\]::Create")

    def test_one_official_manifest_no_independent_python_pin(self):
        manifest = json.loads((SCRIPT.parent / "runtime_manifest.json").read_text())
        self.assertIn('Read-CoatManifest (Join-Path $PSScriptRoot "runtime_manifest.json")', self.source)
        self.assertNotIn(manifest["version"], self.source)
        for artifact in manifest["artifacts"].values():
            self.assertNotIn(artifact["sha256"], self.source)
            self.assertNotIn(artifact["url"], self.source)

    def test_only_documentation_validation_allows_official_fragment(self):
        manifest = json.loads((SCRIPT.parent / "runtime_manifest.json").read_text())
        self.assertTrue(manifest["documentation"].endswith("#installing-without-ui"))
        self.assertIn('Assert-CoatOfficialUri $manifest.documentation "docs.python.org" -AllowFragment', self.source)
        self.assertIn('($uri.Fragment -and -not $AllowFragment)', self.source)
        self.assertIn('Assert-CoatOfficialUri $manifest.source)', self.source)
        self.assertIn('Assert-CoatOfficialUri $property.Value.url)', self.source)
        self.assertIn('Assert-CoatOfficialUri $Artifact.url', self.source)
        self.assertEqual(self.source.count("-AllowFragment"), 1)

    def test_isolated_stdlib_probe_and_manager_entry_are_executable(self):
        probe_match = re.search(r"\$code = @'\n(.*?)\n'@", self.source, re.S)
        assert probe_match is not None
        probe = probe_match.group(1)
        entry_match = re.search(r'\$entry = "([^"]+)"', self.source)
        assert entry_match is not None
        entry = entry_match.group(1)
        self.assertIn('@("-I", "-S", "-B", "-c", $code, $mode)', self.source)
        self.assertIn('@("-I", "-S", "-B", "-c", $entry, $root)', self.source)
        with tempfile.TemporaryDirectory(prefix="coat bootstrap 中文 ") as temporary:
            directory = Path(temporary)
            marker = directory / "unexpected-sitecustomize"
            (directory / "sitecustomize.py").write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()", encoding="utf-8")
            environment = dict(os.environ, PYTHONHOME=str(directory / "missing"), PYTHONPATH=str(directory))
            result = subprocess.run([BASE_PYTHON, "-I", "-S", "-B", "-c", probe, "cli"], env=environment,
                                    capture_output=True, text=True, timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr)
            identity = json.loads(result.stdout)
            self.assertTrue(identity["venv"] and identity["ensurepip"])
            self.assertNotIn("tkinter", probe)
            self.assertNotIn("gui_verified", identity)
            # Execute the actual source entry from a path with spaces/non-ASCII text.
            shutil.copy(ROOT / "dependency_manager.py", directory)
            (directory / "tools").mkdir()
            shutil.copytree(SCRIPT.parent, directory / "tools/dependency_manager", ignore=shutil.ignore_patterns("__pycache__"))
            result = subprocess.run([BASE_PYTHON, "-I", "-S", "-B", "-c", entry, str(directory), "doctor", "--json"],
                                    env=environment, capture_output=True, text=True, timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "success")
            self.assertFalse(marker.exists())

    def test_real_venv_is_detected_even_with_site_initialization_disabled(self):
        probe_match = re.search(r"\$code = @'\n(.*?)\n'@", self.source, re.S)
        assert probe_match is not None
        probe = probe_match.group(1)
        with tempfile.TemporaryDirectory(prefix="coat-venv-bootstrap-") as temporary:
            target = Path(temporary) / "venv"
            venv.EnvBuilder(with_pip=False).create(target)
            executable = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            result = subprocess.run([str(executable), "-I", "-S", "-B", "-c", probe, "cli"],
                                    capture_output=True, text=True, timeout=25)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"venv_config": True})
        self.assertIn('Join-Path $configurationDirectory "pyvenv.cfg"', self.source)
        self.assertIn('$identity.venv_config -or', self.source)

    def test_default_bootstraps_manager_then_forwards_existing_operation(self):
        self.assertIn('if (-not $identity) {', self.source)
        self.assertIn('$identity = Install-CoatRuntime $manifest', self.source)
        self.assertIn('$arguments = @($ManagerArguments)', self.source)
        self.assertIn('if ($Cli) { "inspect" } else { "interactive" }', self.source)
        self.assertIn('return $result.ExitCode', self.source)
        self.assertNotIn("project-deploy --yes", self.source)
        self.assertNotIn("-m pip install", self.source)

    def test_check_only_does_not_enter_install_path(self):
        start = self.source.index('        if ($CheckOnly) {')
        end = self.source.index('        if (-not $identity) {', start)
        block = self.source[start:end]
        self.assertIn('installed = $false', block)
        self.assertIn('return $(if ($identity) { 0 } else { 2 })', block)
        self.assertNotIn("Install-CoatRuntime", block)
        self.assertIn('if (-not $CheckOnly) {', self.source)

    def test_powershell_sources_parse_under_legacy_windows_encoding(self):
        # PowerShell 5.1 treats BOM-less scripts as the active ANSI code page.
        # Generate non-ASCII test arguments from code points at runtime instead.
        self.assertTrue(self.source.isascii())
        harness = (ROOT / "tests/dependency_bootstrap_windows_offline.ps1").read_text(encoding="utf-8")
        self.assertTrue(harness.isascii())

    def test_no_readonly_automatic_variable_assignment(self):
        # PowerShell variables are case-insensitive; $HOME and $PID are read-only.
        self.assertNotRegex(self.source, r"(?im)\$(?:home|host|pid|pshome|psversiontable)\s*=")

    def test_pin_hash_precedes_install_and_target_rechecked_after_download(self):
        install = self.source.split("function Install-CoatRuntime", 1)[1].split("function Invoke-CoatBootstrap", 1)[0]
        download = install.index("Save-CoatInstaller")
        self.assertGreater(install.index("Assert-CoatInstallTarget", download), download)
        self.assertGreater(install.index("Invoke-CoatProcess $installer", download), download)
        self.assertIn("Assert-CoatInstallerHash $Destination $Artifact.sha256", self.source)
        self.assertIn("$request.AllowAutoRedirect = $false", self.source)
        self.assertIn("$watch.Elapsed.TotalSeconds -gt 180", self.source)
        self.assertIn("$total -gt 134217728", self.source)

    def test_existing_minor_registration_and_reparse_paths_are_protected(self):
        for required in ("Registry32", "Registry64", "CurrentUser", "LocalMachine",
                         "GetSubKeyNames", "ReparsePoint", "FileMode]::CreateNew"):
            self.assertIn(required, self.source)
        self.assertIn('Get-CoatRegisteredPython $minor', self.source)
        self.assertIn('(?:$|-)', self.source)
        self.assertIn('Managed runtime directory already exists but is unusable', self.source)
        self.assertNotIn("Remove-Item -LiteralPath $Directory", self.source)
        self.assertIn('$script:PreserveRuntimeLock = $true', self.source)
        self.assertIn('if (-not $script:PreserveRuntimeLock) { Remove-Item -LiteralPath $lockPath', self.source)

    def test_hash_verification_has_no_cmdlet_module_autoload_dependency(self):
        hashing = self.source.split("function Assert-CoatInstallerHash", 1)[1].split("function Get-CoatInstallerArguments", 1)[0]
        self.assertNotIn("Get-FileHash", self.source)
        self.assertIn('[Security.Cryptography.SHA256]::Create()', hashing)
        self.assertIn('$sha256.ComputeHash($stream)', hashing)
        self.assertIn('[IO.FileMode]::Open, [IO.FileAccess]::Read', hashing)
        self.assertIn('$stream.Dispose()', hashing)
        self.assertIn('$sha256.Dispose()', hashing)
        self.assertNotRegex(self.source, r'\$env:PSModulePath\s*=')
        harness = (ROOT / "tests/dependency_bootstrap_windows_offline.ps1").read_text(encoding="utf-8")
        self.assertIn('Assert-CoatInstallerHash $hashPath "ba7816bf', harness)
        self.assertIn('Assert-CoatInstallerHash $hashPath ("0" * 64)', harness)

    def test_system_ucrt_is_required_but_global_vc_runtime_is_not(self):
        prerequisites = self.source.split("function Assert-CoatWindowsPrerequisites", 1)[1].split("function Get-CoatDownloadProxy", 1)[0]
        self.assertIn('"System32\\ucrtbase.dll"', prerequisites)
        self.assertIn('Test-CoatUcrtVersion $fileVersion.FileMajorPart', prerequisites)
        self.assertNotIn('"vcruntime140.dll"', prerequisites)
        self.assertNotIn('LoadLibraryW((Join-Path', prerequisites)
        self.assertIn('Include_exe=1', self.source)

    def test_runtime_target_stays_descendant_with_parent_only_home_exception(self):
        path_check = self.source.split("function Assert-CoatUserPath", 1)[1].split("function Assert-CoatInstallTarget", 1)[0]
        target_check = self.source.split("function Assert-CoatInstallTarget", 1)[1].split("function Test-CoatUcrtVersion", 1)[0]
        self.assertIn('$isDescendant = -not $isHomeDirectory', path_check)
        self.assertIn('$AllowHomeDirectory -and $isHomeDirectory', path_check)
        self.assertIn('ReparsePoint', path_check)
        self.assertIn('Assert-CoatUserPath $parent $HomeDirectory -AllowHomeDirectory', self.source)
        self.assertNotIn('-AllowHomeDirectory', target_check)
        self.assertEqual(self.source.count('-AllowHomeDirectory'), 1)
        harness = (ROOT / "tests/dependency_bootstrap_windows_offline.ps1").read_text(encoding="utf-8")
        self.assertIn('direct home child target accepted', harness)
        self.assertIn('Assert-CoatInstallTarget $fixture $fixture', harness)
        self.assertIn('Offline bootstrap harness failed at stage:', harness)

    def test_no_elevation_credentials_or_raw_exception_logging(self):
        self.assertIn('$start.UseShellExecute = $false', self.source)
        self.assertNotRegex(self.source, r"(?i)-Verb\s+RunAs|ServerCertificateValidationCallback|ServicePointManager.*=")
        self.assertIn('"COAT_DEPENDENCY_PROXY", "HTTP_PROXY", "HTTPS_PROXY"', self.source)
        self.assertIn('$start.EnvironmentVariables.Remove($name)', self.source)
        self.assertNotIn('Write-CoatBootstrap $Endpoint', self.source)
        self.assertNotIn('Write-CoatBootstrap $result.Stdout', self.source)
        self.assertIn('if ($message.StartsWith("COAT_BOOTSTRAP: "))', self.source)
        self.assertIn('Include_tcltk=0', self.source)
        self.assertNotIn('Windows.Forms', self.source)
        self.assertNotIn('UserInteractive', self.source)

    def test_linux_pwsh_does_not_run_windows_native_contracts(self):
        case = BootstrapSourceTests("test_native_powershell_offline_harness_when_available")
        result = unittest.TestResult()
        with patch.object(sys, "platform", "linux"), patch.object(shutil, "which", return_value="pwsh"), patch.object(subprocess, "run") as run:
            case.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("requires Windows", result.skipped[0][1])
        run.assert_not_called()

    def test_native_diagnostics_decode_utf8_and_local_encoding(self):
        self.assertEqual(_native_text(None), "")
        self.assertEqual(_native_text("error"), "error")
        self.assertEqual(_native_text("错误".encode("utf-8")), "错误")
        with patch.object(locale, "getencoding", return_value="cp936"):
            self.assertEqual(_native_text("错误".encode("cp936")), "错误")

    def test_native_powershell_offline_harness_when_available(self):
        if sys.platform != "win32":
            self.skipTest("Full native harness requires Windows registry/path contracts; Windows source CI runs it")
        executable = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not executable:
            self.skipTest("PowerShell is absent; native harness awaits Windows source CI")
        result = subprocess.run([executable, "-NoLogo", "-NoProfile", "-File",
                                 str(ROOT / "tests/dependency_bootstrap_windows_offline.ps1"),
                                 "-BootstrapScript", str(SCRIPT), "-SourcePython", sys.executable],
                                capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, _native_text(result.stdout) + _native_text(result.stderr))
        help_result = subprocess.run([executable, "-NoLogo", "-NoProfile", "-File", str(SCRIPT),
                                      "-Help", "doctor", "--json"],
                                     capture_output=True, timeout=30)
        self.assertEqual(help_result.returncode, 0, _native_text(help_result.stdout) + _native_text(help_result.stderr))
        self.assertIn("dependency_manager.cmd", _native_text(help_result.stdout))
        report = json.loads(_native_text(result.stdout).strip().splitlines()[-1])
        self.assertEqual(report["status"], "success")
        self.assertGreater(report["assertions"], 70)
        self.assertFalse(report["network_used"])
        self.assertEqual(report["real_installers_executed"], 0)


if __name__ == "__main__":
    unittest.main()
