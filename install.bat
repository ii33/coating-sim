@echo off
setlocal DisableDelayedExpansion
rem Tail-chain directly: CALL would expand percent signs in the source path a second time.
rem The target CMD owns the exit code; no execution-policy override.
"%~dp0dependency_manager.cmd" %*
