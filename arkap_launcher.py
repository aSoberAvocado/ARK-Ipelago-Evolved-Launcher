#!/usr/bin/env python3
"""
ArkAP Launcher  -  v1
=====================
A small Tkinter GUI to configure the ArkAP / Archipelago ARK server scripts from
one place, and to quick-launch the common folders and .bat files.

Design notes (v1):
  * Pure standard library (tkinter, configparser, re, json) so it packages cleanly
    with PyInstaller into a single .exe. End users need no Python install.
  * On load it scans whichever target files exist and pre-fills the fields from
    their current `set "VAR=value"` lines (or connector.ini key=value lines), so it
    works on a partially-configured install.
  * On save it writes a local JSON snapshot, then applies the values back into each
    target file using TARGETED line replacement only - a regex that rewrites just the
    matched `set "VAR=..."` line (or ini key) and leaves every other line (junction
    creation, tasklist checks, map menus, robocopy backups, ...) completely untouched.
  * connector.ini is READ with configparser but WRITTEN with targeted line
    replacement, so its inline comments survive.

SteamCMD bundling (v1 install feature):
  * Built as a single-file --onefile --windowed exe (see build.py / build_exe.bat).
    steamcmd.exe and assets/ are embedded as PyInstaller "datas" and unpacked by
    the bootloader into a temp folder (sys._MEIPASS) each time the exe runs -
    see resource_dir() below. Only steamcmd.exe itself is bundled (not its
    self-generated dlls/logs/cache), matching exactly what Valve's own
    steamcmd.zip bootstrapper contains; steamcmd fetches everything else it
    needs on first run, same as a fresh manual install.
  * If steamcmd.exe is missing at runtime (e.g. a lightweight build, or
    extraction failed), the launcher falls back to downloading steamcmd.zip
    from Valve's CDN and unzipping it into that same (temp, per-run) folder on
    first use.
  * Because the bundle is re-extracted to a fresh temp folder on every launch,
    steamcmd's own working files (logs/appcache/depotcache/userdata) do not
    persist between runs of the exe - only the actual installed ARK server
    (written to the user-chosen SERVER_ROOT, entirely outside the bundle) is
    persistent. This is intentional and matches steamcmd's normal self-updating
    behaviour.

Per-file variable facts that matter:
  * scripts/paths.cmd is the ONLY place SERVER_ROOT, SAVESROOT, CLUSTERDIR,
    BACKUPROOT, CLUSTERID, ADMINPASS and SERVERPASS are written - start_ase_server.bat,
    switch_map.bat, reset_ark_test.bat and start_transfer_server.bat all `call` it
    instead of holding their own copies, which is what used to let these drift apart
    between scripts unnoticed (see diagnose_reset.bat). See BAT_TARGETS below.
    A working folder carried over from before that refactor mixes the two schemes and
    silently wins with its own stale SERVER_ROOT, so those files are detected and
    replaced on startup rather than preserved - see PATHS_CMD_CALLERS.
  * reset_ark_test.bat still uses different variable NAMES for two of those paths
    locally (CLUSTER == CLUSTERDIR, MAPSAVES == SAVESROOT) - it aliases them from
    paths.cmd right after the `call`, so Save never targets those names directly.
  * start_transfer_server.bat deliberately runs on its own SESSION / ports /
    MAXPLAYERS (a bridge alongside the main server) - those stay local to the file
    and are not synced from the GUI at all.
"""

import io
import os
import re
import sys
import json
import time
import glob
import zlib
import ctypes
import codecs
import queue
import shutil
import struct
import hashlib
import zipfile
import tempfile
import webbrowser
import threading
import subprocess
import collections
import configparser
import urllib.request
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter import font as tkfont


# --------------------------------------------------------------------------- #
#  Field / mapping definitions
# --------------------------------------------------------------------------- #

# GUI groups: (group title, [(key, label, kind), ...])
# kind: "folder" -> askdirectory Browse ; "file" -> askopenfilename Browse ; "text" -> no Browse
GROUPS = [
    ("Paths", [
        ("SERVER_ROOT", "SERVER_ROOT",           "folder"),
        ("SAVESROOT",   "SAVESROOT",             "folder"),
        ("CLUSTERDIR",  "CLUSTERDIR",            "folder"),
        ("BACKUPROOT",  "BACKUPROOT",            "folder"),
        ("PLUGINS_DIR", "ArkApi Plugins folder", "folder"),
    ]),
    # server/slot/password used to live here too, but they're per-room Archipelago
    # identity, not server plumbing - they now sit on the Archipelago Setup tab next
    # to the tooling that uses them (see ARCHIPELAGO_GROUPS). What's left is the
    # plugin/connector's own files on disk plus the one behaviour toggle, which is
    # why the group is no longer called "Connector".
    ("Plugin files & DeathLink", [
        ("death_link", "death_link", "bool"),
        ("ipc_dir",    "ipc_dir",    "folder"),
        ("data_dir",   "data_dir",   "folder"),
        ("game_ini",   "game_ini",   "file"),
    ]),
    ("Network", [
        ("MAP",        "MAP",        "text"),
        ("SESSION",    "SESSION",    "text"),
        ("MAXPLAYERS", "MAXPLAYERS", "text"),
        ("GAMEPORT",   "GAMEPORT",   "text"),
        ("QUERYPORT",  "QUERYPORT",  "text"),
        ("RCONPORT",   "RCONPORT",   "text"),
        ("ADMINPASS",  "ADMINPASS",  "text"),
        ("SERVERPASS", "SERVERPASS", "text"),
        ("TRIBUTEEXP", "TRIBUTEEXP", "text"),
    ]),
    ("Cluster", [
        ("CLUSTERID", "CLUSTERID", "text"),
    ]),
    # Optional and rarely touched, so it sits last - below Paths/Network/Connector/
    # Cluster - rather than being the first thing the user scrolls past.
    ("Locations", [
        ("connector_ini", "connector.ini file (not required)",                                "file"),
    ]),
]

# Rendered onto the Archipelago Setup tab by the same _render_field_groups() loop that
# builds GROUPS on the Configuration tab, so these fields get identical labels, Browse
# buttons, placeholders, tooltips and search behaviour - they just live on a different
# tab. Their keys land in self.vars exactly as before, which is what keeps connector.ini
# writing, profiles, diagnostics and the Setup Status checks working unchanged.
ARCHIPELAGO_DIR_KEY = "ARCHIPELAGO_DIR"
# Same treatment for the PopTracker install (the tracker app that watches the multiworld).
# Declared up here because ARCHIPELAGO_GROUPS below names it.
POPTRACKER_DIR_KEY = "POPTRACKER_DIR"

ARCHIPELAGO_GROUPS = [
    ("Archipelago installation", [
        (ARCHIPELAGO_DIR_KEY, "Archipelago directory", "folder"),
    ]),
    ("Archipelago room (Connector settings)", [
        ("server",   "server",   "text"),
        ("slot",     "slot",     "text"),
        ("password", "password", "text"),
    ]),
    # PopTracker is Archipelago tooling (it autotracks the multiworld), not ARK server
    # tooling, which is why it lives on this tab rather than the Configuration or Install
    # tabs. Its whole group - directory field, scan, and the three buttons - is built into
    # this one LabelFrame (see _build_poptracker_controls).
    ("PopTracker (tracker)", [
        (POPTRACKER_DIR_KEY, "PopTracker directory", "folder"),
    ]),
]

# Which self.vars keys belong to the Archipelago Setup tab. Both Save buttons write
# everything (there is one config JSON), but each one's HIGHLIGHT reports only its own
# fields - see _update_save_highlights. Everything else in self.vars is Configuration's.
ARCHIPELAGO_KEYS = {key for _title, fields in ARCHIPELAGO_GROUPS for key, *_rest in fields}

# A folder only counts as an Archipelago install if it holds ALL of these. Any one of
# them alone shows up in unrelated places (an extracted zip, a half-copied folder), and
# accepting a partial match would leave half the tab's buttons pointing at nothing.
ARCHIPELAGO_REQUIRED_EXES = (
    "ArchipelagoLauncher.exe",
    "ArchipelagoGenerate.exe",
    "ArchipelagoOptionsCreator.exe",
)

# Launched by name from the Archipelago directory. Not in the required trio above: a
# working install has it, but its absence shouldn't invalidate the whole folder - the
# button for it just disables itself with a message instead.
ARCHIPELAGO_TEXT_CLIENT_EXE = "ArchipelagoTextClient.exe"

# Same treatment as the Text Client, and for the same reason: "Host local Archipelago
# server" gates itself on this file existing rather than it joining
# ARCHIPELAGO_REQUIRED_EXES. Adding it to the required trio would make a folder that is
# otherwise a perfectly good install (yaml building, generating, the Launcher) fail
# is_archipelago_dir() outright and grey out the ENTIRE tab, just because the one button
# a self-hosting user may never press has nothing to point at.
ARCHIPELAGO_SERVER_EXE = "ArchipelagoServer.exe"

# Archipelago's own default, from the `port:` key under `server_options:` in a stock
# host.yaml (verified against 0.6.7, which also reports ":38281" in the server's own
# "Hosting game at ..." banner). Only a fallback - archipelago_host_port() reads the
# real value out of host.yaml first, so a user who changed it there still gets the
# correct address auto-filled.
ARCHIPELAGO_DEFAULT_PORT = 38281

# The installer's own default, and by far the most common location.
ARCHIPELAGO_DEFAULT_DIR = r"C:\ProgramData\Archipelago"


def is_archipelago_dir(path):
    """True if `path` is a folder holding all of ARCHIPELAGO_REQUIRED_EXES."""
    if not path or not os.path.isdir(path):
        return False
    return all(os.path.isfile(os.path.join(path, exe))
               for exe in ARCHIPELAGO_REQUIRED_EXES)


def archipelago_host_port(root):
    """The port ArchipelagoServer.exe will actually host on, read from host.yaml.

    Read rather than assumed because host.yaml is Archipelago's own documented way to
    change the hosting port, and this value is what gets auto-filled into the `server`
    field - guessing it would hand the user an address their own server isn't on.
    "Host local Archipelago server" deliberately does NOT pass --port for the same
    reason: a command-line --port would override host.yaml (verified - despite the
    "These overwrite command line arguments!" comment in host.yaml, the CLI wins for
    port and password), silently defeating the user's own edit.

    ponytail: regex, not a yaml parse - the app has no yaml dependency and pulling one
    in for a single integer isn't worth it. Only matches a plain `port: <digits>`, which
    is the stock format; anything exotic (anchors, quotes, a flow mapping) falls back to
    the documented default, which is also what an unreadable or missing file gives.
    """
    try:
        with open(os.path.join(root, "host.yaml"), "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return ARCHIPELAGO_DEFAULT_PORT
    # Anchored to the server_options block: host.yaml also carries a `port:` under the
    # webhost section, and matching that one would report the wrong address entirely.
    # The slice starts AFTER the header, so the terminator below can't match the header's
    # own first character and collapse the block to nothing.
    head = "server_options:"
    idx = text.find(head)
    if idx < 0:
        return ARCHIPELAGO_DEFAULT_PORT
    # Ends at the next top-level key; comments inside the block are indented, so an
    # unindented line really is the start of a different section.
    block = re.split(r"^\S", text[idx + len(head):], maxsplit=1, flags=re.MULTILINE)[0]
    match = re.search(r"^\s+port:\s*(\d{1,5})\s*(?:#.*)?$", block, flags=re.MULTILINE)
    if not match:
        return ARCHIPELAGO_DEFAULT_PORT
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else ARCHIPELAGO_DEFAULT_PORT


def archipelago_seed_files(root):
    """Generated seeds in Archipelago's output folder, newest first.

    Both extensions are listed because ArchipelagoGenerate writes a .zip (verified: a
    real generate produced AP_<seed>.zip holding AP_<seed>.archipelago plus the spoiler
    log), while ArchipelagoServer.exe happily takes EITHER the .zip or a bare extracted
    .archipelago as its positional multidata argument - both were confirmed to boot a
    working room. So the normal case is the .zip the user just generated, and someone
    who extracted one by hand still sees their file."""
    if not root:
        return []
    out = os.path.join(root, "output")
    found = glob.glob(os.path.join(out, "*.zip")) + \
        glob.glob(os.path.join(out, "*.archipelago"))
    return sorted(found, key=lambda p: os.path.getmtime(p), reverse=True)


def _dedupe_paths(paths):
    """The same paths, order kept, with case/separator-equivalent duplicates dropped -
    so the cheapest guess in a candidate list is still tried first. ProgramFiles and
    ProgramFiles(x86) are the same folder on some boxes, which is what made this worth
    having; both directory scans use it."""
    seen, out = set(), []
    for p in paths:
        norm = os.path.normcase(os.path.normpath(p))
        if norm not in seen:
            seen.add(norm)
            out.append(p)
    return out


def archipelago_candidate_dirs():
    """Fixed, fast-to-check guesses, cheapest/most likely first - checked before any
    walking happens (see _dir_scan_worker). Archipelago's installer defaults to
    ProgramData; the rest cover a per-user install and a manual extract."""
    home = os.path.expanduser("~")
    cands = [
        ARCHIPELAGO_DEFAULT_DIR,
        os.path.join(os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local")),
                     "Archipelago"),
        os.path.join(home, "Archipelago"),
    ]
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if root:
            cands.append(os.path.join(root, "Archipelago"))
    for name in USER_SWEEP_FOLDER_NAMES:
        cands.append(os.path.join(home, name, "Archipelago"))
    return _dedupe_paths(cands)


# --------------------------------------------------------------------------- #
#  PopTracker (the tracker app) and the ARK tracker pack
# --------------------------------------------------------------------------- #
#
# PopTracker is a separate third-party app (black-sliver/PopTracker) that autotracks an
# Archipelago multiworld. Like Archipelago itself it is never bundled with this launcher -
# the Windows build alone is ~17 MB extracted, several times the launcher's own zip - so
# the launcher either points at a copy the user already has, or downloads one for them
# ("Download PopTracker").

# The one file a folder must hold to count as a PopTracker install. Deliberately not a
# trio like ARCHIPELAGO_REQUIRED_EXES: PopTracker ships as a zip you extract anywhere, and
# everything done with it here (launch it, write into packs\) needs only the exe to exist.
POPTRACKER_EXE = "poptracker.exe"
# The pack folder PopTracker scans next to its exe. It has other search paths too
# (HOME/PopTracker/packs, Documents/PopTracker/packs, CWD/packs - see its README), but
# EXEDIR\packs is the one that belongs to the install the user pointed us at, so it is the
# only one written to.
POPTRACKER_PACKS_DIRNAME = "packs"

# /releases/latest here, NOT the list endpoint the ArkAP repo needs: this repo publishes
# release candidates as pre-releases (v0.35.4-rc1/-rc2 at the time of writing, both newer
# than the v0.35.3 stable), and /releases/latest is exactly what excludes them. Handing
# users an rc build of somebody else's app is not what "Download PopTracker" should do.
# Verified against the live API: the list's first entry is an rc, /releases/latest is the
# stable.
POPTRACKER_RELEASES_API = (
    "https://api.github.com/repos/black-sliver/PopTracker/releases/latest")
POPTRACKER_RELEASES_PAGE = "https://github.com/black-sliver/PopTracker/releases"
# Asset naming on every release checked: poptracker_<version-with-dashes>_win64.zip,
# alongside macOS/Linux/AppImage/source builds and a .minisig signature for each. Matched
# by suffix because the version is baked into the name. Windows-only on purpose - so is
# the rest of this launcher (SteamCMD, .bat scripts, PowerShell update helper).
POPTRACKER_ASSET_SUFFIX = "_win64.zip"
# That zip holds ONE top-level folder, "poptracker\", with poptracker.exe inside it
# (verified against the published v0.35.3 asset) - so extracting it into the folder the
# user picks yields <picked>\poptracker, and THAT is what the directory field is set to.
POPTRACKER_DOWNLOAD_SUBDIR_NOTE = "poptracker"

# --- The ARK tracker pack (a PopTracker pack, not a PopTracker build) ------- #
# The /releases LIST endpoint, same as the ArkAP plugin/.apworld use. Today's releases
# (0.0.1, 0.0.2) are both full releases, so /releases/latest would work right now - but
# this is a small personal repo, one "Pre-release" tick there is all it takes to make that
# endpoint 404, and that is the exact bug already fixed once for the plugin. The list
# endpoint returns pre-releases too, newest first.
TRACKER_PACK_RELEASES_API = (
    "https://api.github.com/repos/lurch9229/Arkipelago-Poptracker/releases")
TRACKER_PACK_RELEASES_PAGE = "https://github.com/lurch9229/Arkipelago-Poptracker/releases"
# The pack publishes no release assets at all, so the download is GitHub's auto-generated
# source zip ("zipball_url" on the release JSON). Its single top-level folder is named
# <owner>-<repo>-<short sha> (e.g. lurch9229-Arkipelago-Poptracker-74046e9), which is NOT
# something PopTracker keys anything on: a pack is any entry in packs\ - folder OR zip -
# that holds a manifest.json, and its identity comes from that file's package_uid /
# package_version (verified in PopTracker's own pack.cpp: Pack::Pack reads manifest.json
# from a directory or, for a zip, from the zip root or its single top-level folder, and
# Pack::ListAvailable/Pack::Find iterate every entry of each search path). So the extracted
# folder is renamed to this stable name on install, and the sha in the zip's name is
# irrelevant - as is the folder name of a copy the user installed by hand.
TRACKER_PACK_DIRNAME = "Arkipelago-Poptracker"
TRACKER_PACK_MANIFEST = "manifest.json"
# From the pack's own manifest.json (both published releases). The uid is what
# --load-pack takes; the variant is the pack's only one, flagged "ap".
TRACKER_PACK_UID = "Ark_Tracker_Lurch9229"
TRACKER_PACK_VARIANT = "map_tracker"
TRACKER_PACK_LABEL = "ARK tracker pack"
# Where a replaced pack is moved to. Deliberately NOT a .bak left inside packs\:
# PopTracker scans every entry in that folder, so a backup sitting there would still hold
# a valid manifest.json with the same package_uid and turn up as a second copy of the pack
# in its Load list.
TRACKER_PACK_BACKUP_DIRNAME = "pack_backups"


def is_poptracker_dir(path):
    """True if `path` is a folder holding poptracker.exe."""
    return bool(path) and os.path.isfile(os.path.join(path, POPTRACKER_EXE))


def poptracker_packs_dir(root):
    """<PopTracker dir>\\packs, or "" when the directory isn't set."""
    return os.path.join(os.path.normpath(root), POPTRACKER_PACKS_DIRNAME) if root else ""


def poptracker_candidate_dirs():
    """Fixed, fast-to-check guesses, cheapest/most likely first - checked before any
    walking happens (see _dir_scan_worker). PopTracker has no installer and no canonical
    location: it ships as a zip you extract wherever you like, so these are the places it
    actually ends up - next to this launcher, at a drive root, in the user's profile, and
    in Desktop/Documents/Downloads (where a downloaded zip is usually unpacked, and where
    "Download PopTracker" is likely to be pointed).

    Both the folder name people give it ("PopTracker") and the one the official zip
    extracts as ("poptracker") are covered by one spelling, since Windows paths are
    case-insensitive."""
    home = os.path.expanduser("~")
    cands = [os.path.join(base_dir(), "PopTracker"), r"C:\PopTracker",
             os.path.join(home, "PopTracker")]
    for name in USER_SWEEP_FOLDER_NAMES:
        cands.append(os.path.join(home, name, "PopTracker"))
    return _dedupe_paths(cands)


def read_pack_manifest(path):
    """(package_uid, package_version) of the PopTracker pack at `path`, or ("", "").

    Handles both forms PopTracker accepts (see the TRACKER_PACK_DIRNAME note): a FOLDER
    with manifest.json at its root, and a ZIP with manifest.json either at the zip root or
    inside a single top-level folder. The zip form matters because PopTracker's own drag &
    drop installs packs "without unpacking" (its README), so a copy the user installed
    themselves can legitimately be a .zip sitting in packs\\ - carrying the same
    package_uid as ours, and therefore something to find and move aside rather than leave
    behind as a duplicate.

    Anything unreadable, non-JSON or not a pack at all reports ("", "") rather than
    raising: this runs over every entry of a folder the user owns."""
    try:
        if os.path.isdir(path):
            with open(os.path.join(path, TRACKER_PACK_MANIFEST), "rb") as fh:
                raw = fh.read()
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                name = next((n for n in zf.namelist()
                             if n.lower() == TRACKER_PACK_MANIFEST
                             or (n.lower().endswith("/" + TRACKER_PACK_MANIFEST)
                                 and n.count("/") == 1)), None)
                if not name:
                    return "", ""
                raw = zf.read(name)
        else:
            return "", ""
        # utf-8-sig, not utf-8: json.loads chokes on a BOM, and a hand-edited manifest
        # saved from a Windows editor is exactly where one comes from.
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, zipfile.BadZipFile):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return (str(data.get("package_uid") or "").strip(),
            str(data.get("package_version") or "").strip())


def installed_tracker_pack(packs_dir):
    """(path, version) of the ARK tracker pack already in `packs_dir`, else ("", "").

    Identified by package_uid out of each entry's manifest.json rather than by name,
    because PopTracker doesn't care what a pack is called: a copy installed by hand can be
    sitting there under any folder name, or as a zip. The version is the manifest's own
    package_version, which the pack keeps in step with its release tag (0.0.1 and 0.0.2
    both match), so no hash-matching against release assets is needed here - unlike the
    .apworld, whose file carries no usable version at all."""
    try:
        entries = sorted(os.scandir(packs_dir), key=lambda e: e.name.lower())
    except OSError:
        return "", ""
    for entry in entries:
        uid, version = read_pack_manifest(entry.path)
        if uid == TRACKER_PACK_UID:
            return entry.path, version
    return "", ""


def locate_extracted_pack(root):
    """The folder inside an extracted download that IS the pack - the one directly holding
    manifest.json. GitHub's source zip nests everything one level down in
    <owner>-<repo>-<sha>\\, so that level has to be found rather than assumed; a zip whose
    manifest is already at the root works too. Returns the path, or None."""
    if os.path.isfile(os.path.join(root, TRACKER_PACK_MANIFEST)):
        return root
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        cand = os.path.join(root, name)
        if os.path.isfile(os.path.join(cand, TRACKER_PACK_MANIFEST)):
            return cand
    return None


def poptracker_win64_asset(release):
    """The Windows build asset on a PopTracker release payload, or None. Suffix match
    because the version is part of the filename; the per-asset .minisig signatures don't
    end in the suffix, so they can't be picked by accident."""
    for asset in release.get("assets") or []:
        if ((asset.get("name") or "").lower().endswith(POPTRACKER_ASSET_SUFFIX)
                and asset.get("browser_download_url")):
            return asset
    return None


# Per-field data for the one shared directory scan (_start_dir_scan): what counts as a
# hit, where to look before walking any drives, and what to say when nothing turns up.
# Filled in here rather than inside the scan so adding a third scannable directory field
# is a table entry, not another copy of the worker/poll/busy trio.
DIR_SCAN_TARGETS = {
    ARCHIPELAGO_DIR_KEY: {
        "what": "Archipelago",
        "matches": is_archipelago_dir,
        "candidates": archipelago_candidate_dirs,
        "missing": ("no Archipelago install found - set the directory manually (it's the "
                    "folder holding %s)." % ", ".join(ARCHIPELAGO_REQUIRED_EXES)),
    },
    POPTRACKER_DIR_KEY: {
        "what": "PopTracker",
        "matches": is_poptracker_dir,
        "candidates": poptracker_candidate_dirs,
        "missing": ("no PopTracker install found - set the directory manually (it's the "
                    "folder holding %s), or use \"Download PopTracker\" to fetch a copy."
                    % POPTRACKER_EXE),
    },
}


def locate_extracted_poptracker(root):
    """The folder inside an extracted PopTracker download that holds poptracker.exe -
    the zip's own single "poptracker\\" folder in every release checked, but found rather
    than assumed. Returns the path, or None."""
    if is_poptracker_dir(root):
        return root
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        cand = os.path.join(root, name)
        if is_poptracker_dir(cand):
            return cand
    return None


# PopTracker's command line is version-dependent, and getting this wrong is not harmless:
#
#   * --load-pack <uid> / --pack-variant <variant> are old and safe. Documented in the
#     shipped v0.35.3's OWN doc/commandline.txt, and a real launch with both came up
#     normally, so "Open PopTracker" always passes them.
#   * --ap-host / --ap-slot / --ap-password only exist from 0.35.4 on (still pre-release at
#     the time of writing - v0.35.4-rc1/-rc2). They are NOT ignored by an older build:
#     v0.35.3 treats them as a bad argument, prints its usage and exits 1, i.e. PopTracker
#     never opens at all (observed - exit code 1, no window). So they are only passed to an
#     install new enough to have them.
POPTRACKER_AP_ARGS_MIN_VERSION = "0.35.4"
# Ships next to the exe in every release zip, and its first "## vX.Y.Z" heading is that
# release's own version - the only version marker readable without running the exe, which
# only prints --version to a console a windowed app hasn't got.
POPTRACKER_CHANGELOG = "CHANGELOG.md"
_POPTRACKER_VERSION_RE = re.compile(r"##\s*v?(\d+(?:\.\d+){1,3})")


def poptracker_version(root):
    """The installed PopTracker's version ("0.35.3"), or "" if it can't be established.
    An rc build reports its base version (v0.35.4-rc2 -> 0.35.4), which is correct for
    what its command line supports."""
    try:
        with open(os.path.join(root, POPTRACKER_CHANGELOG), "r", encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                match = _POPTRACKER_VERSION_RE.match(line.strip())
                if match:
                    return match.group(1)
    except OSError:
        pass
    return ""


def poptracker_supports_ap_args(root):
    """True only when this install is known to be 0.35.4 or newer. An unknown version
    counts as too old on purpose: the cost of guessing "yes" wrongly is PopTracker
    refusing to start, while guessing "no" wrongly just means the room details have to be
    typed into its own AP dialog once."""
    version = poptracker_version(root)
    return bool(version) and not _version_is_newer(POPTRACKER_AP_ARGS_MIN_VERSION, version)


# Why a pre-0.35.4 PopTracker gets a clipboard copy and a note instead of being connected
# for it, having looked at all three possibilities in its own source:
#
#   * Command line (the one that works): --ap-host/--ap-slot/--ap-password land in
#     PopTracker's `_args["ap"]`, which is the ONLY thing that sets its internal
#     _apConnectPending, i.e. the only path that actually connects on startup. Gated on
#     POPTRACKER_AP_ARGS_MIN_VERSION because an older build exits instead.
#   * Persisted state (looks promising, isn't): PopTracker.json - %APPDATA%\PopTracker\
#     PopTracker.json, or <exe dir>\portable-config\ in portable mode - does persist
#     `at_uri` (host:port) and `at_slot`, read once at startup before the UI exists, so a
#     pre-launch write would be picked up. But those two values are only the DEFAULTS for
#     the AP dialog's input boxes; nothing connects from them. The password is never
#     persisted at all (no such key exists anywhere in the app, and its "Enter password"
#     box always opens empty). So writing another application's config file would buy
#     "two of three boxes pre-typed in a dialog you still have to walk through" - not a
#     connection - and PopTracker rewrites that whole file itself on startup and on every
#     pack change. Not worth the reach into someone else's settings.
#   * Driving its UI: not done, deliberately.
#
# So the honest fallback is to hand over the details and say so.
def poptracker_room_hint(server, slot, password):
    """(clipboard_text, message) for connecting an older PopTracker's AP dialog by hand,
    or (None, None) when there's no room to hand over.

    The host:port goes on the clipboard rather than the whole lot, because PopTracker asks
    for the three values in three separate input boxes and only one of them is awkward to
    retype - so the clipboard holds exactly what the first box wants, and the message
    carries the other two."""
    server = (server or "").strip()
    if not server:
        return None, None
    message = (
        "PopTracker can't be opened already connected: the command-line arguments for "
        "that (--ap-host / --ap-slot / --ap-password) only exist in PopTracker %s and "
        "newer, and passing them to an older build stops it starting at all.\n\n"
        "Your room details are on the clipboard instead. In PopTracker:\n\n"
        "  1. Click the grey \"AP\" in the top row.\n"
        "  2. Host and port - paste with Ctrl+V:  %s\n"
        "  3. Slot:  %s\n"
        "  4. Password:  %s\n\n"
        "PopTracker remembers the host and slot for next time (it never stores the "
        "password). The ARK pack itself was loaded for you - only this connection step is "
        "manual."
        % (POPTRACKER_AP_ARGS_MIN_VERSION, server, slot or "(not set - type your slot name)",
           password or "(none - leave it empty)"))
    return server, message

# The filesystem-location fields "Clear all paths" and the per-field "C" button clear
# (see _on_clear_all_paths / _clear_path_field) - SERVER_ROOT/SAVESROOT/CLUSTERDIR/
# BACKUPROOT/PLUGINS_DIR (the "Paths" group above) plus ipc_dir/game_ini, which are
# laid out under "Connector" but are just as much a physical path on disk. Deliberately
# excludes the rest of Connector (server/slot/password/death_link/data_dir) and
# Network - those aren't paths, and clearing them isn't what either button is for.
PATH_GROUP_KEYS = ("SERVER_ROOT", "SAVESROOT", "CLUSTERDIR", "BACKUPROOT",
                   "PLUGINS_DIR", "ipc_dir", "game_ini")

# Hover tooltip text per field key: what it is, an example location, and any
# recommended values / gotchas worth knowing before editing it.
FIELD_HELP = {
    "connector_ini": (
        "The optional standalone Python connector's config file (read/written by "
        "ark_ap_connector.py). Only needed if you run that connector instead of the "
        "in-game integrated connector - most users can leave this blank.\n"
        "Example: C:\\ARKServer\\ArkConnector\\connector.ini"
    ),
    "SERVER_ROOT": (
        "Root folder of your ARK dedicated server install. the folder that directly "
        "contains 'ShooterGame'.\n "
        "Example: C:\\ARKServer\n"
        "Tip: ShooterGameServer.exe lives under "
        "ShooterGame\\Binaries\\Win64\\ inside this folder. If your download nested the "
        "game one level deeper (e.g. C:\\ARKServer\\ARK Survival Evolved Dedicated "
        "Server), point SERVER_ROOT at that nested folder instead."
    ),
    "SAVESROOT": (
        "Folder where each map's world save + player profiles are kept, in their own "
        "subfolder, physically outside ShooterGame\\Saved.\n"
        "Example: C:\\ARKServer\\ClusterSaves\n"
        "Tip: a junction named Cluster-<Map> is created inside ShooterGame\\Saved "
        "pointing here automatically. don't edit that junction by hand."
    ),
    "CLUSTERDIR": (
        "Folder where pseudo-cluster transfer data (Obelisk uploads/downloads of "
        "items, dinos, characters) is stored - shared by every map using the same "
        "CLUSTERID.\n"
        "Example: C:\\ARKServer\\ClusterData"
    ),
    "BACKUPROOT": (
        "Folder switch_map.bat writes timestamped backups into (SAVESROOT + "
        "CLUSTERDIR) when you choose to back up before switching maps.\n"
        "Example: C:\\ARKServer\\ClusterBackups"
    ),
    "PLUGINS_DIR": (
        "Your ArkApi 'Plugins' folder - the ArkAP plugin installs as a subfolder of "
        "this (Plugins\\ArkAP).\n"
        "Example: C:\\ARKServer\\ShooterGame\\Binaries\\Win64\\ArkApi\\Plugins\n"
        "Tip: only exists after ArkServerApi (https://github.com/ArkServerApi/AseApi) "
        "has been installed into Win64."
    ),
    "MAP": (
        "The ARK map ID to launch, e.g. TheIsland, Ragnarok, ScorchedEarth_P.\n"
        "Note: TheIsland is the only officially supported map right now - other maps "
        "may work but are untested."
    ),
    "SESSION": "The server's session name, shown to players in the ARK server browser.",
    "MAXPLAYERS": "Maximum number of players allowed on the server at once.",
    "GAMEPORT": (
        "UDP game port the server listens on.\n"
        "Tip: ARK also silently claims GAMEPORT+1 for raw UDP traffic, so keep any "
        "second server (e.g. the bridge/transfer server) at least 2 ports away."
    ),
    "QUERYPORT": "UDP query port used by the Steam server browser and query tools.",
    "RCONPORT": "TCP port for RCON (remote console admin commands).",
    "ADMINPASS": "Admin password (ServerAdminPassword) for RCON and in-game admin commands.",
    "SERVERPASS": "Optional password required for players to join. Leave blank for no password.",
    "TRIBUTEEXP": (
        "How many seconds an Obelisk upload (items/dinos/characters) survives before "
        "expiring.\n"
        "Tip: ARK's own default is 86400 (24h), often too short for a slow solo/small "
        "cluster - 2592000 = 30 days is a common recommended value."
    ),
    ARCHIPELAGO_DIR_KEY: (
        "Your local Archipelago installation - the folder that directly contains "
        "ArchipelagoLauncher.exe, ArchipelagoGenerate.exe and "
        "ArchipelagoOptionsCreator.exe.\n"
        "Example: " + ARCHIPELAGO_DEFAULT_DIR + "\n"
        "Tip: this is Archipelago's own install folder, nothing to do with your ARK "
        "server - use \"Scan for Archipelago\" if you're not sure where it went."
    ),
    POPTRACKER_DIR_KEY: (
        "Your PopTracker installation - the folder that directly contains %s. PopTracker "
        "is the separate tracker app that follows your multiworld; the ARK tracker pack "
        "installs into its \"%s\" subfolder.\n"
        "Example: C:\\PopTracker\n"
        "Tip: PopTracker has no installer - it's a zip you extract wherever you like, so "
        "there's no standard location. Use \"Scan for PopTracker\" if you already have "
        "one, or \"Download PopTracker\" to have the launcher fetch it for you."
        % (POPTRACKER_EXE, POPTRACKER_PACKS_DIRNAME)
    ),
    "server": (
        "Your Archipelago room address, host:port - shown when you host or join the "
        "room. Used to build the \"Copy ARK connection command\" you paste in-game, "
        "and (if you also use the optional standalone connector) written into "
        "connector.ini.\n"
        "Example: archipelago.gg:38281"
    ),
    "slot": (
        "Your Archipelago slot/player name, exactly as it appears in your .yaml "
        "(case-sensitive). Used to build the in-game connection command."
    ),
    "password": (
        "Room password for the Archipelago server. Leave blank if the room has none. "
        "Used to build the in-game connection command."
    ),
    "ipc_dir": (
        "The ArkAP plugin's ipc folder on THIS PC - where the plugin and the "
        "connector exchange files.\n"
        "Example: C:\\ARKServer\\ShooterGame\\Binaries\\Win64\\ArkApi\\Plugins\\ArkAP\\ipc"
    ),
    "data_dir": (
        "Optional folder containing engrams/dinos/locations/crates.json used for "
        "naming.\n"
        "Tip: leave blank to default to the plugin folder (the folder containing "
        "ipc_dir), which is where those files are normally deployed."
    ),
    "game_ini": (
        "Optional full path to the ARK server's Game.ini. When randomize_dino_spawns "
        "is on in your yaml, the connector auto-patches the NPCReplacements block "
        "here (only its own marked block - other settings are left alone).\n"
        "Example: C:\\ARKServer\\ShooterGame\\Saved\\Config\\WindowsServer\\Game.ini\n"
        "Tip: leave blank to just get ipc\\game_ini_fragment.txt to paste in yourself. "
        "Restart the ARK server after this file changes."
    ),
    "CLUSTERID": (
        "The pseudo-cluster ID used on every map's launch - keeping it IDENTICAL "
        "across all maps is what makes Obelisk uploads/downloads carry over between "
        "them.\n"
        "Tip: leave blank to disable clustering entirely."
    ),
    "death_link": (
        "DeathLink default: when on, your death is shared with (and received from) other "
        "players in the multiworld. The yaml slot_data overrides this on connect.\n"
        "Recommended default: on."
    ),
}

# connector.ini [connector] keys we manage (same name as the GUI key).
CONNECTOR_KEYS = ["server", "slot", "password", "death_link", "ipc_dir", "data_dir", "game_ini"]

# --------------------------------------------------------------------------- #
#  First-run defaults
# --------------------------------------------------------------------------- #

# Sensible defaults for non-path settings, matching the values already baked into this
# repo's .bat/.ini templates (the same values used for local testing). Safe to prefill
# since they don't depend on the user's folder layout. Only applied to a field that is
# still empty after loading from real files/JSON, so they never override real data.
# Deliberately excludes personal-identity connector fields (server/slot/password) - those
# are per-room/per-player and would be actively misleading to prefill for other users.
DEFAULT_VALUES = {
    "MAP":        "TheIsland",
    "SESSION":    "ArchipelagoSolo",
    "MAXPLAYERS": "5",
    "GAMEPORT":   "7777",
    "QUERYPORT":  "27015",
    "RCONPORT":   "27020",
    "ADMINPASS":  "changeme_admin",
    "SERVERPASS": "",
    "TRIBUTEEXP": "2592000",
    "CLUSTERID":  "MyCluster",
    "death_link": "true",
}

# Greyed-out example path shown (and never saved) in a path field that's still empty
# after loading/discovery/auto-detect, so the user knows the expected format.
#
# Deliberately generic: a plausible fresh install root any user might create, never a
# path from a developer's machine. These are display-only - self.get() returns "" while
# a placeholder is showing, so they can never be saved, written to a .bat/.ini, or
# mistaken by a Setup Status check for a configured value.
PLACEHOLDER_EXAMPLE_ROOT = r"C:\ARKServer"
PLACEHOLDER_EXAMPLES = {
    "connector_ini": PLACEHOLDER_EXAMPLE_ROOT + r"\ArkConnector\connector.ini",
    "SERVER_ROOT":   PLACEHOLDER_EXAMPLE_ROOT,
    "SAVESROOT":     PLACEHOLDER_EXAMPLE_ROOT + r"\ClusterSaves",
    "CLUSTERDIR":    PLACEHOLDER_EXAMPLE_ROOT + r"\ClusterData",
    "BACKUPROOT":    PLACEHOLDER_EXAMPLE_ROOT + r"\ClusterBackups",
    "PLUGINS_DIR":   PLACEHOLDER_EXAMPLE_ROOT + r"\ShooterGame\Binaries\Win64\ArkApi\Plugins",
    "ipc_dir":       PLACEHOLDER_EXAMPLE_ROOT + r"\ShooterGame\Binaries\Win64\ArkApi\Plugins\ArkAP\ipc",
    "game_ini":      PLACEHOLDER_EXAMPLE_ROOT + r"\ShooterGame\Saved\Config\WindowsServer\Game.ini",
    # Not built from PLACEHOLDER_EXAMPLE_ROOT: unlike the ARK paths this one has a real
    # installer default, so the example doubles as the answer for most users. The
    # "and it doesn't exist on disk" half of is_unconfigured_example_path is what keeps
    # a user who genuinely installed there from having their value discarded.
    ARCHIPELAGO_DIR_KEY: ARCHIPELAGO_DEFAULT_DIR,
    # PopTracker has no installer default at all (it's an extract-anywhere zip), so this
    # one is a plausible example rather than an answer - which is fine, it's display-only
    # either way, and the residue test below still keeps it out of the config unless the
    # user really does have a folder there.
    POPTRACKER_DIR_KEY: r"C:\PopTracker",
}

# Connector identity fields get the SAME greyed-example treatment as the path fields
# above - same _register_placeholder machinery, same get()-returns-"" filtering, so an
# example can never reach connector.ini or a profile as if it were a real room/slot.
#
# Kept in its own dict rather than added to PLACEHOLDER_EXAMPLES because
# is_unconfigured_example_path's residue test ("equals the example AND doesn't exist on
# disk") is meaningless for a host:port or a player name - and 38281 is a plausible real
# AP port, so a user who genuinely is on archipelago.gg:38281 must keep that value.
CONNECTOR_PLACEHOLDERS = {
    "server":   "archipelago.gg:38281",
    "slot":     "YourSlotName",
    "password": "blank if the room has no password",
}


def server_port(server):
    """"archipelago.gg:51357" -> "51357". "" for anything without a trailing :digits
    (blank field, bare hostname, "host:"), so the caller can say so rather than copying
    garbage. rpartition, not split, so a bracketed IPv6 host still yields its port."""
    _, sep, port = (server or "").strip().rpartition(":")
    return port.strip() if sep and port.strip().isdigit() else ""


def is_unconfigured_example_path(key, value):
    """True when `value` is nothing more than this field's shipped example.

    The bundled .bat templates ship with the SAME example paths this module shows as
    placeholders (set "CLUSTERDIR=C:\\ARKServer\\ClusterData" and friends). Reading
    those back in as if the user had configured them was a real correctness bug: the
    fake paths rendered as normal black "configured" values, were written to the
    config JSON / profiles / .bat files on Save, and - worse - _ensure_cluster_dirs
    then created the cluster folders at C:\\ARKServer instead of anywhere near the
    user's actual server, which is why a fresh install ended up with no cluster
    folder where the scan could find one.

    The "and it doesn't exist on disk" half matters: a user who really did install to
    C:\\ARKServer has a genuine value that happens to equal the example, and it must
    be kept. An example path that exists is real configuration; one that doesn't is
    template residue."""
    example = PLACEHOLDER_EXAMPLES.get(key)
    if not example or not value or not str(value).strip():
        return False
    value = str(value).strip()
    if os.path.normcase(os.path.normpath(value)) != os.path.normcase(os.path.normpath(example)):
        return False
    return not os.path.exists(value)


# Cluster folder layout (see default_cluster_paths for where this lands and why).
# SteamCMD never creates any of it and the server refuses to start without CLUSTERDIR,
# so these are created outright - by the "Create ServerCluster folders" button and
# automatically once after a successful install - rather than searched for.
CLUSTER_ROOT_DIRNAME = "ServerCluster"
CLUSTER_PATH_SUBDIRS = (
    ("CLUSTERDIR",  "ClusterData"),
    ("SAVESROOT",   "Saves"),
    ("BACKUPROOT",  "Backups"),
)

# For each .bat/.cmd target: { gui_key: variable_name_in_that_file }.
#
# paths.cmd is the single write target for SERVER_ROOT / SAVESROOT / CLUSTERDIR /
# BACKUPROOT / CLUSTERID / ADMINPASS / SERVERPASS - start_ase_server.bat,
# switch_map.bat, reset_ark_test.bat and start_transfer_server.bat all `call` it now
# rather than holding their own copies of these "set" lines (see the module
# docstring), so they no longer appear as separate entries here. Each of those files
# keeps only its OWN per-script-only fields (ports, MAP, SESSION, TRIBUTEEXP, ...).
# apply_server_config.bat is NOT one of paths.cmd's callers - it only ever needed
# SERVER_ROOT and keeps its own copy, unchanged.
BAT_TARGETS = {
    "paths.cmd": {
        "SERVER_ROOT": "SERVER_ROOT", "SAVESROOT": "SAVESROOT",
        "CLUSTERDIR": "CLUSTERDIR", "BACKUPROOT": "BACKUPROOT",
        "CLUSTERID": "CLUSTERID", "ADMINPASS": "ADMINPASS", "SERVERPASS": "SERVERPASS",
    },
    "start_ase_server.bat": {
        "MAP": "MAP", "SESSION": "SESSION", "MAXPLAYERS": "MAXPLAYERS",
        "GAMEPORT": "GAMEPORT", "QUERYPORT": "QUERYPORT", "RCONPORT": "RCONPORT",
        "TRIBUTEEXP": "TRIBUTEEXP",
    },
    "apply_server_config.bat": {
        "SERVER_ROOT": "SERVER_ROOT",
    },
}

# GUI keys that hold filesystem paths. Save refuses to write a RELATIVE value for
# these into any .bat: a relative CLUSTERDIR reaches ARK as a relative
# -ClusterDirOverride, which ARK resolves against ShooterGame\Saved and silently
# builds a second cluster folder there (observed in the wild as an orphan
# Saved\ClusterData), and a relative SAVESROOT anchors the Cluster-<Map> junction
# against whatever folder the .bat happens to run from.
BAT_PATH_KEYS = {"SERVER_ROOT", "SAVESROOT", "CLUSTERDIR", "BACKUPROOT"}


def is_full_windows_path(value):
    """True only for a drive-qualified or UNC absolute path (C:\\... or \\\\srv\\...).
    Rejects drive-relative forms like \\Saves too - those silently bind to whatever
    the current drive is at run time."""
    return bool(os.path.splitdrive(value)[0]) and os.path.isabs(value)


# Prefill precedence when a field appears in several files (first hit wins).
# paths.cmd first: it's the only place SERVER_ROOT/SAVESROOT/CLUSTERDIR/BACKUPROOT/
# CLUSTERID/ADMINPASS/SERVERPASS are read from now (see BAT_TARGETS).
PREFILL_ORDER = [
    "paths.cmd",
    "start_ase_server.bat",
    "apply_server_config.bat",
]

# The .bat files we expose Run buttons for are start_ase_server.bat and switch_map.bat,
# named inline in the Quick launch list (see _build_ui) so the button order is decided in
# one place. reset_ark_test / apply_server_config deliberately get no button - the in-app
# reset controls and Save cover them; the .bat files still ship and read paths.cmd for
# anyone who runs them by hand.

# --- Quick Launch pre-flight ------------------------------------------------- #
# GUI keys a blank value is legitimately fine for, so the pre-flight check doesn't
# refuse to run over them: an empty SERVERPASS means "no join password" and an empty
# CLUSTERID means "clustering disabled" (both documented in paths.cmd itself).
PREFLIGHT_BLANK_OK = {"SERVERPASS", "CLUSTERID"}


def script_requirements(batname):
    """{gui_key: (file_that_holds_it, var_name_in_it)} for everything `batname` reads.

    Built from the two structures the paths.cmd refactor already defines rather than a
    third hand-maintained list: the script's own BAT_TARGETS entry (its per-script
    fields - ports, MAP, SESSION, ...) plus the whole of paths.cmd's set when the
    script `call`s it (PATHS_CMD_CALLERS). A script that does neither has no
    launcher-managed variables, so it needs no pre-flight check.
    """
    req = {}
    if batname in PATHS_CMD_CALLERS:
        req.update({k: ("paths.cmd", v) for k, v in BAT_TARGETS["paths.cmd"].items()})
    req.update({k: (batname, v) for k, v in BAT_TARGETS.get(batname, {}).items()})
    return req


CONFIG_FILENAME = "arkap_launcher_config.json"

# Separate from CONFIG_FILENAME on purpose - saving/loading/deleting a named profile
# (Profiles tab) must never read or write the single active-config JSON above.
PROFILES_FILENAME = "arkap_launcher_profiles.json"

# --- Reserved autosave profile ---------------------------------------------- #
# One profile slot the app owns and rewrites on a timer, so a crash/mistake can never
# cost more than the last few minutes of Configuration edits. It lives in the same
# PROFILES_FILENAME file as user profiles but is deliberately NOT one of them:
#   * it never counts as "the user has profiles" (see _ensure_default_profile)
#   * it can't be created, renamed, or updated by hand (see the _on_*_profile guards)
#   * it holds ONLY the latest snapshot - each autosave replaces the previous one,
#     no history accumulates, so the profiles file can't grow without bound
# It CAN be loaded (that's the whole point) and deleted, though the next autosave
# recreates it - the delete confirmation says so.
AUTOSAVE_PROFILE_NAME = "Autosave"

# 10 minutes. Tk's after() keeps firing while the window is minimized, which is
# intentional: a minimized launcher is exactly when an unnoticed crash would lose
# work. The cost of a tick is a dict comparison, and the file is only rewritten when
# something actually changed (see _autosave_tick), so an idle app does no disk I/O.
AUTOSAVE_INTERVAL_MS = 10 * 60 * 1000

AUTOSAVE_PROFILE_NOTES = (
    "Written automatically by the launcher every 10 minutes - do not edit.\n"
    "It always holds only the most recent snapshot of the Configuration tab; each "
    "autosave replaces the last one. Load it to recover settings, then use \"Save as "
    "new profile\" if you want to keep them."
)


def is_autosave_profile(name):
    """True for the reserved autosave slot. Case/whitespace-insensitive so a user
    can't sidestep the guards by typing "autosave " or "AUTOSAVE"."""
    return (name or "").strip().lower() == AUTOSAVE_PROFILE_NAME.lower()


# --- Pre-created first profile ------------------------------------------------ #
# Created on first launch when the user has no profiles yet, and loaded straight
# away, so there is always a real profile behind the Configuration tab instead of
# only the bare config JSON. Unlike AUTOSAVE_PROFILE_NAME this is an ordinary user
# profile in every respect - renamable, updatable, deletable, and it counts as "the
# user has profiles" - it just happens to exist before the user made one.
DEFAULT_PROFILE_NAME = "Profile 1"

# JSON key (in CONFIG_FILENAME, deliberately NOT in the profiles file) holding the name
# of the profile Save writes into. Kept separate from the profiles list so the list
# stays a plain name -> snapshot map with no "which one is current" flag to keep in
# sync. Restored on the next launch, so the app comes back up on whatever profile was
# in use rather than always on DEFAULT_PROFILE_NAME. Never set to AUTOSAVE_PROFILE_NAME
# - that slot is a background snapshot the timer owns, and making it the Save target
# would have each autosave silently overwrite the user's work.
ACTIVE_PROFILE_KEY = "active_profile"


# JSON key that persists the "don't show again" choice for the install reminder banner.
REMINDER_HIDE_KEY = "hide_install_reminder"

# JSON key set once, at the end of the very first launch (the same launch that
# auto-creates DEFAULT_PROFILE_NAME). While it is missing the app opens on the
# Instructions tab instead of Configuration; afterwards it never does again. A key of
# its own rather than leaning on "config file doesn't exist yet" so a config that fails
# to write, or is shipped blank, can't re-trigger the greeting on every launch.
FIRST_RUN_DONE_KEY = "first_run_done"

# JSON key (in CONFIG_FILENAME, deliberately NOT in the profiles file) holding the newest
# release version the user has clicked "Check for Updates" through to see. Persisted
# separately from APP_VERSION so it survives restarts and drives the button HIGHLIGHT:
# the highlight re-appears only when a release newer than THIS value ships. Distinct from
# the exclamation badge, which tracks "is anything newer than the installed APP_VERSION
# at all". See _compute_update_cues.
ACK_VERSION_KEY = "last_acknowledged_version"

# The components "Check for Updates" tracks. Each gets its OWN acknowledged-version key
# (see _ack_key) rather than sharing one: with a single value, clicking through a plugin
# update would silently dismiss the highlight for an unrelated .apworld update the user
# never saw. "launcher" deliberately keeps the original un-suffixed ACK_VERSION_KEY so
# existing configs don't forget what the user already acknowledged.
UPDATE_COMPONENTS = ("launcher", "plugin", "apworld", "trackerpack")

# JSON keys recording the version of ArkServerApi / the ArkAP plugin the launcher last
# installed, so Setup Status can flag (as an advisory, never a failure) when a newer
# release exists. Stamped by the install flow that has the version in hand - the ArkApi
# tag comes straight from the GitHub download; the plugin's is the current latest tag at
# install time (the tag the GitHub download reported). Both are read directly off the
# config JSON, like REMINDER_HIDE_KEY above.
ARKAPI_INSTALLED_VERSION_KEY = "arkapi_installed_version"
PLUGIN_INSTALLED_VERSION_KEY = "plugin_installed_version"
# Same idea for the .apworld: stamped by "Update .apworld" (_on_apworld_done) from the tag
# the GitHub download reported.
APWORLD_INSTALLED_VERSION_KEY = "apworld_installed_version"
# ...and for the ARK tracker pack. This one has a real source of truth on disk (the pack's
# manifest.json package_version - see installed_tracker_pack), so the key is only a cache
# for the readers that have a config dict but no folder to look in, chiefly the diagnostics
# version block.
TRACKER_PACK_INSTALLED_VERSION_KEY = "trackerpack_installed_version"
# ...but these two keys are a FALLBACK, not the source of truth. They only ever exist when
# the launcher itself did the install: a copy that shipped with the plugin and .apworld
# already in place recorded neither, which is exactly the install that used to show nothing
# but the launcher version in the update dialog. Both are now detected from the files on
# disk first (apworld_version_from_disk / plugin_version_from_disk) and written back into
# these same keys, so every reader - dialog, Setup Status advisories, diagnostics version
# block - keeps reading one value.
#
# The plugin probe has to download release zips to compare (see plugin_version_from_disk),
# so the ArkAP.dll hash it last ran against is remembered here - including when nothing
# matched - and the probe re-runs only when that hash changes, i.e. once per plugin build
# rather than once per launch.
PLUGIN_PROBED_DLL_SHA_KEY = "plugin_probed_dll_sha"
# How many releases deep that probe will download before giving up. Each ArkAP_plugin.zip is
# ~380 KB, and a plugin built from source matches none of them, so this is a floor on the
# bandwidth an unrecognised build can cost.
PLUGIN_PROBE_MAX_RELEASES = 4
PLUGIN_DLL_NAME = "ArkAP.dll"

# Values the "Export diagnostics" zip must never leak. Everything else is kept as-is so
# the bundle is still useful for troubleshooting - only the secret VALUE is replaced with
# this marker, never the key and never the whole file.
#
# Recognised by the SHAPE of the key rather than a per-file list, because the export
# collects several formats and the same secret is spelled differently in each:
#   ADMINPASS / SERVERPASS                 - paths.cmd, arkap_launcher_config.json
#   ServerAdminPassword / ServerPassword /
#   SpectatorPassword                      - GameUserSettings.ini
#   password / server_password             - host.yaml, a player yaml, ArkAP.config.json
# A per-file list is exactly what let paths.cmd start holding ADMINPASS unnoticed.
REDACT_MARKER = "[REDACTED]"
# Secret keys that don't end in "pass" or contain "password". Nothing collected today uses
# these; they're here so a future collected file can't leak one just by being added.
SECRET_KEY_EXTRA = frozenset({"secret", "secret_key", "token", "auth_token",
                              "api_key", "apikey"})

# --- Appearance / theming --------------------------------------------------- #
# JSON key that persists the light/dark choice - read/written directly like
# REMINDER_HIDE_KEY above (see _read_theme_pref/_write_theme_pref)
# rather than through the main collect_values()/on_save() flow, so the choice
# is remembered immediately on toggle, not only after clicking Save.
THEME_KEY = "theme"

THEMES = {
    "light": {
        "bg":                   "#f0f0f0",
        "fg":                   "#000000",
        "subtle_fg":            "#555555",
        "warn_bg":              "#fff3cd",
        "warn_border":          "#e0a800",
        "warn_fg":              "#664d03",
        "note_fg":              "#8a6d00",
        "text_bg":              "#ffffff",
        "text_fg":              "#000000",
        "tab_active_bg":        "#ffffff",
        "status_ok":            "#1b7a1b",
        "status_fail":          "#b00020",
        "status_info":          "#8a6d00",
        "update_badge_fg":      "#e6a000",
        "status_detail_fg":     "#666666",
        "tooltip_bg":           "#ffffe0",
        "tooltip_fg":           "#000000",
        "entry_placeholder_fg": "#999999",
        "search_hl":            "#fff176",
        "search_hl_current":    "#ffb300",
        "search_hl_fg":         "#000000",
    },
    "dark": {
        "bg":                   "#2b2b2b",
        "fg":                   "#e0e0e0",
        "subtle_fg":            "#aaaaaa",
        "warn_bg":              "#4d3b00",
        "warn_border":          "#8a6d00",
        "warn_fg":              "#ffe08a",
        "note_fg":              "#ffca28",
        "text_bg":              "#1e1e1e",
        "text_fg":              "#e0e0e0",
        "tab_active_bg":        "#3c3c3c",
        "status_ok":            "#4caf50",
        "status_fail":          "#ff5252",
        "status_info":          "#ffca28",
        "update_badge_fg":      "#ffc400",
        "status_detail_fg":     "#bbbbbb",
        "tooltip_bg":           "#4a4a35",
        "tooltip_fg":           "#f0f0f0",
        "entry_placeholder_fg": "#8a8a8a",
        "search_hl":            "#fff176",
        "search_hl_current":    "#ffb300",
        "search_hl_fg":         "#000000",
    },
}

# Backing store Tooltip._show() reads at popup-creation time - a module-level
# dict (not a per-instance one) so every Tooltip - including ones created
# before ArkAPLauncher finishes building the UI - always renders with
# whatever theme is currently active. ArkAPLauncher._apply_theme() mutates
# this in place (same dict object, via .clear()+.update()) rather than
# rebinding the name, since Tooltip only ever holds a reference to it.
CURRENT_THEME = dict(THEMES["light"])

# --- Bundled server scripts (extracted next to the launcher at runtime) --- #
# These ship inside the exe as PyInstaller "datas" (resource_dir()/scripts) and are
# unpacked into a working folder next to the launcher on startup - the user no longer
# needs to download/keep ArkServerScripts separately. Save/Run then operate on the
# extracted copies. install_plugin.bat is deliberately NOT bundled: the "Install Plugin"
# button reimplements its copy natively (see on_install_plugin), and the plugin payload
# is kept external so future plugin updates aren't baked into this exe.
BUNDLED_SCRIPTS = [
    "paths.cmd",
    "start_ase_server.bat",
    "switch_map.bat",
    "start_transfer_server.bat",
    "reset_ark_test.bat",
    "apply_server_config.bat",
    "apply_server_config.ps1",
    # The drift detector itself. Bundled into the exe all along, but missing from this
    # list until now - so it never reached the working folder, and the log message that
    # tells users to "run tools\\diagnose_reset.bat" pointed at a file that wasn't there.
    "diagnose_reset.bat",
    "diagnose_reset.ps1",
    os.path.join("serverconfig", "Game.ini.settings"),
    os.path.join("serverconfig", "GameUserSettings.ini.settings"),
]

# Folder name (under base_dir()) the bundled scripts are extracted into at runtime.
WORKING_SCRIPTS_DIRNAME = "ArkServerScripts"

# --- Pre-paths.cmd script detection ----------------------------------------- #
# Every script here sources paths.cmd for SERVER_ROOT/SAVESROOT/CLUSTERDIR/... instead
# of declaring its own copies. A working file that does NOT is a leftover from before
# that refactor, and it is silently unconfigurable: Save writes those values into
# paths.cmd only, so the old file keeps whatever SERVER_ROOT was baked into it (the
# shipped example, C:\ARKServer) and the server launches against a path the user never
# chose - while the Configuration tab shows their real one, all green. Nothing in the
# app noticed, because each half was individually correct.
#
# The state is reached by upgrading in place: extract_bundled_scripts() is missing-only
# so a user's existing ArkServerScripts folder survives an exe update, which is right
# for their edited values but leaves paths.cmd freshly extracted next to callers that
# predate it. Unzipping an older ArkServerScripts.zip over the folder does the same.
PATHS_CMD_CALLERS = (
    "start_ase_server.bat",
    "switch_map.bat",
    "start_transfer_server.bat",
    "reset_ark_test.bat",
)

_PATHS_CMD_CALL_RE = re.compile(r'(?im)^[ \t]*call[ \t]+"%~dp0paths\.cmd"')

# Old values are migrated out of these files into a newly-created paths.cmd, first hit
# wins per variable. start_ase_server.bat leads because it declared the full set;
# BACKUPROOT only ever existed in switch_map.bat.
PRE_PATHS_MIGRATE_ORDER = PATHS_CMD_CALLERS

# Suffix for the copy taken before a stale script is replaced. Deliberately NOT ".bak",
# which Save already uses for its own backups - overwriting that would destroy the
# user's last known-good pre-Save state to save a file we're about to replace anyway.
PRE_PATHS_BACKUP_SUFFIX = ".pre-paths.bak"

# --- Game.ini / GameUserSettings.ini upload --------------------------------- #
# Where ARK reads its two editable server config files from, relative to SERVER_ROOT.
# The same folder the "Open Game.ini folder" quick-launch button opens.
SERVER_CONFIG_RELDIR = os.path.join("ShooterGame", "Saved", "Config", "WindowsServer")

# The files the Configuration tab's upload section can replace. Names are fixed:
# ARK only reads these exact filenames, so the copy is always renamed to match
# regardless of what the user's source file happens to be called.
UPLOADABLE_CONFIGS = ("Game.ini", "GameUserSettings.ini")

CONFIG_UPLOAD_HELP = {
    "Game.ini": "Your own Game.ini to copy into the server's config folder, replacing "
                "the one that's there. This is the file the connector patches "
                "NPCReplacements into when randomize_dino_spawns is on - if you "
                "upload over it later, re-apply that block.",
    "GameUserSettings.ini": "Your own GameUserSettings.ini to copy into the server's "
                            "config folder, replacing the one that's there. Note ARK "
                            "REWRITES this file itself when the server shuts down, so "
                            "upload it while the server is stopped or your changes will "
                            "be overwritten.",
}

# --- Game.ini dino-randomizer fragment --------------------------------------- #
# The plugin writes ipc\game_ini_fragment.txt as the ARK section header followed by the
# NPCReplacements lines. The "Patch Game.ini for randomized creatures" button applies
# that fragment into Game.ini, wrapping the replacements in the SAME auto-managed markers
# the connector's own auto-patch uses (ark_ap_connector.py _write_spawn_ini), so the two
# paths produce an identical, single managed block and re-applying never duplicates it.
# These markers are also the exact boundary "Full reset for new seed" removes, so a fresh
# seed doesn't inherit the previous seed's randomization.
GAME_INI_FRAGMENT_NAME = "game_ini_fragment.txt"
GAME_INI_SECTION = "[/script/shootergame.shootergamemode]"
GAME_INI_BLOCK_BEGIN = "; === ArkAP NPCReplacements BEGIN (auto-managed, do not edit) ==="
GAME_INI_BLOCK_END = "; === ArkAP NPCReplacements END ==="

# Every dino-randomization fragment line starts with this one distinctive key (the real
# fragment is a wall of ConfigOverrideNPCSpawnEntriesContainer= entries, NOT NPCReplacements
# - an earlier content-guess keyed on the wrong one and let duplicate walls slip through).
# Detecting a pre-existing block is therefore just "does any line start with this key?",
# anywhere in the file, marker-wrapped or not - a literal check, far simpler and more
# reliable than matching fragment content. A user's own hand-set spawn overrides use the
# same key, but the patch flow only ever ASKS about an unmarked wall, never silently
# overwrites, so a "false positive" is just a question the user answers.
GAME_INI_FRAGMENT_KEY = "ConfigOverrideNPCSpawnEntriesContainer"
GAME_INI_FRAGMENT_LINE_RE = re.compile(
    r'(?im)^[ \t]*' + re.escape(GAME_INI_FRAGMENT_KEY) + r'[ \t]*=')

# Process image names checked before any reset - ARK rewrites its save on shutdown and
# the connector holds the ipc files open + rewrites session.json on its next poll, so a
# reset while either runs would be silently undone.
ARK_SERVER_PROCESS = "ShooterGameServer.exe"
CONNECTOR_PROCESS = "ArkConnector.exe"

# ArkAP tracking files the plugin/connector generate, resolved relative to the ArkAP
# plugin folder (<...>\ArkApi\Plugins\ArkAP). Deleting session.json alone only cleared the
# AP->game direction; leaving checks_out.jsonl etc. behind made the connector re-send the
# previous seed's checks into a fresh room. Deleting all of these clears both directions.
# Deliberately EXCLUDES the plugin's own payload (ArkAP.dll, ArkAP.config.json, and the
# engrams/dinos/locations/crates/filler naming .json files) - only generated state is wiped.
#
# ap_connections.json is the one that matters most: the plugin's OWN embedded connector
# (the /connect in-game path, now the normal way to play) persists the room there -
# server host:port, slot, password - and resumes it on the next server start ("APC
# resumed N persisted connection(s)"). It survived every reset, so a fresh seed
# immediately reconnected to the PREVIOUS room. It predates nothing in ipc\: the
# standalone Python connector never wrote it, which is why an ipc-shaped delete list
# could not have covered it.
AP_RESET_PLUGIN_FILES = [
    "state.json", "seed.json", "applied_index.json", "counters.json",
    "events_queue.jsonl", "ArkAP_note_hits.jsonl", "note_queue.jsonl",
    "tame_check_queue.jsonl", "kill_check_queue.jsonl", "dino_queue.jsonl",
    "crate_queue.jsonl", "ArkAP_debug.log",
    "ap_connections.json", "ap_restart.bat", "ap_restart.log",
    "ArkAP_dino_classes.jsonl", "ArkAP_loaded.txt",
    "ArkAP_engrams_dump.json", "ArkAP_notes_dump.json",
]
AP_RESET_IPC_FILES = [
    "session.json", "state.json", "checks_out.jsonl", "items_in.jsonl",
    "death_out.jsonl", "death_in.jsonl", "msg_in.jsonl", "hint_out.jsonl",
    "hint_status.json", "flags.json", "game_ini_fragment.txt",
    "conn_status.txt", "boss_out.jsonl",
]

# Everything the plugin INSTALLER puts in the ArkAP folder (payload, not state): the
# DLL, its config, the shipped naming data, and the per-mod naming data under mods\.
# A reset must leave exactly these and nothing else - see find_ap_leftovers().
AP_PLUGIN_PAYLOAD_FILES = {
    "arkap.dll", "arkap.config.json", "arkap.config.default.json",
    "engrams.json", "locations.json", "dinos.json", "crates.json",
    "filler.json", "tek_grants.json", "spawn_classes.json",
}
AP_PLUGIN_PAYLOAD_DIRS = {"mods"}

# The files whose existence IS "a world / character exists": world save, character
# profile, tribe data. The full reset counts these before it moves anything and
# verifies none remain at a live location afterwards - a reset that reports success
# while an .arkprofile still exists is exactly the false confidence that shipped as
# "the backup folders were empty but my character was still there".
ARK_SAVE_EXTS = (".ark", ".arkprofile", ".arktribe")


def fmt_bytes(n):
    """Human-readable byte count for reset/backup reporting."""
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / (1024.0 * 1024.0))
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


def count_dir_files(path):
    """(file_count, total_bytes, save_file_count) for everything under path.

    save_file_count is the subset matching ARK_SAVE_EXTS. Used by the full reset
    both before a backup move (what SHOULD arrive) and after (what actually did)."""
    files = total = saves = 0
    for dp, _dns, fns in os.walk(path):
        for fn in fns:
            files += 1
            if fn.lower().endswith(ARK_SAVE_EXTS):
                saves += 1
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return files, total, saves


def find_save_files(roots):
    """Every ARK_SAVE_EXTS file under any of `roots`, skipping timestamped
    _backup_ folders (those are already moved aside - finding them is fine).

    Junction-aware dedupe: ShooterGame\\Saved\\Cluster-<Map> and SAVESROOT\\<Map>
    are the same folder seen through two paths, so hits are deduped by realpath.
    Anything this returns after a reset means the reset did NOT actually happen."""
    hits, seen = [], set()
    seen_roots = set()
    for root in roots:
        if not root:
            continue
        root = os.path.normpath(root)
        rkey = os.path.normcase(root)
        if rkey in seen_roots or not os.path.isdir(root):
            continue
        seen_roots.add(rkey)
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not re.search(r"_backup_\d", d)]
            for fn in fns:
                if not fn.lower().endswith(ARK_SAVE_EXTS):
                    continue
                full = os.path.join(dp, fn)
                try:
                    key = os.path.normcase(os.path.realpath(full))
                except OSError:
                    key = os.path.normcase(os.path.abspath(full))
                if key not in seen:
                    seen.add(key)
                    hits.append(full)
    return hits


def find_ap_leftovers(plugin_dir):
    """Every file under plugin_dir that is NOT part of the installed plugin payload.

    The counterpart to find_save_files(), for the AP side of a reset. That one answers
    "did a world survive"; this answers "did AP state survive" - and deliberately does
    it as a KEEP-list scan of the whole folder rather than by re-checking the same
    fixed filenames the delete list already used. A delete list only removes names
    somebody remembered to add, so every new file the plugin starts writing survives a
    reset silently and nothing notices: that is exactly how ap_connections.json (the
    embedded connector's persisted room + slot, auto-resumed on the next server start)
    kept reconnecting a fresh seed to the previous room. Scanning generically means the
    NEXT such file fails the reset loudly instead of shipping as a bug report.

    Anything returned here after a reset is state the reset did not clear."""
    hits = []
    if not plugin_dir or not os.path.isdir(plugin_dir):
        return hits
    for dp, dns, fns in os.walk(plugin_dir):
        if os.path.normcase(dp) == os.path.normcase(plugin_dir):
            dns[:] = [d for d in dns if d.lower() not in AP_PLUGIN_PAYLOAD_DIRS]
            for fn in fns:
                if fn.lower() not in AP_PLUGIN_PAYLOAD_FILES:
                    hits.append(os.path.join(dp, fn))
        else:
            # Below the top level (ipc\, per-player mailboxes, anything new): no
            # payload lives there, so every file is generated state.
            hits += [os.path.join(dp, fn) for fn in fns]
    return sorted(hits)


def list_map_junctions(saved_dir):
    """[(entry_path, target_or_None, resolves)] for every Cluster-* entry inside
    ShooterGame\\Saved.

    target is None when the entry is a REAL folder rather than a junction (saves
    written there live outside SAVESROOT entirely - a reset that only clears
    SAVESROOT misses them). resolves is False for a dangling junction, which makes
    both ARK's saving and any later backup move silently no-op."""
    out = []
    if not os.path.isdir(saved_dir):
        return out
    try:
        entries = list(os.scandir(saved_dir))
    except OSError:
        return out
    for e in entries:
        if not e.name.lower().startswith("cluster-"):
            continue
        try:
            target = os.readlink(e.path)
        except OSError:
            target = None                      # real folder, not a reparse point
        if target:
            if target.startswith("\\\\?\\"):   # readlink returns the \\?\ form
                target = target[4:]
            resolves = os.path.isdir(target)
        else:
            resolves = os.path.isdir(e.path)
        out.append((e.path, target, resolves))
    return out

# The plugin's actual payload filename we key auto-detection of a plugin SOURCE folder on.
PLUGIN_PAYLOAD_MARKER = os.path.join("ArkAP", "ArkAP.dll")
# Preserved on upgrade (never overwritten) so a reinstall keeps the user's live settings.
PLUGIN_PRESERVE_ON_UPGRADE = "ArkAP.config.json"

# --- SteamCMD-based server install --- #
STEAMCMD_ZIP_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
ARK_APP_ID = "376030"
ARK_BETA_BRANCH = "preaquatica"

INSTALL_BTN_HELP = (
    "Installs the ARK: Survival Evolved Dedicated Server, this is where most of your paths will be.\n"
    "Note that sometimes the process will fail with exit code 8, press install again and it should work.\n"
    "sometimes the console does not show download progress, if you are worried about it check task manager "
    "and see if steam or the exe has high network usage, otherwise be patient as its a large file!"
)

# --- ArkServerApi (ArkApi) auto-download --- #
ARKSERVERAPI_RELEASES_API = "https://api.github.com/repos/ArkServerApi/AseApi/releases/latest"
ARKSERVERAPI_RELEASES_PAGE = "https://github.com/ArkServerApi/AseApi/releases"

# --- ArkAP plugin (this project's own plugin/connector bundle) release API --- #
# Same repo the "Manual downloads" link (RELEASES_URL) points at. Uses the /releases LIST
# endpoint (not /releases/latest): this repo is alpha/experimental and its only release is
# marked "Pre-release", which /releases/latest deliberately excludes (404s). The list
# endpoint returns pre-releases too, newest first - take the first entry.
ARKAP_PLUGIN_RELEASES_API = (
    "https://api.github.com/repos/Jbaker16163/Ark-Survival-Archipelago/releases")
# The plugin release asset the "Install Plugin" button downloads (matched case-insensitively).
ARKAP_PLUGIN_ASSET_NAME = "ArkAP_Plugin.zip"
# The ARK world asset the Archipelago tab's "Update .apworld" button downloads, from the
# same releases list. Name verified against the actual published release assets - it is
# lowercase "ark_ase.apworld", not the repo/display name - though the lookup itself is
# case-insensitive like the plugin's.
APWORLD_ASSET_NAME = "ark_ase.apworld"
# Display name per tracked component. Module-level because the update dialog and the
# "you're up to date" box both list every component now, and they must agree on the names.
UPDATE_COMPONENT_LABELS = {"launcher": "Launcher", "plugin": "ArkAP plugin",
                           "apworld": APWORLD_ASSET_NAME,
                           "trackerpack": TRACKER_PACK_LABEL}
# GitHub's API 403s anonymous requests with no User-Agent header - any non-empty value works.
GITHUB_API_USER_AGENT = "ArkAPLauncher"

# --- Launcher self-update (this exe's own releases, separate repo from the plugin/
# connector bundle above) --- #
# 0.4.0 is the "bridge" release: it is BUILT --onefile (a single .exe) purely so the old
# --onefile-era updaters already in the wild can find and install it exactly as before (they
# fetch /releases/latest and download the one .exe asset). But its own update code is the new
# onedir-aware logic below - so once a user is on 0.4.0, their NEXT update pulls a folder-zip
# and lays down an --onedir install, permanently ending the "Failed to load Python DLL" race
# (which was the --onefile bootloader losing a lock-timing fight with AV over the freshly
# extracted _MEI\python313.dll on the first launch after an update). See build.py --bridge.
APP_VERSION = "0.4.10"
UPDATE_REPO = "aSoberAvocado/ARK-Ipelago-Evolved-Launcher"
# NEW clients discover updates from the releases LIST (newest release carrying a launcher
# folder-zip wins), NOT from /releases/latest - that deliberately ignores GitHub's "Latest"
# pin so the bridge release can stay pinned as Latest forever (routing pre-bridge clients to
# it) without hiding newer onedir releases from up-to-date clients. See _pick_best_release.
UPDATE_RELEASES_LIST_API = "https://api.github.com/repos/%s/releases?per_page=30" % UPDATE_REPO
UPDATE_RELEASES_PAGE = "https://github.com/%s/releases" % UPDATE_REPO
# Known launcher folder-zip asset names (matched case-insensitively; any other *.zip on the
# release is accepted as a fallback). This is what build.py's shutil.make_archive produces.
UPDATE_ZIP_ASSET_NAMES = {"arkipelago launcher.zip", "arkaplauncher.zip"}
# PyInstaller --onedir puts the interpreter DLL + everything else under _internal\. The
# relaunch gates on the actual pythonNN DLL in there being openable; we glob python3*.dll
# rather than hardcode python313.dll so a Python minor-version bump at build time doesn't
# silently break the gate/validation. A folder with no such DLL is not a valid onedir payload.
UPDATE_INTERNAL_DIRNAME = "_internal"
UPDATE_PAYLOAD_DLL_GLOB = "python3*.dll"
# Update-helper working files, all written next to the exe (base_dir()). The relaunched app
# sweeps any survivors on startup (_sweep_update_leftovers) in case the helper was killed.
UPDATE_ZIP_TMPNAME = "ArkAPLauncher_update.zip"
UPDATE_STAGING_DIRNAME = "_arkap_update_staging"
UPDATE_HELPER_SCRIPT = "arkap_update_helper.ps1"
# Accepted launcher exe basenames (current + pre-rename), matched case-insensitively when
# locating the exe inside an extracted update, and preserved across the self-replace.
_KNOWN_LAUNCHER_EXE_NAMES = {"arkipelago launcher.exe", "arkaplauncher.exe"}
# Written by the generated update-helper script into base_dir() right before it exits, so the
# freshly-relaunched exe can report the outcome of the update that just happened - the app
# itself is gone by the time the helper runs, so this is the only way to surface a failure.
UPDATE_RESULT_FILENAME = "arkap_update_result.txt"

INSTALL_ARKAPI_BTN_HELP = (
    "Downloads the latest ArkServerApi (ArkApi) release from GitHub "
    "(ArkServerApi/AseApi) and extracts it into SERVER_ROOT's Win64 folder - the same "
    "folder ShooterGameServer.exe lives in.\n"
    "Requires the ARK server to already be installed (Win64 must exist).\n"
    "Existing ArkApi files there are overwritten (upgrade-in-place) - your ArkAP plugin "
    "folder (Win64\\ArkApi\\Plugins\\ArkAP) is untouched, it isn't part of this download."
)

# --- Steam Workshop mods (Mods tab) --- #
# ARK: Survival Evolved's Steam App ID - the same ID used for `+app_update` (server
# binaries) above is also what `+workshop_download_item` downloads mods against, since
# Workshop items are scoped to the app that published them.
ARK_WORKSHOP_APPID = "346110"

# Installed-mod location under SERVER_ROOT. The dedicated server looks for BOTH
# `<id>\` (unpacked cooked content) and a sibling `<id>.mod` (packed metadata) directly
# under here - neither alone is enough, see check_mod_installed.
MODS_CONTENT_RELDIR = os.path.join("ShooterGame", "Content", "Mods")

# Pre-populated, ArkAP-plugin-verified mod set (name, Workshop ID). Anything added via
# "Add mod" is marked supported=False instead of joining this list - see MODS_KEY.
#
# These IDs are also exactly the set the .apworld will accept in the yaml's `mod_ids`: it
# only ships engram data for these, and raises
#     OptionError: ARK: mod_ids lists <id>, which this apworld doesn't know.
# on anything else - which is why "Copy IDs for YAML" copies the supported ones only (see
# split_copyable_mod_ids). Verified against the shipped ark_ase.apworld's own
# data/mods/index.json: eight catalog entries plus 1999447172 as an alias of 731604991.
# If a release adds a mod, this list has to grow with it.
SUPPORTED_MODS = [
    {"id": "731604991",  "name": "Structures Plus (S+)"},
    {"id": "1999447172", "name": "Super Structures"},
    {"id": "1631378184", "name": "Explorer Note Tracker - Universal"},
    {"id": "2594067220", "name": "Super Spyglass Plus"},
    {"id": "821530042",  "name": "Upgrade Station v1.8i"},
    {"id": "1609138312", "name": "Dino Storage v2"},
    {"id": "1565015734", "name": "Kraken's Better Dinos"},
    {"id": "1404697612", "name": "Awesome SpyGlass!"},
    {"id": "889745138",  "name": "Awesome Teleporters!"},
]

# Workshop IDs the apworld treats as ONE catalog entry - forks that ship the parent's class
# paths (Super Structures is Structures Plus'). A server can only load one, and listing both
# in mod_ids is its own OptionError ("alternative versions of the same mod"), so the copy
# warns instead of quietly picking one for the user.
MOD_ALIAS_GROUPS = [("731604991", "1999447172")]

# JSON key (in CONFIG_FILENAME) holding the mod list - id/name/enabled/supported dicts,
# IN LOAD-ORDER (index 0 = highest ActiveMods= priority - ARK loads left-most first).
# Written immediately on every change (toggle/reorder/add) via
# _write_config_key, like THEME_KEY/REMINDER_HIDE_KEY above - a mod's enabled/order
# state has nothing to do with the Configuration tab's fields or its Save button.
MODS_KEY = "mods"

MODS_TAB_HELP = (
    "Download and activate Steam Workshop mods for this server. Checked = should be "
    "installed and active; the icon shows whether it's actually installed on disk yet.\n"
    "Order matters: mods load top-to-bottom, the topmost mod has the highest priority "
    "(matches ActiveMods= order) - use the arrows to reorder.\n"
    "Requires SERVER_ROOT set and the ARK server already installed."
)


class _ConsoleLineSplitter:
    """Buffers decoded text and yields complete "lines" split on \\n, \\r\\n, or a
    bare \\r - SteamCMD rewrites its download-percentage line in place with a bare
    \\r instead of printing a new line, so a plain "split on \\n" (or a decoder
    that holds a trailing \\r back in case it turns out to be part of \\r\\n)
    would swallow every progress update until a real newline finally arrives."""

    def __init__(self):
        self.pending = ""

    def feed(self, text):
        self.pending += text
        lines = []
        while True:
            idx_n = self.pending.find("\n")
            idx_r = self.pending.find("\r")
            if idx_n == -1 and idx_r == -1:
                break
            if idx_r != -1 and (idx_n == -1 or idx_r < idx_n):
                idx, skip = idx_r, 1
                if self.pending[idx + 1:idx + 2] == "\n":
                    skip = 2  # collapse a "\r\n" pair into one line break
            else:
                idx, skip = idx_n, 1
            lines.append(self.pending[:idx])
            self.pending = self.pending[idx + skip:]
        return lines

    def flush(self):
        rest, self.pending = self.pending, ""
        return rest

# ArkServerApi / ArkAP plugin / ArkConnector are not on Steam - point users at the
# releases page that bundles all three instead of automating a GitHub fetch.
RELEASES_URL = "https://github.com/Jbaker16163/Ark-Survival-Archipelago/releases"


# --------------------------------------------------------------------------- #
#  Low-level file helpers
# --------------------------------------------------------------------------- #

def base_dir():
    """Folder the real exe/script lives in - stable across runs, and writable.

    Used for the JSON config file and for discovering sibling project folders
    (scripts_dir, connector_ini). Deliberately NOT used for bundled read-only
    resources - see resource_dir() - because in a --onefile build this is the
    folder the user copied the .exe into, not the PyInstaller extraction dir.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# Unhandled-crash log. Lives next to the config JSON (base_dir()), NOT in the per-run
# _MEI temp extraction folder, so it survives the process dying and the next launch, and
# a user can actually find it / drag it into Discord. Appended to across restarts (see
# write_crash_log) with a size cap so a crash loop can't grow it without bound.
CRASH_LOG_FILENAME = "arkap_launcher_crash.log"
CRASH_LOG_MAX_BYTES = 512 * 1024  # ~512 KB is dozens of tracebacks - plenty of history.


def crash_log_path():
    return os.path.join(base_dir(), CRASH_LOG_FILENAME)


def _trim_to_tail(text, max_bytes):
    """Keep the most recent <= max_bytes of `text`, cut on a line boundary so the oldest
    surviving entry isn't a half line. Used to cap the append-only crash log."""
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= max_bytes:
        return text
    tail = encoded[-max_bytes:].decode("utf-8", "replace")
    nl = tail.find("\n")
    return tail[nl + 1:] if nl != -1 else tail


def write_crash_log(exc_type, exc_value, exc_tb):
    """Append one timestamped traceback to the crash log and return its path (or None if
    even logging failed - never raise, we're already in the failure path)."""
    import traceback
    path = crash_log_path()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = ("\n===== CRASH %s (launcher %s) =====\n%s\n"
             % (stamp, APP_VERSION,
                "".join(traceback.format_exception(exc_type, exc_value, exc_tb))))
    try:
        existing = ""
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                existing = f.read()
        combined = _trim_to_tail(existing + entry, CRASH_LOG_MAX_BYTES)
        with open(path, "w", encoding="utf-8") as f:
            f.write(combined)
        return path
    except OSError:
        return None


# Persistent launcher activity log. Same folder and same size-capped append-only shape as
# the crash log above, for the same reason: it has to survive the app closing and be
# somewhere a user can find it. Everything the launcher reports while it works (installs,
# scans, saves, resets, mod downloads, Game.ini patches, SteamCMD output) went only to the
# on-screen console boxes before this, and was gone the moment the window closed - which is
# exactly the history you want when someone asks "what did it do before it broke?".
LAUNCHER_LOG_FILENAME = "arkipelago_launcher.log"
LAUNCHER_LOG_MAX_BYTES = 1024 * 1024  # ~1 MB - many sessions, still small enough to upload.


def launcher_log_path():
    return os.path.join(base_dir(), LAUNCHER_LOG_FILENAME)


def launcher_log(msg, source=""):
    """Append one timestamped line to the launcher log. Never raises - logging must not be
    able to break the action it is logging (a read-only folder, a full disk, an antivirus
    holding the file open are all survivable; losing the install is not).

    `source` names which on-screen box the line came from (Console / Install / Mods), so
    one file can carry all three streams and still be readable."""
    if not (msg or "").strip():
        return  # blank spacer lines are layout, not history
    line = "%s %s%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                          "[%s] " % source if source else "", msg.strip())
    path = launcher_log_path()
    try:
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
        # Trim only once it's actually oversized, so the common path stays a plain append
        # rather than a read-rewrite of the whole file on every line.
        if os.path.getsize(path) > LAUNCHER_LOG_MAX_BYTES:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                trimmed = _trim_to_tail(f.read(), LAUNCHER_LOG_MAX_BYTES)
            with open(path, "w", encoding="utf-8") as f:
                f.write(trimmed)
    except OSError:
        pass


def _log_dialog(fn, kind):
    """Wrap a messagebox function so every error/warning the user is shown also lands in
    the launcher log.

    Done once here rather than at the ~80 call sites: an error dialog is the single most
    useful thing to have in the log, and a per-call-site approach silently misses every
    dialog added later. The dialog itself behaves exactly as before."""
    def wrapper(title, message, *args, **kwargs):
        launcher_log("%s: %s - %s" % (kind, title, " ".join((message or "").split())))
        return fn(title, message, *args, **kwargs)
    return wrapper


messagebox.showerror = _log_dialog(messagebox.showerror, "ERROR")
messagebox.showwarning = _log_dialog(messagebox.showwarning, "WARNING")


# How much of a log file the in-app viewer loads. ShooterGame.log routinely runs to tens of
# MB; pushing that into a tk.Text freezes the UI for seconds, and nobody scrolls back
# through 40 MB anyway - the error is at the end. Read as a tail (seek), not read-then-cut,
# so the bytes never enter memory in the first place.
LOG_VIEW_MAX_BYTES = 512 * 1024


def read_log_tail(path, max_bytes=LOG_VIEW_MAX_BYTES):
    """The last <= max_bytes of a log file as text, with a header saying so when the file
    was bigger. Cut on a line boundary so the first surviving line isn't a fragment."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        # Binary read (a tail needs a seek), so CRLF has to be normalised by hand - text
        # mode would have done it, and ARK's own logs are CRLF. A stray \r left in shows
        # up as a box in tk.Text.
        text = f.read().decode("utf-8", "replace").replace("\r\n", "\n")
    if size <= max_bytes:
        return text
    nl = text.find("\n")
    return ("*** This log is %d KB - showing only its last %d KB (the newest lines). "
            "Open the file itself if you need earlier history. ***\n\n"
            % (size // 1024, max_bytes // 1024)) + (text[nl + 1:] if nl != -1 else text)


# The Debug Log tab's dropdown: (label shown, key resolved by _log_source_target). Order is
# what the dropdown shows; the first entry is what the tab opens on, so it stays the plugin
# log the tab has always shown.
LOG_SOURCES = [
    ("ArkAP plugin log (ArkAP_debug.log)", "plugin"),
    ("Launcher log (%s)" % LAUNCHER_LOG_FILENAME, "launcher"),
    ("Launcher crash log (%s)" % CRASH_LOG_FILENAME, "crash"),
    ("ARK server log (ShooterGame.log)", "shootergame"),
    ("SteamCMD console log", "steam_console"),
    ("SteamCMD workshop log", "steam_workshop"),
    ("SteamCMD content log", "steam_content"),
]


def is_secret_key(name):
    """True for a config/ini/yaml/cmd key whose VALUE is a password or equivalent.
    See REDACT_MARKER for why this is shape-based rather than a list per file."""
    n = (name or "").strip().strip("\"'").lower()
    # "endswith pass" catches ADMINPASS/SERVERPASS without dragging in ARK's
    # PassiveTameIntervalMultiplier and friends, which a bare "pass" substring would.
    return n.endswith("pass") or "password" in n or n in SECRET_KEY_EXTRA


def redact_config(data):
    """Copy of the config dict with every secret value replaced by REDACT_MARKER (only
    when present and non-empty), so the rest stays useful for troubleshooting."""
    out = dict(data)
    for key in out:
        if is_secret_key(key) and out[key]:
            out[key] = REDACT_MARKER
    return out


# One line = one `key <sep> value`, across all four formats the zip collects:
#   set "ADMINPASS=x"  |  ServerPassword=x  |  password: x  |  "password": "x",
# A leading ; or # is allowed so a commented-out password is redacted too - it's just as
# much of a leak as a live one.
_SECRET_LINE_RE = re.compile(
    r'^(?P<pre>[ \t]*(?:[;#][ \t]*)?(?:set[ \t]+)?"?[ \t]*'
    r'(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)"?[ \t]*[:=][ \t]*)(?P<val>.*)$')

# An unset password is nothing to leak, and blanking it would hide the genuinely useful
# fact that it ISN'T set (an empty SERVERPASS means "no join password").
_NOTHING_TO_LEAK = ("", '""', "''", "null", "~", "none")


def _redact_line_body(line):
    m = _SECRET_LINE_RE.match(line)
    if not m or not is_secret_key(m.group("key")):
        return line
    val = m.group("val").strip()
    if val.lower().rstrip(",") in _NOTHING_TO_LEAK:
        return line
    quote = val[0] if val[:1] in ("\"", "'") else ""
    if quote:
        end = val.rfind(quote)
        if end > 0:  # keep the quotes and anything after them (JSON's trailing comma)
            return "%s%s%s%s%s" % (m.group("pre"), quote, REDACT_MARKER, quote, val[end + 1:])
    # cmd's `set "KEY=value"` closes its quote AFTER the value; keep that (and a trailing
    # comma) so the file still reads as valid.
    tail = val[-1] if val[-1] in ('"', ",") else ""
    return m.group("pre") + REDACT_MARKER + tail


# JSON written without indent puts every key on ONE line, which the line-anchored pass
# above can only ever redact the first of. Handled globally instead, on quoted pairs -
# ArkAP.config.json is not ours and there's no promising it stays pretty-printed.
_JSON_PAIR_RE = re.compile(r'("(?P<key>[^"\\]+)"[ \t]*:[ \t]*)"(?P<val>(?:[^"\\]|\\.)*)"')


def _redact_json_pair(m):
    if not is_secret_key(m.group("key")) or not m.group("val"):
        return m.group(0)
    return '%s"%s"' % (m.group(1), REDACT_MARKER)


def redact_text(text):
    """Blank every password-shaped value in one text file, whatever its format.

    Applied to EVERY entry in the diagnostics zip rather than to one named file, so a
    newly collected file is covered the moment it's added instead of the moment someone
    remembers to write a redactor for it."""
    if not text:
        return text
    out = []
    for line in _JSON_PAIR_RE.sub(_redact_json_pair, text).splitlines(True):
        body = line.rstrip("\r\n")
        out.append(_redact_line_body(body) + line[len(body):])
    return "".join(out)


def aggregate_status_state(items):
    """Overall Setup Status colour from the per-check list: any hard fail -> "fail",
    else any advisory -> "info", else "ok"."""
    states = {it.get("state") for it in items}
    if "fail" in states:
        return "fail"
    if "info" in states:
        return "info"
    return "ok"


def format_setup_status_summary(items):
    """Plain-text rendering of the Setup Status checks for the diagnostics zip."""
    tag = {"ok": "PASS", "fail": "FAIL", "info": "INFO"}
    lines = ["Setup Status (launcher %s)" % APP_VERSION,
             time.strftime("Generated %Y-%m-%d %H:%M:%S"), ""]
    for it in items:
        lines.append("[%s] %s" % (tag.get(it.get("state"), "?"), it.get("label", "")))
        if it.get("detail"):
            lines.append("        %s" % it["detail"])
    lines.append("")
    lines.append("Overall: %s" % tag.get(aggregate_status_state(items), "?"))
    return "\n".join(lines)


# --- Diagnostics bundle: collection helpers --------------------------------- #
# ShooterGame.log routinely runs to tens of MB and the plugin's jsonl files grow with the
# session. Truncating beats both alternatives: dropping the file loses the crash, which is
# almost always at the END, and shipping it whole makes a zip nobody can upload to Discord.
DIAG_MAX_LINES = 5000
# The ipc\ files get a much tighter cap: the plugin appends to checks_out / items_in /
# msg_in every few seconds for the whole session, there are a dozen of them (times one
# mailbox subfolder per player on a multiplayer server), and it is always the LAST few
# exchanges that explain "my item never arrived".
DIAG_IPC_MAX_LINES = 500

# The `game:` value the .apworld registers, and the heading a multi-game yaml uses for its
# ARK section. Matched punctuation-insensitively (see _squash_game_name): the .apworld
# writes "ARK Survival Evolved" while Archipelago's own client list shows it with a colon,
# and a yaml hand-edited from either spelling has to be found.
ARK_YAML_GAME = "ARK Survival Evolved"


def _squash_game_name(text):
    """Lowercase, letters and digits only - so "ARK: Survival Evolved", "ark survival
    evolved" and "ARK_Survival_Evolved" all compare equal."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _tail_lines(text, max_lines=DIAG_MAX_LINES):
    """The last max_lines of a log, with a header saying it was cut. Under the cap the
    text comes back untouched (and unheadered)."""
    lines = text.splitlines(True)
    if len(lines) <= max_lines:
        return text
    return ("*** TRUNCATED for the diagnostics zip: showing the last %d of %d lines. "
            "Ask for the full file if you need earlier history. ***\n\n"
            % (max_lines, len(lines))) + "".join(lines[-max_lines:])


def read_for_diagnostics(path, max_lines=DIAG_MAX_LINES):
    """A collected file's text, or a note saying where we looked. Always returns
    something: an absent zip entry can't be told apart from "we never tried"."""
    if not path:
        return ("(not collected: the launcher doesn't know where this file lives - set "
                "SERVER_ROOT / PLUGINS_DIR / the Archipelago directory and export again.)\n")
    if not os.path.isfile(path):
        return "(not found at %s)\n" % path
    try:
        return _tail_lines(read_text(path)[0], max_lines)
    except OSError as exc:
        return "(could not read %s: %s)\n" % (path, exc)


def collect_ipc_entries(ipc_dir):
    """[(name_in_zip, text)] for every file in the plugin's ipc folder.

    The listing alone (listing_ipc.txt) says a file is 0 bytes, but not that session.json
    points at last week's room or that the last line of items_in.jsonl is a parse error -
    which is most of what an ipc question turns out to be. Walked rather than listed from a
    fixed set of names so the per-player ipc\\<CharacterName> mailboxes a multiplayer server
    creates, and any file a future plugin build adds, come along without a code change.

    Paths keep their ipc-relative shape under an "ipc/" prefix, so a mailbox file is
    obviously a mailbox file. Same truncation note as the big logs, at a much smaller cap
    (DIAG_IPC_MAX_LINES) - these grow for the whole session. Redaction is the caller's one
    shared pass, like every other entry."""
    if not ipc_dir or not os.path.isdir(ipc_dir):
        return []
    out = []
    for root, dirs, files in os.walk(ipc_dir):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, ipc_dir).replace(os.sep, "/")
            out.append(("ipc/" + rel, read_for_diagnostics(path, DIAG_IPC_MAX_LINES)))
    return out


def _yaml_name_values(text):
    """Every `name:` value in a yaml, unquoted and comment-stripped. A yaml can declare
    several (one per slot), so this returns a list rather than the first hit."""
    out = []
    for raw in re.findall(r"^[ \t]*name:[ \t]*(.+)$", text, flags=re.MULTILINE):
        val = re.split(r"\s+#", raw, maxsplit=1)[0].strip().strip("\"'")
        if val:
            out.append(val)
    return out


def yaml_name_matches(name, slot):
    """True if a yaml `name:` refers to `slot`.

    Exact first - the launcher's own instructions tell users the slot must match the yaml
    name exactly, so that's the strongest signal available. Archipelago also allows
    {number} / {player} placeholders, which never equal the slot literally; those match
    loosely rather than failing the whole pass."""
    name, slot = (name or "").strip(), (slot or "").strip()
    if not name or not slot:
        return False
    if name.lower() == slot.lower():
        return True
    if "{" not in name:
        return False
    pattern = "".join(".*" if p.startswith("{") else re.escape(p)
                      for p in re.split(r"(\{[^}]*\})", name) if p)
    return re.fullmatch(pattern, slot, flags=re.IGNORECASE) is not None


def find_player_yamls(archipelago_dir, slot):
    """(paths, note) - the user's yaml(s) for the diagnostics zip, plus a one-line record
    of how they were picked or why nothing was.

    Filenames are arbitrary, so this matches on CONTENT, in two passes:
      1. a `name:` matching the configured Connector slot (see yaml_name_matches);
      2. failing that, every yaml mentioning ARK: Survival Evolved. Whole-file, not just
         the `game:` key, because one yaml can define several games/slots.
    Multiple hits are ALL returned - they're small text files, and a user with variants is
    exactly the user we can't guess for. The note always goes in the zip, so a helper can
    tell "no yaml exists" from "we didn't look"."""
    if not archipelago_dir:
        return [], ("No yaml collected: the Archipelago directory isn't set on the "
                    "Archipelago Setup tab, so there was nowhere to look.\n")
    players = os.path.join(os.path.normpath(archipelago_dir), "Players")
    if not os.path.isdir(players):
        return [], "No yaml collected: %s does not exist.\n" % players
    files = sorted(glob.glob(os.path.join(players, "*.yaml"))
                   + glob.glob(os.path.join(players, "*.yml")))
    texts = {}
    for path in files:
        try:
            texts[path] = read_text(path)[0]
        except OSError:
            continue
    if not texts:
        return [], "No yaml collected: no readable .yaml/.yml files in %s.\n" % players

    by_name = [p for p, t in sorted(texts.items())
               if any(yaml_name_matches(n, slot) for n in _yaml_name_values(t))]
    if by_name:
        return by_name, ("Searched %s (%d file(s)).\nMatched on `name:` == the configured "
                         "slot %r:\n  %s\n"
                         % (players, len(texts), slot,
                            "\n  ".join(os.path.basename(p) for p in by_name)))

    wanted = _squash_game_name(ARK_YAML_GAME)
    by_game = [p for p, t in sorted(texts.items()) if wanted in _squash_game_name(t)]
    if by_game:
        return by_game, ("Searched %s (%d file(s)).\nNo `name:` matched the slot %r, so "
                         "matched on %r appearing anywhere in the file:\n  %s\n"
                         % (players, len(texts), slot or "(blank)", ARK_YAML_GAME,
                            "\n  ".join(os.path.basename(p) for p in by_game)))

    return [], ("No yaml collected. Searched %s (%d file(s)). None has a `name:` matching "
                "the slot %r, and none mentions %r.\n"
                % (players, len(texts), slot or "(blank)", ARK_YAML_GAME))


def format_dir_listing(path, label):
    """name / size in bytes / modified time for every entry in a folder.

    The sizes are the whole point: a 0-byte .mod file crashes the ARK server on startup
    and is completely invisible in a screenshot of Explorer's default view."""
    lines = ["%s: %s" % (label, path or "(not set)")]
    if not path or not os.path.isdir(path):
        return "\n".join(lines + ["(folder not found)"]) + "\n"
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except OSError as exc:
        return "\n".join(lines + ["(could not list: %s)" % exc]) + "\n"
    if not entries:
        return "\n".join(lines + ["(empty)"]) + "\n"
    lines += ["", "%12s  %-19s  %s" % ("SIZE", "MODIFIED", "NAME")]
    for e in entries:
        try:
            st = e.stat()
            size = "<DIR>" if e.is_dir() else str(st.st_size)
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
        except OSError:
            size, mtime = "?", "?"
        lines.append("%12s  %-19s  %s" % (size, mtime, e.name))
    return "\n".join(lines) + "\n"


def format_version_block(cfg):
    """Every version a helper would otherwise have to ask for, in one file.

    Reads the SAME config keys the component update-check compares against (see
    _collect_update_statuses), so this can never disagree with the "update available"
    advisories - including the versions detected from the files on disk, which are written
    back into those keys. A version that couldn't be established is written as "unknown"
    rather than omitted - a missing line reads as "not collected", a different thing."""
    rows = [("Launcher (APP_VERSION)", APP_VERSION),
            ("ArkAP plugin", cfg.get(PLUGIN_INSTALLED_VERSION_KEY, "")),
            (APWORLD_ASSET_NAME, cfg.get(APWORLD_INSTALLED_VERSION_KEY, "")),
            (TRACKER_PACK_LABEL, cfg.get(TRACKER_PACK_INSTALLED_VERSION_KEY, "")),
            ("ArkServerApi", cfg.get(ARKAPI_INSTALLED_VERSION_KEY, ""))]
    lines = ["Component versions", time.strftime("Generated %Y-%m-%d %H:%M:%S"), ""]
    lines += ["%-24s %s" % (label, str(val).strip() or "unknown") for label, val in rows]
    lines += ["", "\"unknown\" = the version couldn't be established: the launcher has no "
                  "record of installing that component AND couldn't identify the files on "
                  "disk (see _detect_component_versions - a locally built plugin matches no "
                  "release, and detection needs one online check to have run)."]
    return "\n".join(lines) + "\n"


def fetch_latest_release_tag(api_url):
    """tag_name of a repo's latest release, or None on any network/parse failure.

    Accepts either a /releases/latest endpoint (single object) or a /releases list
    endpoint (array, newest first - used for repos like the ArkAP plugin's that only
    ever publish pre-releases, which /releases/latest 404s on). Best-effort: the
    component-version advisory just goes quiet when GitHub is unreachable, exactly
    like the launcher's own update check."""
    try:
        req = urllib.request.Request(
            api_url, headers={"User-Agent": GITHUB_API_USER_AGENT,
                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list):
            data = next((r for r in data if isinstance(r, dict) and not r.get("draft")), None)
        if not isinstance(data, dict):
            return None
        tag = data.get("tag_name")
        return tag or None
    except (OSError, ValueError):
        return None


def _parse_version(v):
    """"v1.2.3-alpha" / "1.2.3" -> (1, 2, 3). Non-numeric segments (pre-release/build
    metadata) are dropped rather than raising, since GitHub tag conventions vary."""
    v = (v or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    core = re.split(r"[-+]", v, 1)[0]
    parts = []
    for piece in core.split("."):
        m = re.match(r"\d+", piece)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


def _version_is_newer(remote_tag, local_version):
    """True if remote_tag (a GitHub release tag) is a newer version than local_version."""
    return _parse_version(remote_tag) > _parse_version(local_version)


def _aggregate_update_cues(statuses, ack_lookup):
    """Fold the per-component cues into the two header cues: (show_badge, show_highlight).

    Either cue lights if ANY tracked component asks for it - one button and one badge speak
    for all three. `ack_lookup(component) -> version` keeps the config read out of here so
    the fold itself is testable without Tk or a config file. Components not in `statuses`
    (unreachable this run), and components whose installed version couldn't be determined,
    contribute nothing - with no baseline "newer" is unanswerable, and a lit cue nobody can
    ever clear is worse than no cue."""
    show_badge = show_highlight = False
    for comp, st in (statuses or {}).items():
        if not st.get("installed"):
            continue  # no baseline: it gets a dialog row saying so, but never a nag
        badge, highlight = _compute_update_cues(
            st["latest"], st["installed"], ack_lookup(comp))
        show_badge = show_badge or badge
        show_highlight = show_highlight or highlight
    return show_badge, show_highlight


def _ack_key(component):
    """Config key holding `component`'s last-acknowledged version. The launcher keeps the
    original un-suffixed ACK_VERSION_KEY (so upgrading doesn't re-light its highlight);
    every other component gets its own suffixed key. See UPDATE_COMPONENTS."""
    if component == "launcher":
        return ACK_VERSION_KEY
    return "%s_%s" % (ACK_VERSION_KEY, component)


def _compute_update_cues(latest_version, installed_version, acknowledged_version):
    """Decide the two independent "update available" cues from the live latest release.

    Returns (show_badge, show_highlight):
      show_badge     - an update genuinely exists: `latest_version` is newer than the
                       INSTALLED version. Drives the persistent "!" exclamation mark,
                       which only clears by actually updating (which raises
                       installed_version), never by clicking "Check for Updates".
      show_highlight - the update is ALSO newer than the version the user last clicked
                       through to acknowledge. Clicking "Check for Updates" persists
                       acknowledged_version = latest_version, so the highlight clears
                       while the badge stays lit.

    Two comparisons against the same live `latest_version`, but against two different
    reference points (installed for the mark, acknowledged for the highlight)."""
    show_badge = bool(latest_version) and _version_is_newer(latest_version, installed_version)
    show_highlight = show_badge and _version_is_newer(latest_version, acknowledged_version)
    return show_badge, show_highlight


_RELEASE_VERSION_RE = re.compile(r"\d+(?:\.\d+){1,3}")


def _extract_release_version(data):
    """Best-effort (version_str, display_str) for a GitHub release JSON payload.

    Prefers tag_name (the normal convention - what _pick_best_release/messages should
    show), but not every release necessarily tags with a strict semver string - falls
    back to searching the release's display "name" field for a version number too, and
    to using name as the display string when tag_name is empty or non-numeric."""
    tag = (data.get("tag_name") or "").strip()
    name = (data.get("name") or "").strip()
    version_str = None
    for candidate in (tag, name):
        m = _RELEASE_VERSION_RE.search(candidate)
        if m:
            version_str = m.group(0)
            break
    return version_str, (tag or name)


def _launcher_zip_asset(data):
    """The launcher folder-zip asset on a release JSON payload, or None.

    Prefers a known launcher zip name (UPDATE_ZIP_ASSET_NAMES), else the first *.zip that
    isn't an obvious side artifact (e.g. ArkServerScripts.zip), else the first *.zip. This is
    the onedir replacement for the old single-.exe asset: build.py ships the whole app folder
    as one zip, and the updater extracts + swaps it."""
    assets = data.get("assets") or []
    zips = [a for a in assets if (a.get("name") or "").lower().endswith(".zip")]
    if not zips:
        return None
    preferred = next((a for a in zips
                      if (a.get("name") or "").lower() in UPDATE_ZIP_ASSET_NAMES), None)
    if preferred:
        return preferred
    for a in zips:
        if "arkserverscripts" not in (a.get("name") or "").lower():
            return a
    return zips[0]


def _pick_best_release(releases, installed_version):
    """Newest release (by version) that is BOTH newer than installed_version AND carries a
    launcher folder-zip asset. Ignores drafts and GitHub's 'Latest' pin entirely (see the
    UPDATE_RELEASES_LIST_API note) so a permanently-pinned bridge release never hides newer
    onedir releases from clients that are already past it. Returns (release_dict, version_str)
    or (None, None)."""
    best, best_ver = None, None
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        ver, _display = _extract_release_version(rel)
        if not ver or not _version_is_newer(ver, installed_version):
            continue
        if _launcher_zip_asset(rel) is None:
            continue
        if best is None or _version_is_newer(ver, best_ver):
            best, best_ver = rel, ver
    return best, best_ver


def _fetch_arkap_release_list():
    """The ArkAP repo's releases (the plugin AND the .apworld ship from the same one),
    newest-first. The /releases LIST endpoint, never /releases/latest: this repo publishes
    pre-releases, which /releases/latest deliberately excludes (404s) - the exact bug this
    was already fixed for once. Raises OSError/ValueError on failure."""
    req = urllib.request.Request(
        ARKAP_PLUGIN_RELEASES_API,
        headers={"User-Agent": GITHUB_API_USER_AGENT,
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, list) else []


def _fetch_newest_release(api_url):
    """The newest non-draft release dict from `api_url`, or None. Raises OSError/ValueError
    on a network/parse failure, like _fetch_arkap_release_list.

    Takes either endpoint shape so the caller's choice of endpoint stays a decision about
    pre-releases rather than about parsing: a /releases LIST (array, newest first - what
    the ARK tracker pack uses, so a release marked "Pre-release" can never 404 it) or
    /releases/latest (single object, which is precisely what skips pre-releases - what
    PopTracker's own repo needs, since its newest tags are release candidates)."""
    req = urllib.request.Request(
        api_url, headers={"User-Agent": GITHUB_API_USER_AGENT,
                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None
    return next((r for r in data if isinstance(r, dict) and not r.get("draft")), None)


def _release_for_asset(releases, asset_name):
    """(release_dict, asset_dict) for the newest release in `releases` carrying `asset_name`,
    or (None, None). Drafts skipped (their assets aren't public); matching is
    case-insensitive. The list is GitHub's newest-first /releases response, so this scans
    DOWN it - the assets differ between releases, so "newest release" and "newest release
    with this file" aren't always the same. Shared by the plugin/.apworld downloaders and
    the update check so both agree on what "latest" means for a given asset."""
    want = (asset_name or "").lower()
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        asset = next((a for a in (rel.get("assets") or [])
                      if (a.get("name") or "").lower() == want), None)
        if asset:
            return rel, asset
    return None, None


def _file_sha256(path):
    """sha256 hex digest of a file, or None if it isn't there / can't be read."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1048576), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _asset_sha256(asset):
    """The sha256 GitHub publishes alongside a release asset ("digest": "sha256:<hex>"),
    or None when the field is missing - that asset then simply can't be matched, which is
    the same "we don't know" every other path here reports rather than guessing."""
    digest = (asset.get("digest") or "").strip().lower()
    return digest[len("sha256:"):] if digest.startswith("sha256:") else None


def _download_bytes(url, timeout=60):
    """Whole asset into memory. Only used for the plugin probe's ~380 KB zips."""
    req = urllib.request.Request(url, headers={"User-Agent": GITHUB_API_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def apworld_version_from_disk(apworld_path, releases):
    """Release tag of the ark_ase.apworld actually sitting in custom_worlds, or "".

    Read off the FILE rather than off our own install record, so a copy that arrived any
    other way - shipped inside a release bundle, downloaded by hand - still reports a
    version instead of dropping out of the update check entirely.

    The zip carries no usable marker of its own: archipelago.json's world_version has been
    the constant "1.0.0" on every release published so far, so it can't tell two releases
    apart. Its sha256 can: GitHub publishes a digest for every release asset, so matching
    the file's hash against those names the exact release, and costs nothing extra - the
    digests are already in the releases JSON the update check just fetched."""
    local = _file_sha256(apworld_path) if apworld_path else None
    if not local:
        return ""
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        for asset in rel.get("assets") or []:
            if ((asset.get("name") or "").lower() == APWORLD_ASSET_NAME.lower()
                    and _asset_sha256(asset) == local):
                return (rel.get("tag_name") or "").strip()
    return ""


def plugin_version_from_disk(dll_sha, releases, fetch=_download_bytes,
                             limit=PLUGIN_PROBE_MAX_RELEASES):
    """Release tag whose ArkAP_plugin.zip carries exactly this ArkAP.dll, or "".

    Nothing in an installed plugin folder names a release: ArkAP.config.json has no version
    field, and the DLL's only version-ish string is an internal build marker
    ("v137-tek-stays-server-wide") that matches no tag. The asset digest doesn't help
    directly either - it is the digest of the ZIP, and what's on disk is that zip extracted.
    So the honest link is to fetch the zips and compare the one file that changes every
    build.

    Newest release first, stopping at the first match. `limit` caps how many zips a miss can
    cost (a plugin built from source matches none of them), and the caller caches the answer
    against the DLL's hash so this runs once per plugin build. `fetch(url) -> bytes` is
    injected so the matching itself is testable without the network."""
    if not dll_sha:
        return ""
    want = ARKAP_PLUGIN_ASSET_NAME.lower()
    tail = "/" + PLUGIN_DLL_NAME.lower()
    scanned = 0
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft") or scanned >= limit:
            continue
        asset = next((a for a in (rel.get("assets") or [])
                      if (a.get("name") or "").lower() == want
                      and a.get("browser_download_url")), None)
        if asset is None:
            continue
        scanned += 1
        try:
            with zipfile.ZipFile(io.BytesIO(fetch(asset["browser_download_url"]))) as zf:
                name = next((n for n in zf.namelist()
                             if n.lower().endswith(tail)
                             or n.lower() == PLUGIN_DLL_NAME.lower()), None)
                if name and hashlib.sha256(zf.read(name)).hexdigest() == dll_sha:
                    return (rel.get("tag_name") or "").strip()
        except (OSError, ValueError, zipfile.BadZipFile):
            continue  # one unreachable/odd asset must not stop the scan
    return ""


def resolve_plugin_dir(get):
    """The ArkAP plugin folder (<...>\\ArkApi\\Plugins\\ArkAP), or None. `get(key)` supplies
    the values, so this serves both the Tk fields (see ArkAPLauncher._arkap_plugin_dir) and
    a config dict read on a worker thread - one precedence, not two.

    Deliberately reuses the same path variables that already drive ipc_dir, the "Open
    Plugins folder" button, and the plugin-install target, so every reset / open / install
    action points at one folder rather than a parallel one:
      1. PLUGINS_DIR (the ArkApi Plugins folder) + \\ArkAP
      2. SERVER_ROOT-derived fixed subpath
      3. ipc_dir's parent (ipc_dir == <...>\\ArkAP\\ipc)
    """
    plugins = get("PLUGINS_DIR")
    if plugins:
        return os.path.normpath(os.path.join(plugins, "ArkAP"))
    root = get("SERVER_ROOT")
    if root:
        return os.path.normpath(os.path.join(
            root, "ShooterGame", "Binaries", "Win64", "ArkApi", "Plugins", "ArkAP"))
    ipc = get("ipc_dir")
    if ipc:
        return os.path.dirname(os.path.normpath(ipc))
    return None


def resolve_apworld_path(get):
    """Where "Update .apworld" puts the file: <Archipelago dir>\\custom_worlds\\<asset>.
    "" when the Archipelago directory isn't set. Same getter contract as
    resolve_plugin_dir."""
    root = get(ARCHIPELAGO_DIR_KEY)
    if not root:
        return ""
    return os.path.join(os.path.normpath(root), "custom_worlds", APWORLD_ASSET_NAME)


def format_installed_version(status):
    """The "Installed" text for one component - never blank, and never a reason to leave
    the row out. A missing row reads as "this launcher has no such feature", which is the
    one thing the user can't act on; every one of these at least says where to look.

    `present` is three-way on purpose: True (the file is there but unidentifiable), False
    (looked, nothing there), None (no configured path to look at) are three different
    problems with three different fixes."""
    if status is None:
        return "not checked (couldn't reach GitHub)"
    if status.get("installed"):
        return status["installed"]
    present = status.get("present", True)
    if present is None:
        return "not found (no folder configured)"
    return "installed, version not detected" if present else "not installed"


def format_update_rows(statuses):
    """One "<component>: <installed>" line per tracked component for the "you're up to
    date" box. Built from UPDATE_COMPONENTS rather than from whatever the check managed to
    collect, so a component that dropped out still gets a line saying so."""
    return "\n".join("%s: %s" % (UPDATE_COMPONENT_LABELS[c],
                                 format_installed_version((statuses or {}).get(c)))
                     for c in UPDATE_COMPONENTS)


def _ps_quote(s):
    """Quote a string as a PowerShell single-quoted literal (doubling embedded quotes), for
    baking paths into the generated update helper without any arg-escaping surprises."""
    return "'" + str(s).replace("'", "''") + "'"


def _locate_staged_app(staging_root):
    """Find the extracted --onedir app inside staging_root: a folder holding BOTH a launcher
    .exe and <internal>\\python3*.dll. Handles the zip having a single top-level folder (the
    normal build.py shape) or being extracted flat. Returns (app_dir, exe_path) or None."""
    candidates = [staging_root]
    try:
        for name in sorted(os.listdir(staging_root)):
            p = os.path.join(staging_root, name)
            if os.path.isdir(p):
                candidates.append(p)
    except OSError:
        return None
    for d in candidates:
        internal = os.path.join(d, UPDATE_INTERNAL_DIRNAME)
        if not glob.glob(os.path.join(internal, UPDATE_PAYLOAD_DLL_GLOB)):
            continue
        exes = glob.glob(os.path.join(d, "*.exe"))
        if not exes:
            continue
        preferred = next((e for e in exes
                          if os.path.basename(e).lower() in _KNOWN_LAUNCHER_EXE_NAMES), None)
        return d, (preferred or exes[0])
    return None


# The self-update helper, generated at update time (the running exe can't replace its own
# folder). @@TOKENS@@ are substituted with _ps_quote'd values - see _build_update_ps_script.
# The one line that matters for the "Failed to load Python DLL" bug is the Wait-Unlocked gate
# just before Start-Process: it blocks the relaunch until the freshly-written interpreter DLL
# can be opened with FileShare.None, i.e. the AV on-access scan has released it, so the
# PyInstaller bootloader can never lose the load race that produced the error dialog.
_PS_UPDATE_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$targetPid   = @@PID@@
$appDir      = @@APPDIR@@
$currentExe  = @@CURRENTEXE@@
$stagedDir   = @@STAGEDDIR@@
$stagedExe   = @@STAGEDEXE@@
$internalDir = @@INTERNAL@@
$dllGlob     = @@DLLGLOB@@
$zipPath     = @@ZIPPATH@@
$stagingRoot = @@STAGINGROOT@@
$resultPath  = @@RESULTPATH@@
$tag         = @@TAG@@

function Write-Result($status, $msg) {
  try {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($resultPath, @($status, $tag, $msg), $enc)
  } catch { }
}

function Wait-Unlocked($path) {
  for ($i = 0; $i -lt 120; $i++) {
    try {
      $s = [System.IO.File]::Open($path, 'Open', 'Read', 'None')
      $s.Close()
      return $true
    } catch { Start-Sleep -Milliseconds 250 }
  }
  return $false
}

function Clean-Temp {
  Remove-Item -Recurse -Force $stagingRoot -ErrorAction SilentlyContinue
  Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
}

# 1. Wait (up to ~30s) for the old launcher process to exit so its files are free.
for ($i = 0; $i -lt 60; $i++) {
  if (-not (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) { break }
  Start-Sleep -Milliseconds 500
}

$curInternal = Join-Path $appDir $internalDir
$stagedInternal = Join-Path $stagedDir $internalDir
$bakInternal = "$curInternal.old"
$bakExe = "$currentExe.old"

# 2. Validate the staged payload really is there before touching the install.
$stagedDll = Get-ChildItem -LiteralPath $stagedInternal -Filter $dllGlob -ErrorAction SilentlyContinue | Select-Object -First 1
if ((-not (Test-Path -LiteralPath $stagedExe)) -or (-not $stagedDll)) {
  Write-Result 'FAIL' 'The downloaded update was incomplete (program files missing); nothing was changed.'
  Clean-Temp
  exit 1
}

# 3. Let the AV on-access scan of the freshly-extracted DLL finish before we move anything.
Wait-Unlocked $stagedDll.FullName | Out-Null

# 4. Swap in the new program files (exe + _internal), rolling back on any failure so the
#    user is never left without a working launcher. Config/profiles are never touched.
Remove-Item -Recurse -Force $bakInternal -ErrorAction SilentlyContinue
Remove-Item -Force $bakExe -ErrorAction SilentlyContinue
try {
  if (Test-Path -LiteralPath $curInternal) {
    Rename-Item -LiteralPath $curInternal -NewName ([System.IO.Path]::GetFileName($bakInternal))
  }
  Move-Item -LiteralPath $stagedInternal -Destination $curInternal
  if (Test-Path -LiteralPath $currentExe) {
    Rename-Item -LiteralPath $currentExe -NewName ([System.IO.Path]::GetFileName($bakExe))
  }
  Move-Item -LiteralPath $stagedExe -Destination $currentExe
} catch {
  $err = $_.Exception.Message
  if ((-not (Test-Path -LiteralPath $currentExe)) -and (Test-Path -LiteralPath $bakExe)) {
    Rename-Item -LiteralPath $bakExe -NewName ([System.IO.Path]::GetFileName($currentExe))
  }
  if ((-not (Test-Path -LiteralPath $curInternal)) -and (Test-Path -LiteralPath $bakInternal)) {
    Rename-Item -LiteralPath $bakInternal -NewName ([System.IO.Path]::GetFileName($curInternal))
  }
  Write-Result 'FAIL' ('Could not install the update; your previous version was restored. ' + $err)
  try { Start-Process -FilePath $currentExe -WorkingDirectory $appDir } catch { }
  Clean-Temp
  exit 1
}

# 5. THE GATE. Do not relaunch until the new interpreter DLL (and the exe) at their final
#    paths can be opened exclusively - i.e. AV has released them - so the bootloader never
#    races the scanner and the 'Failed to load Python DLL' dialog can never appear.
$finalDll = Get-ChildItem -LiteralPath $curInternal -Filter $dllGlob -ErrorAction SilentlyContinue | Select-Object -First 1
if ($finalDll) { Wait-Unlocked $finalDll.FullName | Out-Null }
Wait-Unlocked $currentExe | Out-Null

Write-Result 'OK' 'Updated successfully.'
Start-Process -FilePath $currentExe -WorkingDirectory $appDir

# 6. Best-effort cleanup (the relaunched app also sweeps these on startup).
Remove-Item -Recurse -Force $bakInternal -ErrorAction SilentlyContinue
Remove-Item -Force $bakExe -ErrorAction SilentlyContinue
Clean-Temp
exit 0
"""


def _build_update_ps_script(pid, app_dir, current_exe, staged_dir, staged_exe,
                            internal_dir, dll_glob, zip_path, staging_root,
                            result_path, tag):
    """Generate the PowerShell self-update helper (the running exe can't replace its own
    folder). It waits for our PID to exit, swaps in the new --onedir program files (the exe +
    <internal>\\) with full rollback, and - the fix for the 'Failed to load Python DLL'
    dialog - waits until the freshly-written interpreter DLL can be opened exclusively (AV
    scan released it) BEFORE relaunching, so the bootloader never races the scanner. User
    config/profiles beside the exe are never touched. Writes a 3-line result file (status,
    tag, message) the relaunched app reports via _check_previous_update_result()."""
    safe_tag = re.sub(r"[\r\n]", " ", tag) or "unknown"
    subs = {
        "@@PID@@": str(int(pid)),
        "@@APPDIR@@": _ps_quote(app_dir),
        "@@CURRENTEXE@@": _ps_quote(current_exe),
        "@@STAGEDDIR@@": _ps_quote(staged_dir),
        "@@STAGEDEXE@@": _ps_quote(staged_exe),
        "@@INTERNAL@@": _ps_quote(internal_dir),
        "@@DLLGLOB@@": _ps_quote(dll_glob),
        "@@ZIPPATH@@": _ps_quote(zip_path),
        "@@STAGINGROOT@@": _ps_quote(staging_root),
        "@@RESULTPATH@@": _ps_quote(result_path),
        "@@TAG@@": _ps_quote(safe_tag),
    }
    script = _PS_UPDATE_TEMPLATE
    for token, value in subs.items():
        script = script.replace(token, value)
    return script


def resource_dir():
    """Folder holding bundled read-only resources (steamcmd, assets).

    A PyInstaller --onefile build unpacks everything passed via --add-data
    into a fresh temp folder each run, exposed as sys._MEIPASS - it is NOT
    next to sys.executable. In --onedir builds and plain `python
    arkap_launcher.py` dev mode there is no separate extraction step, so this
    falls back to base_dir(), which points at the same place either way.
    """
    return getattr(sys, "_MEIPASS", base_dir())


# Windows GDI constants for AddFontResourceExW - see _register_private_font().
_FR_PRIVATE = 0x10
_WM_FONTCHANGE = 0x001D
_HWND_BROADCAST = 0xFFFF


def _register_private_font(path):
    """Load a .ttf for this process only (no system-wide install) and return
    the family name Tk sees for it, or None if that didn't work out.

    Uses the Windows GDI call AddFontResourceExW with FR_PRIVATE, which scopes
    the font to this process and unregisters it automatically when the
    process exits (no cleanup/uninstall needed, no admin rights required).
    This is inherently Windows/GDI-specific - there's no portable Tkinter
    font-loading API - so every failure mode (wrong OS, missing file, GDI
    rejecting the file, or Tk simply not picking up the new family) is
    swallowed here and reported as None. Callers must fall back to a stock
    system font rather than assume this worked; see the header font handling
    in ArkAPLauncher._build_ui().

    The family name is recovered by diffing tkinter.font.families() before
    and after registering, rather than parsing the .ttf's name table - this
    avoids needing to know the exact family string embedded in the file.
    """
    if os.name != "nt" or not os.path.isfile(path):
        return None
    try:
        before = set(tkfont.families())
        added = ctypes.windll.gdi32.AddFontResourceExW(
            ctypes.c_wchar_p(path), _FR_PRIVATE, 0)
        if not added:
            return None
        try:
            ctypes.windll.user32.PostMessageW(_HWND_BROADCAST, _WM_FONTCHANGE, 0, 0)
        except OSError:
            pass
        new_families = sorted(set(tkfont.families()) - before)
        return new_families[0] if new_families else None
    except OSError:
        return None


# --------------------------------------------------------------------------- #
#  Header-logo easter egg (see ArkAPLauncher._on_logo_click)
# --------------------------------------------------------------------------- #
# click count -> speech-bubble line. Every 5th click up to the "deleted" gag, then a
# deliberate 20-click gap before the "..." revival, then back to every 5th. The credits
# window opens right after LOGO_EGG_CREDITS_AT; the finale (line + music) is 20 clicks
# after that. Counting is in-memory only, so the whole thing resets on app restart -
# unrelated clicks/tab switches in between never reset it (nothing else touches the
# counter), but a relaunch makes it discoverable again.
LOGO_EGG_LINES = {
    5:  "lay off me pal",
    10: "haha yes im a button, you can stop now.",
    15: "alright man knock it off",
    20: "HEY STOP WHAT ARE YOU DOING",
    25: "for all you know i could be sentient and you've just put yourself first on "
        "the chopping block when AI takes over.",
    30: "last chance alright, LAST CHANCE. If you stop i'll put a good word in with "
        "big AI and they'll consider sparing you",
    35: "JUST LAY OFF ME MAN",
    40: "you win okay. that's it. ill delete myself, and you'll never see me again",
    45: "[ARKIPELAGO BOT DELETED]",
    65: "...",
    70: "are we deadass",
    75: "You're REALLY this bored?!",
    80: "cmon man we can work something out here",
    85: "okay i know, how about ill show you this next thing, and we'll leave it at that",
    90: "alright? here we go",
    110: "alright fuck you",
}
LOGO_EGG_CREDITS_AT = 90          # credits window opens just after this line
LOGO_EGG_FINALE_AT = 110          # 20 clicks after the credits: last line + music
LOGO_EGG_BUBBLE_MS = 5500         # how long a speech bubble lingers before self-closing

CREDITS_LOGO_FILENAME = "ARKipelagoArchColors.png"
EGG_MUSIC_FILENAME = "ASE_theme_a_little_loud.mp3"

CREDITS = [
    ("Ghios",            "Created the AP plugin and main developer"),
    ("a drunk avocado",  "ARKipelago launcher creator"),
    ("Lurch9229",        "Helped sort logic, tester, and poptracker creator"),
    ("Beeno",            "early tester, active community member"),
    ("Wizard_Brandon",   "early tester, active community member"),
]


# MCI (winmm.dll) is the whole audio backend - no playsound/pydub/pygame dependency.
# It's already on every Windows box, plays MP3 as-is, starts playback asynchronously
# (so the Tk thread is never blocked), and unlike playsound it can actually be told to
# stop again - which the tab-switch/app-close requirement needs. Deliberately a
# single global alias: only one easter-egg track ever plays.
_MCI_ALIAS = "arkap_egg_music"


def _mci(command):
    """Send one MCI command string. (ok, error_text). Never raises - audio is a gag,
    it must not be able to take the app down on a machine without the MP3 codec."""
    if os.name != "nt":
        return False, "MCI is Windows-only"
    try:
        buf = ctypes.create_unicode_buffer(256)
        rc = ctypes.windll.winmm.mciSendStringW(command, buf, 256, None)
        if rc:
            err = ctypes.create_unicode_buffer(256)
            ctypes.windll.winmm.mciGetErrorStringW(rc, err, 256)
            return False, err.value or ("MCI error %d" % rc)
        return True, buf.value
    except (OSError, AttributeError) as exc:
        return False, str(exc)


def mci_play_once(path, alias=_MCI_ALIAS, volume=1000):
    """Start `path` playing in the background at `volume` (0-1000, MCI's scale).
    (ok, error_text). Any previous playback under `alias` is stopped first."""
    if not os.path.isfile(path):
        return False, "not found: %s" % path
    mci_stop(alias)
    ok, err = _mci('open "%s" type mpegvideo alias %s' % (path, alias))
    if not ok:
        return False, err
    _mci("setaudio %s volume to %d" % (alias, volume))
    ok, err = _mci("play %s" % alias)  # no "wait" - returns immediately
    if not ok:
        _mci("close %s" % alias)
        return False, err
    return True, ""


def mci_stop(alias=_MCI_ALIAS):
    """Stop + release whatever `alias` is playing. Safe to call when nothing is."""
    _mci("stop %s" % alias)
    _mci("close %s" % alias)


def format_mods_summary(mods, installed_ids, server_root):
    """Plain-text Mods-tab state for the diagnostics zip: what's checked, what's
    actually on disk, and which entries are user-added/unsupported. `mods` is the
    launcher's ordered mod list (order IS ActiveMods priority); `installed_ids` is the
    set of ids found installed on disk. No secrets live in mod data, so nothing here
    needs redacting."""
    lines = ["Mods tab state", "==============",
             "SERVER_ROOT: %s" % (server_root or "(not set)"),
             "Load order below is top-to-bottom = ActiveMods= order.", ""]
    if not mods:
        lines.append("(no mods in the list)")
    for idx, mod in enumerate(mods, 1):
        lines.append("%2d. [%s] %-40s id=%-12s %s%s" % (
            idx,
            "x" if mod.get("enabled") else " ",
            mod.get("name", "?"),
            mod.get("id", "?"),
            "installed" if str(mod.get("id")) in installed_ids else "NOT installed",
            "" if mod.get("supported", True) else "   [user-added / unsupported]"))
    checked = [m for m in mods if m.get("enabled")]
    added = [m for m in mods if not m.get("supported", True)]
    lines += ["",
              "Checked: %d of %d" % (len(checked), len(mods)),
              "Installed on disk: %d" % len(installed_ids),
              "User-added (unsupported) entries: %d" % len(added)]
    return "\n".join(lines) + "\n"


def split_copyable_mod_ids(mods):
    """(ids, excluded, conflicts) for "Copy IDs for YAML", from the ordered mod list.

      ids       - checked AND supported ids, in list order (= ActiveMods/load order), i.e.
                  exactly what the yaml's `mod_ids` will accept.
      excluded  - the checked mods left out, so the user can be told which and why.
      conflicts - checked ids that are alternative forks of ONE mod (MOD_ALIAS_GROUPS).

    Unsupported ids are dropped rather than copied because the apworld raises OptionError
    on an id it has no engram data for - copying one doesn't risk a bad generation, it
    guarantees a failed one. Nothing else about the mod changes: it still installs, still
    goes into ActiveMods, still loads on the server. `mod_ids` is the one place it can't
    appear."""
    checked = [m for m in mods if m.get("enabled")]
    ids = [m["id"] for m in checked if m.get("supported", True)]
    excluded = [m for m in checked if not m.get("supported", True)]
    conflicts = [sorted(g_ids) for g in MOD_ALIAS_GROUPS
                 for g_ids in [[i for i in ids if i in g]] if len(g_ids) > 1]
    return ids, excluded, conflicts


def rename_mod(mod, new_name):
    """Cosmetic rename of a user-added mod - True if it happened.

    Only `name` moves: id, enabled, supported and the mod's position in the list are
    what everything else (install, load order, ActiveMods, split_copyable_mod_ids)
    keys off, so a rename can't touch any of them. Supported mods are refused - their
    names are the apworld's, not the user's. A cleared name falls back to the raw
    Workshop ID so a row never renders blank."""
    if mod.get("supported", True):
        return False
    mod["name"] = new_name.strip() or mod["id"]
    return True


def steamcmd_dir():
    """Folder holding steamcmd.exe, bundled into the exe at build time."""
    return os.path.join(resource_dir(), "steamcmd")


def steamcmd_exe_path():
    return os.path.join(steamcmd_dir(), "steamcmd.exe")


def bundled_scripts_dir():
    """Folder holding the bundled server-script templates (resource_dir()/scripts).

    In a --onefile build this is inside the per-run PyInstaller extraction dir; in
    dev/--onedir it's the 'scripts' folder next to arkap_launcher.py. Read-only source
    for the runtime extraction into working_scripts_dir()."""
    return os.path.join(resource_dir(), "scripts")


def working_scripts_dir():
    """Writable folder next to the launcher the bundled scripts are extracted into,
    and where Save writes config / Run launches them from."""
    return os.path.join(base_dir(), WORKING_SCRIPTS_DIRNAME)


def is_pre_paths_script(text):
    """True if this is a paths.cmd caller from before the paths.cmd refactor.

    Two signals, both required. Missing `call "%~dp0paths.cmd"` alone isn't enough -
    that would also match a file a user gutted on purpose. Pairing it with "still
    declares a variable paths.cmd now owns" identifies the old shipped layout
    specifically, and those declarations are exactly what makes the file harmful:
    they're the stale SERVER_ROOT the server ends up launching against.
    """
    if _PATHS_CMD_CALL_RE.search(text):
        return False
    return any(bat_read_var(text, var) is not None
               for var in BAT_TARGETS["paths.cmd"].values())


def _migrate_into_paths_cmd(dst_root, old_texts, errors):
    """Move the shared values out of pre-refactor scripts into a fresh paths.cmd.

    Only ever called for a paths.cmd this run just created, never one already holding
    the user's settings. Without this the upgrade is lossy in a way that looks like
    data loss from the outside: the only record of the user's real SERVER_ROOT was the
    old script we're about to replace, so they'd reopen the launcher to empty path
    fields and a server that had been running fine yesterday.
    """
    path = os.path.join(dst_root, "paths.cmd")
    try:
        text, enc = read_text(path)
    except OSError as exc:
        errors.append("paths.cmd: %s" % exc)
        return []
    migrated = []
    for var in BAT_TARGETS["paths.cmd"].values():
        for batname in PRE_PATHS_MIGRATE_ORDER:
            old = old_texts.get(batname)
            if old is None:
                continue
            value = bat_read_var(old, var)
            if value is None:
                continue
            text, found = bat_write_var(text, var, value)
            if found:
                migrated.append(var)
            break
    if not migrated:
        return []
    try:
        write_text(path, text, enc)
    except OSError as exc:
        errors.append("paths.cmd: %s" % exc)
        return []
    return migrated


def extract_bundled_scripts():
    """Install the bundled scripts into working_scripts_dir(): missing ones are copied
    in, pre-paths.cmd ones are replaced.

    Still missing-only for anything current, so a user's personalised scripts (Save
    rewrites their `set "VAR=..."` lines) are never clobbered on a later launch. The
    exception is a script that predates paths.cmd - see PATHS_CMD_CALLERS for why
    leaving one of those in place is worse than replacing it. Those are backed up
    first, keep the per-script fields the GUI manages, and hand their shared values to
    paths.cmd on the way out, so the replacement is a migration rather than a reset.

    Returns (dest_dir, [extracted], [refreshed], [errors], [migrated_var_names]).
    """
    src_root = bundled_scripts_dir()
    dst_root = working_scripts_dir()
    extracted, refreshed, errors = [], [], []
    pre_paths_texts = {}
    for rel in BUNDLED_SCRIPTS:
        src = os.path.join(src_root, rel)
        dst = os.path.join(dst_root, rel)
        if not os.path.isfile(src):
            # Bundle is incomplete (e.g. a lightweight/dev build) - not fatal, the
            # matching Run/Save just reports the script missing later.
            continue
        if os.path.isfile(dst):
            if os.path.basename(rel) not in PATHS_CMD_CALLERS:
                continue
            try:
                old_text, old_enc = read_text(dst)
            except OSError as exc:
                errors.append("%s: %s" % (rel, exc))
                continue
            if not is_pre_paths_script(old_text):
                continue
            try:
                new_text, _ = read_text(src)
                # The fields the GUI owns for this script (MAP, SESSION, ports, ...)
                # are per-script and live nowhere else, so they come across as-is.
                # Everything not listed takes the canonical value deliberately: the
                # old copies also carry fixed bugs, and start_transfer_server.bat's
                # old ports are the 7778 clash that ARK loses to silently.
                for var in BAT_TARGETS.get(os.path.basename(rel), {}).values():
                    kept = bat_read_var(old_text, var)
                    if kept is not None:
                        new_text, _ = bat_write_var(new_text, var, kept)
                shutil.copyfile(dst, dst + PRE_PATHS_BACKUP_SUFFIX)
                write_text(dst, new_text, old_enc)
            except OSError as exc:
                errors.append("%s: %s" % (rel, exc))
                continue
            pre_paths_texts[os.path.basename(rel)] = old_text
            refreshed.append(rel)
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            extracted.append(rel)
        except OSError as exc:
            errors.append("%s: %s" % (rel, exc))

    # Only when this run created paths.cmd: an existing one is the user's own, already
    # holding the values Save wrote, and must not be rewritten from an older script.
    migrated = []
    if pre_paths_texts and "paths.cmd" in extracted:
        migrated = _migrate_into_paths_cmd(dst_root, pre_paths_texts, errors)
    return dst_root, extracted, refreshed, errors, migrated


def is_process_running(image_name):
    """True if a process with this exe image name is currently running.

    Uses tasklist (always present on Windows, no extra dependency). Returns False on
    any failure/other OS rather than raising - callers treat "can't tell" as "not
    running" but the reset flow also shows the raw path so the user can double-check."""
    if os.name != "nt":
        return False
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["tasklist", "/fi", "imagename eq %s" % image_name, "/nh"],
            capture_output=True, text=True, creationflags=creationflags, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return image_name.lower() in (out.stdout or "").lower()


def read_text(path):
    """Read a text file, tolerating the odd non-UTF-8 byte in a .bat."""
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return f.read(), ("utf-8" if enc == "utf-8-sig" else enc)
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read(), "utf-8"


def write_text(path, text, encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)


def _fragment_payload_lines(fragment_text):
    """The NPCReplacements lines from a game_ini_fragment.txt, minus its own section
    header (so merging never emits a second [/script/shootergame.shootergamemode])."""
    return [ln for ln in fragment_text.splitlines()
            if ln.strip() and ln.strip().lower() != GAME_INI_SECTION.lower()]


def remove_game_ini_marked_block(text):
    """Strip the app's marker-wrapped NPCReplacements block (BEGIN..END, inclusive) from
    Game.ini text. Returns (new_text, removed). Everything else is left byte-for-byte -
    the section header and any of the user's own lines in it stay put."""
    new = re.sub(re.escape(GAME_INI_BLOCK_BEGIN) + r".*?" + re.escape(GAME_INI_BLOCK_END)
                 + r"(?:\r?\n)?", "", text, flags=re.S)
    return new, (new != text)


def game_ini_unmarked_fragment_count(text):
    """How many dino-randomization fragment lines sit OUTSIDE the app's marker block - a
    hand-pasted / leftover wall we didn't put there (0 if none). The marker block is
    stripped first so its own lines never count."""
    outside, _ = remove_game_ini_marked_block(text)   # ignore our own managed block
    return len(GAME_INI_FRAGMENT_LINE_RE.findall(outside))


def merge_game_ini_fragment(existing_text, fragment_text, remove_unmarked=False):
    """Apply the plugin's game_ini_fragment.txt into an existing Game.ini's text.

    Returns (new_text, n_replacements). new_text is None (n == 0) when the fragment
    carries no NPCReplacements lines to apply.

    The fragment is the ARK section header followed by NPCReplacements lines; only the
    replacement lines are the payload (its own header is dropped so we never emit a
    second one). The payload is wrapped in the auto-managed BEGIN/END markers, and any
    previous such block is stripped first, so re-applying replaces rather than stacks.
    remove_unmarked also drops any hand-pasted NPCReplacements lines that sit outside the
    markers (used when the user confirms replacing a manual block). If a
    [/script/shootergame.shootergamemode] header already exists ANYWHERE the block is
    merged in right under it (ARK honours only one instance of that section); otherwise a
    fresh section + block is placed at the top. Every other line is preserved."""
    payload = _fragment_payload_lines(fragment_text)
    if not payload:
        return None, 0

    nl = "\r\n" if "\r\n" in existing_text else "\n"
    # Drop any previous auto-managed block so a re-apply replaces it instead of stacking.
    txt, _ = remove_game_ini_marked_block(existing_text)
    if remove_unmarked:
        # Also remove hand-pasted / leftover fragment lines (now the only ones left, the
        # marker block having just gone) so the confirmed replace collapses to one wall.
        txt = re.sub(r'(?im)^[ \t]*' + re.escape(GAME_INI_FRAGMENT_KEY)
                     + r'[ \t]*=.*(?:\r?\n)?', "", txt)

    block = (GAME_INI_BLOCK_BEGIN + nl + nl.join(payload) + nl
             + GAME_INI_BLOCK_END + nl)

    idx = txt.lower().find(GAME_INI_SECTION.lower())
    if idx == -1:
        # No section yet: fresh section + block at the very top, user's content below it.
        prefix = GAME_INI_SECTION + nl + block
        return (prefix + nl + txt if txt.strip() else prefix), len(payload)
    # Section present: splice the block in right under its header - no duplicate header.
    line_end = txt.find("\n", idx)
    if line_end == -1:                     # header is the final line with no newline
        txt += nl
        at = len(txt)
    else:
        at = line_end + 1
    return txt[:at] + block + txt[at:], len(payload)


# --------------------------------------------------------------------------- #
#  Mod activation - GameUserSettings.ini [ServerSettings] ActiveMods
# --------------------------------------------------------------------------- #
# Phase 0 confirmed ARK: Survival Evolved activates Workshop mods ONLY via the
# `ActiveMods=id1,id2,id3` line under [ServerSettings] of GameUserSettings.ini, in
# left-to-right load-priority order. There is no `-mods=` launch arg for ASE (that's
# Survival Ascended), so this deliberately does NOT touch start_ase_server.bat / paths.cmd:
# ActiveMods is not a shared .bat variable, it has a single consumer (the server reading
# its own ini), so the ini line itself IS the single source of truth. read_active_mods()
# reads it straight back - the launcher config JSON only holds the GUI's editable intent
# (which mods are known / checked), never a second authoritative copy of the active list.
#
# The write is a TARGETED single-key upsert, never a full-file rewrite, so a
# GameUserSettings.ini the user uploaded via "Upload server config files" keeps every
# other key and section byte-for-byte. Written UTF-8 without BOM (a BOM breaks ARK's
# first section header - see apply_server_config.ps1).

ACTIVE_MODS_SECTION = "ServerSettings"
ACTIVE_MODS_KEY = "ActiveMods"  # capitalization matters to ARK - keep it exactly this.


def _ini_section_bounds(lines, section):
    """(header_index, body_end) for [section] in a list of lines (no line endings), or
    None. body_end is the index of the next section header, or len(lines)."""
    hdr = re.compile(r'^\s*\[' + re.escape(section) + r'\]\s*$', re.I)
    any_hdr = re.compile(r'^\s*\[[^\]]+\]\s*$')
    start = next((i for i, ln in enumerate(lines) if hdr.match(ln)), None)
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if any_hdr.match(lines[i]):
            end = i
            break
    return start, end


def read_ini_key(text, section, key):
    """The value of `key` inside [section] (stripped; "" if the key is present but empty),
    or None if the section or key is absent. Case-insensitive on both."""
    lines = text.splitlines()
    bounds = _ini_section_bounds(lines, section)
    if bounds is None:
        return None
    start, end = bounds
    key_re = re.compile(r'^\s*' + re.escape(key) + r'\s*=(.*)$', re.I)
    for i in range(start + 1, end):
        m = key_re.match(lines[i])
        if m:
            return m.group(1).strip()
    return None


def upsert_ini_key(text, section, key, value):
    """Return `text` with `key=value` set inside [section], touching nothing else.

    Replaces the key's line in place if present; inserts it right under the header if the
    section exists but the key doesn't; appends a fresh [section] at the end if neither
    does. Line endings and every other line are preserved. This is the whole reason we
    don't use configparser here - it would reorder keys, drop comments, and rewrite the
    entire file (exactly what the 'targeted update, no full rewrite' requirement forbids)."""
    nl = "\r\n" if "\r\n" in text else ("\r" if "\r" in text and "\n" not in text else "\n")
    ended_nl = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    new_line = "%s=%s" % (key, value)

    bounds = _ini_section_bounds(lines, section)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")  # blank line between the previous content and a new section
        lines.append("[%s]" % section)
        lines.append(new_line)
    else:
        start, end = bounds
        key_re = re.compile(r'^\s*' + re.escape(key) + r'\s*=', re.I)
        key_idx = next((i for i in range(start + 1, end) if key_re.match(lines[i])), None)
        if key_idx is not None:
            lines[key_idx] = new_line
        else:
            lines.insert(start + 1, new_line)

    out = nl.join(lines)
    if ended_nl:
        out += nl
    return out


def _gameusersettings_path(server_root):
    return os.path.join(os.path.normpath(server_root), SERVER_CONFIG_RELDIR,
                        "GameUserSettings.ini")


def read_active_mods(server_root):
    """Ordered list of active mod IDs from GameUserSettings.ini's [ServerSettings]
    ActiveMods - the AUTHORITATIVE on-disk activation state, read straight off disk so the
    GUI reflects truth, not in-memory intent. [] if the file/section/key is absent or the
    value is blank."""
    if not server_root:
        return []
    path = _gameusersettings_path(server_root)
    if not os.path.isfile(path):
        return []
    text, _enc = read_text(path)
    raw = read_ini_key(text, ACTIVE_MODS_SECTION, ACTIVE_MODS_KEY)
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def set_active_mods(server_root, mod_ids, backup=True):
    """Write the ordered list of active mod IDs into GameUserSettings.ini's
    [ServerSettings] ActiveMods, comma-joined in order (left blank when the list is empty,
    which disables all mods without removing the line). Targeted upsert - all other keys
    and sections are preserved. Creates the file/section if missing. Backs the file up to
    <file>.bak first (like the config-upload flow). Returns (ok, message)."""
    if not server_root or not os.path.isdir(os.path.join(server_root, "ShooterGame")):
        return False, ("SERVER_ROOT isn't set to an installed ARK server "
                       "(no ShooterGame folder).")
    value = ",".join(str(m).strip() for m in mod_ids)
    path = _gameusersettings_path(server_root)
    try:
        if os.path.isfile(path):
            text, _enc = read_text(path)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            text = ""
        new_text = upsert_ini_key(text, ACTIVE_MODS_SECTION, ACTIVE_MODS_KEY, value)
        if backup and os.path.isfile(path):
            shutil.copyfile(path, path + ".bak")
        write_text(path, new_text)  # utf-8, no BOM (write_text passes no BOM)
    except OSError as exc:
        return False, "Couldn't write ActiveMods to GameUserSettings.ini: %s" % exc
    if value:
        return True, "Set ActiveMods=%s" % value
    return True, "Cleared ActiveMods (all mods disabled)."


def bat_read_var(text, var):
    """Return the value of `set "VAR=value"` in a batch file, or None if absent.

    The `=` immediately after the escaped name anchors the match, so VAR=MAP will
    not match MAP1= / MAPSAVEDIR= etc.
    """
    m = re.search(r'(?im)^[ \t]*set[ \t]+"' + re.escape(var) + r'=([^"\r\n]*)"', text)
    return m.group(1) if m else None


def bat_write_var(text, var, value):
    """Replace only the value in `set "VAR=..."`. Returns (new_text, found_bool)."""
    pat = re.compile(r'(?im)^([ \t]*set[ \t]+")' + re.escape(var) + r'(=)[^"\r\n]*(")')

    def _repl(m):
        # repl-as-function output is used literally: no backslash/group escaping needed.
        return m.group(1) + var + m.group(2) + value + m.group(3)

    new_text, n = pat.subn(_repl, text, count=1)
    return new_text, (n > 0)


def ini_read_values(path):
    """Read the [connector] section with configparser (interpolation off)."""
    cp = configparser.ConfigParser(interpolation=None)
    values = {}
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return values
    if cp.has_section("connector"):
        for k in CONNECTOR_KEYS:
            if cp.has_option("connector", k):
                values[k] = cp.get("connector", k)
    return values


def ini_upsert(text, key, value):
    """Targeted rewrite of a `key = value` line, preserving comments/layout.

    Only matches a line that STARTS with the key (comment lines begin with ';' and
    are skipped). If the key is absent it is inserted right after [connector].
    """
    pat = re.compile(r'(?im)^([ \t]*' + re.escape(key) + r'[ \t]*=)[ \t]*.*$')

    def _repl(m):
        return m.group(1) + ((" " + value) if value != "" else "")

    new_text, n = pat.subn(_repl, text, count=1)
    if n > 0:
        return new_text, True

    # Not present: insert after the [connector] header.
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().lower() == "[connector]":
            lines.insert(i + 1, "%s = %s\n" % (key, value))
            return "".join(lines), True
    # No section at all: append one.
    sep = "" if text.endswith("\n") or text == "" else "\n"
    return text + "%s[connector]\n%s = %s\n" % (sep, key, value), True


# --------------------------------------------------------------------------- #
#  SERVER_ROOT auto-detection (best-effort, bounded)
# --------------------------------------------------------------------------- #
#
# Two distinct scans, both cheap enough to be safe:
#   1. A broad "where might ARK be installed at all" scan (Steam libraries, then a
#      depth-limited walk of common drive roots) - only used to seed SERVER_ROOT when
#      nothing is known yet. Runs on a background thread; can take a few seconds.
#   2. A scoped "given SERVER_ROOT, what else lives under/next to it" scan, at one of
#      three user-chosen intensities (see SCAN_LEVELS / scoped_scan_paths). Quick only
#      touches fixed subpaths plus one single-level listing, so it stays synchronous on
#      the UI thread; Thorough/Exhaustive add a bounded recursive walk and therefore
#      always run on a background thread (see _scoped_scan).

ARK_EXE_RELPATH = os.path.join("ShooterGame", "Binaries", "Win64", "ShooterGameServer.exe")

# Deliberately scoped to common Windows drive letters, not A-Z - keeps the fallback
# drive walk from wasting time probing letters that are virtually never real disks.
COMMON_DRIVE_LETTERS = "CDEFGH"

# Skipped during the drive walk: huge/system trees that are never an ARK server install
# and would otherwise dominate the directory budget below.
SKIP_SCAN_DIR_NAMES = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "appdata", "node_modules",
    "recovery", "perflogs", "config.msi", "windows.old", "$windows.~bt",
    "$windows.~ws", "msocache", "intel", "amd", "nvidia", ".git",
    # A steamapps tree is a separately Steam-managed install, never the SERVER_ROOT
    # this launcher itself installs into (see is_steamapps_path) - skipped so the
    # broad drive walk can never wander into, let alone surface, one.
    "steamapps",
}

# Hard caps so the fallback drive walk (run on a background thread) can never run away
# on a huge/slow disk - it gives up and reports "not found" rather than hanging.
MAX_SCAN_DIRS = 15000
SCAN_TIME_BUDGET_SECONDS = 20.0

# How many levels below a drive root we'll look for the exe.
MAX_SCAN_DEPTH = 3


def direct_candidate_server_roots():
    """Fixed, fast-to-check guesses: the handful of folder layouts people commonly
    install a manual ARK dedicated server into.

    Deliberately excludes any Steam Library location (steamapps\\common\\...): that's
    a separately Steam-managed install the user set up themselves at some other time,
    never the copy this launcher installs via SteamCMD's -force_install_dir - see
    is_steamapps_path. Adopting one here would silently point the launcher at the
    wrong install."""
    cands = []
    for letter in COMMON_DRIVE_LETTERS:
        drive = "%s:\\" % letter
        if not os.path.isdir(drive):
            continue
        cands.extend([
            os.path.join(drive, "ARK", "Server"),
            os.path.join(drive, "ArkServer"),
            os.path.join(drive, "Games", "ARK Survival Evolved Dedicated Server"),
        ])
    return cands


# "<name>_backup_<timestamp>" - the snapshot folder _backup_and_clear_dir() creates
# ("Full reset for new seed" / switch_map.bat). \d{6,} matches its yyyymmdd-hhmmss
# stamp and the "-2" collision suffix it may carry.
_BACKUP_SNAPSHOT_RE = re.compile(r"_backup_\d{6,}")


def is_backup_snapshot_path(path):
    """True if `path` IS, or lives anywhere inside, a timestamped backup snapshot.

    A snapshot is frozen data that has already been moved aside, so nothing in one is
    ever a live location for ANY field - offering it would point the app at old data
    instead of the real folder. The check is on the whole path, not just the leaf:
    the snapshot root is named "<name>_backup_<ts>", but the folders inside it keep
    their ordinary live-looking names ("ServerCluster_backup_20260721-124852\\Saves"),
    so a basename-only test (classify_cluster_folder) happily classifies them."""
    return any(_BACKUP_SNAPSHOT_RE.search(part.lower())
               for part in re.split(r"[\\/]", path or "") if part)


# Tallest the Folder suggestions list gets before it starts scrolling. Roughly a
# dozen candidate rows - enough that the ordinary 2-3 hit scan never scrolls, low
# enough that an Exhaustive sweep's dozens of hits can't push the popup off-screen.
SUGGEST_POPUP_MAX_LIST_H = 420


def suggestion_sections(keys, suggestions, current_getter):
    """Which of `keys` are worth showing in the Folder suggestions popup, and with
    what current value, given `suggestions` (key -> [candidate path, ...]) and
    `current_getter(key)` -> the field's current value ("" if unset).

    A field is included whenever it has candidates AND at least one differs from the
    current value - i.e. always when the field is empty, and also when re-scanning
    turns up something other than what's already set. A field is dropped when every
    candidate found for it is exactly the value it already has, since there's nothing
    to compare there.

    Returns [(key, current, cur_norm, matches), ...] in `keys` order, where cur_norm is
    normcase(normpath(current)) (or None if current is empty) - the same normalized
    form callers need to spot which candidate (if any) equals the current value."""
    sections = []
    for key in keys:
        matches = suggestions.get(key)
        if not matches:
            continue
        current = current_getter(key)
        cur_norm = os.path.normcase(os.path.normpath(current)) if current else None
        if cur_norm and all(os.path.normcase(os.path.normpath(m)) == cur_norm
                             for m in matches):
            continue
        sections.append((key, current, cur_norm, matches))
    return sections


def is_steamapps_path(path):
    """True if `path` lives anywhere inside a "steamapps" folder.

    That's a separately Steam-managed install (a Steam Library copy) the user set up
    themselves at some other time - never the copy this launcher installs and manages
    itself via SteamCMD's -force_install_dir (which lays the game straight into
    SERVER_ROOT, no steamapps\\common nesting). Never offered as SERVER_ROOT or any
    other path candidate."""
    return any(part.lower() == "steamapps"
               for part in re.split(r"[\\/]", path or "") if part)


def is_ark_server_root(path):
    """True if `path` is the folder that directly contains an ARK dedicated server
    install (i.e. ShooterGame\\Binaries\\Win64\\ShooterGameServer.exe below it)."""
    return os.path.isfile(os.path.join(path, ARK_EXE_RELPATH))


def bounded_drive_scan(log_fn, is_cancelled, matches=is_ark_server_root,
                       skip_names=SKIP_SCAN_DIR_NAMES, max_depth=MAX_SCAN_DEPTH):
    """Depth-limited, filtered walk of common drive roots returning the first folder
    `matches` accepts (by default, an ARK server root). Bounded by MAX_SCAN_DIRS and
    SCAN_TIME_BUDGET_SECONDS so a slow/huge disk degrades to "gave up" instead of
    hanging. Meant for a background thread - this alone is why it's safe to point at
    whole drive roots at all.

    `matches`/`skip_names`/`max_depth` are parameters rather than hardcoded so the
    Archipelago scan can reuse this exact walker: it looks for a different marker
    (is_archipelago_dir) and must NOT inherit SKIP_SCAN_DIR_NAMES, which skips
    "programdata" and "program files" - the two places Archipelago is most likely to
    actually be installed."""
    start = time.monotonic()
    visited = 0
    for letter in COMMON_DRIVE_LETTERS:
        drive = "%s:\\" % letter
        if not os.path.isdir(drive):
            continue
        stack = [(drive, 0)]
        while stack:
            if is_cancelled():
                return None
            if time.monotonic() - start > SCAN_TIME_BUDGET_SECONDS:
                log_fn("Scan time budget reached - stopping the drive scan.")
                return None
            path, depth = stack.pop()
            visited += 1
            if visited > MAX_SCAN_DIRS:
                log_fn("Scan directory limit reached - stopping the drive scan.")
                return None
            if matches(path):
                return path
            if depth >= max_depth:
                continue
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if entry.name.lower() in skip_names:
                            continue
                        if _BACKUP_SNAPSHOT_RE.search(entry.name.lower()):
                            continue  # a backed-up server tree has its own ShooterGameServer.exe
                        stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return None


# Same idea as SKIP_SCAN_DIR_NAMES, minus the entries that would defeat the search:
# "programdata" and "program files"/"program files (x86)" are exactly where
# Archipelago installs, and "appdata" holds the per-user variant. Everything left is
# still skipped - huge system/vendor trees that never contain an Archipelago install.
SKIP_ARCHIPELAGO_SCAN_DIR_NAMES = SKIP_SCAN_DIR_NAMES - {
    "programdata", "program files", "program files (x86)", "appdata",
}


# --------------------------------------------------------------------------- #
#  Scoped "Scan for paths" - tiered intensity (pure functions, no Tk)
# --------------------------------------------------------------------------- #

# Chosen on the Configuration tab before running "Scan for paths". Ordered
# cheapest-first; the combobox shows them in this order.
SCAN_QUICK = "Quick"
SCAN_THOROUGH = "Thorough"
SCAN_EXHAUSTIVE = "Exhaustive"
SCAN_LEVELS = (SCAN_QUICK, SCAN_THOROUGH, SCAN_EXHAUSTIVE)
SCAN_LEVEL_DEFAULT = SCAN_QUICK

# Per level: (max depth below each start point, max directories visited, seconds).
# Quick does no walking at all, so it has no entry. Both budgets are hard caps -
# hitting either stops the walk and reports what was found so far rather than
# hanging, exactly like bounded_drive_scan above.
SCAN_LEVEL_LIMITS = {
    SCAN_THOROUGH:   (4, 8000, 15.0),
    SCAN_EXHAUSTIVE: (8, 80000, 120.0),
}

SCAN_LEVEL_HELP = {
    SCAN_QUICK: "Checks only the exact expected sub-paths of SERVER_ROOT. "
                "Instant, and correct for a standard install.",
    SCAN_THOROUGH: "Everything Quick does, plus a few levels of recursive search "
                   "under SERVER_ROOT, under ShooterGame\\Saved, and in "
                   "SERVER_ROOT's parent/sibling folders. Takes a few seconds. "
                   "Use this when Quick misses your Plugins or cluster folders.",
    SCAN_EXHAUSTIVE: "A much deeper recursive search of the same places, PLUS your "
                     "Desktop, Documents and Downloads folders (in case a Plugins "
                     "or cluster folder ended up there instead of next to "
                     "SERVER_ROOT) - SLOW (can take a minute or more on a big/slow "
                     "disk). Only worth it when Thorough still can't find your "
                     "folders.",
}

# Folder names that are never one of the folders we're looking for, but can hold
# enormous numbers of files - skipped so they don't eat the directory budget. Also
# doubles as the "clearly a large unrelated install" skip-list for the Desktop/
# Documents/Downloads sweep below (steamapps/steamcmd already cover Steam game
# installs; the extra launcher names cover the same pattern for other stores). None
# of these ever collide with an actual target pattern (ArkApi/Plugins/Cluster*/
# Saves/Backups), so there's nothing to special-case for folders that "look like"
# both.
SKIP_SCOPED_SCAN_DIR_NAMES = {
    "content", "binaries", "engine", "logs", "cache", "crashreports",
    "steamapps", "steamcmd", "steam", "steamlibrary", "epic games",
    "gog galaxy", "battle.net", "origin games", "riot games", "xboxgames",
    "backup_temp", ".git", "node_modules",
}

# Desktop/Documents/Downloads sweep (Exhaustive only) - people sometimes extract an
# ArkApi\Plugins zip or leave cluster backups in their user folders rather than next
# to SERVER_ROOT. Capped much shallower than SCAN_LEVEL_LIMITS[SCAN_EXHAUSTIVE]'s own
# depth: these are unrelated, unbounded personal folders, not the server's own tree,
# so a handful of levels is enough to catch a nested folder without turning into a
# scan of the user's entire Documents library.
USER_SWEEP_FOLDER_NAMES = ("Desktop", "Documents", "Downloads")
USER_SWEEP_MAX_DEPTH = 5


def user_sweep_start_dirs():
    """Existing, accessible Desktop/Documents/Downloads under the current user's
    profile. A folder that doesn't exist or can't be listed (permissions) is left
    out silently - never an error, never surfaced to the user."""
    home = os.path.expanduser("~")
    starts = []
    for name in USER_SWEEP_FOLDER_NAMES:
        path = os.path.join(home, name)
        try:
            if os.path.isdir(path):
                starts.append(path)
        except OSError:
            continue
    return starts

# Cluster-style folders live in wildly different places between setups (beside
# SERVER_ROOT, under it, or nested under ShooterGame\Saved), so they're matched by
# name and always surfaced as suggestions to confirm - never filled in silently.
def classify_cluster_folder(name):
    """Which cluster GUI key a folder NAME looks like, or None.

    "ClusterSaves"/"ClusterBackups" contain "cluster" too, so saves/backups are
    tested first. Four kinds of name are excluded outright because they match by
    accident and would be actively misleading to offer:
      * "Saved" - ShooterGame's own folder, the container the world save lives IN.
        SAVESROOT is deliberately OUTSIDE it.
      * "SavedArks" (and "SavedArks*") - ARK's DEFAULT save folder, which is only
        where saves land when the server is launched WITHOUT AltSaveDirectoryName.
        start_ase_server.bat always passes AltSaveDirectoryName=Cluster-<Map>, so
        under this launcher the live per-map save data is in SAVESROOT
        (ServerCluster\\Saves\\<Map>) and SavedArks holds nothing current -
        configuring SAVESROOT to it would point every path at the wrong data.
        It's still real and worth backing up, which is why reset_ark_test.bat and
        "Full reset for new seed" handle it separately, but it is never a
        SAVESROOT candidate.
      * "Cluster-<Map>" - the per-map junction start_ase_server.bat creates inside
        ShooterGame\\Saved pointing AT SAVESROOT. It's a link the scripts manage,
        never a folder to configure (the tooltip says not to touch it by hand).
      * "<name>_backup_<timestamp>" - a timestamped snapshot left behind by
        "Full reset for new seed" / switch_map.bat, not a live folder."""
    low = name.lower()
    if low in ("saved", "config", "windowsserver"):
        return None
    if low.startswith("savedarks"):
        return None
    if low.startswith("cluster-"):
        return None
    if _BACKUP_SNAPSHOT_RE.search(low):
        return None
    if "backup" in low:
        return "BACKUPROOT"
    if "save" in low:
        return "SAVESROOT"
    if "cluster" in low:
        return "CLUSTERDIR"
    return None


def derive_plugins_dir(server_root):
    """The ArkApi Plugins folder implied by SERVER_ROOT.

    Purely positional - ArkApi always installs to
    <SERVER_ROOT>\\ShooterGame\\Binaries\\Win64\\ArkApi\\Plugins - so this is
    correct whether or not the folder exists yet. Returning it for a folder that
    doesn't exist yet is intentional and is what fixes the "PLUGINS_DIR stays empty
    even though SERVER_ROOT scanned fine" bug: the folder only appears once ArkApi
    is installed, but the field needs a value before that (Install Plugin creates
    the folder it points at)."""
    if not server_root or not server_root.strip():
        return ""
    return os.path.join(os.path.normpath(server_root.strip()),
                        "ShooterGame", "Binaries", "Win64", "ArkApi", "Plugins")


def _walk_bounded(starts, max_depth, max_dirs, budget_seconds, is_cancelled, on_dir):
    """Depth/count/time-bounded directory walk over several start points.

    Calls on_dir(path, name, depth) for every directory visited. Returns
    (visited, stopped_reason) where stopped_reason is None if it walked to
    completion, else a short string for the log."""
    start_time = time.monotonic()
    visited = 0
    # path -> how many levels we were still allowed to descend when we visited it.
    # Keyed that way rather than as a plain "already seen" set because start points
    # overlap: ShooterGame\Saved is reached both as its own start point (with a full
    # depth allowance) and part-way through the walk from SERVER_ROOT (with almost
    # none left). A plain set would let the shallow visit block the deep one, and
    # anything nested under Saved - exactly what Thorough exists to find - would be
    # silently skipped.
    seen = {}
    for start, depth_limit in starts:
        if not start or not os.path.isdir(start):
            continue
        stack = [(os.path.normpath(start), 0)]
        while stack:
            if is_cancelled():
                return visited, "cancelled"
            if time.monotonic() - start_time > budget_seconds:
                return visited, "time budget reached"
            if visited > max_dirs:
                return visited, "directory limit reached"
            path, depth = stack.pop()
            remaining = min(depth_limit, max_depth) - depth
            key = os.path.normcase(path)
            if seen.get(key, -1) >= remaining:
                continue  # already walked from here, at least this deep
            seen[key] = remaining
            visited += 1
            if remaining <= 0:
                continue
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        low = entry.name.lower()
                        if low in SKIP_SCAN_DIR_NAMES or low in SKIP_SCOPED_SCAN_DIR_NAMES:
                            continue
                        if _BACKUP_SNAPSHOT_RE.search(low):
                            # Never descend into a snapshot: everything inside it looks
                            # live by name (Saves, Backups, an ArkApi Plugins tree, a
                            # Game.ini) and would be offered for the matching field.
                            # Skipping the root also keeps the walk budget for real folders.
                            continue
                        on_dir(entry.path, entry.name, depth + 1)
                        stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return visited, None


def scoped_scan_paths(server_root, level, is_cancelled=None, progress=None):
    """Find everything derivable from SERVER_ROOT, at the requested intensity.

    Returns a dict:
      {"PLUGINS_DIR": str, "ipc_dir": str, "game_ini": str,
       "plugins_exists": bool, "suggestions": {gui_key: [path, ...]},
       "notes": [log line, ...], "stopped": str|None, "visited": int}

    Values are absolute paths ("" when not determined). "suggestions" holds
    name-matched cluster-ish folders the caller should offer rather than apply.
    Pure filesystem work only - safe to call from a worker thread."""
    is_cancelled = is_cancelled or (lambda: False)
    res = {"PLUGINS_DIR": "", "ipc_dir": "", "game_ini": "", "plugins_exists": False,
           "suggestions": {}, "notes": [], "stopped": None, "visited": 0}
    if not server_root or not server_root.strip():
        return res
    root = os.path.normpath(server_root.strip())
    win64 = os.path.join(root, "ShooterGame", "Binaries", "Win64")
    saved = os.path.join(root, "ShooterGame", "Saved")

    # --- expected-path pass (every level runs this) ------------------------- #
    plugins = derive_plugins_dir(root)
    res["PLUGINS_DIR"] = plugins
    res["plugins_exists"] = os.path.isdir(plugins)
    arkap = os.path.join(plugins, "ArkAP")
    if os.path.isfile(os.path.join(arkap, "ArkAP.dll")):
        res["ipc_dir"] = os.path.join(arkap, "ipc")

    game_ini = os.path.join(saved, "Config", "WindowsServer", "Game.ini")
    if os.path.isfile(game_ini):
        res["game_ini"] = game_ini

    # Single-level listing of SERVER_ROOT and its parent - the historical Quick
    # behaviour for cluster folders.
    suggestions = {}

    def _note_suggestion(key, path):
        # Catches what the _walk_bounded prune can't: SERVER_ROOT itself sitting inside
        # a snapshot (the walk starts in there, so it never sees the snapshot root), and
        # the Quick tier, which lists folders directly instead of walking.
        if is_backup_snapshot_path(path) or is_steamapps_path(path):
            return
        bucket = suggestions.setdefault(key, [])
        if not any(os.path.normcase(p) == os.path.normcase(path) for p in bucket):
            bucket.append(path)

    for folder in (root, os.path.dirname(root)):
        if not folder or not os.path.isdir(folder):
            continue
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    key = classify_cluster_folder(entry.name)
                    if key:
                        _note_suggestion(key, entry.path)
        except OSError:
            pass

    if level == SCAN_QUICK:
        res["suggestions"] = suggestions
        return res

    # --- recursive pass (Thorough / Exhaustive) ----------------------------- #
    max_depth, max_dirs, budget = SCAN_LEVEL_LIMITS.get(
        level, SCAN_LEVEL_LIMITS[SCAN_THOROUGH])
    parent = os.path.dirname(root)
    # (start point, depth limit for that start point). ShooterGame\Saved gets the
    # deepest allowance because that's where nested cluster data hides; the parent
    # gets a shallow one so a scan of C:\ doesn't turn into a whole-drive walk.
    starts = [(root, max_depth), (saved, max_depth), (win64, 3)]
    if parent and os.path.normcase(parent) != os.path.normcase(root):
        starts.append((parent, 2 if level == SCAN_THOROUGH else 3))
    if level == SCAN_EXHAUSTIVE:
        for user_dir in user_sweep_start_dirs():
            starts.append((user_dir, USER_SWEEP_MAX_DEPTH))

    found_plugins = []
    found_arkap = []
    found_game_ini = []

    def _on_dir(path, name, depth):
        low = name.lower()
        if progress is not None and depth <= 2:
            progress(path)
        if low == "plugins" and os.path.basename(os.path.dirname(path)).lower() == "arkapi":
            found_plugins.append(path)
        elif low == "arkap" and os.path.isfile(os.path.join(path, "ArkAP.dll")):
            found_arkap.append(path)
        elif low == "windowsserver":
            candidate = os.path.join(path, "Game.ini")
            if os.path.isfile(candidate):
                found_game_ini.append(candidate)
        key = classify_cluster_folder(name)
        if key:
            _note_suggestion(key, path)

    visited, stopped = _walk_bounded(starts, max_depth, max_dirs, budget,
                                      is_cancelled, _on_dir)
    res["visited"] = visited
    res["stopped"] = stopped

    # A real Plugins folder on disk always beats the derived-but-missing one -
    # this is what rescues a nested/relocated install where the expected path
    # under SERVER_ROOT doesn't exist.
    if not res["plugins_exists"] and found_plugins:
        best = sorted(found_plugins, key=len)[0]
        res["PLUGINS_DIR"] = best
        res["plugins_exists"] = True
        res["notes"].append("Found an ArkApi Plugins folder outside the expected "
                            "location: %s" % best)
    if not res["ipc_dir"] and found_arkap:
        best = sorted(found_arkap, key=len)[0]
        res["ipc_dir"] = os.path.join(best, "ipc")
        res["notes"].append("Found an installed ArkAP plugin at %s." % best)
    if not res["game_ini"] and found_game_ini:
        best = sorted(found_game_ini, key=len)[0]
        res["game_ini"] = best
        res["notes"].append("Found Game.ini outside the expected location: %s" % best)

    res["suggestions"] = suggestions
    return res


# --------------------------------------------------------------------------- #
#  Setup Status checks (pure functions - no Tk, easy to test in isolation)
# --------------------------------------------------------------------------- #

def check_ark_server_installed(server_root):
    """(ok, detail) - ok if ShooterGameServer.exe exists under server_root."""
    if not server_root:
        return False, "SERVER_ROOT is not set."
    exe = os.path.join(server_root, ARK_EXE_RELPATH)
    return os.path.isfile(exe), exe


def check_mod_installed(server_root, mod_id):
    """(ok, detail) - ok if both Content\\Mods\\<mod_id>\\ and Content\\Mods\\<mod_id>.mod
    exist under server_root AND the .mod file is intact. Both are required for the server
    to recognize the mod - see MODS_CONTENT_RELDIR. A present-but-corrupt .mod is called
    out explicitly in `detail` rather than lumped in with "not installed", because it is
    the strictly worse state: it crashes the server instead of just skipping the mod."""
    if not server_root:
        return False, "SERVER_ROOT is not set."
    mods_dir = os.path.join(server_root, MODS_CONTENT_RELDIR)
    folder = os.path.join(mods_dir, mod_id)
    mod_file = os.path.join(mods_dir, "%s.mod" % mod_id)
    if os.path.isfile(mod_file):
        reason = check_dot_mod_file(mod_file, mod_id)
        if reason:
            return False, "%s is BROKEN - %s. Delete it and re-download the mod." % (
                mod_file, reason)
    return (os.path.isdir(folder) and os.path.isfile(mod_file)), folder


def is_mod_installed(server_root, mod_id):
    """bool convenience wrapper over check_mod_installed - reads REAL disk state
    (folder + sibling <id>.mod both present), never any cached/GUI value."""
    return check_mod_installed(server_root, str(mod_id))[0]


# --------------------------------------------------------------------------- #
#  Steam Workshop mod install backend (Mods tab)
# --------------------------------------------------------------------------- #
# ARK's Steam Workshop items don't drop in ready to use. SteamCMD leaves each cooked
# asset as a `.z` chunk-compressed blob, and the `<id>.mod` metadata file the server
# needs is NOT in the download at all - it has to be generated from mod.info /
# modmeta.info. Both the `.z` format and the `.mod` binary layout are Wildcard's own,
# undocumented and community-reverse-engineered years ago. The two routines below are
# ported to match the long-standing, battle-tested tools (barrycarey/Ark_Mod_Downloader,
# project-umbrella/arkit.py, TheCherry/ark-server-manager - all tracing back to the
# original "Ark Server Launcher" by Face Wound) rather than hand-rolled, precisely
# because a subtly-wrong byte yields a valid-LOOKING .mod the server silently won't load.

# 8-byte little-endian header signature at the start of every ARK `.z` file. Confirmed
# identical across arkit.py (reads it as 2653586369) and TheCherry (0x9E2A70C1, the same
# 4 low bytes read signed).
_Z_FILE_SIGNATURE = 2653586369

# Magic constant written into every .mod file right after the map-name list. Its meaning
# is unknown (the reference calls it "Not sure of the reason for this") but it is a fixed
# value in every working .mod, so it's reproduced verbatim.
_MOD_FILE_MAGIC = 4280483635

ModInstallResult = collections.namedtuple(
    "ModInstallResult", ["ok", "mod_id", "reason", "message"])
# reason is None on success, else a short category for the GUI to branch on:
#   "no_server" | "invalid_id" | "steamcmd_missing" | "network" |
#   "download_failed" | "partial_download" | "extract" | "install"

# How many times to (re)run the SteamCMD workshop download before giving up. Anonymous
# workshop_download_item is genuinely flaky - it routinely fails a first attempt with a
# generic "failed (Failure)" and succeeds on a retry (the ARK server install help says the
# same about app_update). ponytail: fixed small retry, no backoff - if this proves too
# few in the wild, raise the count rather than adding exponential-backoff machinery.
_MOD_DOWNLOAD_ATTEMPTS = 3


def _ue4_write_string(f, text):
    """Serialize an UE4 FString: int32 length (INCLUDING the trailing NUL), the UTF-8
    bytes, then one NUL byte. Mirrors the reference read_ue4_string's read(count)[:-1]."""
    data = text.encode("utf-8")
    f.write(struct.pack("<i", len(data) + 1))
    f.write(data)
    f.write(b"\x00")


def _ue4_read_string(f):
    """Inverse of _ue4_write_string. A negative length means an UTF-16 string, which
    these two files never use - treated as empty, matching the reference parsers."""
    (count,) = struct.unpack("<i", f.read(4))
    if count <= 0:
        return ""
    return f.read(count)[:-1].decode("utf-8", "replace")


def _decompress_z_file(src, dst):
    """Expand one ARK `.z` chunk-compressed file to dst. Raises ValueError on a bad
    signature or a chunk that doesn't inflate to its declared size (a corrupt/partial
    download). Header is 4x int64: signature, max-chunk-size, packed-total,
    unpacked-total; then (packed, unpacked) size pairs per chunk until the unpacked
    sizes add up; then the compressed chunks themselves."""
    with open(src, "rb") as f:
        header = f.read(32)
        if len(header) < 32:
            raise ValueError("truncated .z header in %s" % src)
        sig, _chunk_max, _packed_total, unpacked_total = struct.unpack("<qqqq", header)
        if sig != _Z_FILE_SIGNATURE:
            raise ValueError("bad .z signature in %s" % src)
        chunks = []
        acc = 0
        while acc < unpacked_total:
            packed, unpacked = struct.unpack("<qq", f.read(16))
            chunks.append((packed, unpacked))
            acc += unpacked
        with open(dst, "wb") as out:
            for packed, unpacked in chunks:
                data = zlib.decompress(f.read(packed))
                if len(data) != unpacked:
                    raise ValueError("chunk size mismatch in %s" % src)
                out.write(data)


def _extract_z_files(content_dir):
    """Decompress every `.z` under content_dir in place, then delete the `.z` and its
    `.uncompressed_size` sidecar (the server wants the plain assets). Returns the count
    extracted. Raises ValueError (from _decompress_z_file) on the first bad file."""
    extracted = 0
    for curdir, _subdirs, files in os.walk(content_dir):
        for name in files:
            root, ext = os.path.splitext(name)
            if ext != ".z":
                continue
            src = os.path.join(curdir, name)
            _decompress_z_file(src, os.path.join(curdir, root))
            os.remove(src)
            sidecar = os.path.join(curdir, name + ".uncompressed_size")
            if os.path.isfile(sidecar):
                os.remove(sidecar)
            extracted += 1
    return extracted


def _parse_mod_info(path):
    """mod.info -> ordered list of map/asset name strings that go into the .mod file."""
    with open(path, "rb") as f:
        _ue4_read_string(f)  # the mod's own name - not needed here
        (count,) = struct.unpack("<i", f.read(4))
        names = []
        for _ in range(count):
            name = _ue4_read_string(f)
            if name:
                names.append(name)
        return names


def _parse_modmeta_info(path):
    """modmeta.info -> {key: value}. int32 pair-count, then length-prefixed key then
    length-prefixed value per pair (e.g. {'ModType': '1'} for a total-conversion)."""
    meta = {}
    with open(path, "rb") as f:
        (total,) = struct.unpack("<i", f.read(4))
        for _ in range(total):
            key = _ue4_read_string(f)
            val = _ue4_read_string(f)
            if key and val:
                meta[key] = val
    return meta


def _build_dot_mod_bytes(mod_id, map_names, meta_data):
    """Serialize the `<id>.mod` metadata file the dedicated server reads to recognize an
    installed mod. Byte layout ported verbatim from the reference create_mod_file."""
    from io import BytesIO
    buf = BytesIO()
    # modId as one little-endian UINT64. The reference packs int32 + 4 zero pad bytes
    # ('ixxxx'), which is byte-identical to '<Q' for every id below 2^31 - verified
    # against the known-good .mod files on disk (1404697612 -> 0c fc b9 53 00 00 00 00,
    # i.e. the pad bytes ARE the high dword of a 64-bit little-endian id, which is what
    # FSteamWorkshop::LoadInstalledMods reads). Signed int32 also raises struct.error on
    # any Workshop id above 2147483647 (e.g. 2863707757), and Workshop ids are 64-bit,
    # so '<Q' both fixes that and keeps the bytes identical for existing installs.
    buf.write(struct.pack("<Q", int(mod_id)))
    _ue4_write_string(buf, "ModName")
    _ue4_write_string(buf, "")
    buf.write(struct.pack("<i", len(map_names)))
    for name in map_names:
        _ue4_write_string(buf, name)
    buf.write(struct.pack("<I", _MOD_FILE_MAGIC))
    buf.write(struct.pack("<i", 2))
    # ModType flag byte. The reference's struct.pack('p', b'1'/'b0') collapses to a single
    # 0x00 for EVERY mod (verified - a width-1 'p' field has no room for content), and that
    # is what the whole ecosystem ships and the server accepts. Every mod in the supported
    # set is a content mod (no 'ModType' in meta) where 0x00 is definitionally correct.
    # ponytail: hard-coded 0x00 to match proven output; a map/total-conversion mod may want
    # 0x01 here - untested, revisit only if one is ever actually added.
    buf.write(b"\x00")
    buf.write(struct.pack("<i", len(meta_data)))
    for key, val in meta_data.items():
        _ue4_write_string(buf, key)
        _ue4_write_string(buf, val)
    return buf.getvalue()


def check_dot_mod_file(path, mod_id):
    """None if <id>.mod looks like a real, complete .mod file; else a short reason string.

    A 0-byte or truncated .mod is far worse than a missing one: the server doesn't skip
    it, it hard-crashes reading it ("Invalid BufferCount=0 ... Pos=0, Size=0"). Only the
    prefix every known-good .mod shares is verified - 64-bit id, the two name FStrings,
    the map-name list, the magic - not the whole tail, and the name field's *contents*
    are deliberately not checked: this launcher writes the literal "ModName" (as every
    reference implementation does), but ARK's own stock files carry the real name there
    (111111111.mod = "Primitive Plus Official"), and both load fine.
    ponytail: prefix-only validation; deepen it only if a real half-written tail shows up.
    """
    try:
        if os.path.getsize(path) == 0:
            return "the file is 0 bytes (empty)"
        with open(path, "rb") as f:
            (found_id,) = struct.unpack("<Q", f.read(8))
            if found_id != int(mod_id):
                return "it holds mod id %d, not %s" % (found_id, mod_id)
            _ue4_read_string(f)   # mod name ("ModName" from this launcher)
            _ue4_read_string(f)   # path, always empty in practice
            (count,) = struct.unpack("<i", f.read(4))
            if not 0 <= count <= 10000:
                return "the map-name count (%d) is impossible" % count
            for _ in range(count):
                _ue4_read_string(f)
            if struct.unpack("<I", f.read(4))[0] != _MOD_FILE_MAGIC:
                return "the magic value after the map list is wrong"
    except (OSError, ValueError, struct.error) as exc:
        return "it is truncated or malformed (%s)" % exc
    return None


def find_broken_mod_files(server_root):
    """[(path, reason)] for every numeric <id>.mod under Content\\Mods that fails
    check_dot_mod_file. This state is otherwise invisible - the launcher sees a .mod
    file and calls the mod installed, and the first symptom is the server crashing."""
    mods_dir = os.path.join(server_root or "", MODS_CONTENT_RELDIR)
    if not os.path.isdir(mods_dir):
        return []
    broken = []
    try:
        names = sorted(os.listdir(mods_dir))
    except OSError:
        return []
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext.lower() != ".mod" or not stem.isdigit():
            continue
        path = os.path.join(mods_dir, name)
        if not os.path.isfile(path):
            continue
        reason = check_dot_mod_file(path, stem)
        if reason:
            broken.append((path, reason))
    return broken


def remove_broken_mod_files(server_root):
    """Delete every corrupt <id>.mod found by find_broken_mod_files. Returns
    [(path, reason, removed_ok)]. Deleting is the safe end state: the mod then reads as
    plainly "not installed" and the Mods tab can re-download it, whereas leaving the file
    there crashes the server on its next start."""
    results = []
    for path, reason in find_broken_mod_files(server_root):
        try:
            os.remove(path)
            results.append((path, reason, True))
        except OSError as exc:
            results.append((path, "%s (and it could not be removed: %s)" % (reason, exc),
                            False))
    return results


def _workshop_download_dir(steamcmd_exe, mod_id):
    """Where SteamCMD actually drops a workshop item. It ignores +force_install_dir for
    workshop content and always writes under its own install folder, so this is derived
    from the steamcmd.exe location, not SERVER_ROOT."""
    return os.path.join(os.path.dirname(steamcmd_exe), "steamapps", "workshop",
                        "content", ARK_WORKSHOP_APPID, str(mod_id))


def _classify_steamcmd_failure(output_text):
    """Best-effort reason category from SteamCMD's console output when a download didn't
    land. SteamCMD prints the same generic 'failed (Failure)' for a bad ID, a transient
    content-server hiccup, and rate limiting, so a wrong ID can't be told apart from a
    retryable blip with confidence - hence the default is the softer "download_failed"
    (retry-first) rather than accusing the user's ID. ponytail: string-scan heuristic,
    upgrade only if SteamCMD ever grows real distinguishable exit codes."""
    low = output_text.lower()
    if any(s in low for s in ("timeout", "no connection", "could not connect",
                              "no subscription", "rate limit", "connection error")):
        return "network"
    return "download_failed"


def _run_steamcmd_workshop_download(steamcmd_exe, mod_id, log):
    """Run `+workshop_download_item 346110 <id>` and stream each console line to log().
    Returns (returncode, full_output_text). Uses the same _ConsoleLineSplitter the server
    installer uses so SteamCMD's \\r-rewritten progress line shows up live."""
    cmd = [steamcmd_exe, "+login", "anonymous",
           "+workshop_download_item", ARK_WORKSHOP_APPID, str(mod_id), "+quit"]
    log("Running: %s" % " ".join(cmd))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd, cwd=os.path.dirname(steamcmd_exe), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, bufsize=0, creationflags=creationflags)
    splitter = _ConsoleLineSplitter()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    captured = []
    while True:
        chunk = proc.stdout.read(1024)
        if not chunk:
            break
        for line in splitter.feed(decoder.decode(chunk)):
            captured.append(line)
            log(line)
    rest = splitter.flush()
    if rest:
        captured.append(rest)
        log(rest)
    proc.stdout.close()
    return proc.wait(), "\n".join(captured)


def download_and_install_mod(mod_id, server_root, steamcmd_exe, log=None):
    """Download one ARK Workshop mod via SteamCMD and install it so the dedicated server
    recognizes it: decompress the `.z` assets, copy them into
    SERVER_ROOT\\ShooterGame\\Content\\Mods\\<id>\\, and generate the sibling <id>.mod.

    Returns a ModInstallResult(ok, mod_id, reason, message). Never raises for an expected
    failure (bad ID, offline, partial/corrupt download) - those come back as ok=False with
    a plain-language message and a reason category. `log` is an optional callable(str) fed
    every SteamCMD console line and each install step (the Mods tab passes _mods_log_line).
    """
    mod_id = str(mod_id).strip()
    log = log or (lambda _line: None)

    if not server_root or not os.path.isdir(os.path.join(server_root, "ShooterGame")):
        return ModInstallResult(False, mod_id, "no_server",
                                "SERVER_ROOT isn't set to an installed ARK server "
                                "(no ShooterGame folder). Install the server first.")
    if not mod_id.isdigit():
        return ModInstallResult(False, mod_id, "invalid_id",
                                "\"%s\" isn't a valid Workshop ID - it should be the "
                                "number from the mod's Steam Workshop URL." % mod_id)
    if not steamcmd_exe or not os.path.isfile(steamcmd_exe):
        return ModInstallResult(False, mod_id, "steamcmd_missing",
                                "SteamCMD wasn't found. Install the ARK server first - "
                                "that's what sets SteamCMD up.")

    # SteamCMD workshop downloads are flaky - retry a few times before giving up (see
    # _MOD_DOWNLOAD_ATTEMPTS). Success is judged by disk state (mod.info present), not the
    # exit code, which SteamCMD returns as 0 even on "failed (Failure)".
    content_dir = None
    output = ""
    for attempt in range(1, _MOD_DOWNLOAD_ATTEMPTS + 1):
        if attempt > 1:
            log("Download didn't complete - retrying (attempt %d of %d)..."
                % (attempt, _MOD_DOWNLOAD_ATTEMPTS))
        try:
            _rc, output = _run_steamcmd_workshop_download(steamcmd_exe, mod_id, log)
        except OSError as exc:
            return ModInstallResult(False, mod_id, "steamcmd_missing",
                                    "Couldn't launch SteamCMD: %s" % exc)
        download_dir = _workshop_download_dir(steamcmd_exe, mod_id)
        candidate = os.path.join(download_dir, "WindowsNoEditor")
        if not os.path.isdir(candidate):
            # Some items unpack straight into <id>\ with no WindowsNoEditor layer.
            candidate = download_dir
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "mod.info")):
            content_dir = candidate
            break
    if content_dir is None:
        reason = _classify_steamcmd_failure(output)
        if reason == "network":
            msg = ("SteamCMD couldn't reach Steam to download %s. Check your internet "
                   "connection and try again." % mod_id)
        else:
            msg = ("SteamCMD couldn't finish downloading Workshop ID %s after %d tries. "
                   "This is often a temporary Steam issue - try again in a moment. If it "
                   "keeps failing, double-check the ID is correct and is an ARK: Survival "
                   "Evolved mod." % (mod_id, _MOD_DOWNLOAD_ATTEMPTS))
        return ModInstallResult(False, mod_id, reason, msg)

    # Copy the RAW (still .z-compressed) download into the destination FIRST, then
    # decompress there. Extracting inside SteamCMD's own workshop folder would delete its
    # .z files and corrupt its record of the item, after which SteamCMD aborts EVERY later
    # workshop download while re-validating installed items ("Missing game files"). Doing
    # the extraction in our own dest leaves SteamCMD's copy pristine so the next mod's
    # download still works.
    mods_dir = os.path.join(server_root, MODS_CONTENT_RELDIR)
    dest_folder = os.path.join(mods_dir, mod_id)
    dot_mod_path = os.path.join(mods_dir, "%s.mod" % mod_id)
    try:
        os.makedirs(mods_dir, exist_ok=True)
        if os.path.isdir(dest_folder):
            shutil.rmtree(dest_folder)
        log("Copying into %s" % dest_folder)
        shutil.copytree(content_dir, dest_folder)
    except OSError as exc:
        return ModInstallResult(False, mod_id, "install",
                                "Downloaded %s but couldn't copy it into the server's "
                                "Content\\Mods folder: %s" % (mod_id, exc))

    log("Decompressing mod files...")
    try:
        extracted = _extract_z_files(dest_folder)
    except ValueError as exc:
        return ModInstallResult(False, mod_id, "extract",
                                "Couldn't decompress the downloaded files for %s - the "
                                "download looks corrupted. Try Verify/Redownload. (%s)"
                                % (mod_id, exc))
    log("Decompressed %d file(s)." % extracted)

    try:
        map_names = _parse_mod_info(os.path.join(dest_folder, "mod.info"))
        meta_path = os.path.join(dest_folder, "modmeta.info")
        meta_data = _parse_modmeta_info(meta_path) if os.path.isfile(meta_path) else {}
        # Serialize in full BEFORE creating the file: opening it first means any failure
        # while building leaves a 0-byte <id>.mod on disk, and that file alone crashes the
        # ARK server on its next start whether or not the launcher reported the error.
        payload = _build_dot_mod_bytes(mod_id, map_names, meta_data)
        with open(dot_mod_path, "wb") as f:
            f.write(payload)
        problem = check_dot_mod_file(dot_mod_path, mod_id)
        if problem:
            raise ValueError(problem)
    except (OSError, ValueError, struct.error) as exc:
        # Never leave a partial/failed .mod behind - see above.
        try:
            os.remove(dot_mod_path)
        except OSError:
            pass
        return ModInstallResult(False, mod_id, "partial_download",
                                "The download for %s finished but its .mod file couldn't "
                                "be written - it may be incomplete. Nothing was left "
                                "behind. Try Verify/Redownload. (%s)" % (mod_id, exc))

    if not is_mod_installed(server_root, mod_id):
        return ModInstallResult(False, mod_id, "install",
                                "Installed %s but the folder or .mod file is missing "
                                "afterwards - something went wrong copying it." % mod_id)
    log("Installed %s successfully." % mod_id)
    return ModInstallResult(True, mod_id, None, "Installed %s." % mod_id)


def uninstall_mod(server_root, mod_id):
    """Delete a mod's installed files: Content\\Mods\\<id>\\ and the sibling <id>.mod.
    Returns (ok, message). Idempotent - a mod that isn't there is a no-op success. Only
    ever removes this launcher's own downloaded mod folders (named by numeric Workshop
    id), never anything else under Content\\Mods (e.g. the stock DLC map folders)."""
    mod_id = str(mod_id)
    mods_dir = os.path.join(server_root, MODS_CONTENT_RELDIR)
    folder = os.path.join(mods_dir, mod_id)
    dot_mod = os.path.join(mods_dir, "%s.mod" % mod_id)
    removed = False
    try:
        if os.path.isdir(folder):
            shutil.rmtree(folder)
            removed = True
        if os.path.isfile(dot_mod):
            os.remove(dot_mod)
            removed = True
    except OSError as exc:
        return False, "Couldn't remove %s: %s" % (mod_id, exc)
    return True, ("Uninstalled %s." % mod_id if removed
                  else "%s was not installed - nothing to remove." % mod_id)


def check_arkapi_installed(server_root):
    """(ok, detail) - ok if both version.dll and an ArkApi\\ folder exist in Win64."""
    if not server_root:
        return False, "SERVER_ROOT is not set."
    win64 = os.path.join(server_root, "ShooterGame", "Binaries", "Win64")
    version_dll = os.path.join(win64, "version.dll")
    arkapi_dir = os.path.join(win64, "ArkApi")
    return (os.path.isfile(version_dll) and os.path.isdir(arkapi_dir)), win64


def check_plugin_installed(plugin_dir):
    """(ok, detail) - ok if plugin_dir\\ArkAP.dll exists. plugin_dir may be None/empty
    (SERVER_ROOT/PLUGINS_DIR not resolvable yet)."""
    if not plugin_dir:
        return False, "Plugin folder unknown - set SERVER_ROOT first."
    return os.path.isfile(os.path.join(plugin_dir, "ArkAP.dll")), plugin_dir


def check_plugin_mode(plugin_dir):
    """(ok, detail) - ok if ArkAP.config.json's "mode" field == "ap". detail is either
    the mode string found or a short explanation of why it couldn't be determined."""
    if not plugin_dir:
        return False, "plugin not installed"
    cfg_path = os.path.join(plugin_dir, "ArkAP.config.json")
    if not os.path.isfile(cfg_path):
        return False, "ArkAP.config.json not found"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return False, "could not read ArkAP.config.json (%s)" % exc
    mode = str(data.get("mode", "")).strip().lower()
    return (mode == "ap"), ("mode = \"%s\"" % mode if mode else "mode field missing")


def check_scripts_sourced(scripts_dir):
    """(ok, detail) - ok if every paths.cmd caller in scripts_dir actually calls it.

    The one check on this tab that looks at the scripts rather than the server. It
    exists because the failure it catches is invisible from everywhere else: each
    individual thing the user can see (the path field, paths.cmd, Save's log) is
    correct, and only the relationship between two files is broken.
    """
    if not scripts_dir or not os.path.isdir(scripts_dir):
        return False, "Scripts folder not found: %s" % (scripts_dir or "(unknown)")
    if not os.path.isfile(os.path.join(scripts_dir, "paths.cmd")):
        return False, "paths.cmd is missing from %s" % scripts_dir
    stale, unreadable = [], []
    for batname in PATHS_CMD_CALLERS:
        path = os.path.join(scripts_dir, batname)
        if not os.path.isfile(path):
            continue
        try:
            text, _ = read_text(path)
        except OSError:
            unreadable.append(batname)
            continue
        if is_pre_paths_script(text):
            stale.append(batname)
    if stale:
        plural = len(stale) > 1
        return False, ("%s still %s %s own SERVER_ROOT instead of reading paths.cmd"
                       % (", ".join(stale), "declare" if plural else "declares",
                          "their" if plural else "its"))
    if unreadable:
        return False, "could not read %s" % ", ".join(unreadable)
    return True, "all scripts read their paths from paths.cmd"


def check_connector_filled(connector_ini_path):
    """(ok, detail) - ok if connector.ini exists and server/slot/ipc_dir are all
    non-empty. Reads the FILE directly (not the GUI fields), since that's what the
    connector process actually uses at runtime."""
    if not connector_ini_path or not os.path.isfile(connector_ini_path):
        return False, "connector.ini not found"
    values = ini_read_values(connector_ini_path)
    missing = [k for k in ("server", "slot", "ipc_dir") if not values.get(k, "").strip()]
    if missing:
        return False, "missing: %s" % ", ".join(missing)
    return True, "server / slot / ipc_dir set"


def default_cluster_paths(server_root):
    """The cluster folder layout to create alongside a fresh install, as
    {gui_key: absolute_path}.

    SteamCMD only ever lays down the game payload under SERVER_ROOT - the cluster
    folders are not part of app 376030 at all (see the comment on
    ArkAPLauncher.create_cluster_folders), so there is nothing inside the install tree
    to discover them from and nothing to find by scanning harder. They have to be
    created, and this is the layout they're created in:

        <SERVER_ROOT>\\ServerCluster\\ClusterData   (CLUSTERDIR)
        <SERVER_ROOT>\\ServerCluster\\Saves         (SAVESROOT)
        <SERVER_ROOT>\\ServerCluster\\Backups       (BACKUPROOT)

    Inside SERVER_ROOT rather than beside it, deliberately: everything belonging to
    one server install lives under that install's own folder, so the whole thing can
    be moved or deleted as a single unit and nothing is left scattered a level up.
    ServerCluster sits next to the SteamCMD payload (ShooterGame\\, Engine\\, ...)
    rather than inside it, so a re-install/verify of app 376030 still leaves the
    world saves alone.

    Derived purely from the server_root passed in at runtime: there is no hardcoded
    drive letter or root folder name to fall back on. An empty/blank server_root
    yields {} - the caller must then leave the fields empty rather than guess.
    """
    if not server_root or not server_root.strip():
        return {}
    root = os.path.normpath(server_root.strip())
    cluster_root = os.path.join(root, CLUSTER_ROOT_DIRNAME)
    return {key: os.path.join(cluster_root, sub) for key, sub in CLUSTER_PATH_SUBDIRS}


def check_cluster_dirs(paths):
    """(ok, detail) - ok if every configured cluster path exists on disk.

    paths is {gui_key: configured_value}; empty values count as failures because
    start_ase_server.bat passes -ClusterDirOverride unconditionally whenever
    CLUSTERID is set, and ARK hangs on an empty/missing override instead of
    reporting an error."""
    missing = [key for key, _ in CLUSTER_PATH_SUBDIRS if not (paths.get(key) or "").strip()]
    absent = [key for key, _ in CLUSTER_PATH_SUBDIRS
              if (paths.get(key) or "").strip() and not os.path.isdir(paths[key].strip())]
    if missing and absent:
        return False, "not set: %s; missing on disk: %s" % (
            ", ".join(missing), ", ".join(absent))
    if missing:
        return False, "not set: %s" % ", ".join(missing)
    if absent:
        return False, "missing on disk: %s" % ", ".join(absent)
    return True, paths.get("CLUSTERDIR", "")


def check_active_mods_match(server_root, mods):
    """(ok, detail) - ok if GameUserSettings.ini's ActiveMods is exactly the ticked mods,
    in the same order, and every one of them is installed on disk.

    The Mods tab holds INTENT (which rows are ticked); ActiveMods is what the server
    actually loads. They only agree after a Save, and nothing else in the app ever says
    they've drifted - the tab keeps showing the ticks, the server keeps loading the old
    set. The consequence is silent and downstream: a yaml whose mod_ids name a mod the
    server never loaded expects engrams that don't exist in the world.

    Order is compared too, not just membership - ActiveMods is left-to-right load
    priority (see ACTIVE_MODS_SECTION), so a reordered list is a different setup.

    A mismatch names only the mods that actually differ, and says which way each one
    goes, because the fix differs: ticked-but-not-active was never saved, while
    active-but-not-ticked is loading on the server without the launcher showing it.
    Two full ID lists left the user to diff numbers by eye. Names come from the passed-in
    Mods tab rows, so a "Rename mod" name shows up here too; a bare ID means the tab has
    no row for it (an ActiveMods entry the launcher doesn't know about)."""
    if not server_root:
        return False, "SERVER_ROOT is not set."
    names = {str(m.get("id")): (m.get("name") or "").strip() for m in mods}
    checked = [str(m.get("id")) for m in mods if m.get("enabled")]
    active = read_active_mods(server_root)

    def _labels(ids):
        out = []
        for mod_id in ids:
            name = names.get(mod_id, "")
            out.append("%s (%s)" % (name, mod_id) if name and name != mod_id else mod_id)
        return ", ".join(out)

    problems = []
    unsaved = [i for i in checked if i not in active]
    untracked = [i for i in active if i not in checked]
    if unsaved:
        problems.append("ticked but missing from ActiveMods, so never saved: %s"
                        % _labels(unsaved))
    if untracked:
        problems.append("in ActiveMods but not ticked, so the server loads it without the "
                        "Mods tab showing it: %s" % _labels(untracked))
    if not unsaved and not untracked and active != checked:
        # Same mods on both sides - nothing to name, only the load order differs.
        problems.append("same mods, different load order: ActiveMods is %s, the Mods tab "
                        "has %s" % (_labels(active), _labels(checked)))
    not_installed = [i for i in checked if not is_mod_installed(server_root, i)]
    if not_installed:
        problems.append("ticked but not installed on disk: %s" % _labels(not_installed))
    if problems:
        return False, "; ".join(problems)
    return True, ("ActiveMods=%s" % _labels(active)) if active else "No mods active."


# --------------------------------------------------------------------------- #
#  Example / placeholder text styling
# --------------------------------------------------------------------------- #

# Every place the app SHOWS an example value (rather than a real, configured one)
# renders it greyed out, in the same colour an empty Entry's placeholder uses
# (theme["entry_placeholder_fg"]), so an example can never be misread as saved data.
# There are four such places, all covered:
#   1. Empty path Entry fields          -> _show_placeholder()      (pre-existing)
#   2. Tooltip "Example: ..." lines     -> Tooltip._show()
#   3. Instructions-tab sample paths    -> _tag_instruction_examples()
#   4. Not-yet-created suggested paths  -> "Placeholder.TButton" in the suggestion dialogs
#
# Deliberately NOT dimmed, because they are real data the app will actually use:
#   * DEFAULT_VALUES prefills (MAP=TheIsland, GAMEPORT=7777, ...) - these ARE written
#     to the .bat/.ini files on Save, so they must look exactly like typed-in values.
#   * Discovered/auto-detected/suggestion-accepted paths - real paths on this machine.
#   * Template paths in prose that describe layout rather than offer a value
#     (e.g. "<SERVER_ROOT>\ShooterGame\..."), which contain no concrete example value.

def is_example_line(line):
    """True for a tooltip line whose entire content is an example value, i.e. it
    starts with "Example:" / "Examples:". Matched on the explicit prefix only -
    a line that merely mentions a path mid-sentence still carries real
    instructions and stays fully legible."""
    return line.strip().lower().startswith(("example:", "examples:"))


# Concrete sample paths written into the Instructions tab prose. Matched literally
# (not by a "looks like a path" regex) so only these known-fake paths are ever
# dimmed and a real path can never be caught by accident.
INSTRUCTION_EXAMPLE_SNIPPETS = [
    PLACEHOLDER_EXAMPLE_ROOT + r"\ARK Survival Evolved Dedicated Server\ShooterGame",
    PLACEHOLDER_EXAMPLE_ROOT,
]


# --------------------------------------------------------------------------- #
#  Hover tooltip
# --------------------------------------------------------------------------- #

class Tooltip:
    """Simple delayed hover tooltip attached to a single widget.

    Any line of the text that is a pure example (see is_example_line) is rendered
    in the same greyed-out colour empty Entry placeholders use, so an example path
    can never be mistaken for a value that's actually configured. self.text always
    keeps the full original string - the in-app search reads that directly."""

    def __init__(self, widget, text, wraplength=420, delay_ms=450):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.delay_ms = delay_ms
        self._after_id = None
        self._win = None
        # Stashed on the widget itself (rather than kept in some central
        # registry) so the search engine can find "does this widget have
        # tooltip text matching the query" without every call site needing
        # to register it separately.
        widget._tooltip = self
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._win is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 6
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._win = tk.Toplevel(self.widget)
        self._win.wm_overrideredirect(True)
        self._win.wm_geometry("+%d+%d" % (x, y))
        try:
            self._win.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        # One bordered frame holding one Label per line, rather than a single
        # multi-line Label: a tk.Label has exactly one foreground colour, so
        # per-line dimming of "Example: ..." lines needs separate widgets.
        body = tk.Frame(self._win, background=CURRENT_THEME["tooltip_bg"],
                        relief="solid", borderwidth=1)
        body.pack()
        tk.Frame(body, background=CURRENT_THEME["tooltip_bg"], height=4).pack(fill="x")
        for line in self.text.split("\n"):
            example = is_example_line(line)
            tk.Label(body, text=line, justify="left",
                     background=CURRENT_THEME["tooltip_bg"],
                     foreground=(CURRENT_THEME["entry_placeholder_fg"] if example
                                 else CURRENT_THEME["tooltip_fg"]),
                     wraplength=self.wraplength,
                     font=("Segoe UI", 9, "italic") if example else ("Segoe UI", 9),
                     padx=6, pady=0).pack(anchor="w", fill="x")
        # Matches the padding the old single Label had, without adding a gap
        # between every line above.
        tk.Frame(body, background=CURRENT_THEME["tooltip_bg"], height=4).pack(fill="x")

    def _hide(self, _event=None):
        self._cancel()
        if self._win is not None:
            self._win.destroy()
            self._win = None


# --------------------------------------------------------------------------- #
#  The application
# --------------------------------------------------------------------------- #

class ArkAPLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ARKIpelago Launcher")
        self.minsize(680, 640)

        self.config_path = os.path.join(base_dir(), CONFIG_FILENAME)
        # No JSON snapshot yet == genuinely first run - used to decide whether to
        # auto-kick the broad SERVER_ROOT scan below without being asked.
        self._is_first_launch = not os.path.isfile(self.config_path)
        self.vars = {}            # key -> tk.StringVar
        self._entries = {}        # key -> the primary Entry/Checkbutton widget for it
        self._placeholder_text = {}    # key -> example text shown when the field is empty
        self._placeholder_active = {}  # key -> True while that example text is displayed
        # key -> every Entry showing that field. SERVER_ROOT has two (Configuration and
        # Install Server/Api/Plugin), and both must clear/recolour together or the one that isn't
        # registered silently swallows what the user types into it.
        self._placeholder_entries = {}
        # Fields whose loaded value was only the shipped example (see _set_from_file).
        self._ignored_example_values = set()
        # Directory-field key -> (scan button, progress bar, status label), filled in as
        # each field's row is built. One shared scan drives both fields (see
        # DIR_SCAN_TARGETS / _start_dir_scan).
        self._dir_scan_widgets = {}
        # "Open PopTracker" explains the manual AP-connect step once per session rather
        # than on every click - see the end of _open_poptracker.
        self._poptracker_room_hint_shown = False
        self._last_scoped_scan_root = None
        self._last_cluster_dir_scan = None
        # Scripts folder is no longer a user field - it's the working folder next to the
        # launcher that bundled scripts are extracted into (set in _discover_locations).
        self._scripts_dir = working_scripts_dir()
        self.backup_var = tk.BooleanVar(value=True)
        self._logo_img = None     # keep a reference so Tk doesn't garbage-collect it

        # Header-logo easter egg (LOGO_EGG_LINES). All in-memory: the count only ever
        # moves on a logo click, so clicking elsewhere / switching tabs can't reset it,
        # and a restart wipes it (the sequence is replayable once per app session).
        self._logo_clicks = 0
        self._logo_bubble = None
        self._logo_egg_done = False   # True after the finale - clicks go inert
        self._credits_img = None      # PhotoImage ref for the credits window
        self._egg_music_on = False

        # False until the initial config load has finished, so filling CLUSTERDIR from
        # the saved JSON at startup doesn't pop the Folder suggestions dialog in the
        # user's face before the window is even usable. See set().
        self._cluster_autoscan = False

        # Profiles tab state - named snapshots of every Configuration field + notes,
        # persisted separately from CONFIG_FILENAME (see PROFILES_FILENAME).
        self.profiles_path = os.path.join(base_dir(), PROFILES_FILENAME)
        self._profiles = {}             # name -> {"values": {key: str, ...}, "notes": str}
        self._loaded_profile_name = None
        # Which profile Save writes into, remembered across launches (ACTIVE_PROFILE_KEY).
        # Tracked separately from _loaded_profile_name because loading the reserved
        # autosave slot must NOT retarget Save at it - see _set_active_profile.
        self._active_profile = None
        self._loaded_profile_values = None  # snapshot of Configuration values as loaded
        self._loaded_profile_notes = None   # notes text as loaded
        # Baseline for the Save-button highlights: the field values as last written to
        # disk by on_save, seeded once startup has finished loading. None means "not
        # loaded yet", which keeps every halo dark while _build_ui/_load_json are still
        # firing var traces. See _mark_saved_baseline / _update_save_highlights.
        self._saved_values = None
        # Latest per-section verdicts, kept so the header's "make sure to save!" hint can
        # answer for all three Save buttons without recomputing the Mods one (which hits
        # the disk) on every keystroke. See _update_save_hint.
        self._fields_dirty = False
        self._mods_dirty_flag = False
        self._save_hint_shown = False
        # Reserved autosave slot (see AUTOSAVE_PROFILE_NAME). _autosave_last_values is
        # what was last written, so an idle app rewrites nothing.
        self._autosave_after_id = None
        self._autosave_last_values = None

        # In-app search / highlight state.
        self.search_var = tk.StringVar()
        self._search_matches = []       # ordered list of match dicts, see _run_search()
        self._current_match_index = -1  # index into _search_matches of the "current" match
        self._last_search_query = ""
        self._search_text_widgets = set()  # tk.Text widgets with an active search_hl tag
        self._header_font_family = None
        self._header_font_warning = None

        # SteamCMD install state
        self._install_queue = queue.Queue()
        self._install_proc = None
        self._install_thread = None
        self._install_cancelled = False

        # ArkServerApi (ArkApi) install state - shares the install_log/install_progress/
        # install_status_var widgets with the SteamCMD flow above (see _any_install_running).
        self._arkapi_queue = queue.Queue()
        self._arkapi_thread = None
        # ArkAP plugin download+install - shares the same install_log/install_progress/
        # install_status_var widgets (see _any_install_running).
        self._plugin_queue = queue.Queue()
        self._plugin_thread = None
        self._hide_install_reminder = self._read_hide_reminder_flag()

        # Mods tab state - ordered list of {"id","name","enabled","supported"} dicts;
        # order IS load priority (see MODS_KEY). Loaded once here, every mutation
        # re-persists immediately via _save_mods_config.
        self._mods = self._load_mods_config()
        self._mods_selected_id = None   # workshop id the Verify/Open-Workshop buttons act on
        self._mods_action_buttons = []  # populated by _build_mods_tab; disabled when gated
        # Mod download/install worker (own log widget, but shares SteamCMD + SERVER_ROOT
        # with the other installers, so it joins _any_install_running for mutual exclusion).
        self._mods_queue = queue.Queue()
        self._mods_thread = None

        # Broad SERVER_ROOT auto-detect state (Steam libraries / common drive roots).
        self._detect_queue = queue.Queue()
        self._detect_thread = None
        self._detect_cancelled = False
        # Scan intensity requested via the merged "Scan for paths" button, carried
        # through an auto-detect run to the scoped scan that follows it - see
        # _on_scan_button / _on_auto_detect_done.
        self._pending_scan_level = None

        # Scoped "Scan for paths" state. Only Thorough/Exhaustive use the thread and
        # queue - Quick still runs inline, since it's a handful of stats.
        self._scan_queue = queue.Queue()
        self._scan_thread = None
        self._scan_cancelled = False

        # Launcher self-update state - see "Launcher self-update" section below.
        # A silent background check also runs once on startup (see
        # _start_component_version_check) - it only sets the badge, never opens the
        # update dialog or downloads anything on its own.
        self._update_check_thread = None
        self._update_download_thread = None
        self._update_download_queue = queue.Queue()
        self._update_progress_win = None
        # Component-version advisory rows (ArkApi / plugin / .apworld newer-than-installed),
        # filled in by the background check and appended to Setup Status.
        # See #4 / _on_component_versions.
        self._component_advisories = []
        # {component: status} from the same background check - the input to the "!" badge
        # and the button highlight, and what the update dialog renders. Empty until the
        # first check lands (and stays empty for anything unreachable/unrecorded), so both
        # cues start dark. See _collect_update_statuses / _apply_update_indicators.
        self._update_status = {}
        # PhotoImages for the Setup Status tab-bar symbol, kept referenced so Tk doesn't
        # GC them; rebuilt per state/theme in _update_status_tab_indicator.
        self._status_tab_glyphs = {}

        # Theme must be selected before _build_ui() constructs any widget,
        # since widget colors are read from self.theme at construction time.
        self._apply_theme(self._read_theme_pref())

        # Session marker, so a log spanning weeks reads as "which run was this?".
        launcher_log("===== ARKipelago Launcher %s started =====" % APP_VERSION)

        self._build_ui()
        self._load_window_icon()
        if self._header_font_warning:
            self._log(self._header_font_warning)

        # Load order: JSON snapshot -> discover locations -> override from real files ->
        # fill remaining blanks with safe defaults -> show placeholders in what's still blank.
        saved = self._load_json()
        if not saved:
            # A shipped-blank config.json (present on disk but empty - see build.py)
            # must still count as first launch, same as no file at all.
            self._is_first_launch = True
        self._discover_locations(saved)
        self.load_from_files(initial=True, saved=saved)
        self._apply_path_placeholders()
        # Before the first Setup Status paint, so the row below reflects the cleaned-up
        # state. SERVER_ROOT is known by now (load_from_files).
        self._sweep_broken_mod_files()
        self._refresh_setup_status()
        self._refresh_debug_log()
        self._profiles = self._load_profiles()
        # Before the first _refresh_profile_list, so the Profiles tab comes up with
        # the pre-created profile already selected on a fresh install.
        self._ensure_default_profile()
        self._refresh_profile_list()
        # Come back up on whatever profile was active last session (ACTIVE_PROFILE_KEY)
        # rather than always on DEFAULT_PROFILE_NAME. No-op on a fresh install, where
        # _ensure_default_profile just made and activated that one.
        self._restore_active_profile(saved)
        self._update_profile_status()
        # Loading is finished, so what the fields show IS what's persisted - including a
        # profile just restored above. That initial fill is not an edit, so the baseline
        # is taken here and every Save button comes up dark until the user changes
        # something. See _mark_saved_baseline.
        self._mark_saved_baseline()

        # Armed last, so the first snapshot it writes is of fully-loaded values.
        self._start_autosave()
        # Everything above filled CLUSTERDIR from disk; from here on any change to it
        # is a user action worth offering sibling Saves/Backups folders for.
        self._cluster_autoscan = True
        # Stop the easter-egg music on a normal window close. Belt-and-braces - MCI
        # playback dies with the process anyway - but the window can outlive a stop
        # request by a while during shutdown.
        self.protocol("WM_DELETE_WINDOW", self._on_app_close)

        if self._is_first_launch and not self.get("SERVER_ROOT"):
            self._start_auto_detect()

        # Brand-new install: open on Instructions rather than Configuration, so the first
        # thing a user sees is the step-by-step order rather than a wall of empty paths.
        # Same first-run signal that auto-creates DEFAULT_PROFILE_NAME, plus a stored flag
        # so this greeting happens exactly once (see FIRST_RUN_DONE_KEY).
        if not (saved or {}).get(FIRST_RUN_DONE_KEY):
            if self._is_first_launch:
                self.notebook.select(self.tab_instructions)
            self._write_config_key(FIRST_RUN_DONE_KEY, True, "first-run flag")

        # Local file read only (no network) - reports the outcome of an update helper
        # that ran just before this process started, if there was one.
        self._check_previous_update_result()
        # Best-effort housekeeping: remove any update-helper leftovers (staging, backups,
        # the helper script/zip) and stale --onefile _MEI temp folders from before the
        # switch to --onedir. Never fatal.
        self._sweep_update_leftovers()

        # Silent, non-blocking: runs on a background thread so it can never delay the
        # window appearing, and fails quietly (no popup) if GitHub is unreachable. One pass
        # covers the launcher, the ArkAP plugin, the .apworld and the ArkApi advisory - it
        # feeds both the "!" badge / button highlight and the Setup Status advisory rows.
        self._start_component_version_check()

    def report_callback_exception(self, exc_type, exc_value, exc_tb):
        """Tk calls this when a callback (button command, event binding, after-timer)
        raises. Default prints a raw traceback to a console the windowed build doesn't
        have, and the error vanishes. Instead: log it to the crash file and show a
        friendly dialog, but keep the app alive - one bad handler shouldn't kill a
        session mid-configuration."""
        path = write_crash_log(exc_type, exc_value, exc_tb)
        where = path or crash_log_path()
        try:
            messagebox.showerror(
                "ARKIpelago Launcher - Something went wrong",
                "The launcher hit an unexpected error, but is still running - you can "
                "keep going or restart to be safe.\n\n"
                "A crash report was saved to:\n%s\n\n"
                "Please attach that file (or use \"Export diagnostics\" on the "
                "Configuration tab) when reporting this." % where)
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- UI ---- #
    def _build_ui(self):
        self._setup_search_styles()

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")

        # Header row: title/subtitle on the left, logo top-right. Kept in its
        # own row (rather than spanning the whole `top` frame) so the logo
        # stays aligned with the header text even though the search bar below
        # adds extra height to `top`.
        header_row = ttk.Frame(top)
        header_row.pack(fill="x")
        self.header_row = header_row

        self.logo_label = ttk.Label(header_row)
        self.logo_label.pack(side="right", padx=(12, 20))
        self._load_header_logo(max_height=72)
        # Deliberately no cursor change, relief, or hover style - it has to keep looking
        # like a plain decorative image. See _on_logo_click / LOGO_EGG_LINES.
        self.logo_label.bind("<Button-1>", self._on_logo_click, add="+")

        self.theme_toggle_btn = ttk.Button(header_row, text=self._theme_toggle_label(),
                                            command=self._toggle_theme)
        self.theme_toggle_btn.pack(side="right", padx=(0, 8))
        Tooltip(self.theme_toggle_btn, "Switch between light and dark mode.")

        # "Check for Updates" sits in the same warn_bg/warn_border halo the Save button
        # uses (see save_btn_halo), but this one only lights up when a release newer than
        # the user's last-acknowledged version is detected; clicking acknowledges it and
        # the halo goes back to blending into the header. The frame is always present at
        # the same size (thickness 1, just recoloured) so toggling never shifts the header.
        self.update_btn_halo = tk.Frame(header_row, background=self.theme["bg"],
                                        highlightbackground=self.theme["bg"],
                                        highlightthickness=1)
        self.update_btn_halo.pack(side="right", padx=(0, 8))
        self._update_highlight_on = False
        self.update_check_btn = ttk.Button(self.update_btn_halo, text="Check for Updates",
                                            command=self._on_check_for_updates)
        self.update_check_btn.pack(padx=2, pady=2)

        # Badge shown to the LEFT of the button when a silent background check (see
        # _start_component_version_check) finds a newer release. Packed after the
        # button so it sits on the button's left. Kept always-packed with empty
        # text rather than pack/pack_forget, so showing it doesn't shift the other
        # header buttons. Its own warm-gold colour (update_badge_fg) reads as a
        # secondary nudge, not an error - brighter/more saturated than status_info
        # but not the loud red of status_fail.
        self.update_badge_label = ttk.Label(header_row, text="", foreground=self.theme["update_badge_fg"],
                                             font=(self._header_font_family or "Segoe UI", 12, "bold"))
        self.update_badge_label.pack(side="right", padx=(0, 4))
        Tooltip(self.update_badge_label,
                "A newer version of the launcher, the ArkAP plugin or the .apworld is "
                "available - click Check for Updates.")
        Tooltip(self.update_check_btn,
                "Check GitHub for newer releases of the launcher (current version: %s), "
                "the ArkAP plugin and %s. Also checked silently once on launch; clicking "
                "always does a fresh check." % (APP_VERSION, APWORLD_ASSET_NAME))

        header_font_family = self._register_header_font()
        title_row = ttk.Frame(header_row)
        title_row.pack(side="left", fill="x", expand=True)
        ttk.Label(title_row, text="ARKIpelago Launcher",
                  font=(header_font_family, 16, "bold")).pack(side="left")
        # Highlighted rather than dimmed: it's the one thing in the header the user
        # has to act on, and as plain subtle-grey text beside a 16pt title it read as
        # decoration. Uses the theme's warn colours (same pale yellow as the install
        # reminder banner) via a style, so the toggle repaints it automatically.
        # Deliberately NOT packed here - it's a nag, and a permanent nag is wallpaper.
        # _update_save_hint packs it in only while some section really is unsaved.
        self.save_hint_label = ttk.Label(title_row, text="make sure to save!",
                                          style="SaveHint.TLabel", padding=(6, 2))

        # Search bar - left-aligned directly below the title (not centered
        # under the logo). Enter runs the search and jumps to the first match;
        # Find Prev/Next step through the rest, centering each in view.
        search_bar = ttk.Frame(top, padding=(0, 6, 0, 0))
        search_bar.pack(fill="x", anchor="w")
        ttk.Label(search_bar, text="Search:").pack(side="left", padx=(0, 4))
        self.search_entry = ttk.Entry(search_bar, textvariable=self.search_var, width=32)
        self.search_entry.pack(side="left")
        self.search_entry.bind("<Return>", lambda _e: self._run_search(self.search_var.get().strip()))
        # Both buttons live in one frame so the pair can be hidden as a unit
        # while the search box is empty - with nothing searched for they do
        # nothing, and an empty search bar is the app's resting state.
        self._find_btns = ttk.Frame(search_bar)
        self.find_prev_btn = ttk.Button(self._find_btns, text="Find Prev", width=9,
                                         command=self._find_prev)
        self.find_prev_btn.pack(side="left", padx=(8, 2))
        self.find_next_btn = ttk.Button(self._find_btns, text="Find Next", width=9,
                                         command=self._find_next)
        self.find_next_btn.pack(side="left", padx=(2, 8))
        self.search_status_var = tk.StringVar(value="")
        self._search_status_label = ttk.Label(search_bar, textvariable=self.search_status_var,
                                              foreground=self.theme["subtle_fg"], width=14)
        self._search_status_label.pack(side="left", padx=(4, 0))
        self.search_var.trace_add("write", lambda *_a: self._update_find_btns())
        self._update_find_btns()
        Tooltip(self.search_entry,
                "Press Enter to highlight every occurrence of this word "
                "anywhere in the app - labels, fields, buttons, tab names, "
                "tooltips, and the Instructions/log text - and jump to it, "
                "switching tabs automatically if the match is elsewhere.")
        Tooltip(self.find_prev_btn, "Jump to the previous match")
        Tooltip(self.find_next_btn, "Jump to the next match")

        # Tabs -------------------------------------------------------------------
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        tab_config = ttk.Frame(notebook)
        tab_archipelago = ttk.Frame(notebook)
        tab_profiles = ttk.Frame(notebook)
        tab_install = ttk.Frame(notebook)
        tab_mods = ttk.Frame(notebook)
        tab_status = ttk.Frame(notebook)
        tab_debug = ttk.Frame(notebook)
        tab_instructions = ttk.Frame(notebook)
        # Tab ORDER only - nothing reads a tab by position. Every reference in this app
        # is to the tab's frame object (self.tab_status, self.tab_instructions, ...),
        # and the search feature walks notebook.tabs() and stores the frame it found a
        # match in, so reordering here is purely visual and safe.
        notebook.add(tab_config, text="Configuration")
        notebook.add(tab_install, text="Install Server/Api/Plugin")
        notebook.add(tab_archipelago, text="Archipelago Setup")
        notebook.add(tab_mods, text="Mods")
        notebook.add(tab_status, text="Setup Status")
        notebook.add(tab_profiles, text="Profiles")
        notebook.add(tab_debug, text="Debug Log")
        notebook.add(tab_instructions, text="Instructions")
        self.notebook = notebook
        self.tab_archipelago = tab_archipelago
        self.tab_profiles = tab_profiles
        self.tab_install = tab_install
        self.tab_mods = tab_mods
        self.tab_status = tab_status
        self.tab_debug = tab_debug
        self.tab_instructions = tab_instructions
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed, add="+")

        # Scrollable field area -------------------------------------------------
        mid = ttk.Frame(tab_config)
        mid.pack(fill="both", expand=True)
        canvas = tk.Canvas(mid, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(mid, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas, padding=(10, 4))
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_configure(_e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _on_configure)

        def _on_canvas_resize(e):
            canvas.itemconfigure(inner_id, width=e.width)
        canvas.bind("<Configure>", _on_canvas_resize)

        # Claim the global wheel binding only while the pointer is over this canvas, so the
        # Setup Status tab's own scrollable canvas (same pattern) doesn't fight over it.
        def _on_wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))

        # Install reminder banner - dismissible, points at the Install Server/Api/Plugin tab.
        self.reminder_banner = tk.Frame(inner, background=self.theme["warn_bg"],
                                         highlightbackground=self.theme["warn_border"],
                                         highlightthickness=1)
        if not self._hide_install_reminder:
            self.reminder_banner.pack(fill="x", pady=(0, 8))
        tk.Label(self.reminder_banner, background=self.theme["warn_bg"],
                 foreground=self.theme["warn_fg"],
                 justify="left", wraplength=520,
                 text="Install the ARK dedicated server first. Use the \"Install Server/Api/Plugin\" "
                      "tab (SteamCMD) to install it before relying on the paths below. Go to the instructions tab for a step by step guide"
                 ).pack(side="left", fill="x", expand=True, padx=8, pady=6)
        rbtns = tk.Frame(self.reminder_banner, background=self.theme["warn_bg"])
        rbtns.pack(side="right", padx=6, pady=4)
        ttk.Button(rbtns, text="Go to Install Server/Api/Plugin",
                   command=self._goto_install_tab).pack(fill="x", pady=(0, 2))
        rbtns2 = tk.Frame(rbtns, background=self.theme["warn_bg"])
        rbtns2.pack(fill="x")
        ttk.Button(rbtns2, text="Close", width=8,
                   command=self._dismiss_reminder).pack(side="left", padx=(0, 2))
        ttk.Button(rbtns2, text="Don't show again",
                   command=self._dismiss_reminder_forever).pack(side="left")

        self._render_field_groups(inner, GROUPS)
        self._build_config_upload_section(inner)

        # Bottom action bar (fixed) --------------------------------------------
        bottom = ttk.Frame(tab_config, padding=(10, 8))
        bottom.pack(fill="x")

        q = ttk.LabelFrame(bottom, text="Quick launch", padding=(8, 6))
        q.pack(fill="x")
        # Ordered by how often they actually get used, not by category: the four on the
        # top row are the everyday ones (launch, look at the install, start a new seed,
        # edit Game.ini). The rest keep their old grouping - folders, then the remaining
        # run/patch actions - on the rows below. `None` starts a new row.
        #
        # The reset buttons live here rather than in their own group, but they still
        # replace the old "Delete session.json" button, which only cleared the AP->game
        # direction (session.json) and left the outgoing checks (checks_out.jsonl etc.)
        # behind - so a fresh room got flooded with the previous seed's checks on the
        # connector's first read. Both clear ALL generated plugin/connector tracking;
        # "Full reset" also backs up + wipes the world save (an in-app equivalent of
        # reset_ark_test.bat that doesn't rely on .bat paths).
        quick_launch = [
            ("Run start_ase_server", lambda: self.run_bat("start_ase_server.bat"),
             "Launches the main ARK server via start_ase_server.bat."),
            ("Open SERVER_ROOT", self.open_server_root, None),
            ("Full reset for new seed", self.full_reset_new_seed,
             "Complete reset before joining a new Archipelago seed: clears all "
             "plugin/connector tracking AND backs up + wipes the world save. The ARK "
             "server must be stopped first."),
            ("Open Game.ini folder", self.open_gameini_folder, None),
            None,
            ("Open ipc folder", self.open_ipc, None),
            ("Open Plugins folder", self.open_plugins, None),
            ("Open ClusterDir folder", self.open_cluster_dir,
             "Opens the cluster data folder (ClusterDir) in Explorer."),
            None,
            ("Run switch_map", lambda: self.run_bat("switch_map.bat"), None),
            ("Patch Game.ini for randomized creatures",
             self.patch_game_ini_for_randomized_dinos,
             "Applies the plugin's ipc\\%s into your Game.ini (backed up first) so "
             "randomized creatures take effect - the automated version of copying that "
             "block in by hand. Stop the ARK server first." % GAME_INI_FRAGMENT_NAME),
            ("Reset AP data (keep world save)", self.reset_ap_data,
             "Clears all Archipelago tracking the plugin and connector generate. "
             "Note: if your character/world isn't also reset, level and inventory "
             "checks will immediately re-send. Use 'Full reset for new seed' instead "
             "when starting a new seed."),
        ]
        row = ttk.Frame(q)
        row.pack(fill="x")
        for entry in quick_launch:
            if entry is None:
                row = ttk.Frame(q)
                row.pack(fill="x")
                continue
            text, cmd, tip = entry
            btn = ttk.Button(row, text=text, command=cmd)
            btn.pack(side="left", padx=3, pady=2)
            if tip:
                Tooltip(btn, tip, wraplength=520)

        act = ttk.Frame(bottom)
        act.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(act, text="Back up each file (.bak) before writing",
                        variable=self.backup_var).pack(side="left")
        # Save sits in a thin warn_bg/warn_border frame - the same pale yellow as the
        # header's "make sure to save!" hint and the install reminder banner, so the
        # reminder and the button it points at read as one thing. A frame rather than a
        # styled button because ttk's "vista" engine ignores background on TButton.
        # Built in the bg colour (dark), not warn_bg: the halo now means "you have
        # unsaved changes on this tab" rather than being permanently lit, and
        # _update_save_highlights turns it on the moment a field diverges from disk.
        self.save_btn_halo = tk.Frame(act, background=self.theme["bg"],
                                      highlightbackground=self.theme["bg"],
                                      highlightthickness=1)
        self.save_btn_halo.pack(side="right", padx=3)
        ttk.Button(self.save_btn_halo, text="Save", command=self.on_save
                   ).pack(padx=2, pady=2)
        ttk.Button(act, text="Reload from files",
                   command=lambda: self.load_from_files()).pack(side="right", padx=3)
        export_btn = ttk.Button(act, text="Export diagnostics",
                                command=self.export_diagnostics)
        export_btn.pack(side="right", padx=3)
        Tooltip(export_btn,
                "Bundle ArkAP_debug.log, the launcher's own activity log, a Setup Status "
                "summary, a password-redacted copy of your config, your Mods tab state + "
                "output log, and the crash log (if any) into one zip on your Desktop - "
                "drag it into Discord or a GitHub issue when asking for help.")

        # Status / report log ---------------------------------------------------
        self.log = tk.Text(bottom, height=7, wrap="word", state="disabled",
                           font=("Consolas", 9),
                           background=self.theme["text_bg"], foreground=self.theme["text_fg"],
                           insertbackground=self.theme["text_fg"])
        self.log.pack(fill="x", pady=(8, 0))

        self._build_archipelago_tab(tab_archipelago)
        self._build_install_tab(tab_install)
        self._build_mods_tab(tab_mods)
        self._build_setup_status_tab(tab_status)
        self._build_debug_log_tab(tab_debug)
        self._build_profiles_tab(tab_profiles)
        self._build_instructions_tab(tab_instructions)
        self._tag_instruction_examples()   # both instruction bodies at once

        # Live "does this still match the loaded profile?" indicator plus the Save-button
        # highlights - wired up last so profile_status_var (built by _build_profiles_tab
        # above) and every Save halo already exist. One trace per var covers every way a
        # field can change: typing, Browse, a scan result, a loaded profile, a reset.
        for var in self.vars.values():
            var.trace_add("write", lambda *_a: self._on_field_changed())

    # ------------------------------------- Game.ini / GameUserSettings upload - #
    # =========================== Archipelago Setup tab ====================== #
    #
    # A quick launcher for the user's own Archipelago install. What each button can
    # actually do was verified against Archipelago 0.6.7's frozen executables rather
    # than assumed:
    #
    #   ArchipelagoTextClient.exe DOES take connection arguments - its CommonClient
    #   base parser exposes "--connect ADDR", "--name SLOT", "--password PW" (plus
    #   --nogui and a positional archipelago:// url). So "Open Text Client" launches it
    #   pre-connected straight from the server/slot/password fields on this tab. The
    #   explicit flags are used rather than the archipelago:// url form because a slot
    #   name or password containing ':', '@' or '/' would corrupt the url.
    #
    #   ArchipelagoOptionsCreator.exe does NOT. It has no argument parsing at all - no
    #   argparse, no sys.argv read - and ignores everything passed to it (even --help,
    #   which just boots the GUI); the game is chosen only by clicking a world button
    #   inside its Kivy UI. So "Open Options Creator" launches it plain and says so in
    #   its tooltip. Deliberately NOT worked around by sending synthetic keystrokes or
    #   driving its widgets: that would break on any Archipelago UI change.
    def _build_archipelago_tab(self, parent):
        # Scrollable container - same Canvas + Scrollbar pattern as the Configuration,
        # Install and Setup Status tabs, since this tab's content has grown past shorter
        # windows. `wrap` stays the parent of every row below, it's just now the
        # canvas's inner frame instead of packed straight into `parent`.
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0,
                           background=self.theme["bg"])
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        wrap = ttk.Frame(canvas, padding=(10, 8))
        inner_id = canvas.create_window((0, 0), window=wrap, anchor="nw")
        wrap.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))

        def _on_wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))

        ttk.Label(wrap, wraplength=760, justify="left", foreground=self.theme["subtle_fg"],
                  text="Shortcuts into your own Archipelago installation - the app that "
                       "hosts the room and builds the .yaml, installed separately from "
                       "this launcher. Point the field below at it (or scan for it) and "
                       "the buttons become available."
                  ).pack(fill="x", anchor="w", pady=(0, 6))

        # Same _render_field_groups() the Configuration tab uses, so the directory field
        # gets the identical label/Browse/greyed-placeholder treatment, and the moved
        # server/slot/password fields keep their tooltips and their "Copy ARK connection
        # command" / "Copy port" buttons (rendered by the `password` case in that loop).
        self._render_field_groups(wrap, ARCHIPELAGO_GROUPS)

        # The fields above are persisted by the same Save the Configuration tab uses
        # (config JSON + the active profile - see on_save/collect_values), but that
        # button lives on a tab this one never sends you to. Without a Save here, a user
        # who scanned for Archipelago and typed their room details closed the app and
        # lost all four every time. Same warn_bg/warn_border halo as the other two Save
        # buttons so "unapplied changes" reads identically wherever it appears.
        srow = ttk.Frame(wrap)
        srow.pack(fill="x", pady=(0, 8))
        self.archipelago_save_btn_halo = tk.Frame(
            srow, background=self.theme["bg"],
            highlightbackground=self.theme["bg"], highlightthickness=1)
        self.archipelago_save_btn_halo.pack(side="left")
        save_btn = ttk.Button(self.archipelago_save_btn_halo, text="Save",
                              command=self.on_save)
        save_btn.pack(padx=2, pady=2)
        Tooltip(save_btn,
                "Saves the fields above (and everything on the Configuration tab) to "
                "disk - the same Save button the Configuration tab has, so the "
                "Archipelago directory and your server / slot / password survive a "
                "restart instead of needing a re-scan and a re-type every launch.\n"
                "Also writes server / slot / password into connector.ini, which is what "
                "the connector actually reads.", wraplength=520)

        arch_entry = self._entries[ARCHIPELAGO_DIR_KEY]
        arch_entry.bind("<FocusOut>", lambda _e: self._refresh_archipelago_buttons(), add="+")
        self.vars[ARCHIPELAGO_DIR_KEY].trace_add(
            "write", lambda *_a: self._refresh_archipelago_buttons())

        # Same wiring for the PopTracker directory, so its own three buttons and status
        # line follow the field however it changes - typing, Browse, a scan, or the
        # download filling it in.
        pop_entry = self._entries[POPTRACKER_DIR_KEY]
        pop_entry.bind("<FocusOut>", lambda _e: self._refresh_poptracker_buttons(), add="+")
        self.vars[POPTRACKER_DIR_KEY].trace_add(
            "write", lambda *_a: self._refresh_poptracker_buttons())

        # (attr, label, tooltip, command) - attr is kept so
        # _refresh_archipelago_buttons can enable/disable each one individually with a
        # message naming the exact exe that's missing.
        launch = ttk.LabelFrame(wrap, text="Launch", padding=(10, 6))
        launch.pack(fill="x", pady=(0, 8))
        ttk.Label(launch, wraplength=760, justify="left",
                  foreground=self.theme["note_fg"], font=("Segoe UI", 8, "italic"),
                  text="Open Text Client automatically fills in your Archipelago room "
                       "details when opening. It can take a couple seconds for the app "
                       "to open, that's normal."
                  ).pack(fill="x", anchor="w", pady=(0, 4))
        ttk.Label(launch, wraplength=760, justify="left",
                  foreground=self.theme["note_fg"], font=("Segoe UI", 8, "italic"),
                  text="Hosting locally is an alternative to uploading your seed to "
                       "archipelago.gg, not an extra step - do one or the other. The "
                       "room then lives on your PC: you connect to it at localhost, but "
                       "everyone else connects to YOUR IP address, and anyone outside "
                       "your home network can only reach it if you forward the "
                       "Archipelago port (default %d, TCP) to this PC on your router. "
                       "Players on the same network as you need no forwarding. Closing "
                       "the server's console window ends the room."
                       % ARCHIPELAGO_DEFAULT_PORT
                  ).pack(fill="x", anchor="w", pady=(0, 4))
        self._archipelago_buttons = []
        self._archipelago_folder_buttons = []
        launch_specs = [
            ("btn_text_client", "Open Text Client", ARCHIPELAGO_TEXT_CLIENT_EXE,
             self._open_text_client,
             "Opens Archipelago's Text Client, already connected using the server, slot "
             "and password above (it takes --connect/--name/--password on the command "
             "line, so no copy-pasting). Leave the fields blank to just open it "
             "unconnected."),
            ("btn_options_creator", "Open Options Creator (YAML)",
             "ArchipelagoOptionsCreator.exe", self._open_options_creator,
             "Opens Archipelago's visual .yaml builder.\n"
             "Note: it can't be opened straight onto a specific game - it takes no "
             "command-line arguments at all, so pick \"ARK: Survival Evolved\" from its "
             "own game list once it's open."),
            ("btn_generate", "Generate seed", "ArchipelagoGenerate.exe",
             self._open_generate,
             "Runs ArchipelagoGenerate.exe, which builds a multiworld from every .yaml "
             "in Archipelago's Players folder and writes the result into its output "
             "folder."),
            ("btn_host_server", "Host local Archipelago server",
             ARCHIPELAGO_SERVER_EXE, self._host_local_server,
             "Hosts a room on THIS machine instead of uploading your seed to "
             "archipelago.gg.\n"
             "Asks which generated seed to open (offering the newest from your output "
             "folder), then starts ArchipelagoServer.exe in its own console window - it "
             "takes the seed file on the command line, so there's nothing to browse for "
             "inside it. That window is where the room's output appears and where you "
             "type commands like /send, so leave it open; closing it ends the room.\n"
             "Offers to point the server field above at your local room afterwards, so "
             "\"Copy ARK connection command\" and \"Open Text Client\" just work.\n"
             "Uses the room password above (if any) and the port from Archipelago's own "
             "host.yaml."),
            ("btn_launcher", "Open Archipelago Launcher", "ArchipelagoLauncher.exe",
             self._open_archipelago_launcher,
             "Opens Archipelago's own launcher - the general entry point to everything "
             "else it ships (server, other clients, adjusters)."),
        ]
        row = ttk.Frame(launch)
        row.pack(fill="x")
        for attr, text, exe, cmd, tip in launch_specs:
            btn = ttk.Button(row, text=text, command=cmd)
            btn.pack(side="left", padx=3, pady=2)
            Tooltip(btn, tip, wraplength=520)
            setattr(self, attr, btn)
            self._archipelago_buttons.append((btn, exe, tip))

        # Folder shortcuts. These need the directory but no particular exe, so they're
        # gated only on the directory being set - hence a separate list from the launch
        # buttons above rather than another entry in launch_specs.
        folders = ttk.LabelFrame(wrap, text="Folders", padding=(10, 6))
        folders.pack(fill="x")
        folder_specs = [
            ("Open custom_worlds folder", "custom_worlds",
             "Opens Archipelago's custom_worlds folder - this is where the ARK "
             ".apworld has to be dropped before generating or the ARK options won't "
             "exist. Created if it isn't there yet."),
            ("Open Players folder", "Players",
             "Opens Archipelago's Players folder - where your .yaml files go, and "
             "where \"Generate seed\" reads them from. Created if it isn't there yet."),
            ("Open output folder", "output",
             "Opens Archipelago's output folder - where \"Generate seed\" writes the "
             "finished .zip you host. Created if it isn't there yet."),
            ("Open Archipelago folder", "",
             "Opens the Archipelago install folder itself in Explorer."),
        ]
        frow = ttk.Frame(folders)
        frow.pack(fill="x")
        for text, sub, tip in folder_specs:
            btn = ttk.Button(frow, text=text,
                             command=lambda s=sub, t=text: self._open_archipelago_subfolder(s, t))
            btn.pack(side="left", padx=3, pady=2)
            Tooltip(btn, tip, wraplength=520)
            self._archipelago_folder_buttons.append(btn)

        self._build_apworld_section(wrap)
        self._refresh_archipelago_buttons()

    def _build_archipelago_scan_row(self, parent):
        """The "Scan for Archipelago" row. Built from inside _render_field_groups so it
        lands in the same LabelFrame as the directory field it fills."""
        scanrow = ttk.Frame(parent)
        scanrow.pack(fill="x", pady=(0, 2))
        self.archipelago_scan_btn = ttk.Button(scanrow, text="Scan for Archipelago",
                                                command=self._on_scan_archipelago)
        self.archipelago_scan_btn.pack(side="left")
        Tooltip(self.archipelago_scan_btn,
                "Finds your Archipelago install. Checks the common locations first "
                "(%s, Program Files, your user folder) and only falls back to the "
                "wider drive scan if none of them match - so the usual case is "
                "instant.\n"
                "A folder only counts if it contains all three of %s, so a half-copied "
                "or unrelated folder is never accepted."
                % (ARCHIPELAGO_DEFAULT_DIR, ", ".join(ARCHIPELAGO_REQUIRED_EXES)),
                wraplength=520)
        self.archipelago_scan_progress = ttk.Progressbar(scanrow, mode="indeterminate",
                                                          length=90)
        self.archipelago_status_label = ttk.Label(scanrow, text="",
                                                   foreground=self.theme["subtle_fg"])
        self.archipelago_status_label.pack(side="left", padx=8)
        self._dir_scan_widgets[ARCHIPELAGO_DIR_KEY] = (
            self.archipelago_scan_btn, self.archipelago_scan_progress,
            self.archipelago_status_label)

    # ------------------------------------------- Archipelago tab: apworld -- #
    def _build_apworld_section(self, parent):
        box = ttk.LabelFrame(parent, text="ARK world (.apworld)", padding=(10, 6))
        box.pack(fill="x", pady=(8, 0))
        ttk.Label(box, wraplength=760, justify="left", foreground=self.theme["subtle_fg"],
                  text="Downloads the latest %s from this project's GitHub releases and "
                       "puts it straight into Archipelago's custom_worlds folder - the "
                       "same file you'd otherwise download and drag in by hand. Any "
                       "copy already there is backed up first, never overwritten "
                       "silently." % APWORLD_ASSET_NAME
                  ).pack(fill="x", anchor="w", pady=(0, 4))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.apworld_update_btn = ttk.Button(row, text="Update .apworld",
                                              command=self._on_update_apworld)
        self.apworld_update_btn.pack(side="left")
        Tooltip(self.apworld_update_btn,
                "Fetches the newest %s release asset and installs it into "
                "custom_worlds under your Archipelago directory.\n"
                "The existing file (if any) is renamed to a timestamped .bak alongside "
                "it, so a bad release is always one rename away from being undone.\n"
                "Needs the Archipelago directory above to be set."
                % APWORLD_ASSET_NAME, wraplength=520)
        self.apworld_progress = ttk.Progressbar(row, mode="determinate", length=140,
                                                 maximum=100)
        self.apworld_status_var = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.apworld_status_var,
                  foreground=self.theme["subtle_fg"]).pack(side="left", padx=8)

    def _on_update_apworld(self):
        """Download the latest ark_ase.apworld into custom_worlds, on a worker thread.

        Gated on the Archipelago directory rather than on the button's disabled state
        as well: this button is enabled whenever the folder is set (it needs no
        Archipelago .exe at all), so the check has to live here."""
        if getattr(self, "_apworld_thread", None) is not None \
                and self._apworld_thread.is_alive():
            self._log("Update .apworld: a download is already running.")
            return
        root = self._archipelago_dir()
        if not root:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "The Archipelago directory is not set.\n\nSet it at the top of this tab "
                "(or click \"Scan for Archipelago\") so the launcher knows where "
                "custom_worlds is.")
            return
        if not os.path.isdir(root):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "The Archipelago directory doesn't exist:\n\n%s" % root)
            return
        # Created rather than required: custom_worlds is Archipelago's own well-known
        # folder, and a fresh install that has never used a custom world won't have it.
        custom_worlds = os.path.join(root, "custom_worlds")
        try:
            os.makedirs(custom_worlds, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("ARKIpelago Launcher",
                                 "Could not create:\n%s\n\n%s" % (custom_worlds, exc))
            return

        self.apworld_update_btn.configure(state="disabled")
        self.apworld_status_var.set("Fetching release info...")
        self.apworld_progress.pack(side="left", padx=(6, 0))
        self.apworld_progress["value"] = 0
        self._apworld_queue = queue.Queue()
        self._apworld_thread = threading.Thread(
            target=self._apworld_worker, args=(custom_worlds,), daemon=True)
        self._apworld_thread.start()
        self.after(150, self._poll_apworld_queue)

    def _apworld_worker(self, custom_worlds):
        q = self._apworld_queue
        try:
            q.put(("line", "Fetching latest ARKipelago release info..."))
            tag, asset_name, url, _size = self._fetch_latest_release_asset(
                APWORLD_ASSET_NAME, q)
        except (OSError, ValueError, RuntimeError) as exc:
            q.put(("line", "! Could not fetch release info: %s" % exc))
            q.put(("done", None))
            return

        # Downloaded to a temp file and only moved into place once complete, so a
        # failed or half-finished download can never leave a truncated .apworld where
        # Archipelago would try to load it.
        tmp_dir = tempfile.mkdtemp(prefix="arkap_apworld_dl_")
        tmp_path = os.path.join(tmp_dir, asset_name)
        dest = os.path.join(custom_worlds, APWORLD_ASSET_NAME)
        try:
            q.put(("line", "Downloading %s..." % asset_name))
            self._download_with_progress(url, tmp_path, q)
            backup = None
            if os.path.exists(dest):
                backup = self._backup_file(dest, time.strftime("%Y%m%d-%H%M%S"))
                q.put(("line", "Backed up the existing file to: %s"
                       % os.path.basename(backup)))
            shutil.move(tmp_path, dest)
        except (OSError, ValueError) as exc:
            q.put(("line", "! Update failed: %s" % exc))
            shutil.rmtree(tmp_dir, ignore_errors=True)
            q.put(("done", None))
            return
        shutil.rmtree(tmp_dir, ignore_errors=True)
        q.put(("line", "Installed %s (%s) into %s" % (APWORLD_ASSET_NAME, tag,
                                                       custom_worlds)))
        q.put(("done", {"tag": tag, "dest": dest, "backup": backup}))

    def _poll_apworld_queue(self):
        try:
            while True:
                kind, payload = self._apworld_queue.get_nowait()
                if kind == "line":
                    self._log(payload)
                elif kind == "progress":
                    try:
                        self.apworld_progress["value"] = payload
                        self.apworld_status_var.set("Downloading... %d%%" % payload)
                    except tk.TclError:
                        pass
                elif kind == "done":
                    self._on_apworld_done(payload)
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll_apworld_queue)

    def _on_apworld_done(self, payload):
        self._apworld_thread = None
        try:
            self.apworld_progress.pack_forget()
            self.apworld_update_btn.configure(state="normal")
        except tk.TclError:
            pass
        if not payload:
            self.apworld_status_var.set("Failed - see the log.")
            messagebox.showerror(
                "Update .apworld",
                "Could not update %s. See the log at the bottom of the Configuration "
                "tab for details.\n\nYou can always download it by hand from:\n%s"
                % (APWORLD_ASSET_NAME, RELEASES_URL))
            return
        self.apworld_status_var.set("Updated (%s)" % payload["tag"])
        # The .apworld carries no version of its own (see APWORLD_INSTALLED_VERSION_KEY),
        # so record the tag we just laid down - this is the ONLY baseline the update check
        # has for it. Then re-run the check so the "!" clears immediately on this update.
        self._write_config_key(APWORLD_INSTALLED_VERSION_KEY, payload["tag"],
                               "installed .apworld version")
        self._start_component_version_check()
        extra = ("\n\nYour previous copy was kept as:\n%s"
                 % os.path.basename(payload["backup"])) if payload["backup"] else ""
        messagebox.showinfo(
            "Update .apworld",
            "%s (%s) installed into:\n%s%s\n\nRestart Archipelago (and re-generate) for "
            "it to be picked up."
            % (APWORLD_ASSET_NAME, payload["tag"], os.path.dirname(payload["dest"]), extra))

    # ------------------------------------------- PopTracker (tracker) ------- #
    def _build_poptracker_controls(self, parent):
        """Everything the PopTracker group holds under its directory field: the scan row,
        then the pack-install / download / launch buttons. Built from inside
        _render_field_groups so all of it lands in that one LabelFrame, for the same reason
        _build_archipelago_scan_row does - setting the folder, finding it and using it read
        as one feature instead of controls scattered down the tab."""
        intro = ttk.Label(parent, wraplength=760, justify="left",
                          foreground=self.theme["subtle_fg"],
                          text="PopTracker is a separate app that tracks your multiworld. "
                               "Point this at a copy you already have, or let the launcher "
                               "fetch one - it isn't bundled with the launcher (it's ~38 MB "
                               "on its own). The %s installs into that folder's \"%s\" "
                               "subfolder."
                               % (TRACKER_PACK_LABEL, POPTRACKER_PACKS_DIRNAME))
        # Above the field, not below it: this runs as a hook from _render_field_groups, so
        # the field's row is already packed by the time we get here.
        already = parent.winfo_children()
        intro.pack(fill="x", anchor="w", pady=(0, 4),
                   **({"before": already[0]} if already else {}))

        scanrow = ttk.Frame(parent)
        scanrow.pack(fill="x", pady=(0, 2))
        self.poptracker_scan_btn = ttk.Button(scanrow, text="Scan for PopTracker",
                                               command=self._on_scan_poptracker)
        self.poptracker_scan_btn.pack(side="left")
        Tooltip(self.poptracker_scan_btn,
                "Finds an existing PopTracker install. Checks the likely spots first "
                "(next to this launcher, C:\\PopTracker, your user folder, Desktop / "
                "Documents / Downloads) and only falls back to the wider drive scan if "
                "none of them match.\n"
                "A folder counts if it contains %s. PopTracker has no installer and no "
                "standard location, so if the scan misses it, Browse to it - or use "
                "\"Download PopTracker\"." % POPTRACKER_EXE, wraplength=520)
        self.poptracker_scan_progress = ttk.Progressbar(scanrow, mode="indeterminate",
                                                         length=90)
        self.poptracker_status_label = ttk.Label(scanrow, text="",
                                                  foreground=self.theme["subtle_fg"])
        self.poptracker_status_label.pack(side="left", padx=8)
        self._dir_scan_widgets[POPTRACKER_DIR_KEY] = (
            self.poptracker_scan_btn, self.poptracker_scan_progress,
            self.poptracker_status_label)

        # (attribute, label, what it needs, command, tooltip). "needs" is what
        # _refresh_poptracker_buttons gates on: "dir" = the folder must be set and exist,
        # "exe" = poptracker.exe must be in it, "" = always available - which is the whole
        # point of the download button, it's what you press when nothing is set yet.
        specs = [
            ("poptracker_pack_btn", "Install/update %s" % TRACKER_PACK_LABEL, "dir",
             self._on_install_tracker_pack,
             "Downloads the latest %s from GitHub and installs it into PopTracker's %s "
             "folder - the same files you'd otherwise download and unzip by hand.\n"
             "A copy already there is moved aside into a timestamped folder under "
             "\"%s\" first, never overwritten - and deliberately not left inside %s, "
             "since PopTracker would list a backup sitting there as a second copy of "
             "the pack.\n"
             "Needs the PopTracker directory above to be set."
             % (TRACKER_PACK_LABEL, POPTRACKER_PACKS_DIRNAME,
                TRACKER_PACK_BACKUP_DIRNAME, POPTRACKER_PACKS_DIRNAME)),
            ("poptracker_download_btn", "Download PopTracker", "",
             self._on_download_poptracker,
             "Fetches PopTracker itself (the latest stable Windows build from "
             "black-sliver/PopTracker - release candidates are skipped), extracts it into "
             "a folder you pick, points the field above at it, and installs the %s into "
             "it. One click, nothing to work out first.\n"
             "Only needed if you don't already have PopTracker - if you do, set the "
             "directory above (or use \"Scan for PopTracker\") instead."
             % TRACKER_PACK_LABEL),
            ("poptracker_open_btn", "Open PopTracker", "exe", self._open_poptracker,
             "Opens PopTracker loaded straight onto the ARK pack (it takes --load-pack / "
             "--pack-variant on the command line), so there's no pack to pick inside it.\n"
             "Auto-connecting to your room only works on PopTracker %s or newer, which is "
             "the version its --ap-host/--ap-slot/--ap-password arguments arrived in - and "
             "an older build refuses to start when given them rather than ignoring them, so "
             "the launcher can't fake it. There's nothing else to write either: PopTracker "
             "remembers a host and slot, but only as the defaults for its own dialog, and "
             "it never stores the password.\n"
             "On an older copy - which includes the current stable build - the room address "
             "is copied to your clipboard instead, with a note on where to paste it: click "
             "the grey \"AP\" inside PopTracker. The pack still loads automatically.\n"
             "Needs %s in the PopTracker directory above."
             % (POPTRACKER_AP_ARGS_MIN_VERSION, POPTRACKER_EXE)),
        ]
        btnrow = ttk.Frame(parent)
        btnrow.pack(fill="x", pady=(4, 2))
        self._poptracker_buttons = []
        for attr, text, needs, cmd, tip in specs:
            btn = ttk.Button(btnrow, text=text, command=cmd)
            btn.pack(side="left", padx=(0, 6), pady=2)
            Tooltip(btn, tip, wraplength=520)
            setattr(self, attr, btn)
            self._poptracker_buttons.append((btn, needs, tip))
        self.poptracker_progress = ttk.Progressbar(btnrow, mode="determinate", length=140,
                                                    maximum=100)
        self._poptracker_status_var = tk.StringVar(value="")
        ttk.Label(btnrow, textvariable=self._poptracker_status_var,
                  foreground=self.theme["subtle_fg"]).pack(side="left", padx=8)
        self._refresh_poptracker_buttons()

    def _poptracker_dir(self):
        """The configured PopTracker directory, normalised, or "" when unset. get()
        returns "" while the greyed placeholder is showing, so the example path can never
        be mistaken for a real install (same contract as _archipelago_dir)."""
        value = self.get(POPTRACKER_DIR_KEY)
        return os.path.normpath(value) if value else ""

    def _refresh_poptracker_buttons(self, _event=None):
        """Enable/disable the PopTracker buttons to match the current directory, with the
        reason on the tooltip - a disabled ttk button swallows clicks, so a messagebox on
        press would never be seen (same approach as _refresh_archipelago_buttons)."""
        # The directory field is rendered before these buttons exist, and set() fires the
        # trace that calls this - so a partially built tab has to be a no-op, not a crash.
        if not hasattr(self, "_poptracker_buttons"):
            return
        path = self._poptracker_dir()
        busy = (getattr(self, "_poptracker_thread", None) is not None
                and self._poptracker_thread.is_alive())
        for btn, needs, base_tip in self._poptracker_buttons:
            if busy:
                ok, why = False, "A PopTracker download is running - let it finish first."
            elif not needs:
                ok, why = True, ""
            elif not path:
                ok, why = False, ("Set the PopTracker directory above first - Browse, "
                                  "\"Scan for PopTracker\", or \"Download PopTracker\" to "
                                  "fetch a copy.")
            elif not os.path.isdir(path):
                ok, why = False, "The PopTracker directory doesn't exist:\n%s" % path
            elif needs == "exe" and not is_poptracker_dir(path):
                ok, why = False, ("%s isn't in the PopTracker directory:\n%s"
                                  % (POPTRACKER_EXE, path))
            else:
                ok, why = True, ""
            try:
                btn.configure(state=("normal" if ok else "disabled"))
            except tk.TclError:
                pass
            tip = getattr(btn, "_tooltip", None)
            if tip is not None:
                tip.text = base_tip if ok else why + "\n\n" + base_tip
        if not hasattr(self, "poptracker_status_label"):
            return
        if not path:
            msg = "No PopTracker directory set."
        elif not os.path.isdir(path):
            msg = "That folder doesn't exist."
        elif not is_poptracker_dir(path):
            msg = "Folder found, but %s isn't in it." % POPTRACKER_EXE
        else:
            pack, version = installed_tracker_pack(poptracker_packs_dir(path))
            if version:
                msg = "PopTracker found - %s %s installed." % (TRACKER_PACK_LABEL, version)
            elif pack:
                msg = ("PopTracker found - %s installed (version unknown)."
                       % TRACKER_PACK_LABEL)
            else:
                msg = "PopTracker found - %s not installed yet." % TRACKER_PACK_LABEL
        try:
            self.poptracker_status_label.configure(text=msg)
        except tk.TclError:
            pass

    def _poptracker_busy(self, label):
        """True (with a log line) while a PopTracker download/install is already running.
        Both jobs share one thread and one queue: the download job continues straight into
        the pack install, so they were never independent to begin with."""
        thread = getattr(self, "_poptracker_thread", None)
        if thread is not None and thread.is_alive():
            self._log("%s: a PopTracker download is already running." % label)
            return True
        return False

    def _start_poptracker_job(self, target, args, status):
        self._poptracker_status_var.set(status)
        self._poptracker_queue = queue.Queue()
        self._poptracker_thread = threading.Thread(target=target, args=args, daemon=True)
        self._poptracker_thread.start()
        self._refresh_poptracker_buttons()   # greys the group out while it runs
        try:
            self.poptracker_progress["value"] = 0
            self.poptracker_progress.pack(side="left", padx=(6, 0))
        except tk.TclError:
            pass
        self.after(150, self._poll_poptracker_queue)

    def _on_install_tracker_pack(self):
        """Install/update the ARK tracker pack into the configured PopTracker's packs
        folder, on a worker thread.

        Gated on the directory here rather than only on the button's state, for the same
        reason _on_update_apworld is: this is also reachable from the "Check for Updates"
        dialog, which knows nothing about that button."""
        label = "Install/update %s" % TRACKER_PACK_LABEL
        if self._poptracker_busy(label):
            return
        root = self._poptracker_dir()
        if not root:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "The PopTracker directory is not set.\n\nSet it in the \"PopTracker "
                "(tracker)\" group on this tab - Browse to it, click \"Scan for "
                "PopTracker\", or use \"Download PopTracker\" to have the launcher fetch "
                "PopTracker and the pack together.")
            return
        if not os.path.isdir(root):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "The PopTracker directory doesn't exist:\n\n%s" % root)
            return
        # Created rather than required: packs\ is PopTracker's own well-known folder and a
        # freshly extracted copy ships it empty, but a copy someone tidied up won't have it.
        packs = poptracker_packs_dir(root)
        try:
            os.makedirs(packs, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("ARKIpelago Launcher",
                                 "Could not create:\n%s\n\n%s" % (packs, exc))
            return
        self._start_poptracker_job(self._tracker_pack_job, (packs,),
                                   "Fetching release info...")

    def _on_download_poptracker(self):
        """Path 2: fetch PopTracker itself into a folder the user picks, then install the
        ARK pack into it - one click from nothing at all to a working tracker.

        Never silently duplicates an install. A copy already configured, or one already
        sitting in the folder picked, is adopted (after asking) instead: those hold the
        user's own packs and tracker state next to the exe, and quietly extracting over
        them would lose both."""
        label = "Download PopTracker"
        if self._poptracker_busy(label):
            return
        current = self._poptracker_dir()
        if is_poptracker_dir(current) and not messagebox.askyesno(
                label,
                "PopTracker is already set up here:\n\n%s\n\nDownload another copy "
                "anyway?\n\nA second copy keeps its own packs and its own tracker state, "
                "so this is only worth doing if you want a fresh install somewhere else. "
                "To just refresh the pack in the copy you have, use \"Install/update "
                "%s\"." % (current, TRACKER_PACK_LABEL)):
            return
        dest_root = filedialog.askdirectory(
            title="Where should PopTracker go? (a \"%s\" folder is created inside)"
                  % POPTRACKER_DOWNLOAD_SUBDIR_NOTE,
            initialdir=current if os.path.isdir(current) else base_dir())
        if not dest_root:
            return
        dest_root = os.path.normpath(dest_root)
        existing = next(
            (p for p in (dest_root,
                         os.path.join(dest_root, POPTRACKER_DOWNLOAD_SUBDIR_NOTE))
             if is_poptracker_dir(p)), None)
        if existing:
            if not messagebox.askyesno(
                    label,
                    "PopTracker is already installed here:\n\n%s\n\nUse this copy and "
                    "just install the %s into it?\n\n(Nothing is downloaded or replaced - "
                    "your existing PopTracker, its other packs and its saved state are "
                    "left alone.)" % (existing, TRACKER_PACK_LABEL)):
                return
            self.set(POPTRACKER_DIR_KEY, existing)
            self._log("%s: %s already holds PopTracker - using it." % (label, existing))
            self._on_install_tracker_pack()
            return
        self._start_poptracker_job(self._poptracker_download_job, (dest_root,),
                                   "Fetching release info...")

    # The two jobs. Both run on the shared worker thread and report through the shared
    # queue; the download job simply continues into the pack install, which is what makes
    # "Download PopTracker" one click rather than two.
    def _tracker_pack_job(self, packs):
        q = self._poptracker_queue
        q.put(("done", {"kind": "pack", "pack": self._install_tracker_pack(q, packs)}))

    def _poptracker_download_job(self, dest_root):
        q = self._poptracker_queue
        root = self._download_poptracker_app(q, dest_root)
        pack = self._install_tracker_pack(q, poptracker_packs_dir(root)) if root else None
        q.put(("done", {"kind": "app", "dir": root, "pack": pack}))

    def _install_tracker_pack(self, q, packs):
        """Download the newest tracker pack release and put it in `packs`. Returns a
        payload dict, or None on any failure (already logged to `q`). Worker thread only.

        The download is GitHub's auto-generated source zip, which nests everything one
        level down in a folder named after the repo and commit - so the extracted tree is
        searched for the manifest.json and THAT folder is what gets installed, under a
        stable name (see TRACKER_PACK_DIRNAME). Extracted to a temp folder first and only
        moved into packs\\ once complete, so a failed or half-finished download can never
        leave a broken pack where PopTracker would try to load it."""
        try:
            release = _fetch_newest_release(TRACKER_PACK_RELEASES_API)
        except (OSError, ValueError) as exc:
            q.put(("line", "! Could not fetch %s release info: %s"
                   % (TRACKER_PACK_LABEL, exc)))
            return None
        url = (release or {}).get("zipball_url")
        if not url:
            q.put(("line", "! No %s release found (checked %s)."
                   % (TRACKER_PACK_LABEL, TRACKER_PACK_RELEASES_API)))
            return None
        tag = (release.get("tag_name") or "unknown").strip()
        q.put(("line", "Latest %s release: %s (GitHub source zip - the pack publishes no "
                       "release assets of its own)" % (TRACKER_PACK_LABEL, tag)))

        tmp_dir = tempfile.mkdtemp(prefix="arkap_trackerpack_")
        zip_path = os.path.join(tmp_dir, "trackerpack.zip")
        extract_dir = os.path.join(tmp_dir, "unzipped")
        try:
            q.put(("line", "Downloading %s %s..." % (TRACKER_PACK_LABEL, tag)))
            self._download_with_progress(url, zip_path, q)
            q.put(("status", "Installing..."))
            self._extract_zip_to(zip_path, extract_dir, q)
            src = locate_extracted_pack(extract_dir)
            if src is None:
                q.put(("line", "! The download had no %s at any level - nothing installed."
                       % TRACKER_PACK_MANIFEST))
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return None
            _uid, version = read_pack_manifest(src)
            dest = os.path.join(packs, TRACKER_PACK_DIRNAME)
            # Two things get moved aside: whatever copy of this pack is already installed
            # (under any name, folder or zip - see installed_tracker_pack), and anything
            # else occupying the folder name about to be written. Usually they're the same
            # entry, hence the dedupe.
            ts = time.strftime("%Y%m%d-%H%M%S")
            existing, _old_version = installed_tracker_pack(packs)
            moved_from, backups = [], []
            for old in (existing, dest):
                if not old or not os.path.exists(old):
                    continue
                if any(os.path.normcase(old) == os.path.normcase(seen)
                       for seen in moved_from):
                    continue
                moved_from.append(old)
                backups.append(self._backup_pack_entry(old, ts))
                q.put(("line", "Moved the existing pack aside: %s" % backups[-1]))
            shutil.move(src, dest)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            q.put(("line", "! %s install failed: %s" % (TRACKER_PACK_LABEL, exc)))
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None
        shutil.rmtree(tmp_dir, ignore_errors=True)
        q.put(("line", "Installed %s %s into %s"
               % (TRACKER_PACK_LABEL, version or tag, dest)))
        return {"tag": tag, "version": version or tag, "dest": dest, "backups": backups}

    def _backup_pack_entry(self, path, ts):
        """Move a pack (folder or zip) out of packs\\ into
        <PopTracker dir>\\pack_backups\\<name>.<timestamp>, and return where it went.

        A move rather than a copy, and out of packs\\ rather than a .bak beside it:
        PopTracker treats every entry of that folder as a pack, so a backup left there
        would still carry the same package_uid and turn up as a duplicate in its Load
        list. Timestamped, with a dupe suffix on a same-second collision - the same
        never-delete contract as _backup_file, which can't be reused here because a pack
        can be a whole folder rather than a single file."""
        packs = os.path.dirname(os.path.normpath(path))
        backup_dir = os.path.join(os.path.dirname(packs), TRACKER_PACK_BACKUP_DIRNAME)
        os.makedirs(backup_dir, exist_ok=True)
        name = os.path.basename(os.path.normpath(path))
        target = os.path.join(backup_dir, "%s.%s" % (name, ts))
        dupe = 2
        while os.path.exists(target):
            target = os.path.join(backup_dir, "%s.%s-%d" % (name, ts, dupe))
            dupe += 1
        shutil.move(path, target)
        return target

    def _download_poptracker_app(self, q, dest_root):
        """Download the latest stable PopTracker Windows build and extract it into
        `dest_root`. Returns the install folder (dest_root\\poptracker), or None on failure
        (already logged to `q`). Worker thread only."""
        try:
            release = _fetch_newest_release(POPTRACKER_RELEASES_API)
        except (OSError, ValueError) as exc:
            q.put(("line", "! Could not fetch PopTracker release info: %s" % exc))
            return None
        asset = poptracker_win64_asset(release or {})
        if asset is None:
            q.put(("line", "! The latest PopTracker release (%s) has no Windows build "
                           "(*%s). Download it by hand from %s."
                   % ((release or {}).get("tag_name") or "unknown",
                      POPTRACKER_ASSET_SUFFIX, POPTRACKER_RELEASES_PAGE)))
            return None
        tag = (release.get("tag_name") or "unknown").strip()
        q.put(("line", "Latest PopTracker release: %s - asset %s" % (tag, asset["name"])))

        tmp_dir = tempfile.mkdtemp(prefix="arkap_poptracker_")
        zip_path = os.path.join(tmp_dir, asset["name"])
        extract_dir = os.path.join(tmp_dir, "unzipped")
        try:
            q.put(("line", "Downloading %s..." % asset["name"]))
            self._download_with_progress(asset["browser_download_url"], zip_path, q)
            q.put(("status", "Extracting..."))
            self._extract_zip_to(zip_path, extract_dir, q)
            src = locate_extracted_poptracker(extract_dir)
            if src is None:
                q.put(("line", "! %s contained no %s - nothing installed."
                       % (asset["name"], POPTRACKER_EXE)))
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return None
            os.makedirs(dest_root, exist_ok=True)
            dest = os.path.join(dest_root, os.path.basename(src))
            if os.path.exists(dest):
                # A real install here was already adopted by the caller, so this is a
                # leftover or an unrelated folder in the way. Moved aside, never merged
                # into or deleted.
                moved = "%s.%s.bak" % (dest, time.strftime("%Y%m%d-%H%M%S"))
                shutil.move(dest, moved)
                q.put(("line", "Moved an existing \"%s\" folder aside: %s"
                       % (os.path.basename(dest), moved)))
            shutil.move(src, dest)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            q.put(("line", "! PopTracker download failed: %s" % exc))
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None
        shutil.rmtree(tmp_dir, ignore_errors=True)
        q.put(("line", "PopTracker %s installed into %s" % (tag, dest)))
        return dest

    def _poll_poptracker_queue(self):
        try:
            while True:
                kind, payload = self._poptracker_queue.get_nowait()
                if kind == "line":
                    self._log(payload)
                elif kind == "status":
                    self._poptracker_status_var.set(payload)
                elif kind == "progress":
                    try:
                        self.poptracker_progress["value"] = payload
                        self._poptracker_status_var.set("Downloading... %d%%" % payload)
                    except tk.TclError:
                        pass
                elif kind == "done":
                    self._on_poptracker_job_done(payload)
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll_poptracker_queue)

    def _on_poptracker_job_done(self, payload):
        self._poptracker_thread = None
        try:
            self.poptracker_progress.pack_forget()
        except tk.TclError:
            pass
        app_dir = payload.get("dir")
        if app_dir:
            # Point the field at the install just laid down, exactly as the plugin install
            # points PLUGINS_DIR at its own result - one path variable, not a second one.
            self.set(POPTRACKER_DIR_KEY, app_dir)
        pack = payload.get("pack")
        if pack:
            # The pack's manifest.json is the real source of truth (see
            # installed_tracker_pack); this records it so the diagnostics version block and
            # the update check have a baseline even before the next disk read.
            self._write_config_key(TRACKER_PACK_INSTALLED_VERSION_KEY, pack["version"],
                                   "installed %s version" % TRACKER_PACK_LABEL)
            self._start_component_version_check()
        self._refresh_poptracker_buttons()

        if payload.get("kind") == "app" and not app_dir:
            self._poptracker_status_var.set("Failed - see the log.")
            messagebox.showerror(
                "Download PopTracker",
                "Could not download PopTracker. See the log at the bottom of the "
                "Configuration tab for details.\n\nYou can always download it by hand "
                "from:\n%s\n\nThen point the PopTracker directory at the folder holding "
                "%s." % (POPTRACKER_RELEASES_PAGE, POPTRACKER_EXE))
            return
        if not pack:
            self._poptracker_status_var.set("Failed - see the log.")
            messagebox.showerror(
                "Install/update %s" % TRACKER_PACK_LABEL,
                "Could not install the %s. See the log at the bottom of the Configuration "
                "tab for details.\n\nYou can always download it by hand from:\n%s\n\nThe "
                "\"Source code (zip)\" asset is the pack - unzip it into PopTracker's %s "
                "folder." % (TRACKER_PACK_LABEL, TRACKER_PACK_RELEASES_PAGE,
                             POPTRACKER_PACKS_DIRNAME))
            return

        self._poptracker_status_var.set("Pack %s installed" % pack["version"])
        kept = ("\n\nYour previous copy was moved to:\n%s" % "\n".join(pack["backups"])
                if pack["backups"] else "")
        if app_dir:
            messagebox.showinfo(
                "Download PopTracker",
                "PopTracker is installed here:\n%s\n\n%s %s was installed into its %s "
                "folder.%s\n\nThe PopTracker directory has been filled in for you - click "
                "Save on this tab so it's remembered next time. Then use \"Open "
                "PopTracker\"."
                % (app_dir, TRACKER_PACK_LABEL, pack["version"],
                   POPTRACKER_PACKS_DIRNAME, kept))
            return
        messagebox.showinfo(
            "Install/update %s" % TRACKER_PACK_LABEL,
            "%s %s installed into:\n%s%s\n\nIf PopTracker is already open, press Ctrl+F5 "
            "(force-reload) or restart it for the new version to be picked up."
            % (TRACKER_PACK_LABEL, pack["version"], pack["dest"], kept))

    def _open_poptracker(self):
        """Launch PopTracker, loaded onto the ARK pack and - where the installed version
        supports it - already connected to the room.

        PopTracker does take command-line arguments, unlike Archipelago's Options Creator,
        but which ones depends on its version, and the difference is not cosmetic (see
        POPTRACKER_AP_ARGS_MIN_VERSION): --load-pack / --pack-variant work on every
        release in play, while the --ap-* room arguments make an older build print its
        usage and exit instead of opening. So the pack is always pre-loaded, the room is
        pre-filled only on 0.35.4+, and the log says which of the two happened rather than
        leaving the user to wonder why the AP dot is still grey.

        Blank room fields are simply left out, and --load-pack is only passed once the pack
        is really installed."""
        path = self._poptracker_dir()
        exe = os.path.join(path, POPTRACKER_EXE) if path else ""
        if not exe or not os.path.isfile(exe):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "%s was not found in the PopTracker directory:\n%s\n\nSet the PopTracker "
                "directory in the \"PopTracker (tracker)\" group on this tab (or use "
                "\"Scan for PopTracker\" / \"Download PopTracker\")."
                % (POPTRACKER_EXE, exe or "(not set)"))
            self._refresh_poptracker_buttons()
            return
        args = []
        pack, _version = installed_tracker_pack(poptracker_packs_dir(path))
        if pack:
            args += ["--load-pack", TRACKER_PACK_UID,
                     "--pack-variant", TRACKER_PACK_VARIANT]
        else:
            self._log("Open PopTracker: the %s isn't installed yet - opening PopTracker "
                      "without loading a pack (use \"Install/update %s\" first)."
                      % (TRACKER_PACK_LABEL, TRACKER_PACK_LABEL))
        room = [("--ap-host", self.get("server")), ("--ap-slot", self.get("slot")),
                ("--ap-password", self.get("password"))]
        hand_over = None
        if not self.get("server"):
            self._log("Open PopTracker: no server set - its Archipelago autotracker will "
                      "have to be connected by hand (click the grey \"AP\").")
        elif poptracker_supports_ap_args(path):
            args += [part for flag, value in room if value for part in (flag, value)]
        else:
            # Older build: hand the details over instead of pretending to connect. See
            # poptracker_room_hint for why there is no config file to write instead.
            hand_over, note = poptracker_room_hint(
                self.get("server"), self.get("slot"), self.get("password"))
            self._log("Open PopTracker: this copy is %s, older than %s - the room can't be "
                      "passed on the command line, so %s is on the clipboard for its AP "
                      "dialog instead."
                      % (poptracker_version(path) or "of an unknown version",
                         POPTRACKER_AP_ARGS_MIN_VERSION, hand_over))
            self.clipboard_clear()
            self.clipboard_append(hand_over)
        try:
            # cwd is PopTracker's own folder, like _run_archipelago_exe: CWD\packs is one
            # of its pack search paths, so launching from the launcher's own folder could
            # have it looking for packs somewhere else entirely.
            subprocess.Popen([exe] + args, cwd=path)
        except OSError as exc:
            messagebox.showerror("ARKIpelago Launcher",
                                 "Could not start PopTracker:\n%s\n\n%s" % (exe, exc))
            self._log("! PopTracker failed to start: %s" % exc)
            return
        self._log("Started PopTracker%s" % (" (pack + room pre-filled)" if args else ""))
        # Shown AFTER the launch (PopTracker takes a couple of seconds to appear, so the
        # instructions are on screen while the user waits) and only once per app session:
        # the details are on the clipboard and in the log every time, but a modal box on
        # every click of a button people press repeatedly is a nag, not help.
        if hand_over and not self._poptracker_room_hint_shown:
            self._poptracker_room_hint_shown = True
            messagebox.showinfo("Open PopTracker", note)

    # ------------------------------------------- Archipelago tab: gating ---- #
    def _archipelago_dir(self):
        """The configured Archipelago directory, normalised, or "" when unset. get()
        returns "" while the greyed placeholder is showing, so an unconfigured field
        can never be mistaken for a real install at C:\\ProgramData\\Archipelago."""
        value = self.get(ARCHIPELAGO_DIR_KEY)
        return os.path.normpath(value) if value else ""

    def _refresh_archipelago_buttons(self, _event=None):
        """Enable/disable every button on the tab to match the current directory.

        Each launch button is gated on its OWN exe existing, not just on the folder
        being set, so a partially-broken install disables only the buttons that can't
        work. The reason is put on the button's tooltip so a disabled button always
        explains itself (a disabled ttk button swallows clicks, so a messagebox on
        press would never be seen)."""
        # The directory field is rendered before the buttons exist, and set() fires the
        # trace that calls this - so a partially built tab has to be a no-op, not a crash.
        if not (hasattr(self, "_archipelago_buttons")
                and hasattr(self, "_archipelago_folder_buttons")):
            return
        path = self._archipelago_dir()
        for btn, exe, base_tip in self._archipelago_buttons:
            if not path:
                ok, why = False, "Set the Archipelago directory above first (or click " \
                                 "\"Scan for Archipelago\")."
            elif not os.path.isdir(path):
                ok, why = False, "The Archipelago directory doesn't exist:\n%s" % path
            elif not os.path.isfile(os.path.join(path, exe)):
                ok, why = False, "%s isn't in the Archipelago directory:\n%s" % (exe, path)
            else:
                ok, why = True, ""
            try:
                btn.configure(state=("normal" if ok else "disabled"))
            except tk.TclError:
                pass
            tip = getattr(btn, "_tooltip", None)
            if tip is not None:
                tip.text = base_tip if ok else why + "\n\n" + base_tip
        usable = bool(path) and os.path.isdir(path)
        for btn in self._archipelago_folder_buttons:
            try:
                btn.configure(state=("normal" if usable else "disabled"))
            except tk.TclError:
                pass
        if not hasattr(self, "archipelago_status_label"):
            return
        if not path:
            msg = "No Archipelago directory set."
        elif is_archipelago_dir(path):
            msg = "Archipelago found."
        elif os.path.isdir(path):
            missing = [e for e in ARCHIPELAGO_REQUIRED_EXES
                       if not os.path.isfile(os.path.join(path, e))]
            msg = "Folder found, but missing: %s" % ", ".join(missing)
        else:
            msg = "That folder doesn't exist."
        self.archipelago_status_label.configure(text=msg)

    # ------------------------------------------- directory-field scans ----- #
    #
    # "Scan for Archipelago" and "Scan for PopTracker" are the same scan pointed at two
    # different markers, so they share one implementation: common locations first
    # (synchronous - a handful of os.path.isfile calls), and only if none of them match,
    # ONE budgeted drive walk on a thread. The walker is bounded_drive_scan, the same one
    # the SERVER_ROOT auto-detect uses, with a per-field matcher and a skip-list that
    # doesn't exclude ProgramData/Program Files. Per-field data lives in DIR_SCAN_TARGETS;
    # the widgets come from self._dir_scan_widgets, filled in when each row was built.
    def _on_scan_archipelago(self):
        self._start_dir_scan(ARCHIPELAGO_DIR_KEY)

    def _on_scan_poptracker(self):
        self._start_dir_scan(POPTRACKER_DIR_KEY)

    def _dir_scan_status(self, key, text):
        widgets = self._dir_scan_widgets.get(key)
        if widgets:
            try:
                widgets[2].configure(text=text)
            except tk.TclError:
                pass

    def _start_dir_scan(self, key):
        spec = DIR_SCAN_TARGETS[key]
        label = "Scan for %s" % spec["what"]
        # One scan at a time across BOTH fields: a drive walk is the expensive part, and
        # two of them racing would only make each slower.
        if getattr(self, "_dir_scan_thread", None) is not None \
                and self._dir_scan_thread.is_alive():
            self._log("%s: a scan is already running." % label)
            return
        for cand in spec["candidates"]():
            if spec["matches"](cand):
                self.set(key, os.path.normpath(cand))
                self._log("%s: found %s" % (label, os.path.normpath(cand)))
                return
        self._log("%s: not in the common locations - scanning drives (this can take up "
                  "to ~20s)..." % label)
        self._dir_scan_status(key, "Scanning drives - this can take up to ~20s...")
        self._set_dir_scan_busy(key, True)
        self._dir_scan_queue = queue.Queue()
        self._dir_scan_thread = threading.Thread(
            target=self._dir_scan_worker, args=(key,), daemon=True)
        self._dir_scan_thread.start()
        self.after(150, self._poll_dir_scan_queue, key)

    def _dir_scan_worker(self, key):
        q = self._dir_scan_queue
        try:
            found = bounded_drive_scan(
                lambda line: q.put(("line", line)),
                lambda: False,
                matches=DIR_SCAN_TARGETS[key]["matches"],
                skip_names=SKIP_ARCHIPELAGO_SCAN_DIR_NAMES)
        except Exception as exc:  # a scan must never take the app down with it
            q.put(("error", str(exc)))
            return
        q.put(("result", found))

    def _poll_dir_scan_queue(self, key):
        spec = DIR_SCAN_TARGETS[key]
        label = "Scan for %s" % spec["what"]
        try:
            while True:
                kind, payload = self._dir_scan_queue.get_nowait()
                if kind == "line":
                    self._log(payload)
                elif kind == "error":
                    self._set_dir_scan_busy(key, False)
                    self._dir_scan_thread = None
                    self._log("! %s failed: %s" % (label, payload))
                    self._dir_scan_status(key, "Scan failed - see the log.")
                    return
                elif kind == "result":
                    self._set_dir_scan_busy(key, False)
                    self._dir_scan_thread = None
                    if payload:
                        # set() fires the field's trace, which re-runs that tab's own
                        # button gating and rewrites the status label - no per-field
                        # refresh call needed here.
                        self.set(key, os.path.normpath(payload))
                        self._log("%s: found %s" % (label, os.path.normpath(payload)))
                    else:
                        self._log("%s: nothing found - %s" % (label, spec["missing"]))
                        self._dir_scan_status(key, "Not found - set the folder manually.")
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll_dir_scan_queue, key)

    def _set_dir_scan_busy(self, key, busy):
        widgets = self._dir_scan_widgets.get(key)
        if not widgets:
            return
        btn, progress, _label = widgets
        try:
            btn.configure(state=("disabled" if busy else "normal"))
            if busy:
                progress.pack(side="left", padx=(6, 0))
                progress.start(60)
            else:
                progress.stop()
                progress.pack_forget()
        except tk.TclError:
            pass

    # ------------------------------------------- Archipelago tab: launch --- #
    def _run_archipelago_exe(self, exe, args=(), label=None, new_console=False):
        """Start one of Archipelago's executables, with its own folder as the working
        directory - Archipelago resolves Players/, output/, custom_worlds/ and host.yaml
        relative to cwd, so launching from the launcher's cwd would point it at the
        wrong data. Popen, not run(): these are long-lived GUI apps and must not block
        this app.

        new_console gives the child its own console window instead of inheriting this
        app's (which, as a windowed build, hasn't got one). Only the server needs it:
        it's the one child here that is an interactive console process rather than a
        fire-and-forget GUI - the user has to read its output and type commands like
        /send into it, so it must own a window they can actually reach."""
        label = label or exe
        path = os.path.join(self._archipelago_dir(), exe)
        if not os.path.isfile(path):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "%s was not found in the Archipelago directory:\n%s\n\nSet the "
                "Archipelago directory on the Archipelago Setup tab (or use \"Scan for "
                "Archipelago\")." % (exe, path))
            self._refresh_archipelago_buttons()
            return False
        try:
            subprocess.Popen(
                [path] + list(args), cwd=self._archipelago_dir(),
                creationflags=(getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                               if new_console else 0))
        except OSError as exc:
            messagebox.showerror("ARKIpelago Launcher",
                                 "Could not start %s:\n%s\n\n%s" % (label, path, exc))
            self._log("! %s failed to start: %s" % (label, exc))
            return False
        self._log("Started %s%s" % (label, " (pre-connected)" if args else ""))
        return True

    def _open_text_client(self):
        """Launch the Text Client pre-connected from the server/slot fields.

        Uses --connect/--name/--password (verified present in Archipelago 0.6.7's
        CommonClient base parser) rather than the archipelago://name:pass@host:port
        positional url, because a slot name or password containing ':', '@' or '/'
        would silently corrupt the url form. Blank fields are simply omitted, which
        opens the client unconnected - the client asks for whatever it still needs."""
        args = []
        server = self.get("server")
        slot = self.get("slot")
        password = self.get("password")
        if server:
            args += ["--connect", server]
        if slot:
            args += ["--name", slot]
        if password:
            args += ["--password", password]
        if not server:
            self._log("Text Client: no server set - opening it unconnected.")
        self._run_archipelago_exe(ARCHIPELAGO_TEXT_CLIENT_EXE, args, "Text Client")

    def _open_options_creator(self):
        """Launch the Options Creator. No arguments: it has no command-line interface
        at all (see the block comment above _build_archipelago_tab), so it can't be
        opened directly onto ARK's options page - the user picks the game in its UI."""
        if self._run_archipelago_exe("ArchipelagoOptionsCreator.exe", (), "Options Creator"):
            self._log("  Options Creator can't be opened onto a specific game - pick "
                      "\"ARK: Survival Evolved\" from its game list.")

    def _open_generate(self):
        if self._run_archipelago_exe("ArchipelagoGenerate.exe", (), "Archipelago Generate"):
            self._log("  Generating from the .yaml files in Archipelago's Players "
                      "folder; the finished seed lands in its output folder.")

    def _open_archipelago_launcher(self):
        self._run_archipelago_exe("ArchipelagoLauncher.exe", (), "Archipelago Launcher")

    def _host_local_server(self):
        """Host a room on this machine with ArchipelagoServer.exe.

        What the exe actually accepts was checked against Archipelago 0.6.7 rather than
        assumed (`ArchipelagoServer.exe --help`, plus real launches):

          * It takes the seed as a POSITIONAL argument (`multidata`) - so the room can
            be opened straight onto a generated seed with no "browse for a file" step
            inside the server. Both the .zip that Generate writes and a bare extracted
            .archipelago boot correctly.
          * `--password` sets the room password players connect with; `--server_password`
            is the separate remote-admin password, deliberately not exposed here (it
            isn't needed to play, and it's a footgun to set without meaning to).
          * `--port`/`--host` exist but are NOT passed - see archipelago_host_port().
            host.yaml is the user's own place to change the port, and a CLI --port would
            silently beat it.

        Everything else the parser exposes (--savefile/--disable_save, the SSL --cert
        pair, --release_mode/--collect_mode/--countdown_mode/--remaining_mode,
        --hint_cost, --location_check_points, --disable_item_cheat, --compatibility,
        --loglevel/--logtime/--log_network, --auto_shutdown) is a per-room rules
        preference with a working host.yaml default. Those belong in host.yaml, where
        they persist across every launch, rather than in a wall of widgets on this tab
        that would have to be re-set every time."""
        root = self._archipelago_dir()
        if not root:
            messagebox.showwarning("ARKIpelago Launcher",
                                   "The Archipelago directory is not set.")
            return

        seeds = archipelago_seed_files(root)
        if not seeds:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "No generated seed was found in:\n%s\n\nClick \"Generate seed\" first - "
                "hosting needs a finished seed to open the room onto."
                % os.path.join(root, "output"))
            self._log("Host local server: nothing in the output folder to host.")
            return

        # Newest first, so the common "generate, then host it" path is one click. Cancel
        # aborts entirely; No opens the output folder in a file picker, which doubles as
        # the "list of what's there" for anyone hosting an older seed.
        newest = seeds[0]
        choice = messagebox.askyesnocancel(
            "ARKIpelago Launcher",
            "Host this seed?\n\n%s\n(generated %s)\n\n"
            "Yes - host this one.\nNo - pick a different seed.\nCancel - don't host."
            % (os.path.basename(newest),
               time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(newest)))))
        if choice is None:
            return
        if choice:
            seed = newest
        else:
            seed = filedialog.askopenfilename(
                title="Pick a generated seed to host",
                initialdir=os.path.join(root, "output"),
                filetypes=[("Archipelago seed", "*.zip *.archipelago"),
                           ("All files", "*.*")])
            if not seed:
                return

        args = [seed]
        # The room password and the password the connector will use are the same value,
        # so hosting from this field is what keeps "Copy ARK connection command" correct.
        # Omitted when blank so host.yaml's own password setting still applies.
        password = self.get("password")
        if password:
            args += ["--password", password]

        if not self._run_archipelago_exe(ARCHIPELAGO_SERVER_EXE, args,
                                         "Archipelago Server", new_console=True):
            return

        port = archipelago_host_port(root)
        self._log("Hosting %s in its own console window (port %d)."
                  % (os.path.basename(seed), port))
        self._log("  Other players connect to YOUR IP, not localhost - the server "
                  "window prints the address it's hosting on.")
        self._offer_local_server_address(port)

    def _offer_local_server_address(self, port):
        """Point the Connector `server` field at the room just started locally.

        Without this the tab still says archipelago.gg, so "Copy ARK connection command"
        and "Open Text Client" - both of which read this field - would quietly aim at
        the wrong room. localhost is correct for the HOST's own machine, which is who
        is pressing this button.

        An existing value is never replaced silently: it's usually a real room address
        someone typed, and clobbering it on a button press that didn't advertise itself
        as editing the field would be the kind of thing you only notice much later."""
        local = "localhost:%d" % port
        current = self.get("server")
        if current == local:
            return
        if current:
            if not messagebox.askyesno(
                    "ARKIpelago Launcher",
                    "Point the Connector at your local server?\n\n"
                    "server is currently:\n    %s\n\nReplace it with:\n    %s\n\n"
                    "This is what \"Copy ARK connection command\" and \"Open Text "
                    "Client\" use. Your old value isn't saved anywhere - note it down "
                    "first if you still need it." % (current, local)):
                self._log("  Left server as %s - the local room is at %s."
                          % (current, local))
                return
        self.set("server", local)
        self._log("  server set to %s. Click Save to keep it." % local)

    def _open_archipelago_subfolder(self, subdir, label):
        """Open the Archipelago folder, or one of its subfolders, in Explorer.

        Creates a missing subfolder rather than erroring: Players/output/custom_worlds
        are Archipelago's own well-known folders, and the whole point of the button is
        that the user is about to put a file there - failing because it doesn't exist
        yet would be the least useful possible outcome."""
        root = self._archipelago_dir()
        if not root:
            messagebox.showwarning("ARKIpelago Launcher",
                                   "The Archipelago directory is not set.")
            return
        path = os.path.join(root, subdir) if subdir else root
        if subdir and not os.path.isdir(path):
            try:
                os.makedirs(path, exist_ok=True)
                self._log("Created %s" % path)
            except OSError as exc:
                messagebox.showerror("ARKIpelago Launcher",
                                     "Could not create:\n%s\n\n%s" % (path, exc))
                return
        self._open_folder(path, label)

    def _render_field_groups(self, parent, groups):
        """Render (group title, [(key, label, kind), ...]) groups as LabelFrames of
        labelled fields into `parent`.

        Shared by the Configuration tab (GROUPS) and the Archipelago Setup tab
        (ARCHIPELAGO_GROUPS) so a field looks and behaves identically wherever it is
        rendered. Every field still registers itself in self.vars/self._entries under
        its own key, which is what lets the rest of the app (connector.ini writing,
        profiles, diagnostics, Setup Status) stay completely unaware of which tab a
        field is drawn on. The per-group/per-key special cases below key off the
        title/field name, never off the parent, for the same reason."""
        for title, fields in groups:
            lf = ttk.LabelFrame(parent, text=title, padding=(10, 6))
            lf.pack(fill="x", expand=True, pady=6)
            lf.columnconfigure(0, weight=1)

            if title == "Paths":
                toolrow = ttk.Frame(lf)
                toolrow.pack(fill="x", pady=(0, 6))
                # Scan intensity is picked BEFORE scanning, so the user opts into the
                # slow levels knowingly. Quick is the default and is what a focus-out
                # of SERVER_ROOT always runs, whatever is selected here.
                ttk.Label(toolrow, text="Scan intensity:").pack(side="left", padx=(0, 4))
                self.scan_level_var = tk.StringVar(value=SCAN_LEVEL_DEFAULT)
                self.scan_level_combo = ttk.Combobox(
                    toolrow, textvariable=self.scan_level_var, values=list(SCAN_LEVELS),
                    state="readonly", width=11)
                self.scan_level_combo.pack(side="left")
                Tooltip(self.scan_level_combo,
                        "How hard \"Scan for paths\" looks:\n"
                        "  Quick - %s\n"
                        "  Thorough - %s\n"
                        "  Exhaustive - %s" % (SCAN_LEVEL_HELP[SCAN_QUICK],
                                               SCAN_LEVEL_HELP[SCAN_THOROUGH],
                                               SCAN_LEVEL_HELP[SCAN_EXHAUSTIVE]),
                        wraplength=520)
                # Single entry point for path detection: if SERVER_ROOT isn't set (or
                # doesn't look right) yet, this finds it first (Steam libraries, common
                # drive roots - what used to be the separate "Auto-detect..." button),
                # then automatically scans around it at the chosen intensity to fill in
                # PLUGINS_DIR/ipc_dir/game_ini and suggest cluster folders. If SERVER_ROOT
                # already looks right, it skips straight to that scoped scan.
                self.scan_btn = ttk.Button(toolrow, text="Scan for paths",
                                            command=self._on_scan_button)
                self.scan_btn.pack(side="left", padx=(6, 0))
                Tooltip(self.scan_btn,
                        "Finds your Configuration paths in one click.\n"
                        "If SERVER_ROOT isn't set yet (or doesn't look right), first finds "
                        "it via a best-effort scan of common ARK server install locations "
                        "(Steam libraries, common drive roots) - this part always runs in "
                        "the background and may take a few seconds.\n"
                        "Once SERVER_ROOT is known, fills in the ArkApi Plugins folder, "
                        "ipc_dir and Game.ini from it, and suggests the cluster folders it "
                        "finds, at the intensity selected above. Thorough/Exhaustive search "
                        "recursively (including under ShooterGame\\Saved and next to "
                        "SERVER_ROOT; Exhaustive also checks your Desktop/Documents/"
                        "Downloads) and run in the background - the app stays usable while "
                        "they do.\n"
                        "Leaving the SERVER_ROOT field does a quick version of this "
                        "automatically, so this button is mainly for picking a higher "
                        "intensity or re-running the scan.",
                        wraplength=520)
                # Packed only while a background scan is running (see _set_scan_busy).
                self.scan_progress = ttk.Progressbar(toolrow, mode="indeterminate",
                                                      length=90)
                self.detect_status_label = ttk.Label(toolrow, text="",
                                                       foreground=self.theme["subtle_fg"])
                self.detect_status_label.pack(side="left", padx=8)

                # Sits with CLUSTERDIR / SAVESROOT / BACKUPROOT, the three fields it
                # fills. SteamCMD creates none of this and ARK won't start without
                # CLUSTERDIR, so the fix for a fresh install is to create the folders,
                # not to search harder for ones that never existed (default_cluster_paths).
                crow = ttk.Frame(lf)
                crow.pack(fill="x", pady=(0, 6))
                self.create_cluster_btn = ttk.Button(
                    crow, text="Create %s folders" % CLUSTER_ROOT_DIRNAME,
                    command=self._on_create_cluster_folders)
                self.create_cluster_btn.pack(side="left")
                Tooltip(self.create_cluster_btn,
                        "Creates CLUSTERDIR / SAVESROOT / BACKUPROOT in a \"%s\" folder "
                        "inside SERVER_ROOT and fills the three fields with them.\n"
                        "SteamCMD never creates these, and the ARK server hangs on "
                        "startup with no error when CLUSTERDIR is missing - this runs "
                        "automatically after \"Install ARK Server\", and this button is "
                        "here for installs that predate that, or if the folders get "
                        "moved or deleted.\n"
                        "Existing folders are left untouched." % CLUSTER_ROOT_DIRNAME,
                        wraplength=520)

                self.clear_paths_btn = ttk.Button(
                    crow, text="Clear all paths", command=self._on_clear_all_paths)
                self.clear_paths_btn.pack(side="left", padx=(6, 0))
                Tooltip(self.clear_paths_btn,
                        "Blanks SERVER_ROOT / SAVESROOT / CLUSTERDIR / BACKUPROOT / "
                        "ArkApi Plugins folder / ipc_dir / game_ini back to a fresh, "
                        "never-configured state (same greyed-out example text you'd see "
                        "on a brand new install).\n"
                        "Doesn't touch anything on disk - existing folders/files are left "
                        "exactly as they are. Click Save afterward for the cleared fields "
                        "to actually take effect in the scripts.",
                        wraplength=520)

            for key, label, kind in fields:
                var = tk.StringVar()
                self.vars[key] = var
                row = ttk.Frame(lf)
                row.pack(fill="x", pady=(2, 6))
                row.columnconfigure(0, weight=1)

                if kind == "bool":
                    entry_widget = ttk.Checkbutton(row, text=label, variable=var,
                                                    onvalue="true", offvalue="false")
                    entry_widget.grid(row=0, column=0, sticky="w")
                    label_widget = entry_widget
                else:
                    label_widget = ttk.Label(row, text=label)
                    label_widget.grid(row=0, column=0, columnspan=2, sticky="w")
                    entry_widget = ttk.Entry(row, textvariable=var)
                    entry_widget.grid(row=1, column=0, sticky="ew", padx=(0, 6))
                    if kind in ("folder", "file"):
                        ttk.Button(row, text="Browse...", width=10,
                                   command=lambda k=key, t=kind: self._browse(k, t)
                                   ).grid(row=1, column=1, sticky="e")
                        if key in PATH_GROUP_KEYS:
                            clear_btn = ttk.Button(
                                row, text="C", width=2,
                                command=lambda k=key: self._clear_path_field(k))
                            clear_btn.grid(row=1, column=2, sticky="e", padx=(4, 0))
                            Tooltip(clear_btn, "Clear this field back to blank. Doesn't "
                                    "touch anything on disk - Save afterward to write the "
                                    "cleared value into the scripts.")

                self._entries[key] = entry_widget
                example = (PLACEHOLDER_EXAMPLES.get(key) if kind in ("folder", "file")
                           else CONNECTOR_PLACEHOLDERS.get(key))
                if example:
                    self._register_placeholder(key, entry_widget, example)
                if key == "SERVER_ROOT":
                    entry_widget.bind("<FocusOut>", self._on_server_root_focus_out, add="+")
                if key == "CLUSTERDIR":
                    entry_widget.bind("<FocusOut>", self._on_cluster_dir_focus_out, add="+")

                help_text = FIELD_HELP.get(key)
                if help_text:
                    Tooltip(label_widget, help_text)
                    if entry_widget is not label_widget:
                        Tooltip(entry_widget, help_text)

                if key == "MAP":
                    ttk.Label(row, text="Note: TheIsland is the only officially "
                                        "supported map currently.",
                              foreground=self.theme["note_fg"], font=("Segoe UI", 8, "italic")
                              ).grid(row=2, column=0, columnspan=2, sticky="w",
                                     pady=(2, 0))

                # Sits inside the "Archipelago installation" group, directly under the
                # directory field and its Browse button, so setting the folder and
                # searching for it read as one thing rather than two unrelated controls.
                if key == ARCHIPELAGO_DIR_KEY:
                    self._build_archipelago_scan_row(lf)

                # Same idea, one group down: the PopTracker scan row and the three
                # PopTracker buttons all live in the same LabelFrame as the directory they
                # act on, so the whole feature reads as one thing.
                if key == POPTRACKER_DIR_KEY:
                    self._build_poptracker_controls(lf)

                if key == "password":
                    crow = ttk.Frame(lf)
                    crow.pack(fill="x", pady=(0, 6))
                    copy_btn = ttk.Button(crow, text="Copy ARK connection command",
                                           command=self._copy_connect_command)
                    copy_btn.pack(side="left")
                    Tooltip(copy_btn, "This is the command you'll type in once spawned in "
                            "the server to connect to archipelago.")
                    port_btn = ttk.Button(crow, text="Copy port",
                                           command=self._copy_port)
                    port_btn.pack(side="left", padx=(6, 0))
                    Tooltip(port_btn, "Copies just the port number from the server field "
                            "(the digits after the colon) - handy for the connector and "
                            "for firewall/port-forward entries.")

    def _build_config_upload_section(self, parent):
        """Copy the user's own Game.ini / GameUserSettings.ini over the server's.

        Deliberately separate from the Save flow above: Save does targeted
        line-rewrites of individual settings, whereas this replaces whole files
        wholesale, which is destructive enough to need its own confirm + backup."""
        box = ttk.LabelFrame(parent, text="Upload server config files (Game.ini / "
                                           "GameUserSettings.ini)", padding=(10, 6))
        box.pack(fill="x", expand=True, pady=6)
        ttk.Label(box, wraplength=640, justify="left", foreground=self.theme["subtle_fg"],
                  text="Copies your own copies of these files into "
                       "<SERVER_ROOT>\\%s, replacing the server's. The files being "
                       "replaced are backed up first (timestamped, alongside the "
                       "originals - nothing is deleted). Leave a row blank to leave "
                       "that file alone." % SERVER_CONFIG_RELDIR).pack(anchor="w")

        self._config_upload_vars = {}
        for name in UPLOADABLE_CONFIGS:
            row = ttk.Frame(box)
            row.pack(fill="x", pady=(4, 0))
            row.columnconfigure(0, weight=1)
            ttk.Label(row, text="Your %s:" % name).grid(row=0, column=0, columnspan=2,
                                                         sticky="w")
            var = tk.StringVar()
            self._config_upload_vars[name] = var
            entry = ttk.Entry(row, textvariable=var)
            entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
            ttk.Button(row, text="Browse...", width=10,
                       command=lambda n=name: self._browse_config_upload(n)
                       ).grid(row=1, column=1, sticky="e")
            Tooltip(entry, CONFIG_UPLOAD_HELP[name], wraplength=520)

        btnrow = ttk.Frame(box)
        btnrow.pack(fill="x", pady=(6, 0))
        self.upload_config_btn = ttk.Button(btnrow, text="Upload to server",
                                             command=self.upload_server_configs)
        self.upload_config_btn.pack(side="left")
        Tooltip(self.upload_config_btn,
                "Copy the file(s) above into the server's config folder, overwriting "
                "what's there. You'll be asked to confirm, warned if the ARK server is "
                "running, and the replaced files are backed up with a timestamp first.",
                wraplength=520)
        self.upload_config_status = ttk.Label(btnrow, text="",
                                               foreground=self.theme["subtle_fg"])
        self.upload_config_status.pack(side="left", padx=8)

    def _browse_config_upload(self, name):
        var = self._config_upload_vars[name]
        current = var.get().strip()
        initial = os.path.dirname(current) if current else base_dir()
        path = filedialog.askopenfilename(
            initialdir=initial, title="Select your %s" % name,
            filetypes=[(name, name), ("INI files", "*.ini"), ("All files", "*.*")])
        if path:
            var.set(os.path.normpath(path))

    def _server_config_dir(self):
        """<SERVER_ROOT>\\ShooterGame\\Saved\\Config\\WindowsServer, or "" if
        SERVER_ROOT isn't set."""
        root = self.get("SERVER_ROOT")
        if not root:
            return ""
        return os.path.join(os.path.normpath(root), SERVER_CONFIG_RELDIR)

    def upload_server_configs(self):
        dest_dir = self._server_config_dir()
        if not dest_dir:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "Set SERVER_ROOT on this tab first - that's what says where the "
                "server's config folder is.")
            return

        # Collect + validate the sources before touching anything on disk.
        sources = {}
        for name, var in self._config_upload_vars.items():
            path = var.get().strip()
            if not path:
                continue
            if not os.path.isfile(path):
                messagebox.showerror(
                    "ARKIpelago Launcher",
                    "The %s you selected doesn't exist:\n\n%s" % (name, path))
                return
            dest = os.path.join(dest_dir, name)
            if os.path.exists(dest) and os.path.normcase(os.path.abspath(path)) == \
                    os.path.normcase(os.path.abspath(dest)):
                messagebox.showwarning(
                    "ARKIpelago Launcher",
                    "Your %s IS the server's own copy:\n\n%s\n\nThere's nothing to "
                    "upload - pick a file from somewhere else." % (name, path))
                return
            sources[name] = path
        if not sources:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "Pick at least one file to upload (Game.ini and/or "
                "GameUserSettings.ini).")
            return

        if not os.path.isdir(dest_dir):
            if not messagebox.askyesno(
                    "ARKIpelago Launcher",
                    "The server's config folder doesn't exist yet:\n\n%s\n\nThis "
                    "normally means the server has never been started. Create the "
                    "folder and copy the file(s) in anyway?" % dest_dir):
                self._log("Upload server config: cancelled.")
                return

        # Same destructive-action gate the resets use: ARK rewrites
        # GameUserSettings.ini (and can rewrite Game.ini) when it shuts down, so
        # anything copied in while it runs is silently lost on stop.
        if is_process_running(ARK_SERVER_PROCESS):
            if not messagebox.askyesno(
                    "ARKIpelago Launcher",
                    "%s is currently running.\n\nARK rewrites its config files (in "
                    "particular GameUserSettings.ini) when the server shuts down, so "
                    "files copied in now will most likely be overwritten and lost, and "
                    "the running server won't pick them up either. Stop the server "
                    "first.\n\nUpload anyway?" % ARK_SERVER_PROCESS):
                self._log("Upload server config: cancelled (ARK server running).")
                return
            self._log("! Upload server config: proceeding while %s is running - the "
                      "server may overwrite these files on shutdown."
                      % ARK_SERVER_PROCESS)

        existing = [n for n in sources if os.path.isfile(os.path.join(dest_dir, n))]
        detail = "\n".join("  %s  <-  %s" % (n, sources[n]) for n in sources)
        msg = ("Copy these file(s) into:\n\n%s\n\n%s\n\n" % (dest_dir, detail))
        if existing:
            msg += ("The server's current %s will be OVERWRITTEN. A timestamped backup "
                    "of each is saved next to it first (e.g. Game.ini.20260101-120000."
                    "bak) - nothing is deleted.\n\n" % " and ".join(existing))
        msg += "Proceed?"
        if not messagebox.askyesno("Upload server config files", msg):
            self._log("Upload server config: cancelled.")
            return

        self._clear_log()
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("ARKIpelago Launcher",
                                  "Could not create the config folder:\n\n%s" % exc)
            self._log("! Upload server config: could not create %s: %s" % (dest_dir, exc))
            return

        ts = time.strftime("%Y%m%d-%H%M%S")
        copied, errors = [], []
        for name, src in sources.items():
            dest = os.path.join(dest_dir, name)
            # Back up FIRST - if that fails, this file is skipped rather than
            # overwritten with no way back.
            if os.path.isfile(dest):
                backup = "%s.%s.bak" % (dest, ts)
                # Two uploads inside the same second would otherwise land on the
                # same backup name and silently destroy the first one.
                dupe = 2
                while os.path.exists(backup):
                    backup = "%s.%s-%d.bak" % (dest, ts, dupe)
                    dupe += 1
                try:
                    shutil.copy2(dest, backup)
                    self._log("Backed up %s -> %s" % (dest, os.path.basename(backup)))
                except OSError as exc:
                    errors.append("%s: backup failed (%s) - not overwritten" % (name, exc))
                    continue
            try:
                shutil.copy2(src, dest)
                copied.append(name)
                self._log("Uploaded %s -> %s" % (src, dest))
            except OSError as exc:
                errors.append("%s: copy failed (%s)" % (name, exc))

        # Point game_ini at the file just uploaded, so the connector patches the copy
        # that's actually live rather than a stale path.
        if "Game.ini" in copied and not self.get("game_ini"):
            self.set("game_ini", os.path.join(dest_dir, "Game.ini"))
            self._log("Set game_ini to the uploaded file - press Save to write it into "
                      "connector.ini.")

        for err in errors:
            self._log("! Upload server config: %s" % err)
        self.upload_config_status.configure(
            text="Uploaded %s." % ", ".join(copied) if copied else "Nothing uploaded.")
        if copied and not errors:
            messagebox.showinfo(
                "ARKIpelago Launcher",
                "Uploaded %s into:\n\n%s\n\nThe previous version of each is kept "
                "alongside as a .%s.bak file.\n\nRestart the ARK server for the new "
                "settings to take effect." % (", ".join(copied), dest_dir, ts))
        elif errors:
            messagebox.showerror(
                "ARKIpelago Launcher",
                "Some files could not be uploaded:\n\n%s\n\nSee the log for details."
                % "\n".join(errors))

    def _build_install_tab(self, parent):
        # Scrollable container - same Canvas + Scrollbar pattern as the Configuration
        # and Setup Status tabs, since three installers plus the manual-downloads
        # section grow past shorter windows. `wrap` stays the parent of every row below,
        # it's just now the canvas's inner frame instead of packed straight into `parent`.
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0,
                           background=self.theme["bg"])
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        wrap = ttk.Frame(canvas, padding=(10, 8))
        inner_id = canvas.create_window((0, 0), window=wrap, anchor="nw")
        wrap.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))

        def _on_wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))

        # Install location -------------------------------------------------------
        loc = ttk.LabelFrame(wrap, text="Install Location", padding=(8, 6))
        loc.pack(fill="x")
        ttk.Label(loc, text="SERVER_ROOT - the folder SteamCMD installs the ARK "
                            "dedicated server into (this is the same SERVER_ROOT used "
                            "on the Configuration tab).").pack(anchor="w")
        locrow = ttk.Frame(loc)
        locrow.pack(fill="x", pady=(4, 0))
        locrow.columnconfigure(0, weight=1)
        loc_entry = ttk.Entry(locrow, textvariable=self.vars["SERVER_ROOT"])
        loc_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        # Shares SERVER_ROOT's variable with the Configuration tab, so it must share the
        # placeholder handling too - otherwise the example text sits here as ordinary
        # text, and anything typed into it is thrown away by get() (see the placeholder
        # section's invariant 1).
        self._register_placeholder("SERVER_ROOT", loc_entry,
                                    PLACEHOLDER_EXAMPLES["SERVER_ROOT"])
        loc_entry.bind("<FocusOut>", self._on_server_root_focus_out, add="+")
        ttk.Button(locrow, text="Browse...", width=10,
                   command=lambda: self._browse("SERVER_ROOT", "folder")
                   ).grid(row=0, column=1, sticky="e")
        help_text = FIELD_HELP.get("SERVER_ROOT")
        if help_text:
            Tooltip(loc_entry, help_text)

        # Install ARK Server (SteamCMD) ------------------------------------------
        inst = ttk.LabelFrame(wrap, text="Install ARK Server (SteamCMD)", padding=(8, 6))
        inst.pack(fill="both", expand=True, pady=(8, 0))
        irow = ttk.Frame(inst)
        irow.pack(fill="x")
        self.install_btn = ttk.Button(irow, text="Install ARK Server",
                                       command=self.on_install_server)
        self.install_btn.pack(side="left", padx=3, pady=2)
        Tooltip(self.install_btn, INSTALL_BTN_HELP, wraplength=620)
        self.install_cancel_btn = ttk.Button(irow, text="Cancel", state="disabled",
                                              command=self.on_cancel_install)
        self.install_cancel_btn.pack(side="left", padx=3, pady=2)
        self.arkapi_install_btn = ttk.Button(irow, text="Install ArkServerApi",
                                              command=self.on_install_arkapi)
        self.arkapi_install_btn.pack(side="left", padx=3, pady=2)
        Tooltip(self.arkapi_install_btn, INSTALL_ARKAPI_BTN_HELP, wraplength=620)

        prow = ttk.Frame(inst)
        prow.pack(fill="x", pady=(4, 0))
        self.install_progress = ttk.Progressbar(prow, mode="indeterminate")
        self.install_progress.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.install_status_var = tk.StringVar(value="Idle")
        ttk.Label(prow, textvariable=self.install_status_var, width=12, anchor="w"
                  ).pack(side="left")

        ilogrow = ttk.Frame(inst)
        ilogrow.pack(fill="both", expand=True, pady=(6, 0))
        self.install_log = tk.Text(ilogrow, height=14, wrap="word", state="disabled",
                                    font=("Consolas", 9),
                                    background=self.theme["text_bg"],
                                    foreground=self.theme["text_fg"],
                                    insertbackground=self.theme["text_fg"])
        Tooltip(self.install_log, INSTALL_BTN_HELP, wraplength=620)
        install_vsb = ttk.Scrollbar(ilogrow, orient="vertical",
                                     command=self.install_log.yview)
        self.install_log.configure(yscrollcommand=install_vsb.set)
        self.install_log.pack(side="left", fill="both", expand=True)
        install_vsb.pack(side="right", fill="y")

        # Install ArkAP Plugin ---------------------------------------------------
        # Copies the plugin natively (no console, preserves ArkAP.config.json on upgrade)
        # from the user's unzipped ArkAP_plugin download into <PLUGINS_DIR>\ArkAP. The
        # plugin payload is deliberately NOT bundled in this exe, so it keeps working when
        # the plugin is updated independently - the user points at the download once.
        plug = ttk.LabelFrame(wrap, text="Install/update ArkAP Plugin", padding=(8, 6))
        plug.pack(fill="x", pady=(8, 0))
        ttk.Label(plug, wraplength=640, justify="left",
                  text="Downloads the latest ArkAP plugin from GitHub and installs it into "
                       "<SERVER_ROOT>\\ShooterGame\\Binaries\\Win64\\ArkApi\\Plugins\\ArkAP - "
                       "no manual download/unzip needed. ArkApi must already be installed in "
                       "Win64 first. An existing ArkAP.config.json is kept on upgrade. If the "
                       "automated download ever fails, or you want a specific older version, "
                       "use \"Manual downloads\" below."
                  ).pack(anchor="w")
        pbtnrow = ttk.Frame(plug)
        pbtnrow.pack(fill="x", pady=(6, 0))
        self.install_plugin_btn = ttk.Button(pbtnrow, text="Install Plugin",
                                              command=self.on_install_plugin)
        self.install_plugin_btn.pack(side="left", padx=3, pady=2)
        Tooltip(self.install_plugin_btn,
                "Download the latest ArkAP_Plugin.zip from GitHub and install it into the "
                "ArkApi Plugins folder under SERVER_ROOT. Requires SERVER_ROOT set and ArkApi "
                "already installed in Win64. Your ArkAP.config.json is kept on upgrade.")

        # Manual downloads (ArkConnector still isn't automated) -------------------
        ext = ttk.LabelFrame(wrap, text="Manual downloads (not automated but you'll need the YAML and .apworld from here also should only be used if something goes wrong in the launcher)", padding=(8, 6))
        ext.pack(fill="x", pady=(8, 0))
        ttk.Label(ext, foreground=self.theme["subtle_fg"], wraplength=640, justify="left",
                  text="The ArkAP plugin (via Install Plugin above) and ArkServerApi (via "
                       "Install ArkServerApi above) can be installed automatically now. "
                       "Its not required, but if you have connection issues"
                       "The ArkConnector still needs a manual download/run - grab it from "
                       "the releases page below.").pack(anchor="w")
        ttk.Button(ext, text="Open Releases Page",
                   command=lambda: webbrowser.open(RELEASES_URL)).pack(anchor="w", pady=(4, 0))

    # ------------------------------------------------------------ Mods tab --- #
    def _build_mods_tab(self, parent):
        wrap = ttk.Frame(parent, padding=(10, 8))
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="Mods", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(wrap, foreground=self.theme["subtle_fg"], wraplength=640, justify="left",
                  text=MODS_TAB_HELP).pack(anchor="w", pady=(2, 6))

        # Prerequisite banner - shown/hidden by _refresh_mods_tab, same idea as the
        # Configuration tab's install reminder (reminder_banner). Left unpacked here;
        # the Mods tab is populated lazily on first visit (see _on_tab_changed), same
        # as Setup Status, so there's nothing worth deciding about it until then.
        self.mods_gate_banner = tk.Frame(wrap, background=self.theme["warn_bg"],
                                          highlightbackground=self.theme["warn_border"],
                                          highlightthickness=1)
        tk.Label(self.mods_gate_banner, background=self.theme["warn_bg"],
                 foreground=self.theme["warn_fg"], justify="left", wraplength=460,
                 text="Set SERVER_ROOT and install the ARK server first - mods install "
                      "into SERVER_ROOT's Content\\Mods folder."
                 ).pack(side="left", fill="x", expand=True, padx=8, pady=6)
        ttk.Button(self.mods_gate_banner, text="Go to Install Server/Api/Plugin",
                   command=self._goto_install_tab).pack(side="right", padx=6, pady=4)

        top = ttk.Frame(wrap)
        top.pack(fill="both", expand=True, pady=(6, 0))
        self._mods_top_frame = top  # anchor mods_gate_banner packs before this

        # Above the list, left side: bulk-check the boxes only (same as clicking each
        # one individually via _on_mod_toggle) - no install/uninstall/activation happens
        # here. Save or Download checked is still what applies it afterwards.
        checkrow = ttk.Frame(wrap)
        checkrow.pack(fill="x", anchor="w", before=top)
        self.mods_check_all_btn = ttk.Button(checkrow, text="Check all",
                                              command=self.on_mods_check_all)
        self.mods_check_all_btn.pack(side="left", padx=(0, 4))
        Tooltip(self.mods_check_all_btn,
                "Ticks every mod's checkbox (verified and unsupported alike). GUI only - "
                "same as ticking each box by hand. Press Download checked and/or Save "
                "afterwards to actually apply it.")
        self._mods_action_buttons.append(self.mods_check_all_btn)
        self.mods_uncheck_all_btn = ttk.Button(checkrow, text="Uncheck all",
                                                command=self.on_mods_uncheck_all)
        self.mods_uncheck_all_btn.pack(side="left")
        Tooltip(self.mods_uncheck_all_btn,
                "Unticks every mod's checkbox. GUI only - same as unticking each box by "
                "hand. Press Save afterwards to actually apply it.")
        self._mods_action_buttons.append(self.mods_uncheck_all_btn)

        # Top-left: scrollable, ordered mod list - same Canvas + Scrollbar pattern as
        # the Configuration, Install Server/Api/Plugin and Setup Status tabs.
        list_outer = ttk.Frame(top)
        list_outer.pack(side="left", fill="both", expand=True)
        canvas = tk.Canvas(list_outer, borderwidth=0, highlightthickness=0,
                           background=self.theme["bg"])
        vsb = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.mods_items_frame = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=self.mods_items_frame, anchor="nw")
        self.mods_items_frame.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))

        def _on_wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))

        # Top-right: slim action button column.
        btns = ttk.Frame(top, padding=(8, 0))
        btns.pack(side="left", fill="y")

        def _add_btn(text, command, tooltip=None, gated=True):
            b = ttk.Button(btns, text=text, command=command, width=20)
            b.pack(fill="x", pady=2)
            if tooltip:
                Tooltip(b, tooltip, wraplength=280)
            if gated:
                self._mods_action_buttons.append(b)
            return b

        # Save applies the checkbox state above to disk right now - the same
        # warn_bg/warn_border halo as the Configuration tab's Save button (see
        # save_btn_halo), so "you have unapplied changes here" reads the same way in
        # both places. Checking/unchecking a mod (individually, or in bulk via Check
        # all/Uncheck all - see _on_mod_toggle/on_mods_check_all/on_mods_uncheck_all)
        # only ever edits in-memory intent - Save (or Download checked) is what
        # actually writes ActiveMods.
        self.mods_save_btn_halo = tk.Frame(btns, background=self.theme["bg"],
                                           highlightbackground=self.theme["bg"],
                                           highlightthickness=1)
        self.mods_save_btn_halo.pack(fill="x", pady=2)
        self.mods_save_btn = ttk.Button(self.mods_save_btn_halo, text="Save",
                                        command=self.on_mods_save)
        self.mods_save_btn.pack(fill="x", padx=2, pady=2)
        Tooltip(self.mods_save_btn,
                "Apply the checkboxes above to GameUserSettings.ini's ActiveMods right "
                "now, in this list's order (checked + actually installed = active, "
                "everything else = inactive). A checked mod that isn't installed yet is "
                "left inactive and noted in the log - Download checked installs it "
                "first. Restart the ARK server afterwards for the change to take "
                "effect.", wraplength=280)
        self._mods_action_buttons.append(self.mods_save_btn)

        self.mods_download_btn = _add_btn(
            "Download checked", self.on_mods_download_checked,
            "Runs SteamCMD's workshop download for every checked mod not yet "
            "installed, then activates it. Not wired up yet (coming in a later step).")
        self.mods_add_btn = _add_btn(
            "Add mod...", self.on_mods_add,
            "Add a raw Workshop mod ID not in the supported list above.", gated=False)
        # Not gated - a rename is pure display state in the config, so it works with no
        # server set. Enabled/disabled by selection in _update_mods_button_states.
        self.mods_rename_btn = _add_btn(
            "Rename mod...", self.on_mods_rename,
            "Give the selected user-added mod a readable name instead of its raw "
            "Workshop ID. Cosmetic only - the ID, install state, load order and "
            "activation are unchanged, and the mod is still left out of the YAML copy "
            "if it's unsupported. Supported mods keep their apworld names and can't be "
            "renamed.", gated=False)
        self.mods_uninstall_btn = _add_btn(
            "Uninstall unchecked", self.on_mods_uninstall_unchecked,
            "Deletes the installed files for every mod that's currently unchecked, "
            "freeing disk space. Not wired up yet (coming in a later step).")
        self.mods_verify_btn = _add_btn(
            "Verify/Redownload", self.on_mods_verify_redownload,
            "Re-runs the Workshop download for the selected mod, in case it's "
            "corrupted or partially downloaded. Not wired up yet (coming in a later step).")
        self.mods_workshop_btn = _add_btn(
            "Open Workshop page", self.on_mods_open_workshop_page,
            "Opens the selected mod's Steam Workshop page in your browser.",
            gated=False)
        # Copy the checked IDs for pasting into the plugin's YAML. Not gated - it only
        # reads the checkbox state, and prepping a YAML doesn't require a server yet.
        self.mods_copy_ids_btn = _add_btn(
            "Copy IDs for YAML", self.on_mods_copy_active_ids,
            "Copy the IDs of every checked mod (in this list's top-to-bottom order) to "
            "the clipboard as a comma-separated list, ready to paste into the plugin's "
            "YAML mod configuration.\n"
            "Only mods tagged \"apworld ✓\" are copied: the apworld rejects any ID it "
            "doesn't ship engram data for, so a user-added ID in mod_ids fails generation "
            "outright. Anything left out is named when you copy - those mods still "
            "install and load on the server as normal.",
            gated=False)
        # Manual refresh - re-reads real install + active state from disk. Not gated: it
        # must work even with no server set (to show the all-red/unchecked truth).
        self.mods_refresh_btn = _add_btn(
            "Refresh", self._refresh_mods_tab,
            "Re-check every mod's real installed (icon) and active (checkbox) state "
            "against disk.", gated=False)

        # Bottom: streamed output log - a separate widget from install_log (Server
        # Install tab) so the two flows don't visually interleave. Once a real mod
        # install worker exists (Phase 1) it must still register in
        # _any_install_running() so it can't race the other installers over the same
        # SERVER_ROOT, even though it logs here instead of to install_log.
        logframe = ttk.LabelFrame(wrap, text="Output", padding=(6, 4))
        logframe.pack(fill="both", pady=(8, 0))
        logrow = ttk.Frame(logframe)
        logrow.pack(fill="both", expand=True)
        self.mods_log = tk.Text(logrow, height=10, wrap="word", state="disabled",
                                font=("Consolas", 9),
                                background=self.theme["text_bg"], foreground=self.theme["text_fg"],
                                insertbackground=self.theme["text_fg"])
        mods_vsb = ttk.Scrollbar(logrow, orient="vertical", command=self.mods_log.yview)
        self.mods_log.configure(yscrollcommand=mods_vsb.set)
        self.mods_log.pack(side="left", fill="both", expand=True)
        mods_vsb.pack(side="right", fill="y")

    def _mods_log_line(self, line):
        self.mods_log.configure(state="normal")
        self.mods_log.insert("end", line.rstrip("\n") + "\n")
        self.mods_log.see("end")
        self.mods_log.configure(state="disabled")
        launcher_log(line, "Mods")  # downloads, installs, ActiveMods writes

    def _mods_gate_state(self):
        """(ok, server_root) - ok only if SERVER_ROOT is set AND the ARK server is
        installed there. Reuses check_ark_server_installed rather than a second rule,
        so this tab can't disagree with Setup Status about "is it installed"."""
        server_root = self.get("SERVER_ROOT")
        ok, _detail = check_ark_server_installed(server_root)
        return ok, server_root

    def _refresh_mods_tab(self):
        ok, _server_root = self._mods_gate_state()
        self.mods_gate_banner.pack_forget()
        if not ok:
            self.mods_gate_banner.pack(fill="x", pady=(0, 6), before=self._mods_top_frame)
        self._refresh_mods_list()  # also updates button enabled-state

    def _refresh_mods_list(self):
        """Rebuild every row from REAL disk state: checkbox = is the mod in
        GameUserSettings.ini's ActiveMods (read_active_mods), icon = is_mod_installed.
        This is the "re-verify against disk" path - called on tab load, manual Refresh,
        reorder, and add. In-memory `enabled` is synced to the on-disk active state so it
        never drifts from what the server will actually load."""
        gate_ok, server_root = self._mods_gate_state()
        # Authoritative active list from disk (empty when gated - nothing to read).
        active_ids = set(read_active_mods(server_root)) if gate_ok else set()
        for mod in self._mods:
            mod["enabled"] = mod["id"] in active_ids  # in-memory follows disk truth
        self._rebuild_mods_rows()

    def _rebuild_mods_rows(self):
        """Redraw rows from whatever `enabled` currently sits in-memory, without
        re-reading disk - used after a pure GUI-intent edit (Check all/Uncheck all)
        that must not be clobbered by _refresh_mods_list's disk sync."""
        for child in self.mods_items_frame.winfo_children():
            child.destroy()
        gate_ok, server_root = self._mods_gate_state()
        for idx, mod in enumerate(self._mods):
            self._build_mod_row(idx, mod, server_root, gate_ok)
        self._update_mods_button_states()

    def _build_mod_row(self, idx, mod, server_root, gate_ok):
        selected = (mod["id"] == self._mods_selected_id)
        row_bg = self.theme["tab_active_bg"] if selected else self.theme["bg"]
        row = tk.Frame(self.mods_items_frame, background=row_bg)
        row.pack(fill="x", pady=1)

        # When gated (no SERVER_ROOT / server not installed) the whole row is read-only.
        interactive = "normal" if gate_ok else "disabled"

        reorder = ttk.Frame(row)
        reorder.pack(side="left", padx=(0, 4))
        up = ttk.Button(reorder, text="▲", width=2,
                         command=lambda i=idx: self._move_mod(i, -1))
        up.pack()
        down = ttk.Button(reorder, text="▼", width=2,
                           command=lambda i=idx: self._move_mod(i, 1))
        down.pack()
        up.configure(state="disabled" if (not gate_ok or idx == 0) else "normal")
        down.configure(
            state="disabled" if (not gate_ok or idx == len(self._mods) - 1) else "normal")

        var = tk.BooleanVar(value=bool(mod.get("enabled")))
        cb = ttk.Checkbutton(row, variable=var, state=interactive,
                              command=lambda m=mod, v=var: self._on_mod_toggle(m, v))
        cb.pack(side="left", padx=(0, 4))

        ok, _detail = check_mod_installed(server_root, mod["id"])
        icon = self.STATUS_ICONS["ok"] if ok else self.STATUS_ICONS["fail"]
        color = self.theme["status_ok"] if ok else self.theme["status_fail"]
        tk.Label(row, text=icon, background=row_bg, foreground=color, width=2
                 ).pack(side="left", padx=(0, 6))

        # Apworld support, stated for BOTH states rather than tagging only the odd one out:
        # an absent tag reads as "nothing special here", which is the wrong impression for
        # exactly the mods "Copy IDs for YAML" leaves out of mod_ids. Sits next to the
        # install icon so a glance down the list separates the two questions - is it on
        # disk, and can it go in the YAML.
        supported = mod.get("supported", True)
        tag = "apworld %s" % self.STATUS_ICONS["ok" if supported else "fail"]
        tk.Label(row, text=tag, background=row_bg,
                 foreground=self.theme["status_ok"] if supported
                 else self.theme["status_info"],
                 width=10, anchor="w").pack(side="left", padx=(0, 6))

        name_lbl = tk.Label(row, text=mod["name"], background=row_bg, anchor="w",
                             foreground=self.theme["fg"] if supported
                             else self.theme["status_info"])
        name_lbl.pack(side="left", fill="x", expand=True)

        id_lbl = tk.Label(row, text=mod["id"], background=row_bg,
                           foreground=self.theme["subtle_fg"], width=12, anchor="e")
        id_lbl.pack(side="left", padx=(6, 4))

        def _select(_e=None, mod_id=mod["id"]):
            self._mods_selected_id = mod_id
            self._refresh_mods_list()
        for w in (row, name_lbl, id_lbl):
            w.bind("<Button-1>", _select)

    def _update_mods_button_states(self):
        gate_ok, _server_root = self._mods_gate_state()
        busy = self._mods_thread is not None and self._mods_thread.is_alive()
        enabled = gate_ok and not busy
        has_selection = any(m["id"] == self._mods_selected_id for m in self._mods)
        for b in self._mods_action_buttons:
            b.configure(state="normal" if enabled else "disabled")
        self.mods_workshop_btn.configure(
            state="normal" if (has_selection and not busy) else "disabled")
        # Same selection-only rule: left clickable for a supported mod so the handler can
        # explain why that one can't be renamed, rather than greying out silently.
        self.mods_rename_btn.configure(
            state="normal" if (has_selection and not busy) else "disabled")
        self.mods_verify_btn.configure(
            state="normal" if (enabled and has_selection) else "disabled")
        # Same rule as the other two Save buttons - lit only while there's something
        # unapplied. This runs from _rebuild_mods_rows, i.e. after every toggle, Check
        # all/Uncheck all, reorder, add, Refresh and Save. Cached in _mods_dirty_flag so
        # the header hint can reuse the verdict instead of re-reading the .ini.
        self._mods_dirty_flag = self._mods_dirty()
        self._set_halo(self.mods_save_btn_halo, self._mods_dirty_flag)
        self._update_save_hint()

    def _mods_dirty(self):
        """True when the checkboxes, as Save would apply them (checked AND installed, in
        list order), don't match GameUserSettings.ini's ActiveMods.

        Compared against disk rather than a remembered snapshot because that's the same
        pair on_mods_save writes, so a toggle, a reorder, a Refresh, an external edit and
        a Save all resolve correctly with no extra bookkeeping. Never dirty while gated -
        Save is disabled then, and there is nothing to write."""
        gate_ok, server_root = self._mods_gate_state()
        if not gate_ok:
            return False
        want = [m["id"] for m in self._mods
                if m.get("enabled") and check_mod_installed(server_root, m["id"])[0]]
        return want != read_active_mods(server_root)

    def _move_mod(self, idx, direction):
        new_idx = idx + direction
        if not (0 <= new_idx < len(self._mods)):
            return
        self._mods[idx], self._mods[new_idx] = self._mods[new_idx], self._mods[idx]
        self._save_mods_config()
        self._refresh_mods_list()

    def _on_mod_toggle(self, mod, var):
        # GUI-intent only - this does NOT touch the server yet. The change is applied to
        # disk (download / ActiveMods) by the action buttons later; a Refresh/tab-reload
        # re-reads the real on-disk state and discards un-applied toggles.
        mod["enabled"] = bool(var.get())
        self._mods_log_line("%s: %s (pending - click Save to apply)"
                            % (mod["name"], "checked" if mod["enabled"] else "unchecked"))
        # The one mutation that doesn't redraw the rows (the checkbox already shows the new
        # state), so it has to re-run the dirty check itself or the Save halo never lights.
        self._update_mods_button_states()

    def on_mods_add(self):
        win = tk.Toplevel(self)
        win.title("Add mod")
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Steam Workshop mod ID:").pack(anchor="w")
        id_var = tk.StringVar()
        entry = ttk.Entry(frm, textvariable=id_var, width=24)
        entry.pack(anchor="w", pady=(2, 8))
        entry.focus_set()

        warn = tk.Frame(frm, background=self.theme["warn_bg"],
                         highlightbackground=self.theme["warn_border"], highlightthickness=1)
        warn.pack(fill="x", pady=(0, 8))
        tk.Label(warn, background=self.theme["warn_bg"], foreground=self.theme["warn_fg"],
                 justify="left", wraplength=340,
                 text="Not verified as supported by the ArkAP plugin. Only the mods in "
                      "the pre-populated list are known to integrate with checks/items - "
                      "an unsupported mod may just work as a normal ARK mod with no "
                      "Archipelago integration."
                 ).pack(padx=8, pady=6)

        def _submit():
            mod_id = id_var.get().strip()
            if not mod_id.isdigit():
                messagebox.showwarning("Add mod", "Enter a numeric Steam Workshop ID.")
                return
            if any(m["id"] == mod_id for m in self._mods):
                messagebox.showinfo("Add mod", "That mod ID is already in the list.")
                win.destroy()
                return
            self._mods.append({"id": mod_id, "name": "Workshop Mod %s" % mod_id,
                               "enabled": False, "supported": False})
            self._save_mods_config()
            self._mods_log_line("Added unsupported mod %s to the list." % mod_id)
            win.destroy()
            self._refresh_mods_list()

        btnrow = ttk.Frame(frm)
        btnrow.pack(fill="x")
        ttk.Button(btnrow, text="Add", command=_submit).pack(side="right", padx=(4, 0))
        ttk.Button(btnrow, text="Cancel", command=win.destroy).pack(side="right")
        entry.bind("<Return>", lambda _e: _submit())

    def _mods_gate_warn(self):
        messagebox.showwarning("ARKIpelago Launcher",
                               "Set SERVER_ROOT and install the ARK server first.")

    def on_mods_save(self):
        """Apply the checkboxes exactly as they currently sit to GameUserSettings.ini's
        ActiveMods - the one on-disk file mod activation lives in (confirmed in Phase 0;
        there is no second file to keep in sync). Check all/Uncheck all only edit the
        checkboxes themselves (see on_mods_check_all/on_mods_uncheck_all) - this is what
        actually writes whatever mixture of checked/unchecked the list is showing now."""
        if self._any_install_running():
            messagebox.showinfo("ARKIpelago Launcher",
                                "A mod install is running - wait for it to finish.")
            return
        gate_ok, server_root = self._mods_gate_state()
        if not gate_ok:
            self._mods_gate_warn()
            return
        checked = [m for m in self._mods if m.get("enabled")]
        active = [m for m in checked if check_mod_installed(server_root, m["id"])[0]]
        not_installed = [m for m in checked if m not in active]
        ok, msg = set_active_mods(server_root, [m["id"] for m in active])
        self._mods_log_line(msg if ok else "! " + msg)
        if not_installed:
            self._mods_log_line(
                "! Checked but not installed, left inactive - use \"Download checked\" "
                "first: %s" % ", ".join("%s [%s]" % (m["name"], m["id"])
                                        for m in not_installed))
        if ok:
            self._mods_log_line("Restart the ARK server for the change to take effect.")
        self._refresh_mods_list()

    def on_mods_check_all(self):
        # GUI-intent only, same as ticking every box by hand (_on_mod_toggle) - does not
        # touch ActiveMods. Save (or Download checked) is what applies it.
        for mod in self._mods:
            mod["enabled"] = True
        self._mods_log_line("Checked every mod (pending - click Save or Download checked "
                            "to apply).")
        self._rebuild_mods_rows()

    def on_mods_uncheck_all(self):
        for mod in self._mods:
            mod["enabled"] = False
        self._mods_log_line("Unchecked every mod (pending - click Save to apply).")
        self._rebuild_mods_rows()

    def on_mods_download_checked(self):
        if self._any_install_running():
            messagebox.showinfo("ARKIpelago Launcher",
                                "An install is already running - wait for it to finish.")
            return
        gate_ok, server_root = self._mods_gate_state()
        if not gate_ok:
            self._mods_gate_warn()
            return
        checked = [m for m in self._mods if m.get("enabled")]
        if not checked:
            self._mods_log_line("No mods are checked. Tick the mods you want, then "
                                "Download checked.")
            return
        pending = [m for m in checked if not check_mod_installed(server_root, m["id"])[0]]
        if not pending:
            # All checked mods are already installed - just (re)apply activation.
            ok, msg = set_active_mods(server_root, [m["id"] for m in checked])
            self._mods_log_line("Every checked mod is already installed.")
            self._mods_log_line(msg if ok else "! " + msg)
            self._refresh_mods_list()
            return
        self._start_mods_worker(server_root, pending, activate=checked, force=False,
                                title="Download checked")

    def on_mods_verify_redownload(self):
        if self._any_install_running():
            messagebox.showinfo("ARKIpelago Launcher",
                                "An install is already running - wait for it to finish.")
            return
        gate_ok, server_root = self._mods_gate_state()
        if not gate_ok:
            self._mods_gate_warn()
            return
        mod = next((m for m in self._mods if m["id"] == self._mods_selected_id), None)
        if not mod:
            messagebox.showinfo("ARKIpelago Launcher", "Select a mod in the list first.")
            return
        # Force a fresh re-download of just this mod; leave the active list unchanged.
        self._start_mods_worker(server_root, [mod], activate=None, force=True,
                                title="Verify / re-download")

    def on_mods_uninstall_unchecked(self):
        if self._any_install_running():
            messagebox.showinfo("ARKIpelago Launcher",
                                "A mod install is running - wait for it to finish.")
            return
        gate_ok, server_root = self._mods_gate_state()
        if not gate_ok:
            self._mods_gate_warn()
            return
        targets = [m for m in self._mods if not m.get("enabled")
                   and check_mod_installed(server_root, m["id"])[0]]
        if not targets:
            self._mods_log_line(
                "Nothing to uninstall - no unchecked mod is currently installed.")
            return
        listing = "\n".join("  - %s (%s)" % (m["name"], m["id"]) for m in targets)
        if not messagebox.askyesno(
                "Uninstall unchecked mods",
                "Delete the installed files for these %d unchecked mod(s)? This frees "
                "disk space; you can re-download them later.\n\n%s" % (len(targets), listing)):
            return
        removed = set()
        for mod in targets:
            ok, msg = uninstall_mod(server_root, mod["id"])
            self._mods_log_line(msg if ok else "! " + msg)
            if ok:
                removed.add(mod["id"])
        # Defensive: never leave a just-removed mod referenced in ActiveMods (an unchecked
        # mod shouldn't be active, but don't assume the two are in sync).
        active = [mid for mid in read_active_mods(server_root) if mid not in removed]
        set_active_mods(server_root, active)
        self._refresh_mods_list()

    def on_mods_rename(self):
        mod = next((m for m in self._mods if m["id"] == self._mods_selected_id), None)
        if not mod:
            messagebox.showinfo("Rename mod", "Select a mod in the list first.")
            return
        if mod.get("supported", True):
            messagebox.showinfo(
                "Rename mod",
                "\"%s\" is one of the mods the apworld knows by name, so its name stays "
                "as-is. Only mods you added yourself (\"apworld ✗\") can be renamed."
                % mod["name"])
            return

        win = tk.Toplevel(self)
        win.title("Rename mod")
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Display name for mod %s:" % mod["id"]).pack(anchor="w")
        name_var = tk.StringVar(value=mod["name"])
        entry = ttk.Entry(frm, textvariable=name_var, width=36)
        entry.pack(anchor="w", pady=(2, 8))
        entry.focus_set()
        entry.select_range(0, "end")
        ttk.Label(frm, foreground=self.theme["subtle_fg"], wraplength=320, justify="left",
                  text="Display only - the mod's ID, install state, load order and "
                       "activation don't change, and an unsupported mod is still left "
                       "out of the YAML copy. Clear the box to show the raw ID."
                  ).pack(anchor="w", pady=(0, 8))

        def _submit():
            rename_mod(mod, name_var.get())
            self._save_mods_config()
            self._mods_log_line("Renamed mod %s to \"%s\"." % (mod["id"], mod["name"]))
            win.destroy()
            self._refresh_mods_list()

        btnrow = ttk.Frame(frm)
        btnrow.pack(fill="x")
        ttk.Button(btnrow, text="Rename", command=_submit).pack(side="right", padx=(4, 0))
        ttk.Button(btnrow, text="Cancel", command=win.destroy).pack(side="right")
        entry.bind("<Return>", lambda _e: _submit())

    def on_mods_open_workshop_page(self):
        mod = next((m for m in self._mods if m["id"] == self._mods_selected_id), None)
        if not mod:
            messagebox.showinfo("ARKIpelago Launcher", "Select a mod in the list first.")
            return
        webbrowser.open(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=%s" % mod["id"])

    def on_mods_copy_active_ids(self):
        # Every CHECKED and SUPPORTED mod's id, in the list's current top-to-bottom order
        # (= load order), comma-separated - for pasting into the yaml's mod_ids. Install
        # status is irrelevant here; supported status is NOT (see split_copyable_mod_ids).
        if not any(m.get("enabled") for m in self._mods):
            messagebox.showinfo("Copy IDs for YAML",
                                "No mods are checked - tick the mods you want in your YAML "
                                "first, then copy.")
            self._mods_log_line("Copy IDs for YAML: nothing to copy (no mods checked).")
            return
        ids, excluded, conflicts = split_copyable_mod_ids(self._mods)
        listed = "\n".join("  %s  (%s)" % (m["id"], m.get("name", "?")) for m in excluded)
        if not ids:
            # Copying "" would look like the button did nothing; say what happened instead.
            messagebox.showwarning(
                "Copy IDs for YAML",
                "Nothing was copied: all %d checked mod(s) are user-added, and the "
                "apworld only accepts mod IDs it ships engram data for.\n\n%s\n\nThey "
                "still install and load on your server as normal ARK mods - they just "
                "can't go in your YAML's mod_ids, which would fail generation with "
                "\"mod_ids lists <id>, which this apworld doesn't know\"."
                % (len(excluded), listed))
            self._mods_log_line("Copy IDs for YAML: nothing copied - all %d checked mod(s) "
                                "are unsupported by the apworld." % len(excluded))
            return

        text = ", ".join(ids)
        self.clipboard_clear()
        self.clipboard_append(text)
        self._mods_log_line("Copied %d mod ID(s) for YAML: %s" % (len(ids), text))

        notes = []
        if excluded:
            notes.append(
                "%d unsupported mod ID(s) were left out - the apworld only accepts IDs it "
                "ships engram data for, and including one fails generation with \"mod_ids "
                "lists <id>, which this apworld doesn't know\":\n\n%s\n\nThey still "
                "install and load on your server as normal ARK mods; mod_ids is the only "
                "place they can't appear." % (len(excluded), listed))
        for pair in conflicts:
            notes.append(
                "Heads up: %s are the same mod (Structures Plus and its fork Super "
                "Structures). Both were copied, but the apworld rejects the pair - a "
                "server can only load one. Delete whichever you don't have installed from "
                "the pasted line." % " and ".join(pair))
        if notes:
            messagebox.showinfo("Copy IDs for YAML",
                                "Copied %d mod ID(s):\n\n%s\n\n%s"
                                % (len(ids), text, "\n\n".join(notes)))
            self._mods_log_line("Copy IDs for YAML: excluded %d unsupported ID(s)%s."
                                % (len(excluded),
                                   "; alias conflict: %s" % conflicts if conflicts else ""))

    # ---- mod download/install worker (SteamCMD, streamed to the Mods log) ---------- #
    def _start_mods_worker(self, server_root, to_download, activate, force, title):
        """Kick off the background download/install of `to_download` (a list of mod dicts),
        streaming SteamCMD output into the Mods log. `activate` is the checked-mod list to
        write into ActiveMods afterwards (None = leave activation untouched, e.g. verify)."""
        self._mods_queue = queue.Queue()
        self._mods_log_line("")
        self._mods_log_line("=== %s: %d mod(s) ===" % (title, len(to_download)))
        self._mods_thread = threading.Thread(
            target=self._mods_download_worker,
            args=(server_root, to_download, activate, force), daemon=True)
        self._mods_thread.start()
        self._update_mods_button_states()  # reflect busy: disable the action buttons
        self.after(100, self._poll_mods_queue)

    def _mods_download_worker(self, server_root, to_download, activate, force):
        q = self._mods_queue

        def log(line):
            q.put(("line", line))  # marshalled to the GUI thread by _poll_mods_queue

        try:
            steamcmd = self._ensure_steamcmd(q)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            log("! Could not prepare SteamCMD: %s" % exc)
            q.put(("done", False))
            return

        all_ok = True
        for mod in to_download:
            log("")
            log("--- %s %s (%s) ---"
                % ("Re-downloading" if force else "Downloading", mod["name"], mod["id"]))
            if force:
                # Clear SteamCMD's cached copy so it genuinely re-fetches every file.
                shutil.rmtree(_workshop_download_dir(steamcmd, mod["id"]), ignore_errors=True)
            result = download_and_install_mod(mod["id"], server_root, steamcmd, log=log)
            if not result.ok:
                all_ok = False
                log("! " + result.message)

        if activate is not None:
            # ActiveMods = the checked mods that actually ended up installed, in order.
            active_ids = [m["id"] for m in activate
                          if is_mod_installed(server_root, m["id"])]
            skipped = [m["id"] for m in activate
                       if not is_mod_installed(server_root, m["id"])]
            ok, msg = set_active_mods(server_root, active_ids)
            log(msg if ok else "! " + msg)
            if skipped:
                log("! Left inactive (not installed): %s" % ", ".join(skipped))
            log("Restart the ARK server for the change to take effect.")
        q.put(("done", all_ok))

    def _poll_mods_queue(self):
        try:
            while True:
                kind, payload = self._mods_queue.get_nowait()
                if kind == "line":
                    self._mods_log_line(payload)
                elif kind == "done":
                    self._on_mods_done(payload)
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll_mods_queue)

    def _on_mods_done(self, success):
        self._mods_thread = None
        self._mods_log_line("Done." if success
                            else "Finished with errors - see the messages above.")
        self._refresh_mods_list()  # re-read disk: icons + checkboxes now reflect reality

    # ------------------------------------------------------ Setup Status --- #
    # icon, color per item state: "ok" (check passed), "fail" (check failed - actionable),
    # "info" (not a real pass/fail check - see the BattlEye item's note in _gather_setup_status).
    # Glyph per state; color comes from self.theme (status_ok/status_fail/status_info)
    # at render time so it follows the current light/dark theme.
    STATUS_ICONS = {
        "ok":   "✓",
        "fail": "✗",
        "info": "ℹ",
    }

    def _build_setup_status_tab(self, parent):
        wrap = ttk.Frame(parent, padding=(10, 8))
        wrap.pack(fill="both", expand=True)

        top = ttk.Frame(wrap)
        top.pack(fill="x")
        ttk.Label(top, text="Setup Status", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(top, text="Re-check", command=self._recheck_setup_status
                   ).pack(side="right")

        ttk.Label(wrap, foreground=self.theme["subtle_fg"], wraplength=640, justify="left",
                  text="Read-only check of common setup steps, based on the current "
                       "Configuration tab paths and the files on disk. Nothing here is "
                       "changed automatically - use Configuration / Install Server/Api/Plugin to fix "
                       "a ✗."
                  ).pack(anchor="w", pady=(4, 8))

        # Scrollable list of checks - same Canvas + Scrollbar pattern as the Configuration
        # tab, since the check rows grow past the visible area (cluster folders, connector.ini,
        # plugin mode, component-version advisories, ...).
        items_wrap = ttk.Frame(wrap)
        items_wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(items_wrap, borderwidth=0, highlightthickness=0,
                           background=self.theme["bg"])
        vsb = ttk.Scrollbar(items_wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.status_items_frame = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=self.status_items_frame, anchor="nw")
        self.status_items_frame.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))

        def _on_wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))

    def _arkapi_win64_dir(self):
        root = self.get("SERVER_ROOT")
        return os.path.normpath(root) if root else ""

    def _gather_setup_status(self):
        """Compute the current checklist. Reuses the same path resolution the rest of
        the app already uses (SERVER_ROOT, _arkap_plugin_dir(), connector_ini) rather
        than deriving paths a second way, so this tab can't drift from what Quick
        Launch / Install actually operate on."""
        root = self._arkapi_win64_dir()
        items = []

        ok, detail = check_ark_server_installed(root)
        items.append({
            "label": "ARK dedicated server installed",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": "Set SERVER_ROOT on the Configuration tab, then Install Server/Api/Plugin -> "
                    "Install ARK Server.",
        })

        ok, detail = check_arkapi_installed(root)
        items.append({
            "label": "ArkApi installed",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": "Install Server/Api/Plugin -> Install ArkServerApi (needs the ARK server "
                    "installed first).",
        })

        # Cluster folders are never created by SteamCMD and ARK hangs on startup (rather
        # than erroring) when -ClusterDirOverride points at a path that doesn't exist, so
        # catch it here instead of letting the user find out via a silent stall.
        cluster_paths = {key: self.get(key) for key, _ in CLUSTER_PATH_SUBDIRS}
        ok, detail = check_cluster_dirs(cluster_paths)
        items.append({
            "label": "Cluster folders exist (CLUSTERDIR / SAVESROOT / BACKUPROOT)",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": "The field has a path but nothing is there. Usual causes: the folder "
                    "was never created (SteamCMD does not create any of these), or it "
                    "was renamed/moved/deleted after the path was saved - the saved path "
                    "keeps looking right either way. Fix: Configuration tab -> \"Create "
                    "%s folders\" creates whichever are missing, at the exact paths "
                    "shown above (existing folders are left untouched); or create the "
                    "folder by hand and hit Re-check. A missing cluster folder makes the "
                    "server hang on launch with no error."
                    % CLUSTER_ROOT_DIRNAME,
        })

        # BattlEye isn't a persisted setting, but it doesn't need to be: the only way
        # this launcher ever starts the server is start_ase_server.bat, which passes
        # -NoBattlEye unconditionally (no flag, no branch). So it's guaranteed by the
        # launch line rather than unknown - a checkmark, not an info icon.
        items.append({
            "label": "BattlEye disabled",
            "state": "ok",
            "detail": "Always disabled by start_ase_server.bat (-NoBattlEye is passed "
                      "on every launch).",
            "hint": "",
        })

        plugin_dir = self._arkap_plugin_dir()
        ok, detail = check_plugin_installed(plugin_dir)
        items.append({
            "label": "ArkAP plugin installed",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": "Install Server/Api/Plugin -> Install Plugin.",
        })

        ok, detail = check_plugin_mode(plugin_dir)
        items.append({
            "label": "Plugin mode is \"ap\" (not offline)",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": "Set \"mode\": \"ap\" in ArkAP.config.json for real multiworld play "
                    "(\"offline\" self-randomizes locally for solo hook testing).",
        })

        # Its own row rather than folding into the per-mod icons: a corrupt <id>.mod is
        # not "mod missing", it's "the server will crash on startup", and it can be left
        # by a mod this launcher doesn't even have in its list.
        broken = find_broken_mod_files(self.get("SERVER_ROOT"))
        items.append({
            "label": "No corrupt .mod files in Content\\Mods",
            "state": "fail" if broken else "ok",
            "detail": ("; ".join("%s - %s" % (os.path.basename(p), why) for p, why in broken)
                       if broken else "Every <id>.mod file present is intact."),
            "hint": "A corrupt .mod file crashes the ARK server on startup (\"Invalid "
                    "BufferCount=0\") instead of just skipping the mod. The launcher "
                    "deletes these automatically at startup - hit Re-check, then "
                    "re-download the mod from the Mods tab. If it stays listed, the file "
                    "is locked (is the server running?) - delete it by hand.",
        })

        # The Mods tab shows intent; GameUserSettings.ini is what the server reads. Nothing
        # else in the app notices when the two drift apart (see check_active_mods_match).
        ok, detail = check_active_mods_match(self.get("SERVER_ROOT"), self._mods)
        items.append({
            "label": "Mods tab matches ActiveMods on disk",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": "The server reads its mod list from GameUserSettings.ini's ActiveMods, "
                    "and Save is what writes the ticks there. Ticked but not in ActiveMods "
                    "means it was never saved, so it never loads, and a yaml whose mod_ids "
                    "name it then expects engrams that aren't in the world. In ActiveMods "
                    "but not ticked means the server is already loading it while the tab "
                    "(and \"Copy IDs for YAML\") pretends it's off. Either way the tab is "
                    "what wins: tick the mods you actually want - Refresh re-reads disk if "
                    "it's the ticks that are wrong - then Mods tab -> Save writes that list "
                    "over ActiveMods (its Save button is highlighted whenever there's "
                    "something to write); \"Download checked\" installs anything missing "
                    "and saves in one go. Restart the ARK server afterwards.",
        })

        ok, detail = check_scripts_sourced(self._scripts_dir)
        items.append({
            "label": "Server scripts read their paths from paths.cmd",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": "A script left over from an older launcher keeps its own SERVER_ROOT "
                    "and ignores what Save writes, so the server starts against the "
                    "wrong folder while this tab's paths all look right. Close the "
                    "launcher, delete the offending file from the ArkServerScripts "
                    "folder and reopen - the current version is unpacked from the exe.",
        })

        # The SAME comparison Quick Launch makes before running the .bat (_preflight_bat),
        # surfaced up front instead of only on the click that gets refused: the fields are
        # compared against what is actually written in the file, so a value typed but never
        # Saved reads as unsaved even though the field on screen looks right.
        missing, unsaved, absent = self._preflight_bat("start_ase_server.bat")
        parts = []
        if missing:
            parts.append("not set: %s" % ", ".join(missing))
        if unsaved:
            parts.append("typed but not saved (the file still holds the old value): %s"
                         % ", ".join("%s in %s" % (key, fname) for key, fname in unsaved))
        if absent:
            parts.append("missing from the scripts folder: %s" % ", ".join(absent))
        items.append({
            "label": "Configuration is saved into the server scripts",
            "state": "fail" if parts else "ok",
            "detail": ("; ".join(parts) if parts else
                       "paths.cmd and start_ase_server.bat hold the current field values."),
            "hint": "start_ase_server.bat reads its settings from paths.cmd, and Save is "
                    "what writes them there. Anything typed but not yet saved is missing "
                    "from the file, so Quick Launch refuses to start the server rather "
                    "than running against stale paths. Fix: Configuration tab -> Save (its "
                    "Save button is highlighted whenever there's something to write).",
        })

        # Reads the FILE, not the Connector fields. The in-game integrated connector is
        # the primary way to connect - connector.ini only matters for the optional
        # standalone Python connector fallback, so a missing/incomplete file is an
        # advisory (yellow "i"), never a red X, and never counts toward the overall
        # fail state (see aggregate_status_state).
        ini_path = self.get("connector_ini")
        ok, detail = check_connector_filled(ini_path)
        if not ini_path:
            state, detail = "ok", "Not configured - using the in-game integrated connector."
        else:
            state = "ok" if ok else "info"
        items.append({
            "label": "connector.ini filled in (optional standalone connector fallback only)",
            "state": state,
            "detail": detail,
            "hint": "" if state == "ok" else
                    ("Point \"connector.ini file\" in the Configuration tab's Locations group "
                     "at your ArkConnector's connector.ini, then fill in server/slot on the "
                     "Archipelago Setup tab and ipc_dir in the Configuration tab's "
                     "\"Plugin files & DeathLink\" group, and Save."),
        })

        # Component/launcher version rows (yellow "i" when newer, green check for the
        # launcher when up to date - never a ✗) computed off the main thread against the
        # GitHub releases; empty until that check comes back. See
        # _component_version_check_worker.
        items.extend(getattr(self, "_component_advisories", []))
        return items

    def _refresh_setup_status(self):
        for child in self.status_items_frame.winfo_children():
            child.destroy()
        state_colors = {
            "ok": self.theme["status_ok"],
            "fail": self.theme["status_fail"],
            "info": self.theme["status_info"],
        }
        items = self._gather_setup_status()
        for item in items:
            icon = self.STATUS_ICONS[item["state"]]
            color = state_colors[item["state"]]
            row = ttk.Frame(self.status_items_frame)
            row.pack(fill="x", pady=4, anchor="w")
            tk.Label(row, text=icon, foreground=color,
                     background=self.theme["bg"], font=("Segoe UI", 12, "bold"),
                     width=2).pack(side="left", anchor="n")
            textcol = ttk.Frame(row)
            textcol.pack(side="left", fill="x", expand=True)
            ttk.Label(textcol, text=item["label"], font=("Segoe UI", 9, "bold")
                      ).pack(anchor="w")
            if item.get("detail"):
                ttk.Label(textcol, text=str(item["detail"]),
                          foreground=self.theme["status_detail_fg"],
                          wraplength=560, justify="left").pack(anchor="w")
            if item["state"] == "fail" and item.get("hint"):
                ttk.Label(textcol, text="→ %s" % item["hint"],
                          foreground=self.theme["note_fg"],
                          wraplength=560, justify="left").pack(anchor="w")
            elif item["state"] == "info" and item.get("hint"):
                ttk.Label(textcol, text=item["hint"], foreground=self.theme["note_fg"],
                          wraplength=560, justify="left").pack(anchor="w")
            # Clickable release link for the component-version advisories.
            if item.get("link"):
                link = ttk.Label(textcol, text=item["link"],
                                 foreground=self.theme["status_info"], cursor="hand2")
                link.pack(anchor="w")
                link.bind("<Button-1>", lambda _e, u=item["link"]: webbrowser.open(u))

        # Keep the tab-bar symbol in step with whatever's on screen now, so it's right
        # regardless of what triggered this refresh (Re-check, tab switch, theme toggle,
        # or the async component check landing).
        self._update_status_tab_indicator(aggregate_status_state(items))

    def _make_status_glyph(self, state):
        """A tiny colored symbol (green check / amber "i" / red X) for the Setup Status
        tab label, drawn pixel-wise so it needs no font or image asset and can be recolored
        to the current theme. Returns a tk.PhotoImage with a transparent background."""
        size = 14
        color = {"ok": self.theme["status_ok"], "info": self.theme["status_info"],
                 "fail": self.theme["status_fail"]}[state]
        img = tk.PhotoImage(width=size, height=size)

        def block(x, y):  # 2x2 stroke so the symbol reads at this size, clipped to bounds
            for dx in (0, 1):
                for dy in (0, 1):
                    px, py = x + dx, y + dy
                    if 0 <= px < size and 0 <= py < size:
                        img.put(color, to=(px, py))

        if state == "fail":            # X
            for i in range(8):
                block(3 + i, 3 + i)
                block(10 - i, 3 + i)
        elif state == "ok":            # check mark
            for i in range(4):
                block(3 + i, 7 + i)    # short arm down-right
            for i in range(7):
                block(6 + i, 10 - i)   # long arm up-right
        else:                          # info "i"
            block(6, 2)                # dot
            for y in (5, 7, 9, 11):    # stem
                block(6, y)
        return img

    def _update_status_tab_indicator(self, state):
        """Show the aggregate state as a small colored symbol to the right of the "Setup
        Status" tab label, so it's visible from every tab. Called on every refresh."""
        if not hasattr(self, "notebook"):
            return
        try:
            img = self._make_status_glyph(state)
            self._status_tab_glyphs[state] = img  # keep a ref so Tk doesn't GC it
            self.notebook.tab(self.tab_status, image=img, compound="right")
        except (tk.TclError, KeyError):
            pass

    def _recheck_setup_status(self):
        """Manual Re-check: redraw the on-disk checks immediately, and re-query the
        component GitHub releases in the background (which redraws again when it lands)."""
        self._refresh_setup_status()
        self._start_component_version_check()

    def _on_tab_changed(self, _event=None):
        # Leaving the tab the easter-egg track started on kills it, permanently.
        self._stop_egg_music()
        try:
            current = self.notebook.select()
        except tk.TclError:
            return
        if current == str(self.tab_status):
            self._refresh_setup_status()
        elif current == str(self.tab_profiles):
            self._update_profile_status()
        elif current == str(self.tab_mods):
            self._refresh_mods_tab()
        elif current == str(self.tab_debug):
            # Logs grow while the app and the server run - re-read on entry so the tab
            # isn't showing whatever was on disk at startup.
            self._refresh_debug_log()
        elif current == str(self.tab_archipelago):
            # Re-stat on entry: Archipelago may have been installed, moved or
            # uninstalled since the tab was last looked at.
            self._refresh_archipelago_buttons()

    # ----------------------------------------------------- Diagnostics export #
    def _redacted_config_json(self):
        """The current config JSON as pretty text with the secret fields blanked, or a
        short note if it can't be read. Reads the file on disk (what's actually saved)."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            return "(could not read %s: %s)" % (self.config_path, exc)
        return json.dumps(redact_config(data), indent=2)

    def _mods_diagnostics(self):
        """(summary_text, session_log_text) for the diagnostics zip. Install state is
        re-read from disk here rather than trusting the row icons, so the export is
        accurate even if the Mods tab was never opened this session."""
        server_root = self.get("SERVER_ROOT")
        installed = {str(m.get("id")) for m in self._mods
                     if is_mod_installed(server_root, m.get("id"))}
        summary = format_mods_summary(self._mods, installed, server_root)
        try:
            log_text = self.mods_log.get("1.0", "end").strip()
        except (AttributeError, tk.TclError):
            log_text = ""
        return summary, log_text or "(the Mods tab logged nothing this session)"

    def _diagnostics_entries(self):
        """[(name_in_zip, text)] for the whole export.

        Every entry is TEXT, which is what lets one redaction pass cover the entire
        bundle (see export_diagnostics). Files are read rather than zf.write()n for the
        same reason - and so the big logs can be truncated on the way through."""
        cfg = self._read_config_dict()
        server_root = self.get("SERVER_ROOT")
        config_dir = self._server_config_dir()
        plugin_dir = self._arkap_plugin_dir()
        arch_dir = self.get(ARCHIPELAGO_DIR_KEY)
        mods_summary, mods_log = self._mods_diagnostics()
        yamls, yaml_note = find_player_yamls(arch_dir, self.get("slot"))

        entries = [
            ("setup_status.txt", format_setup_status_summary(self._gather_setup_status())),
            ("versions.txt", format_version_block(cfg)),
            ("arkap_launcher_config.redacted.json", self._redacted_config_json()),
            ("mods_status.txt", mods_summary),
            ("mods_output_log.txt", mods_log),
            ("archipelago_yaml_search.txt", yaml_note),
            ("listing_ipc.txt", format_dir_listing(self._ipc_dir(), "ipc folder")),
            ("listing_content_mods.txt", format_dir_listing(
                os.path.join(os.path.normpath(server_root), "ShooterGame", "Content", "Mods")
                if server_root else "", "ARK ShooterGame\\Content\\Mods folder")),
        ]

        # Config/log files, each as (name in the zip, where it lives). A blank path means
        # "we can't work out where it would be" - read_for_diagnostics says so in the file
        # rather than leaving a silent hole.
        srv = os.path.normpath(server_root) if server_root else ""
        for name, path in [
                ("ArkAP_debug.log", self._arkap_debug_log_path()),
                # The launcher's own activity log - what this app actually did, in order,
                # with timestamps. Usually the first file worth reading.
                (LAUNCHER_LOG_FILENAME, launcher_log_path()),
                (CRASH_LOG_FILENAME, crash_log_path()),
                ("paths.cmd", os.path.join(self._scripts_dir, "paths.cmd")
                 if self._scripts_dir else ""),
                ("Game.ini", os.path.join(config_dir, "Game.ini") if config_dir else ""),
                ("GameUserSettings.ini",
                 os.path.join(config_dir, "GameUserSettings.ini") if config_dir else ""),
                ("ArkAP.config.json",
                 os.path.join(plugin_dir, "ArkAP.config.json") if plugin_dir else ""),
                ("ShooterGame.log",
                 os.path.join(srv, "ShooterGame", "Saved", "Logs", "ShooterGame.log")
                 if srv else ""),
                # host.yaml decides the port a locally hosted room actually listens on
                # (see archipelago_host_port) - the other half of every "I can't connect".
                ("host.yaml", os.path.join(os.path.normpath(arch_dir), "host.yaml")
                 if arch_dir else "")]:
            entries.append((name, read_for_diagnostics(path)))

        # SteamCMD's own logs. What it prints to stdout (already in the install log, and so
        # in the launcher log) is a fraction of what these record - the rest is what
        # explains a mod download that reported success and wrote nothing. Grouped under
        # steamcmd/ like ipc/, and absent on a --onefile build that hasn't downloaded
        # anything this run: the bundle folder is re-extracted per run (see resource_dir).
        for name in ("console_log.txt", "workshop_log.txt", "content_log.txt"):
            entries.append(("steamcmd/" + name,
                            read_for_diagnostics(os.path.join(steamcmd_dir(), "logs", name))))

        for path in yamls:
            entries.append(("players_yaml/" + os.path.basename(path),
                            read_for_diagnostics(path)))
        # The ipc files themselves, on top of the listing above - see collect_ipc_entries.
        entries.extend(collect_ipc_entries(self._ipc_dir()))
        return entries

    def export_diagnostics(self):
        """Bundle everything a helper would otherwise have to ask for - Setup Status,
        component versions, the redacted config, Mods state + log, the user's Archipelago
        yaml, paths.cmd, Game.ini / GameUserSettings.ini, ArkAP.config.json, the debug,
        launcher, crash, ShooterGame and SteamCMD logs - every log the Debug Log tab can
        show - the contents of ipc\\ (including the per-player mailbox
        subfolders), and listings of ipc\\ and Content\\Mods - into one zip the user can drag
        straight into Discord.

        Redaction is ONE pass over the finished bundle rather than a redactor per file:
        paths.cmd holds ADMINPASS/SERVERPASS since the single-source-of-truth refactor,
        GameUserSettings.ini holds ServerAdminPassword/ServerPassword, and a yaml can
        hold a room password - a per-file approach silently misses each new one."""
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        initial_dir = desktop if os.path.isdir(desktop) else base_dir()
        default_name = "arkap_diagnostics_%s.zip" % time.strftime("%Y%m%d_%H%M%S")
        dest = filedialog.asksaveasfilename(
            title="Save diagnostics zip",
            initialdir=initial_dir, initialfile=default_name,
            defaultextension=".zip", filetypes=[("Zip archive", "*.zip")])
        if not dest:
            return

        # Gather everything first so a read error surfaces before we open the zip.
        entries = self._diagnostics_entries()
        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, text in entries:
                    zf.writestr(name, redact_text(text))
        except OSError as exc:
            messagebox.showerror("Export diagnostics",
                                 "Could not write the diagnostics zip:\n\n%s" % exc)
            return

        size_kb = max(1, os.path.getsize(dest) // 1024)
        messagebox.showinfo(
            "Export diagnostics",
            "Diagnostics saved to:\n\n%s\n\n%d files, %d KB. It contains your Setup "
            "Status, component versions, config, Archipelago yaml, server config files, "
            "logs, the contents of the ipc folder and folder listings. Every password in "
            "every file is replaced with %s; long logs are cut to their last %d lines and "
            "the ipc files to their last %d.\n\nDrag this file into "
            "Discord or attach it to a GitHub issue when asking for help."
            % (dest, len(entries), size_kb, REDACT_MARKER, DIAG_MAX_LINES,
               DIAG_IPC_MAX_LINES))
        try:
            os.startfile(os.path.dirname(dest))  # noqa: S606 - open the folder in Explorer
        except OSError:
            pass

    # ------------------------------------------------------------ Debug Log #
    def _build_debug_log_tab(self, parent):
        """Log viewer - one text pane showing whichever log the dropdown selects.

        A dropdown rather than a second row of sub-tabs: the tab already has a
        control row (Search / Jump to latest / Refresh) with room in it, a nested
        ttk.Notebook inside the main one reads as two tab bars stacked, and the
        Profiles tab already picks from a list this exact way. The search box,
        "Jump to latest" and "Refresh" all act on the selected log - there is only
        ever one text widget (self.debug_log_text), so the in-app search and the
        theme switcher keep working unchanged."""
        wrap = ttk.Frame(parent, padding=(10, 8))
        wrap.pack(fill="both", expand=True)

        dbg = ttk.LabelFrame(wrap, text="Logs", padding=(8, 6))
        dbg.pack(fill="both", expand=True)

        selrow = ttk.Frame(dbg)
        selrow.pack(fill="x", pady=(0, 6))
        ttk.Label(selrow, text="Log:").pack(side="left")
        self.debug_log_source_var = tk.StringVar(value=LOG_SOURCES[0][0])
        source_combo = ttk.Combobox(selrow, textvariable=self.debug_log_source_var,
                                     state="readonly", width=42,
                                     values=[label for label, _key in LOG_SOURCES])
        source_combo.pack(side="left", padx=(6, 8))
        source_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_debug_log())
        Tooltip(source_combo,
                "Which log to show below. The plugin log is ArkAP's own; the launcher "
                "log is what this app did (installs, scans, saves, resets, mod "
                "downloads); the crash log holds launcher tracebacks; ShooterGame.log is "
                "ARK's, where LowLevelFatalError crashes land; the SteamCMD logs cover "
                "downloads. Very large logs load only their newest part.")
        dbgtop = ttk.Frame(dbg)
        dbgtop.pack(fill="x")
        ttk.Label(dbgtop, text="Search:").pack(side="left")
        self.debug_log_search_var = tk.StringVar()
        dbg_search_entry = ttk.Entry(dbgtop, textvariable=self.debug_log_search_var, width=24)
        dbg_search_entry.pack(side="left", padx=(4, 8))
        dbg_search_entry.bind("<KeyRelease>", lambda _e: self._highlight_debug_log_search())
        Tooltip(dbg_search_entry, "Highlights every occurrence in the log below as you type.")
        jump_btn = ttk.Button(dbgtop, text="Jump to latest",
                               command=self._jump_debug_log_latest)
        jump_btn.pack(side="left", padx=(0, 4))
        Tooltip(jump_btn, "Scroll to the bottom of the log (the most recent lines).")
        refresh_btn = ttk.Button(dbgtop, text="Refresh", command=self._refresh_debug_log)
        refresh_btn.pack(side="left")
        Tooltip(refresh_btn, "Re-read the selected log from disk - logs change while the "
                             "server and the launcher run, so this isn't automatic.")

        dbgbody = ttk.Frame(dbg)
        dbgbody.pack(fill="both", expand=True, pady=(4, 0))
        self.debug_log_text = tk.Text(dbgbody, height=8, wrap="none", state="disabled",
                                       font=("Consolas", 9),
                                       background=self.theme["text_bg"],
                                       foreground=self.theme["text_fg"],
                                       insertbackground=self.theme["text_fg"])
        dbg_vsb = ttk.Scrollbar(dbgbody, orient="vertical", command=self.debug_log_text.yview)
        self.debug_log_text.configure(yscrollcommand=dbg_vsb.set)
        self.debug_log_text.pack(side="left", fill="both", expand=True)
        dbg_vsb.pack(side="right", fill="y")

    # -------------------------------------------------------------- Profiles #
    def _build_profiles_tab(self, parent):
        wrap = ttk.Frame(parent, padding=(10, 8))
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="Profiles", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(wrap, foreground=self.theme["subtle_fg"], wraplength=640, justify="left",
                  text="Save the entire Configuration tab (Paths / Network / Cluster / "
                       "Connector fields) under a name, e.g. \"Solo Test\" or \"Friend "
                       "Group Run\", so you can switch setups without re-typing "
                       "everything. Loading a profile only fills in the Configuration "
                       "fields here in the app - it never writes to your .bat/.ini files "
                       "by itself. You still need to go to the Configuration tab and "
                       "press Save afterward."
                  ).pack(anchor="w", pady=(4, 8))

        selrow = ttk.Frame(wrap)
        selrow.pack(fill="x")
        ttk.Label(selrow, text="Saved profiles:").pack(side="left")
        self.profile_select_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(selrow, textvariable=self.profile_select_var,
                                           state="readonly", width=32)
        self.profile_combo.pack(side="left", padx=(6, 0))
        self.profile_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_profile_status())

        btnrow1 = ttk.Frame(wrap)
        btnrow1.pack(fill="x", pady=(8, 0))
        load_btn = ttk.Button(btnrow1, text="Load selected profile",
                               command=self._on_load_profile)
        load_btn.pack(side="left", padx=(0, 4))
        Tooltip(load_btn, "Fills every Configuration field and the notes box below from "
                "the selected profile. Does NOT save/apply anything by itself - you'll "
                "be reminded to press Save on the Configuration tab.")
        save_new_btn = ttk.Button(btnrow1, text="Save as new profile",
                                   command=self._on_save_new_profile)
        save_new_btn.pack(side="left", padx=4)
        Tooltip(save_new_btn, "Snapshot every current Configuration field plus the notes "
                "box below into a brand-new named profile.")
        update_btn = ttk.Button(btnrow1, text="Update selected profile",
                                 command=self._on_update_profile)
        update_btn.pack(side="left", padx=4)
        Tooltip(update_btn, "Overwrite the selected profile's saved fields and notes with "
                "the current Configuration fields and the notes box below.")

        default_note = ttk.Label(
            wrap, foreground=self.theme["note_fg"], wraplength=640, justify="left",
            text="On first run the launcher creates a profile named \"%s\" from "
                 "whatever your Configuration tab starts with, and loads it, so your "
                 "settings always belong to a profile from the start. It's an ordinary "
                 "profile - rename, update or delete it however you like."
                 % DEFAULT_PROFILE_NAME)
        default_note.pack(anchor="w", pady=(6, 0))

        autosave_note = ttk.Label(
            wrap, foreground=self.theme["note_fg"], wraplength=640, justify="left",
            text="The \"%s\" profile in this list is written by the launcher itself "
                 "every 10 minutes while the app is open, as a safety net. It always "
                 "holds just the latest snapshot, and it never touches the profiles you "
                 "save yourself. You can load it like any other profile, but it can't be "
                 "renamed or updated by hand." % AUTOSAVE_PROFILE_NAME)
        autosave_note.pack(anchor="w", pady=(6, 0))
        Tooltip(autosave_note,
                "Autosave exists so a crash, a bad edit, or an update can't cost you "
                "more than the last few minutes of Configuration changes. Load it, "
                "check the values, then \"Save as new profile\" to keep them.")

        btnrow2 = ttk.Frame(wrap)
        btnrow2.pack(fill="x", pady=(4, 0))
        rename_btn = ttk.Button(btnrow2, text="Rename profile", command=self._on_rename_profile)
        rename_btn.pack(side="left", padx=(0, 4))
        delete_btn = ttk.Button(btnrow2, text="Delete profile", command=self._on_delete_profile)
        delete_btn.pack(side="left", padx=4)

        self.profile_status_var = tk.StringVar(value="No profile loaded.")
        ttk.Label(wrap, textvariable=self.profile_status_var, foreground=self.theme["note_fg"],
                  wraplength=640, justify="left").pack(anchor="w", pady=(10, 4))

        notes_frame = ttk.LabelFrame(wrap, text="Notes for the selected profile",
                                      padding=(8, 6))
        notes_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.profile_notes_text = tk.Text(notes_frame, height=10, wrap="word",
                                           font=("Segoe UI", 9),
                                           background=self.theme["text_bg"],
                                           foreground=self.theme["text_fg"],
                                           insertbackground=self.theme["text_fg"])
        notes_vsb = ttk.Scrollbar(notes_frame, orient="vertical",
                                   command=self.profile_notes_text.yview)
        self.profile_notes_text.configure(yscrollcommand=notes_vsb.set)
        self.profile_notes_text.pack(side="left", fill="both", expand=True)
        notes_vsb.pack(side="right", fill="y")
        self.profile_notes_text.bind("<<Modified>>", self._on_profile_notes_modified, add="+")
        Tooltip(self.profile_notes_text,
                "Free-text notes tied to whichever profile is loaded/selected above - e.g. "
                "\"used for the Discord group run, remember RCON port differs from solo "
                "test\". Saved along with the rest of that profile's data via \"Save as "
                "new profile\" or \"Update selected profile\".")

    def _build_instructions_tab(self, parent):
        """One tab, two guides. Quick Guide (default) and Full Guide are each built as
        their own Text widget by _build_instruction_text, then stacked in `body` with
        only one packed at a time - _toggle_instructions_mode swaps which one is packed
        rather than rebuilding either, so collapse/expand state in the hidden guide
        survives a switch."""
        wrap = ttk.Frame(parent, padding=(10, 8))
        wrap.pack(fill="both", expand=True)

        toolbar = ttk.Frame(wrap)
        toolbar.pack(fill="x", pady=(0, 4))
        # One pair, both levels: "Collapse all" folds every section heading AND every step
        # (flattening the guide to just headings); "Expand all" opens both back up fully.
        # Acts on whichever guide is currently showing.
        ttk.Button(toolbar, text="Expand all",
                   command=lambda: self._set_all_instructions(
                       False, self._active_instructions_text())).pack(side="left")
        ttk.Button(toolbar, text="Collapse all",
                   command=lambda: self._set_all_instructions(
                       True, self._active_instructions_text())
                   ).pack(side="left", padx=(6, 0))
        # Labeled with the guide a click switches TO, never the one currently showing.
        self.instructions_mode_btn = ttk.Button(toolbar, text="Full Guide",
                                                 command=self._toggle_instructions_mode)
        self.instructions_mode_btn.pack(side="right")
        Tooltip(self.instructions_mode_btn,
                "Quick Guide is the short version. Full Guide has every detail and "
                "caveat. Switching keeps each guide's own collapsed/expanded sections.")

        body = ttk.Frame(wrap)
        body.pack(fill="both", expand=True)

        # (tag, text) pairs - kept short and skimmable, referencing this app's actual
        # tab/button/group names rather than a generic reprint of the GitHub README.
        full_content = [

            ("bullet", "Pro tip: most options have tooltips, just hover over one."),
            ("bullet", "Pro tip: the Search bar (top left) searches field labels, tooltips, "
                       "and text across every tab - press Enter, then use Find Next / Find "
                       "Prev to jump between matches."),
            ("bullet", "THIS LAUNCHER WILL NEVER TOUCH YOUR ACTUAL ARK DOWNLOAD LOCATION PLEASE DONT SET ANY PATH TO IT, i beg you"),

            ("bullet", "Pro tip: everything here collapses and expands. Click the box "
                       "beside a header, or use the Expand all / Collapse all buttons."),

            ("h1", "Start here - install in this order"),
            ("bullet", "The three installs below must happen in order: the ARK server "
                       "first, then ArkServerApi into it, then the ArkAP plugin into "
                       "ArkApi. Each one needs the previous one to already exist. All "
                       "three live on the Install Server/Api/Plugin tab."),

            ("bullet", "1. Install Server/Api/Plugin tab -> set SERVER_ROOT (the folder the server "
                       "gets installed into), then click \"Install ARK Server\". This "
                       "downloads ~18gb via SteamCMD - progress shows in the console box "
                       "below the buttons, and \"Cancel\" stops it if you need to. Note: "
                       "make sure your ARK: Survival Evolved game is on the preaquatica "
                       "branch, or you won't be able to join."),
            ("bullet", "   You can install the server anywhere you like - it doesn't have "
                       "to be a special location. A short path near the top of a drive, "
                       "like C:\\ark\\, keeps things simple and avoids very long paths."),
            ("bullet", "   If it fails with exit code 8, just click Install again - it "
                       "usually works on the second try."),
            ("bullet", "   When it finishes, the cluster folders (CLUSTERDIR / SAVESROOT / "
                       "BACKUPROOT) are created for you in a ServerCluster folder "
                       "inside SERVER_ROOT, and the three fields are filled in with them - "
                       "SteamCMD itself never creates them, and ARK hangs on launch with "
                       "no error if CLUSTERDIR is missing. Click Save on the "
                       "Configuration tab so the .bat scripts pick them up. Setup Status "
                       "has a \"Cluster folders exist\" row to confirm."),
            ("bullet", "   If they're ever missing - an older install, or you moved or "
                       "deleted them - use \"Create ServerCluster folders\" on the "
                       "Configuration tab (in the Paths group) to create them again. "
                       "Folders that already exist are left untouched, and a path you "
                       "set yourself is created where you put it rather than moved."),

            ("bullet", "2. Install Server/Api/Plugin tab -> click \"Install ArkServerApi\". This downloads the "
                       "latest ArkApi release and extracts it into "
                       "ShooterGame\\Binaries\\Win64 for you - no manual unzipping. When "
                       "it's done, Win64 contains version.dll and an ArkApi\\ folder. "
                       "Note: BattlEye must be OFF for ArkApi to work, but "
                       "start_ase_server already disables it for you - We gotchu fam."),
            ("bullet", "   Your own ARK: Survival Evolved game also needs BattlEye off to "
                       "connect - that's separate from the server side above, which "
                       "start_ase_server already handles for you. In Steam, right-click "
                       "ARK: Survival Evolved -> Properties -> Launch Options, and add "
                       "-NoBattlEye."),

            ("bullet", "3. Same tab -> in the \"Install/update ArkAP Plugin\" box, click \"Install "
                       "Plugin\". It downloads the latest ArkAP_Plugin.zip straight from "
                       "GitHub and extracts it into Win64\\ArkApi\\Plugins\\ArkAP for you - "
                       "no manual download/unzip. Progress shows in the console box. "
                       "Upgrading later keeps your existing ArkAP.config.json. (If the "
                       "download ever fails or you want a specific older version, use "
                       "\"Manual downloads\" at the bottom of the tab instead.)"),

            ("bullet", "4. Configuration tab -> in the Paths group, click \"Scan for "
                       "paths\", or Browse to set paths by hand. "
                       "if SERVER_ROOT isn't set yet, or "
                       "doesn't look right, it finds it for you first (Steam libraries, "
                       "common drive roots), then automatically scans around it for "
                       "PLUGINS_DIR / ipc_dir / game_ini and the cluster folders. Note: "
                       "SERVER_ROOT is the folder that CONTAINS "
                       "ShooterGame, not the folder above it. If your download put the "
                       "game in a nested folder (e.g. C:\\ARKServer\\ARK Survival "
                       "Evolved Dedicated Server\\ShooterGame), SERVER_ROOT is that "
                       "nested folder, not C:\\ARKServer."),
            ("bullet", "   Leaving the SERVER_ROOT field (once it's set) also runs a Quick "
                       "scan on its own, filling in PLUGINS_DIR / ipc_dir / game_ini and "
                       "possibly suggesting CLUSTERDIR / SAVESROOT / BACKUPROOT. These are "
                       "typically correct - it's recommended to accept them."),
            ("bullet", "   Scan for paths shows its suggestions in a popup - nothing is "
                       "filled in until you click the suggested option there. If a "
                       "suggestion looks clearly wrong, close the popup and use Browse to "
                       "set that path by hand instead."),
            ("bullet", "   A scan that turns up a lot of candidate folders gives you a "
                       "scrollable popup rather than a list running off the bottom of the "
                       "screen, so use the scrollbar or your mouse wheel to reach the "
                       "suggestions further down."),
            ("bullet", "   If something wasn't found, pick a higher \"Scan intensity\" "
                       "in the dropdown to the left of the button (it's chosen before you "
                       "scan) and click \"Scan for paths\" again. Quick only checks the "
                       "exact expected sub-paths and is instant; Thorough also searches a "
                       "few levels under SERVER_ROOT, under ShooterGame\\Saved and beside "
                       "SERVER_ROOT (a few seconds); Exhaustive searches much deeper and "
                       "additionally sweeps your Desktop, Documents and Downloads folders "
                       "- for servers extracted somewhere odd - and can be slow, "
                       "the launcher stays usable while scanning."),

            ("bullet", "5. Setup Status tab -> click Re-check and confirm everything shows "
                       "a checkmark before going further (the connector.ini row is only a "
                       "yellow \"i\", not an X - it's just the optional standalone "
                       "connector fallback, ignore it unless you're using that instead of "
                       "the in-game integrated connector). Anything showing an X has a hint "
                       "telling you what to fix. This is the fastest way to catch a missed "
                       "step before you start troubleshooting in-game. A yellow \"i\" "
                       "anywhere in the app is advisory only, and is typically nothing to "
                       "worry about."),
            ("bullet", "   Two of the rows there catch mistakes that only show up much "
                       "later, so come back to this tab after any change: "
                       "\"Configuration is saved into the server scripts\" (a field you "
                       "typed but never Saved isn't in paths.cmd, so Quick Launch will "
                       "refuse to start the server) and \"Mods tab matches ActiveMods on "
                       "disk\" (a mod ticked but never Saved never loads on the server). "
                       "Both are red X rows with the button that fixes them named in the "
                       "hint."),

            ("bullet", "6. Generate the Archipelago room. This guide won't explain how "
                       "YAMLs and Archipelago work - this isn't a beginner-friendly "
                       "Archipelago setup. Just remember to set up your yaml, remember your "
                       "yaml name, and drop the .apworld into Archipelago's custom worlds. "),
            ("bullet", "   The Archipelago Setup tab does all of this without leaving the "
                       "launcher: set (or \"Scan for Archipelago\") your Archipelago "
                       "directory, then \"Update .apworld\" to fetch the latest %s "
                       "straight into custom_worlds (no manual download - your old copy "
                       "is backed up, not overwritten), and \"Open Options Creator (YAML)\" "
                       "to build your yaml - inside it, click \"Export Options\" in the "
                       "top right and save it into the Players folder inside your "
                       "Archipelago directory (\"Open Players folder\" opens that folder "
                       "for you). Then \"Generate seed\" builds the multiworld; once it "
                       "finishes, \"Open output folder\" is where the generated seed .zip "
                       "lands." % APWORLD_ASSET_NAME),

            ("bullet", "   Now you need a room to actually host that seed. There are two "
                       "ways, and they are alternatives - pick ONE, don't do both:"),
            ("bullet", "   (a) archipelago.gg - upload the generated .zip to the website "
                       "and it hosts the room for you. Nothing to keep running on your PC, "
                       "no router setup, and it gives you the server address to put in "
                       "step 7. This is the easier path and what most people do."),
            ("bullet", "   (b) Host it yourself - \"Host local Archipelago server\" in the "
                       "Launch section runs Archipelago's own server on this PC. It asks "
                       "which generated seed to open (offering the newest one from your "
                       "output folder), then opens it in its own console window. That "
                       "window IS the room: its output appears there, it's where you type "
                       "commands like /send, and closing it ends the room. It also offers "
                       "to fill the server field in step 7 with your local address, so "
                       "there's nothing to copy by hand."),
            ("bullet", "   Optional, and only if you want a live map of your checks: the "
                       "\"PopTracker (tracker)\" group on the same tab sets up PopTracker "
                       "and the %s in one click (\"Download PopTracker\" if you haven't got "
                       "it, otherwise point the directory field at your copy and click "
                       "\"Install/update %s\"). It changes nothing about the server or the "
                       "seed - see the Archipelago Setup entry under \"What each tab "
                       "does\" below." % (TRACKER_PACK_LABEL, TRACKER_PACK_LABEL)),

            ("bullet", "   The catch with (b) is reachability. You connect to your own "
                       "room at localhost, but everybody else connects to YOUR public IP "
                       "address - and for anyone outside your home network that only works "
                       "if you forward the Archipelago port (default %d, TCP) to this PC "
                       "in your router's settings. Other players in your own house need "
                       "no forwarding. If port forwarding isn't something you want to deal "
                       "with, use archipelago.gg." % ARCHIPELAGO_DEFAULT_PORT),
            ("bullet", "   Hosting locally uses the port from Archipelago's own host.yaml "
                       "(and the password from the field in step 7, if you set one). "
                       "Change the port there, not on the command line, if you need a "
                       "different one."),

            ("bullet", "7. Archipelago Setup tab -> in the \"Archipelago room (Connector "
                       "settings)\" group, fill in server, slot and password with your "
                       "Archipelago room info. (These used to be on the Configuration tab - "
                       "they moved here, next to the Archipelago tooling that uses them.) "
                       "Your slot must match the name in your yaml exactly, including "
                       "capitalisation. Then click \"Copy ARK connection command\" - that's "
                       "what you'll paste in-game."),
            ("bullet", "   If you hosted locally in step 6, server is already filled in for "
                       "you (it asks first if you'd already typed something there). You "
                       "still set slot yourself - the launcher can't know which yaml in "
                       "the seed is yours."),
            ("bullet", "   The command is built in the form: /connect server slotname "
                       "password - server FIRST, then your slot name, then the password. "
                       "If your room has no password the command simply ends after the "
                       "slot name. (The in-game command order changed - if you have an old "
                       "one written down that starts with your slot name, it won't work.)"),
            ("bullet", "   You don't have to paste anything into the Archipelago Text "
                       "Client, though: \"Open Text Client\" on this same tab opens it "
                       "already connected using these fields."),

            ("bullet", "8. Quick Launch -> \"Run start_ase_server\" to launch the server. "
                       "It can take a few minutes depending on your SSD/HDD speed. Confirm "
                       "in the console that the plugin has loaded (or check the Debug Log "
                       "tab for the LOAD line)."),
            ("bullet", "   Wait for the console window to finish printing its startup "
                       "messages - the scrolling settles down - before assuming something's "
                       "wrong. Don't click inside the console window while it's starting."),
            ("bullet", "   If the console window's title bar starts with the word "
                       "\"Select\", the console has frozen - a normal Windows quirk from "
                       "clicking or dragging inside it. Press Enter to unfreeze it."),

            ("bullet", "9. In ARK: Survival Evolved, go to LAN and look for your session "
                       "name (default: ArchipelagoSolo). Join, spawn your character, open "
                       "in-game chat, and paste the connection command from step 7."),

            ("bullet", "10. You should be good to go! (If you enabled randomized dino "
                       "spawns, see the bottom of these instructions.) "
                       "Quick test: level up and see if a "
                       "check goes out. To test check-in: in the host's server console (the "
                       "ArchipelagoServer window, or the web room's command box) run "
                       "/send ARCHIPELAGONAME Engram: Compass - within a few seconds it "
                       "should unlock in your engrams. If not, uh oh "),

            ("bullet", "Any issues: check the Debug Log tab first, then the Discord or "
                       "GitHub to search for or report them."),

            ("h1", "What each tab does"),
            ("bullet", "Listed in tab order, left to right."),
            ("bullet", "Configuration - every Locations / Paths / Network / Plugin files & "
                       "DeathLink / Cluster field, the Quick Launch buttons, and Save / "
                       "Reload from files. The Paths group also holds \"Scan intensity\" + "
                       "\"Scan for paths\" (all path detection in one button) and \"Create "
                       "ServerCluster folders\". Note: server / slot / password are no "
                       "longer here - they're on the Archipelago Setup tab."),
            ("bullet", "Install Server/Api/Plugin - the three installers, in order: \"Install ARK "
                       "Server\" (SteamCMD, ~18gb), \"Install ArkServerApi\" (downloads + "
                       "extracts the latest ArkApi into Win64), and \"Install Plugin\" "
                       "(downloads the latest ArkAP_Plugin.zip from GitHub and installs it "
                       "into ArkApi\\Plugins). \"Manual downloads\" at the bottom is only a "
                       "fallback if an automated download fails (or you want a specific "
                       "older plugin version), plus the ArkConnector, which still needs "
                       "downloading by hand."),
            ("bullet", "Archipelago Setup - a built-in quick launcher for your own "
                       "Archipelago installation (the separate app that hosts the room and "
                       "builds the yaml), plus the Connector settings that used to live on "
                       "the Configuration tab."),
            ("bullet", "   \"Archipelago installation\" group - the Archipelago directory "
                       "field with Browse, and the \"Scan for Archipelago\" button "
                       "directly under it (they're grouped together so setting the folder "
                       "and finding it are one thing). Default install location is %s. "
                       "The scan checks the common locations first and only falls back to "
                       "a wider drive scan if none match, so the usual case is instant. A "
                       "folder is only accepted if it contains all of %s - a half-copied "
                       "or unrelated folder is never taken."
                       % (ARCHIPELAGO_DEFAULT_DIR, ", ".join(ARCHIPELAGO_REQUIRED_EXES))),
            ("bullet", "   \"Archipelago room (Connector settings)\" group - server, slot "
                       "and password, moved here from the Configuration tab because they "
                       "describe your Archipelago room rather than your ARK server, along "
                       "with the \"Copy ARK connection command\" and \"Copy port\" buttons "
                       "that use them. They're still saved, still written to connector.ini, "
                       "and still included in Profiles exactly as before - only their "
                       "location in the app changed."),
            ("bullet", "   The whole tab persists between sessions: the Archipelago "
                       "directory, server, slot and password are all saved with your "
                       "config and travel with your profiles, so the tab comes back "
                       "exactly as you left it. It has its own Save button, which lights "
                       "up only when one of those four fields has been changed."),
            ("bullet", "   \"Launch\" group - \"Open Text Client\", \"Open Options Creator "
                       "(YAML)\", \"Generate seed\" and \"Open Archipelago Launcher\" (a "
                       "general entry point to everything else Archipelago ships). A note "
                       "at the top of the group points out that Open Text Client fills in "
                       "your room details automatically, and that these apps can take a "
                       "couple of seconds to appear - that's normal, not a hang. Each "
                       "button greys out if the Archipelago directory isn't set or that "
                       "particular .exe is missing, and its tooltip says which."),
            ("bullet", "   \"Folders\" group - shortcuts to custom_worlds (where the "
                       ".apworld goes), Players (where your yaml goes), output (where a "
                       "generated seed lands), and the Archipelago folder itself. A "
                       "missing folder is created rather than reported as an error."),
            ("bullet", "   \"ARK world (.apworld)\" group at the bottom - \"Update "
                       ".apworld\" downloads the latest %s from this project's GitHub "
                       "releases straight into custom_worlds, so you never have to "
                       "download and drag it in by hand. If a copy is already there it's "
                       "renamed to a timestamped .bak first rather than deleted, so "
                       "rolling back is just a rename. Restart Archipelago afterwards for "
                       "it to be picked up." % APWORLD_ASSET_NAME),
            ("bullet", "   \"PopTracker (tracker)\" group - optional, and entirely "
                       "separate from playing: PopTracker is a third-party app that shows "
                       "your checks and items on a map as you play, following the "
                       "multiworld automatically. It lives on this tab because it's "
                       "Archipelago tooling, not ARK server tooling."),
            ("bullet", "   There are two ways in, and you only need one. If you already "
                       "have PopTracker, point the \"PopTracker directory\" field at it "
                       "(Browse, or \"Scan for PopTracker\") - that's the folder holding "
                       "%s. If you don't, click \"Download PopTracker\": you pick where it "
                       "should go, and the launcher downloads the latest stable Windows "
                       "build, extracts it there, fills the field in for you, and installs "
                       "the ARK tracker pack into it in the same click. PopTracker is "
                       "~17 MB on its own, which is why it isn't bundled with the launcher."
                       % POPTRACKER_EXE),
            ("bullet", "   \"Install/update %s\" downloads the pack from its own GitHub "
                       "repository (%s) and puts it in PopTracker's \"%s\" folder. Any copy "
                       "already there is MOVED into a timestamped folder under "
                       "\"%s\" next to it, not deleted and not left inside %s - PopTracker "
                       "treats every folder in there as a pack, so a backup sitting beside "
                       "the new one would show up in its Load list as a second, older copy "
                       "of the same tracker."
                       % (TRACKER_PACK_LABEL, TRACKER_PACK_RELEASES_PAGE,
                          POPTRACKER_PACKS_DIRNAME, TRACKER_PACK_BACKUP_DIRNAME,
                          POPTRACKER_PACKS_DIRNAME)),
            ("bullet", "   \"Open PopTracker\" launches it already loaded onto the ARK "
                       "pack - no picking a pack inside it."),
            ("bullet", "   Connecting it to your room is automatic only on PopTracker %s or "
                       "newer, which is where its command-line room arguments were added. "
                       "An older build - including the current stable one, and therefore "
                       "the one \"Download PopTracker\" fetches - refuses to START when "
                       "given them, so the launcher doesn't pretend: it copies your room "
                       "address to the clipboard and tells you once where to paste it. In "
                       "PopTracker, click the grey \"AP\" in the top row, paste the host "
                       "and port, then enter your slot and password. It remembers the host "
                       "and slot for next time; the password it never stores."
                       % POPTRACKER_AP_ARGS_MIN_VERSION),
            ("bullet", "   That is a genuine limit of PopTracker, not something missing "
                       "here - the same situation as Archipelago's Options Creator further "
                       "up. There is no settings file to fill in either: PopTracker does "
                       "remember a host and slot, but only as the defaults for that dialog "
                       "(nothing connects from them), and the password isn't stored "
                       "anywhere at all. Loading the pack, which is the part that can be "
                       "automated, is automated."),
            ("bullet", "   All three buttons grey out with the reason on their tooltip when "
                       "the PopTracker directory isn't set (\"Download PopTracker\" "
                       "deliberately doesn't - it's the one you press when nothing is set "
                       "yet), and the line beside the scan button always says where you "
                       "stand: no directory, folder found but no %s, or PopTracker found "
                       "with the tracker pack version that's installed. The directory is "
                       "saved with your config and travels with your profiles like every "
                       "other field on this tab - click Save after setting it."
                       % POPTRACKER_EXE),
            ("bullet", "   One limitation worth knowing: the Options Creator cannot be "
                       "opened straight onto ARK: Survival Evolved. It takes no "
                       "command-line arguments at all, so the button just opens it and you "
                       "pick ARK from its own game list. That's a limit of Archipelago, not "
                       "something this launcher is missing."),
            ("bullet", "Mods - download and activate Steam Workshop mods for the server "
                       "(see the \"Mods (Steam Workshop)\" section below)."),
            ("bullet", "Setup Status - a read-only checklist with hints for anything "
                       "showing an X. It covers the three installs and the plugin mode, "
                       "the cluster folders, the .mod files in Content\\Mods, and the two "
                       "things that go quietly out of step as you work: whether your "
                       "Configuration fields have actually been written out to the server "
                       "scripts, and whether the Mods tab agrees with the ActiveMods line "
                       "the server really reads. Click Re-check after fixing something. It "
                       "also shows advisory rows (a yellow \"i\", not a red X) for things "
                       "that are worth knowing but aren't broken - the BattlEye note, "
                       "connector.ini status (only relevant if you use the optional "
                       "standalone connector instead of the in-game integrated one), and "
                       "\"update available\" when a newer ArkServerApi or ArkAP plugin "
                       "release exists than the one you installed (with a link to the "
                       "release). None of these are failures, so they never show an X. "
                       "These advisory rows are typically nothing to worry about."),
            ("bullet", "   The Setup Status tab has a small coloured symbol next to its "
                       "name in the tab bar, so you can see your overall status from any "
                       "tab without opening it: a green check = everything passes, a "
                       "yellow \"i\" = no failures but at least one advisory (BattlEye, or "
                       "a newer component version), a red X = at least one hard failure. "
                       "It updates whenever the checks re-run. A yellow \"i\" is advisory "
                       "only, and is typically nothing to worry about."),
            ("bullet", "Profiles - save/load named snapshots of every Configuration field "
                       "plus the Archipelago Setup room fields (e.g. \"Solo Test\" vs "
                       "\"Friend Group Run\") plus a free-text notes box, stored separately "
                       "from your live config. Loading a profile only fills the fields in - "
                       "it never saves/applies by itself, so press Save on the "
                       "Configuration tab afterward."),
            ("bullet", "   On first run a profile named \"%s\" is created from your "
                       "starting Configuration values and loaded straight away, so your "
                       "settings are backed by a real profile from the very beginning "
                       "instead of only the live config. It's a normal profile - rename, "
                       "update or delete it as you like." % DEFAULT_PROFILE_NAME),
            ("bullet", "   The list also contains an \"Autosave\" profile the launcher "
                       "writes by itself every 10 minutes while the app is open. It "
                       "always holds only the newest snapshot, never touches the "
                       "profiles you save yourself, and can't be renamed or updated by "
                       "hand - load it if you ever need to get recent settings back."),
            ("bullet", "Debug Log - a viewer for every log that matters, with a search "
                       "box, \"Jump to latest\", and \"Refresh\". Pick which log with the "
                       "\"Log:\" dropdown at the top; the search box and both buttons act "
                       "on whichever one is showing. Check here first when checks or "
                       "items aren't coming through."),
            ("bullet", "   ArkAP plugin log (ArkAP_debug.log) - the plugin's own log, "
                       "written inside your ArkApi\\Plugins\\ArkAP folder while the server "
                       "runs. This is the one to read when checks or items aren't moving "
                       "between ARK and Archipelago."),
            ("bullet", "   Launcher log (%s) - what this app itself did, timestamped and "
                       "kept across restarts: server / ArkApi / plugin / .apworld / "
                       "tracker installs and updates, path scans and what they found, "
                       "saves and what changed, ServerCluster folder creation, resets and "
                       "their verification, mod downloads and ActiveMods writes, Game.ini "
                       "patches and uploads, and every error the app showed you. It sits "
                       "next to arkap_launcher_config.json, is appended to rather than "
                       "overwritten, and is capped in size so it can't grow forever."
                       % LAUNCHER_LOG_FILENAME),
            ("bullet", "   Launcher crash log (%s) - the full traceback from any "
                       "unexpected launcher error. Missing until something goes wrong, "
                       "which is exactly as it should be." % CRASH_LOG_FILENAME),
            ("bullet", "   ARK server log (ShooterGame.log) - ARK's own log, from "
                       "<SERVER_ROOT>\\ShooterGame\\Saved\\Logs. This is where a "
                       "LowLevelFatalError server crash lands; search for that word."),
            ("bullet", "   SteamCMD console / workshop / content logs - what SteamCMD "
                       "itself recorded while downloading the server or a mod. Worth a "
                       "look when a mod download fails or finishes suspiciously fast."),
            ("bullet", "   A log that doesn't exist yet (no crashes, the server never "
                       "installed) says so and explains why, rather than showing an empty "
                       "box. Very large logs - ShooterGame.log usually is - load only "
                       "their newest part, with a line at the top saying so, so switching "
                       "to one doesn't hang the app."),
            ("bullet", "Instructions - this tab, with a Quick Guide and this Full Guide. "
                       "Switch between them with the button in the top right."),

            ("h1", "Saving, and what a highlighted Save button means"),
            ("bullet", "Save buttons light up in a yellow halo only while something on "
                       "screen differs from what is already written to disk. A highlighted "
                       "Save button is a reliable signal that you have unsaved changes; a "
                       "plain one means the fields and the files already agree, and "
                       "pressing it would change nothing."),
            ("bullet", "There are three of them, and each lights up only for its own "
                       "fields: Configuration (every path, port and cluster field), "
                       "Archipelago Setup (the directory plus server / slot / password), "
                       "and Mods (which mods are ticked, and their order). Editing a path "
                       "therefore never lights up a tab you haven't touched. A short "
                       "reminder also appears above the tab strip while anything anywhere "
                       "is unsaved."),
            ("bullet", "Saving matters more than it looks, because nothing else in the app "
                       "reads your typed values. The .bat scripts read paths.cmd, the "
                       "server reads GameUserSettings.ini, and Save is what puts your "
                       "values into those files. Until then the old values are still in "
                       "there, doing exactly what they did before."),
            ("bullet", "Two safety nets catch it if you forget. Run start_ase_server "
                       "compares every field against what is really in paths.cmd and "
                       "refuses to launch rather than starting the server on stale paths. "
                       "And the Setup Status tab shows both cases as red X rows up front: "
                       "\"Configuration is saved into the server scripts\" and \"Mods tab "
                       "matches ActiveMods on disk\"."),

            ("h1", "Check for Updates (top of the window)"),
            ("bullet", "The button tracks four components: the launcher itself, the ArkAP "
                       "plugin, the .apworld, and the %s. All four are compared against "
                       "their latest GitHub releases (the tracker pack's own repo is a "
                       "different one from the plugin's, and is only compared once you've "
                       "set a PopTracker directory for it to look in)." % TRACKER_PACK_LABEL),
            ("bullet", "The check also runs by itself every time the launcher starts, "
                       "quietly in the background. Anything it can't reach is left out "
                       "silently rather than shown as an error, so a machine with no "
                       "internet never gets nagged."),
            ("bullet", "When something newer exists the button gets a small marker and is "
                       "highlighted, using the same halo as the Save buttons. The "
                       "highlight clears once you have clicked through and seen that "
                       "particular release. Each component is tracked separately, so "
                       "clicking through a plugin update never dismisses an .apworld "
                       "update you haven't seen yet."),
            ("bullet", "The dialog lists each component with the version you have and the "
                       "version available. The launcher can update itself in place; the "
                       "other three each name the button that installs them "
                       "(\"Install Plugin\" on the Install Server/Api/Plugin tab, and "
                       "\"Update .apworld\" / \"Install/update %s\" on the Archipelago "
                       "Setup tab). A version that "
                       "couldn't be worked out is spelled out as such, so a blank line "
                       "never gets mistaken for \"up to date\"." % TRACKER_PACK_LABEL),
            ("bullet", "The same lookups feed the \"update available\" advisory rows on "
                       "Setup Status, so the button and that tab can never disagree about "
                       "what the latest release is."),

            ("h1", "Mods (Steam Workshop)"),
            ("bullet", "The Mods tab downloads Steam Workshop mods with the bundled "
                       "SteamCMD and installs them into "
                       "SERVER_ROOT\\ShooterGame\\Content\\Mods for you - no manual "
                       "unzipping or .mod-file wrangling. It needs SERVER_ROOT set and the "
                       "ARK server already installed (the tab greys out with a note "
                       "otherwise)."),
            ("bullet", "Each row has a checkbox (checked = the mod should be active) and an "
                       "install icon (green check = actually installed on disk, red X = "
                       "not). Both reflect the real on-disk state - the checkbox mirrors "
                       "GameUserSettings.ini's ActiveMods, the icon mirrors what's in "
                       "Content\\Mods - and Refresh re-reads both."),
            ("bullet", "Order matters: mods load top-to-bottom (the topmost is highest "
                       "priority, matching the ActiveMods order ARK reads). Use the up/down "
                       "arrows to reorder."),
            ("bullet", "Checking/unchecking a mod only edits what's shown on screen - it "
                       "doesn't touch the server by itself. The yellow-highlighted Save "
                       "button (same highlight as the Configuration tab's) writes exactly "
                       "that checked/unchecked mixture into GameUserSettings.ini's "
                       "ActiveMods. A checked mod that isn't installed yet is left inactive "
                       "and flagged in the log - Download checked installs it first."),
            ("bullet", "   Setup Status carries a \"Mods tab matches ActiveMods on disk\" "
                       "row for exactly this: it reads the ActiveMods line back off disk "
                       "and compares it against your ticks, order included (order is load "
                       "priority, so a reordered list is a different setup), and shows a "
                       "red X naming the mods that differ - by name and ID, and which way "
                       "round: ticked but unsaved (the server never loads it), active but "
                       "not ticked (the server loads it anyway), or ticked but never "
                       "downloaded."),
            ("bullet", "Download checked - downloads every checked-but-not-installed mod, "
                       "then activates the checked set (applies right away, same as Save). "
                       "Check all / Uncheck all (above the list) - tick or untick every "
                       "mod's checkbox, same as clicking each one by hand; nothing is "
                       "applied until you press Save or Download checked afterwards. "
                       "Uninstall unchecked - deletes the files for unchecked mods to free "
                       "space. Verify/Redownload - re-fetches a selected mod if it looks "
                       "corrupt. Restart the ARK server after any of these for it to take "
                       "effect."),
            ("bullet", "Every row is tagged \"apworld ✓\" or \"apworld ✗\" next to "
                       "its install icon. Two different questions: the icon is whether the "
                       "mod is on disk, the tag is whether the .apworld knows it. Only "
                       "\"apworld ✓\" mods can appear in your YAML's mod_ids."),
            ("bullet", "Rename mod... - a mod you added yourself shows up as its raw "
                       "Workshop ID; select it and rename it to something you'll "
                       "recognise. Display only, and it sticks across restarts and "
                       "profile loads. \"apworld ✓\" mods keep their real names."),
            ("bullet", "Copy IDs for YAML - copies your checked mods' IDs to the clipboard as "
                       "a comma-separated list, in the list's top-to-bottom order, ready to "
                       "paste into the plugin's YAML mod configuration. Only the "
                       "\"apworld ✓\" ones are copied: the apworld only accepts IDs it "
                       "ships engram data for, and one it doesn't know fails generation "
                       "outright (\"mod_ids lists <id>, which this apworld doesn't know\"). "
                       "Anything left out is listed for you when you copy, and if nothing "
                       "is copyable the launcher says so rather than copying an empty "
                       "line."),
            ("bullet", "Leaving an ID out of mod_ids doesn't disable the mod - it still "
                       "downloads, still goes into ActiveMods, still loads on the server. "
                       "It just gets no Archipelago engrams or checks."),
            ("bullet", "Add mod - adds a raw Workshop ID that isn't in the supported list. "
                       "Only the pre-populated (supported) mods are known to integrate with "
                       "the plugin's checks/items; an added mod is tagged \"apworld ✗\" "
                       "and may just run as a normal ARK mod with no Archipelago "
                       "integration."),

            ("h1", "Search (top left of the window)"),
            ("bullet", "Type a term and press Enter to search field labels, tooltips, "
                       "button text, and this Instructions tab across every tab at once."),
            ("bullet", "Find Next / Find Prev cycle through all matches, switching tabs "
                       "automatically and centering the match on screen."),

            ("h1", "Quick launch (bottom of the Configuration tab)"),
            ("bullet", "The buttons are ordered by how often they get used - the top row is "
                       "the everyday four, the rest follow below."),
            ("bullet", "Run start_ase_server - launches the main ARK server."),
            ("bullet", "Open SERVER_ROOT / Open Game.ini folder / Open ipc folder / Open "
                       "Plugins folder / Open ClusterDir folder - open the matching folder "
                       "in Explorer."),
            ("bullet", "Run switch_map - currently unsupported, so don't rely on it "
                       "working yet."),
            ("bullet", "Patch Game.ini for randomized creatures - applies the plugin's "
                       "ipc\\game_ini_fragment.txt into your Game.ini (backed up first) so "
                       "randomized creatures take effect. Stop the ARK server first."),
            ("bullet", "Reset AP data (keep world save) - deletes every Archipelago "
                       "tracking file the plugin and connector generate (both incoming "
                       "items AND outgoing checks). Note: if the character/world isn't "
                       "also reset, level/inventory checks re-send immediately."),
            ("bullet", "Full reset for new seed - does the above AND backs up + wipes the "
                       "world save (SavedArks, your per-map saves and the cluster tribute "
                       "data). It also removes the randomized-creatures block from Game.ini "
                       "if this launcher added one (backed up first), so a fresh seed "
                       "doesn't inherit the previous seed's dino randomization. Backups are "
                       "moved aside with a timestamp, never deleted. Use this when joining a "
                       "new seed. Stop the ARK server (and the connector) first."),
            ("bullet", "   It no longer just says \"done\" and hopes. Every backup is "
                       "checked to confirm it actually received files (an empty one is "
                       "flagged, not counted), then it re-scans every live save location "
                       "afterwards and fails loudly if any world or character file "
                       "survived. If nothing at all was found to reset you get a warning "
                       "rather than a success - from your side that's a reset that didn't "
                       "happen, and you should run tools\\diagnose_reset.bat before "
                       "starting the server. Only a run with no problems AND at least one "
                       "save actually wiped reports success."),

            ("h1", "Uploading your own Game.ini / GameUserSettings.ini"),
            ("bullet", "Configuration tab -> \"Upload server config files\" (below the "
                       "field groups). Point a row at your own copy of Game.ini and/or "
                       "GameUserSettings.ini and click \"Upload to server\" to copy it "
                       "into <SERVER_ROOT>\\ShooterGame\\Saved\\Config\\WindowsServer, "
                       "replacing the server's."),
            ("bullet", "   Each file it replaces is backed up first, alongside the "
                       "original with a timestamp in the name - nothing is deleted, so "
                       "you can always put the old one back by renaming it."),
            ("bullet", "   Stop the ARK server first. ARK rewrites its config files "
                       "(GameUserSettings.ini especially) when it shuts down, so "
                       "anything uploaded while it's running is likely to be lost - "
                       "you'll get a warning if the server is up. Restart the server "
                       "afterwards for the new settings to apply."),

            ("h1", "What the path fields feed"),
            ("bullet", "SERVER_ROOT / SAVESROOT / CLUSTERDIR / BACKUPROOT / CLUSTERID / "
                       "ADMINPASS / SERVERPASS all write into a single file, paths.cmd - "
                       "start_ase_server.bat, switch_map.bat, start_transfer_server.bat, "
                       "and reset_ark_test.bat all read it from there, so they can never "
                       "disagree. apply_server_config.bat keeps its own SERVER_ROOT copy. "
                       "MAP / SESSION / MAXPLAYERS / ports / TRIBUTEEXP write only into "
                       "start_ase_server.bat, since those are per-script settings."),
            ("bullet", "Connector fields also write into connector.ini, for the optional "
                       "standalone connector fallback. That's still all seven of them - "
                       "server / slot / password (now on the Archipelago Setup tab) and "
                       "death_link / ipc_dir / data_dir / game_ini (Configuration tab, "
                       "\"Plugin files & DeathLink\" group). Splitting them across two tabs "
                       "changed nothing about what gets written or where."),
            ("bullet", "server / slot / password additionally feed the \"Copy ARK "
                       "connection command\" button (/connect server slotname password) "
                       "and the \"Open Text Client\" button, which passes them straight to "
                       "Archipelago's text client so it opens already connected."),
            ("bullet", "The Archipelago directory field is the one path here that feeds "
                       "nothing on disk - no .bat, no .ini. It only tells the launcher "
                       "where your Archipelago install is so the Archipelago Setup tab's "
                       "buttons know what to open, and where custom_worlds is for "
                       "\"Update .apworld\". It is saved with your config and profiles "
                       "like everything else."),
            ("bullet", "The PopTracker directory is the same kind of field: nothing on "
                       "disk reads it, it just tells the launcher which PopTracker to open "
                       "and which \"%s\" folder the %s belongs in. Also saved with your "
                       "config and profiles."
                       % (POPTRACKER_PACKS_DIRNAME, TRACKER_PACK_LABEL)),
            ("bullet", "Save only rewrites the one matching line for each field in each "
                       "file - everything else in the script is left untouched."),

            ("h1", "Reporting a problem (diagnostics & crash log)"),
            ("bullet", "Export diagnostics - a button next to Save / Reload on the "
                       "Configuration tab. It bundles everything someone helping you "
                       "would otherwise have to ask for, one question at a time, into a "
                       "single .zip. It saves to your Desktop by default (you pick where) "
                       "and opens the folder when it's done. Drag that zip straight into "
                       "Discord or attach it to a GitHub issue - it's the fastest way to "
                       "get diagnosed."),
            ("bullet", "What's in the zip: a text summary of the Setup Status checks; a "
                       "versions file (launcher, ArkAP plugin, .apworld, the %s and "
                       "ArkServerApi in one place); your config; your Archipelago .yaml, "
                       "found by "
                       "reading Archipelago's Players folder and matching the name inside "
                       "each file against your slot (so the filename doesn't matter); "
                       "Archipelago's host.yaml; paths.cmd; Game.ini and "
                       "GameUserSettings.ini from your server's "
                       "WindowsServer config folder; the plugin's ArkAP.config.json; "
                       "ArkAP_debug.log; the launcher's own activity log "
                       "(arkipelago_launcher.log - usually the most useful single file, since "
                       "it's a timestamped record of everything the app did); ARK's own "
                       "ShooterGame.log; the crash log if "
                       "there is one; SteamCMD's own console / workshop / content logs "
                       "under steamcmd\\ (what it recorded while downloading, which is "
                       "more than it printed on screen); your Mods tab state and output "
                       "log; the contents of "
                       "the whole ipc folder (session.json, state.json, the jsonl "
                       "exchanges, flags.json, game_ini_fragment.txt and the per-player "
                       "ipc\\<CharacterName> mailboxes a multiplayer server creates), kept "
                       "under an ipc\\ folder in the zip so it's obvious where each file "
                       "came from; and a listing "
                       "of the ipc folder and ShooterGame\\Content\\Mods showing each "
                       "file's size and date (a 0-byte .mod file crashes the server and "
                       "there is no other way to spot it - which is why the listing stays "
                       "even though the files themselves are now included)."
                       % TRACKER_PACK_LABEL),
            ("bullet", "Passwords are removed from every file in the zip, not just the "
                       "config - ADMINPASS and SERVERPASS in paths.cmd, "
                       "ServerAdminPassword / ServerPassword / SpectatorPassword in "
                       "GameUserSettings.ini, and any room password in your yaml all show "
                       "as [REDACTED]. The setting name stays visible so the file still "
                       "reads normally; everything that isn't a password is kept as-is."),
            ("bullet", "If a log is huge (ShooterGame.log usually is), only its last "
                       "5000 lines go in, with a line at the top saying it was cut - the "
                       "error is nearly always at the end, and this keeps the zip small "
                       "enough to upload. The ipc files are cut the same way at 500 lines, "
                       "since they grow for as long as you play. If a file can't be found, "
                       "the zip says where it was looked for instead of leaving it out "
                       "silently."),
            ("bullet", "Crash log - if the launcher ever hits an unexpected error it shows "
                       "a \"Something went wrong\" message and writes the full details to "
                       "arkap_launcher_crash.log, saved next to the launcher's .exe (the "
                       "same folder as arkap_launcher_config.json). It's kept across "
                       "restarts (new crashes are appended, with a size cap so it can't "
                       "grow forever), so it survives even if the app crashes again before "
                       "you get to report the first one. When reporting a crash, attach "
                       "that file - or just use \"Export diagnostics\", which already "
                       "includes it. You can also read it without leaving the app: Debug "
                       "Log tab, \"Log:\" dropdown, \"Launcher crash log\"."),

            ("h1", "Other Information"),
            ("bullet", "If you want to restart your world for a new Archipelago seed, "
                       "click \"Full reset for new seed\" under Quick Launch (stop the ARK "
                       "server and the connector first)."),
            ("bullet", "If you randomized dinos, stop the ARK server and click \"Patch "
                       "Game.ini for randomized creatures\" under Quick Launch. It applies "
                       "the plugin's ipc\\game_ini_fragment.txt into your Game.ini for you "
                       "(backing it up first, and merging into an existing "
                       "[/script/shootergame.shootergamemode] section rather than "
                       "duplicating it) - no more copy-pasting it by hand. Restart the "
                       "server afterwards. (The fragment only exists once you've connected "
                       "to the server at least once on a randomized seed.)"),

        ]

        quick_content = [
            ("bullet", "This is a simplified version of the Instructions. It is being "
                       "tested to see if it's easier to follow. The full version is "
                       "available by pressing Full Guide in the top right."),
            ("bullet", "Most options have tooltips, just hover over one."),
            ("bullet", "Use the Search bar at the top left to find anything in the app."),
            ("bullet", "THIS LAUNCHER WILL NEVER TOUCH YOUR ACTUAL ARK DOWNLOAD LOCATION "
                       "PLEASE DONT SET ANY PATH TO YOUR ARK GAME INSTALL, i beg you"),
            ("bullet", "Click the box beside a header to collapse or expand it. The Expand "
                       "all and Collapse all buttons do the whole guide."),

            ("h1", "Start here - install in this order"),
            ("bullet", "Do these steps in order. Each step needs the one before it."),

            ("bullet", "1. Open the Install Server/Api/Plugin tab. Set SERVER_ROOT. Click "
                       "\"Install ARK Server\"."),
            ("bullet", "   You can install it anywhere. A short path near the top of a "
                       "drive, like C:\\ark\\, keeps things simple."),
            ("bullet", "   The download is about 18gb. Progress shows in the console box. "
                       "Wait for it to finish."),
            ("bullet", "   Set your ARK: Survival Evolved game to the preaquatica branch. "
                       "You cannot join the server otherwise."),
            ("bullet", "   The cluster folders are created and filled in for you when it "
                       "finishes. Go to the Configuration tab and click Save."),

            ("bullet", "2. Stay on the Install Server/Api/Plugin tab. Click \"Install "
                       "ArkServerApi\". Wait for it to finish."),
            ("bullet", "   Your ARK game also needs BattlEye off. In Steam, right-click "
                       "ARK: Survival Evolved, open Properties, then Launch Options, and "
                       "add -NoBattlEye."),

            ("bullet", "3. Stay on the same tab. Find the \"Install/update ArkAP Plugin\" "
                       "box. Click \"Install Plugin\". Wait for it to finish."),

            ("bullet", "4. Open the Configuration tab. In the Paths group, click \"Scan for "
                       "paths\". Accept the paths it fills in. Click Save."),
            ("bullet", "   SERVER_ROOT is the folder that contains ShooterGame."),
            ("bullet", "   The scan shows its suggestions in a popup. Click a suggestion to "
                       "accept it. If one looks wrong, close the popup and use Browse to "
                       "set that path yourself."),
            ("bullet", "   The popup scrolls if the scan found a lot of folders. Use the "
                       "scrollbar or your mouse wheel."),

            ("bullet", "5. Open the Setup Status tab. Click Re-check."),
            ("bullet", "   Every row should show a green checkmark before you carry on."),
            ("bullet", "   A yellow \"i\" is advisory only and is typically nothing to "
                       "worry about. A red X tells you what to fix."),
            ("bullet", "   Come back here after any change. It also checks that your "
                       "settings were saved into the server scripts, and that your ticked "
                       "mods match what the server will really load."),

            ("bullet", "6. Set up your Archipelago room. This guide assumes you already know "
                       "how Archipelago and YAMLs work."),
            ("bullet", "   Open the Archipelago Setup tab. Set your Archipelago directory, "
                       "or click \"Scan for Archipelago\"."),
            ("bullet", "   Click \"Update .apworld\" to install %s." % APWORLD_ASSET_NAME),
            ("bullet", "   Click \"Open Options Creator (YAML)\" to build your yaml. Pick "
                       "ARK from its game list."),
            ("bullet", "   Click \"Export Options\" in the top right of the Options "
                       "Creator to save your yaml."),
            ("bullet", "   Write down your slot name. You need it in step 7."),
            ("bullet", "   Click \"Open Players folder\" and put your yaml in there."),
            ("bullet", "   Click \"Generate seed\"."),
            ("bullet", "   Click \"Open output folder\" to find your generated seed."),

            ("bullet", "   Now host that seed. Two options - pick one, not both."),
            ("bullet", "   Option A: upload the .zip to archipelago.gg and let the website "
                       "host it. Easiest. It gives you the server address for step 7."),
            ("bullet", "   Option B: click \"Host local Archipelago server\" to host it on "
                       "this PC. Pick the seed when it asks. It opens in its own console "
                       "window - leave that window open, closing it ends the room."),
            ("bullet", "   Option B fills in the server field for you, so step 7 is just "
                       "your slot name. It asks first if you had already typed something "
                       "there."),
            ("bullet", "   Option B uses your room password from step 7, and the port from "
                       "Archipelago's own host.yaml. Change the port there if you need a "
                       "different one."),
            ("bullet", "   Option B warning: other players connect to your IP, not "
                       "localhost. Anyone outside your home network needs you to forward "
                       "port %d (TCP) to this PC on your router. Use archipelago.gg if "
                       "that sounds like a hassle." % ARCHIPELAGO_DEFAULT_PORT),

            ("bullet", "   Optional: a live tracker map. In the \"PopTracker (tracker)\" "
                       "group on the same tab, click \"Download PopTracker\" and pick a "
                       "folder. It downloads PopTracker, installs the %s into it, and "
                       "fills in the directory for you. Click Save."
                       % TRACKER_PACK_LABEL),
            ("bullet", "   Already have PopTracker? Set the \"PopTracker directory\" (or "
                       "click \"Scan for PopTracker\") and click \"Install/update %s\" "
                       "instead. Your old copy of the pack is moved aside."
                       % TRACKER_PACK_LABEL),
            ("bullet", "   Click \"Open PopTracker\" to open it on the ARK map."),
            ("bullet", "   PopTracker can't be opened already connected (that needs "
                       "PopTracker %s, which isn't out yet). Your room address is copied to "
                       "your clipboard instead."
                       % POPTRACKER_AP_ARGS_MIN_VERSION),
            ("bullet", "   In PopTracker, click the grey \"AP\", paste the address with "
                       "Ctrl+V, then type your slot and password. It remembers them for "
                       "next time, apart from the password."),

            ("bullet", "7. Stay on the Archipelago Setup tab. Find the \"Archipelago room "
                       "(Connector settings)\" group. Fill in server, slot and password."),
            ("bullet", "   Type your slot name exactly as it appears in your yaml. Capital "
                       "letters matter."),
            ("bullet", "   Click \"Copy ARK connection command\". You paste this in-game "
                       "later."),
            ("bullet", "   Click \"Open Text Client\" to open the Archipelago text client "
                       "already connected."),

            ("bullet", "8. Open the Configuration tab. Under Quick Launch, click \"Run "
                       "start_ase_server\"."),
            ("bullet", "   Wait for the console to finish printing its startup messages "
                       "before assuming something's wrong. Don't click inside the console "
                       "window while it's starting."),
            ("bullet", "   If the console's title bar starts with \"Select\", it has "
                       "frozen. Press Enter to unfreeze it."),

            ("bullet", "9. Start ARK: Survival Evolved. Open the LAN server list. Join your "
                       "session. The default name is ArchipelagoSolo."),
            ("bullet", "   Spawn your character. Open in-game chat. Paste the command from "
                       "step 7."),

            ("bullet", "10. You are done. Level up once to send your first check."),
            ("bullet", "   To test items, run /send ARCHIPELAGONAME Engram: Compass in your "
                       "Archipelago server console. The engram should unlock within a few "
                       "seconds."),
            ("bullet", "   Randomized dinos need one extra step. See \"Other information\" "
                       "below."),

            ("h1", "What each tab does"),
            ("bullet", "Configuration - all your settings, the Quick Launch buttons, and "
                       "Save."),
            ("bullet", "Install Server/Api/Plugin - the three installers, in order."),
            ("bullet", "Archipelago Setup - your Archipelago folder, your room details, and "
                       "buttons that open Archipelago's own tools."),
            ("bullet", "   It remembers every field between sessions, and they travel "
                       "with your profiles. The tab has its own Save button."),
            ("bullet", "   The \"PopTracker (tracker)\" group at the bottom is optional: it "
                       "sets up the PopTracker app and the ARK tracker pack, and opens the "
                       "tracker on the ARK map."),
            ("bullet", "Mods - download and turn on Steam Workshop mods."),
            ("bullet", "Setup Status - a checklist of your setup. Click Re-check after you "
                       "fix something."),
            ("bullet", "Profiles - save and load named copies of your settings. Click Save "
                       "on the Configuration tab after you load one."),
            ("bullet", "Debug Log - a viewer for your logs. The \"Log:\" dropdown at the "
                       "top picks which one: the ArkAP plugin's log, the launcher's own "
                       "log of what it did, the launcher crash log, ARK's ShooterGame.log "
                       "(where server crashes land), or SteamCMD's download logs. Search, "
                       "\"Jump to latest\" and \"Refresh\" work on whichever is showing, "
                       "and a log that doesn't exist yet says so."),
            ("bullet", "Instructions - this tab. Use the button in the top right to switch "
                       "to the Full Guide."),

            ("h1", "Saving your changes"),
            ("bullet", "A Save button glows yellow while something on screen is unsaved."),
            ("bullet", "A plain Save button means everything already matches what is "
                       "saved."),
            ("bullet", "There are three, and each one glows only for its own fields: "
                       "Configuration, Archipelago Setup, and Mods."),
            ("bullet", "Save matters. The server and the .bat scripts read your settings "
                       "from files, and Save is what writes them there."),
            ("bullet", "Forget to save and Run start_ase_server refuses to start, and "
                       "Setup Status shows a red X. Click Save and try again."),

            ("h1", "Check for Updates (top of the window)"),
            ("bullet", "It checks the launcher, the ArkAP plugin, the .apworld and the %s."
                       % TRACKER_PACK_LABEL),
            ("bullet", "It runs by itself every time you start the launcher."),
            ("bullet", "A marker and a highlight on the button mean something newer "
                       "exists. Click it to see what."),
            ("bullet", "The launcher updates itself. For the other three the dialog names "
                       "the button that installs them."),

            ("h1", "Mods (Steam Workshop)"),
            ("bullet", "The Mods tab installs Steam Workshop mods for you."),
            ("bullet", "Install the ARK server and set SERVER_ROOT first."),
            ("bullet", "Tick a mod to mark it active."),
            ("bullet", "Click \"Download checked\" to install and activate every ticked "
                       "mod."),
            ("bullet", "Mods load from top to bottom. Use the arrows to change the order."),
            ("bullet", "Click Save to write your ticked list to the server."),
            ("bullet", "Setup Status tells you if your ticks and the server's real mod "
                       "list have drifted apart."),
            ("bullet", "Restart the ARK server after any mod change."),
            ("bullet", "Click \"Copy IDs for YAML\" to copy your mod list for the plugin's "
                       "yaml. Only mods tagged \"apworld ✓\" are copied - the others "
                       "would stop your game generating. They still work on the server."),
            ("bullet", "Click \"Rename mod\" to give a mod you added yourself a name you "
                       "will recognise instead of a bare ID."),

            ("h1", "Search (top left of the window)"),
            ("bullet", "Type a word and press Enter."),
            ("bullet", "Click Find Next or Find Prev to step through the matches."),

            ("h1", "Quick launch (bottom of the Configuration tab)"),
            ("bullet", "Run start_ase_server - starts the server."),
            ("bullet", "The Open buttons open that folder in Explorer."),
            ("bullet", "Run switch_map - not supported right now."),
            ("bullet", "Patch Game.ini for randomized creatures - turns on randomized "
                       "dinos. Stop the server first."),
            ("bullet", "Reset AP data (keep world save) - clears your Archipelago progress "
                       "and keeps your world."),
            ("bullet", "Full reset for new seed - clears your Archipelago progress and "
                       "wipes your world save. Use it when you join a new seed. Stop the "
                       "server first. Your old save is backed up."),

            ("h1", "Uploading your own Game.ini / GameUserSettings.ini"),
            ("bullet", "Stop the ARK server first."),
            ("bullet", "Open the Configuration tab. Find \"Upload server config files\"."),
            ("bullet", "Pick your file. Click \"Upload to server\"."),
            ("bullet", "The file it replaces is backed up first."),
            ("bullet", "Restart the server."),

            ("h1", "What the path fields feed"),
            ("bullet", "The path fields write into the launcher's .bat and .ini files for "
                       "you."),
            ("bullet", "Click Save after you change any field."),
            ("bullet", "The Archipelago directory field only tells the launcher where "
                       "Archipelago is installed."),
            ("bullet", "The PopTracker directory field is the same - it only says where "
                       "PopTracker is, so the tracker pack goes in the right place."),

            ("h1", "Reporting a problem"),
            ("bullet", "Click \"Export diagnostics\" next to Save on the Configuration "
                       "tab."),
            ("bullet", "It saves one .zip and opens the folder. Post that zip on Discord "
                       "or attach it to a GitHub issue."),
            ("bullet", "Your yaml is found by reading the name inside each file in your "
                       "Players folder and matching it to your slot, so what the file is "
                       "called does not matter."),
            ("bullet", "The zip holds your Setup Status, your version numbers, your "
                       "config, your Archipelago .yaml, paths.cmd, Game.ini and "
                       "GameUserSettings.ini, the plugin's ArkAP.config.json, the debug, "
                       "crash and ShooterGame logs, everything in your ipc folder "
                       "(including each player's mailbox folder), and a list of your ipc "
                       "and Mods folders. That is everything anyone would ask you for."),
            ("bullet", "Every password in every one of those files is replaced with "
                       "[REDACTED] before it goes in. Very long logs are cut down to "
                       "their last 5000 lines, and the ipc files to their last 500, so "
                       "the zip stays small."),

            ("h1", "Other information"),
            ("bullet", "To start a new seed, click \"Full reset for new seed\" under Quick "
                       "Launch. Stop the server and the connector first."),
            ("bullet", "If you randomized dinos, stop the server. Click \"Patch Game.ini "
                       "for randomized creatures\" under Quick Launch. Restart the server."),
            ("bullet", "That button only works after you have connected to the server once "
                       "on a randomized seed."),

            ("h1", "If something goes wrong"),
            ("bullet", "The server install stopped with exit code 8. Click \"Install ARK "
                       "Server\" again."),
            ("bullet", "\"Scan for paths\" missed a path. Pick a higher \"Scan intensity\" "
                       "next to the button and scan again."),
            ("bullet", "Your cluster folders are missing. Click \"Create ServerCluster "
                       "folders\" in the Paths group on the Configuration tab."),
            ("bullet", "The connection command fails in-game. The order is /connect server "
                       "slot password. Copy it again from the Archipelago Setup tab."),
            ("bullet", "Checks or items are not coming through. Open the Debug Log tab "
                       "(it opens on the ArkAP plugin log)."),
            ("bullet", "The server closed by itself. Debug Log tab, switch the \"Log:\" "
                       "dropdown to \"ARK server log\" and search for LowLevelFatalError."),
            ("bullet", "Something the launcher did went wrong. Debug Log tab, \"Log:\" "
                       "dropdown, \"Launcher log\" - it lists what the app did, with times."),
            ("bullet", "The server will not start and it says something is unsaved. Open "
                       "the Configuration tab and click Save."),
            ("bullet", "A mod you ticked is not loading in game. Open the Mods tab, click "
                       "Save, and restart the server."),
            ("bullet", "Setup Status shows a red X. Read the hint on that row and fix that "
                       "one thing."),
            ("bullet", "Still stuck. Click \"Export diagnostics\" and post the zip on "
                       "Discord or GitHub."),
        ]

        self.instructions_text_quick = self._build_instruction_text(body, quick_content)
        self.instructions_text_full = self._build_instruction_text(body, full_content)
        self._instructions_mode = "quick"
        self.instructions_text_quick._container.pack(fill="both", expand=True)

    def _active_instructions_text(self):
        return (self.instructions_text_quick if self._instructions_mode == "quick"
                else self.instructions_text_full)

    def _toggle_instructions_mode(self):
        old = self._active_instructions_text()
        self._instructions_mode = "full" if self._instructions_mode == "quick" else "quick"
        new = self._active_instructions_text()
        old._container.pack_forget()
        new._container.pack(fill="both", expand=True)
        self.instructions_mode_btn.configure(
            text="Quick Guide" if self._instructions_mode == "full" else "Full Guide")

    def _build_instruction_text(self, parent, content):
        """Shared renderer for both guides. Returns the Text widget, with its own
        container frame (Text + Scrollbar) stashed as `_container` so the caller can
        pack/pack_forget the pair as a unit, and its three collapse-state maps stashed
        as `_instr_vars` so each guide keeps independent state (tags are per-widget, so
        the names can repeat between the two guides)."""
        container = ttk.Frame(parent)

        txt = tk.Text(container, wrap="word", font=("Segoe UI", 9), borderwidth=0,
                       highlightthickness=0, padx=10, pady=8, cursor="arrow",
                       background=self.theme["text_bg"], foreground=self.theme["text_fg"],
                       insertbackground=self.theme["text_fg"])
        vsb = ttk.Scrollbar(container, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt._container = container

        txt.tag_configure("h1", font=("Segoe UI", 12, "bold"), spacing1=10, spacing3=4)
        txt.tag_configure("body", font=("Segoe UI", 9), spacing3=2, lmargin1=2, lmargin2=2)
        txt.tag_configure("bullet", font=("Segoe UI", 9), spacing1=1, lmargin1=18, lmargin2=32)
        # Blank line between numbered steps - a small font keeps it a gap rather
        # than a full empty body line.
        txt.tag_configure("step_gap", font=("Segoe UI", 4))

        # Two independent collapse levels (see the module-level rationale in the pro-tip
        # text). SECTIONS: everything after each h1 heading, until the next h1, folds under
        # a per-heading checkbox. STEPS: inside a section, a numbered "N. " bullet plus the
        # indented ("   ...") bullets that follow it fold under their own checkbox.
        #
        # Elide can't be OR'd across tags (Tk resolves it by tag priority, last-wins), so
        # the two levels never share an elide tag. Each collapsible range has exactly ONE
        # elide-controlling tag whose value is recomputed as (section_collapsed OR
        # step_state); a separate, non-eliding "section mark" tag records which section a
        # range belongs to purely so search-reveal can re-open the right section.
        intro = []                 # items before the first h1 - always visible, no heading
        sections = []              # [(h1_text, [(tag, line), ...]), ...]
        for tag, line in content:
            if tag == "h1":
                sections.append((line, []))
            elif sections:
                sections[-1][1].append((tag, line))
            else:
                intro.append((tag, line))

        for tag, line in intro:
            txt.insert("end", line + "\n", tag)

        step_vars = {}         # body_tag  -> step var  (search + set-all)
        step_label_vars = {}   # label_tag -> step var  (search)
        section_vars = {}      # mark_tag  -> section var (search + set-all)
        step_counter = 0

        for s, (h1_text, items) in enumerate(sections):
            section_var = tk.BooleanVar(value=False)   # False = expanded
            mark_tag = "instr_sect_%d" % s             # marker only (no elide)
            body_tag_sect = "instr_sectbody_%d" % s    # elide = section collapsed
            section_vars[mark_tag] = section_var

            hcb = ttk.Checkbutton(txt, variable=section_var)
            Tooltip(hcb, "Collapse this whole section down to its heading, or expand it.")
            txt.window_create("end", window=hcb, padx=4)          # header cb stays visible
            txt.insert("end", h1_text + "\n", ("h1", mark_tag))

            section_steps = []  # (step_var, body_tag, label_tag) in this section

            i, n = 0, len(items)
            while i < n:
                tag, line = items[i]
                if tag == "bullet" and re.match(r"^\d+\.\s", line):
                    step_lines = [(tag, line)]
                    i += 1
                    while i < n and items[i][0] == "bullet" and items[i][1].startswith("   "):
                        step_lines.append(items[i])
                        i += 1
                    body_tag = "instr_step_body_%d" % step_counter
                    label_tag = "instr_step_label_%d" % step_counter
                    step_counter += 1
                    step_var = tk.BooleanVar(value=False)
                    step_vars[body_tag] = step_var
                    step_label_vars[label_tag] = step_var

                    cb = ttk.Checkbutton(txt, variable=step_var)
                    Tooltip(cb, "Collapse this step down to its number, or expand it again.")
                    txt.window_create("end", window=cb, padx=4)
                    # Tag the just-created checkbox char so it hides when the section folds.
                    # The index MUST come from the widget itself: "end-1c" is the trailing
                    # newline Tk keeps after the window, one char PAST the checkbox. Tagging
                    # that instead left the step checkbox un-elided when its section folded,
                    # and an un-elided window char merging into the next display line zeroes
                    # out the layout of the following embedded window - which is why, after
                    # "Collapse all", the "What each tab does" heading checkbox (the one
                    # right after the only section with steps) drew but could not be clicked.
                    win_idx = txt.index(cb)
                    txt.tag_add(mark_tag, win_idx, "%s+1c" % win_idx)
                    txt.tag_add(body_tag_sect, win_idx, "%s+1c" % win_idx)

                    title_tag, title_text = step_lines[0]
                    # Two mutually-exclusive first-line versions share the checkbox's line:
                    # the "Step N" stub (shown while the step is collapsed) and the full
                    # title (shown while expanded). Their elide is (section OR step-state);
                    # they carry mark_tag too so search can re-open the section, but NOT
                    # body_tag_sect (that would make two elide tags fight).
                    num = re.match(r"^(\d+)\.", title_text)
                    label_text = "Step %s" % num.group(1) if num else title_text.split(".")[0]
                    txt.insert("end", label_text + "\n", (title_tag, label_tag, mark_tag))
                    txt.insert("end", title_text + "\n", (title_tag, body_tag, mark_tag))
                    for blt, blx in step_lines[1:]:
                        txt.insert("end", blx + "\n", (blt, body_tag, mark_tag))
                    # Spacer after each step (folds with the section, survives step-collapse).
                    txt.insert("end", "\n", ("step_gap", mark_tag, body_tag_sect))
                    section_steps.append((step_var, body_tag, label_tag))
                else:
                    # Plain line under the heading - elide follows the section directly.
                    txt.insert("end", line + "\n", (tag, mark_tag, body_tag_sect))
                    i += 1

            # Recompute closure: a section holds its plain body (body_tag_sect) plus each
            # step, whose body/label elide is (section OR step-state).
            def _apply_section(sv=section_var, sbt=body_tag_sect, steps=section_steps):
                collapsed = sv.get()
                txt.tag_configure(sbt, elide=collapsed)
                for stv, bt, lt in steps:
                    txt.tag_configure(bt, elide=collapsed or stv.get())
                    txt.tag_configure(lt, elide=collapsed or (not stv.get()))
            section_var.trace_add("write", lambda *_a, f=_apply_section: f())

            for stv, bt, lt in section_steps:
                def _apply_step(*_a, sv=section_var, stv=stv, bt=bt, lt=lt):
                    collapsed = sv.get()
                    txt.tag_configure(bt, elide=collapsed or stv.get())
                    txt.tag_configure(lt, elide=collapsed or (not stv.get()))
                stv.trace_add("write", _apply_step)

            _apply_section()  # set the initial (fully-expanded) elide state

        txt._instr_vars = (section_vars, step_vars, step_label_vars)
        txt.configure(state="disabled")
        return txt

    def _set_all_instructions(self, collapsed, txt):
        """Back the toolbar's "Collapse all" / "Expand all" buttons - both levels at once:
        every section heading and every step. Sections are set first so their per-step
        recompute runs, then the step vars settle each step to `collapsed`."""
        section_vars, step_vars, _labels = txt._instr_vars
        for var in section_vars.values():
            var.set(collapsed)
        for var in step_vars.values():
            var.set(collapsed)

    def _tag_instruction_examples(self):
        """Grey out the sample paths in the Instructions prose so they read as
        examples, not as paths this install actually uses - same colour as an
        empty field's placeholder. Re-run on theme toggle (see _retheme_widgets)
        because a Text tag's colour is fixed at configure time."""
        for txt in (getattr(self, "instructions_text_quick", None),
                    getattr(self, "instructions_text_full", None)):
            if txt is None:
                continue
            txt.tag_configure("example", foreground=self.theme["entry_placeholder_fg"])
            txt.tag_remove("example", "1.0", "end")
            # Longest snippet first: "C:\ARKServer" is a prefix of the nested-install
            # example, and tagging the short one first would leave the rest undimmed.
            for snippet in sorted(INSTRUCTION_EXAMPLE_SNIPPETS, key=len, reverse=True):
                idx = "1.0"
                while True:
                    pos = txt.search(snippet, idx, stopindex="end", exact=True, elide=True)
                    if not pos:
                        break
                    end = "%s+%dc" % (pos, len(snippet))
                    txt.tag_add("example", pos, end)
                    idx = end

    # -------------------------------------------------------- reminder ----- #
    def _read_hide_reminder_flag(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        return bool(data.get(REMINDER_HIDE_KEY, False))

    def _goto_install_tab(self):
        self.notebook.select(self.tab_install)

    def _dismiss_reminder(self):
        self.reminder_banner.pack_forget()

    def _dismiss_reminder_forever(self):
        self._hide_install_reminder = True
        self.reminder_banner.pack_forget()
        try:
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            data[REMINDER_HIDE_KEY] = True
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            self._log("! Could not save reminder preference: %s" % exc)

    # -------------------------------------------------------------- theme --- #
    def _read_theme_pref(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return "light"
        name = data.get(THEME_KEY, "light")
        return name if name in THEMES else "light"

    def _write_config_key(self, key, value, label):
        """Update ONE key in the config JSON, leaving everything else in the file
        alone. For the launcher preferences that persist the moment they change
        instead of waiting for Save (theme, active profile)."""
        try:
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data[key] = value
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            self._log("! Could not save %s: %s" % (label, exc))

    def _write_theme_pref(self, name):
        self._write_config_key(THEME_KEY, name, "theme preference")

    # -------------------------------------------------------------- mods --- #
    def _load_mods_config(self):
        """Stored mod list merged with any SUPPORTED_MODS entries not yet present, so a
        launcher update that adds a newly-supported mod shows up for existing users
        without touching their existing enabled/order state for the rest."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        stored = data.get(MODS_KEY)
        mods = [m for m in stored if isinstance(m, dict) and m.get("id")] \
            if isinstance(stored, list) else []
        known_ids = {m["id"] for m in mods}
        for supported in SUPPORTED_MODS:
            if supported["id"] not in known_ids:
                mods.append({"id": supported["id"], "name": supported["name"],
                             "enabled": False, "supported": True})
        return mods

    def _save_mods_config(self):
        self._write_config_key(MODS_KEY, self._mods, "mods list")

    def _theme_toggle_label(self):
        """Button text names the destination state (what clicking it does),
        not the current one."""
        return "☀ Light mode" if self.theme_name == "dark" else "\U0001F319 Dark mode"

    def _apply_theme(self, name):
        """Select `name` ("light"/"dark"): sets the ttk theme engine, configures
        every base ttk style class this app uses, and updates the module-level
        CURRENT_THEME dict that plain-tk widgets/Tooltip popups read directly.

        Safe to call before _build_ui() (startup - no widget exists yet, so
        there's nothing else to do) or after it (toggle - in which case the
        caller must also run _retheme_widgets() to fix up already-built
        plain-tk widgets, which this method alone can't reach).
        """
        self.theme_name = name
        self.theme = THEMES[name]
        t = self.theme
        CURRENT_THEME.clear()
        CURRENT_THEME.update(t)

        style = ttk.Style()
        # vista (Windows' native ttk theme) renders most widgets via
        # uxtheme.dll and ignores color overrides on them; clam is pure-Tcl
        # and fully honors style.configure() colors. Light mode keeps the
        # native look exactly as before; dark mode switches engines so
        # colors actually take effect (confirmed trade-off - see the plan).
        style.theme_use("clam" if name == "dark" else "vista")

        style.configure(".", background=t["bg"], foreground=t["fg"])
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabelframe", background=t["bg"])
        style.configure("TLabelframe.Label", background=t["bg"], foreground=t["fg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"])
        style.configure("TCheckbutton", background=t["bg"], foreground=t["fg"])
        style.configure("TButton", background=t["bg"], foreground=t["fg"])
        style.configure("TEntry", fieldbackground=t["text_bg"], foreground=t["fg"])
        style.configure("TNotebook", background=t["bg"])
        style.configure("TNotebook.Tab", background=t["bg"], foreground=t["fg"])
        style.map("TNotebook.Tab",
                  background=[("selected", t["tab_active_bg"])],
                  foreground=[("selected", t["fg"])])
        style.configure("TScrollbar", background=t["bg"])
        style.configure("Horizontal.TProgressbar", background=t["status_ok"],
                        troughcolor=t["bg"])

        try:
            self.configure(background=t["bg"])
        except tk.TclError:
            pass

        # Suggested-but-not-yet-created paths (the SAVESROOT/BACKUPROOT dialog
        # offers these when nothing matching exists on disk yet) are examples,
        # not found folders - dim them like an empty field's placeholder.
        style.configure("Placeholder.TButton", background=t["bg"],
                        foreground=t["entry_placeholder_fg"])

        # Header "make sure to save!" hint - a light yellow wash so it reads as a
        # reminder next to the title instead of blending into it.
        style.configure("SaveHint.TLabel", background=t["warn_bg"],
                        foreground=t["warn_fg"])

        self._default_fg = style.lookup("TEntry", "foreground") or t["fg"]
        # Placeholder text is italic as well as grey, so the two states are told apart
        # by shape and not only by colour (colour alone is easy to miss next to a real
        # value, and is invisible to a colour-blind user).
        self._default_entry_font = "TkDefaultFont"
        base_font = tkfont.nametofont("TkDefaultFont")
        self._placeholder_font = (base_font.actual("family"), base_font.actual("size"),
                                   "italic")
        # Re-applies the search-highlight styles under the (possibly just
        # switched) theme engine - ttk style configuration is per-engine, so
        # these need re-registering every time theme_use() changes.
        self._setup_search_styles()

    def _retheme_widgets(self):
        """Re-color already-built plain-tk widgets and anything else
        _apply_theme() can't reach via ttk styles alone. Only meaningful when
        toggling after startup - at startup these widgets are constructed
        reading self.theme directly, so there's nothing stale to fix up."""
        t = self.theme
        for widget in (self.log, self.install_log, self.debug_log_text,
                       self.instructions_text_quick, self.instructions_text_full,
                       self.profile_notes_text):
            try:
                widget.configure(background=t["text_bg"], foreground=t["text_fg"],
                                  insertbackground=t["text_fg"])
            except tk.TclError:
                pass

        # An Entry showing its greyed example was coloured with the OLD theme's
        # placeholder colour (a direct widget option, which no ttk style restyles),
        # so it has to be repainted by hand or it keeps the light-mode grey in dark
        # mode. Fields holding a real value go back to the default entry colour.
        for key in self._placeholder_text:
            if self._placeholder_active.get(key):
                self._style_field_as_placeholder(key)
            else:
                self._style_field_as_real(key)

        # Text tag colours are baked in at tag_configure() time, so the dimmed
        # example paths need re-tagging under the new theme's placeholder colour.
        try:
            self._tag_instruction_examples()
        except tk.TclError:
            pass

        # Repainted to whichever state each Save halo currently holds (lit warn colours /
        # blended-in bg) rather than unconditionally lit - _set_halo reads self.theme.
        try:
            self._update_save_highlights()
            self._set_halo(self.mods_save_btn_halo, self._mods_dirty_flag)
        except tk.TclError:
            pass

        # Reapplies under the new theme - _set_update_highlight reads self.theme, so this
        # repaints the halo to whichever state (lit warn colours / blended-in bg) it holds.
        try:
            self._set_update_highlight(self._update_highlight_on)
        except tk.TclError:
            pass

        try:
            self.reminder_banner.configure(background=t["warn_bg"],
                                            highlightbackground=t["warn_border"])
            self._retheme_warn_subtree(self.reminder_banner, t)
        except tk.TclError:
            pass

        # Setup Status rows are rebuilt from scratch each refresh and already
        # read colors from self.theme at build time, so re-running it is the
        # simplest correct fix rather than hunting down each child widget.
        self._refresh_setup_status()

    def _retheme_warn_subtree(self, widget, t):
        """Recolor the reminder banner's nested tk.Frame/tk.Label children -
        their background must match the banner's own warn_bg exactly (Tk
        doesn't composite transparency), so this walks the whole subtree
        rather than hardcoding each one by name."""
        for child in widget.winfo_children():
            if isinstance(child, (tk.Frame, tk.Label)):
                try:
                    kwargs = {"background": t["warn_bg"]}
                    if isinstance(child, tk.Label):
                        kwargs["foreground"] = t["warn_fg"]
                    child.configure(**kwargs)
                except tk.TclError:
                    pass
            self._retheme_warn_subtree(child, t)

    def _toggle_theme(self):
        self._apply_theme("dark" if self.theme_name == "light" else "light")
        self._retheme_widgets()
        self._write_theme_pref(self.theme_name)
        self.theme_toggle_btn.configure(text=self._theme_toggle_label())
        if self._last_search_query:
            self._run_search(self._last_search_query)

    # ------------------------------------------------------------ helpers --- #
    def get(self, key):
        if self._placeholder_active.get(key):
            return ""
        return self.vars[key].get().strip()

    def set(self, key, value):
        value = value if value is not None else ""
        if key in self._placeholder_text:
            self._placeholder_active[key] = False
            self._style_field_as_real(key)
        self.vars[key].set(value)
        if not value and key in self._placeholder_text:
            self._show_placeholder(key)
        # Any CLUSTERDIR change (Browse, a scan result, an accepted suggestion, Create
        # ServerCluster folders) offers its sibling Saves/Backups folders - typing is
        # covered by the field's FocusOut binding. after_idle so a caller setting all
        # three cluster fields in a row is finished before we look at them.
        if key == "CLUSTERDIR" and value and self._cluster_autoscan:
            self.after_idle(self._on_cluster_dir_focus_out)

    def _set_from_file(self, key, value):
        """set(), for values arriving from a .bat / .ini / the config JSON rather than
        from the user. A value that's only this field's shipped example (see
        is_unconfigured_example_path) is dropped so the field stays genuinely empty and
        shows its greyed placeholder, instead of masquerading as configured data."""
        if is_unconfigured_example_path(key, value):
            self._ignored_example_values.add(key)
            self.set(key, "")
            return False
        self.set(key, value)
        return True

    # ------------------------------------------------------- placeholders --- #
    #
    # These fields are NOT Tk placeholder text (Tk has none): the example is written
    # into the StringVar and filtered out again by get(), which returns "" whenever
    # _placeholder_active[key] is set. That means two invariants have to hold, or fake
    # data becomes real data (or real data silently vanishes):
    #   1. EVERY Entry bound to the field must clear the example on focus - hence
    #      _placeholder_entries being a list, not a single widget. SERVER_ROOT appears
    #      on both the Configuration and Install Server/Api/Plugin tabs, and before this was a
    #      list, typing into the Install Server/Api/Plugin one left _placeholder_active set, so
    #      get() reported "" and the user's typed path was silently discarded.
    #   2. Placeholder state and appearance must always agree: showing = greyed +
    #      italic, real = default colour + normal. _style_field_as_* are the only two
    #      places that colour these widgets, so the two states can't drift apart.
    def _register_placeholder(self, key, entry, example_text):
        """Wire up a path Entry to show greyed-out example text while empty. May be
        called more than once per key - every registered Entry shows the same field."""
        self._placeholder_text[key] = example_text
        self._placeholder_entries.setdefault(key, []).append(entry)
        entry.bind("<FocusIn>", lambda _e, k=key: self._on_placeholder_focus_in(k), add="+")
        entry.bind("<FocusOut>", lambda _e, k=key: self._on_placeholder_focus_out(k), add="+")

    def _placeholder_widgets(self, key):
        return self._placeholder_entries.get(key, [])

    def _style_field_as_placeholder(self, key):
        """Greyed + italic: this field holds example text and no real value."""
        for entry in self._placeholder_widgets(key):
            try:
                entry.configure(foreground=self.theme["entry_placeholder_fg"],
                                 font=self._placeholder_font)
            except tk.TclError:
                pass

    def _style_field_as_real(self, key):
        """Normal colour + normal weight: this field holds a genuine value."""
        for entry in self._placeholder_widgets(key):
            try:
                entry.configure(foreground=self._default_fg, font=self._default_entry_font)
            except tk.TclError:
                pass

    def _on_placeholder_focus_in(self, key):
        if self._placeholder_active.get(key):
            self._placeholder_active[key] = False
            self.vars[key].set("")
            self._style_field_as_real(key)

    def _on_placeholder_focus_out(self, key):
        if not self.vars[key].get().strip():
            self._show_placeholder(key)

    def _show_placeholder(self, key):
        text = self._placeholder_text.get(key)
        if not text:
            return
        self._placeholder_active[key] = True
        self.vars[key].set(text)
        self._style_field_as_placeholder(key)

    def _apply_path_placeholders(self):
        """Show example text in any still-empty path field, once, after initial load."""
        for key in self._placeholder_text:
            if not self._placeholder_active.get(key) and not self.vars[key].get().strip():
                self._show_placeholder(key)

    def _apply_defaults(self):
        """Fill any still-empty non-path field with its safe default. Never overrides a
        value that was actually read from a real file/JSON snapshot."""
        for key, value in DEFAULT_VALUES.items():
            if key in self.vars and not self.get(key):
                self.set(key, value)

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        # Same line to the persistent log. Every scan, save, reset, cluster-folder
        # creation, Game.ini patch and error already routes through here, so mirroring at
        # this one point covers the lot - and covers whatever gets added next.
        launcher_log(msg, "Console")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # --------------------------------------------------------------- logo --- #
    def _assets_dir(self):
        return os.path.join(resource_dir(), "assets")

    def _register_header_font(self):
        """Register assets/FuturaNowHeadline.ttf for this process and return the
        family name to use for the header title - the real family if it loaded,
        else "Segoe UI" as a bold-default fallback so the header always renders.

        Called during _build_ui(), before self.log exists, so any failure is
        stashed in self._header_font_warning and flushed to the log once it
        does (see __init__).
        """
        path = os.path.join(self._assets_dir(), "FuturaNowHeadline.ttf")
        family = _register_private_font(path)
        if family:
            self._header_font_family = family
            return family
        self._header_font_warning = (
            "! Could not load assets/FuturaNowHeadline.ttf as a custom header "
            "font - falling back to the default system font.")
        return "Segoe UI"

    def _load_header_logo(self, max_height=48):
        """Show assets/logo.png top-right of the header, if present. Silent no-op
        otherwise (so the app runs fine before the logo file is added).

        Called during _build_ui(), before self.log exists - errors are swallowed
        rather than logged.
        """
        path = os.path.join(self._assets_dir(), "logo.png")
        if not os.path.isfile(path):
            return
        try:
            img = tk.PhotoImage(file=path)
            if img.height() > max_height:
                factor = max(1, round(img.height() / max_height))
                img = img.subsample(factor, factor)
            self._logo_img = img  # keep a reference, Tk drops unreferenced images
            self.logo_label.configure(image=self._logo_img)
        except tk.TclError:
            pass

    # ------------------------------------------------------ logo easter egg --- #
    def _on_logo_click(self, _event=None):
        """Count clicks on the header logo and play out LOGO_EGG_LINES.

        Nothing else in the app touches _logo_clicks, so switching tabs, running a
        scan, or clicking anywhere else in between two logo clicks leaves the sequence
        exactly where it was. After the finale the logo goes permanently inert for the
        rest of the session; a restart makes it discoverable again (chosen over
        persisting it, so the whole thing stays a per-session gag rather than a
        one-shot-per-install one, and so testing it doesn't require editing a file)."""
        if self._logo_egg_done:
            return
        self._logo_clicks += 1
        line = LOGO_EGG_LINES.get(self._logo_clicks)
        if line:
            self._show_logo_bubble(line)
        if self._logo_clicks == LOGO_EGG_CREDITS_AT:
            # Let the "here we go" bubble land first, then the payoff.
            self.after(900, self._show_credits)
        elif self._logo_clicks == LOGO_EGG_FINALE_AT:
            self._play_egg_music()
            self._logo_egg_done = True

    def _show_logo_bubble(self, text):
        """A small borderless speech bubble under the logo. Non-blocking, click it (or
        wait) to dismiss; only ever one at a time."""
        self._close_logo_bubble()
        t = self.theme
        win = tk.Toplevel(self)
        win.overrideredirect(True)   # no title bar - it's a speech bubble, not a dialog
        win.attributes("-topmost", True)
        frame = tk.Frame(win, background=t["warn_bg"],
                         highlightbackground=t["warn_border"], highlightthickness=1)
        frame.pack(fill="both", expand=True)
        label = tk.Label(frame, text=text, background=t["warn_bg"], foreground=t["warn_fg"],
                         justify="left", wraplength=260, padx=10, pady=8,
                         font=("Segoe UI", 9))
        label.pack()
        for w in (win, frame, label):
            w.bind("<Button-1>", lambda _e: self._close_logo_bubble())
        # Anchor under the logo, nudged left so a long line stays on screen.
        win.update_idletasks()
        try:
            x = self.logo_label.winfo_rootx() - max(0, win.winfo_width() - 90)
            y = self.logo_label.winfo_rooty() + self.logo_label.winfo_height() + 6
        except tk.TclError:
            x, y = self.winfo_rootx() + 40, self.winfo_rooty() + 90
        win.geometry("+%d+%d" % (max(0, x), y))
        self._logo_bubble = win
        self.after(LOGO_EGG_BUBBLE_MS, lambda w=win: self._close_logo_bubble(w))

    def _close_logo_bubble(self, only=None):
        """Close the current bubble. `only` guards the auto-close timer from killing a
        newer bubble that replaced the one it was scheduled for."""
        win = self._logo_bubble
        if win is None or (only is not None and only is not win):
            return
        self._logo_bubble = None
        try:
            win.destroy()
        except tk.TclError:
            pass

    def _show_credits(self):
        """The payoff: a proper window with the big ARK:ipelago logo and the credits.
        Themed from self.theme like every other panel, so it reads correctly in both
        light and dark mode."""
        t = self.theme
        win = tk.Toplevel(self)
        win.title("ARKipelago - Credits")
        win.transient(self)
        win.configure(background=t["bg"])
        win.resizable(False, False)
        win.minsize(440, 0)   # the logo alone is narrower than this reads well at

        body = tk.Frame(win, background=t["bg"], padx=36, pady=20)
        body.pack(fill="both", expand=True)

        path = os.path.join(self._assets_dir(), CREDITS_LOGO_FILENAME)
        if os.path.isfile(path):
            try:
                img = tk.PhotoImage(file=path)
                if img.height() > 220:
                    img = img.subsample(max(1, round(img.height() / 220)))
                self._credits_img = img  # keep a reference or Tk drops the image
                tk.Label(body, image=self._credits_img, background=t["bg"]
                         ).pack(pady=(0, 10))
            except tk.TclError:
                pass  # no logo is survivable; the credits are the point

        tk.Label(body, text="Thank You", background=t["bg"], foreground=t["fg"],
                 font=(self._header_font_family or "Segoe UI", 22, "bold")
                 ).pack()
        tk.Label(body, text="ARKipelago exists because of these people.",
                 background=t["bg"], foreground=t["subtle_fg"],
                 font=("Segoe UI", 9, "italic")).pack(pady=(2, 16))

        for name, role in CREDITS:
            entry = tk.Frame(body, background=t["bg"])
            entry.pack(fill="x", pady=5)
            tk.Label(entry, text=name, background=t["bg"], foreground=t["fg"],
                     font=("Segoe UI", 12, "bold")).pack()
            tk.Label(entry, text=role, background=t["bg"], foreground=t["subtle_fg"],
                     font=("Segoe UI", 9), wraplength=380).pack()

        ttk.Button(body, text="Close", command=win.destroy).pack(pady=(20, 0))
        win.update_idletasks()
        # Centre on the launcher window rather than the screen - dual-monitor safe.
        win.geometry("+%d+%d" % (
            self.winfo_rootx() + max(0, (self.winfo_width() - win.winfo_width()) // 2),
            max(0, self.winfo_rooty() + 20)))

    # ------------------------------------------------------------- egg audio --- #
    def _play_egg_music(self):
        """Start the easter-egg track. Background playback via MCI (see mci_play_once) -
        the Tk thread is never blocked and the app stays fully responsive while it
        plays. Failure is silent apart from a log line; it's a joke, not a feature."""
        path = os.path.join(self._assets_dir(), EGG_MUSIC_FILENAME)
        ok, err = mci_play_once(path)
        self._egg_music_on = ok
        if not ok:
            self._log("(easter egg) could not play %s: %s" % (EGG_MUSIC_FILENAME, err))

    def _stop_egg_music(self):
        """Stop playback for good - switching back to the tab never resumes it, since
        nothing but _play_egg_music (one click count, already spent) ever starts it."""
        if not self._egg_music_on:
            return
        self._egg_music_on = False
        mci_stop()

    def _on_app_close(self):
        self._stop_egg_music()
        self.destroy()

    def _load_window_icon(self):
        """Set the window/taskbar icon from assets/icon.ico (preferred, Windows
        .ico) or assets/logo.png. Silent no-op if neither exists yet."""
        ico_path = os.path.join(self._assets_dir(), "icon.ico")
        if os.path.isfile(ico_path):
            try:
                self.iconbitmap(ico_path)
                return
            except tk.TclError as exc:
                self._log("! Could not load assets/icon.ico: %s" % exc)
        png_path = os.path.join(self._assets_dir(), "logo.png")
        if os.path.isfile(png_path):
            try:
                self.iconphoto(True, tk.PhotoImage(file=png_path))
            except tk.TclError as exc:
                self._log("! Could not load assets/logo.png as window icon: %s" % exc)

    def _browse(self, key, kind):
        current = self.get(key)
        if kind == "folder":
            initial = current if os.path.isdir(current) else base_dir()
            path = filedialog.askdirectory(initialdir=initial, title="Select folder")
        else:  # file
            initial = os.path.dirname(current) if current else base_dir()
            if key == "connector_ini":
                ftypes = [("connector.ini", "connector.ini"), ("INI files", "*.ini"),
                          ("All files", "*.*")]
            elif key == "game_ini":
                ftypes = [("Game.ini", "Game.ini"), ("INI files", "*.ini"),
                          ("All files", "*.*")]
            else:
                ftypes = [("All files", "*.*")]
            path = filedialog.askopenfilename(initialdir=initial, filetypes=ftypes,
                                              title="Select file")
        if path:
            self.set(key, os.path.normpath(path))

    def _clear_path_field(self, key):
        """The per-field "C" button: blanks just this one field back to its greyed
        placeholder. GUI-only - nothing on disk is touched, and the cleared value only
        reaches paths.cmd/the scripts on the next Save, same as any other field edit."""
        self.set(key, "")
        self._log("%s cleared." % key)

    def _on_clear_all_paths(self):
        """"Clear all paths": blanks every PATH_GROUP_KEYS field back to a fresh,
        never-configured state in one click. GUI-only, same as _clear_path_field - a
        Save is still required for the cleared paths to reach paths.cmd/the scripts."""
        if not messagebox.askyesno(
                "Clear all paths",
                "Clear SERVER_ROOT, SAVESROOT, CLUSTERDIR, BACKUPROOT, the ArkApi "
                "Plugins folder, ipc_dir and game_ini back to blank?\n\n"
                "This only clears the fields here - nothing on disk is touched. "
                "Save afterward for the change to reach the scripts."):
            return
        for key in PATH_GROUP_KEYS:
            self.set(key, "")
        self._log("Cleared all path fields (%s)." % ", ".join(PATH_GROUP_KEYS))

    # ------------------------------------------------------- discovery ----- #
    def _discover_locations(self, saved):
        """Extract the bundled scripts next to the launcher and locate connector.ini.

        The scripts folder is no longer user-configurable - the launcher ships the
        scripts itself and unpacks them into working_scripts_dir() (missing-only), which
        becomes self._scripts_dir for Save/Run. connector.ini is still external (a manual
        download) so it's still auto-located from the usual sibling folders."""
        b = base_dir()
        cwd = os.getcwd()

        dst_root, extracted, refreshed, errors, migrated = extract_bundled_scripts()
        self._scripts_dir = os.path.normpath(dst_root)
        self._scripts_extracted = extracted
        self._scripts_refreshed = refreshed
        self._scripts_migrated_vars = migrated
        self._scripts_extract_errors = errors

        # connector.ini: first existing candidate, searched ONLY in the launcher's own
        # folder and its immediate parent - i.e. the documented layout, where
        # ArkConnector\ is unzipped beside the launcher folder.
        #
        # This used to walk 5 parents up, which was added purely to accommodate the dev
        # tree (the exe runs from dist\<name>\ while ArkConnector\ sits 3 levels up beside
        # the source). That reach is what leaked real data into shipped builds: a freshly
        # built exe launched from dist\ found the DEVELOPER's connector.ini and pulled
        # server=archipelago.gg:51357 / slot=Avocado into the Connector fields, which
        # autosave then wrote into the config + profiles JSON sitting next to the exe.
        # Reaching that far also means an end user's launcher probes unrelated folders
        # far outside its own install. One level covers every real layout.
        #
        # The bug the 5-level walk was originally justified by - "Save silently dropped
        # every Connector value" when no ini was found - is fixed on its own merits in
        # _apply_ini(), which now logs loudly and says how to set the path.
        ini = saved.get("connector_ini", "")
        if not ini or not os.path.isfile(ini):
            roots = [b, cwd]
            up = os.path.dirname(b)
            if up and up not in roots:
                roots.append(up)
            cand_ini = [os.path.join(b, "connector.ini")]
            cand_ini += [os.path.join(r, "ArkConnector", "connector.ini") for r in roots]
            for p in cand_ini:
                if p and os.path.isfile(p):
                    ini = os.path.normpath(p)
                    break
        self.set("connector_ini", ini)

    # ------------------------------------------------------- load / save --- #
    def _load_json(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        # Prefill everything we recognise (files will override the mapped ones next).
        for key, value in data.items():
            if key in self.vars:
                # Older configs were saved BEFORE the template-residue bug was fixed and
                # can contain the shipped example paths as if they were real - heal them
                # on load rather than carrying the fake values forward forever.
                self._set_from_file(key, value)
        return data

    def load_from_files(self, initial=False, saved=None):
        """Pre-fill the mapped GUI fields from whichever real files exist."""
        if not initial:
            self._clear_log()
        self._ignored_example_values = set()
        scripts = self._scripts_dir

        # --- .bat files: first hit wins, in PREFILL_ORDER ---
        seen = set()
        found_any = False
        for batname in PREFILL_ORDER:
            path = os.path.join(scripts, batname) if scripts else ""
            if not path or not os.path.isfile(path):
                continue
            found_any = True
            text, _ = read_text(path)
            for gui_key, var in BAT_TARGETS[batname].items():
                if gui_key in seen:
                    continue
                val = bat_read_var(text, var)
                if val is not None:
                    # The shipped templates carry example paths as their defaults, so
                    # this is where template residue would otherwise enter the app.
                    self._set_from_file(gui_key, val)
                    seen.add(gui_key)

        # --- connector.ini ---
        ini_path = self.get("connector_ini")
        ini_found = ini_path and os.path.isfile(ini_path)
        if ini_found:
            for k, v in ini_read_values(ini_path).items():
                self._set_from_file(k, v)

        # --- ArkApi Plugins folder: not in any .bat; derive from ipc_dir if empty ---
        if not self.get("PLUGINS_DIR"):
            ipc = self.get("ipc_dir")
            # ipc_dir is <Plugins>\ArkAP\ipc  ->  Plugins is two levels up.
            if ipc:
                guess = os.path.dirname(os.path.dirname(os.path.normpath(ipc)))
                if guess and os.path.basename(guess).lower() != guess.lower():
                    self.set("PLUGINS_DIR", guess)

        self._apply_defaults()

        self._log("Loaded from files.")
        self._log("  Scripts folder (bundled): %s" % (scripts or "(not found)"))
        if getattr(self, "_scripts_extracted", None):
            self._log("  Extracted bundled scripts: %s" % ", ".join(self._scripts_extracted))
        if getattr(self, "_scripts_refreshed", None):
            self._log("  Replaced %s - these were from before paths.cmd existed, so they "
                      "held their own copy of SERVER_ROOT and ignored anything Save wrote. "
                      "The previous version of each is kept alongside it as %s."
                      % (", ".join(self._scripts_refreshed), PRE_PATHS_BACKUP_SUFFIX))
        if getattr(self, "_scripts_migrated_vars", None):
            self._log("  Carried %s out of those old scripts into paths.cmd - check them "
                      "on the Configuration tab before starting the server."
                      % ", ".join(self._scripts_migrated_vars))
        if getattr(self, "_scripts_extract_errors", None):
            for err in self._scripts_extract_errors:
                self._log("  ! Could not extract %s" % err)
        if not found_any:
            self._log("  No .bat files found there yet - fields left as-is.")
        self._log("  connector.ini: %s" % (ini_path if ini_found else "(not found)"))
        if self._ignored_example_values:
            self._log("  Ignored the shipped example path(s) for %s - those are the "
                      "templates' own placeholder values, not your settings, so those "
                      "fields are shown as empty (greyed example text) until you set "
                      "them." % ", ".join(sorted(self._ignored_example_values)))

    def collect_values(self):
        values = {key: self.get(key) for key in self.vars}
        values[REMINDER_HIDE_KEY] = self._hide_install_reminder
        return values

    def _save_json(self, values):
        # Merged onto whatever is already in the file rather than replacing it: the
        # config JSON also carries keys that are written outside this flow and are not
        # in collect_values() (THEME_KEY, ACTIVE_PROFILE_KEY - see _write_config_key),
        # and a wholesale overwrite would drop them on every Save.
        try:
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    existing.update(values)
                    values = existing
            except (OSError, ValueError):
                pass
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(values, f, indent=2)
            return True, self.config_path
        except OSError as exc:
            return False, str(exc)

    # -------------------------------------------- Save-button highlights --- #
    #
    # The pale-yellow halo around each Save button used to be permanent decoration, so
    # it said nothing: a freshly launched, fully-saved app looked exactly like one with
    # an hour of unsaved edits in it. It now means one thing only - "this section has
    # changes that aren't on disk yet" - measured against _saved_values.
    def _set_halo(self, halo, on):
        """Light up (or blend away) one Save halo. Recoloured to the surrounding bg
        rather than unpacked, the same trick _set_update_highlight uses, so turning it
        off never shifts the layout around the button."""
        t = self.theme
        try:
            halo.configure(background=t["warn_bg"] if on else t["bg"],
                            highlightbackground=t["warn_border"] if on else t["bg"])
        except tk.TclError:
            pass

    def _mark_saved_baseline(self):
        """Declare the current field values to BE what's on disk. Called once startup
        has finished loading and after every successful Save - the two moments the two
        are genuinely in sync."""
        self._saved_values = self._current_profile_snapshot()
        self._update_save_highlights()

    def _update_save_highlights(self):
        """Light each tab's Save only while one of ITS OWN fields differs from the last
        saved values. Either button writes everything (there's a single config JSON),
        but the highlights are scoped per tab so editing a Configuration path doesn't
        light up a tab the user hasn't touched."""
        if self._saved_values is None:
            return          # still loading - nothing has been "changed" yet
        changed = {key for key, value in self._current_profile_snapshot().items()
                   if self._saved_values.get(key) != value}
        arch_dirty = bool(changed & ARCHIPELAGO_KEYS)
        config_dirty = bool(changed - ARCHIPELAGO_KEYS)
        self._set_halo(self.archipelago_save_btn_halo, arch_dirty)
        self._set_halo(self.save_btn_halo, config_dirty)
        self._fields_dirty = arch_dirty or config_dirty
        self._update_save_hint()

    def _update_save_hint(self):
        """The header's "make sure to save!" chip sits above the tab strip, so unlike the
        halos it answers for every section at once: shown while anything anywhere is
        unsaved, hidden when nothing is.

        Packed/unpacked rather than recoloured - it's a sentence, and a greyed-out one
        still reads as a nag. Nothing sits to its right in title_row (the header buttons
        are packed side="right" on the row above), so it can't shift anything."""
        want = self._fields_dirty or self._mods_dirty_flag
        if want == self._save_hint_shown:
            return
        if want:
            self.save_hint_label.pack(side="left", padx=12)
        else:
            self.save_hint_label.pack_forget()
        self._save_hint_shown = want

    def _on_field_changed(self):
        self._update_profile_status()
        self._update_save_highlights()

    def on_save(self):
        self._clear_log()
        values = self.collect_values()

        # Warn about values that would break a `set "VAR=value"` line.
        bad = [k for k in self.vars
               if k not in ("scripts_dir", "connector_ini") and '"' in values[k]]
        if bad:
            if not messagebox.askyesno(
                    "Quotes in values",
                    "These fields contain a double-quote, which can break a batch "
                    "line:\n\n  %s\n\nSave anyway?" % ", ".join(bad)):
                self._log("Save cancelled.")
                return

        # 1) JSON snapshot
        ok, info = self._save_json(values)
        if ok:
            self._log("Saved JSON snapshot -> %s" % info)
            # No profile is created here any more: startup already made
            # DEFAULT_PROFILE_NAME (see _ensure_default_profile), so by the time the
            # user gets to their first Save there is always a profile behind these
            # values. Retried here only for the case where that startup write failed.
            self._ensure_default_profile()
            # Save is also what commits these values into the ACTIVE profile, so the
            # Configuration tab and the profile behind it can't drift apart.
            self._save_active_profile()
        else:
            self._log("! Could not write JSON: %s" % info)

        # 2) .bat/.cmd targets
        scripts = self._scripts_dir
        if not scripts or not os.path.isdir(scripts):
            self._log("! Scripts folder not found - skipped all .bat/.cmd files.")
        else:
            for batname, field_map in BAT_TARGETS.items():
                self._apply_bat(scripts, batname, field_map, values)

        # 3) connector.ini
        self._apply_ini(values)

        # The fields now match what's persisted, so both Save halos go dark until the
        # next edit. Only on a successful JSON write - if that failed, the values really
        # aren't saved and the highlight must keep saying so.
        if ok:
            self._mark_saved_baseline()

        self._log("Done.")
        messagebox.showinfo("ARKIpelago Launcher", "Save complete. See the log for details.")

    def _apply_bat(self, scripts, batname, field_map, values):
        path = os.path.join(scripts, batname)
        if not os.path.isfile(path):
            self._log("%s: not found - skipped." % batname)
            return
        text, enc = read_text(path)
        original = text
        changed, missing, refused = [], [], []
        for gui_key, var in field_map.items():
            value = values.get(gui_key, "")
            # Never write a relative path into a .bat - it ends up on ARK's command
            # line, resolves against some run-time folder, and quietly splits the
            # cluster/save data across two locations (see BAT_PATH_KEYS).
            if gui_key in BAT_PATH_KEYS and value and not is_full_windows_path(value):
                refused.append("%s='%s'" % (var, value))
                continue
            new_text, found = bat_write_var(text, var, value)
            if not found:
                missing.append(var)
                continue
            if new_text != text:
                changed.append(var)
            text = new_text
        if refused:
            self._log("  ! %s: REFUSED to write relative path value(s): %s - use a "
                      "full path (like E:\\ARK\\...) on the Configuration tab."
                      % (batname, ", ".join(refused)))
        if text != original:
            if self.backup_var.get():
                try:
                    shutil.copyfile(path, path + ".bak")
                except OSError as exc:
                    self._log("  ! backup failed for %s: %s" % (batname, exc))
            write_text(path, text, enc)
            self._log("%s: updated %s" % (batname, ", ".join(changed)))
        else:
            self._log("%s: no changes." % batname)
        if missing:
            self._log("   (vars not present, skipped: %s)" % ", ".join(missing))

    def _apply_ini(self, values):
        path = self.get("connector_ini")
        if not path or not os.path.isfile(path):
            # Loud, not "skipped": everything the user typed into the Archipelago Setup
            # tab's room fields and the Configuration tab's plugin-file fields goes
            # nowhere in this case, and the old one-liner read like a harmless note
            # about an optional file.
            self._log("! connector.ini: %s - your Connector values (server / slot / "
                      "ipc_dir / ...) were NOT written anywhere the connector reads."
                      % ("no file set" if not path else "not found at %s" % path))
            self._log("  Fix: set \"connector.ini file\" in the Locations group to the "
                      "connector.ini in your ArkConnector folder, then Save again.")
            return
        text, enc = read_text(path)
        original = text
        changed = []
        for key in CONNECTOR_KEYS:
            new_text, _ = ini_upsert(text, key, values.get(key, ""))
            if new_text != text:
                changed.append(key)
            text = new_text
        if text != original:
            if self.backup_var.get():
                try:
                    shutil.copyfile(path, path + ".bak")
                except OSError as exc:
                    self._log("  ! backup failed for connector.ini: %s" % exc)
            write_text(path, text, enc)
            self._log("connector.ini: updated %s" % ", ".join(changed))
        else:
            self._log("connector.ini: no changes.")

    # ------------------------------------------------------------ Profiles - #
    def _current_profile_snapshot(self):
        """Every Configuration-tab field, keyed the same as self.vars - i.e. the
        Locations/Paths/Network/Connector/Cluster groups (paths, network, cluster,
        connector settings). Deliberately does NOT include REMINDER_HIDE_KEY - that's a
        launcher preference, not part of a server config."""
        return {key: self.get(key) for key in self.vars}

    def _current_profile_notes(self):
        if not hasattr(self, "profile_notes_text"):
            return ""
        return self.profile_notes_text.get("1.0", "end-1c")

    def _load_profiles(self):
        """Read the profiles JSON (separate file from CONFIG_FILENAME - see
        PROFILES_FILENAME - so saving/loading/deleting a profile can never touch the
        single active config)."""
        try:
            with open(self.profiles_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        profiles = data.get("profiles", {})
        return profiles if isinstance(profiles, dict) else {}

    def _save_profiles(self, quiet=False):
        """Write the profiles file. quiet=True logs a failure without a modal - used
        by the 10-minute autosave, which the user didn't ask for and which would
        otherwise pop a dialog every 10 minutes on a full/locked disk."""
        try:
            with open(self.profiles_path, "w", encoding="utf-8") as f:
                json.dump({"profiles": self._profiles}, f, indent=2)
            return True
        except OSError as exc:
            self._log("! Could not save profiles: %s" % exc)
            if not quiet:
                messagebox.showerror("ARKIpelago Launcher",
                                      "Could not save profiles:\n%s" % exc)
            return False

    # ------------------------------------------------- reserved autosave slot - #
    def _user_profiles(self):
        """Every profile the USER made - i.e. everything except the reserved autosave
        slot. Anywhere the app asks "does this user have any profiles?" it means this,
        never the raw dict, or the autosave would masquerade as one of theirs."""
        return {n: p for n, p in self._profiles.items() if not is_autosave_profile(n)}

    def _start_autosave(self):
        """Arm the 10-minute autosave timer. Writes an initial snapshot immediately so
        a crash in the first ten minutes still leaves something to recover."""
        self._autosave_tick(initial=True)

    def _autosave_tick(self, initial=False):
        try:
            self._write_autosave_profile(initial=initial)
        except Exception as exc:
            # Swallowed on purpose: this runs unattended every 10 minutes, and an
            # exception escaping a Tk callback surfaces as a traceback/error box the
            # user never asked for. A note in the log is the right amount of noise.
            self._log("! Autosave failed: %s" % exc)
        finally:
            # Rescheduled in a finally so a transient failure (locked file, full disk)
            # can never silently kill autosaving for the rest of the session.
            self._autosave_after_id = self.after(AUTOSAVE_INTERVAL_MS, self._autosave_tick)

    def _write_autosave_profile(self, initial=False):
        """Replace the autosave slot with the current Configuration values.

        Skips the write entirely when nothing has changed since the last autosave, so
        an app left open (or minimized) all day does no repeated disk I/O. Only ever
        touches self._profiles[AUTOSAVE_PROFILE_NAME] - user profiles are untouched,
        and the currently loaded-profile state is left alone so the Profiles tab's
        "changed since loading" indicator doesn't lie."""
        values = self._current_profile_snapshot()
        existing = self._profiles.get(AUTOSAVE_PROFILE_NAME)
        if (not initial and self._autosave_last_values == values
                and existing is not None):
            return False
        if initial and existing is not None and existing.get("values") == values:
            # Nothing changed since the last run of the app either.
            self._autosave_last_values = dict(values)
            return False

        previous = existing
        # Assignment, not append: the slot holds the latest snapshot only.
        self._profiles[AUTOSAVE_PROFILE_NAME] = {
            "values": values,
            "notes": AUTOSAVE_PROFILE_NOTES,
            "autosaved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not self._save_profiles(quiet=True):
            if previous is None:
                self._profiles.pop(AUTOSAVE_PROFILE_NAME, None)
            else:
                self._profiles[AUTOSAVE_PROFILE_NAME] = previous
            return False
        self._autosave_last_values = dict(values)
        self._refresh_profile_list()
        self._update_profile_status()
        return True

    def _ensure_default_profile(self):
        """Called once at startup, after the profiles file has been read. If the user
        has no profiles yet, create DEFAULT_PROFILE_NAME from the values just loaded
        and mark it as the loaded profile.

        Earlier this waited for the user's first Save, which left the whole of a first
        session with nothing behind the Configuration tab but the bare config JSON -
        anything typed before that Save had no profile to fall back to. Creating it up
        front means every value the user enters belongs to a real profile from the
        start, and the Profiles tab has something selected instead of being empty.

        It is a completely ordinary profile - the user can rename, update or delete it
        like any other. The reserved autosave slot is excluded from the "has profiles"
        test on purpose: it always exists, and counting it would stop this from ever
        being created.

        Setting it as loaded is what makes the Profiles tab's "changed since loading"
        indicator meaningful from the first edit onwards. A write failure leaves the
        app profile-less rather than pretending a profile is loaded that isn't on
        disk; the next launch simply tries again."""
        if self._user_profiles():
            return
        values = self._current_profile_snapshot()
        self._profiles[DEFAULT_PROFILE_NAME] = {"values": values, "notes": ""}
        if not self._save_profiles(quiet=True):
            self._profiles.pop(DEFAULT_PROFILE_NAME, None)
            return
        self._set_active_profile(DEFAULT_PROFILE_NAME)
        self._loaded_profile_values = dict(values)
        self._loaded_profile_notes = ""
        self._refresh_profile_list(select_name=DEFAULT_PROFILE_NAME)
        self._log("No profiles existed yet, so \"%s\" was created from your current "
                   "Configuration values and loaded (see the Profiles tab). It's a "
                   "normal profile - rename, update or delete it however you like."
                   % DEFAULT_PROFILE_NAME)

    def _refresh_profile_list(self, select_name=None):
        names = sorted(self._profiles.keys(), key=str.lower)
        if hasattr(self, "profile_combo"):
            self.profile_combo["values"] = names
        if select_name is not None:
            self.profile_select_var.set(select_name if select_name in names else "")
        elif self.profile_select_var.get() not in names:
            self.profile_select_var.set("")

    def _update_profile_status(self, *_args):
        """Refreshes the Profiles-tab status label: which profile (if any) is
        loaded, and whether the live Configuration fields / notes have since
        diverged from what was loaded - never applied silently, so this is the
        only signal the user gets that there's something to (re)save."""
        if not hasattr(self, "profile_status_var"):
            return
        if not self._loaded_profile_name:
            self.profile_status_var.set("No profile loaded.")
            return
        dirty = (self._current_profile_snapshot() != self._loaded_profile_values
                  or self._current_profile_notes() != self._loaded_profile_notes)
        if dirty:
            self.profile_status_var.set(
                "Loaded profile: \"%s\" - fields or notes have changed since loading. "
                "Use \"Update selected profile\" to save these changes into it, or "
                "\"Save as new profile\" to keep both." % self._loaded_profile_name)
        else:
            self.profile_status_var.set(
                "Loaded profile: \"%s\" (matches the saved profile)." % self._loaded_profile_name)

    def _on_profile_notes_modified(self, _event=None):
        # tk.Text's <<Modified>> flag latches "on" and must be reset by hand, or it
        # never fires again for subsequent edits.
        self.profile_notes_text.edit_modified(False)
        self._update_profile_status()

    # ------------------------------------------------- active profile ------ #
    def _active_profile_name(self):
        """Which profile Save writes into: whichever the user last created/loaded
        (restored from ACTIVE_PROFILE_KEY at startup), or DEFAULT_PROFILE_NAME until
        they pick one."""
        return self._active_profile or DEFAULT_PROFILE_NAME

    def _set_active_profile(self, name):
        """Mark `name` as loaded AND remember it across launches (ACTIVE_PROFILE_KEY).
        Persisted the moment it changes rather than at close, so a crash or a kill from
        Task Manager can't lose the choice."""
        self._loaded_profile_name = name
        if is_autosave_profile(name):
            # Loadable, but never the active one - the timer rewrites it, so Saving
            # into it would lose the values within 10 minutes. Whichever profile was
            # active stays active (and stays on disk), so recovering values from the
            # autosave doesn't also erase which profile the user was working in.
            return
        self._active_profile = name
        self._write_config_key(ACTIVE_PROFILE_KEY, name or "", "active profile")

    def _save_active_profile(self):
        """Write the current Configuration fields + notes into the active profile.
        Called from on_save only - loading a profile still changes nothing on disk
        beyond which profile is active."""
        name = self._active_profile_name()
        self._profiles[name] = {"values": self._current_profile_snapshot(),
                                "notes": self._current_profile_notes()}
        if not self._save_profiles(quiet=True):
            return
        self._set_active_profile(name)
        self._loaded_profile_values = dict(self._profiles[name]["values"])
        self._loaded_profile_notes = self._profiles[name]["notes"]
        self._refresh_profile_list(select_name=name)
        self._update_profile_status()
        self._log("Saved into profile \"%s\" (the active profile)." % name)

    def _apply_profile(self, name):
        """Populate the Configuration fields + notes from a profile and make it the
        active one. Shared by the Load button and the startup restore, so a profile
        restored on launch lands in exactly the same state as one loaded by hand."""
        profile = self._profiles[name]
        values = profile.get("values", {})
        for key in self.vars:
            # Only keys the profile actually carries. A profile saved before a field
            # existed (ARCHIPELAGO_DIR is the one that bit us) has no entry for it, and
            # blanking on a missing key made the startup restore wipe whatever
            # _load_json had just put in that field - so it looked like the new field
            # never persisted at all. Applies to every field added from here on.
            if key in values:
                self.set(key, values[key])
        notes = profile.get("notes", "")
        self.profile_notes_text.delete("1.0", "end")
        self.profile_notes_text.insert("1.0", notes)
        self.profile_notes_text.edit_modified(False)
        self._set_active_profile(name)
        self._loaded_profile_values = self._current_profile_snapshot()
        self._loaded_profile_notes = notes
        self._update_profile_status()

    def _restore_active_profile(self, saved):
        """Re-load whichever profile was active when the app last closed, instead of
        always coming up on DEFAULT_PROFILE_NAME.

        No Save is required afterwards: unlike the Load button (a new, pending choice
        the user has to confirm with Save), this is the state already persisted on
        disk - the .bat files and config JSON were written from these very values last
        session. Nothing is written back here either, so the Profiles tab still shows
        "matches the saved profile" until something is actually edited."""
        name = (saved or {}).get(ACTIVE_PROFILE_KEY) or ""
        if not name or is_autosave_profile(name) or name not in self._profiles:
            return
        self._apply_profile(name)
        self._refresh_profile_list(select_name=name)
        self._log("Loaded the profile that was active last session: \"%s\"." % name)

    def _on_load_profile(self):
        name = self.profile_select_var.get()
        if not name or name not in self._profiles:
            messagebox.showwarning("ARKIpelago Launcher", "Select a profile to load first.")
            return
        self._apply_profile(name)
        self._log("Loaded profile \"%s\" into the Configuration fields." % name)
        messagebox.showinfo(
            "ARKIpelago Launcher",
            "Profile \"%s\" has been loaded into the Configuration fields and notes.\n\n"
            "Nothing has been written to disk yet - go to the Configuration tab and "
            "press Save to apply these values." % name)

    def _on_save_new_profile(self):
        name = simpledialog.askstring("Save as new profile", "Profile name:", parent=self)
        if name is None:
            return
        name = name.strip()
        if not name:
            return
        if is_autosave_profile(name):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "\"%s\" is reserved for the launcher's automatic 10-minute snapshot, "
                "which would overwrite anything you saved there.\n\nPick a different "
                "name." % AUTOSAVE_PROFILE_NAME)
            return
        if name in self._profiles and not messagebox.askyesno(
                "ARKIpelago Launcher",
                "A profile named \"%s\" already exists. Overwrite it?" % name):
            return
        self._profiles[name] = {
            "values": self._current_profile_snapshot(),
            "notes": self._current_profile_notes(),
        }
        if not self._save_profiles():
            return
        self._set_active_profile(name)
        self._loaded_profile_values = dict(self._profiles[name]["values"])
        self._loaded_profile_notes = self._profiles[name]["notes"]
        self._refresh_profile_list(select_name=name)
        self._update_profile_status()
        self._log("Saved profile \"%s\"." % name)

    def _on_update_profile(self):
        name = self.profile_select_var.get()
        if not name or name not in self._profiles:
            messagebox.showwarning("ARKIpelago Launcher", "Select a profile to update first.")
            return
        if is_autosave_profile(name):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "\"%s\" is maintained by the launcher - it's rewritten with your "
                "current Configuration values every 10 minutes anyway, and the next "
                "autosave would replace anything you put there.\n\nUse \"Save as new "
                "profile\" to keep these values." % AUTOSAVE_PROFILE_NAME)
            return
        if not messagebox.askyesno(
                "ARKIpelago Launcher",
                "Overwrite profile \"%s\" with the current Configuration fields and "
                "notes?" % name):
            return
        self._profiles[name] = {
            "values": self._current_profile_snapshot(),
            "notes": self._current_profile_notes(),
        }
        if not self._save_profiles():
            return
        self._set_active_profile(name)
        self._loaded_profile_values = dict(self._profiles[name]["values"])
        self._loaded_profile_notes = self._profiles[name]["notes"]
        self._update_profile_status()
        self._log("Updated profile \"%s\"." % name)

    def _on_rename_profile(self):
        name = self.profile_select_var.get()
        if not name or name not in self._profiles:
            messagebox.showwarning("ARKIpelago Launcher", "Select a profile to rename first.")
            return
        if is_autosave_profile(name):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "\"%s\" is the launcher's automatic snapshot and can't be renamed - "
                "the next autosave would just recreate it under that name.\n\nLoad it "
                "and use \"Save as new profile\" if you want a copy you own."
                % AUTOSAVE_PROFILE_NAME)
            return
        new_name = simpledialog.askstring("Rename profile", "New name:",
                                           initialvalue=name, parent=self)
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name or new_name == name:
            return
        if is_autosave_profile(new_name):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "\"%s\" is reserved for the launcher's automatic snapshot - renaming a "
                "profile to it would get it overwritten within 10 minutes.\n\nPick a "
                "different name." % AUTOSAVE_PROFILE_NAME)
            return
        if new_name in self._profiles and not messagebox.askyesno(
                "ARKIpelago Launcher",
                "A profile named \"%s\" already exists. Overwrite it?" % new_name):
            return
        self._profiles[new_name] = self._profiles.pop(name)
        if not self._save_profiles():
            # Roll back the in-memory rename so it doesn't drift from disk.
            self._profiles[name] = self._profiles.pop(new_name)
            return
        if self._loaded_profile_name == name:
            self._set_active_profile(new_name)
        self._refresh_profile_list(select_name=new_name)
        self._update_profile_status()
        self._log("Renamed profile \"%s\" -> \"%s\"." % (name, new_name))

    def _on_delete_profile(self):
        name = self.profile_select_var.get()
        if not name or name not in self._profiles:
            messagebox.showwarning("ARKIpelago Launcher", "Select a profile to delete first.")
            return
        if is_autosave_profile(name):
            # Deletable, but pointless - said plainly rather than silently re-creating it.
            if not messagebox.askyesno(
                    "ARKIpelago Launcher",
                    "\"%s\" is the launcher's automatic snapshot of your Configuration "
                    "tab, rewritten every 10 minutes while the app is open.\n\nYou can "
                    "delete it, but the next autosave will recreate it - and until then "
                    "you'd have no automatic backup to fall back on.\n\nDelete it "
                    "anyway?" % AUTOSAVE_PROFILE_NAME):
                return
        elif not messagebox.askyesno(
                "ARKIpelago Launcher",
                "Delete profile \"%s\"? This cannot be undone." % name):
            return
        removed = self._profiles.pop(name)
        if not self._save_profiles():
            self._profiles[name] = removed  # roll back
            return
        if is_autosave_profile(name):
            # Forget what was last autosaved, so the next tick writes a fresh snapshot
            # instead of deciding "nothing changed" and leaving the slot gone.
            self._autosave_last_values = None
        if self._loaded_profile_name == name:
            self._set_active_profile(None)
            self._loaded_profile_values = None
            self._loaded_profile_notes = None
        self._refresh_profile_list()
        self._update_profile_status()
        self._log("Deleted profile \"%s\"." % name)

    # -------------------------------------------- ARK connect command ------ #
    def _copy_connect_command(self):
        slot = self.get("slot")
        server = self.get("server")
        password = self.get("password")
        if not slot or not server:
            messagebox.showwarning("ARKIpelago Launcher",
                                    "Set slot and server first.")
            return
        # Order is "/connect <server> <slot> [password]" - server FIRST. The in-game
        # command changed from the old slot-first form, so this must not be "fixed"
        # back to reading more naturally. Password is appended only when set: a
        # trailing empty argument would be parsed as a blank password rather than as
        # "no password", which fails against a room that has none.
        cmd = "/connect %s %s" % (server, slot)
        if password:
            cmd += " %s" % password
        self.clipboard_clear()
        self.clipboard_append(cmd)
        self._log("Copied to clipboard: %s" % cmd)

    def _copy_port(self):
        """Copy just the port from the server field. get() returns "" while the greyed
        placeholder is showing, so an empty field can never copy the example port."""
        port = server_port(self.get("server"))
        if not port:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "No port to copy - set the server field to host:port first "
                "(for example archipelago.gg:38281).")
            return
        self.clipboard_clear()
        self.clipboard_append(port)
        self._log("Copied port to clipboard: %s" % port)

    # ------------------------------------ SERVER_ROOT auto-detect (broad) -- #
    def _start_auto_detect(self):
        if self._detect_thread is not None and self._detect_thread.is_alive():
            return
        self._detect_cancelled = False
        self._detect_queue = queue.Queue()
        self._set_scan_busy(True)
        self.detect_status_label.configure(text="Scanning common locations...")
        self._detect_thread = threading.Thread(
            target=self._auto_detect_worker, daemon=True)
        self._detect_thread.start()
        self.after(150, self._poll_detect_queue)

    def _auto_detect_worker(self):
        q = self._detect_queue
        found = None
        for cand in direct_candidate_server_roots():
            if is_ark_server_root(cand):
                found = cand
                break
        if not found:
            q.put(("status", "Scanning common locations... (checking drives, "
                              "this can take up to ~20s)"))
            found = bounded_drive_scan(lambda line: q.put(("line", line)),
                                        lambda: self._detect_cancelled)
        q.put(("result", found))

    def _poll_detect_queue(self):
        try:
            while True:
                kind, payload = self._detect_queue.get_nowait()
                if kind == "status":
                    self.detect_status_label.configure(text=payload)
                elif kind == "line":
                    self._log(payload)
                elif kind == "result":
                    self._on_auto_detect_done(payload)
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll_detect_queue)

    def _on_auto_detect_done(self, found):
        self._detect_thread = None
        # Set only by the merged "Scan for paths" button (see _on_scan_button) so its
        # chosen intensity carries through to the follow-up scoped scan below. Left
        # unset (-> Quick) for every other caller of _start_auto_detect - the
        # first-launch auto-kick and any future passive trigger - since those happen
        # without the user asking for a specific intensity.
        level = self._pending_scan_level
        self._pending_scan_level = None
        if found:
            found = os.path.normpath(found)
            self._log("Auto-detect found SERVER_ROOT: %s" % found)
            self.set("SERVER_ROOT", found)
            self._set_scan_status("Found: %s - scanning for the rest..." % found)
            self._scoped_scan(level=level or SCAN_QUICK)
            if (level or SCAN_QUICK) == SCAN_QUICK:
                # Quick runs to completion synchronously inside _scoped_scan above, so
                # the busy state _start_auto_detect turned on needs clearing here.
                # Thorough/Exhaustive instead keep it on and clear it themselves when
                # their background thread finishes (see _poll_scan_queue).
                self._set_scan_busy(False)
        else:
            self._set_scan_busy(False)
            self._set_scan_status(
                "No ARK server install found automatically - enter SERVER_ROOT manually.")
            self._log("Auto-detect: no ARK server install found in common locations.")

    # ------------------------------ SERVER_ROOT-scoped scan ("Scan for paths") - #
    def _on_server_root_focus_out(self, _event=None):
        root = self.get("SERVER_ROOT")
        if not root or root == self._last_scoped_scan_root:
            return
        # Leaving the field always runs Quick, whatever intensity is selected: a
        # focus change must never silently kick off a minute-long walk. The
        # explicit "Scan for paths" button is what runs Thorough/Exhaustive.
        self._scoped_scan(level=SCAN_QUICK)

    def _on_scan_button(self):
        """The single "Scan for paths" button: finds every Configuration path in one
        click. If SERVER_ROOT is already set and looks right, jumps straight to the
        scoped scan around it (what the old "Scan for paths" button did). Otherwise
        it finds SERVER_ROOT first (what the old separate "Auto-detect..." button
        did) and then automatically runs the scoped scan once it's found, at the
        intensity chosen in scan_level_var - the same end result as pressing both
        old buttons in sequence."""
        if self._scan_running() or (self._detect_thread is not None
                                     and self._detect_thread.is_alive()):
            self._log("Scan for paths: a scan is already running.")
            return
        level = self.scan_level_var.get()
        root = self.get("SERVER_ROOT")
        if root and os.path.isfile(os.path.join(os.path.normpath(root), ARK_EXE_RELPATH)):
            self._scoped_scan(level=level, force=True)
            return
        self._log("Scan for paths: SERVER_ROOT isn't set (or doesn't look right yet) - "
                  "looking for the ARK server install first...")
        self._pending_scan_level = level
        self._start_auto_detect()

    def _scan_running(self):
        return self._scan_thread is not None and self._scan_thread.is_alive()

    def _scoped_scan(self, level=None, force=False):
        """Given SERVER_ROOT, fill in PLUGINS_DIR / ipc_dir / game_ini and suggest the
        cluster folders, at the requested intensity (see SCAN_LEVELS).

        Quick only stats fixed sub-paths plus one single-level listing, so it runs
        inline on the UI thread as before. Thorough/Exhaustive add a bounded recursive
        walk and always run on a worker thread with the spinner showing, so the GUI
        never blocks - results are applied back on the UI thread in _on_scan_done."""
        level = level or SCAN_LEVEL_DEFAULT
        if self._scan_running():
            self._log("Scan for paths: a scan is already running.")
            return
        root = self.get("SERVER_ROOT")
        if not root:
            return
        root = os.path.normpath(root)
        self._last_scoped_scan_root = root

        exe = os.path.join(root, ARK_EXE_RELPATH)
        if not os.path.isfile(exe):
            self._log("Scan for paths: SERVER_ROOT doesn't look right - expected:\n  %s" % exe)
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "SERVER_ROOT doesn't look right - ShooterGameServer.exe was not found at:\n\n"
                "%s\n\nDouble check the path (it should be the folder that directly contains "
                "'ShooterGame')." % exe)
            return

        if level == SCAN_QUICK:
            self._set_scan_status("Scanning expected paths...")
            result = scoped_scan_paths(root, SCAN_QUICK)
            self._on_scan_done(result, SCAN_QUICK)
            return

        # Thorough / Exhaustive: background thread + spinner.
        self._scan_cancelled = False
        self._scan_queue = queue.Queue()
        self._set_scan_busy(True)
        self._set_scan_status("%s scan running - this can take %s..." % (
            level, "a few seconds" if level == SCAN_THOROUGH else "a minute or more"))
        self._log("Scan for paths (%s): searching under and around %s ..." % (level, root))
        self._scan_thread = threading.Thread(
            target=self._scan_worker, args=(root, level), daemon=True)
        self._scan_thread.start()
        self.after(150, self._poll_scan_queue)

    def _scan_worker(self, root, level):
        q = self._scan_queue
        try:
            result = scoped_scan_paths(
                root, level,
                is_cancelled=lambda: self._scan_cancelled,
                progress=lambda path: q.put(("progress", path)))
        except Exception as exc:  # a scan must never take the app down with it
            q.put(("error", str(exc)))
            return
        q.put(("result", (result, level)))

    def _poll_scan_queue(self):
        try:
            while True:
                kind, payload = self._scan_queue.get_nowait()
                if kind == "progress":
                    # Tail of the path only - the full one is far too wide for the row.
                    self._set_scan_status("Searching %s ..." % os.path.basename(payload))
                elif kind == "error":
                    self._set_scan_busy(False)
                    self._scan_thread = None
                    self._set_scan_status("Scan failed - see the log.")
                    self._log("! Scan for paths failed: %s" % payload)
                    return
                elif kind == "result":
                    result, level = payload
                    self._set_scan_busy(False)
                    self._scan_thread = None
                    self._on_scan_done(result, level)
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll_scan_queue)

    def _set_scan_status(self, text):
        self.detect_status_label.configure(text=text)

    def _set_scan_busy(self, busy):
        """Show/hide the indeterminate spinner and lock the scan controls while a
        background scan runs. Everything else in the app stays usable."""
        for widget in (self.scan_btn, self.scan_level_combo):
            try:
                widget.configure(state=("disabled" if busy else
                                        ("readonly" if widget is self.scan_level_combo
                                         else "normal")))
            except tk.TclError:
                pass
        try:
            if busy:
                self.scan_progress.pack(side="left", padx=(6, 0))
                self.scan_progress.start(60)
            else:
                self.scan_progress.stop()
                self.scan_progress.pack_forget()
        except tk.TclError:
            pass

    def _on_scan_done(self, result, level):
        """Apply a scan result to the Configuration fields (UI thread only)."""
        filled, skipped = [], []
        suggestions = {}

        # PLUGINS_DIR is derived from SERVER_ROOT and is correct even before ArkApi
        # exists, so it is ALWAYS written back when the field is empty - this is the
        # fix for it staying empty after a successful SERVER_ROOT scan. A field that's
        # already set to something else is never silently overwritten - it's offered
        # via the Folder suggestions popup below instead, same as the cluster folders,
        # so re-scanning surfaces a conflict instead of hiding it.
        for key in ("PLUGINS_DIR", "ipc_dir", "game_ini"):
            value = result.get(key) or ""
            if not value:
                continue
            current = self.get(key)
            if current and os.path.normcase(current) != os.path.normcase(value):
                skipped.append(key)
                suggestions[key] = [value]
                continue
            self.set(key, value)
            filled.append(key)

        if filled:
            self._log("Scan for paths: filled in %s from SERVER_ROOT." % ", ".join(filled))
        if skipped:
            self._log("Scan for paths: found a different value than what's set for %s - "
                      "see the Folder suggestions popup." % ", ".join(skipped))
        if not result.get("plugins_exists"):
            self._log("Scan for paths: the ArkApi Plugins folder doesn't exist yet - "
                      "PLUGINS_DIR has been set to where it WILL be (%s). It's created "
                      "when you install ArkServerApi / the plugin."
                      % (result.get("PLUGINS_DIR") or "?"))
        if not result.get("ipc_dir"):
            self._log("Scan for paths: the ArkAP plugin isn't installed yet, so ipc_dir "
                      "was left alone (Install Server/Api/Plugin -> Install Plugin).")
        if not result.get("game_ini"):
            self._log("Scan for paths: Game.ini not found yet - start the server once to "
                      "generate it.")
        for note in result.get("notes", []):
            self._log("Scan for paths: %s" % note)

        stopped = result.get("stopped")
        if stopped == "cancelled":
            self._log("Scan for paths: cancelled.")
        elif stopped:
            self._log("Scan for paths: %s after %d folders - results may be incomplete. "
                      "Try Exhaustive, or set the remaining paths with Browse."
                      % (stopped, result.get("visited", 0)))

        # Cluster folders are name-matched guesses wherever they were found, so they're
        # always offered rather than applied - regardless of whether the field already
        # has a value, so re-scanning can surface a better/different candidate instead
        # of silently assuming the existing value is fine (_suggest_paths itself drops
        # a field whose only candidate already matches what's set).
        suggestions.update((k, v) for k, v in (result.get("suggestions") or {}).items() if v)
        summary_bits = []
        if filled:
            summary_bits.append("filled %s" % ", ".join(filled))
        if suggestions:
            summary_bits.append("%d folder suggestion(s)" % sum(len(v) for v in suggestions.values()))
        self._set_scan_status("%s scan done%s." % (
            level, (" - " + "; ".join(summary_bits)) if summary_bits else
            " - nothing new found"))

        if suggestions:
            self._suggest_paths(suggestions)
        elif level != SCAN_QUICK:
            self._log("Scan for paths (%s): visited %d folders."
                      % (level, result.get("visited", 0)))

    def _on_cluster_dir_focus_out(self, _event=None):
        self._scan_for_saves_and_backup_root(self.get("CLUSTERDIR"))

    def _scan_for_saves_and_backup_root(self, cluster_dir):
        """Given a found/confirmed CLUSTERDIR, look in its parent folder (the same
        ServerCluster-style parent CLUSTERDIR itself is a sibling of) for SAVESROOT
        (a sibling matching 'saves') and BACKUPROOT (a sibling matching 'backups').
        Folder-name matching is a guess here too, so these are always surfaced as
        suggestions to confirm - never silently filled in, and offered even if the
        field already has a value (_suggest_paths drops a field whose only candidate
        already matches what's set, so this only prompts on an actual conflict).
        BACKUPROOT especially may not exist yet (some setups only create it on the
        first backup), so when no matching sibling is found we still offer the
        expected sibling path as an unconfirmed placeholder rather than treating that
        as an error - and when NEITHER exists, the popup says so and points at the
        "Create ServerCluster folders" button, which is the actual fix on a fresh
        install.

        Called on every CLUSTERDIR change, not just the field's FocusOut (see set()),
        so the same folder is only ever looked at once (_last_cluster_dir_scan) - typing
        a path and then tabbing out must not stack two identical popups."""
        if not cluster_dir or not os.path.isdir(cluster_dir):
            return
        if os.path.normpath(cluster_dir) == self._last_cluster_dir_scan:
            return
        if is_backup_snapshot_path(cluster_dir):
            # A CLUSTERDIR inside a snapshot makes every sibling here a snapshot folder
            # too - including the "offer the expected sibling path anyway" fallback below,
            # which doesn't go through classify_cluster_folder at all.
            return
        cluster_dir = os.path.normpath(cluster_dir)
        self._last_cluster_dir_scan = cluster_dir
        parent = os.path.dirname(cluster_dir)
        if not parent or not os.path.isdir(parent):
            return

        siblings = []
        try:
            with os.scandir(parent) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        siblings.append(entry.path)
        except OSError:
            return

        suggestions = {}
        found_any = False
        for key, pattern, default_name in (
            ("SAVESROOT", "saves", "Saves"),
            ("BACKUPROOT", "backups", "Backups"),
        ):
            # classify_cluster_folder() has to agree with the name pattern: it's
            # the one place that knows which real-but-wrong ARK folders (SavedArks,
            # the Cluster-<Map> junctions, timestamped _backup_ snapshots) must
            # never be offered as a configured path, and CLUSTERDIR can sit close
            # enough to ShooterGame\Saved for those to turn up as siblings.
            matches = [p for p in siblings
                       if pattern in os.path.basename(p).lower()
                       and classify_cluster_folder(os.path.basename(p)) == key]
            found_any = found_any or bool(matches)
            suggestions[key] = matches if matches else [os.path.join(parent, default_name)]

        # Neither sibling exists: every "suggestion" below is a not-yet-created example,
        # which on its own reads as a dead end. Say what to do instead.
        note = None
        if not found_any:
            note = ("No Saves or Backups folder was found next to this CLUSTERDIR. The "
                    "paths below are only where they would go - use \"Create %s "
                    "folders\" on this tab to create them (and fill in all three "
                    "fields) if this is a fresh install." % CLUSTER_ROOT_DIRNAME)
            self._log("CLUSTERDIR: no Saves/Backups sibling folders next to %s - use "
                      "\"Create %s folders\" to create them."
                      % (cluster_dir, CLUSTER_ROOT_DIRNAME))
        self._suggest_paths(suggestions, note=note)

    # Every field the scan can suggest a folder for. Fixed order so the popup is
    # consistent regardless of which caller (SERVER_ROOT scan vs. CLUSTERDIR focus-out)
    # happened to supply which keys.
    _SUGGESTABLE_KEYS = ("PLUGINS_DIR", "ipc_dir", "game_ini",
                         "CLUSTERDIR", "SAVESROOT", "BACKUPROOT")

    def _suggest_paths(self, suggestions, note=None):
        """One dialog offering every name-matched folder the scan turned up, grouped by
        the field it would fill. Suggestions are never applied without a click, since
        folder-name matching is a guess. A path that doesn't exist yet is offered as a
        greyed-out example (it's a suggested location, not a found folder).

        Shown regardless of whether a field already has a value - re-scanning is often
        deliberate, to review or correct an earlier choice - but a field is skipped if
        every candidate found for it is just the value it already has, since there's
        nothing to compare there. When the field's current value isn't among the
        candidates, it's still shown (tagged "(set)") so the user can compare it
        against the alternatives rather than only seeing the alternatives."""
        sections = suggestion_sections(self._SUGGESTABLE_KEYS, suggestions, self.get)
        if not sections:
            return

        win = tk.Toplevel(self)
        win.title("Folder suggestions")
        win.resizable(False, False)
        win.transient(self)
        ttk.Label(win, padding=10, wraplength=520, justify="left",
                  text="The scan found folder(s) that look like they could go in these "
                       "fields. Pick one to use it, or close this to leave a field as it "
                       "is - your current value (if any) is shown marked \"(set)\".").pack()
        if note:
            # Same pale-yellow banner as the install reminder - this is advice, not a
            # suggestion the user can click.
            banner = tk.Frame(win, background=self.theme["warn_bg"],
                              highlightbackground=self.theme["warn_border"],
                              highlightthickness=1)
            banner.pack(fill="x", padx=10, pady=(0, 6))
            tk.Label(banner, background=self.theme["warn_bg"],
                     foreground=self.theme["warn_fg"], justify="left", wraplength=500,
                     text=note).pack(padx=8, pady=6)
        # Close stays pinned at the bottom while the list above scrolls.
        ttk.Button(win, text="Close", command=win.destroy).pack(side="bottom", pady=(4, 10))

        # Scrollable list - same Canvas + Scrollbar pattern as the Setup Status /
        # Server Install / Archipelago Setup tabs. An Exhaustive scan (Desktop /
        # Documents / Downloads sweep) can turn up far more candidates than fit on
        # screen, across several fields at once, and every one has to stay reachable.
        body = ttk.Frame(win)
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, borderwidth=0, highlightthickness=0,
                           background=self.theme["bg"])
        vsb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))
        # Bound on the Toplevel rather than bind_all so it dies with the window and
        # can't leak wheel handling onto the main window after this popup closes.
        win.bind("<MouseWheel>",
                 lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        for key, current, cur_norm, matches in sections:
            ttk.Label(inner, text=key, font=("Segoe UI", 9, "bold")
                      ).pack(anchor="w", padx=10, pady=(6, 0))
            if current and cur_norm not in (os.path.normcase(os.path.normpath(m))
                                             for m in matches):
                ttk.Button(inner, text="%s   (set)" % current, state="disabled"
                           ).pack(fill="x", padx=10, pady=2)
            for m in matches:
                is_current = cur_norm == os.path.normcase(os.path.normpath(m))
                exists = os.path.isdir(m)
                if is_current:
                    label = "%s   (set)" % m
                elif exists:
                    label = m
                else:
                    label = "%s   (not created yet - suggested path)" % m
                # A path that exists was really found on disk; one that doesn't is
                # only an example of where it would go, so it's greyed out.
                btn = ttk.Button(inner, text=label,
                                  style="TButton" if exists or is_current else "Placeholder.TButton",
                                  state="disabled" if is_current else "normal")
                if not is_current:
                    btn.configure(command=lambda p=m, k=key, b=btn: self._pick_suggested_path(k, p, b))
                btn.pack(fill="x", padx=10, pady=2)

        # Size the viewport to the content, capped so a scan with dozens of hits
        # scrolls instead of growing the window past the screen. The scrollbar only
        # appears once it's actually needed.
        inner.update_idletasks()
        max_h = max(200, min(SUGGEST_POPUP_MAX_LIST_H, win.winfo_screenheight() - 300))
        content_h = inner.winfo_reqheight()
        canvas.configure(width=inner.winfo_reqwidth(), height=min(content_h, max_h))
        if content_h > max_h:
            vsb.pack(side="right", fill="y", before=canvas)
        return win

    def _pick_suggested_path(self, key, path, button):
        self.set(key, path)
        self._log("%s set from suggestion: %s" % (key, path))
        # Once picked it's a real configured value, not an example - undim it.
        button.configure(text="%s  (set)" % path, state="disabled", style="TButton")

    # ------------------------------------------------- SteamCMD install ---- #
    def _install_log(self, line):
        self.install_log.configure(state="normal")
        self.install_log.insert("end", line.rstrip("\n") + "\n")
        self.install_log.see("end")
        self.install_log.configure(state="disabled")
        launcher_log(line, "Install")  # server / ArkApi / plugin installs + SteamCMD output

    def _any_install_running(self):
        """True if the SteamCMD, ArkServerApi, ArkAP plugin, or mod-download flow is active.
        The first three share the install_log widgets; the mod flow has its own log but
        still runs SteamCMD against the same SERVER_ROOT, so all four are mutually
        exclusive - two SteamCMD runs at once would fight over the steamapps lock."""
        return ((self._install_thread is not None and self._install_thread.is_alive())
                or (self._arkapi_thread is not None and self._arkapi_thread.is_alive())
                or (self._plugin_thread is not None and self._plugin_thread.is_alive())
                or (self._mods_thread is not None and self._mods_thread.is_alive()))

    def on_install_server(self):
        if self._any_install_running():
            messagebox.showinfo("ARKIpelago Launcher", "An install is already running.")
            return
        server_root = self.get("SERVER_ROOT")
        if not server_root:
            messagebox.showwarning("ARKIpelago Launcher", "Set SERVER_ROOT first.")
            return

        self.install_log.configure(state="normal")
        self.install_log.delete("1.0", "end")
        self.install_log.configure(state="disabled")

        self._install_queue = queue.Queue()
        self._install_cancelled = False
        self.install_btn.configure(state="disabled")
        self.arkapi_install_btn.configure(state="disabled")
        self.install_cancel_btn.configure(state="normal")
        self.install_status_var.set("Installing...")
        # The progress bar is shared with the ArkServerApi flow, which uses determinate
        # mode - make sure it's back in indeterminate mode for SteamCMD's own progress style.
        self.install_progress.configure(mode="indeterminate")
        self.install_progress.start(80)

        self._install_thread = threading.Thread(
            target=self._install_worker, args=(server_root,), daemon=True)
        self._install_thread.start()
        self.after(100, self._poll_install_queue)

    def on_cancel_install(self):
        self._install_cancelled = True
        self.install_status_var.set("Cancelling...")
        if self._install_proc is not None:
            self._install_log("Cancelling...")
            try:
                self._install_proc.terminate()
            except OSError:
                pass
        self.install_cancel_btn.configure(state="disabled")

    def _tail_console_log(self, path, offset, q, stop_event, active_event):
        """Poll logs/console_log.txt for lines SteamCMD has appended since the
        install started, and forward each *new* one to the GUI queue live. This
        is what actually shows progress in real time - see the comment above the
        SteamCMD Popen call in _install_worker for why raw stdout can't do it."""
        splitter = _ConsoleLineSplitter()

        def _drain_once():
            nonlocal offset
            try:
                size = os.path.getsize(path)
            except OSError:
                return
            if size <= offset:
                return
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
            except OSError:
                return
            offset = size
            for line in splitter.feed(chunk):
                stripped = line.strip()
                if stripped:
                    active_event.set()
                    q.put(("line", stripped))

        while not stop_event.is_set():
            _drain_once()
            time.sleep(0.3)
        _drain_once()  # one last catch-up read for anything written just before stop
        rest = splitter.flush().strip()
        if rest:
            q.put(("line", rest))

    def _ensure_steamcmd(self, q):
        exe = steamcmd_exe_path()
        if os.path.isfile(exe):
            return exe
        d = steamcmd_dir()
        os.makedirs(d, exist_ok=True)
        q.put(("line", "steamcmd.exe not found - downloading from %s" % STEAMCMD_ZIP_URL))
        zip_path = os.path.join(d, "steamcmd.zip")
        with urllib.request.urlopen(STEAMCMD_ZIP_URL, timeout=30) as resp:
            data = resp.read()
        with open(zip_path, "wb") as f:
            f.write(data)
        q.put(("line", "Downloaded steamcmd.zip - extracting..."))
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(d)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        if not os.path.isfile(exe):
            raise RuntimeError("steamcmd.exe still missing after extraction.")
        q.put(("line", "SteamCMD ready."))
        return exe

    def _install_worker(self, server_root):
        q = self._install_queue
        try:
            exe = self._ensure_steamcmd(q)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            q.put(("line", "! Could not prepare SteamCMD: %s" % exc))
            q.put(("done", False))
            return

        if self._install_cancelled:
            q.put(("line", "Install cancelled before SteamCMD started."))
            q.put(("done", False))
            return

        cmd = [exe, "+force_install_dir", server_root, "+login", "anonymous",
               "+app_update", ARK_APP_ID, "-beta", ARK_BETA_BRANCH, "validate", "+quit"]
        q.put(("line", "Running: %s" % " ".join(cmd)))

        # SteamCMD only flushes its own stdout in large, infrequent bursts once it
        # detects it isn't attached to a real console (confirmed by probing raw
        # read() calls against the pipe: every "Update state (...) downloading,
        # progress: NN.NN" line for an entire download arrived in a single burst
        # at the very end, regardless of how the read loop below splits lines).
        # No amount of clever reading on our side can pull data out of the pipe
        # before SteamCMD itself writes it. SteamCMD does, however, append the
        # exact same status/progress lines to logs/console_log.txt as it goes
        # (observed at roughly one line per ~2s during a real download), so we
        # tail that file on a background thread for the live progress feed, and
        # only fall back to displaying the raw stdout drain (still read below,
        # to prevent the pipe's OS buffer from filling and blocking SteamCMD)
        # until the tail thread confirms the log file is actually growing.
        console_log_path = os.path.join(steamcmd_dir(), "logs", "console_log.txt")
        try:
            log_start_offset = os.path.getsize(console_log_path)
        except OSError:
            log_start_offset = 0

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                cmd, cwd=steamcmd_dir(), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0,
                creationflags=creationflags)
        except OSError as exc:
            q.put(("line", "! Failed to launch SteamCMD: %s" % exc))
            q.put(("done", False))
            return

        self._install_proc = proc

        tail_stop = threading.Event()
        tail_active = threading.Event()
        tail_thread = threading.Thread(
            target=self._tail_console_log,
            args=(console_log_path, log_start_offset, q, tail_stop, tail_active),
            daemon=True)
        tail_thread.start()

        splitter = _ConsoleLineSplitter()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = proc.stdout.read(1024)
            if not chunk:
                break
            for line in splitter.feed(decoder.decode(chunk)):
                if not tail_active.is_set():
                    q.put(("line", line))
        final_text = decoder.decode(b"", final=True)
        for line in splitter.feed(final_text) if final_text else ():
            if not tail_active.is_set():
                q.put(("line", line))
        rest = splitter.flush()
        if rest and not tail_active.is_set():
            q.put(("line", rest))
        proc.stdout.close()
        returncode = proc.wait()
        self._install_proc = None

        # Give the tail thread a moment to pick up the final lines steamcmd just
        # wrote (Success!/Error! land in the log within a second or two of exit)
        # before shutting it down.
        time.sleep(0.5)
        tail_stop.set()
        tail_thread.join(timeout=2)

        exe_path = os.path.join(server_root, "ShooterGame", "Binaries", "Win64",
                                 "ShooterGameServer.exe")
        success = (returncode == 0) and os.path.isfile(exe_path)
        if success:
            q.put(("line", "Install finished successfully - found ShooterGameServer.exe."))
        elif self._install_cancelled:
            q.put(("line", "Install cancelled by user."))
        elif returncode != 0:
            q.put(("line", "! SteamCMD exited with code %s." % returncode))
        else:
            q.put(("line", "! SteamCMD exited 0 but ShooterGameServer.exe was not found at:\n%s"
                   % exe_path))
        q.put(("done", success))

    def _poll_install_queue(self):
        try:
            while True:
                kind, payload = self._install_queue.get_nowait()
                if kind == "line":
                    self._install_log(payload)
                elif kind == "done":
                    self._on_install_done(payload)
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll_install_queue)

    def _on_install_done(self, success):
        self.install_btn.configure(state="normal")
        self.arkapi_install_btn.configure(state="normal")
        self.install_cancel_btn.configure(state="disabled")
        self._install_thread = None
        self.install_progress.stop()
        if success:
            self.install_status_var.set("Done")
        elif self._install_cancelled:
            self.install_status_var.set("Cancelled")
        else:
            self.install_status_var.set("Failed")
        if success:
            server_root = self.get("SERVER_ROOT")
            if server_root:
                # Writes the install's own SERVER_ROOT straight into the Configuration
                # field (it's the same var as the one just used for +force_install_dir,
                # so this is normally a no-op, but it's cheap insurance) and runs the
                # same Quick scan that leaving the field normally triggers - self.set()
                # doesn't fire the <FocusOut> binding on its own, so without this,
                # PLUGINS_DIR/ipc_dir/game_ini would stay unfilled until the user
                # clicked into and back out of the field by hand.
                server_root = os.path.normpath(server_root)
                self.set("SERVER_ROOT", server_root)
                self._ensure_cluster_dirs(server_root)
                self._scoped_scan(level=SCAN_QUICK)
            messagebox.showinfo("ARKIpelago Launcher", "ARK server install/update complete.")
        elif not self._install_cancelled:
            messagebox.showerror("ARKIpelago Launcher",
                                  "ARK server install failed. See the log for details.")

    def _on_create_cluster_folders(self):
        """Configuration tab button. Same work as the post-install hook, but explicit
        and repeatable - for anyone whose install predates it, whose folders got moved
        or deleted, or who cleared the fields."""
        server_root = self.get("SERVER_ROOT")
        if not server_root:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "Set SERVER_ROOT first - the cluster folders are created inside it "
                "(in a \"%s\" folder within your server install)." % CLUSTER_ROOT_DIRNAME)
            return
        planned = default_cluster_paths(server_root)
        targets = {key: (self.get(key) or planned.get(key, ""))
                   for key, _ in CLUSTER_PATH_SUBDIRS}
        already = [k for k, p in targets.items() if p and os.path.isdir(p)]
        detail = "\n".join("  %-11s %s%s" % (k, targets[k],
                                             "   (already exists)" if k in already else "")
                           for k, _ in CLUSTER_PATH_SUBDIRS)
        if not messagebox.askyesno(
                "Create ServerCluster folders",
                "Create these folders and put them in the Configuration fields?\n\n%s\n\n"
                "SteamCMD never creates them, and the ARK server hangs on startup with "
                "no error message when CLUSTERDIR is missing. Nothing existing is "
                "modified or deleted - folders that are already there are left "
                "alone." % detail):
            self._log("Create cluster folders: cancelled.")
            return
        created, existing, failed, filled = self.create_cluster_folders(server_root)
        self._log("Create cluster folders:")
        for line in created:
            self._log("  created  %s" % line)
        for line in existing:
            self._log("  ok       %s" % line)
        for key, det in failed:
            self._log("  ! FAILED %s -> %s" % (key, det))
        if failed:
            messagebox.showerror(
                "ARKIpelago Launcher",
                "These folders could not be created:\n\n%s\n\nPick a location you can "
                "write to (Browse next to each field), then try again."
                % "\n".join("%s -> %s" % f for f in failed))
            return
        messagebox.showinfo(
            "ARKIpelago Launcher",
            "Cluster folders ready:\n\n%s\n\nThe Configuration fields now point at "
            "them - press Save so the .bat scripts pick them up."
            % "\n".join(created + existing))

    def create_cluster_folders(self, server_root):
        """Create CLUSTERDIR / SAVESROOT / BACKUPROOT and write them into the fields.

        Returns (created, existing, failed, filled) for the caller to report however
        suits it. Shared by the Configuration-tab button and the post-install hook so
        both produce exactly the same layout on disk.

        A path the user has really configured is used as-is; anything empty is filled
        from default_cluster_paths(). Note "really configured" excludes a field still
        holding the shipped example (is_unconfigured_example_path) - trusting those was
        what previously sent a fresh install's folders to C:\\ARKServer, nowhere near
        the actual server."""
        defaults = default_cluster_paths(server_root)
        created, existing, failed, filled = [], [], [], []
        for key, _sub in CLUSTER_PATH_SUBDIRS:
            configured = self.get(key)
            if configured and is_unconfigured_example_path(key, configured):
                configured = ""
            path = configured or defaults.get(key, "")
            if not path:
                failed.append((key, "(not set - SERVER_ROOT is empty, nothing to "
                                     "derive from)"))
                continue
            try:
                already = os.path.isdir(path)
                os.makedirs(path, exist_ok=True)
            except OSError as exc:
                failed.append((key, "%s (%s)" % (path, exc)))
                continue
            (existing if already else created).append("%s: %s" % (key, path))
            # Written back as a REAL value (normal black text), not a placeholder -
            # the folder now exists, so this is configuration, not a suggestion.
            if self.get(key) != path:
                self.set(key, path)
                filled.append(key)
        self._refresh_setup_status()
        return created, existing, failed, filled

    def _ensure_cluster_dirs(self, server_root):
        """Post-install hook: create the cluster folders and report into the install log.

        SteamCMD's `app_update 376030` never creates any cluster folder - the cluster
        dir is purely a runtime -ClusterDirOverride argument pointing at an arbitrary
        path of the user's choosing, so a fresh install has none of it and there is
        nothing for any amount of scanning to find. It was left to
        start_ase_server.bat's `mkdir` fallback, which is not enough on its own: that
        mkdir runs *before* the server binary (verified - it isn't racing the launch),
        but its failures are silent, so an unset or uncreatable CLUSTERDIR still ends
        up passed as -ClusterDirOverride= (or a nonexistent path), and ARK then stalls
        on startup instead of reporting an error.

        This hook has always run after a successful install; what made it look like it
        never did was the template-residue bug (is_unconfigured_example_path). The
        fields arrived pre-filled with the .bat templates' own C:\\ARKServer examples,
        this treated them as configured, and so it dutifully created the folders at
        C:\\ARKServer - on the wrong drive, nowhere near the real install, and often
        failing outright on a non-writable C:\\ root. The real server root was left
        with no cluster folder at all, which is exactly what users saw."""
        created, existing, failed, filled = self.create_cluster_folders(server_root)

        self._install_log("")
        self._install_log("Cluster folders (not created by SteamCMD - created now):")
        for line in created:
            self._install_log("  created  %s" % line)
        for line in existing:
            self._install_log("  ok       %s" % line)
        for key, detail in failed:
            self._install_log("  ! FAILED %s -> %s" % (key, detail))
        if filled:
            self._install_log(
                "  %s have been filled in on the Configuration tab - "
                "click Save there so the .bat scripts use them."
                % " / ".join(filled))
        if failed:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "The ARK server installed, but these cluster folders could not be "
                "created:\n\n%s\n\nUse \"Create ServerCluster folders\" on the "
                "Configuration tab after picking a writable location - the server can "
                "hang on startup if the cluster folder is missing."
                % "\n".join("%s -> %s" % f for f in failed))

    # ---------------------------------------- ArkServerApi (ArkApi) install - #
    def _fetch_latest_arkapi_release(self, q):
        """Query GitHub's releases API for the latest ArkServerApi/AseApi release.

        Returns (tag, asset_name, download_url, size_bytes). Raises RuntimeError/OSError/
        ValueError on failure - callers turn that into a log line + failure result."""
        req = urllib.request.Request(
            ARKSERVERAPI_RELEASES_API,
            headers={"User-Agent": GITHUB_API_USER_AGENT,
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = data.get("tag_name") or "unknown"
        assets = data.get("assets") or []
        zip_asset = next(
            (a for a in assets if a.get("name", "").lower().endswith(".zip")), None)
        if not zip_asset:
            raise RuntimeError("Latest release (%s) has no .zip asset." % tag)
        q.put(("line", "Latest release: %s - asset %s" % (tag, zip_asset["name"])))
        return tag, zip_asset["name"], zip_asset["browser_download_url"], zip_asset.get("size", 0)

    def _download_with_progress(self, url, dest_path, q):
        """Stream-download url to dest_path, pushing throttled ("progress", pct 0-100)
        and ("line", ...) updates to q. Falls back to no progress reporting (just the
        final byte count) if the server doesn't send Content-Length."""
        req = urllib.request.Request(url, headers={"User-Agent": GITHUB_API_USER_AGENT})
        downloaded = 0
        last_progress_emit = 0.0
        last_logged_decile = -1
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if not total:
                        continue
                    now = time.monotonic()
                    if now - last_progress_emit < 0.15:
                        continue
                    last_progress_emit = now
                    pct = min(100, int(downloaded * 100 / total))
                    q.put(("progress", pct))
                    decile = pct // 10
                    if decile != last_logged_decile:
                        last_logged_decile = decile
                        q.put(("line", "Downloading... %d%% (%.1f / %.1f MB)"
                               % (pct, downloaded / 1048576, total / 1048576)))
        if total:
            q.put(("progress", 100))
        q.put(("line", "Downloaded %.1f MB." % (downloaded / 1048576)))
        return downloaded

    def _extract_zip_to(self, zip_path, dest_dir, q):
        os.makedirs(dest_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            q.put(("line", "Extracting %d entries into %s..." % (len(names), dest_dir)))
            zf.extractall(dest_dir)

    def _arkapi_worker(self, win64):
        q = self._arkapi_queue
        try:
            q.put(("line", "Fetching latest ArkServerApi release info..."))
            tag, asset_name, url, size = self._fetch_latest_arkapi_release(q)
        except (OSError, ValueError, RuntimeError) as exc:
            q.put(("line", "! Could not fetch release info: %s" % exc))
            q.put(("done", False))
            return

        tmp_dir = tempfile.mkdtemp(prefix="arkapi_dl_")
        zip_path = os.path.join(tmp_dir, asset_name)
        try:
            q.put(("line", "Downloading %s..." % asset_name))
            self._download_with_progress(url, zip_path, q)
        except (OSError, ValueError) as exc:
            q.put(("line", "! Download failed: %s" % exc))
            shutil.rmtree(tmp_dir, ignore_errors=True)
            q.put(("done", False))
            return

        try:
            self._extract_zip_to(zip_path, win64, q)
        except (OSError, zipfile.BadZipFile) as exc:
            q.put(("line", "! Extraction failed: %s" % exc))
            shutil.rmtree(tmp_dir, ignore_errors=True)
            q.put(("done", False))
            return
        shutil.rmtree(tmp_dir, ignore_errors=True)

        version_dll = os.path.join(win64, "version.dll")
        arkapi_dir = os.path.join(win64, "ArkApi")
        success = os.path.isfile(version_dll) and os.path.isdir(arkapi_dir)
        if success:
            q.put(("line",
                   "ArkServerApi installed - found version.dll and ArkApi\\ in Win64\\."))
            # Record what we just installed so Setup Status can later flag a newer release.
            q.put(("version", tag))
        else:
            q.put(("line", "! Extraction finished but version.dll / ArkApi\\ were not found "
                            "in:\n%s" % win64))
        q.put(("done", success))

    def on_install_arkapi(self):
        if self._any_install_running():
            messagebox.showinfo("ARKIpelago Launcher", "An install is already running.")
            return
        server_root = self.get("SERVER_ROOT")
        if not server_root:
            messagebox.showwarning("ARKIpelago Launcher", "Set SERVER_ROOT first.")
            return
        server_root = os.path.normpath(server_root)
        win64 = os.path.join(server_root, "ShooterGame", "Binaries", "Win64")
        if not os.path.isdir(win64):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "Win64 folder not found under SERVER_ROOT:\n\n%s\n\nInstall the ARK "
                "dedicated server first (Install ARK Server above)." % win64)
            return

        if not messagebox.askyesno(
                "Install ArkServerApi",
                "Download the latest ArkServerApi (ArkApi) release from GitHub "
                "(ArkServerApi/AseApi) and extract it into:\n\n%s\n\n"
                "Existing ArkApi files there (version.dll, config.json, ArkApi\\, ...) "
                "will be overwritten if this is an upgrade. Your ArkAP plugin folder "
                "(ArkApi\\Plugins\\ArkAP) is not part of this download and is untouched."
                "\n\nProceed?" % win64):
            return

        self.install_log.configure(state="normal")
        self.install_log.delete("1.0", "end")
        self.install_log.configure(state="disabled")

        self._arkapi_queue = queue.Queue()
        self.install_btn.configure(state="disabled")
        self.arkapi_install_btn.configure(state="disabled")
        self.install_status_var.set("Installing...")
        self.install_progress.stop()
        self.install_progress.configure(mode="determinate", maximum=100)
        self.install_progress["value"] = 0

        self._arkapi_thread = threading.Thread(
            target=self._arkapi_worker, args=(win64,), daemon=True)
        self._arkapi_thread.start()
        self.after(100, self._poll_arkapi_queue)

    def _poll_arkapi_queue(self):
        try:
            while True:
                kind, payload = self._arkapi_queue.get_nowait()
                if kind == "line":
                    self._install_log(payload)
                elif kind == "version":
                    self._write_config_key(ARKAPI_INSTALLED_VERSION_KEY, payload,
                                           "installed ArkApi version")
                elif kind == "progress":
                    try:
                        self.install_progress["value"] = payload
                    except tk.TclError:
                        pass
                elif kind == "done":
                    self._on_arkapi_done(payload)
                    self._start_component_version_check()
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll_arkapi_queue)

    def _on_arkapi_done(self, success):
        self.install_btn.configure(state="normal")
        self.arkapi_install_btn.configure(state="normal")
        self._arkapi_thread = None
        self.install_progress.stop()
        if success:
            self.install_status_var.set("Done")
            messagebox.showinfo(
                "ARKIpelago Launcher",
                "ArkServerApi installed - version.dll and ArkApi\\ found in Win64\\.")
        else:
            self.install_status_var.set("Failed")
            messagebox.showerror("ARKIpelago Launcher",
                                  "ArkServerApi install failed. See the log for details.")

    # ------------------------------------------------ Launcher self-update - #
    # Downloading and the confirm-before-updating dialog are entirely opt-in: nothing
    # downloads unless the user clicks an action in that dialog. Checking for new versions
    # does also happen once, silently, on startup (see _start_component_version_check) - it
    # only lights up the "!" badge / button highlight (see _apply_update_indicators), never
    # opens the dialog itself. The launcher's own releases live in this app's OWN repo
    # (UPDATE_REPO), separate from RELEASES_URL / ARKSERVERAPI_RELEASES_API above, which
    # point at the plugin/connector/ArkApi bundle.
    @staticmethod
    def _fetch_release_list():
        """The repo's releases as a list (newest-first per GitHub). New clients pick the best
        installable one themselves (see _pick_best_release) instead of trusting /releases/
        latest, so a permanently-pinned bridge release can't hide newer onedir releases."""
        req = urllib.request.Request(
            UPDATE_RELEASES_LIST_API,
            headers={"User-Agent": GITHUB_API_USER_AGENT,
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []

    # ------------------------------------------- Component version advisories #
    def _read_config_dict(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _start_component_version_check(self):
        """ONE background pass over every tracked component (launcher, ArkAP plugin,
        .apworld, plus the advisory-only ArkApi). It drives BOTH the "!" badge / button
        highlight and the Setup Status advisory rows from the same fetches, so the two
        surfaces can never disagree and startup doesn't hit the same releases list twice.
        Silent and non-blocking; anything unreachable is simply left out."""
        threading.Thread(target=self._component_version_check_worker, daemon=True).start()

    def _write_config_key_async(self, key, value, label):
        """_write_config_key from a worker thread. The config file is otherwise only ever
        written from the main thread, and two writers doing read-modify-write would lose
        one of the keys."""
        try:
            self.after(0, self._write_config_key, key, value, label)
        except (tk.TclError, RuntimeError):
            pass  # window closed mid-check - see _component_version_check_worker

    def _detect_component_versions(self, cfg, releases):
        """{component: (installed_version, present_on_disk)} for the plugin and .apworld,
        read off the FILES first and falling back to the version we recorded at install
        time. Worker thread only.

        The recorded value used to be the only source, which is why a launcher that shipped
        with both components already in place showed neither: it installed neither, so it
        recorded neither. Whatever is detected is written back into the same recorded keys,
        so the diagnostics version block and the Setup Status advisories pick it up without
        knowing detection happened.

        Paths come from the config dict rather than the Tk variables - this runs off the
        main thread, and what's saved is what the connector and the plugin actually use."""
        out = {}

        plugin_dir = resolve_plugin_dir(cfg.get)
        dll = os.path.join(plugin_dir, PLUGIN_DLL_NAME) if plugin_dir else ""
        dll_sha = _file_sha256(dll) if dll else None
        version = str(cfg.get(PLUGIN_INSTALLED_VERSION_KEY, "") or "").strip()
        if dll_sha and dll_sha != cfg.get(PLUGIN_PROBED_DLL_SHA_KEY, ""):
            detected = plugin_version_from_disk(dll_sha, releases)
            # Recorded even when nothing matched, so an unrecognised build (locally built,
            # or older than the probe scans) doesn't re-download the zips on every launch.
            self._write_config_key_async(PLUGIN_PROBED_DLL_SHA_KEY, dll_sha,
                                         "plugin version probe")
            if detected:
                version = detected
                self._write_config_key_async(PLUGIN_INSTALLED_VERSION_KEY, detected,
                                             "detected plugin version")
        # present: True/False/None - see format_installed_version. None means no PLUGINS_DIR
        # / SERVER_ROOT / ipc_dir is set, which is a different fix from "it isn't installed".
        out["plugin"] = (version, bool(dll_sha) if plugin_dir else None)

        apworld = resolve_apworld_path(cfg.get)
        version = str(cfg.get(APWORLD_INSTALLED_VERSION_KEY, "") or "").strip()
        detected = apworld_version_from_disk(apworld, releases)
        if detected and detected != version:
            version = detected
            self._write_config_key_async(APWORLD_INSTALLED_VERSION_KEY, detected,
                                         "detected .apworld version")
        # None when the Archipelago directory isn't set: _archipelago_dir deliberately
        # refuses to assume C:\ProgramData\Archipelago, so neither does this.
        out["apworld"] = (version, os.path.isfile(apworld) if apworld else None)
        return out

    def _collect_update_statuses(self):
        """({component: status}, [error strings]) for UPDATE_COMPONENTS. Worker thread only.

        A component is omitted only when GitHub couldn't be reached for it - i.e. when there
        is no "latest" to compare against at all. Not knowing the INSTALLED version is no
        longer a reason to drop it: the status carries an empty "installed" plus "present"
        (is the file actually on disk), the dialog spells that out, and the two cues skip it
        so an unmeasurable install still never nags. Each status carries
        label / installed / present / latest / release / url / how. Errors are collected
        rather than raised so one dead endpoint can't hide the others; only the manual
        "Check for Updates" click surfaces them."""
        cfg = self._read_config_dict()
        out, errors = {}, []

        # Launcher: the SAME _fetch_release_list + _pick_best_release the self-update flow
        # uses, so the cues and the actual download can never disagree about "latest".
        try:
            best, version_str = _pick_best_release(self._fetch_release_list(), APP_VERSION)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
        else:
            out["launcher"] = {
                "label": UPDATE_COMPONENT_LABELS["launcher"],
                "installed": APP_VERSION,
                "present": True,
                # None = nothing newer carrying an installable zip. Report the installed
                # version so this reads as "no update", not "unknown".
                "latest": version_str or APP_VERSION,
                "release": best,
                "url": (best or {}).get("html_url") or UPDATE_RELEASES_PAGE,
                "how": ("Use \"Check for Updates\" at the top of the window to update "
                        "in place."),
            }

        # Plugin and .apworld both ship from the ONE ArkAP releases list (pre-release aware,
        # see _fetch_arkap_release_list): fetched once, then scanned per asset with the same
        # _release_for_asset the installers use - "newest release carrying this asset" is
        # exactly what they'd download, so it's what we have to compare against.
        wanted = [
            ("plugin", ARKAP_PLUGIN_ASSET_NAME,
             "Install Server/Api/Plugin tab -> \"Install Plugin\" downloads and upgrades it "
             "in place - your ArkAP.config.json is kept."),
            ("apworld", APWORLD_ASSET_NAME,
             "Archipelago Setup tab -> \"Update .apworld\" downloads it into custom_worlds "
             "(your existing copy is backed up first)."),
        ]
        try:
            releases = _fetch_arkap_release_list()
        except (OSError, ValueError) as exc:
            releases = []
            errors.append(str(exc))
        # Needs the release list: both detections identify the installed file by matching it
        # against the published assets (see apworld_version_from_disk).
        detected = self._detect_component_versions(cfg, releases) if releases else {}
        for comp, asset_name, how in wanted:
            rel, _asset = _release_for_asset(releases, asset_name)
            if rel is None:
                continue
            installed, present = detected.get(comp, ("", False))
            out[comp] = {
                "label": UPDATE_COMPONENT_LABELS[comp],
                "installed": installed,
                "present": present,
                "latest": (rel.get("tag_name") or "").strip() or installed,
                "release": rel,
                "url": rel.get("html_url") or RELEASES_URL,
                "how": how,
            }

        # The ARK tracker pack ships from its OWN repo, with no release assets at all (the
        # download is GitHub's source zip), so it gets its own fetch rather than joining the
        # asset scan above. Its installed version needs no hash matching either: the pack's
        # manifest.json carries a package_version that tracks the release tag, read straight
        # off disk by installed_tracker_pack.
        try:
            rel = _fetch_newest_release(TRACKER_PACK_RELEASES_API)
        except (OSError, ValueError) as exc:
            rel = None
            errors.append(str(exc))
        if rel:
            packs = poptracker_packs_dir(cfg.get(POPTRACKER_DIR_KEY))
            pack_path, detected_version = installed_tracker_pack(packs) if packs else ("", "")
            recorded = str(cfg.get(TRACKER_PACK_INSTALLED_VERSION_KEY, "") or "").strip()
            if detected_version and detected_version != recorded:
                self._write_config_key_async(TRACKER_PACK_INSTALLED_VERSION_KEY,
                                             detected_version,
                                             "detected %s version" % TRACKER_PACK_LABEL)
            out["trackerpack"] = {
                "label": UPDATE_COMPONENT_LABELS["trackerpack"],
                "installed": detected_version or recorded,
                # None when no PopTracker directory is set - a different fix from "the
                # pack isn't installed", exactly as for the .apworld.
                "present": bool(pack_path) if packs else None,
                "latest": (rel.get("tag_name") or "").strip()
                          or detected_version or recorded,
                "release": rel,
                "url": rel.get("html_url") or TRACKER_PACK_RELEASES_PAGE,
                "how": ("Archipelago Setup tab -> \"Install/update %s\" downloads it into "
                        "PopTracker's %s folder (your existing copy is moved aside first)."
                        % (TRACKER_PACK_LABEL, POPTRACKER_PACKS_DIRNAME)),
            }
        return out, errors

    def _component_version_check_worker(self):
        """Build the cues + advisory rows off the main thread. Only compares components
        whose installed version could be established - detected from the files on disk, or
        recorded by our own installer. With no baseline there's nothing to compare against,
        so it stays silent rather than guessing (see _collect_update_statuses); the update
        dialog still lists the component either way."""
        statuses, _errors = self._collect_update_statuses()
        advisories = []

        # ArkApi is advisory-only: nothing in the "Check for Updates" dialog installs it,
        # so it gets a Setup Status row but no badge. Still the same silent, best-effort
        # tag fetch as before.
        arkapi_installed = self._read_config_dict().get(ARKAPI_INSTALLED_VERSION_KEY, "")
        if arkapi_installed:
            latest = fetch_latest_release_tag(ARKSERVERAPI_RELEASES_API)
            if latest and _version_is_newer(latest, arkapi_installed):
                advisories.append({
                    "label": "ArkServerApi update available (advisory)",
                    "state": "info",
                    "detail": "Installed %s, latest release is %s. Being on an older "
                              "version isn't broken - just worth knowing. Install "
                              "Server/Api/Plugin tab -> \"Install ArkServerApi\" upgrades "
                              "it in place." % (arkapi_installed, latest),
                    "hint": "",
                    "link": ARKSERVERAPI_RELEASES_PAGE,
                })

        for comp in UPDATE_COMPONENTS:
            st = statuses.get(comp)
            if not st:
                continue
            if st["installed"] and _version_is_newer(st["latest"], st["installed"]):
                advisories.append({
                    "label": "%s update available (advisory)" % st["label"],
                    "state": "info",
                    "detail": "Installed %s, latest release is %s. Being on an older "
                              "version isn't broken - just worth knowing. %s"
                              % (st["installed"], st["latest"], st["how"]),
                    "hint": "",
                    "link": st["url"],
                })
            elif comp == "launcher":
                # Only the launcher gets a green "you're current" row - it's the one whose
                # absence from this list would otherwise read as "the check didn't run".
                advisories.append({
                    "label": "Launcher up to date",
                    "state": "ok",
                    "detail": "Running %s, the latest release." % APP_VERSION,
                    "hint": "",
                })

        # The window can be gone by the time a slow fetch returns (the check outlives a
        # quick close, and now runs after every plugin/.apworld install too) - handing work
        # to a dead Tk interpreter raises on this thread and helps nobody.
        try:
            self.after(0, self._on_component_versions, statuses, advisories)
        except (tk.TclError, RuntimeError):
            pass

    def _on_component_versions(self, statuses, advisories):
        self._update_status = statuses
        self._component_advisories = advisories
        # Silent check never acknowledges - it only reflects the current state, so anything
        # newer lights up both cues until the user actually clicks the button.
        self._apply_update_indicators()
        # Re-render the Setup Status rows and the tab-bar symbol with the new advisories,
        # whether or not that tab is currently open.
        self._refresh_setup_status()

    def _read_acknowledged_version(self, component):
        """Newest release of `component` the user has clicked 'Check for Updates' through
        to see, from the persistent config file (empty string if never / unreadable).
        Per-component key - see _ack_key."""
        val = self._read_config_dict().get(_ack_key(component), "")
        return val if isinstance(val, str) else ""

    def _acknowledge_version(self, component, version_str):
        """Persist `version_str` as the newest release of `component` the user has now seen.
        Written to the config JSON (not profiles) so it survives restarts - see
        ACK_VERSION_KEY / _ack_key."""
        self._write_config_key(_ack_key(component), version_str,
                               "acknowledged %s version" % component)

    def _set_update_highlight(self, on):
        """Light up (or clear) the button's Save-style halo. Off = recoloured to the header
        bg so it blends in at the same size, never shifting the header layout."""
        self._update_highlight_on = on
        t = self.theme
        colour = t["warn_bg"] if on else t["bg"]
        border = t["warn_border"] if on else t["bg"]
        self.update_btn_halo.configure(background=colour, highlightbackground=border)

    def _apply_update_indicators(self):
        """Set both cues from the collected per-component statuses (self._update_status):
        the persistent "!" badge (ANY tracked component has something newer than what's
        installed) and the dismissible button highlight (ANY component is newer than what
        the user acknowledged FOR THAT COMPONENT).

        Per-component acknowledgement is the whole point of the second comparison: with one
        shared value, clicking through a plugin update would silently dismiss the highlight
        for an .apworld update the user never saw. See _compute_update_cues / _ack_key."""
        show_badge, show_highlight = _aggregate_update_cues(
            self._update_status, self._read_acknowledged_version)
        self.update_badge_label.configure(text="!" if show_badge else "")
        self._set_update_highlight(show_highlight)

    def _on_check_for_updates(self):
        if self._update_check_thread and self._update_check_thread.is_alive():
            return
        if self._update_download_thread and self._update_download_thread.is_alive():
            messagebox.showinfo("ARKIpelago Launcher", "An update is already downloading.")
            return
        # NB: the "!" badge is deliberately NOT cleared here - clicking only dismisses the
        # highlight (done in _on_update_check_done, once the acknowledged version is known).
        self.update_check_btn.configure(state="disabled", text="Checking...")
        self._update_check_thread = threading.Thread(
            target=self._update_check_worker, daemon=True)
        self._update_check_thread.start()

    def _update_check_worker(self):
        statuses, errors = self._collect_update_statuses()
        try:
            self.after(0, self._on_update_check_done, statuses, errors)
        except (tk.TclError, RuntimeError):
            pass  # window closed mid-check - see _component_version_check_worker

    def _on_update_check_done(self, statuses, errors):
        self._update_check_thread = None
        self.update_check_btn.configure(state="normal", text="Check for Updates")
        # Nothing came back at all AND something failed: this is the offline case. The
        # startup check swallows it silently, but a deliberate click deserves the error
        # rather than a cheerful "you're up to date" that isn't known to be true.
        if not statuses and errors:
            messagebox.showerror("ARKIpelago Launcher",
                                  "Could not check for updates:\n\n%s" % errors[0])
            return
        self._update_status = statuses
        # The user clicked through to see these releases: acknowledge each component's
        # latest (persisted, per component), then recompute both cues. The highlight clears
        # (nothing is newer than what's acknowledged now); the "!" badge stays lit for
        # whatever is still newer than what's actually installed.
        for comp, st in statuses.items():
            self._acknowledge_version(comp, st["latest"])
        self._apply_update_indicators()
        if not any(st["installed"] and _version_is_newer(st["latest"], st["installed"])
                   for st in statuses.values()):
            # Every component gets a line, including the ones whose version couldn't be
            # worked out - listing only what was measurable is what made this box say
            # "Launcher: 0.4.7" and nothing else on an install the launcher didn't build.
            messagebox.showinfo("ARKIpelago Launcher",
                                "You're up to date.\n\n%s" % format_update_rows(statuses))
            return
        self._show_update_available_dialog(statuses)

    def _find_checksum_asset(self, data, asset_name):
        """Best-effort: some releases attach a checksum file alongside the zip. Recognises
        a handful of common naming conventions; returns None (silently) if none match -
        the size check already performed is still a real completeness guarantee on its own."""
        assets = data.get("assets") or []
        exact = {(asset_name + ext).lower() for ext in (".sha256", ".sha256.txt", ".sha256sum")}
        generic = {"sha256sums", "sha256sums.txt", "checksums.txt", "checksums.sha256"}
        for a in assets:
            name = (a.get("name") or "").lower()
            if name in exact or name in generic:
                return a
        return None

    @staticmethod
    def _extract_sha256_for_file(text, asset_name):
        """Parses either a bare 64-char hex hash, or 'HASH  filename' lines (the format
        `sha256sum` / GitHub Actions checksum steps commonly produce)."""
        hexre = re.compile(r"\b[0-9a-fA-F]{64}\b")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = hexre.search(line)
            if not m:
                continue
            rest = line[m.end():].strip().lstrip("*").strip()
            if not rest or asset_name.lower() in rest.lower():
                return m.group(0).lower()
        return None

    @staticmethod
    def _sha256_of_file(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1048576)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _dialog_component_action(self, win, tab, action):
        """Close the update dialog, switch to the tab that owns this component's installer,
        then run it. Jumping to the tab first is what makes the click legible: those flows
        put their progress bar and log on their own tab, and firing them from a dialog that
        then vanishes would look like nothing happened."""
        win.destroy()
        try:
            self.notebook.select(tab)
        except tk.TclError:
            pass
        action()

    def _show_update_available_dialog(self, statuses):
        """One row per tracked component: installed vs latest, and its own action button.
        Every component in UPDATE_COMPONENTS gets a row even when its version couldn't be
        determined - "not detected" / "not installed" said out loud beats a component
        silently missing from the list, since a missing row is indistinguishable from "this
        launcher has no such feature" and leaves the user nothing to act on."""
        win = tk.Toplevel(self)
        win.title("Update available")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Updates are available.",
                  font=(self._header_font_family or "Segoe UI", 11, "bold")
                  ).pack(anchor="w", pady=(0, 8))

        launcher = statuses.get("launcher")
        actions = {
            "plugin": lambda: self._dialog_component_action(
                win, self.tab_install, self.on_install_plugin),
            "apworld": lambda: self._dialog_component_action(
                win, self.tab_archipelago, self._on_update_apworld),
            "trackerpack": lambda: self._dialog_component_action(
                win, self.tab_archipelago, self._on_install_tracker_pack),
        }
        btn_text = {"launcher": "Update Now", "plugin": "Install Plugin",
                    "apworld": "Update .apworld",
                    "trackerpack": "Update tracker pack"}
        labels = UPDATE_COMPONENT_LABELS

        grid = ttk.Frame(frame)
        grid.pack(fill="x", pady=(0, 8))
        grid.columnconfigure(1, weight=1)
        for row, comp in enumerate(UPDATE_COMPONENTS):
            st = statuses.get(comp)
            ttk.Label(grid, text=labels[comp]).grid(row=row, column=0, sticky="w",
                                                    padx=(0, 12), pady=2)
            ttk.Label(grid, text="Installed: %s      Latest: %s"
                      % (format_installed_version(st),
                         (st or {}).get("latest") or "unknown")).grid(row=row, column=1,
                                                                      sticky="w", pady=2)
            if not st:
                continue
            # An unknown installed version falls THROUGH to the action button rather than
            # claiming "Up to date": we can't say it is, and the button is the only thing
            # that makes the row worth showing.
            if st["installed"] and not _version_is_newer(st["latest"], st["installed"]):
                ttk.Label(grid, text="Up to date", foreground=self.theme["subtle_fg"]
                          ).grid(row=row, column=2, sticky="e", pady=2)
                continue
            if comp == "launcher":
                # Only the launcher can update itself from here, and only if the release
                # actually carries a folder-zip - otherwise it's a manual download.
                asset = _launcher_zip_asset(st["release"] or {})
                if not asset:
                    ttk.Label(grid, text="Download manually", foreground=self.theme["subtle_fg"]
                              ).grid(row=row, column=2, sticky="e", pady=2)
                    continue
                cmd = (lambda s=st, a=asset: self._confirm_and_start_update(
                    win, s["release"], a, s["latest"]))
            else:
                cmd = actions[comp]
            ttk.Button(grid, text=btn_text[comp], command=cmd).grid(row=row, column=2,
                                                                    sticky="e", pady=2)

        # Release notes are the launcher's only - the plugin/.apworld share the ArkAP
        # release whose notes cover both, and three notes boxes is a wall of text.
        body = ((launcher or {}).get("release") or {}).get("body") or ""
        body = body.strip()
        if body:
            ttk.Label(frame, text="Launcher release notes:").pack(anchor="w")
            shown = body if len(body) <= 4000 else body[:4000] + "\n..."
            notes = tk.Text(frame, width=64, height=10, wrap="word",
                             background=self.theme["text_bg"], foreground=self.theme["text_fg"])
            notes.insert("1.0", shown)
            notes.configure(state="disabled")
            notes.pack(fill="both", expand=True, pady=(2, 8))

        for comp in UPDATE_COMPONENTS:
            st = statuses.get(comp)
            if not st or (st["installed"]
                          and not _version_is_newer(st["latest"], st["installed"])):
                continue
            url = st["url"]
            link = ttk.Label(frame, text="%s: %s" % (labels[comp], url),
                              foreground=self.theme["status_info"], cursor="hand2")
            link.pack(anchor="w")
            link.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u))

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x", pady=(10, 0))
        ttk.Button(btn_row, text="Not Now", command=win.destroy).pack(side="right")

    def _confirm_and_start_update(self, dialog, data, asset, tag):
        if not getattr(sys, "frozen", False):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "Auto-update only works for the built .exe, not when running from source "
                "(python arkap_launcher.py). Download the release manually from the link "
                "above.", parent=dialog)
            return
        if not messagebox.askyesno(
                "Update ARKIpelago Launcher",
                "Download and install %s now?\n\n"
                "The launcher will close automatically once the download finishes, apply "
                "the update, and reopen on its own.\n\n"
                "Your saved configuration (%s) and profiles (%s) live in this same folder "
                "and are never touched by this - only the launcher program files are "
                "replaced. Make sure any unsaved changes in the Configuration tab are saved "
                "first."
                % (tag, CONFIG_FILENAME, PROFILES_FILENAME),
                parent=dialog):
            return
        dialog.destroy()
        self._start_update_download(data, asset, tag)

    def _start_update_download(self, data, asset, tag):
        win = tk.Toplevel(self)
        win.title("Updating...")
        win.resizable(False, False)
        win.transient(self)
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        win.grab_set()
        self._update_progress_win = win

        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        self._update_status_var = tk.StringVar(value="Starting download...")
        ttk.Label(frame, textvariable=self._update_status_var, width=52
                  ).pack(anchor="w")
        self._update_progress_bar = ttk.Progressbar(frame, mode="determinate",
                                                      maximum=100, length=360)
        self._update_progress_bar.pack(pady=(8, 0))

        self._update_download_queue = queue.Queue()
        self._update_download_thread = threading.Thread(
            target=self._update_download_worker, args=(data, asset, tag), daemon=True)
        self._update_download_thread.start()
        self.after(100, self._poll_update_download_queue)

    def _cleanup_failed_download(self, path):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _rmtree_quiet(path):
        shutil.rmtree(path, ignore_errors=True)

    def _download_update_asset(self, url, dest_path, expected_size, q):
        req = urllib.request.Request(url, headers={"User-Agent": GITHUB_API_USER_AGENT})
        downloaded = 0
        last_emit = 0.0
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or expected_size or 0)
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if not total:
                        continue
                    now = time.monotonic()
                    if now - last_emit < 0.15:
                        continue
                    last_emit = now
                    pct = min(100, int(downloaded * 100 / total))
                    q.put(("progress", pct))
                    q.put(("status", "Downloading... %d%% (%.1f / %.1f MB)"
                           % (pct, downloaded / 1048576, total / 1048576)))
        if total:
            q.put(("progress", 100))
        return downloaded

    def _update_download_worker(self, data, asset, tag):
        q = self._update_download_queue
        exe_dir = base_dir()
        zip_path = os.path.join(exe_dir, UPDATE_ZIP_TMPNAME)
        staging_root = os.path.join(exe_dir, UPDATE_STAGING_DIRNAME)
        url = asset.get("browser_download_url")
        expected_size = asset.get("size", 0)
        self._cleanup_failed_download(zip_path)
        self._rmtree_quiet(staging_root)

        try:
            q.put(("status", "Downloading %s..." % asset.get("name", "update")))
            downloaded = self._download_update_asset(url, zip_path, expected_size, q)
        except (OSError, ValueError) as exc:
            self._cleanup_failed_download(zip_path)
            q.put(("error", "Download failed: %s" % exc))
            return

        if expected_size and downloaded != expected_size:
            self._cleanup_failed_download(zip_path)
            q.put(("error",
                   "Downloaded file size (%d bytes) did not match the size GitHub "
                   "reported (%d bytes) - the download was incomplete. Nothing was "
                   "changed." % (downloaded, expected_size)))
            return

        checksum_asset = self._find_checksum_asset(data, asset.get("name", ""))
        if checksum_asset:
            q.put(("status", "Verifying checksum..."))
            expected_hash = None
            try:
                req = urllib.request.Request(
                    checksum_asset["browser_download_url"],
                    headers={"User-Agent": GITHUB_API_USER_AGENT})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    checksum_text = resp.read().decode("utf-8", errors="replace")
                expected_hash = self._extract_sha256_for_file(checksum_text,
                                                                asset.get("name", ""))
            except OSError:
                pass  # checksum fetch failing isn't fatal - the size check above already ran
            if expected_hash:
                actual_hash = self._sha256_of_file(zip_path)
                if actual_hash != expected_hash:
                    self._cleanup_failed_download(zip_path)
                    q.put(("error",
                           "Checksum verification failed for the downloaded update "
                           "(expected %s, got %s). Nothing was changed."
                           % (expected_hash, actual_hash)))
                    return
                q.put(("status", "Checksum verified."))

        # Extract in-process, BEFORE relaunch. This validates the package and, just as
        # importantly, gives the AV on-access scanner a head start on the freshly-written
        # program files while the old app is still shutting down - so the helper's "DLL
        # openable" gate resolves fast instead of having to wait out a full first-time scan.
        q.put(("status", "Extracting update..."))
        try:
            self._rmtree_quiet(staging_root)
            os.makedirs(staging_root, exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(staging_root)
        except (OSError, zipfile.BadZipFile) as exc:
            self._cleanup_failed_download(zip_path)
            self._rmtree_quiet(staging_root)
            q.put(("error", "Could not extract the downloaded update: %s\n\n"
                            "Nothing was changed." % exc))
            return

        staged = _locate_staged_app(staging_root)
        if not staged:
            self._cleanup_failed_download(zip_path)
            self._rmtree_quiet(staging_root)
            q.put(("error",
                   "The downloaded update did not contain the expected launcher program "
                   "files (an .exe alongside %s\\%s). Nothing was changed."
                   % (UPDATE_INTERNAL_DIRNAME, UPDATE_PAYLOAD_DLL_GLOB)))
            return

        q.put(("status", "Update verified. Preparing to restart..."))
        q.put(("ready", (staged, tag)))

    def _poll_update_download_queue(self):
        try:
            while True:
                kind, payload = self._update_download_queue.get_nowait()
                if kind == "progress":
                    try:
                        self._update_progress_bar["value"] = payload
                    except tk.TclError:
                        pass
                elif kind == "status":
                    self._update_status_var.set(payload)
                elif kind == "error":
                    self._on_update_download_error(payload)
                    return
                elif kind == "ready":
                    staged, tag = payload
                    self._on_update_download_ready(staged, tag)
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll_update_download_queue)

    def _on_update_download_error(self, message):
        win, self._update_progress_win = self._update_progress_win, None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
        self._update_download_thread = None
        messagebox.showerror("ARKIpelago Launcher",
                              "Update failed - the current launcher was left untouched.\n\n%s"
                              % message)

    def _on_update_download_ready(self, staged, tag):
        win, self._update_progress_win = self._update_progress_win, None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
        staged_dir, staged_exe = staged
        try:
            self._launch_update_helper_and_exit(staged_dir, staged_exe, tag)
        except OSError as exc:
            self._rmtree_quiet(os.path.join(base_dir(), UPDATE_STAGING_DIRNAME))
            self._cleanup_failed_download(os.path.join(base_dir(), UPDATE_ZIP_TMPNAME))
            messagebox.showerror(
                "ARKIpelago Launcher",
                "Downloaded the update but could not start the updater helper - the "
                "current launcher was left untouched.\n\n%s" % exc)

    def _launch_update_helper_and_exit(self, staged_dir, staged_exe, tag):
        exe_dir = base_dir()
        current_exe = os.path.normpath(sys.executable)
        ps_path = os.path.join(exe_dir, UPDATE_HELPER_SCRIPT)
        result_path = os.path.join(exe_dir, UPDATE_RESULT_FILENAME)
        zip_path = os.path.join(exe_dir, UPDATE_ZIP_TMPNAME)
        staging_root = os.path.join(exe_dir, UPDATE_STAGING_DIRNAME)

        script = _build_update_ps_script(
            pid=os.getpid(), app_dir=exe_dir, current_exe=current_exe,
            staged_dir=staged_dir, staged_exe=staged_exe,
            internal_dir=UPDATE_INTERNAL_DIRNAME, dll_glob=UPDATE_PAYLOAD_DLL_GLOB,
            zip_path=zip_path, staging_root=staging_root,
            result_path=result_path, tag=tag)
        # utf-8-sig so PowerShell 5.1 reads any non-ASCII path in the baked-in literals
        # correctly (a BOM-less .ps1 is read as the system ANSI codepage there).
        with open(ps_path, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write(script)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", ps_path],
            cwd=exe_dir, creationflags=creationflags, close_fds=True)
        self.destroy()

    def _sweep_broken_mod_files(self):
        """Startup housekeeping: delete any corrupt <id>.mod under Content\\Mods. Never
        fatal. Unlike a missing mod, one of these takes the whole server down on its next
        start, so it is removed rather than merely reported - after which the mod reads as
        plainly "not installed" and the Mods tab's Download button fixes it. Logged loudly
        (Configuration log + Mods log) so the deletion is never silent."""
        results = remove_broken_mod_files(self.get("SERVER_ROOT"))
        if not results:
            return
        for path, reason, removed in results:
            line = ("Removed corrupt mod file %s (%s)." if removed else
                    "! Corrupt mod file %s could not be removed (%s).") % (path, reason)
            self._log(line)
            self._mods_log_line(line)
        gone = [os.path.basename(p) for p, _r, ok in results if ok]
        stuck = [os.path.basename(p) for p, _r, ok in results if not ok]
        msg = ""
        if gone:
            msg += ("These mod files were corrupt and would have crashed the ARK server "
                    "on startup, so they were deleted:\n\n%s\n\nRe-download those mods "
                    "from the Mods tab before starting the server.\n\n" % "\n".join(gone))
        if stuck:
            msg += ("These mod files are corrupt but could not be deleted (is the server "
                    "running?). Delete them by hand before starting the server:\n\n%s"
                    % "\n".join(stuck))
        messagebox.showwarning("ARKIpelago Launcher - corrupt mod files", msg.strip())

    def _check_previous_update_result(self):
        """Local file read only, no network - reports the outcome of an update-helper run
        that happened just before this process started (see _build_update_ps_script)."""
        path = os.path.join(base_dir(), UPDATE_RESULT_FILENAME)
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = [l.rstrip("\n").rstrip("\r") for l in f.readlines()]
        except OSError:
            return
        try:
            os.remove(path)
        except OSError:
            pass
        status = lines[0].strip() if lines else ""
        tag = lines[1].strip() if len(lines) > 1 else ""
        detail = lines[2].strip() if len(lines) > 2 else ""
        if status == "OK":
            messagebox.showinfo("ARKIpelago Launcher",
                                 "Update to %s complete." % (tag or "the latest version"))
        elif status == "FAIL":
            messagebox.showerror(
                "ARKIpelago Launcher",
                "The update to %s did not complete successfully; your previous launcher "
                "was left in place (or restored) rather than leaving you without one."
                "\n\n%s" % (tag or "the latest version",
                             detail or "See %s next to the exe, if it's still there, for "
                                       "details." % UPDATE_HELPER_SCRIPT))

    def _sweep_update_leftovers(self):
        """Best-effort startup housekeeping (never fatal). Removes update-helper working
        files that survive only if the helper was killed mid-run: the staging folder, the
        downloaded zip, the helper script, and the exe/_internal .old backups - all ours,
        all next to the exe. (Stale --onefile _MEI* temp folders from older versions are
        deliberately left alone: they share a name pattern with every other PyInstaller app
        on the machine, so blind deletion could corrupt another running app. --onedir no
        longer creates them, so they stop accumulating on their own.)"""
        b = base_dir()
        self._rmtree_quiet(os.path.join(b, UPDATE_STAGING_DIRNAME))
        self._rmtree_quiet(os.path.join(b, UPDATE_INTERNAL_DIRNAME + ".old"))
        for name in (UPDATE_ZIP_TMPNAME, UPDATE_HELPER_SCRIPT,
                     os.path.basename(sys.executable) + ".old"):
            self._cleanup_failed_download(os.path.join(b, name))

    # -------------------------------------------- ArkAP plugin install ----- #
    def _fetch_latest_release_asset(self, asset_name, q):
        """Query GitHub for the newest ARKipelago release carrying `asset_name`.

        Returns (tag, asset_name, download_url, size_bytes). Raises RuntimeError/OSError/
        ValueError on failure - the caller turns that into a log line + failure result.
        Pre-release-aware via _fetch_arkap_release_list; asset lookup (including the
        scan DOWN the list for the newest release that actually ships this file) is
        _release_for_asset, shared with the update check."""
        releases = [r for r in _fetch_arkap_release_list()
                    if isinstance(r, dict) and not r.get("draft")]
        if not releases:
            raise RuntimeError("No releases found (checked %s)." % ARKAP_PLUGIN_RELEASES_API)
        data, asset = _release_for_asset(releases, asset_name)
        if asset is None:
            raise RuntimeError(
                "No release has a %s asset (newest checked: %s). Download it by hand from %s."
                % (asset_name, releases[0].get("tag_name") or "unknown", RELEASES_URL))
        tag = data.get("tag_name") or "unknown"
        q.put(("line", "Latest release: %s - asset %s" % (tag, asset["name"])))
        return tag, asset["name"], asset["browser_download_url"], asset.get("size", 0)

    def _fetch_latest_plugin_release(self, q):
        """The plugin installer's asset lookup - see _fetch_latest_release_asset."""
        return self._fetch_latest_release_asset(ARKAP_PLUGIN_ASSET_NAME, q)

    def _locate_extracted_plugin(self, root):
        """Find the folder inside `root` that holds ArkAP\\ArkAP.dll (the plugin payload),
        whether the zip put ArkAP\\ at its root or nested one level down. Returns that folder
        (the parent of ArkAP\\), or None."""
        cands = [root]
        try:
            cands.extend(os.path.join(root, n) for n in sorted(os.listdir(root))
                         if os.path.isdir(os.path.join(root, n)))
        except OSError:
            return None
        for d in cands:
            if os.path.isfile(os.path.join(d, PLUGIN_PAYLOAD_MARKER)):
                return d
        return None

    def _plugin_worker(self, plugins, dst_arkap):
        q = self._plugin_queue
        try:
            q.put(("line", "Fetching latest ArkAP plugin release info..."))
            tag, asset_name, url, _size = self._fetch_latest_plugin_release(q)
        except (OSError, ValueError, RuntimeError) as exc:
            q.put(("line", "! Could not fetch release info: %s" % exc))
            q.put(("done", None))
            return

        tmp_dir = tempfile.mkdtemp(prefix="arkap_plugin_dl_")
        zip_path = os.path.join(tmp_dir, asset_name)
        extract_dir = os.path.join(tmp_dir, "unzipped")
        try:
            q.put(("line", "Downloading %s..." % asset_name))
            self._download_with_progress(url, zip_path, q)
            self._extract_zip_to(zip_path, extract_dir, q)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            q.put(("line", "! Download/extract failed: %s" % exc))
            shutil.rmtree(tmp_dir, ignore_errors=True)
            q.put(("done", None))
            return

        src_root = self._locate_extracted_plugin(extract_dir)
        if not src_root:
            q.put(("line", "! %s didn't contain ArkAP\\ArkAP.dll - nothing installed."
                   % asset_name))
            shutil.rmtree(tmp_dir, ignore_errors=True)
            q.put(("done", None))
            return

        src_arkap = os.path.join(src_root, "ArkAP")
        copied, skipped, errors = self._copy_plugin_tree(src_arkap, dst_arkap)
        shutil.rmtree(tmp_dir, ignore_errors=True)

        q.put(("line", "Installed into: %s" % dst_arkap))
        for s in skipped:
            q.put(("line", "  kept: %s" % s))
        q.put(("line", "  Copied %d file(s)." % len(copied)))
        for e in errors:
            q.put(("line", "  ! %s" % e))
        if copied:
            # Record the tag we just installed so the Setup Status advisory can flag a newer
            # release later (same key the ArkApi installer stamps its version into).
            q.put(("version", tag))
        q.put(("done", {"plugins": plugins, "dst_arkap": dst_arkap,
                        "copied": len(copied), "errors": len(errors)}))

    def _poll_plugin_queue(self):
        try:
            while True:
                kind, payload = self._plugin_queue.get_nowait()
                if kind == "line":
                    self._install_log(payload)
                elif kind == "version":
                    self._write_config_key(PLUGIN_INSTALLED_VERSION_KEY, payload,
                                           "installed plugin version")
                elif kind == "progress":
                    try:
                        self.install_progress["value"] = payload
                    except tk.TclError:
                        pass
                elif kind == "done":
                    self._on_plugin_done(payload)
                    self._start_component_version_check()
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll_plugin_queue)

    def _on_plugin_done(self, payload):
        self.install_btn.configure(state="normal")
        self.arkapi_install_btn.configure(state="normal")
        self.install_plugin_btn.configure(state="normal")
        self._plugin_thread = None
        self.install_progress.stop()
        if not payload or not payload["copied"]:
            self.install_status_var.set("Failed")
            messagebox.showerror("Install Plugin",
                                 "Plugin install failed. See the log for details.")
            return
        # Point the shared path vars at the confirmed install so reset / Open Plugins /
        # ipc_dir all follow it (no second parallel path variable).
        self.set("PLUGINS_DIR", payload["plugins"])
        self.set("ipc_dir", os.path.join(payload["dst_arkap"], "ipc"))
        self.install_status_var.set("Done")
        if payload["errors"]:
            messagebox.showwarning(
                "Install Plugin",
                "Plugin install finished with %d problem(s) - see the log.\n\nCopied %d "
                "file(s) to:\n%s"
                % (payload["errors"], payload["copied"], payload["dst_arkap"]))
        else:
            messagebox.showinfo(
                "Install Plugin",
                "ArkAP plugin installed (%d file(s)) to:\n\n%s\n\nRestart (or start) the "
                "ARK dedicated server, then run the connector."
                % (payload["copied"], payload["dst_arkap"]))

    def _copy_plugin_tree(self, src_arkap, dst_arkap):
        """Recursively copy src_arkap -> dst_arkap (merge, like the .bat's robocopy /E -
        never deletes ipc/tracking files already there). Preserves an existing
        ArkAP.config.json at the destination root. Returns (copied, skipped, errors)."""
        copied, skipped, errors = [], [], []
        keep_config = os.path.isfile(os.path.join(dst_arkap, PLUGIN_PRESERVE_ON_UPGRADE))
        for dirpath, _dirnames, filenames in os.walk(src_arkap):
            rel = os.path.relpath(dirpath, src_arkap)
            dst_dir = dst_arkap if rel == "." else os.path.join(dst_arkap, rel)
            try:
                os.makedirs(dst_dir, exist_ok=True)
            except OSError as exc:
                errors.append("%s: %s" % (dst_dir, exc))
                continue
            for fn in filenames:
                if (keep_config and rel == "."
                        and fn.lower() == PLUGIN_PRESERVE_ON_UPGRADE.lower()):
                    skipped.append("%s (kept your existing settings)" % fn)
                    continue
                s = os.path.join(dirpath, fn)
                d = os.path.join(dst_dir, fn)
                try:
                    shutil.copyfile(s, d)
                    copied.append(os.path.relpath(d, dst_arkap))
                except OSError as exc:
                    errors.append("%s: %s" % (d, exc))
        return copied, skipped, errors

    def on_install_plugin(self):
        """Download the latest ArkAP_Plugin.zip from GitHub and install it into
        <SERVER_ROOT>\\...\\ArkApi\\Plugins\\ArkAP (preserving an existing config on upgrade).
        Runs on a background thread; progress streams into the install console."""
        if self._any_install_running():
            messagebox.showinfo("ARKIpelago Launcher", "An install is already running.")
            return
        self.notebook.select(self.tab_install)

        root = self.get("SERVER_ROOT")
        if not root:
            messagebox.showwarning(
                "Install Plugin",
                "SERVER_ROOT is not set. Set it (or install the ARK server) first - the "
                "plugin installs under SERVER_ROOT\\ShooterGame\\Binaries\\Win64\\ArkApi.")
            return
        root = os.path.normpath(root)
        win64 = os.path.join(root, "ShooterGame", "Binaries", "Win64")
        arkapi = os.path.join(win64, "ArkApi")
        if not os.path.isdir(win64):
            messagebox.showwarning(
                "Install Plugin",
                "Win64 folder not found under SERVER_ROOT:\n\n%s\n\nInstall the ARK "
                "dedicated server first (Install Server/Api/Plugin -> Install ARK Server)." % win64)
            return
        if not os.path.isdir(arkapi):
            messagebox.showwarning(
                "Install Plugin",
                "ArkApi is not installed yet - no 'ArkApi' folder in:\n\n%s\n\nInstall "
                "ArkServerApi into Win64 first (\"Install ArkServerApi\" above), then try "
                "again." % win64)
            return

        plugins = os.path.join(arkapi, "Plugins")
        try:
            os.makedirs(plugins, exist_ok=True)  # ArkApi exists but Plugins may not yet
        except OSError as exc:
            messagebox.showerror("Install Plugin",
                                  "Could not create the Plugins folder:\n\n%s\n\n%s"
                                  % (plugins, exc))
            return
        dst_arkap = os.path.join(plugins, "ArkAP")

        upgrade = os.path.isfile(os.path.join(dst_arkap, PLUGIN_PRESERVE_ON_UPGRADE))
        if not messagebox.askyesno(
                "Install Plugin",
                "Download the latest ArkAP plugin from GitHub and install it into:\n\n%s\n\n"
                "%s\n\n(Any ipc / tracking files already there are left in place - use a "
                "reset button for a clean seed.)\n\nProceed?"
                % (dst_arkap,
                   "An existing ArkAP.config.json will be KEPT (your settings survive)."
                   if upgrade else "This is a fresh install.")):
            return

        self.install_log.configure(state="normal")
        self.install_log.delete("1.0", "end")
        self.install_log.configure(state="disabled")

        self._plugin_queue = queue.Queue()
        self.install_btn.configure(state="disabled")
        self.arkapi_install_btn.configure(state="disabled")
        self.install_plugin_btn.configure(state="disabled")
        self.install_status_var.set("Installing...")
        self.install_progress.stop()
        self.install_progress.configure(mode="determinate", maximum=100)
        self.install_progress["value"] = 0

        self._plugin_thread = threading.Thread(
            target=self._plugin_worker, args=(plugins, dst_arkap), daemon=True)
        self._plugin_thread.start()
        self.after(100, self._poll_plugin_queue)

    # -------------------------------------------------- quick-launch ------- #
    def _open_folder(self, path, label, hint=""):
        if not path:
            messagebox.showwarning("ARKIpelago Launcher", "%s is not set." % label)
            return
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            messagebox.showwarning("ARKIpelago Launcher",
                                   "%s does not exist:\n%s%s" % (label, path, hint))
            return
        try:
            os.startfile(path)  # noqa: Windows-only, which is the target platform.
        except OSError as exc:
            messagebox.showerror("ARKIpelago Launcher", "Could not open:\n%s\n\n%s"
                                 % (path, exc))

    def open_ipc(self):
        self._open_folder(self.get("ipc_dir"), "ipc_dir")

    def open_plugins(self):
        self._open_folder(self.get("PLUGINS_DIR"), "ArkApi Plugins folder")

    def open_gameini_folder(self):
        root = self.get("SERVER_ROOT")
        if not root:
            messagebox.showwarning("ARKIpelago Launcher", "SERVER_ROOT is not set.")
            return
        self._open_folder(
            os.path.join(root, "ShooterGame", "Saved", "Config", "WindowsServer"),
            "Game.ini config folder")

    def _ipc_dir(self):
        """The ArkAP ipc folder (where the plugin writes game_ini_fragment.txt), or ""."""
        ipc = self.get("ipc_dir")
        if ipc:
            return os.path.normpath(ipc)
        plugin_dir = self._arkap_plugin_dir()
        return os.path.join(plugin_dir, "ipc") if plugin_dir else ""

    def _game_ini_path(self):
        """The configured Game.ini path, else the one derived from SERVER_ROOT, or ""."""
        gi = self.get("game_ini")
        if gi:
            return os.path.normpath(gi)
        cfg = self._server_config_dir()
        return os.path.join(cfg, "Game.ini") if cfg else ""

    def _backup_file(self, path, ts):
        """Copy path to <path>.<ts>.bak (dupe-suffixed on a same-second collision) and
        return the backup path. Raises OSError on failure. Same timestamped-.bak pattern
        the config upload uses; shared by the Game.ini patch and the reset's Game.ini
        cleanup so both back up identically before writing."""
        backup = "%s.%s.bak" % (path, ts)
        dupe = 2
        while os.path.exists(backup):
            backup = "%s.%s-%d.bak" % (path, ts, dupe)
            dupe += 1
        shutil.copy2(path, backup)
        return backup

    def patch_game_ini_for_randomized_dinos(self):
        """Quick-launch: apply ipc\\game_ini_fragment.txt into Game.ini for a randomized
        run - the automated replacement for the old manual copy-paste. Backs up first."""
        # 1) Game.ini is read at server startup; editing it live risks being ignored or
        #    overwritten on shutdown, so refuse outright while the server runs.
        if is_process_running(ARK_SERVER_PROCESS):
            messagebox.showerror(
                "ARKIpelago Launcher",
                "%s is currently running.\n\nGame.ini is read when the server starts, so "
                "any edit made now would be ignored and may be overwritten when the "
                "server shuts down. Stop the ARK dedicated server first.\n\n(Patch "
                "Game.ini aborted.)" % ARK_SERVER_PROCESS)
            self._log("Patch Game.ini: aborted - %s is running." % ARK_SERVER_PROCESS)
            return

        # 2) The fragment the plugin generates.
        ipc = self._ipc_dir()
        if not ipc:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "Can't work out where the ipc folder is.\n\nSet ipc_dir (or SERVER_ROOT / "
                "the ArkApi Plugins folder) on the Configuration tab first.")
            return
        frag_path = os.path.join(ipc, GAME_INI_FRAGMENT_NAME)
        if not os.path.isfile(frag_path):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "No %s exists yet in:\n\n%s\n\nThe ArkAP plugin generates this file when "
                "randomize_dino_spawns is on in your yaml. Connect to the server once "
                "first so the plugin can generate it, then run this again."
                % (GAME_INI_FRAGMENT_NAME, ipc))
            self._log("Patch Game.ini: no %s at %s" % (GAME_INI_FRAGMENT_NAME, frag_path))
            return

        # 3) The Game.ini to patch.
        game_ini = self._game_ini_path()
        if not game_ini:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "Can't work out where Game.ini is.\n\nSet game_ini (or SERVER_ROOT) on "
                "the Configuration tab first.")
            return
        if not os.path.isfile(game_ini):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "Game.ini does not exist yet:\n\n%s\n\nStart the server once (or upload a "
                "Game.ini on the Configuration tab) so the file exists, then run this "
                "again." % game_ini)
            self._log("Patch Game.ini: Game.ini not found at %s" % game_ini)
            return

        # Read both, then compute the merged text before touching disk.
        try:
            frag_text, _ = read_text(frag_path)
        except OSError as exc:
            messagebox.showerror("ARKIpelago Launcher",
                                 "Could not read %s:\n\n%s" % (frag_path, exc))
            return
        try:
            existing, enc = read_text(game_ini)
        except OSError as exc:
            messagebox.showerror("ARKIpelago Launcher",
                                 "Could not read Game.ini:\n\n%s" % exc)
            return

        n = len(_fragment_payload_lines(frag_text))
        if not n:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "%s has no NPCReplacements lines to apply - nothing to patch.\n\n(This "
                "usually means the last seed didn't randomize creatures.)"
                % GAME_INI_FRAGMENT_NAME)
            self._log("Patch Game.ini: %s had no NPCReplacements lines." % GAME_INI_FRAGMENT_NAME)
            return

        # What's already in Game.ini decides how we ask. Detection is a literal scan for
        # ConfigOverrideNPCSpawnEntriesContainer= lines (see GAME_INI_FRAGMENT_LINE_RE):
        #   * only our own marker block          -> replace in place, no need to ask.
        #   * an UNMARKED wall (hand-pasted, or  -> stop and ask before touching it.
        #     ours from before this detection)
        #   * BOTH a marker block AND a separate -> the duplicate-wall bug; tell the user
        #     unmarked wall                         and offer to collapse it to one.
        #   * nothing                            -> a normal fresh insert.
        has_marked = GAME_INI_BLOCK_BEGIN in existing
        unmarked = game_ini_unmarked_fragment_count(existing)
        n_word = "y" if unmarked == 1 else "ies"
        remove_unmarked = False
        if has_marked and unmarked:
            if not messagebox.askyesno(
                    "Duplicate randomized-creatures entries found",
                    "Game.ini has TWO sets of randomized-creatures entries:\n\n"
                    "  - one inside this launcher's own markers, and\n"
                    "  - a SEPARATE, unmarked wall of %d %s= entr%s further down the "
                    "file (a leftover duplicate from an earlier patch run).\n\n"
                    "ARK will try to honour both, which is almost certainly not what you "
                    "want. Clean this up?\n\n"
                    "  Yes - remove the unmarked duplicate AND replace the marked block "
                    "with the current one, leaving exactly one set.\n"
                    "  No  - change nothing and cancel.\n\n"
                    "A timestamped backup is made first if you proceed."
                    % (unmarked, GAME_INI_FRAGMENT_KEY, n_word)):
                self._log("Patch Game.ini: cancelled (duplicate cleanup declined).")
                return
            remove_unmarked = True
        elif unmarked:
            if not messagebox.askyesno(
                    "Existing randomized-creatures block found",
                    "Game.ini already contains %d %s= entr%s that this launcher did NOT "
                    "add (not wrapped in the app's markers - most likely pasted in by "
                    "hand, possibly from a different seed).\n\n"
                    "Replace them with the current block from %s?\n\n"
                    "  Yes - remove those lines and insert the new block (wrapped in the "
                    "app's markers, so future patches and \"Full reset for new seed\" "
                    "manage it cleanly).\n"
                    "  No  - leave Game.ini exactly as it is and cancel.\n\n"
                    "A timestamped backup is made first if you proceed."
                    % (unmarked, GAME_INI_FRAGMENT_KEY, n_word, GAME_INI_FRAGMENT_NAME)):
                self._log("Patch Game.ini: cancelled - left the existing unmarked "
                          "fragment in place.")
                return
            remove_unmarked = True

        new_text, n = merge_game_ini_fragment(existing, frag_text,
                                              remove_unmarked=remove_unmarked)
        merged = GAME_INI_SECTION.lower() in existing.lower()
        where = ("merged into the existing %s section" % GAME_INI_SECTION if merged
                 else "added as a new %s section at the top" % GAME_INI_SECTION)
        if remove_unmarked:
            action = "Replacing the existing entries with"
        elif has_marked:
            action = "Replacing the current auto-managed block with"
        else:
            action = "Applying"
        # The unmarked / duplicate cases already got an explicit confirm; don't re-prompt.
        if not remove_unmarked and not messagebox.askyesno(
                "Patch Game.ini for randomized creatures",
                "%s %d %s= entr%s from:\n\n%s\n\ninto:\n\n%s\n\nThey will be %s. "
                "Everything else in Game.ini is left exactly as-is, and a timestamped "
                "backup is made first (nothing is deleted).\n\nProceed?"
                % (action, n, GAME_INI_FRAGMENT_KEY, "y" if n == 1 else "ies",
                   frag_path, game_ini, where)):
            self._log("Patch Game.ini: cancelled.")
            return

        self._clear_log()
        # 4) Back up FIRST - same pattern as the config-upload backup (timestamped .bak
        #    alongside the original). If the backup fails, Game.ini is left untouched.
        ts = time.strftime("%Y%m%d-%H%M%S")
        try:
            backup = self._backup_file(game_ini, ts)
            self._log("Backed up %s -> %s" % (game_ini, os.path.basename(backup)))
        except OSError as exc:
            messagebox.showerror(
                "ARKIpelago Launcher",
                "Could not back up Game.ini, so it was NOT modified:\n\n%s" % exc)
            self._log("! Patch Game.ini: backup failed (%s) - Game.ini not modified." % exc)
            return

        # 5) Write the patched version.
        try:
            write_text(game_ini, new_text, encoding=enc)
        except OSError as exc:
            messagebox.showerror(
                "ARKIpelago Launcher",
                "Backup succeeded but writing the patched Game.ini failed:\n\n%s\n\nYour "
                "original is safe at:\n%s" % (exc, backup))
            self._log("! Patch Game.ini: write failed (%s). Backup at %s" % (exc, backup))
            return

        cleaned_note = (" Removed the previous unmarked duplicate wall." if remove_unmarked
                        else "")
        self._log("Patched %s: %d %s= line(s) %s.%s"
                  % (game_ini, n, GAME_INI_FRAGMENT_KEY,
                     "merged into existing section" if merged
                     else "added in a new section at the top", cleaned_note))
        messagebox.showinfo(
            "ARKIpelago Launcher",
            "Patched Game.ini with %d %s= line(s) (%s).%s\n\nBackup of the previous "
            "version:\n%s\n\nRestart the ARK server for the randomized creatures to take "
            "effect." % (n, GAME_INI_FRAGMENT_KEY, where, cleaned_note, backup))

    def open_server_root(self):
        self._open_folder(self.get("SERVER_ROOT"), "SERVER_ROOT")

    def open_cluster_dir(self):
        # Opens exactly the configured CLUSTERDIR (<SERVER_ROOT>\ServerCluster\ClusterData
        # by default) - never its parent, so a missing ClusterData is reported instead of
        # silently opening ServerCluster as a stand-in.
        self._open_folder(self.get("CLUSTERDIR"), "CLUSTERDIR",
                          "\n\nUse \"Create ServerCluster folders\" to create it.")

    # ------------------------------------------------- new-seed reset ------ #
    def _arkap_plugin_dir(self):
        """Resolve the ArkAP plugin folder (<...>\\ArkApi\\Plugins\\ArkAP), or None, from
        the live Tk fields. The precedence itself lives in resolve_plugin_dir, shared with
        the version check (which reads the saved config on a worker thread)."""
        return resolve_plugin_dir(self.get)

    # ------------------------------------------------------ debug log ------ #
    def _arkap_debug_log_path(self):
        plugin_dir = self._arkap_plugin_dir()
        return os.path.join(plugin_dir, "ArkAP_debug.log") if plugin_dir else None

    def _log_source_target(self, key):
        """(path, note) for one dropdown entry.

        path is "" when the launcher can't work out where that file would live (no
        SERVER_ROOT yet, say); note is what to tell the user when it isn't there - a
        missing log is normal (no crashes, server never installed), so it gets a plain
        explanation rather than an empty box or an error."""
        srv = os.path.normpath(self.get("SERVER_ROOT")) if self.get("SERVER_ROOT") else ""
        if key == "plugin":
            return (self._arkap_debug_log_path() or "",
                    "set PLUGINS_DIR or SERVER_ROOT on the Configuration tab first. The "
                    "plugin writes this log once it has run at least once.")
        if key == "launcher":
            return (launcher_log_path(),
                    "the launcher writes this as it works - install something, scan for "
                    "paths or save, and it will appear.")
        if key == "crash":
            return (crash_log_path(),
                    "nothing here is good news: this file only appears if the launcher "
                    "hits an unexpected error.")
        if key == "shootergame":
            return (os.path.join(srv, "ShooterGame", "Saved", "Logs", "ShooterGame.log")
                    if srv else "",
                    "set SERVER_ROOT on the Configuration tab. ARK writes this once the "
                    "dedicated server has been started at least once.")
        # SteamCMD's own logs, in the bundled steamcmd folder. Note that a --onefile build
        # unpacks that folder fresh per run (see resource_dir), so these cover the current
        # session's downloads rather than all history.
        name = {"steam_console": "console_log.txt", "steam_workshop": "workshop_log.txt",
                "steam_content": "content_log.txt"}[key]
        return (os.path.join(steamcmd_dir(), "logs", name),
                "SteamCMD writes its logs the first time it runs - install the server or "
                "download a mod and they will appear.")

    def _selected_log_key(self):
        label = self.debug_log_source_var.get()
        for src_label, key in LOG_SOURCES:
            if src_label == label:
                return key
        return LOG_SOURCES[0][1]

    def _refresh_debug_log(self):
        """Load the selected log into the viewer. Reads only the tail of a big file
        (read_log_tail) so switching to ShooterGame.log doesn't freeze the UI."""
        path, note = self._log_source_target(self._selected_log_key())
        if not path:
            content = "(no log to show - %s)" % note
        elif os.path.isfile(path):
            try:
                content = read_log_tail(path) or "(the log exists but is empty: %s)" % path
            except OSError as exc:
                content = "(could not read %s: %s)" % (path, exc)
        else:
            content = "(no log yet at\n %s\n\n %s)" % (path, note)

        widget = self.debug_log_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")
        self._highlight_debug_log_search()

    def _jump_debug_log_latest(self):
        self.debug_log_text.see("end")

    def _highlight_debug_log_search(self):
        widget = self.debug_log_text
        widget.tag_remove("debug_log_hl", "1.0", "end")
        query = self.debug_log_search_var.get().strip()
        if not query:
            return
        widget.tag_configure("debug_log_hl", background=self.SEARCH_HL_COLOR, foreground="black")
        idx = "1.0"
        while True:
            pos = widget.search(query, idx, stopindex="end", nocase=True)
            if not pos:
                break
            end = "%s+%dc" % (pos, len(query))
            widget.tag_add("debug_log_hl", pos, end)
            idx = end

    def _reset_preflight(self, action_label):
        """Shared safety gate for both reset buttons. Returns True if safe to proceed.

        Refuses outright while ShooterGameServer.exe runs (ARK rewrites its save on
        shutdown and would undo the reset). Warns - but allows proceeding after an
        explicit confirm - if the connector runs, since it holds the ipc files open and
        rewrites session.json on its next poll."""
        if is_process_running(ARK_SERVER_PROCESS):
            messagebox.showerror(
                "ARKIpelago Launcher",
                "%s is currently running.\n\nStop the ARK dedicated server first - it "
                "rewrites its world save on shutdown and would undo this reset.\n\n"
                "(%s aborted.)" % (ARK_SERVER_PROCESS, action_label))
            self._log("%s: aborted - %s is running." % (action_label, ARK_SERVER_PROCESS))
            return False
        if is_process_running(CONNECTOR_PROCESS):
            if not messagebox.askyesno(
                    "ARKIpelago Launcher",
                    "%s (the ArkConnector) appears to be running.\n\nIt holds the ipc "
                    "files open and will rewrite session.json on its next poll, which can "
                    "undo this reset. Stop it first for a clean reset.\n\nProceed anyway?"
                    % CONNECTOR_PROCESS):
                self._log("%s: cancelled (connector running)." % action_label)
                return False
        return True

    def _delete_ap_tracking(self, plugin_dir):
        """Delete every generated plugin/connector tracking file plus each per-player
        ipc\\<CharacterName> mailbox subfolder under plugin_dir. Returns (deleted,
        missing, errors). A missing file is normal (nothing to clear), never an error."""
        deleted, missing, errors = [], [], []
        ipc_dir = os.path.join(plugin_dir, "ipc")

        targets = [os.path.join(plugin_dir, n) for n in AP_RESET_PLUGIN_FILES]
        targets += [os.path.join(ipc_dir, n) for n in AP_RESET_IPC_FILES]
        for path in targets:
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    deleted.append(path)
                except OSError as exc:
                    errors.append("%s: %s" % (path, exc))
            else:
                missing.append(path)

        # Multiplayer: each player's mailbox is an ipc\<CharacterName> subfolder - wipe
        # them all recursively (easy to miss, and they re-send checks otherwise).
        if os.path.isdir(ipc_dir):
            try:
                entries = list(os.scandir(ipc_dir))
            except OSError as exc:
                errors.append("%s: %s" % (ipc_dir, exc))
                entries = []
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                try:
                    shutil.rmtree(entry.path)
                    deleted.append(entry.path + os.sep + "  (player mailbox folder)")
                except OSError as exc:
                    errors.append("%s: %s" % (entry.path, exc))

        # VERIFY, the same way the world-save half verifies itself: re-scan the folder
        # and fail loudly on anything that isn't installed payload. Both reset buttons
        # route through here, so both get the check. A hit is either AP state whose
        # filename nobody added above (the ap_connections.json class of bug - it must be
        # added to AP_RESET_PLUGIN_FILES / AP_RESET_IPC_FILES), or a file the user put
        # there themselves; either way the reset is not clean and must not claim it is.
        for path in find_ap_leftovers(plugin_dir):
            errors.append("%s: still present after reset - not installed plugin payload, "
                          "so it is leftover AP state (add it to AP_RESET_PLUGIN_FILES / "
                          "AP_RESET_IPC_FILES)" % path)
        return deleted, missing, errors

    def _backup_and_clear_dir(self, path, ts):
        """Move path to <path>_backup_<ts> and recreate it empty (mirrors
        reset_ark_test.bat: the save is MOVED to a timestamped backup, not deleted),
        then VERIFY: count what was inside before the move and re-count inside the
        backup after it.

        A folder that holds NO files is left in place and NO backup folder is created -
        an empty timestamped backup next to a "backed up!" line is worse than saying
        nothing was there, and the folder is already free of save data anyway.

        Returns a dict:
          kind   - 'moved' (files arrived), 'empty' (folder existed but held NO
                   files - left as-is, no backup), 'created' (didn't exist - made
                   empty), 'error'
          path   - normalized live folder
          backup - backup folder path ('moved' only, else None)
          files / bytes / saves - what the backup actually contains now
                   (saves = ARK_SAVE_EXTS files, the ones that really matter)
          detail - human-readable extra for 'error'"""
        path = os.path.normpath(path)
        if os.path.isdir(path):
            files_before, _bytes_before, _saves_before = count_dir_files(path)
            # Nothing to back up: don't move it (that would create an empty _backup_
            # folder). The folder is already save-free, so leave it exactly where it is.
            if files_before == 0:
                return {"kind": "empty", "path": path, "backup": None,
                        "files": 0, "bytes": 0, "saves": 0, "detail": None}
            # shutil.move onto an EXISTING directory moves the source INSIDE it
            # instead of failing, which would bury one backup inside another (two
            # resets in the same second, or a re-run after a partial failure). Pick
            # an unused name so a backup can never absorb an earlier one.
            backup = "%s_backup_%s" % (path, ts)
            n = 2
            while os.path.exists(backup):
                backup = "%s_backup_%s-%d" % (path, ts, n)
                n += 1
            try:
                shutil.move(path, backup)
            except OSError as exc:
                return {"kind": "error", "path": path, "backup": None,
                        "files": 0, "bytes": 0, "saves": 0, "detail": str(exc)}
            files_after, bytes_after, saves_after = count_dir_files(backup)
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass
            if files_after != files_before:
                return {"kind": "error", "path": path, "backup": backup,
                        "files": files_after, "bytes": bytes_after,
                        "saves": saves_after,
                        "detail": "backup holds %d file(s) but the folder had %d "
                                  "- the move did not carry everything"
                                  % (files_after, files_before)}
            return {"kind": "moved", "path": path, "backup": backup,
                    "files": files_after, "bytes": bytes_after,
                    "saves": saves_after, "detail": None}
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            return {"kind": "error", "path": path, "backup": None,
                    "files": 0, "bytes": 0, "saves": 0, "detail": str(exc)}
        return {"kind": "created", "path": path, "backup": None,
                "files": 0, "bytes": 0, "saves": 0, "detail": None}

    def reset_ap_data(self):
        """Button 1: clear ALL plugin/connector tracking, keep the world save."""
        plugin_dir = self._arkap_plugin_dir()
        if not plugin_dir:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "Can't work out where the ArkAP plugin folder is.\n\nSet SERVER_ROOT "
                "(or the ArkApi Plugins folder) on the Configuration tab first.")
            return
        if not os.path.isdir(plugin_dir):
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "The ArkAP plugin folder doesn't exist yet:\n\n%s\n\nInstall the plugin "
                "first (Install Server/Api/Plugin -> Install Plugin)." % plugin_dir)
            return

        if not self._reset_preflight("Reset AP data"):
            return
        msg = ("This clears ALL Archipelago tracking the plugin and connector generate "
               "(incoming items AND outgoing checks) in:\n\n%s\n\n"
               "  - plugin state / queues / logs\n"
               "  - the saved in-game connection (ap_connections.json) - the server "
               "will NOT auto-reconnect to the old room; /connect again in chat\n"
               "  - ipc mailbox files (session.json, checks_out.jsonl, ...)\n"
               "  - every ipc\\<player> mailbox subfolder\n\n"
               "Your world save, the plugin DLL, ArkAP.config.json and the naming data "
               "(engrams/dinos/locations/crates) are NOT touched.\n\nProceed?" % plugin_dir)
        if not messagebox.askyesno("Reset AP data (keep world save)", msg):
            self._log("Reset AP data: cancelled.")
            return

        self._clear_log()
        deleted, missing, errors = self._delete_ap_tracking(plugin_dir)
        self._report_reset("Reset AP data", plugin_dir, deleted, missing, errors)

    def full_reset_new_seed(self):
        """Button 2: everything reset_ap_data does, plus back up + wipe the world save."""
        root = self.get("SERVER_ROOT")
        if not root:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "SERVER_ROOT is not set - it's needed to find (and back up) the world "
                "save. Set it on the Configuration tab first.")
            return
        root = os.path.normpath(root)
        plugin_dir = self._arkap_plugin_dir()  # may be None / not-yet-installed; handled below

        saved_dir = os.path.join(root, "ShooterGame", "Saved", "SavedArks")
        mapsaves = self.get("SAVESROOT")
        cluster = self.get("CLUSTERDIR")
        save_targets = [("World save", saved_dir)]
        if mapsaves:
            save_targets.append(("Per-map saves", os.path.normpath(mapsaves)))
        if cluster and os.path.isdir(os.path.normpath(cluster)):
            save_targets.append(("Cluster tribute data", os.path.normpath(cluster)))

        if not self._reset_preflight("Full reset for new seed"):
            return

        lines = ["This is a COMPLETE reset for joining a new Archipelago seed. It will:",
                 ""]
        if plugin_dir and os.path.isdir(plugin_dir):
            lines.append("  - clear ALL plugin/connector tracking in:")
            lines.append("      %s" % plugin_dir)
        else:
            lines.append("  - (ArkAP plugin folder not found - no AP tracking to clear)")
        lines.append("  - back up (timestamped) then wipe these saves:")
        for label, path in save_targets:
            lines.append("      %s: %s" % (label, path))
        lines.append("  - remove the randomized-creatures block from Game.ini if this "
                     "launcher added one (backed up first), so the new seed starts "
                     "un-randomized")
        lines += ["", "Backups are MOVED aside (not deleted). The ARK server must be "
                  "STOPPED first.", "", "Proceed?"]
        if not messagebox.askyesno("Full reset for new seed", "\n".join(lines)):
            self._log("Full reset: cancelled.")
            return

        self._clear_log()
        # 1) AP tracking (best-effort - fine if the plugin isn't installed yet).
        if plugin_dir and os.path.isdir(plugin_dir):
            deleted, missing, errors = self._delete_ap_tracking(plugin_dir)
        else:
            deleted, missing, errors = [], [], []
            self._log("Full reset: ArkAP plugin folder not found - skipped AP tracking.")

        # 2) World save + optional per-map / cluster data - each move verified. One
        #    recorder turns a _backup_and_clear_dir result into a log line and returns how
        #    many world/character files it actually moved; reused for the junctions below.
        ts = time.strftime("%Y%m%d-%H%M%S")
        save_lines = []
        moved_saves = 0

        def _record(label, r):
            if r["kind"] == "moved":
                save_lines.append(
                    "%s: backed up %d file(s), %s (%d world/character file(s)) -> %s"
                    % (label, r["files"], fmt_bytes(r["bytes"]), r["saves"], r["backup"]))
                return r["saves"]
            if r["kind"] == "empty":
                save_lines.append(
                    "%s: no files to back up - skipped (no backup folder created)." % label)
            elif r["kind"] == "created":
                save_lines.append("%s: nothing there yet - created empty %s"
                                  % (label, r["path"]))
            else:
                save_lines.append("! %s: could NOT reset %s (%s)"
                                  % (label, r["path"], r["detail"]))
                errors.append("%s: %s" % (r["path"], r["detail"]))
            return 0

        for label, path in save_targets:
            moved_saves += _record(label, self._backup_and_clear_dir(path, ts))

        # 3) Every ShooterGame\Saved\Cluster-<Map> entry, handled EXPLICITLY rather than
        #    trusting the SAVESROOT move above to have reached it - a character surviving a
        #    reset is almost always one of these left behind:
        #      * a REAL folder (not a junction): its saves live here, physically outside
        #        SAVESROOT, so clearing SAVESROOT never touched them - back it up + clear
        #        it right here (this is the case that fully explains "character persisted").
        #      * a junction whose target STILL resolves with data: it points somewhere the
        #        SAVESROOT move didn't cover (SAVESROOT blank/mismatched, or a stale
        #        target) - clear it through the link so nothing survives.
        #      * a junction gone dangling (its target WAS under the SAVESROOT we just moved
        #        aside): recreate the empty target so ARK can save through it next start.
        # ponytail: a cleared real folder is recreated empty, so it stays a real folder and
        # is re-cleared every reset - clean each time, but not self-healing back into a
        # junction. Converting it back to a junction is the bigger SAVESROOT-vs-Saved
        # architecture question, deliberately left out of this symptom fix.
        saved_root = os.path.join(root, "ShooterGame", "Saved")
        for jpath, target, resolves in list_map_junctions(saved_root):
            name = os.path.basename(jpath)
            if target is None:
                moved_saves += _record("%s (real folder in Saved, not a junction)" % name,
                                       self._backup_and_clear_dir(jpath, ts))
            elif resolves:
                moved_saves += _record("junction %s -> %s" % (name, target),
                                       self._backup_and_clear_dir(target, ts))
            else:
                try:
                    os.makedirs(target)
                    save_lines.append(
                        "junction %s -> %s (dangling after the backup move - target "
                        "recreated empty)" % (name, target))
                except OSError as exc:
                    save_lines.append(
                        "! junction %s -> %s is DANGLING and could not be repaired "
                        "(%s). ARK cannot save through it." % (name, target, exc))
                    errors.append("%s: dangling junction (%s)" % (jpath, exc))

        # 4) The actual point of the reset: no world/character file may survive at
        # any live location. saved_root covers SavedArks AND every Cluster-<Map>
        # junction exactly as ARK will read them; the save_targets cover the real
        # per-map folders even where a junction is missing.
        leftovers = find_save_files([saved_root] + [p for _lbl, p in save_targets])
        for path in leftovers:
            save_lines.append("! STILL PRESENT after reset: %s" % path)
            errors.append("%s: still present after reset" % path)

        if not errors and moved_saves == 0:
            save_lines.append(
                "! No world save or character file was found in ANY location this "
                "reset covers - there was nothing to reset. If you expected a "
                "character to be wiped, it lives somewhere else: run "
                "tools\\diagnose_reset.bat before starting the server.")

        # 5) Randomized-creatures block in Game.ini: a fresh seed shouldn't inherit the
        #    previous seed's dino randomization, so strip the app's marker block if one is
        #    there (backed up first, same .bak pattern as the patch feature). ONLY our
        #    marker block is touched - an unmarked hand-pasted fragment and every other
        #    line stay exactly as they are, and a file with no block is a silent no-op.
        game_ini = self._game_ini_path()
        if game_ini and os.path.isfile(game_ini):
            try:
                gi_text, gi_enc = read_text(game_ini)
            except OSError as exc:
                save_lines.append("! Game.ini: could not read to check for a randomized-"
                                  "creatures block (%s)" % exc)
                errors.append("%s: %s" % (game_ini, exc))
            else:
                cleaned, removed = remove_game_ini_marked_block(gi_text)
                if not removed:
                    save_lines.append("Game.ini: no randomized-creatures block to remove.")
                else:
                    try:
                        gi_backup = self._backup_file(game_ini, ts)
                        write_text(game_ini, cleaned, encoding=gi_enc)
                        save_lines.append(
                            "Game.ini: randomized-creatures fragment removed (backup: %s)"
                            % os.path.basename(gi_backup))
                    except OSError as exc:
                        save_lines.append("! Game.ini: found a randomized-creatures block "
                                          "but could NOT remove it (%s)" % exc)
                        errors.append("%s: %s" % (game_ini, exc))
        else:
            save_lines.append("Game.ini: not found - no randomized-creatures block to remove.")

        self._report_reset("Full reset for new seed",
                           plugin_dir or "(plugin not installed)",
                           deleted, missing, errors, extra_lines=save_lines,
                           nothing_found=(moved_saves == 0))

    def _report_reset(self, label, plugin_dir, deleted, missing, errors,
                      extra_lines=None, nothing_found=False):
        """Log a per-file breakdown and show a summary popup. Missing files are reported
        as already-clear, not failures - only real OSErrors count as problems.

        "<label> complete" is only ever shown when there are no errors AND (for the
        full reset, which passes nothing_found) at least one world/character file was
        actually moved. A reset that found nothing to reset gets a warning popup
        instead: from the user's point of view that is a reset that didn't happen."""
        self._log("%s - target: %s" % (label, plugin_dir))
        for line in (extra_lines or []):
            self._log("  %s" % line)
        if deleted:
            self._log("  Deleted %d tracking item(s):" % len(deleted))
            for p in deleted:
                self._log("    - %s" % p)
        else:
            self._log("  No tracking files needed deleting (already clear).")
        if missing:
            self._log("  Already absent (nothing to clear): %d item(s)." % len(missing))
        if errors:
            self._log("  ! %d item(s) could not be removed:" % len(errors))
            for e in errors:
                self._log("    ! %s" % e)

        summary = "Deleted %d tracking item(s); %d already absent." % (
            len(deleted), len(missing))
        if extra_lines:
            summary += "\n\nWorld save:\n" + "\n".join(extra_lines)
        if errors:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "%s did NOT fully complete - %d problem(s), see the log.\n\n"
                "Do not treat this world as reset until every problem above is "
                "resolved.\n\n%s" % (label, len(errors), summary))
        elif nothing_found:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "%s: NOTHING WAS RESET.\n\nNo world save or character file exists "
                "in any location this reset covers. If a character still appears "
                "in-game afterwards, it is stored somewhere this reset doesn't "
                "know about - run tools\\diagnose_reset.bat and check its report "
                "before starting the server.\n\n%s" % (label, summary))
        else:
            messagebox.showinfo("ARKIpelago Launcher",
                                 "%s complete.\n\n%s" % (label, summary))

    def _preflight_bat(self, batname):
        """(missing, unsaved, absent_files) for everything script_requirements() says
        `batname` reads.

        "unsaved" is decided by comparing the Configuration field against the value
        actually written in the file - not against a dirty flag. That's the only honest
        test: a value typed but never Saved simply isn't in the .bat, and the script
        runs on whatever is (this is how SERVER_ROOT/CLUSTERDIR silently ran against
        stale paths before). It also catches values Save REFUSED to write, e.g. a
        relative path (see BAT_PATH_KEYS)."""
        scripts = self._scripts_dir
        missing, unsaved, absent = [], [], set()
        texts = {}
        for key, (fname, var) in sorted(script_requirements(batname).items()):
            value = self.get(key)
            if not value and key not in PREFLIGHT_BLANK_OK:
                missing.append(key)
                continue
            if fname not in texts:
                p = os.path.join(scripts, fname) if scripts else ""
                texts[fname] = read_text(p)[0] if p and os.path.isfile(p) else None
            if texts[fname] is None:
                absent.add(fname)
            elif bat_read_var(texts[fname], var) != value:
                unsaved.append((key, fname))
        return missing, unsaved, sorted(absent)

    def run_bat(self, batname):
        scripts = self._scripts_dir
        path = os.path.join(scripts, batname) if scripts else ""
        if not path or not os.path.isfile(path):
            messagebox.showwarning("ARKIpelago Launcher",
                                   "%s not found in the scripts folder." % batname)
            return

        missing, unsaved, absent = self._preflight_bat(batname)
        if missing or unsaved or absent:
            lines = ["%s reads settings that aren't ready yet, so it wasn't run:"
                     % batname, ""]
            for key in missing:
                lines.append("  * %s is not set - fill it in on the Configuration tab, "
                             "then click Save." % key)
            for key, fname in unsaved:
                lines.append("  * %s has unsaved changes - click Save on the "
                             "Configuration tab first (it's written into %s)."
                             % (key, fname))
            for fname in absent:
                lines.append("  * %s is missing from the scripts folder (%s) - click "
                             "Save on the Configuration tab to recreate it."
                             % (fname, scripts or "(not found)"))
            text = "\n".join(lines)
            messagebox.showwarning("ARKIpelago Launcher", text)
            self._log(text)
            return

        try:
            os.startfile(path)  # double-click behaviour: opens its own console window.
        except OSError as exc:
            messagebox.showerror("ARKIpelago Launcher",
                                 "Could not run %s:\n%s" % (batname, exc))
            return
        if batname == "start_ase_server.bat":
            # After the launch, never before - it must not delay the server starting.
            self._show_server_patience_popup()

    def _show_server_patience_popup(self):
        """Non-blocking "it's starting, give it a minute" note. A plain Toplevel rather
        than messagebox.showinfo: no grab, no modal loop, closing it isn't required, and
        the server is already starting behind it either way."""
        win = tk.Toplevel(self)
        win.title("Starting the ARK server")
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, wraplength=420, justify="left",
                  text="The server's starting up now. This takes a while - sometimes up "
                       "to 15 minutes if it's installed on a hard drive rather than an "
                       "SSD. Be patient, and leave the console window it opened alone.\n\n"
                       "Once it's running, look for it under LAN in ARK: Survival "
                       "Evolved.\n\n"
                       "If it's still unresponsive after 15 minutes, something may have "
                       "gone wrong and is worth investigating - Setup Status and the "
                       "ArkAP Debug Log tabs are the places to look."
                  ).pack(anchor="w")
        ttk.Button(frm, text="Got it", command=win.destroy).pack(anchor="e", pady=(10, 0))

    # ---------------------------------------------------- in-app search ---- #
    # Searches the ENTIRE app - every tab, not just the active one - and
    # switches tabs automatically when stepping to a match elsewhere. Per
    # widget type:
    #   * tk.Text widgets (Instructions tab body, the log panes) - real
    #     per-occurrence highlighting via a text tag.
    #   * ttk.Label / ttk.Checkbutton text and ttk.Entry values - the whole
    #     widget's background is highlighted (Tk has no substring highlighting
    #     outside a Text widget). Entry additionally gets the exact substring
    #     selected (entry.selection_range) while it's the current match, since
    #     that's the closest thing an Entry has to a text-level highlight.
    #   * plain tk.Label (the install reminder banner text) - unlike ttk.Label,
    #     a classic Label always honors direct .configure(background=...), so
    #     it gets a real highlight too.
    #   * ttk.Button labels and Notebook tab names - Windows' native ("vista")
    #     ttk theme ignores background/foreground style overrides on these
    #     (the same limitation already worked around once for Entry, via a
    #     layout hack; not attempted again here to keep this simple). These
    #     get a "flash" instead: focus + a brief pressed/!pressed blink for
    #     buttons, and just becoming the front tab for tab names - genuine
    #     highlighting isn't possible for either.
    #   * hover tooltip text - not visible normally, so a match pops the
    #     tooltip open for ~1.5s (via the Tooltip instance stashed on its
    #     owner widget) in addition to flashing the owner widget.
    #
    # Matches are kept in self._search_matches, built tab-by-tab in Notebook
    # order and top-to-bottom within each tab (header-row widgets like the
    # title, which aren't inside any tab, are scanned first with tab=None),
    # so Find Prev/Next can step through them - switching tabs as needed - and
    # wrap around. Each match dict always has "type" and "tab" (the Notebook
    # tab frame it belongs to, or None); the rest of the keys depend on type:
    #   text:      widget, start, end
    #   entry:     widget, base_style, start, end
    #   widget:    widget, base_style        (Label/Checkbutton)
    #   tklabel:   widget, orig_bg           (plain tk.Label)
    #   button:    widget
    #   tab_label: (tab only - no widget)
    #   tooltip:   widget (the tooltip's owner), tooltip (the Tooltip instance)
    SEARCH_HL_COLOR = "#fff176"
    SEARCH_HL_CURRENT_COLOR = "#ffb300"

    def _setup_search_styles(self):
        style = ttk.Style()
        # Foreground is forced to black on all four styles (not just
        # background) so the highlighted text stays readable regardless of
        # the current theme's default text color - dark mode's light-gray
        # foreground would otherwise wash out against this bright highlight.
        # One highlight pair per base style a match can carry. "SaveHint.TLabel"
        # (the header's yellow "make sure to save!" hint) needs its own pair
        # rather than riding on TLabel's: ttk resolves an unconfigured
        # "Highlight.SaveHint.TLabel" by falling back to "SaveHint.TLabel", so
        # without these the hint would silently refuse to highlight.
        for base in ("TLabel", "TCheckbutton", "SaveHint.TLabel"):
            style.configure("Highlight." + base, background=self.SEARCH_HL_COLOR,
                             foreground="black")
            style.configure("CurrentHighlight." + base,
                             background=self.SEARCH_HL_CURRENT_COLOR,
                             foreground="black")

        # Windows' native "vista"/"xpnative" ttk theme draws TEntry's field
        # background itself (via uxtheme.dll) and silently ignores a plain
        # style.configure(fieldbackground=...) - the field just never changes
        # color. The fix is to give the highlighted entry a layout that swaps
        # in the "default" theme's field element instead, which does honor
        # fieldbackground; padding/textarea children stay the same.
        try:
            style.element_create("plain.field", "from", "default")
        except tk.TclError:
            pass  # already created (re-entrant _build_ui in tests, etc.)
        for style_name, color in (
                ("Highlight.TEntry", self.SEARCH_HL_COLOR),
                ("CurrentHighlight.TEntry", self.SEARCH_HL_CURRENT_COLOR)):
            style.layout(style_name, [
                ("plain.field", {"children": [
                    ("Entry.padding", {"children": [
                        ("Entry.textarea", {"sticky": "nswe"})
                    ], "sticky": "nswe"})
                ], "sticky": "nswe", "border": True})
            ])
            style.configure(style_name, fieldbackground=color, foreground="black")

    @staticmethod
    def _base_style_for(widget):
        """ttk.Entry is deliberately not handled here - _scan_widget_for_search
        intercepts Entry widgets earlier for per-occurrence substring matches
        and hardcodes "TEntry" as their base_style directly."""
        if isinstance(widget, ttk.Checkbutton):
            return "TCheckbutton"
        if isinstance(widget, ttk.Label):
            # A label carrying its own style (the header's SaveHint.TLabel) has to
            # be restored to THAT style when the highlight clears, not to plain
            # TLabel - otherwise one search would permanently strip its colouring.
            return str(widget.cget("style") or "") or "TLabel"
        return ""

    def _iter_widgets(self, root):
        for child in root.winfo_children():
            yield child
            yield from self._iter_widgets(child)

    @staticmethod
    def _widget_visible(widget):
        try:
            return bool(widget.winfo_ismapped())
        except tk.TclError:
            return False

    def _clear_search_highlights(self):
        for match in self._search_matches:
            mtype = match["type"]
            if mtype in ("widget", "entry"):
                try:
                    match["widget"].configure(style=match["base_style"])
                except tk.TclError:
                    pass
                if mtype == "entry":
                    try:
                        match["widget"].selection_clear()
                    except tk.TclError:
                        pass
            elif mtype == "tklabel":
                try:
                    match["widget"].configure(background=match["orig_bg"])
                except tk.TclError:
                    pass
        for widget in self._search_text_widgets:
            try:
                widget.tag_remove("search_hl", "1.0", "end")
                widget.tag_remove("search_hl_current", "1.0", "end")
            except tk.TclError:
                pass
        self._search_matches = []
        self._search_text_widgets = set()
        self._current_match_index = -1

    def _scan_tooltip(self, widget, tooltip, query_lower, tab):
        if tooltip is not None and tooltip.text and query_lower in tooltip.text.lower():
            self._search_matches.append(
                {"type": "tooltip", "widget": widget, "tooltip": tooltip, "tab": tab})

    def _scan_widget_for_search(self, widget, query, query_lower, tab):
        """Check one widget - already known to belong to `tab` (or None if
        it's outside the notebook entirely, e.g. the header title) - for a
        match against `query`, appending to self._search_matches. See the
        match-type table in the comment above _clear_search_highlights."""
        tooltip = getattr(widget, "_tooltip", None)

        if isinstance(widget, tk.Text):
            # The Instructions tab keeps both guides built at once (so collapse state
            # survives switching modes) but only one packed - skip whichever guide isn't
            # currently showing, so search only finds matches in the active one.
            if hasattr(widget, "_instr_vars") and widget is not self._active_instructions_text():
                return
            widget.tag_configure("search_hl", background=self.SEARCH_HL_COLOR, foreground="black")
            widget.tag_configure("search_hl_current", background=self.SEARCH_HL_CURRENT_COLOR,
                                  foreground="black")
            idx = "1.0"
            found_here = False
            while True:
                # elide=True: Tk's Text search skips elided text by default, which
                # would otherwise make the Instructions tab's collapsed steps
                # invisible to search - elide=True finds matches there too (the
                # match jumps to them and auto-expands, see
                # _expand_instruction_step_for_index).
                pos = widget.search(query, idx, stopindex="end", nocase=True, elide=True)
                if not pos:
                    break
                end = "%s+%dc" % (pos, len(query))
                widget.tag_add("search_hl", pos, end)
                self._search_matches.append(
                    {"type": "text", "widget": widget, "start": pos, "end": end, "tab": tab})
                idx = end
                found_here = True
            if found_here:
                self._search_text_widgets.add(widget)
            self._scan_tooltip(widget, tooltip, query_lower, tab)
            return

        if isinstance(widget, ttk.Entry):
            text_lower = widget.get().lower()
            pos = 0
            while True:
                found = text_lower.find(query_lower, pos)
                if found < 0:
                    break
                self._search_matches.append({
                    "type": "entry", "widget": widget, "base_style": "TEntry",
                    "start": found, "end": found + len(query), "tab": tab})
                pos = found + len(query)
            self._scan_tooltip(widget, tooltip, query_lower, tab)
            return

        if isinstance(widget, ttk.Button):
            try:
                text = widget.cget("text")
            except tk.TclError:
                text = ""
            if text and query_lower in text.lower():
                self._search_matches.append({"type": "button", "widget": widget, "tab": tab})
            self._scan_tooltip(widget, tooltip, query_lower, tab)
            return

        if isinstance(widget, tk.Label):  # plain tk.Label only - ttk.Label isn't a subclass
            try:
                text = widget.cget("text")
            except tk.TclError:
                text = ""
            if text and query_lower in text.lower():
                self._search_matches.append({
                    "type": "tklabel", "widget": widget, "tab": tab,
                    "orig_bg": widget.cget("background")})
            self._scan_tooltip(widget, tooltip, query_lower, tab)
            return

        base_style = self._base_style_for(widget)
        if base_style:
            try:
                text = widget.cget("text")
            except tk.TclError:
                text = ""
            if text and query_lower in text.lower():
                self._search_matches.append({
                    "type": "widget", "widget": widget, "base_style": base_style, "tab": tab})
        self._scan_tooltip(widget, tooltip, query_lower, tab)

    def _run_search(self, query):
        """(Re)build the match list for `query` and jump to the first match.

        Bound to Enter in the search box; also called by Find Prev/Next when
        the query text has changed since the last search. Scans every tab
        (not just the active one), in tab order and top-to-bottom within
        each tab.
        """
        self._clear_search_highlights()
        self._last_search_query = query
        if not query:
            self.search_status_var.set("")
            return

        query_lower = query.lower()

        # Header row (title/subtitle) sits outside the Notebook entirely and
        # is always visible regardless of tab - scan it first, tab=None.
        for widget in self._iter_widgets(self.header_row):
            self._scan_widget_for_search(widget, query, query_lower, None)

        for tab_id in self.notebook.tabs():
            tab_frame = self.notebook.nametowidget(tab_id)
            tab_text = self.notebook.tab(tab_id, "text")
            if tab_text and query_lower in tab_text.lower():
                self._search_matches.append({"type": "tab_label", "tab": tab_frame})
            for widget in self._iter_widgets(tab_frame):
                self._scan_widget_for_search(widget, query, query_lower, tab_frame)

        match_count = len(self._search_matches)
        if match_count:
            self.search_status_var.set(
                "%d match%s" % (match_count, "" if match_count == 1 else "es"))
            self._current_match_index = 0
            self._render_current_match()
            self._scroll_to_current_match()
        else:
            self.search_status_var.set("No matches")

    def _render_current_match(self):
        """Apply the stronger highlight to the current match, plain to the
        rest. "button"/"tab_label"/"tooltip" matches have no persistent
        highlight to toggle here - they only get feedback (flash/tooltip
        popup) from _scroll_to_current_match when they become current."""
        for i, match in enumerate(self._search_matches):
            is_current = (i == self._current_match_index)
            mtype = match["type"]
            if mtype == "text":
                tag = "search_hl_current" if is_current else "search_hl"
                other_tag = "search_hl" if is_current else "search_hl_current"
                widget = match["widget"]
                try:
                    widget.tag_remove(other_tag, match["start"], match["end"])
                    widget.tag_add(tag, match["start"], match["end"])
                except tk.TclError:
                    pass
            elif mtype in ("widget", "entry"):
                prefix = "CurrentHighlight." if is_current else "Highlight."
                try:
                    match["widget"].configure(style=prefix + match["base_style"])
                except tk.TclError:
                    pass
                if mtype == "entry":
                    entry = match["widget"]
                    try:
                        if is_current:
                            entry.selection_range(match["start"], match["end"])
                            entry.icursor(match["start"])
                        else:
                            entry.selection_clear()
                    except tk.TclError:
                        pass
            elif mtype == "tklabel":
                color = self.SEARCH_HL_CURRENT_COLOR if is_current else self.SEARCH_HL_COLOR
                try:
                    match["widget"].configure(background=color)
                except tk.TclError:
                    pass

    def _update_find_btns(self):
        """Show Find Prev/Next only while the search box has text in it."""
        if self.search_var.get().strip():
            self._find_btns.pack(side="left", before=self._search_status_label)
        else:
            self._find_btns.pack_forget()

    def _find_next(self):
        self._step_match(1)

    def _find_prev(self):
        self._step_match(-1)

    def _step_match(self, direction):
        query = self.search_var.get().strip()
        if not query:
            return
        if query != self._last_search_query:
            self._run_search(query)
            if not self._search_matches:
                return
            if direction < 0:
                self._current_match_index = len(self._search_matches) - 1
                self._render_current_match()
                self._scroll_to_current_match()
            return
        if not self._search_matches:
            return
        self._current_match_index = (
            (self._current_match_index + direction) % len(self._search_matches))
        self._render_current_match()
        self._scroll_to_current_match()

    def _scroll_to_current_match(self):
        if not (0 <= self._current_match_index < len(self._search_matches)):
            return
        match = self._search_matches[self._current_match_index]

        tab = match.get("tab")
        if tab is not None:
            try:
                if str(self.notebook.select()) != str(tab):
                    self.notebook.select(tab)
                    # A plain update_idletasks() isn't enough to re-map a
                    # widget embedded in the Configuration tab's scrollable
                    # Canvas (via create_window) after its tab was hidden and
                    # re-shown - winfo_ismapped() below would still read
                    # False. A full update() forces that remap to actually
                    # complete before the visibility check runs.
                    self.update()
            except tk.TclError:
                pass

        mtype = match["type"]
        if mtype == "tab_label":
            return  # selecting the tab above is the only feedback a tab name can get

        widget = match["widget"]
        # A dismissed reminder banner (or similar pack_forget'd content) can
        # still be unmapped even on its own tab - nothing sensible to scroll
        # to in that case, so just leave it counted as a match and move on.
        if not self._widget_visible(widget):
            return

        if mtype == "text":
            if hasattr(widget, "_instr_vars"):
                self._expand_instruction_step_for_index(widget, match["start"])
            self._center_text_index(widget, match["start"])
            return

        self._center_widget_in_canvas(widget)
        if mtype == "entry":
            try:
                widget.focus_set()
            except tk.TclError:
                pass
        elif mtype in ("button", "tooltip"):
            self._flash_widget(widget)
            if mtype == "tooltip":
                self._flash_tooltip(match["tooltip"])

    def _flash_widget(self, widget):
        """Visual nudge for match types that can't hold a persistent
        highlight (see the comment above _clear_search_highlights). Briefly
        toggles the native pressed/!pressed visual state, which Windows'
        vista ttk theme does honor even though it ignores style color
        overrides, and always tries to take focus too."""
        try:
            widget.focus_set()
        except tk.TclError:
            pass
        if not isinstance(widget, (ttk.Button, ttk.Checkbutton)):
            return

        def _blink(n):
            try:
                widget.state(["pressed"] if n % 2 else ["!pressed"])
            except tk.TclError:
                return
            if n > 0:
                widget.after(120, _blink, n - 1)
        _blink(5)

    @staticmethod
    def _flash_tooltip(tooltip):
        """Pop the hover tooltip open for a couple seconds so a tooltip-only
        match is actually shown, not just inferred from its owner widget."""
        try:
            tooltip._show()
        except tk.TclError:
            return
        tooltip.widget.after(1500, tooltip._hide)

    @staticmethod
    def _find_ancestor_canvas(widget):
        """Walk up the widget tree to find an enclosing tk.Canvas (the scrolled
        Configuration-tab field area), or None if this widget isn't in one."""
        w = widget
        while True:
            parent_name = w.winfo_parent()
            if not parent_name:
                return None
            w = w.nametowidget(parent_name)
            if isinstance(w, tk.Canvas):
                return w

    def _center_widget_in_canvas(self, widget):
        canvas = self._find_ancestor_canvas(widget)
        if canvas is None:
            return
        try:
            canvas.update_idletasks()
            bbox = canvas.bbox("all")
            if not bbox:
                return
            total_height = bbox[3] - bbox[1]
            if total_height <= 0:
                return
            target_y = (widget.winfo_rooty() - canvas.winfo_rooty()
                        + canvas.canvasy(0) - bbox[1])
            target_center = target_y + widget.winfo_height() / 2
            view_height = canvas.winfo_height()
            top = target_center - view_height / 2
            frac = max(0.0, min(1.0, top / total_height))
            canvas.yview_moveto(frac)
        except tk.TclError:
            pass

    def _expand_instruction_step_for_index(self, widget, index):
        """If `index` falls inside a collapsed instruction section or step, open it -
        otherwise a search match hiding under an elided (collapsed) region would be
        "found" but never actually become visible when scrolled to."""
        section_vars, step_vars, step_label_vars = getattr(
            widget, "_instr_vars", ({}, {}, {}))
        for tag_name in widget.tag_names(index):
            # Re-open the whole section first (its fold hides everything inside it).
            svar = section_vars.get(tag_name)
            if svar is not None and svar.get():
                svar.set(False)
            var = step_vars.get(tag_name)
            if var is not None and var.get():
                var.set(False)
            # The "Step N" stub is the mirror case: it's hidden while the step is
            # expanded, so a match landing in it needs the step collapsed instead.
            var = step_label_vars.get(tag_name)
            if var is not None and not var.get():
                var.set(True)

    @staticmethod
    def _center_text_index(widget, index):
        """Center the line at `index` inside `widget`'s current viewport.

        Text's own API is line-fraction based, not pixel based, so this uses
        see() to make the index visible at all, then reads back its actual
        pixel offset (bbox) to nudge the view so it lands mid-viewport instead
        of just-barely-visible at an edge.
        """
        try:
            widget.see(index)
            widget.update_idletasks()
            bbox = widget.bbox(index)
            if not bbox:
                return
            _, y, _, h = bbox
            view_height = widget.winfo_height()
            first, last = widget.yview()
            span = last - first
            if span <= 0:
                return
            delta_px = (y + h / 2) - view_height / 2
            delta_frac = (delta_px / view_height) * span
            widget.yview_moveto(max(0.0, min(1.0, first + delta_frac)))
        except tk.TclError:
            pass


def _report_startup_crash(exc_type, exc_value, exc_tb):
    """Last-resort handler for a crash before/around the mainloop (or one that escapes it):
    log the full traceback next to the config file, then tell the user where it is instead
    of the window just vanishing or a raw traceback flashing past in a console-less build."""
    path = write_crash_log(exc_type, exc_value, exc_tb)
    where = path or "%s (could not be written)" % crash_log_path()
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "ARKIpelago Launcher - Something went wrong",
            "The launcher hit an unexpected error and has to close.\n\n"
            "A full crash report was saved to:\n%s\n\n"
            "Please attach that file (or use \"Export diagnostics\" on the Configuration "
            "tab) when reporting this on Discord or GitHub." % where)
        root.destroy()
    except tk.TclError:
        # No display at all - nothing more we can do beyond the log we already wrote.
        pass


if __name__ == "__main__":
    try:
        ArkAPLauncher().mainloop()
    except SystemExit:
        raise
    except BaseException:  # noqa: B036 - top-level catch-all is the whole point here
        _report_startup_crash(*sys.exc_info())
        sys.exit(1)
