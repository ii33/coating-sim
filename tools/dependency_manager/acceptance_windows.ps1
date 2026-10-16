param(
    [Parameter(Mandatory=$true)][string]$BundleDirectory,
    [Parameter(Mandatory=$true)][string]$ProjectDirectory
)
$ErrorActionPreference = "Stop"
# Run in a clean non-admin Windows VM with no Python/Qt/project venv.
$cli = Join-Path $BundleDirectory "deposition-deps.exe"
$gui = Join-Path $BundleDirectory "deposition-deps-gui.exe"
$target = Join-Path $ProjectDirectory ".venv"
if (Test-Path $target) { throw "Use a clean fixture project without .venv; never remove a real environment." }
$beforePath = $env:PATH
$env:PYTHONHOME = "C:\intentionally-missing-python"
$env:PYTHONPATH = "C:\intentionally-missing-packages"
& $cli doctor --json
if ($LASTEXITCODE -ne 0) { throw "Bundled Tcl/Tk runtime failed" }
& $cli inspect --project $ProjectDirectory --json | Tee-Object "dependency-inspect.json"
if ($LASTEXITCODE -ne 0) { throw "Independent CLI inspect failed" }
& $cli plan --project $ProjectDirectory --groups cpu --proxy-mode none --json | Tee-Object "dependency-plan.json"
if ($LASTEXITCODE -ne 2) { throw "Missing bootstrap must be clearly blocked" }
& $cli runtime-plan --project $ProjectDirectory --backend directml --proxy-mode none --json | Tee-Object "dependency-directml-blocked.json"
if ($LASTEXITCODE -ne 2) { throw "DirectML must be blocked before downloading managed 3.13" }
Start-Process $gui -ArgumentList @("gui", "--project", $ProjectDirectory) -Wait
if ($env:PATH -ne $beforePath) { throw "Process PATH changed" }
Write-Host "Read-only smoke finished. Manually verify: paths readable at 100/150/200% DPI; keyboard Tab/F5/Escape; plan cancel; slow benchmark cancel; close/cancel while busy; masked credentials absent from logs; no-source recommendation until user accepts."
Write-Host "Then explicitly approve the runtime plan on this disposable VM, verify pinned version/hash, venv creation/repair, partial failure/retry, no admin request/PATH/launcher/association changes. This script never approves or installs for you."
