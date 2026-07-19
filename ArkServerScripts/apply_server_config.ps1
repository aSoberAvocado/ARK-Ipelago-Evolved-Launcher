# Apply the recommended ARK server settings (serverconfig\*.settings) into the real
# Game.ini / GameUserSettings.ini. Key-level upsert: each key listed in a template is updated
# in place if it already exists under its section, or added under that section if missing.
# Sections are created if absent. Any setting you have that ISN'T in a template is left untouched.
#
# Called by apply_server_config.bat (which passes -ConfigDir). Can also be run directly:
#   powershell -ExecutionPolicy Bypass -File apply_server_config.ps1 -ConfigDir "E:\...\WindowsServer"
param(
    [Parameter(Mandatory = $true)][string]$ConfigDir
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$srcDir = Join-Path $here 'serverconfig'

# template file -> the real ini it applies to
$map = @{
    'Game.ini.settings'             = 'Game.ini'
    'GameUserSettings.ini.settings' = 'GameUserSettings.ini'
}

# Parse a .settings template into an ordered list of @{ Section; Key; Value }.
function Read-Settings($path) {
    $section = ''
    $out = New-Object System.Collections.ArrayList
    foreach ($raw in Get-Content -LiteralPath $path) {
        $line = $raw.Trim()
        if ($line -eq '' -or $line.StartsWith('#') -or $line.StartsWith(';')) { continue }
        if ($line.StartsWith('[') -and $line.EndsWith(']')) { $section = $line; continue }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { continue }
        [void]$out.Add([pscustomobject]@{
            Section = $section
            Key     = $line.Substring(0, $eq).Trim()
            Value   = $line.Substring($eq + 1).Trim()
        })
    }
    return $out
}

# Upsert (section,key,value) triples into an ini file's line list. Returns the new line array.
function Apply-Settings($lines, $settings) {
    $lines = [System.Collections.ArrayList]@($lines)
    foreach ($s in $settings) {
        # find the section header line index (-1 if absent)
        $secIdx = -1
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i].Trim() -ieq $s.Section) { $secIdx = $i; break }
        }
        if ($secIdx -lt 0) {
            # append a new section at end of file
            if ($lines.Count -gt 0 -and $lines[$lines.Count - 1].Trim() -ne '') { [void]$lines.Add('') }
            [void]$lines.Add($s.Section)
            [void]$lines.Add("$($s.Key)=$($s.Value)")
            continue
        }
        # find the extent of the section (until next header or EOF), look for an existing key
        $keyIdx = -1
        $endIdx = $lines.Count
        for ($i = $secIdx + 1; $i -lt $lines.Count; $i++) {
            $t = $lines[$i].Trim()
            if ($t.StartsWith('[') -and $t.EndsWith(']')) { $endIdx = $i; break }
            $eq = $t.IndexOf('=')
            if ($eq -ge 1 -and ($t.Substring(0, $eq).Trim() -ieq $s.Key)) { $keyIdx = $i; break }
        }
        if ($keyIdx -ge 0) {
            $lines[$keyIdx] = "$($s.Key)=$($s.Value)"          # update in place
        } else {
            $lines.Insert($endIdx, "$($s.Key)=$($s.Value)")     # add at end of the section
        }
    }
    return $lines
}

if (-not (Test-Path -LiteralPath $ConfigDir)) {
    Write-Host "Config folder not found: $ConfigDir"
    Write-Host "It's usually <SERVER_ROOT>\ShooterGame\Saved\Config\WindowsServer - and only"
    Write-Host "exists after the server has started once. Start the server, stop it, then re-run."
    exit 1
}

foreach ($tpl in $map.Keys) {
    $src = Join-Path $srcDir $tpl
    $dst = Join-Path $ConfigDir $map[$tpl]
    if (-not (Test-Path -LiteralPath $src)) { Write-Host "  ! skip (missing template): $src"; continue }

    $settings = Read-Settings $src
    if ($settings.Count -eq 0) { Write-Host "  ($tpl has no settings - skipped)"; continue }

    if (Test-Path -LiteralPath $dst) {
        Copy-Item -LiteralPath $dst -Destination "$dst.bak" -Force        # one-level backup
        $lines = Get-Content -LiteralPath $dst
    } else {
        $lines = @()
    }
    $new = Apply-Settings $lines $settings
    # UTF-8 WITHOUT BOM - a BOM at the top makes ARK ignore the first section header.
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($dst, [string[]]$new, $utf8NoBom)
    Write-Host "  applied $($settings.Count) setting(s) -> $($map[$tpl])  (backup: $($map[$tpl]).bak)"
}

Write-Host ''
Write-Host 'Done. Restart the ARK server for the changes to take effect.'
