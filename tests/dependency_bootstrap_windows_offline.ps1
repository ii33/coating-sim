# No Pester, network, installation, registry writes or project dependency required.
param(
    [Parameter(Mandatory=$true)][string]$BootstrapScript,
    [Parameter(Mandatory=$true)][string]$SourcePython
)
$ErrorActionPreference = "Stop"
. $BootstrapScript
$script:Assertions = 0
$script:OfflineStage = "pure_helpers"
function Assert-CoatTest([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Offline bootstrap contract failed: $Message" }
    $script:Assertions++
}
function Assert-CoatThrows([scriptblock]$Action, [string]$Expected) {
    $caught = $false
    try { & $Action } catch {
        $caught = $true
        Assert-CoatTest ($_.Exception.Message.Contains($Expected)) "Expected safe blocker: $Expected"
    }
    Assert-CoatTest $caught "Expected action to be blocked"
}
$fixture = Join-Path ([IO.Path]::GetTempPath()) ("coat-bootstrap-test-" + [Guid]::NewGuid().ToString("N"))
[void][IO.Directory]::CreateDirectory($fixture)
$originalProxy = @{}
foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "COAT_DEPENDENCY_PROXY")) {
    $originalProxy[$name] = [Environment]::GetEnvironmentVariable($name)
    [Environment]::SetEnvironmentVariable($name, $null)
}
try {
    Assert-CoatTest ((ConvertTo-CoatArgument "") -ceq '""') "empty argv"
    Assert-CoatTest ((ConvertTo-CoatArgument "simple") -ceq "simple") "plain argv"
    Assert-CoatTest ((ConvertTo-CoatArgument 'a b\') -ceq '"a b\\"') "trailing slash argv"
    Assert-CoatTest ((ConvertTo-CoatArgument 'a"b') -ceq '"a\"b"') "quote argv"
    $argvFixture = @("", "space path", 'a"b', 'a b\', 'space " quote\', (-join @([char]0x4e2d, [char]0x6587, [char]0x8def, [char]0x5f84)))
    $echo = "import json,sys;print(json.dumps(sys.argv[1:],ensure_ascii=True))"
    $echoResult = Invoke-CoatProcess $SourcePython (@("-I", "-S", "-c", $echo) + $argvFixture) -Capture
    Assert-CoatTest ($echoResult.ExitCode -eq 0) "native argv echo"
    # Windows PowerShell 5.1 emits a JSON array as one pipeline object;
    # wrapping that pipeline in @() adds a spurious extra array dimension.
    $echoed = ConvertFrom-Json -InputObject $echoResult.Stdout
    Assert-CoatTest ($echoed.Count -eq $argvFixture.Count) ("argv count preserved: expected " + $argvFixture.Count + "; actual " + $echoed.Count + "; synthetic echo " + $echoResult.Stdout)
    for ($index = 0; $index -lt $argvFixture.Count; $index++) {
        Assert-CoatTest ($echoed[$index] -ceq $argvFixture[$index]) "argv item $index preserved"
    }
    foreach ($machine in @("AMD64", "x86_64")) {
        Assert-CoatTest ((Get-CoatArchitecture $machine) -eq "x86_64") "x64 architecture"
    }
    Assert-CoatTest ((Get-CoatArchitecture "ARM64") -eq "arm64") "ARM64 architecture"
    Assert-CoatTest ((Get-CoatArchitecture "x86") -eq "x86") "x86 architecture"
    Assert-CoatTest ((Get-CoatArchitecture "unknown") -eq "unsupported") "unknown architecture"
    if ($env:OS -eq "Windows_NT") {
        Assert-CoatWindowsPrerequisites  # Real system DLL metadata, no installer/settings change.
        Assert-CoatTest $true "native system UCRT readiness verified"
    }
    Assert-CoatTest (Test-CoatUcrtVersion 10 0 10137 0) "UCRT readiness threshold accepted"
    Assert-CoatTest (Test-CoatUcrtVersion 10 0 26100 1) "current UCRT accepted"
    Assert-CoatTest (-not (Test-CoatUcrtVersion 10 0 10136 99)) "old UCRT rejected"
    Assert-CoatTest (-not (Test-CoatUcrtVersion 9 0 30000 0)) "unsupported UCRT rejected"
    $manifestPath = Join-Path (Split-Path -Parent $BootstrapScript) "runtime_manifest.json"
    $manifest = Read-CoatManifest $manifestPath
    $documentation = Assert-CoatOfficialUri $manifest.documentation "docs.python.org" -AllowFragment
    Assert-CoatTest ($documentation.Fragment -eq "#installing-without-ui") "current official documentation anchor accepted"
    Assert-CoatThrows { Assert-CoatOfficialUri $manifest.documentation "docs.python.org" } "fixed official HTTPS"
    Assert-CoatThrows { Assert-CoatOfficialUri "https://docs.python.org/3.13/using/windows.html?token=unused#installing-without-ui" "docs.python.org" -AllowFragment } "fixed official HTTPS"
    foreach ($artifact in $manifest.artifacts.PSObject.Properties) {
        [void](Assert-CoatOfficialUri $artifact.Value.url)
        Assert-CoatTest ($artifact.Value.sha256 -cmatch '^[0-9a-f]{64}$') "manifest hash"
    }
    foreach ($bad in @("http://www.python.org/file", "https://evil.example/file",
            "https://user:pass@www.python.org/file", "https://www.python.org:444/file",
            "https://www.python.org/file?token=unused", "https://www.python.org/file#fragment")) {
        Assert-CoatThrows { Assert-CoatOfficialUri $bad } "fixed official HTTPS"
    }
    $invalid = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    $invalid.artifacts.x86_64.sha256 = "invalid"
    $invalidPath = Join-Path $fixture "manifest.json"
    $invalid | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $invalidPath -Encoding UTF8
    Assert-CoatThrows { Read-CoatManifest $invalidPath } "SHA-256"
    $script:OfflineStage = "managed_path_boundary"
    $directChild = Join-Path $fixture "direct-child-runtime"
    Assert-CoatTest ((Assert-CoatUserPath $directChild $fixture) -eq [IO.Path]::GetFullPath($directChild)) "direct home child target accepted"
    Assert-CoatTest ((Assert-CoatUserPath $fixture $fixture -AllowHomeDirectory) -eq [IO.Path]::GetFullPath($fixture)) "home accepted only for parent verification"
    Assert-CoatThrows { Assert-CoatUserPath $fixture $fixture } "inside the current user's home"
    Assert-CoatThrows { Assert-CoatUserPath ($fixture + [IO.Path]::DirectorySeparatorChar) $fixture } "inside the current user's home"
    Assert-CoatThrows { Assert-CoatInstallTarget $fixture $fixture $manifest.version } "inside the current user's home"
    $script:OfflineStage = "hash_and_proxy"
    $hashPath = Join-Path $fixture "hash.bin"
    [IO.File]::WriteAllBytes($hashPath, [Text.Encoding]::ASCII.GetBytes("abc"))
    Assert-CoatInstallerHash $hashPath "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    Assert-CoatThrows { Assert-CoatInstallerHash $hashPath ("0" * 64) } "SHA-256 mismatch"
    $installerArguments = @(Get-CoatInstallerArguments "C:\User Space\runtime")
    foreach ($required in @("InstallAllUsers=0", "TargetDir=C:\User Space\runtime", "PrependPath=0",
            "AppendPath=0", "Include_launcher=0", "InstallLauncherAllUsers=0", "AssociateFiles=0",
            "Shortcuts=0", "Include_pip=1", "Include_dev=1", "Include_tcltk=1")) {
        Assert-CoatTest ($required -in $installerArguments) "safe installer option $required"
    }
    Assert-CoatTest ($null -eq (Get-CoatDownloadProxy "none" "")) "direct proxy mode"
    Assert-CoatTest ($null -eq (Get-CoatDownloadProxy "env" "")) "empty env means direct"
    Assert-CoatThrows { Get-CoatDownloadProxy "http" "socks5://127.0.0.1:1080" } "CONNECT"
    Assert-CoatThrows { Get-CoatDownloadProxy "http" "https://127.0.0.1:1080" } "CONNECT"
    Assert-CoatThrows { Get-CoatDownloadProxy "http" "http://unused-user:unused-password@127.0.0.1:1080" } "never command-line"
    [Environment]::SetEnvironmentVariable("COAT_DEPENDENCY_PROXY", "http://unused-user:unused-password@127.0.0.1:1080")
    $proxy = Get-CoatDownloadProxy "http" ""
    Assert-CoatTest (-not $proxy.Address.UserInfo) "proxy URI strips userinfo"
    Assert-CoatTest ($proxy.Credentials.UserName -eq "unused-user") "authentication remains in memory"
    [Environment]::SetEnvironmentVariable("HTTP_PROXY", "http://127.0.0.1:1080")
    [Environment]::SetEnvironmentVariable("HTTPS_PROXY", "http://127.0.0.1:1081")
    Assert-CoatThrows { Get-CoatDownloadProxy "env" "" } "one consistent"
    [Environment]::SetEnvironmentVariable("HTTPS_PROXY", "http://127.0.0.1:1080")
    [Environment]::SetEnvironmentVariable("NO_PROXY", ".python.org")
    Assert-CoatTest ($null -eq (Get-CoatDownloadProxy "env" "")) "NO_PROXY respected"

    $script:OfflineStage = "python_probe"
    # Probe decisions are tested with a synthetic interpreter response, no executable install.
    $originalProcess = (Get-Item Function:Invoke-CoatProcess).ScriptBlock
    $fakePython = Join-Path $fixture "python.exe"
    [IO.File]::WriteAllText($fakePython, "offline fixture, never executed")
    $script:ProbeIdentity = @{ version = "3.12.14"; implementation = "CPython"; platform = "win32";
        architecture = "x86_64"; prefix = $fixture; base_prefix = $fixture; executable = $fakePython;
        venv = $true; ensurepip = $true; tk = $true; gui_verified = $true; venv_config = $false }
    function Invoke-CoatProcess {
        param([string]$Executable, [string[]]$Arguments, [int]$TimeoutSeconds = 20,
            [switch]$Capture, [switch]$Installer)
        Assert-CoatTest ("-I" -in $Arguments -and "-S" -in $Arguments) "isolated probe"
        return @{ ExitCode = 0; Stdout = ($script:ProbeIdentity | ConvertTo-Json -Compress) }
    }
    try {
        Assert-CoatTest ($null -ne (Test-CoatPython $fakePython "x86_64" $true)) "valid interpreter"
        [IO.File]::WriteAllText((Join-Path $fixture "pyvenv.cfg"), "home = fake")
        Assert-CoatTest ($null -eq (Test-CoatPython $fakePython "x86_64" $true)) "reject adjacent venv cfg under -S"
        Remove-Item -LiteralPath (Join-Path $fixture "pyvenv.cfg")
        $venvDirectory = Join-Path $fixture "fake-venv"
        $scriptsDirectory = Join-Path $venvDirectory "Scripts"
        [void][IO.Directory]::CreateDirectory($scriptsDirectory)
        $venvPython = Join-Path $scriptsDirectory "python.exe"
        [IO.File]::WriteAllText($venvPython, "never executed")
        [IO.File]::WriteAllText((Join-Path $venvDirectory "pyvenv.cfg"), "home = fake")
        Assert-CoatTest ($null -eq (Test-CoatPython $venvPython "x86_64" $true)) "reject parent venv cfg under -S"
        $script:ProbeIdentity.venv_config = $true
        Assert-CoatTest ($null -eq (Test-CoatPython $fakePython "x86_64" $true)) "reject probed venv config"
        $script:ProbeIdentity.venv_config = $false
        foreach ($field in @("venv", "ensurepip", "tk", "gui_verified")) {
            $script:ProbeIdentity[$field] = $false
            Assert-CoatTest ($null -eq (Test-CoatPython $fakePython "x86_64" $true)) "reject missing $field"
            $script:ProbeIdentity[$field] = $true
        }
        foreach ($version in @("3.9.9", "3.14.0", "3.12.14rc1")) {
            $script:ProbeIdentity.version = $version
            Assert-CoatTest ($null -eq (Test-CoatPython $fakePython "x86_64" $true)) "reject unsupported version"
        }
        $script:ProbeIdentity.version = "3.12.14"
        Assert-CoatTest ($null -eq (Test-CoatPython $fakePython "arm64" $true)) "reject wrong architecture"
        $script:ProbeIdentity.base_prefix = Join-Path $fixture "base"
        Assert-CoatTest ($null -eq (Test-CoatPython $fakePython "x86_64" $true)) "reject venv as tool Python"
        $script:ProbeIdentity.base_prefix = $fixture
        $script:ProbeIdentity.gui_verified = $false
        Assert-CoatTest ($null -ne (Test-CoatPython $fakePython "x86_64" $false)) "CLI validates Tcl without desktop"
    } finally { Set-Item Function:Invoke-CoatProcess -Value $originalProcess }

    # All installation effects below are mocked. Only this disposable fixture changes.
    $originalRegistry = (Get-Item Function:Get-CoatRegisteredPython).ScriptBlock
    $originalPrerequisites = (Get-Item Function:Assert-CoatWindowsPrerequisites).ScriptBlock
    $originalDownload = (Get-Item Function:Save-CoatInstaller).ScriptBlock
    $originalProbe = (Get-Item Function:Test-CoatPython).ScriptBlock
    $script:Registrations = @()
    $script:InstallerCalls = 0
    $script:DownloadFailure = $false
    $script:RegisterDuringDownload = $false
    $script:InstallerExit = 0
    $script:InstallerUncertain = $false
    function Get-CoatRegisteredPython([string]$Minor = "") { return $script:Registrations }
    function Assert-CoatWindowsPrerequisites { }
    function Save-CoatInstaller {
        param($Artifact, [string]$Destination, $Proxy)
        if ($script:DownloadFailure) { Stop-CoatBootstrap "Synthetic download failed" }
        [IO.File]::WriteAllText($Destination, "synthetic verified download, never executed")
        if ($script:RegisterDuringDownload) { $script:Registrations = @(@{ Tag = "3.13-32"; Executable = "" }) }
    }
    function Invoke-CoatProcess {
        param([string]$Executable, [string[]]$Arguments, [int]$TimeoutSeconds = 20,
            [switch]$Capture, [switch]$Installer)
        $script:InstallerCalls++
        if ($script:InstallerUncertain) {
            $script:PreserveRuntimeLock = $true
            Stop-CoatBootstrap "Synthetic installer timeout"
        }
        Assert-CoatTest $Installer "installer-specific credential-free environment"
        $targetArgument = @($Arguments | Where-Object { $_.StartsWith("TargetDir=") })[0]
        $targetDirectory = $targetArgument.Substring(10)
        [void][IO.Directory]::CreateDirectory($targetDirectory)
        [IO.File]::WriteAllText((Join-Path $targetDirectory "python.exe"), "partial mock installation")
        return @{ ExitCode = $script:InstallerExit; Stdout = "" }
    }
    function Test-CoatPython {
        param([string]$Executable, [string]$Architecture, [bool]$RequireGui = $true)
        return [pscustomobject]@{ version = $manifest.version; prefix = (Split-Path -Parent $Executable);
            executable = $Executable; gui_verified = $RequireGui }
    }
    try {
        $script:OfflineStage = "mock_installer_success"
        $target = Join-Path $fixture "managed-success"
        [void](Install-CoatRuntime $manifest "x86_64" $target $fixture $null $false)
        Assert-CoatTest ($script:InstallerCalls -eq 1) "one mock installer invocation"
        Assert-CoatTest (Test-Path -LiteralPath (Join-Path $target "python.exe")) "installed identity retained"
        Assert-CoatTest (-not (Test-Path -LiteralPath (Join-Path $fixture "runtime-install.lock"))) "owned lock released"
        Assert-CoatThrows { Assert-CoatInstallTarget $target $fixture $manifest.version } "already exists"
        $script:Registrations = @(@{ Tag = "3.13-arm64"; Executable = "" })
        Assert-CoatThrows { Assert-CoatInstallTarget (Join-Path $fixture "new-target") $fixture $manifest.version } "already registered"
        $script:Registrations = @()
        $script:OfflineStage = "registration_race"
        $script:RegisterDuringDownload = $true
        Assert-CoatThrows { Install-CoatRuntime $manifest "x86_64" (Join-Path $fixture "race") $fixture $null $false } "already registered"
        Assert-CoatTest ($script:InstallerCalls -eq 1) "registration race blocks execution"
        $script:RegisterDuringDownload = $false; $script:Registrations = @()
        $script:OfflineStage = "download_failure"
        $script:DownloadFailure = $true
        Assert-CoatThrows { Install-CoatRuntime $manifest "x86_64" (Join-Path $fixture "download-failure") $fixture $null $false } "Synthetic download failed"
        Assert-CoatTest ($script:InstallerCalls -eq 1) "download failure blocks execution"
        $script:DownloadFailure = $false; $script:InstallerExit = 1603
        $script:OfflineStage = "partial_installer"
        $partialTarget = Join-Path $fixture "partial"
        Assert-CoatThrows { Install-CoatRuntime $manifest "x86_64" $partialTarget $fixture $null $false } "1603"
        Assert-CoatTest (Test-Path -LiteralPath $partialTarget) "partial installation preserved"
        $lockPath = Join-Path $fixture "runtime-install.lock"
        $script:OfflineStage = "uncertain_installer"
        $script:InstallerUncertain = $true
        Assert-CoatThrows { Install-CoatRuntime $manifest "x86_64" (Join-Path $fixture "uncertain") $fixture $null $false } "Synthetic installer timeout"
        Assert-CoatTest (Test-Path -LiteralPath $lockPath) "uncertain installer lock retained"
        Assert-CoatTest (@(Get-ChildItem -LiteralPath $fixture -Filter "download-*").Count -eq 1) "uncertain installer download retained"
        # Mock process has no children; explicit fixture cleanup is safe here.
        Remove-Item -LiteralPath $lockPath
        Get-ChildItem -LiteralPath $fixture -Filter "download-*" | Remove-Item -Recurse -Force
        $script:InstallerUncertain = $false
        $script:OfflineStage = "foreign_lock"
        [IO.File]::WriteAllText($lockPath, "another invocation owns this")
        Assert-CoatThrows { Install-CoatRuntime $manifest "x86_64" (Join-Path $fixture "locked") $fixture $null $false } "lock exists"
        Assert-CoatTest (Test-Path -LiteralPath $lockPath) "foreign lock preserved"
        Assert-CoatTest (-not @(Get-ChildItem -LiteralPath $fixture -Filter "download-*").Count) "owned downloads cleaned"
        Assert-CoatThrows { Assert-CoatUserPath (Join-Path ([IO.Path]::GetTempPath()) "outside") $fixture } "inside the current user's home"
    } finally {
        Set-Item Function:Get-CoatRegisteredPython -Value $originalRegistry
        Set-Item Function:Assert-CoatWindowsPrerequisites -Value $originalPrerequisites
        Set-Item Function:Save-CoatInstaller -Value $originalDownload
        Set-Item Function:Test-CoatPython -Value $originalProbe
        Set-Item Function:Invoke-CoatProcess -Value $originalProcess
    }
    [Console]::WriteLine((@{ status = "success"; assertions = $script:Assertions;
        network_used = $false; real_installers_executed = 0 } | ConvertTo-Json -Compress))
} catch {
    [Console]::Error.WriteLine("Offline bootstrap harness failed at stage: " + $script:OfflineStage)
    throw
} finally {
    foreach ($name in $originalProxy.Keys) { [Environment]::SetEnvironmentVariable($name, $originalProxy[$name]) }
    Remove-Item -LiteralPath $fixture -Recurse -Force
}
