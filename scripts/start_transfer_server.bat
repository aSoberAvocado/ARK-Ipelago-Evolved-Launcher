@echo off
REM Temporary "bridge" server used ONLY to complete a live character transfer (Obelisk ->
REM "Travel to Another Server" needs a genuinely live target session - unlike item/dino tribute,
REM which is file-based and doesn't need this).
REM
REM Workflow:
REM   1. Main server is running, you're connected/playing on it.
REM   2. Run:  start_transfer_server.bat <TargetMap>   (e.g. ScorchedEarth_P)
REM   3. Wait for it to fully boot (console shows the map loaded).
REM   4. In-game on MAIN server: Obelisk -> Travel to Another Server -> select this session ->
REM      Join with Survivor. Your character (and tribute items/dinos, if deposited) transfer in.
REM   5. Once you've loaded into the new map's world, run 'saveworld' in THIS console to flush
REM      the transfer to disk, THEN stop this transfer server (Ctrl+C / close the window, or
REM      taskkill ShooterGameServer.exe). Skipping saveworld risks losing the transfer on close.
REM   6. Run switch_map.bat (or start_ase_server.bat) targeting the SAME map for your real/main
REM      server - both use the same per-map save folder (SAVESROOT\<Map>), so it picks up the
REM      exact save this bridge server just wrote. Nothing to copy - reconnect there and continue.
REM
REM Usage: start_transfer_server.bat <MapID>   (e.g. start_transfer_server.bat ScorchedEarth_P)

setlocal enabledelayedexpansion
set "MAP=%~1"
if not "%MAP%"=="" goto gotmap

REM ---- no argument given (double-clicked) - show a menu instead ----
set "MAP1=TheIsland"        & set "NAME1=The Island"
set "MAP2=TheCenter"        & set "NAME2=The Center"
set "MAP3=ScorchedEarth_P"  & set "NAME3=Scorched Earth"
set "MAP4=Ragnarok"         & set "NAME4=Ragnarok"
set "MAP5=Aberration_P"     & set "NAME5=Aberration"
set "MAP6=Extinction"       & set "NAME6=Extinction"
set "MAP7=Valguero_P"       & set "NAME7=Valguero"
set "MAP8=Genesis"          & set "NAME8=Genesis: Part 1"
set "MAP9=Gen2"             & set "NAME9=Genesis: Part 2"
set "MAP10=CrystalIsles"    & set "NAME10=Crystal Isles"
set "MAP11=LostIsland"      & set "NAME11=Lost Island"
set "MAP12=Fjordur"         & set "NAME12=Fjordur"

:menu
echo Choose the TARGET map for the bridge server:
for /l %%i in (1,1,12) do echo   %%i^) !NAME%%i!  ^(!MAP%%i!^)
echo.
set /p "CHOICE=Number: "
set "MAP="
for /l %%i in (1,1,12) do if "%CHOICE%"=="%%i" set "MAP=!MAP%%i!"
if "%MAP%"=="" (
    echo Invalid choice.
    goto menu
)

:gotmap
REM ---- must match start_ase_server.bat's SERVER_ROOT + cluster settings exactly ----
set "SERVER_ROOT=E:\ARK\Server"
set "SESSION=ArchipelagoSolo-Bridge"
set "MAXPLAYERS=2"
REM distinct ports so this can run ALONGSIDE the main server briefly.
REM NOTE: ASE claims the game port AND game port +1 (raw UDP), so main on 7777 also
REM occupies 7778 - the bridge must start at 7779 or higher (it will use 7779+7780).
set "GAMEPORT=7779"
set "QUERYPORT=27016"
set "RCONPORT=27021"
set "ADMINPASS=changeme_admin"
REM If your ports are forwarded on the router (internet-visible), keep a password set here.
set "SERVERPASS="
set "CLUSTERID=MyCluster"
set "CLUSTERDIR=E:\ARK\ServerCluster\ClusterData"
set "SAVESROOT=E:\ARK\ServerCluster\Saves"
REM ------------------------------------------------------------------------

set "EXE=%SERVER_ROOT%\ShooterGame\Binaries\Win64\ShooterGameServer.exe"
if not exist "%EXE%" (
    echo ShooterGameServer.exe not found at %EXE%
    goto end
)
if not exist "%CLUSTERDIR%" mkdir "%CLUSTERDIR%"

REM Per-map save dir: real folder in SAVESROOT, junction inside Saved so ARK can use it.
REM Must match start_ase_server.bat exactly so main + bridge share each map's world.
set "MAPSAVEDIR=%SAVESROOT%\%MAP%"
set "JUNCTION=%SERVER_ROOT%\ShooterGame\Saved\Cluster-%MAP%"
if not exist "%SERVER_ROOT%\ShooterGame\Saved" mkdir "%SERVER_ROOT%\ShooterGame\Saved"
if not exist "%MAPSAVEDIR%" mkdir "%MAPSAVEDIR%"
if not exist "%JUNCTION%" mklink /J "%JUNCTION%" "%MAPSAVEDIR%" >nul

set "OPTS=%MAP%?listen?SessionName=%SESSION%?Port=%GAMEPORT%?QueryPort=%QUERYPORT%?MaxPlayers=%MAXPLAYERS%?AltSaveDirectoryName=Cluster-%MAP%?RCONEnabled=True?RCONPort=%RCONPORT%?ServerAdminPassword=%ADMINPASS%"
if not "%SERVERPASS%"=="" set "OPTS=%OPTS%?ServerPassword=%SERVERPASS%"
REM Match start_ase_server.bat: obelisk uploads survive 30 days instead of the 24h default.
set "OPTS=%OPTS%?TributeItemExpirationSeconds=2592000?TributeDinoExpirationSeconds=2592000?TributeCharacterExpirationSeconds=2592000"

echo Launching BRIDGE server: %SESSION% on %MAP%  (game %GAMEPORT% / query %QUERYPORT%)
echo Save dir: %MAPSAVEDIR%
echo Cluster: %CLUSTERID%  (%CLUSTERDIR%)
echo Run 'saveworld' in this console BEFORE closing it once your character has transferred in.
echo.
"%EXE%" "%OPTS%" -server -log -NoBattlEye -ClusterId=%CLUSTERID% -ClusterDirOverride=%CLUSTERDIR% -NoTransferFromFiltering

:end
endlocal
pause
