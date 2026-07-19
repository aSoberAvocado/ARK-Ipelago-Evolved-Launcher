@echo off
REM Start the ARK: Survival Evolved dedicated server (Pre-Aquatica, ArkApi).
REM Run this AFTER SteamCMD finishes AND ArkApi (version.dll) is installed in Win64.
REM BattlEye is OFF - required for ArkApi.

setlocal
REM ---- edit these ---------------------------------------------------------
set "SERVER_ROOT=E:\ARK\Server"
set "MAP=TheIsland"
set "SESSION=ArchipelagoSolo"
set "MAXPLAYERS=5"
set "GAMEPORT=7777"
set "QUERYPORT=27015"
set "RCONPORT=27020"
set "ADMINPASS=changeme_admin"
set "SERVERPASS="
REM Cluster: same ClusterId + ClusterDirOverride on EVERY map's launch = uploads/downloads
REM (via Obelisk/transfer terminal) carry over between maps. Leave ClusterId blank to
REM disable clustering entirely.
set "CLUSTERID=MyCluster"
set "CLUSTERDIR=E:\ARK\ServerCluster\ClusterData"
REM Per-map save separation: every map keeps its world + player profiles in its own folder
REM under SAVESROOT (physically outside ShooterGame\Saved). ARK only accepts save dir names
REM relative to ShooterGame\Saved, so a junction Cluster-<Map> is created there pointing at
REM the real folder. Characters move between maps ONLY via Obelisk upload/download.
set "SAVESROOT=E:\ARK\ServerCluster\Saves"
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
set "MAPSAVEDIR=%SAVESROOT%\%MAP%"
set "JUNCTION=%SERVER_ROOT%\ShooterGame\Saved\Cluster-%MAP%"
if not exist "%SERVER_ROOT%\ShooterGame\Saved" mkdir "%SERVER_ROOT%\ShooterGame\Saved"
if not exist "%MAPSAVEDIR%" mkdir "%MAPSAVEDIR%"
if not exist "%JUNCTION%" mklink /J "%JUNCTION%" "%MAPSAVEDIR%" >nul

REM Build the ? option string. ServerPassword line is added only if set.
set "OPTS=%MAP%?listen?SessionName=%SESSION%?Port=%GAMEPORT%?QueryPort=%QUERYPORT%?MaxPlayers=%MAXPLAYERS%?AltSaveDirectoryName=Cluster-%MAP%?RCONEnabled=True?RCONPort=%RCONPORT%?ServerAdminPassword=%ADMINPASS%"
if not "%SERVERPASS%"=="" set "OPTS=%OPTS%?ServerPassword=%SERVERPASS%"
set "OPTS=%OPTS%?TributeItemExpirationSeconds=%TRIBUTEEXP%?TributeDinoExpirationSeconds=%TRIBUTEEXP%?TributeCharacterExpirationSeconds=%TRIBUTEEXP%"

REM Cluster flags (only if CLUSTERID is set).
set "CLUSTERARGS="
if not "%CLUSTERID%"=="" (
    if not exist "%CLUSTERDIR%" mkdir "%CLUSTERDIR%"
    set "CLUSTERARGS=-ClusterId=%CLUSTERID% -ClusterDirOverride=%CLUSTERDIR% -NoTransferFromFiltering"
)

echo Launching: %SESSION% on %MAP%  (game %GAMEPORT% / query %QUERYPORT% / rcon %RCONPORT%)
echo Save dir: %MAPSAVEDIR%
if not "%CLUSTERID%"=="" echo Cluster: %CLUSTERID%  (%CLUSTERDIR%)
echo.
"%EXE%" "%OPTS%" -server -log -NoBattlEye %CLUSTERARGS%

:end
endlocal
pause
