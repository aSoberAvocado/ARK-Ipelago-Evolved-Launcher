@echo off
REM Switch which ARK map the (single) dedicated server is running - the "pseudo-cluster" swap:
REM stop server -> optional backup -> relaunch on the new map. Same server install + ClusterId
REM the whole time, so uploads/downloads at an Obelisk carry over between maps as normal.
REM
REM Usage:
REM   switch_map.bat                 (interactive menu)
REM   switch_map.bat ScorchedEarth_P (direct - map ID from the list below)

setlocal enabledelayedexpansion
set "SERVER_ROOT=E:\ARK\Server"
REM Must match start_ase_server.bat's SAVESROOT/CLUSTERDIR exactly - real per-map save data
REM lives in SAVESROOT now (ShooterGame\Saved\Cluster-<Map> is just a junction pointing at it).
set "SAVESROOT=E:\ARK\ServerCluster\Saves"
set "CLUSTERDIR=E:\ARK\ServerCluster\ClusterData"
set "BACKUPROOT=E:\ARK\ServerCluster\Backups"
set "HERE=%~dp0"

REM ---- known real ASE map IDs (Lost Colony / Astraeos are ASA-only, not listed) ----
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

set "TARGET=%~1"
if not "%TARGET%"=="" goto validate

:menu
echo Choose a map to switch to:
for /l %%i in (1,1,12) do echo   %%i^) !NAME%%i!  ^(!MAP%%i!^)
echo.
set /p "CHOICE=Number: "
set "TARGET="
for /l %%i in (1,1,12) do if "%CHOICE%"=="%%i" set "TARGET=!MAP%%i!"
if "%TARGET%"=="" (
    echo Invalid choice.
    goto menu
)

:validate
set "VALID="
for /l %%i in (1,1,12) do if /i "%TARGET%"=="!MAP%%i!" set "VALID=1"
if not "%VALID%"=="1" (
    echo.
    echo "%TARGET%" is not a recognized map ID. Valid IDs:
    for /l %%i in (1,1,12) do echo   !MAP%%i!  ^(!NAME%%i!^)
    goto end
)

echo.
echo Switching to: %TARGET%
echo Every map now has its OWN save folder (%SAVESROOT%\^<Map^>), so your
echo character/dinos/items only carry over if you UPLOADED them at an Obelisk (or travelled
echo via the bridge server) first - switching does not do this for you.
echo.
pause

REM ---- stop the currently running server (graceful close, then force if it won't quit) ----
tasklist /fi "imagename eq ShooterGameServer.exe" | find /i "ShooterGameServer.exe" >nul
if not errorlevel 1 (
    echo Stopping current server...
    taskkill /im ShooterGameServer.exe >nul 2>nul
    set "WAITED=0"
    :waitloop
    timeout /t 2 /nobreak >nul
    set /a WAITED+=2
    tasklist /fi "imagename eq ShooterGameServer.exe" | find /i "ShooterGameServer.exe" >nul
    if not errorlevel 1 (
        if !WAITED! lss 30 goto waitloop
        echo Server didn't stop gracefully after 30s - forcing.
        taskkill /f /im ShooterGameServer.exe >nul 2>nul
    )
    echo Server stopped.
) else (
    echo No server currently running.
)

REM ---- optional backup (copy, not move - covers ALL maps' saves + cluster uploads) ----
REM Backs up SAVESROOT + CLUSTERDIR (the real per-map data + tribute uploads) rather than
REM ShooterGame\Saved\SavedArks - the latter is now just junction placeholders pointing there.
set /p "DOBACKUP=Back up all map saves + cluster data before switching? (y/n): "
if /i "%DOBACKUP%"=="y" (
    for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "TS=%%t"
    set "BACKUP=%BACKUPROOT%\!TS!"
    echo Backing up -^> !BACKUP!
    mkdir "!BACKUP!" 2>nul
    robocopy "%SAVESROOT%" "!BACKUP!\Saves" /E /NFL /NDL /NJH /NJS /NP >nul
    robocopy "%CLUSTERDIR%" "!BACKUP!\ClusterData" /E /NFL /NDL /NJH /NJS /NP >nul
)

REM ---- relaunch on the new map (new window, same launch script/ports/cluster settings) ----
echo Launching %TARGET% ...
start "ARK Server - %TARGET%" cmd /c ""%HERE%start_ase_server.bat" %TARGET%"

:end
endlocal
