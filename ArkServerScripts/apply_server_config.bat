@echo off
REM Apply the recommended ARK server settings (serverconfig\Game.ini.settings +
REM GameUserSettings.ini.settings) into your real Game.ini / GameUserSettings.ini.
REM
REM Safe key-level merge: only the keys listed in the .settings templates are updated/added;
REM everything else in your config is left alone, and a one-level .bak backup is made first.
REM
REM !! STOP the ARK server first (it rewrites the config on shutdown and would overwrite you).
REM Edit the values in serverconfig\*.settings to taste BEFORE running this.

setlocal
REM ---- edit if your path differs ----
set "SERVER_ROOT=E:\ARK\Server"
REM -----------------------------------
set "CONFIGDIR=%SERVER_ROOT%\ShooterGame\Saved\Config\WindowsServer"

echo Applying recommended server settings to:
echo   %CONFIGDIR%
echo (Make sure the ARK server is STOPPED first.)
echo.
pause

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply_server_config.ps1" -ConfigDir "%CONFIGDIR%"

echo.
pause
endlocal
