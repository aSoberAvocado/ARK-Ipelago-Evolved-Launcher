@echo off
REM Start the ARK: Survival Evolved dedicated server (Pre-Aquatica, ArkApi).
REM Run this AFTER SteamCMD finishes AND ArkApi (version.dll) is installed in Win64.
REM BattlEye is OFF - required for ArkApi.

setlocal
REM SERVER_ROOT / ADMINPASS / SERVERPASS / CLUSTERID / CLUSTERDIR / SAVESROOT all come
REM from paths.cmd now - edit that ONE file (or the launcher's Configuration tab) rather
REM than this copy, so every script that calls it always agrees.
call "%~dp0paths.cmd"
REM ---- edit these (per-script only) ---------------------------------------
set "MAP=TheIsland"
set "SESSION=ArchipelagoSolo"
set "MAXPLAYERS=5"
set "GAMEPORT=7777"
set "QUERYPORT=27015"
set "RCONPORT=27020"
REM How long obelisk uploads (items/dinos/characters) survive before expiring.
REM Default is 24h - too short for a solo cluster. 2592000 = 30 days.
set "TRIBUTEEXP=2592000"
REM ------------------------------------------------------------------------

REM Optional 1st argument overrides MAP (used by switch_map.bat). Double-clicking this file
REM directly still launches the default MAP above, unchanged.
if not "%~1"=="" set "MAP=%~1"

set "EXE=%SERVER_ROOT%\ShooterGame\Binaries\Win64\ShooterGameServer.exe"
if not exist "%EXE%" (
    echo ShooterGameServer.exe not found at:
    echo   %EXE%
    echo Wait for SteamCMD to finish, or fix SERVER_ROOT.
    goto end
)

REM Per-map save dir: real folder in SAVESROOT, junction inside Saved so ARK can use it.
REM SAVESROOT must be a FULL path (X:\...): a relative or empty value would anchor the
REM junction against whatever folder this script runs from, scattering saves silently.
if "%SAVESROOT%"=="" goto badsaves
if not "%SAVESROOT:~1,1%"==":" goto badsaves
set "MAPSAVEDIR=%SAVESROOT%\%MAP%"
set "JUNCTION=%SERVER_ROOT%\ShooterGame\Saved\Cluster-%MAP%"
if not exist "%SERVER_ROOT%\ShooterGame\Saved" mkdir "%SERVER_ROOT%\ShooterGame\Saved"
if not exist "%MAPSAVEDIR%" mkdir "%MAPSAVEDIR%"
if not exist "%JUNCTION%" mklink /J "%JUNCTION%" "%MAPSAVEDIR%" >nul
REM A junction that doesn't resolve (stale target from an old SAVESROOT, or a failed
REM mklink) makes ARK save into the void - re-link it, then verify or refuse to launch.
REM (rmdir on a junction removes only the link itself, never the target's contents.)
if not exist "%JUNCTION%\" (
    rmdir "%JUNCTION%" 2>nul
    mklink /J "%JUNCTION%" "%MAPSAVEDIR%" >nul
)
if not exist "%JUNCTION%\" goto badjunction

REM Build the ? option string. ServerPassword line is added only if set.
set "OPTS=%MAP%?listen?SessionName=%SESSION%?Port=%GAMEPORT%?QueryPort=%QUERYPORT%?MaxPlayers=%MAXPLAYERS%?AltSaveDirectoryName=Cluster-%MAP%?RCONEnabled=True?RCONPort=%RCONPORT%?ServerAdminPassword=%ADMINPASS%"
if not "%SERVERPASS%"=="" set "OPTS=%OPTS%?ServerPassword=%SERVERPASS%"
set "OPTS=%OPTS%?TributeItemExpirationSeconds=%TRIBUTEEXP%?TributeDinoExpirationSeconds=%TRIBUTEEXP%?TributeCharacterExpirationSeconds=%TRIBUTEEXP%"

REM Cluster flags (only if CLUSTERID is set). The cluster folder MUST exist before the
REM server starts: with -ClusterDirOverride pointing at a path that doesn't exist (or an
REM empty one, which is what you get when CLUSTERDIR was never filled in), ARK stalls on
REM its launch sequence and never finishes booting - no error, no crash, just a hang. So
REM the mkdir below is checked, and a failure stops here with a real message instead of
REM launching into that hang. SteamCMD never creates these folders (they live outside the
REM install tree entirely), so on a fresh install the launcher creates them right after
REM "Install ARK Server" - this is the fallback for hand-edited/relocated setups.
set "CLUSTERARGS="
if not "%CLUSTERID%"=="" (
    if "%CLUSTERDIR%"=="" goto badcluster
    if not "%CLUSTERDIR:~1,1%"==":" goto badcluster
    if not exist "%CLUSTERDIR%" mkdir "%CLUSTERDIR%"
    if not exist "%CLUSTERDIR%" goto badcluster
    set "CLUSTERARGS=-ClusterId=%CLUSTERID% -ClusterDirOverride="%CLUSTERDIR%" -NoTransferFromFiltering"
)

echo Launching: %SESSION% on %MAP%  (game %GAMEPORT% / query %QUERYPORT% / rcon %RCONPORT%)
echo Save dir: %MAPSAVEDIR%
if not "%CLUSTERID%"=="" echo Cluster: %CLUSTERID%  (%CLUSTERDIR%)
echo.
"%EXE%" "%OPTS%" -server -log -NoBattlEye %CLUSTERARGS%
goto end

:badcluster
echo.
echo Cluster folder is not usable, so the server was NOT started.
echo   CLUSTERDIR=%CLUSTERDIR%
echo.
echo Starting with a missing cluster folder makes ARK hang on its launch sequence with
echo no error, and a RELATIVE path here silently builds a second cluster folder inside
echo ShooterGame\Saved - so this script stops instead. Fix it one of these ways:
echo   * ARKipelago Launcher -^> Configuration tab: set CLUSTERDIR to a full path
echo     (like E:\ARK\ServerCluster\ClusterData), then Save.
echo   * Create the folder above by hand (full path, starting with a drive letter).
echo   * Clear CLUSTERID in paths.cmd to run without clustering at all.
goto end

:badsaves
echo.
echo SAVESROOT is not usable, so the server was NOT started.
echo   SAVESROOT=%SAVESROOT%
echo.
echo It must be a FULL path starting with a drive letter (like E:\ARK\ServerCluster\Saves).
echo A blank or relative value would scatter world saves into whatever folder this script
echo happens to run from. Set it on the launcher's Configuration tab, then Save.
goto end

:badjunction
echo.
echo The per-map save junction does not resolve, so the server was NOT started:
echo   %JUNCTION%  -^>  %MAPSAVEDIR%
echo.
echo ARK would launch but silently fail to save the world. Delete the Cluster-%MAP%
echo entry inside ShooterGame\Saved by hand (rmdir removes only the link, never your
echo saves) and re-run this script to recreate it cleanly.

:end
endlocal
pause
