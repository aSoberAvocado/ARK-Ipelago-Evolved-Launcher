@echo off
REM Read-only diagnostic for the "reset didn't actually reset" / "backup folder is empty"
REM problem. Reports where your world save and character profiles ACTUALLY live, whether the
REM per-map Cluster-<Map> junctions resolve, and whether every script agrees on the paths.
REM
REM This script NEVER moves, deletes or creates anything. Safe to run at any time, including
REM while the server is up (though numbers are more meaningful with it stopped).
REM
REM Optionally pass the launcher's config json to include it in the comparison:
REM   diagnose_reset.bat "E:\path\to\arkap_launcher_config.json"

setlocal
REM %~dp0 always ends in a backslash, and "...\" would escape the closing quote when
REM PowerShell re-parses the argument - hence the trailing dot.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose_reset.ps1" -ScriptsDir "%~dp0." -LauncherConfig "%~1"
echo.
pause
endlocal
