#requires -Version 7.0
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$PythonPath = $env:COAT_PYTHON,
    [string]$EntryScript = 'launcher.py',
    [string]$LogDirectory = 'artifacts\logs',
    [switch]$Check,
    [switch]$DryRun,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$LauncherArguments = @()
)

$ErrorActionPreference = 'Stop'
$writer = $null
$process = $null
function Resolve-LocalPath([string]$Path, [string]$Base) {
    if (-not [IO.Path]::IsPathRooted($Path)) { $Path = Join-Path $Base $Path }
    return [IO.Path]::GetFullPath($Path)
}
function Write-LaunchLine([string]$Line) {
    if ($null -ne $writer) { $writer.WriteLine($Line) }
    Write-Host $Line
}

try {
    $root = Resolve-LocalPath $ProjectRoot $PSScriptRoot
    $entry = Resolve-LocalPath $EntryScript $root
    if (-not $DryRun) {
        $logs = Resolve-LocalPath $LogDirectory $root
        [IO.Directory]::CreateDirectory($logs) | Out-Null
        $logPath = Join-Path $logs ('start-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N') + '.log')
        $writer = [IO.StreamWriter]::new($logPath, $false, [Text.UTF8Encoding]::new($false))
        $writer.AutoFlush = $true
        Write-LaunchLine "Log: $logPath"
    }
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw "Project launcher missing: $entry"
    }
    if ([string]::IsNullOrWhiteSpace($PythonPath)) {
        $candidates = @((Join-Path $root '.venv\Scripts\python.exe'), (Join-Path $root '.conda\python.exe'))
        $PythonPath = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if (-not $PythonPath) { throw 'No project Python. Create .venv or specify -PythonPath; no system Python fallback or packages are installed.' }
    }
    $python = Resolve-LocalPath $PythonPath $root
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Configured Python missing: $python"
    }
    $versionPath = Join-Path $root 'coat_sim\version.txt'
    $version = if (Test-Path -LiteralPath $versionPath -PathType Leaf) { [IO.File]::ReadAllText($versionPath).Trim() } else { 'unknown' }
    Write-LaunchLine "Project: $root"
    Write-LaunchLine "Version: $version"
    Write-LaunchLine "Python: $python"
    Write-LaunchLine "Entry: $entry"
    $arguments = @('-u', $entry) + $LauncherArguments
    if ($Check) {
        $probe = 'import json,sys; import numpy,scipy,pydantic; from PySide6 import QtCore,QtGui,QtQml,QtQuick,QtWidgets,QtSvg,QtMultimedia,QtNetwork,QtQuickWidgets; print(json.dumps({"python":sys.executable,"version":sys.version.split()[0],"qt":QtCore.qVersion()}))'
        $arguments = @('-u', '-c', $probe)
    }
    if ($DryRun) {
        Write-LaunchLine (([ordered]@{ workingDirectory = $root; python = $python; arguments = $arguments }) | ConvertTo-Json -Compress)
        exit 0
    }
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $python
    $info.WorkingDirectory = $root
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [Text.Encoding]::UTF8
    foreach ($argument in $arguments) { $info.ArgumentList.Add($argument) }
    $info.Environment['PYTHONUTF8'] = '1'
    $info.Environment['PYTHONNOUSERSITE'] = '1'
    if ($IsWindows) { $info.Environment['QT_QPA_PLATFORM'] = 'windows' }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    if (-not $process.Start()) { throw 'Python process did not start' }
    Write-LaunchLine "PID: $($process.Id)"
    $readers = @($process.StandardOutput, $process.StandardError)
    $pending = @($readers[0].ReadLineAsync(), $readers[1].ReadLineAsync())
    while ($null -ne $pending[0] -or $null -ne $pending[1]) {
        for ($index = 0; $index -lt 2; $index++) {
            if ($null -ne $pending[$index] -and $pending[$index].IsCompleted) {
                $line = $pending[$index].GetAwaiter().GetResult()
                if ($null -eq $line) { $pending[$index] = $null }
                else {
                    Write-LaunchLine $line
                    $pending[$index] = $readers[$index].ReadLineAsync()
                }
            }
        }
        $active = @($pending | Where-Object { $null -ne $_ })
        if ($active.Count) { [void][Threading.Tasks.Task]::WaitAny([Threading.Tasks.Task[]]$active, 100) }
    }
    $process.WaitForExit()
    $code = $process.ExitCode
    Write-LaunchLine "Exit code: $code"
    exit $code
} catch {
    Write-LaunchLine "ERROR: $($_.Exception.Message)"
    exit 2
} finally {
    if ($null -ne $writer) { $writer.Dispose() }
    if ($null -ne $process) { $process.Dispose() }
}
