@echo off
setlocal
rem Managed Python defaults to this source project; -BootstrapProjectRoot selects another installation root.
rem Use native Windows PowerShell; do not override the machine's execution policy.
set "COAT_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "COAT_POWERSHELL=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%COAT_POWERSHELL%" (
  >&2 echo Windows PowerShell 5.1 is required. No settings have been changed.
  exit /b 2
)
"%COAT_POWERSHELL%" -NoLogo -NoProfile -File "%~dp0tools\dependency_manager\bootstrap_windows.ps1" %*
set "COAT_EXIT=%ERRORLEVEL%"
if "%COAT_EXIT%"=="1" (
  >&2 echo If PowerShell reported a script policy or signature block, ask your administrator for an approved signed script. See docs\dependency-manager-bootstrap.md.
  >&2 echo This launcher does not change or bypass execution policy. With Python already installed, python dependency_manager.py is an alternative source entry point.
)
exit /b %COAT_EXIT%
