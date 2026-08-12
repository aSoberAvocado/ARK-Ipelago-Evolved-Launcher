@echo off
REM Single source of truth for the paths + identity fields shared by every ARK server
REM script. Edit this ONE file (or use the launcher's Configuration tab -> Save) instead
REM of touching the same "set" lines in every .bat - that per-file duplication is what
REM let SERVER_ROOT/SAVESROOT/CLUSTERDIR drift apart between scripts in the past (see
REM diagnose_reset.bat, which exists specifically to catch that drift).
REM
REM Sourced via:  call "%~dp0paths.cmd"
REM from start_ase_server.bat, switch_map.bat, reset_ark_test.bat, and
REM start_transfer_server.bat. Each of those keeps its OWN per-script values locally
REM (MAP, SESSION, MAXPLAYERS, ports, TRIBUTEEXP, ...) - only the values below are
REM shared across all of them. apply_server_config.bat is NOT one of these callers -
REM it only ever needed SERVER_ROOT and keeps its own copy.
REM
REM Deliberately no setlocal/endlocal here: this file only ever runs via `call` from
REM inside another script's own scope, and setlocal would trap these "set" lines
REM inside a scope that unwinds the moment this file ends - the caller would see none
REM of them.

REM ---- edit these ---------------------------------------------------------
set "SERVER_ROOT=C:\ARKServer"
set "ADMINPASS=changeme_admin"
set "SERVERPASS="
REM Cluster: same ClusterId + ClusterDirOverride on EVERY map's launch = uploads/downloads
REM (via Obelisk/transfer terminal) carry over between maps. Leave ClusterId blank to
REM disable clustering entirely.
set "CLUSTERID=MyCluster"
set "CLUSTERDIR=C:\ARKServer\ClusterData"
REM Per-map save separation: every map keeps its world + player profiles in its own folder
REM under SAVESROOT (physically outside ShooterGame\Saved). ARK only accepts save dir names
REM relative to ShooterGame\Saved, so a junction Cluster-<Map> is created there pointing at
REM the real folder. Characters move between maps ONLY via Obelisk upload/download.
set "SAVESROOT=C:\ARKServer\ClusterSaves"
REM Folder switch_map.bat writes timestamped backups into (SAVESROOT + CLUSTERDIR) when
REM you choose to back up before switching maps.
set "BACKUPROOT=C:\ARKServer\ClusterBackups"
REM ------------------------------------------------------------------------
