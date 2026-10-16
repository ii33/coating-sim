@echo off
setlocal
set "COAT_START_PWSH=%COAT_PWSH%"
if not defined COAT_START_PWSH set "COAT_START_PWSH=pwsh.exe"
"%COAT_START_PWSH%" -NoLogo -NoProfile -File "%~dp0start-dev.ps1" %*
set "COAT_START_RESULT=%ERRORLEVEL%"
if not "%COAT_START_RESULT%"=="0" (
    echo CoatSim could not start. Review the diagnostic message and log above.
    echo PowerShell 7 or later is required; set COAT_PWSH to its executable if it is not on PATH.
    pause
)
exit /b %COAT_START_RESULT%
