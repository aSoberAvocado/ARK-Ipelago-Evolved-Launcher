# Read-only diagnostic for the ARKipelago reset. Called by diagnose_reset.bat.
#
# Answers four questions, in order:
#   1. Is paths.cmd present, does every script that should call it actually call it, and
#      has none of them grown its own stray copy of a shared var again? (paths.cmd is the
#      single source of truth for SERVER_ROOT / SAVESROOT / CLUSTERDIR / BACKUPROOT /
#      CLUSTERID / ADMINPASS / SERVERPASS - see scripts/paths.cmd's own header comment.
#      reset_ark_test.bat additionally aliases CLUSTER=%CLUSTERDIR% and
#      MAPSAVES=%SAVESROOT% right after the call, which this also checks.)
#   2. Does ShooterGame\Saved\SavedArks exist, and does it contain anything?
#   3. Is each ShooterGame\Saved\Cluster-<Map> a junction, and does its target resolve?
#   4. Where do the real *.ark / *.arkprofile / *.arktribe files actually live?
#
# NOTHING here writes, moves or deletes. Get-ChildItem / Test-Path / Get-Content only.
param(
    [Parameter(Mandatory = $true)][string]$ScriptsDir,
    [string]$LauncherConfig = ''
)

$ErrorActionPreference = 'Continue'

# A caller passing "...\dir\" leaves a stray quote on the value (the backslash escapes the
# closing quote), so scrub quotes before the path is ever used.
$ScriptsDir = $ScriptsDir.Trim().Trim('"')
if ($LauncherConfig) { $LauncherConfig = $LauncherConfig.Trim().Trim('"') }

function Write-Head($text) {
    Write-Host ''
    Write-Host ('=' * 78)
    Write-Host "  $text"
    Write-Host ('=' * 78)
}

# Pull `set "NAME=value"` (or bare `set NAME=value`) out of a .bat file.
function Get-BatVar([string]$file, [string]$name) {
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    $esc = [regex]::Escape($name)
    foreach ($line in Get-Content -LiteralPath $file) {
        $t = $line.Trim()
        if ($t -match ('^(?i)set\s+"' + $esc + '=(.*)"\s*$')) { return $Matches[1] }
        if ($t -match ('^(?i)set\s+' + $esc + '=(.*)$'))       { return $Matches[1].Trim() }
    }
    return $null
}

# Expand %VAR% references using values already pulled from the same file.
function Expand-BatVal([string]$val, [hashtable]$vars) {
    if ([string]::IsNullOrEmpty($val)) { return $val }
    foreach ($k in $vars.Keys) {
        if ($null -ne $vars[$k]) { $val = $val -replace ('(?i)%' + [regex]::Escape($k) + '%'), $vars[$k] }
    }
    return $val
}

function Norm([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { return '' }
    return $p.Trim().TrimEnd('\')
}

# files / bytes under a folder, without following junctions back out of it
function Get-Stats([string]$path) {
    $r = New-Object psobject -Property @{ Files = 0; Bytes = 0 }
    if (-not (Test-Path -LiteralPath $path)) { return $r }
    $f = Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue
    if ($f) {
        $r.Files = @($f).Count
        $sum = ($f | Measure-Object -Property Length -Sum).Sum
        if ($sum) { $r.Bytes = $sum }
    }
    return $r
}

function Fmt-Size([long]$b) {
    if ($b -ge 1MB) { return ('{0:N1} MB' -f ($b / 1MB)) }
    if ($b -ge 1KB) { return ('{0:N1} KB' -f ($b / 1KB)) }
    return "$b B"
}

$problems = New-Object System.Collections.ArrayList
function Add-Problem($t) { [void]$problems.Add($t) }

# --------------------------------------------------------------- 1. paths ----
Write-Head '1. Is paths.cmd the single source, and does every script actually use it?'

$pathsCmd  = Join-Path $ScriptsDir 'paths.cmd'
$startBat  = Join-Path $ScriptsDir 'start_ase_server.bat'
$resetBat  = Join-Path $ScriptsDir 'reset_ark_test.bat'
$switchBat = Join-Path $ScriptsDir 'switch_map.bat'
$transferBat = Join-Path $ScriptsDir 'start_transfer_server.bat'

# The vars paths.cmd is supposed to be the ONLY place holding a "set" line for.
$sharedVarNames = 'SERVER_ROOT', 'SAVESROOT', 'CLUSTERDIR', 'BACKUPROOT', 'CLUSTERID', 'ADMINPASS', 'SERVERPASS'

$pathsVars = @{}
if (Test-Path -LiteralPath $pathsCmd) {
    Write-Host "  paths.cmd : $pathsCmd"
    foreach ($n in $sharedVarNames) { $pathsVars[$n] = Get-BatVar $pathsCmd $n }
    foreach ($k in @($pathsVars.Keys)) { $pathsVars[$k] = Expand-BatVal $pathsVars[$k] $pathsVars }
    foreach ($n in $sharedVarNames) {
        Write-Host ("    {0,-11} : {1}" -f $n, $pathsVars[$n])
    }
} else {
    Write-Host "  ! paths.cmd NOT FOUND at: $pathsCmd"
    Add-Problem 'paths.cmd is missing - every script that calls it silently keeps whatever these variables were before the call (normally blank), so nothing will work until it is restored.'
}

# Every script below is supposed to `call` paths.cmd for the shared vars rather than
# holding its own "set" line for them - that per-file duplication is the exact bug this
# whole diagnostic exists to catch (see the header comment).
foreach ($file in @($startBat, $switchBat, $resetBat, $transferBat)) {
    if (-not (Test-Path -LiteralPath $file)) { continue }
    $name = Split-Path -Leaf $file
    $text = Get-Content -LiteralPath $file -Raw
    Write-Host ''
    Write-Host ("  {0}" -f $name)
    if ($text -match '(?im)^\s*call\s+"?%~dp0paths\.cmd"?') {
        Write-Host '    calls paths.cmd : yes'
    } else {
        Write-Host '    calls paths.cmd : NO'
        Add-Problem ("$name does not call paths.cmd - it is not using the shared values.")
    }
    foreach ($n in $sharedVarNames) {
        if ($null -ne (Get-BatVar $file $n)) {
            Write-Host ("    ! still has its own set ""{0}=...""  - drift risk reintroduced." -f $n)
            Add-Problem ("$name has its own '$n' instead of relying on paths.cmd.")
        }
    }
}

# reset_ark_test.bat uses local names (CLUSTER/MAPSAVES) aliased straight from
# paths.cmd's CLUSTERDIR/SAVESROOT right after the call - confirm the alias, not a
# hardcoded value, is actually there.
if (Test-Path -LiteralPath $resetBat) {
    $rawCluster  = Get-BatVar $resetBat 'CLUSTER'
    $rawMapsaves = Get-BatVar $resetBat 'MAPSAVES'
    Write-Host ''
    Write-Host '  reset_ark_test.bat aliases'
    Write-Host ("    CLUSTER  = {0}" -f $rawCluster)
    Write-Host ("    MAPSAVES = {0}" -f $rawMapsaves)
    if ($rawCluster -notmatch '(?i)^%CLUSTERDIR%$') {
        Add-Problem "reset_ark_test.bat's CLUSTER is not aliased from paths.cmd's CLUSTERDIR."
    }
    if ($rawMapsaves -notmatch '(?i)^%SAVESROOT%$') {
        Add-Problem "reset_ark_test.bat's MAPSAVES is not aliased from paths.cmd's SAVESROOT."
    }
}

$launcher = $null
if ($LauncherConfig -and (Test-Path -LiteralPath $LauncherConfig)) {
    try { $launcher = Get-Content -LiteralPath $LauncherConfig -Raw | ConvertFrom-Json }
    catch { Write-Host "  ! could not parse launcher config: $LauncherConfig" }
}

if ($launcher) {
    Write-Host ''
    Write-Host '  Launcher config (the value the launcher actually resets against):'
    foreach ($k in 'SERVER_ROOT', 'SAVESROOT', 'CLUSTERDIR', 'BACKUPROOT', 'MAP') {
        Write-Host ("    {0,-12} : {1}" -f $k, $launcher.$k)
    }
    foreach ($k in 'SERVER_ROOT', 'SAVESROOT', 'CLUSTERDIR', 'BACKUPROOT') {
        $lv = Norm ([string]$launcher.$k); $bv = Norm ([string]$pathsVars[$k])
        if ($lv -and $bv -and ($lv -ne $bv)) {
            Write-Host ("    -> MISMATCH {0}: launcher='{1}' vs paths.cmd='{2}'" -f $k, $lv, $bv)
            Add-Problem ("Launcher and paths.cmd disagree on {0}." -f $k)
        }
    }
}

# Everything below uses the launcher's values when present (that is what the GUI reset uses),
# otherwise paths.cmd's.
$serverRoot = Norm $pathsVars['SERVER_ROOT']
$savesRoot  = Norm $pathsVars['SAVESROOT']
$clusterDir = Norm $pathsVars['CLUSTERDIR']
if ($launcher) {
    if ($launcher.SERVER_ROOT) { $serverRoot = Norm ([string]$launcher.SERVER_ROOT) }
    if ($launcher.SAVESROOT)   { $savesRoot  = Norm ([string]$launcher.SAVESROOT) }
    if ($launcher.CLUSTERDIR)  { $clusterDir = Norm ([string]$launcher.CLUSTERDIR) }
}
$savedDir = Join-Path $serverRoot 'ShooterGame\Saved'

Write-Host ''
Write-Host "  Using for the checks below:"
Write-Host "    SERVER_ROOT : $serverRoot"
Write-Host "    SAVESROOT   : $savesRoot"
Write-Host "    CLUSTERDIR  : $clusterDir"
if (-not (Test-Path -LiteralPath $serverRoot)) {
    Write-Host '    ! SERVER_ROOT does not exist on this machine.'
    Add-Problem 'SERVER_ROOT does not exist.'
}

# ----------------------------------------------------------- 2. SavedArks ----
Write-Head '2. ShooterGame\Saved\SavedArks - the folder the reset calls "the world save"'

$savedArks = Join-Path $savedDir 'SavedArks'
if (Test-Path -LiteralPath $savedArks) {
    $s = Get-Stats $savedArks
    Write-Host "  exists : $savedArks"
    Write-Host ("  contents: {0} file(s), {1}" -f $s.Files, (Fmt-Size $s.Bytes))
    if ($s.Files -eq 0) {
        Write-Host '  -> EMPTY. Nothing here for the reset to back up; a backup of this folder'
        Write-Host '     would be an empty folder. Under AltSaveDirectoryName this is expected.'
        Add-Problem 'SavedArks is empty - backing it up produces an empty backup folder.'
    }
} else {
    Write-Host "  does NOT exist: $savedArks"
    Write-Host '  -> the reset will report a backup path it never created.'
    Add-Problem 'SavedArks does not exist, but the reset still reports backing it up.'
}

# ------------------------------------------------------------ 3. junctions ---
Write-Head '3. Cluster-<Map> junctions in ShooterGame\Saved'

if (-not (Test-Path -LiteralPath $savedDir)) {
    Write-Host "  Saved folder not found: $savedDir"
    Add-Problem 'ShooterGame\Saved not found.'
} else {
    $links = @(Get-ChildItem -LiteralPath $savedDir -Directory -Force -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -like 'Cluster-*' })
    if ($links.Count -eq 0) {
        Write-Host '  No Cluster-<Map> entries at all.'
        Write-Host '  -> the server has not run with AltSaveDirectoryName, or saves live elsewhere.'
        Add-Problem 'No Cluster-<Map> junction found.'
    }
    foreach ($l in $links) {
        Write-Host ''
        Write-Host ("  {0}" -f $l.FullName)
        $item = Get-Item -LiteralPath $l.FullName -Force
        $isLink = $false
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { $isLink = $true }
        $target = ''
        if ($item.PSObject.Properties['Target'] -and $item.Target) { $target = @($item.Target)[0] }
        if (-not $isLink) {
            Write-Host '    type   : REAL FOLDER (not a junction)'
            Write-Host '    -> saves written here do NOT land in SAVESROOT, so a reset that only'
            Write-Host '       clears SAVESROOT will leave this world completely intact.'
            Add-Problem ("{0} is a real folder, not a junction - SAVESROOT reset misses it." -f $l.Name)
        } else {
            Write-Host '    type   : junction'
            Write-Host "    target : $target"
            if ($target -and (Test-Path -LiteralPath $target)) {
                Write-Host '    resolves: YES'
            } else {
                Write-Host '    resolves: NO - dangling (target missing)'
                Write-Host '    -> ARK may fail to save here, or recreate it on next start.'
                Add-Problem ("{0} is a dangling junction." -f $l.Name)
            }
            if ($savesRoot -and $target) {
                if (-not ((Norm $target).ToLower().StartsWith($savesRoot.ToLower()))) {
                    Write-Host '    -> target is OUTSIDE SAVESROOT; the reset would never touch it.'
                    Add-Problem ("{0} points outside SAVESROOT." -f $l.Name)
                }
            }
        }
        $s = Get-Stats $l.FullName
        Write-Host ("    contents: {0} file(s), {1}" -f $s.Files, (Fmt-Size $s.Bytes))
    }
}

# ------------------------------------------------------- 4. real save files --
Write-Head '4. Where the real save + character files actually are'

# Deliberately NOT SERVER_ROOT itself: that is the whole multi-GB steam install and
# recursing it takes minutes. Saves only ever live under ShooterGame\Saved.
$roots = New-Object System.Collections.ArrayList
foreach ($r in @($savedDir, $savesRoot, $clusterDir)) {
    if ($r -and (Test-Path -LiteralPath $r)) {
        $p = (Get-Item -LiteralPath $r).FullName
        if (-not ($roots -contains $p)) { [void]$roots.Add($p) }
    }
}
# also sweep the parent of SAVESROOT/CLUSTERDIR so _backup_<ts> siblings show up
foreach ($r in @($savesRoot, $clusterDir)) {
    if ($r) {
        $par = Split-Path -Parent $r
        if ($par -and (Test-Path -LiteralPath $par) -and -not ($roots -contains $par)) {
            [void]$roots.Add($par)
        }
    }
}

# -Include is silently ignored alongside -LiteralPath, so filter by extension by hand.
$saveExt = @('.ark', '.arkprofile', '.arktribe')
$hits = @()
foreach ($r in $roots) {
    $found = Get-ChildItem -LiteralPath $r -Recurse -File -Force -ErrorAction SilentlyContinue |
             Where-Object { $saveExt -contains $_.Extension.ToLower() }
    if ($found) { $hits += $found }
}
$hits = $hits | Sort-Object FullName -Unique

if (-not $hits -or @($hits).Count -eq 0) {
    Write-Host '  No .ark / .arkprofile / .arktribe files found under any known root.'
    Write-Host ('  (Searched: ' + ($roots -join '; ') + ')')
} else {
    Write-Host '  LIVE (would load on next server start):'
    $live = @($hits | Where-Object { $_.FullName -notmatch '_backup_\d{6,}' })
    if (@($live).Count -eq 0) { Write-Host '    (none)' }
    foreach ($h in $live) {
        Write-Host ("    {0,-11} {1,10}  {2}" -f $h.Extension, (Fmt-Size $h.Length), $h.FullName)
    }
    Write-Host ''
    Write-Host '  IN BACKUPS (already moved aside):'
    $bak = @($hits | Where-Object { $_.FullName -match '_backup_\d{6,}' })
    if (@($bak).Count -eq 0) { Write-Host '    (none)' }
    foreach ($h in $bak) {
        Write-Host ("    {0,-11} {1,10}  {2}" -f $h.Extension, (Fmt-Size $h.Length), $h.FullName)
    }

    $liveProfiles = @($live | Where-Object { $_.Extension -ieq '.arkprofile' })
    if (@($liveProfiles).Count -gt 0) {
        Add-Problem ("{0} live .arkprofile (character) file(s) still present - a reset that reported success did NOT remove them." -f @($liveProfiles).Count)
    }
}

# ---------------------------------------------------------------- verdict ----
Write-Head 'Summary'
if ($problems.Count -eq 0) {
    Write-Host '  No problems detected.'
} else {
    Write-Host ("  {0} problem(s):" -f $problems.Count)
    foreach ($p in $problems) { Write-Host "    ! $p" }
}
Write-Host ''
Write-Host '  (read-only - nothing was moved, deleted or created)'
