# Source-only launcher. Dot-source for offline contract tests; it never installs then.
[CmdletBinding(PositionalBinding=$false)]
param(
    [switch]$Cli,
    [string]$BootstrapPython = "",
    [string]$BootstrapProjectRoot = "",
    [ValidateSet("env", "none", "http")][string]$BootstrapProxyMode = "env",
    [string]$BootstrapProxy = "",
    [switch]$CheckOnly,
    [switch]$Help,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$ManagerArguments = @()
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$script:BootstrapLog = $null
$script:BootstrapPhase = "startup"

function Stop-CoatBootstrap([string]$Message) {
    # Only messages generated here are safe to show. Never echo raw exception/argv.
    throw [System.InvalidOperationException]::new("COAT_BOOTSTRAP: " + $Message)
}

function Write-CoatBootstrap([string]$Message) {
    [Console]::Error.WriteLine($Message)
    if ($script:BootstrapLog) {
        [IO.File]::AppendAllText($script:BootstrapLog,
            ([DateTime]::UtcNow.ToString("o") + " " + $Message + [Environment]::NewLine),
            [Text.UTF8Encoding]::new($false))
    }
}

function ConvertTo-CoatArgument([AllowEmptyString()][string]$Value) {
    # CommandLineToArgvW/CPython quoting, including quotes and trailing backslashes.
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder = [Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (2 * $slashes + 1)))
        } else {
            [void]$builder.Append(('\' * $slashes))
        }
        [void]$builder.Append($character)
        $slashes = 0
    }
    [void]$builder.Append(('\' * (2 * $slashes)))
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-CoatProcess {
    param([string]$Executable, [string[]]$Arguments, [int]$TimeoutSeconds = 20,
        [switch]$Capture, [switch]$Installer)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Executable
    $start.Arguments = (($Arguments | ForEach-Object { ConvertTo-CoatArgument $_ }) -join " ")
    $start.UseShellExecute = $false  # No runas/elevation and no shell interpolation.
    $start.CreateNoWindow = [bool]$Capture
    # -I isolates probes and the manager; also avoid contaminating the installer.
    foreach ($name in @("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")) {
        $start.EnvironmentVariables.Remove($name)
    }
    if ($Installer) {
        $installerTemporary = Split-Path -Parent $Executable
        $start.EnvironmentVariables["TEMP"] = $installerTemporary
        $start.EnvironmentVariables["TMP"] = $installerTemporary
        # Offline full installer gets neither proxy passwords nor manager secrets.
        foreach ($name in @("COAT_DEPENDENCY_PROXY", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")) {
            $start.EnvironmentVariables.Remove($name)
        }
    }
    if ($Capture) {
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    $started = $false; $completed = $false
    try {
        [void]$process.Start()
        $started = $true
        if ($Capture) {
            $stdout = $process.StandardOutput.ReadToEndAsync()
            $stderr = $process.StandardError.ReadToEndAsync()
        }
        if ($TimeoutSeconds -gt 0) {
            $finished = $process.WaitForExit($TimeoutSeconds * 1000)
        } else {
            $process.WaitForExit()
            $finished = $true
        }
        if (-not $finished) {
            # Terminate only this process tree, never search by executable name.
            if ($env:OS -eq "Windows_NT") {
                $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
                & $taskkill /PID $process.Id /T /F 2>&1 | Out-Null
                $terminationCode = $LASTEXITCODE
                Write-CoatBootstrap ("Timed-out process tree termination command exit code: " + $terminationCode)
            } else { $process.Kill() }
            $exitObserved = $process.WaitForExit(5000)
            Write-CoatBootstrap ("Timed-out parent process exit observed: " + $exitObserved)
            if ($exitObserved) { Write-CoatBootstrap ("Timed-out parent process exit code: " + $process.ExitCode) }
            Write-CoatBootstrap "Installer descendants may still require manual inspection; no automatic retry."
            Stop-CoatBootstrap "Child process timed out; no elevated retry. A partial installation must be inspected before retry."
        }
        $process.WaitForExit()  # Flush redirected streams.
        $result = @{ ExitCode = $process.ExitCode; Stdout = "" }
        if ($Capture) {
            $result.Stdout = $stdout.GetAwaiter().GetResult()
            # Consume but never log untrusted interpreter stderr (may contain secrets).
            [void]$stderr.GetAwaiter().GetResult()
        }
        $completed = $true
        return $result
    } finally {
        if ($Installer -and $started -and -not $completed) {
            # A stopped/timeout installer may leave MSI children running. Retain
            # our lock/download for inspection instead of allowing another install.
            $script:PreserveRuntimeLock = $true
        }
        $process.Dispose()
    }
}

function Get-CoatArchitecture([string]$Machine) {
    switch ($Machine.ToLowerInvariant()) {
        "amd64" { return "x86_64" }
        "x86_64" { return "x86_64" }
        "arm64" { return "arm64" }
        "aarch64" { return "arm64" }
        "x86" { return "x86" }
        "i386" { return "x86" }
        "i686" { return "x86" }
        default { return "unsupported" }
    }
}

function Assert-CoatOfficialUri([string]$Value, [string]$HostName = "www.python.org", [switch]$AllowFragment) {
    $uri = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne "https" -or $uri.Host -ne $HostName -or $uri.Port -ne 443 -or
        $uri.UserInfo -or $uri.Query -or ($uri.Fragment -and -not $AllowFragment)) {
        Stop-CoatBootstrap "Manifest/download URL is outside the fixed official HTTPS source."
    }
    return $uri
}

function Read-CoatManifest([string]$Path) {
    $manifest = [IO.File]::ReadAllText($Path) | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $manifest.platform -ne "win32" -or
        $manifest.version -notmatch '^3\.1[0-3]\.\d+$') {
        Stop-CoatBootstrap "Unsupported runtime manifest schema/platform/version."
    }
    [void](Assert-CoatOfficialUri $manifest.source)
    [void](Assert-CoatOfficialUri $manifest.license "docs.python.org")
    [void](Assert-CoatOfficialUri $manifest.documentation "docs.python.org" -AllowFragment)
    foreach ($property in $manifest.artifacts.PSObject.Properties) {
        if ($property.Name -notin @("x86_64", "x86", "arm64")) {
            Stop-CoatBootstrap "Unsupported runtime manifest architecture."
        }
        [void](Assert-CoatOfficialUri $property.Value.url)
        if ($property.Value.sha256 -cnotmatch '^[0-9a-f]{64}$') {
            Stop-CoatBootstrap "Invalid pinned runtime SHA-256."
        }
    }
    return $manifest
}

function Test-CoatPython {
    param([string]$Executable, [string]$Architecture, [bool]$RequireGui = $false)
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $null }
    # On CPython <=3.13, -S hides the venv prefix change. Inspect its config
    # before probing so project/other venv executables cannot masquerade as base Python.
    $executableDirectory = Split-Path -Parent ([IO.Path]::GetFullPath($Executable))
    foreach ($configurationDirectory in @($executableDirectory, (Split-Path -Parent $executableDirectory))) {
        if ($configurationDirectory -and (Test-Path -LiteralPath (Join-Path $configurationDirectory "pyvenv.cfg"))) { return $null }
    }
    # App Execution Aliases must not open the Store or trigger an unbounded prompt.
    if ($Executable -match '(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]') { return $null }
    $code = @'
import json, os, platform, struct, sys
venv_config = any(os.path.isfile(os.path.join(directory,"pyvenv.cfg")) for directory in (os.path.dirname(sys.executable),os.path.dirname(os.path.dirname(sys.executable))))
if venv_config:
 print(json.dumps({"venv_config":True})); sys.exit(2)
try:
 import venv, ensurepip
 machine = platform.machine().lower()
 architecture = "x86" if struct.calcsize("P") == 4 else {"amd64":"x86_64","x86_64":"x86_64","arm64":"arm64","aarch64":"arm64"}.get(machine,"unsupported")
 print(json.dumps({"venv_config":venv_config,"version":platform.python_version(),"implementation":platform.python_implementation(),"platform":sys.platform,"architecture":architecture,"prefix":sys.prefix,"base_prefix":sys.base_prefix,"executable":sys.executable,"venv":True,"ensurepip":bool(ensurepip.version())}))
except Exception:
 sys.exit(2)
'@
    try {
        $mode = "cli"  # Legacy positional flag retained; bootstrap never needs a GUI.
        $result = Invoke-CoatProcess $Executable @("-I", "-S", "-B", "-c", $code, $mode) -Capture
        if ($result.ExitCode -ne 0) { return $null }
        $identity = $result.Stdout | ConvertFrom-Json
        if ($identity.implementation -ne "CPython" -or $identity.platform -ne "win32" -or
            $identity.version -notmatch '^3\.1[0-3]\.\d+$' -or
            $identity.architecture -ne $Architecture -or -not $identity.venv -or
            -not $identity.ensurepip -or
            $identity.venv_config -or $identity.prefix -ne $identity.base_prefix) { return $null }
        return $identity
    } catch { return $null }
}

function Get-CoatRegisteredPython([string]$Minor = "") {
    # Read both registry views/hives and architecture suffixes. Empty or stale
    # registrations still block installing that minor version (maintenance risk).
    $found = @()
    foreach ($hive in @([Microsoft.Win32.RegistryHive]::CurrentUser,
            [Microsoft.Win32.RegistryHive]::LocalMachine)) {
        foreach ($view in @([Microsoft.Win32.RegistryView]::Registry32,
                [Microsoft.Win32.RegistryView]::Registry64)) {
            $base = $null; $core = $null
            try {
                $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
                $core = $base.OpenSubKey("Software\Python\PythonCore", $false)
                if ($null -eq $core) { continue }
                foreach ($tag in $core.GetSubKeyNames()) {
                    if ($Minor) {
                        if ($tag -notmatch ("^" + [regex]::Escape($Minor) + "(?:t)?(?:$|-)")) { continue }
                    } elseif ($tag -notmatch '^3\.1[0-3](?:t)?(?:$|-)') { continue }
                    $key = $core.OpenSubKey($tag + "\InstallPath", $false)
                    $executable = ""
                    try {
                        if ($key) {
                            $executable = [string]$key.GetValue("ExecutablePath", "")
                            if (-not $executable) {
                                $directory = [string]$key.GetValue("", "")
                                if ($directory) { $executable = Join-Path $directory "python.exe" }
                            }
                        }
                    } finally { if ($key) { $key.Dispose() } }
                    $found += [pscustomobject]@{ Tag = $tag; Executable = $executable }
                }
            } catch {
                Stop-CoatBootstrap "Python registry registration could not be checked; no installer will run."
            } finally {
                if ($core) { $core.Dispose() }
                if ($base) { $base.Dispose() }
            }
        }
    }
    return $found
}

function Find-CoatPython {
    param([string]$Explicit, [string]$ManagedDirectory, [string]$Architecture,
        [bool]$RequireGui = $false)
    if ($Explicit) {
        $identity = Test-CoatPython $Explicit $Architecture $RequireGui
        if (-not $identity) {
            Stop-CoatBootstrap "Explicit Python is incompatible or unusable. Select a normal matching-architecture CPython 3.10-3.13 with venv and ensurepip."
        }
        return $identity
    }
    $candidates = @((Join-Path $ManagedDirectory "python.exe"))
    foreach ($name in @("python.exe", "python3.exe")) {
        foreach ($command in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $candidates += $command.Source
        }
    }
    foreach ($registration in @(Get-CoatRegisteredPython)) {
        if ($registration.Executable) { $candidates += $registration.Executable }
    }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        $identity = Test-CoatPython $candidate $Architecture $RequireGui
        if ($identity) { return $identity }
    }
    return $null
}

function Assert-CoatUserPath([string]$Path, [string]$HomeDirectory, [switch]$AllowHomeDirectory) {
    $absolute = [IO.Path]::GetFullPath($Path)
    $homeDirectory = [IO.Path]::GetFullPath($HomeDirectory).TrimEnd('\', '/')
    $isHomeDirectory = [string]::Equals($absolute.TrimEnd('\', '/'), $homeDirectory,
        [StringComparison]::OrdinalIgnoreCase)
    $isDescendant = -not $isHomeDirectory -and $absolute.StartsWith(
        $homeDirectory + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
    if (-not ($isDescendant -or ($AllowHomeDirectory -and $isHomeDirectory)) -or $absolute.StartsWith('\\')) {
        Stop-CoatBootstrap "Managed files must be on an unlinked local path inside the selected project installation root."
    }
    $ancestor = $absolute
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Stop-CoatBootstrap "Managed path/ancestor is a link or reparse point; no overwrite."
            }
        }
        $parent = [IO.Path]::GetDirectoryName($ancestor)
        if ($parent -eq $ancestor) { break }
        $ancestor = $parent
    }
    return $absolute
}

function Assert-CoatInstallTarget([string]$Directory, [string]$HomeDirectory, [string]$Version) {
    [void](Assert-CoatUserPath $Directory $HomeDirectory)
    if (Test-Path -LiteralPath $Directory) {
        Stop-CoatBootstrap "Managed runtime directory already exists but is unusable; inspect it or select another existing Python. No directory is overwritten or removed."
    }
    $minor = ($Version.Split('.')[0..1] -join '.')
    if (@(Get-CoatRegisteredPython $minor).Count -gt 0) {
        Stop-CoatBootstrap "A CPython of the manifest minor version is already registered. Select or repair that interpreter manually; installation could maintain the existing environment."
    }
}

function Test-CoatUcrtVersion([int]$Major, [int]$Minor, [int]$Build, [int]$Revision) {
    # Match the manifest installer's CRTInstalled condition (see bootstrap doc).
    return ($Major -eq 10 -and $Minor -eq 0 -and $Build -ge 10137 -and $Revision -ge 0)
}

function Assert-CoatWindowsPrerequisites {
    if ($env:OS -ne "Windows_NT" -or $PSVersionTable.PSVersion -lt [version]"5.1") {
        Stop-CoatBootstrap "Windows PowerShell 5.1 or newer is required."
    }
    if ([Environment]::OSVersion.Version -lt [version]"6.3") {
        Stop-CoatBootstrap "The managed CPython installer requires Windows 8.1 or newer. No alternate runtime will be installed."
    }
    if (-not ("CoatBootstrapNative" -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CoatBootstrapNative {
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 public static extern IntPtr LoadLibraryW(string filename);
 [DllImport("kernel32.dll")]
 public static extern bool FreeLibrary(IntPtr handle);
}
'@
    }
    # VC runtime DLLs come from Include_exe=1 into the Python install directory;
    # System32/vcruntime140.dll is not a prerequisite. System UCRT stays required
    # so this launcher never attempts the installer's optional OS-CRT deployment.
    $ucrtPath = Join-Path $env:SystemRoot "System32\ucrtbase.dll"
    $handle = [CoatBootstrapNative]::LoadLibraryW($ucrtPath)
    if ($handle -eq [IntPtr]::Zero) {
        Stop-CoatBootstrap "Windows UCRT prerequisite is missing or unverifiable. Ask your administrator to complete the official OS update; no elevation is attempted."
    }
    [void][CoatBootstrapNative]::FreeLibrary($handle)
    $fileVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo($ucrtPath)
    if (-not (Test-CoatUcrtVersion $fileVersion.FileMajorPart $fileVersion.FileMinorPart $fileVersion.FileBuildPart $fileVersion.FilePrivatePart)) {
        Stop-CoatBootstrap "Windows UCRT prerequisite is too old or unverifiable. Ask your administrator to complete the official OS update; no elevation is attempted."
    }
}

function Get-CoatDownloadProxy {
    param([string]$Mode, [string]$Endpoint)
    if ($Mode -ne "http" -and $Endpoint) {
        Stop-CoatBootstrap "BootstrapProxy is valid only with BootstrapProxyMode http."
    }
    if ($Mode -eq "none") { return $null }
    $fromArgument = [bool]$Endpoint
    if ($Mode -eq "http") {
        if (-not $Endpoint) { $Endpoint = $env:COAT_DEPENDENCY_PROXY }
    } else {
        $values = @()
        foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY")) {
            $value = [Environment]::GetEnvironmentVariable($name)
            if ($value) { $values += $value.Trim() }
        }
        if (-not $values.Count -and $env:ALL_PROXY) { $values += $env:ALL_PROXY.Trim() }
        $values = @($values | Select-Object -Unique)
        if ($values.Count -gt 1) {
            Stop-CoatBootstrap "Environment proxies must use one consistent HTTP CONNECT endpoint."
        }
        $Endpoint = if ($values.Count) { $values[0] } else { "" }
    }
    if (-not $Endpoint) {
        if ($Mode -eq "http") { Stop-CoatBootstrap "HTTP proxy mode requires a proxy endpoint (or COAT_DEPENDENCY_PROXY)." }
        return $null
    }
    $uri = $null
    if (-not [Uri]::TryCreate($Endpoint, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne "http" -or -not $uri.Host -or
        $uri.AbsolutePath -ne "/" -or $uri.Query -or $uri.Fragment) {
        Stop-CoatBootstrap "Only http:// CONNECT proxies without paths/query/fragments are supported."
    }
    if ($fromArgument -and $uri.UserInfo) {
        Stop-CoatBootstrap "Proxy credentials must stay in COAT_DEPENDENCY_PROXY or proxy environment variables, never command-line arguments."
    }
    # Respect NO_PROXY for this one official destination; never read system proxy.
    if ($Mode -eq "env" -and $env:NO_PROXY) {
        foreach ($entry in $env:NO_PROXY.Split(',')) {
            $entry = $entry.Trim().ToLowerInvariant()
            if ($entry -eq "*" -or $entry -eq "www.python.org" -or
                $entry.TrimStart('.') -eq "python.org") { return $null }
        }
    }
    $builder = [UriBuilder]::new($uri)
    $builder.UserName = ""; $builder.Password = ""
    $proxy = [Net.WebProxy]::new($builder.Uri, $false)
    if ($uri.UserInfo) {
        $parts = $uri.UserInfo.Split([char[]]@(':'), 2)
        $user = [Uri]::UnescapeDataString($parts[0])
        $password = if ($parts.Length -gt 1) { [Uri]::UnescapeDataString($parts[1]) } else { "" }
        $proxy.Credentials = [Net.NetworkCredential]::new($user, $password)
    }
    return $proxy
}

function Save-CoatInstaller {
    param($Artifact, [string]$Destination, $Proxy)
    $uri = Assert-CoatOfficialUri $Artifact.url
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $response = $null; $output = $null
    try {
        for ($redirect = 0; $redirect -le 5; $redirect++) {
            if ($watch.Elapsed.TotalSeconds -gt 180) { Stop-CoatBootstrap "Official runtime download timed out." }
            $request = [Net.HttpWebRequest]::Create($uri)
            $request.Proxy = $Proxy
            $request.AllowAutoRedirect = $false
            $request.Timeout = 10000
            $request.ReadWriteTimeout = 10000
            $request.UserAgent = "DepositionDependencyManager/1"
            try { $response = $request.GetResponse() }
            catch { Stop-CoatBootstrap "Official runtime download failed (network, TLS or proxy). No file will be executed." }
            $status = [int]$response.StatusCode
            if ($status -in @(301, 302, 303, 307, 308)) {
                $location = $response.Headers["Location"]
                $response.Dispose(); $response = $null
                if (-not $location -or $redirect -eq 5) { Stop-CoatBootstrap "Official runtime redirect limit exceeded." }
                $uri = Assert-CoatOfficialUri ([Uri]::new($uri, $location).AbsoluteUri)
                continue
            }
            if ($status -ne 200) { Stop-CoatBootstrap "Official runtime download returned a non-success response." }
            break
        }
        if ($response.ContentLength -gt 134217728) { Stop-CoatBootstrap "Official runtime download exceeds the 128 MiB limit." }
        $output = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $downloadStream = $response.GetResponseStream()
        $buffer = [byte[]]::new(65536)
        $total = 0L
        try {
            while (($count = $downloadStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                if ($watch.Elapsed.TotalSeconds -gt 180) { Stop-CoatBootstrap "Official runtime download exceeded the 180-second limit." }
                $total += $count
                if ($total -gt 134217728) { Stop-CoatBootstrap "Official runtime download exceeds the 128 MiB limit." }
                $output.Write($buffer, 0, $count)
            }
        } finally { $downloadStream.Dispose() }
        $output.Dispose(); $output = $null
        Assert-CoatInstallerHash $Destination $Artifact.sha256
    } catch {
        if ($output) { $output.Dispose(); $output = $null }
        if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
        throw
    } finally {
        if ($response) { $response.Dispose() }
        if ($output) { $output.Dispose() }
        $watch.Stop()
    }
}

function Assert-CoatInstallerHash([string]$Path, [string]$Expected) {
    # No optional cmdlet/module autoload: Windows PowerShell can inherit a
    # Core-only PSModulePath. Hash a read-only stream using .NET Framework APIs.
    $stream = $null; $sha256 = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $sha256 = [Security.Cryptography.SHA256]::Create()
        $digest = [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
    } finally {
        if ($stream) { $stream.Dispose() }
        if ($sha256) { $sha256.Dispose() }
    }
    if ($Expected -cnotmatch '^[0-9a-f]{64}$' -or $digest -cne $Expected) {
        Stop-CoatBootstrap "Runtime SHA-256 mismatch; the downloaded file will not be executed."
    }
}

function Get-CoatInstallerArguments([string]$Directory, [string]$LogPath = "") {
    $logging = if ($LogPath) { @("/log", $LogPath) } else { @() }
    return $logging + @("/quiet", "InstallAllUsers=0", ("TargetDir=" + $Directory),
        "PrependPath=0", "AppendPath=0", "Include_launcher=0", "InstallLauncherAllUsers=0",
        "AssociateFiles=0", "Shortcuts=0", "Include_pip=1", "Include_exe=1", "Include_lib=1",
        "Include_dev=1", "Include_tcltk=0", "Include_doc=0", "Include_test=0", "Include_tools=0")
}

function Install-CoatRuntime {
    param($Manifest, [string]$Architecture, [string]$Directory, [string]$HomeDirectory,
        $Proxy, [bool]$RequireGui = $false)
    Assert-CoatInstallTarget $Directory $HomeDirectory $Manifest.version
    Assert-CoatWindowsPrerequisites
    $artifactProperty = $Manifest.artifacts.PSObject.Properties[$Architecture]
    if (-not $artifactProperty) { Stop-CoatBootstrap "No official runtime artifact for this Windows architecture." }
    $parent = Split-Path -Parent $Directory
    [void][IO.Directory]::CreateDirectory($parent)
    # A direct-child target has the home directory itself as its parent. This
    # exception is parent-only; Assert-CoatInstallTarget still requires a descendant.
    [void](Assert-CoatUserPath $parent $HomeDirectory -AllowHomeDirectory)
    $lockPath = Join-Path $parent "runtime-install.lock"
    $lock = $null; $temporary = $null
    $script:PreserveRuntimeLock = $false
    try {
        try { $lock = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None) }
        catch { Stop-CoatBootstrap "Runtime install lock exists or cannot be acquired. Inspect any interrupted installer before retry; locks are not removed automatically." }
        Assert-CoatInstallTarget $Directory $HomeDirectory $Manifest.version
        $temporary = Join-Path $parent ("download-" + [Guid]::NewGuid().ToString("N"))
        [void][IO.Directory]::CreateDirectory($temporary)
        [void](Assert-CoatUserPath $temporary $HomeDirectory)
        $installer = Join-Path $temporary "python-installer.exe"
        Write-CoatBootstrap ("No usable manager Python. Automatically installing official CPython " + $Manifest.version + " to " + $Directory)
        Write-CoatBootstrap ("Source: " + $Manifest.source)
        Write-CoatBootstrap ("License: " + $Manifest.license + "; continuing accepts the standard installer license.")
        Write-CoatBootstrap "Per-user maintenance/uninstall records are created. No PATH, launcher, file associations, shortcuts or elevation. The installer runs in the terminal and does not require Tcl/Tk."
        $script:BootstrapPhase = "runtime_download"
        Save-CoatInstaller $artifactProperty.Value $installer $Proxy
        # Recheck after network wait: never maintain another registered CPython.
        Assert-CoatInstallTarget $Directory $HomeDirectory $Manifest.version
        Assert-CoatWindowsPrerequisites
        $script:BootstrapPhase = "runtime_install"
        # Keep vendor logs outside the disposable download directory, including
        # successful package logs and diagnostics from interrupted installers.
        $installerLog = Join-Path $parent ("python-install-" + [Guid]::NewGuid().ToString("N") + ".log")
        Write-CoatBootstrap ("Phase: runtime_install. Installer log: " + $installerLog)
        Write-CoatBootstrap "Waiting up to 30 minutes for the signed installer; Windows package verification can take several minutes per component."
        $result = Invoke-CoatProcess $installer (Get-CoatInstallerArguments $Directory $installerLog) -TimeoutSeconds 1800 -Capture -Installer
        Write-CoatBootstrap ("CPython installer process exit code: " + $result.ExitCode)
        if ($result.ExitCode -ne 0) {
            Stop-CoatBootstrap ("CPython installer returned " + $result.ExitCode + ". No elevated/reboot retry; inspect any partial installation before retry.")
        }
        $identity = Test-CoatPython (Join-Path $Directory "python.exe") $Architecture $RequireGui
        if (-not $identity -or $identity.version -ne $Manifest.version -or
            [IO.Path]::GetFullPath($identity.prefix).TrimEnd('\') -ne [IO.Path]::GetFullPath($Directory).TrimEnd('\')) {
            Stop-CoatBootstrap "Installed CPython identity or venv/ensurepip check failed; partial files are preserved for inspection."
        }
        Write-CoatBootstrap ("CPython license accepted: " + $Manifest.license)
        return $identity
    } finally {
        if ($temporary -and -not $script:PreserveRuntimeLock) {
            # Only the unique download directory this invocation created is removed.
            [void](Assert-CoatUserPath $temporary $HomeDirectory)
            Remove-Item -LiteralPath $temporary -Recurse -Force
        }
        if ($lock) {
            $lock.Dispose()
            if (-not $script:PreserveRuntimeLock) { Remove-Item -LiteralPath $lockPath -Force }
            else { Write-CoatBootstrap "Installer termination is uncertain; the install lock and download are retained for manual inspection." }
        }
    }
}

function Invoke-CoatBootstrap {
    if ($Help) {
        [Console]::WriteLine("dependency_manager.cmd [-Cli] [-BootstrapPython <python.exe>] [-BootstrapProjectRoot <project-directory>] [-BootstrapProxyMode env|none|http] [-BootstrapProxy <credential-free http://host:port>] [-CheckOnly] [manager operation and flags]")
        [Console]::WriteLine("Default: interactive terminal installer. -Cli: manager CLI (inspect by default). -CheckOnly: read-only Python check, never download/install. Source-only; existing PowerShell policy is preserved.")
        return 0
    }
    try {
        if ($env:OS -ne "Windows_NT" -or $PSVersionTable.PSVersion -lt [version]"5.1") {
            Stop-CoatBootstrap "This source launcher requires Windows PowerShell 5.1 or newer."
        }
        $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
        $manifest = Read-CoatManifest (Join-Path $PSScriptRoot "runtime_manifest.json")
        $machine = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        $architecture = Get-CoatArchitecture $machine
        if ($architecture -eq "unsupported" -or -not $manifest.artifacts.PSObject.Properties[$architecture]) {
            Stop-CoatBootstrap "Unsupported Windows architecture; no alternate runtime will be installed."
        }
        # Before Python starts, the source root is known. An explicit selected
        # installation root takes precedence; never silently use AppData.
        $homeDirectory = if ($BootstrapProjectRoot) { [IO.Path]::GetFullPath($BootstrapProjectRoot) } else { $root }
        $base = Assert-CoatUserPath (Join-Path $homeDirectory ".runtime") $homeDirectory
        $directory = Join-Path $base "python"
        if (-not $CheckOnly) {
            $logs = Assert-CoatUserPath (Join-Path $homeDirectory ".dependency-manager\logs") $homeDirectory
            [void][IO.Directory]::CreateDirectory($logs)
            $script:BootstrapLog = Join-Path $logs ("bootstrap-" + [Guid]::NewGuid().ToString("N") + ".log")
            Write-CoatBootstrap ("Bootstrap log: " + $script:BootstrapLog)
        }
        $script:BootstrapPhase = "python_probe"
        $explicit = if ($BootstrapPython) { $BootstrapPython } else { $env:COAT_DEPENDENCY_PYTHON }
        $identity = Find-CoatPython $explicit $directory $architecture $false
        if ($CheckOnly) {
            $status = if ($identity) { "ready" } else { "missing" }
            [Console]::WriteLine((@{ schema_version = 1; operation = "bootstrap_check"; status = $status;
                architecture = $architecture; python = $identity; managed_directory = $directory;
                manifest_version = $manifest.version; installed = $false } | ConvertTo-Json -Depth 4 -Compress))
            return $(if ($identity) { 0 } else { 2 })
        }
        if (-not $identity) {
            $proxy = Get-CoatDownloadProxy $BootstrapProxyMode $BootstrapProxy
            $identity = Install-CoatRuntime $manifest $architecture $directory $homeDirectory $proxy $false
        }
        Write-CoatBootstrap ("Manager Python: " + $identity.executable + " (" + $identity.version + ")")
        $script:BootstrapPhase = "manager_launch"
        $arguments = @($ManagerArguments)
        $hasManagerProject = @($arguments | Where-Object { $_ -eq "--project" -or $_.StartsWith("--project=") }).Count -gt 0
        if (-not $arguments.Count) { $arguments = @($(if ($Cli) { "inspect" } else { "interactive" })) }
        if ($BootstrapProjectRoot -and -not $hasManagerProject) { $arguments += @("--project", $homeDirectory) }
        # -I -S prevents sitecustomize/user site/PYTHONPATH imports. Add only the known
        # source root explicitly, then execute the existing stdlib-only entry point.
        $entry = "import os,runpy,sys; root=sys.argv.pop(1); sys.path.insert(0,root); entry=os.path.join(root,'dependency_manager.py'); sys.argv[0]=entry; runpy.run_path(entry,run_name='__main__')"
        $result = Invoke-CoatProcess $identity.executable (@("-I", "-S", "-B", "-c", $entry, $root) + $arguments) -TimeoutSeconds 0
        return $result.ExitCode
    } catch {
        $message = $_.Exception.Message
        if ($message.StartsWith("COAT_BOOTSTRAP: ")) { $message = $message.Substring(16) }
        else { $message = "Bootstrap failed during " + $script:BootstrapPhase + ". No elevated retry or security-setting change; check the documented prerequisites." }
        try { Write-CoatBootstrap $message } catch { [Console]::Error.WriteLine($message) }
        return 2
    }
}

if ($MyInvocation.InvocationName -ne ".") { exit (Invoke-CoatBootstrap) }
