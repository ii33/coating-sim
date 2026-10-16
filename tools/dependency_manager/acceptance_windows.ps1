param(
    [Parameter(Mandatory=$true)][string]$ProjectDirectory,
    [Parameter(Mandatory=$true)][string]$BootstrapPython
)
$ErrorActionPreference = "Stop"
# Read-only source acceptance on a disposable Windows fixture with no project venv.
# Supply a known working Python explicitly: this script never approves bootstrap.
$launcher = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) "install.bat"
if (-not (Test-Path -LiteralPath $BootstrapPython -PathType Leaf)) { throw "Supply an existing bootstrap Python executable." }
$target = Join-Path $ProjectDirectory ".venv"
if (Test-Path -LiteralPath $target) { throw "Use a clean fixture project without .venv; never remove a real environment." }
$beforePath = $env:PATH
$beforePythonHome = $env:PYTHONHOME
$beforePythonPath = $env:PYTHONPATH
try {
    $env:PYTHONHOME = "C:\intentionally-missing-python"
    $env:PYTHONPATH = "C:\intentionally-missing-packages"
    & $launcher -Cli -BootstrapPython $BootstrapPython doctor --json
    if ($LASTEXITCODE -ne 0) { throw "Source terminal Python doctor failed" }
    & $launcher -Cli -BootstrapPython $BootstrapPython inspect --project $ProjectDirectory --json
    if ($LASTEXITCODE -ne 0) { throw "Independent CLI inspect failed" }
    & $launcher -Cli -BootstrapPython $BootstrapPython plan --project $ProjectDirectory --groups cpu --proxy-mode none --json
    if ($LASTEXITCODE -ne 2) { throw "Missing project environment must be clearly blocked" }
    if (Test-Path -LiteralPath $target) { throw "Read-only acceptance unexpectedly created a project environment" }
    if ($env:PATH -ne $beforePath) { throw "Process PATH changed" }
} finally {
    $env:PYTHONHOME = $beforePythonHome
    $env:PYTHONPATH = $beforePythonPath
}
Write-Host "Read-only source terminal smoke finished. Separately verify interactive install.bat prompts, cancellation, redacted logs and failure/retry on a disposable VM."
Write-Host "Runtime download and installation require explicit plan approval. Verify pinned hashes and no admin/PATH/launcher/association changes. This script never approves or installs for you."
