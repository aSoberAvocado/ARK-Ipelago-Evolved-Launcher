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
REM SERVER_ROOT / ADMINPASS / SERVERPASS / CLUSTERID / CLUSTERDIR / SAVESROOT come from
REM paths.cmd now - edit that ONE file (or the launcher's Configuration tab) rather than
REM this copy, so this always matches start_ase_server.bat exactly.
call "%~dp0paths.cmd"
REM ---- edit these (per-script only - this bridge deliberately runs its own SESSION /
REM ports / MAXPLAYERS alongside the main server) --------------------------
set "SESSION=ArchipelagoSolo-Bridge"
set "MAXPLAYERS=2"
REM distinct ports so this can run ALONGSIDE the main server briefly.
REM NOTE: ASE claims the game port AND game port +1 (raw UDP), so main on 7777 also
REM occupies 7778 - the bridge must start at 7779 or higher (it will use 7779+7780).
set "GAMEPORT=7779"
set "QUERYPORT=27016"
set "RCONPORT=27021"
REM ------------------------------------------------------------------------

set "EXE=%SERVER_ROOT%\ShooterGame\Binaries\Win64\ShooterGameServer.exe"
if not exist "%EXE%" (
    echo ShooterGameServer.exe not found at %EXE%
    goto end
)
REM Same guard as start_ase_server.bat: ARK hangs on its launch sequence (no error) when
REM -ClusterDirOverride points at a missing/empty path, so stop here rather than launch
REM into a silent stall. SteamCMD never creates this folder - the launcher does, right
REM after "Install ARK Server".
if "%CLUSTERDIR%"=="" goto badcluster
if not "%CLUSTERDIR:~1,1%"==":" goto badcluster
if not exist "%CLUSTERDIR%" mkdir "%CLUSTERDIR%"
if not exist "%CLUSTERDIR%" goto badcluster

REM Per-map save dir: real folder in SAVESROOT, junction inside Saved so ARK can use it.
REM Must match start_ase_server.bat exactly so main + bridge share each map's world.
REM SAVESROOT must be a FULL path (X:\...) for the same reason as CLUSTERDIR above.
if "%SAVESROOT%"=="" goto badsaves
if not "%SAVESROOT:~1,1%"==":" goto badsaves
set "MAPSAVEDIR=%SAVESROOT%\%MAP%"
set "JUNCTION=%SERVER_ROOT%\ShooterGame\Saved\Cluster-%MAP%"
if not exist "%SERVER_ROOT%\ShooterGame\Saved" mkdir "%SERVER_ROOT%\ShooterGame\Saved"
if not exist "%MAPSAVEDIR%" mkdir "%MAPSAVEDIR%"
if not exist "%JUNCTION%" mklink /J "%JUNCTION%" "%MAPSAVEDIR%" >nul
REM Self-heal a junction that doesn't resolve (stale target from an old SAVESROOT),
REM then verify - launching through a dangling junction makes ARK save into the void.
if not exist "%JUNCTION%\" (
    rmdir "%JUNCTION%" 2>nul
    mklink /J "%JUNCTION%" "%MAPSAVEDIR%" >nul
)
if not exist "%JUNCTION%\" goto badjunction

set "OPTS=%MAP%?listen?SessionName=%SESSION%?Port=%GAMEPORT%?QueryPort=%QUERYPORT%?MaxPlayers=%MAXPLAYERS%?AltSaveDirectoryName=Cluster-%MAP%?RCONEnabled=True?RCONPort=%RCONPORT%?ServerAdminPassword=%ADMINPASS%"
if not "%SERVERPASS%"=="" set "OPTS=%OPTS%?ServerPassword=%SERVERPASS%"
REM Match start_ase_server.bat: obelisk uploads survive 30 days instead of the 24h default.
set "OPTS=%OPTS%?TributeItemExpirationSeconds=2592000?TributeDinoExpirationSeconds=2592000?TributeCharacterExpirationSeconds=2592000"

echo Launching BRIDGE server: %SESSION% on %MAP%  (game %GAMEPORT% / query %QUERYPORT%)
echo Save dir: %MAPSAVEDIR%
echo Cluster: %CLUSTERID%  (%CLUSTERDIR%)
echo Run 'saveworld' in this console BEFORE closing it once your character has transferred in.
echo.
"%EXE%" "%OPTS%" -server -log -NoBattlEye -ClusterId=%CLUSTERID% -ClusterDirOverride="%CLUSTERDIR%" -NoTransferFromFiltering
goto end

:badcluster
echo.
echo Cluster folder is not usable, so the bridge server was NOT started.
echo   CLUSTERDIR=%CLUSTERDIR%
echo.
echo Starting with a missing cluster folder makes ARK hang on its launch sequence with
echo no error, and a RELATIVE path silently builds a second cluster folder inside
echo ShooterGame\Saved - so this script stops instead. Set CLUSTERDIR to a full path
echo on the launcher's Configuration tab (then Save), or create the folder by hand.
goto end

:badsaves
echo.
echo SAVESROOT is not usable, so the bridge server was NOT started.
echo   SAVESROOT=%SAVESROOT%
echo.
echo It must be a FULL path starting with a drive letter, and must match
echo start_ase_server.bat's SAVESROOT exactly. Set it on the launcher's
echo Configuration tab, then Save.
goto end

:badjunction
echo.
echo The per-map save junction does not resolve, so the bridge server was NOT started:
echo   %JUNCTION%  -^>  %MAPSAVEDIR%
echo.
echo ARK would launch but silently fail to save. Delete the Cluster-%MAP% entry inside
echo ShooterGame\Saved by hand (rmdir removes only the link, never your saves) and
echo re-run this script to recreate it cleanly.

:end
endlocal
pause
