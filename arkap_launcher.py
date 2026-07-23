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

import os
import re
import sys
import json
import time
import glob
import ctypes
import codecs
import queue
import shutil
import hashlib
import zipfile
import tempfile
import webbrowser
import threading
import subprocess
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
    ("Connector", [
        ("server",     "server",     "text"),
        ("slot",       "slot",       "text"),
        ("password",   "password",   "text"),
        ("death_link", "death_link", "bool"),
        ("ipc_dir",    "ipc_dir",    "folder"),
        ("data_dir",   "data_dir",   "folder"),
        ("game_ini",   "game_ini",   "file"),
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

# Hover tooltip text per field key: what it is, an example location, and any
# recommended values / gotchas worth knowing before editing it.
FIELD_HELP = {
    "connector_ini": (
        "The Archipelago connector's config file (read/written by ark_ap_connector.py).\n"
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
    "server": (
        "Your Archipelago room address, host:port - shown when you host or join the "
        "room.\n"
        "Example: archipelago.gg:38281"
    ),
    "slot": "Your Archipelago slot/player name, exactly as it appears in your .yaml (case-sensitive).",
    "password": "Room password for the Archipelago server. Leave blank if the room has none.",
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

# .bat files we expose Run buttons for. (reset_ark_test / apply_server_config no longer
# get Quick Launch buttons - the in-app reset controls and Save cover them; the .bat files
# still ship and read paths.cmd for anyone who runs them by hand.)
RUN_BATS = [
    ("Run start_ase_server", "start_ase_server.bat"),
    ("Run switch_map",       "switch_map.bat"),
]

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

# JSON key (in CONFIG_FILENAME, deliberately NOT in the profiles file) holding the newest
# release version the user has clicked "Check for Updates" through to see. Persisted
# separately from APP_VERSION so it survives restarts and drives the button HIGHLIGHT:
# the highlight re-appears only when a release newer than THIS value ships. Distinct from
# the exclamation badge, which tracks "is anything newer than the installed APP_VERSION
# at all". See _compute_update_cues.
ACK_VERSION_KEY = "last_acknowledged_version"

# JSON keys recording the version of ArkServerApi / the ArkAP plugin the launcher last
# installed, so Setup Status can flag (as an advisory, never a failure) when a newer
# release exists. Stamped by the install flow that has the version in hand - the ArkApi
# tag comes straight from the GitHub download; the plugin's is the current latest tag at
# install time (the tag the GitHub download reported). Both are read directly off the
# config JSON, like REMINDER_HIDE_KEY above.
ARKAPI_INSTALLED_VERSION_KEY = "arkapi_installed_version"
PLUGIN_INSTALLED_VERSION_KEY = "plugin_installed_version"

# Config JSON fields the "Export diagnostics" zip must never leak. Everything else in the
# config is kept as-is so it's still useful for troubleshooting - these three are replaced
# in place with a marker rather than the whole file being withheld.
REDACT_MARKER = "[REDACTED]"
REDACT_CONFIG_KEYS = ("ADMINPASS", "SERVERPASS", "password")

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
AP_RESET_PLUGIN_FILES = [
    "state.json", "seed.json", "applied_index.json", "counters.json",
    "events_queue.jsonl", "ArkAP_note_hits.jsonl", "note_queue.jsonl",
    "tame_check_queue.jsonl", "kill_check_queue.jsonl", "dino_queue.jsonl",
    "crate_queue.jsonl", "ArkAP_debug.log",
]
AP_RESET_IPC_FILES = [
    "session.json", "state.json", "checks_out.jsonl", "items_in.jsonl",
    "death_out.jsonl", "death_in.jsonl", "msg_in.jsonl", "hint_out.jsonl",
    "hint_status.json", "flags.json", "game_ini_fragment.txt",
]

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
APP_VERSION = "0.4.0"
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


def redact_config(data):
    """Copy of the config dict with REDACT_CONFIG_KEYS replaced in place by REDACT_MARKER
    (only when present and non-empty), so the rest stays useful for troubleshooting."""
    out = dict(data)
    for key in REDACT_CONFIG_KEYS:
        if out.get(key):
            out[key] = REDACT_MARKER
    return out


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

STEAM_VDF_PATHS = [
    r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf",
    r"C:\Program Files\Steam\steamapps\libraryfolders.vdf",
]

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
}

# Hard caps so the fallback drive walk (run on a background thread) can never run away
# on a huge/slow disk - it gives up and reports "not found" rather than hanging.
MAX_SCAN_DIRS = 15000
SCAN_TIME_BUDGET_SECONDS = 20.0

# How many levels below a drive root (or Steam library) we'll look for the exe.
MAX_SCAN_DEPTH = 3


def steam_library_dirs():
    """Steam install dir(s) + any additional library folders from libraryfolders.vdf."""
    dirs = []
    for vdf in STEAM_VDF_PATHS:
        if not os.path.isfile(vdf):
            continue
        try:
            text, _ = read_text(vdf)
        except OSError:
            continue
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            p = m.group(1).replace("\\\\", "\\")
            if os.path.isdir(p):
                dirs.append(p)
        steam_root = os.path.dirname(os.path.dirname(vdf))  # .../Steam
        if os.path.isdir(steam_root):
            dirs.append(steam_root)
    seen, out = set(), []
    for d in dirs:
        k = d.lower()
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def direct_candidate_server_roots():
    """Fixed, fast-to-check guesses: Steam libraries + the handful of folder layouts
    people commonly install a manual ARK dedicated server into."""
    cands = []
    for lib in steam_library_dirs():
        common = os.path.join(lib, "steamapps", "common")
        cands.append(os.path.join(common, "ARK Survival Evolved Dedicated Server"))
        cands.append(os.path.join(common, "ARK Survival Evolved"))
    for letter in COMMON_DRIVE_LETTERS:
        drive = "%s:\\" % letter
        if not os.path.isdir(drive):
            continue
        cands.extend([
            os.path.join(drive, "ARK", "Server"),
            os.path.join(drive, "ArkServer"),
            os.path.join(drive, "Games", "ARK Survival Evolved Dedicated Server"),
            os.path.join(drive, "SteamLibrary", "steamapps", "common",
                         "ARK Survival Evolved Dedicated Server"),
            os.path.join(drive, "Steam", "steamapps", "common",
                         "ARK Survival Evolved Dedicated Server"),
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


def bounded_drive_scan(log_fn, is_cancelled):
    """Depth-limited, filtered walk of common drive roots looking for
    ShooterGameServer.exe. Bounded by MAX_SCAN_DIRS and SCAN_TIME_BUDGET_SECONDS so a
    slow/huge disk degrades to "gave up" instead of hanging. Meant for a background
    thread - this alone is why it's safe to point at whole drive roots at all."""
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
            if os.path.isfile(os.path.join(path, ARK_EXE_RELPATH)):
                return path
            if depth >= MAX_SCAN_DEPTH:
                continue
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if entry.name.lower() in SKIP_SCAN_DIR_NAMES:
                            continue
                        if _BACKUP_SNAPSHOT_RE.search(entry.name.lower()):
                            continue  # a backed-up server tree has its own ShooterGameServer.exe
                        stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return None


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
        if is_backup_snapshot_path(path):
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
        # Server Install), and both must clear/recolour together or the one that isn't
        # registered silently swallows what the user types into it.
        self._placeholder_entries = {}
        # Fields whose loaded value was only the shipped example (see _set_from_file).
        self._ignored_example_values = set()
        self._last_scoped_scan_root = None
        self._last_cluster_dir_scan = None
        # Scripts folder is no longer a user field - it's the working folder next to the
        # launcher that bundled scripts are extracted into (set in _discover_locations).
        self._scripts_dir = working_scripts_dir()
        self.backup_var = tk.BooleanVar(value=True)
        self._logo_img = None     # keep a reference so Tk doesn't garbage-collect it

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
        # _start_background_update_check) - it only sets the badge, never opens the
        # update dialog or downloads anything on its own.
        self._update_check_thread = None
        self._update_download_thread = None
        self._update_download_queue = queue.Queue()
        self._update_progress_win = None
        # Component-version advisory rows (ArkApi / plugin newer-than-installed), filled in
        # by the background check and appended to Setup Status. See #4 / _on_component_versions.
        self._component_advisories = []
        # PhotoImages for the Setup Status tab-bar symbol, kept referenced so Tk doesn't
        # GC them; rebuilt per state/theme in _update_status_tab_indicator.
        self._status_tab_glyphs = {}

        # Theme must be selected before _build_ui() constructs any widget,
        # since widget colors are read from self.theme at construction time.
        self._apply_theme(self._read_theme_pref())

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

        # Armed last, so the first snapshot it writes is of fully-loaded values.
        self._start_autosave()

        if self._is_first_launch and not self.get("SERVER_ROOT"):
            self._start_auto_detect()

        # Local file read only (no network) - reports the outcome of an update helper
        # that ran just before this process started, if there was one.
        self._check_previous_update_result()
        # Best-effort housekeeping: remove any update-helper leftovers (staging, backups,
        # the helper script/zip) and stale --onefile _MEI temp folders from before the
        # switch to --onedir. Never fatal.
        self._sweep_update_leftovers()

        # Silent, non-blocking: runs on a background thread so it can never delay the
        # window appearing, and fails quietly (no popup) if GitHub is unreachable.
        self._start_background_update_check()
        # Same check for the installed ArkApi / plugin vs their latest GitHub releases -
        # feeds the yellow "i" advisory rows on Setup Status (see #4). Background/quiet.
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
        # _start_background_update_check) finds a newer release. Packed after the
        # button so it sits on the button's left. Kept always-packed with empty
        # text rather than pack/pack_forget, so showing it doesn't shift the other
        # header buttons. Its own warm-gold colour (update_badge_fg) reads as a
        # secondary nudge, not an error - brighter/more saturated than status_info
        # but not the loud red of status_fail.
        self.update_badge_label = ttk.Label(header_row, text="", foreground=self.theme["update_badge_fg"],
                                             font=(self._header_font_family or "Segoe UI", 12, "bold"))
        self.update_badge_label.pack(side="right", padx=(0, 4))
        Tooltip(self.update_badge_label, "A newer version is available - click Check for Updates.")
        Tooltip(self.update_check_btn,
                "Check GitHub for a newer launcher release (current version: %s). "
                "Also checked silently once on launch; clicking always does a fresh "
                "check." % APP_VERSION)

        header_font_family = self._register_header_font()
        title_row = ttk.Frame(header_row)
        title_row.pack(side="left", fill="x", expand=True)
        ttk.Label(title_row, text="ARKIpelago Launcher",
                  font=(header_font_family, 16, "bold")).pack(side="left")
        # Highlighted rather than dimmed: it's the one thing in the header the user
        # has to act on, and as plain subtle-grey text beside a 16pt title it read as
        # decoration. Uses the theme's warn colours (same pale yellow as the install
        # reminder banner) via a style, so the toggle repaints it automatically.
        ttk.Label(title_row, text="make sure to save!", style="SaveHint.TLabel",
                  padding=(6, 2)).pack(side="left", padx=12)

        # Search bar - left-aligned directly below the title (not centered
        # under the logo). Enter runs the search and jumps to the first match;
        # Find Prev/Next step through the rest, centering each in view.
        search_bar = ttk.Frame(top, padding=(0, 6, 0, 0))
        search_bar.pack(fill="x", anchor="w")
        ttk.Label(search_bar, text="Search:").pack(side="left", padx=(0, 4))
        self.search_entry = ttk.Entry(search_bar, textvariable=self.search_var, width=32)
        self.search_entry.pack(side="left")
        self.search_entry.bind("<Return>", lambda _e: self._run_search(self.search_var.get().strip()))
        self.find_prev_btn = ttk.Button(search_bar, text="Find Prev", width=9,
                                         command=self._find_prev)
        self.find_prev_btn.pack(side="left", padx=(8, 2))
        self.find_next_btn = ttk.Button(search_bar, text="Find Next", width=9,
                                         command=self._find_next)
        self.find_next_btn.pack(side="left", padx=(2, 8))
        self.search_status_var = tk.StringVar(value="")
        ttk.Label(search_bar, textvariable=self.search_status_var,
                  foreground=self.theme["subtle_fg"], width=14).pack(side="left", padx=(4, 0))
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
        tab_profiles = ttk.Frame(notebook)
        tab_install = ttk.Frame(notebook)
        tab_status = ttk.Frame(notebook)
        tab_debug = ttk.Frame(notebook)
        tab_instructions = ttk.Frame(notebook)
        notebook.add(tab_config, text="Configuration")
        notebook.add(tab_profiles, text="Profiles")
        notebook.add(tab_install, text="Server Install")
        notebook.add(tab_status, text="Setup Status")
        notebook.add(tab_debug, text="Debug Log")
        notebook.add(tab_instructions, text="Instructions")
        self.notebook = notebook
        self.tab_profiles = tab_profiles
        self.tab_install = tab_install
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

        # Install reminder banner - dismissible, points at the Server Install tab.
        self.reminder_banner = tk.Frame(inner, background=self.theme["warn_bg"],
                                         highlightbackground=self.theme["warn_border"],
                                         highlightthickness=1)
        if not self._hide_install_reminder:
            self.reminder_banner.pack(fill="x", pady=(0, 8))
        tk.Label(self.reminder_banner, background=self.theme["warn_bg"],
                 foreground=self.theme["warn_fg"],
                 justify="left", wraplength=520,
                 text="Install the ARK dedicated server first. Use the \"Server Install\" "
                      "tab (SteamCMD) to install it before relying on the paths below. Go to the instructions tab for a step by step guide"
                 ).pack(side="left", fill="x", expand=True, padx=8, pady=6)
        rbtns = tk.Frame(self.reminder_banner, background=self.theme["warn_bg"])
        rbtns.pack(side="right", padx=6, pady=4)
        ttk.Button(rbtns, text="Go to Server Install",
                   command=self._goto_install_tab).pack(fill="x", pady=(0, 2))
        rbtns2 = tk.Frame(rbtns, background=self.theme["warn_bg"])
        rbtns2.pack(fill="x")
        ttk.Button(rbtns2, text="Close", width=8,
                   command=self._dismiss_reminder).pack(side="left", padx=(0, 2))
        ttk.Button(rbtns2, text="Don't show again",
                   command=self._dismiss_reminder_forever).pack(side="left")

        for title, fields in GROUPS:
            lf = ttk.LabelFrame(inner, text=title, padding=(10, 6))
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

        self._build_config_upload_section(inner)

        # Bottom action bar (fixed) --------------------------------------------
        bottom = ttk.Frame(tab_config, padding=(10, 8))
        bottom.pack(fill="x")

        q = ttk.LabelFrame(bottom, text="Quick launch", padding=(8, 6))
        q.pack(fill="x")
        qrow = ttk.Frame(q)
        qrow.pack(fill="x")
        for text, cmd in [
            ("Open ipc folder",        self.open_ipc),
            ("Open Plugins folder",    self.open_plugins),
            ("Open Game.ini folder",   self.open_gameini_folder),
            ("Open SERVER_ROOT",       self.open_server_root),
            ("Open ClusterDir folder", self.open_cluster_dir),
        ]:
            btn = ttk.Button(qrow, text=text, command=cmd)
            btn.pack(side="left", padx=3, pady=2)
            if text == "Open ClusterDir folder":
                Tooltip(btn, "Opens the cluster data folder (ClusterDir) in Explorer.")

        rrow = ttk.Frame(q)
        rrow.pack(fill="x")
        for text, batname in RUN_BATS:
            ttk.Button(rrow, text=text,
                       command=lambda b=batname: self.run_bat(b)
                       ).pack(side="left", padx=3, pady=2)

        # Sits where "Run reset_ark_test" / "Run apply_server_config" used to, on the same
        # row as the other Run buttons.
        patch_gi_btn = ttk.Button(rrow, text="Patch Game.ini for randomized creatures",
                                  command=self.patch_game_ini_for_randomized_dinos)
        patch_gi_btn.pack(side="left", padx=3, pady=2)
        Tooltip(patch_gi_btn,
                "Applies the plugin's ipc\\%s into your Game.ini (backed up first) so "
                "randomized creatures take effect - the automated version of copying that "
                "block in by hand. Stop the ARK server first." % GAME_INI_FRAGMENT_NAME,
                wraplength=520)

        # New-seed reset controls. These replace the old "Delete session.json" button,
        # which only cleared the AP->game direction (session.json) and left the outgoing
        # checks (checks_out.jsonl etc.) behind - so a fresh room got flooded with the
        # previous seed's checks on the connector's first read. Both buttons clear ALL
        # generated plugin/connector tracking; the second also backs up + wipes the world
        # save (an in-app equivalent of reset_ark_test.bat that doesn't rely on .bat paths).
        reset_row = ttk.Frame(q)
        reset_row.pack(fill="x", pady=(2, 0))
        reset_ap_btn = ttk.Button(reset_row, text="Reset AP data (keep world save)",
                                   command=self.reset_ap_data)
        reset_ap_btn.pack(side="left", padx=3, pady=2)
        Tooltip(reset_ap_btn,
                "Clears all Archipelago tracking the plugin and connector generate. "
                "Note: if your character/world isn't also reset, level and inventory "
                "checks will immediately re-send. Use 'Full reset for new seed' instead "
                "when starting a new seed.")
        full_reset_btn = ttk.Button(reset_row, text="Full reset for new seed",
                                     command=self.full_reset_new_seed)
        full_reset_btn.pack(side="left", padx=3, pady=2)
        Tooltip(full_reset_btn,
                "Complete reset before joining a new Archipelago seed: clears all "
                "plugin/connector tracking AND backs up + wipes the world save. The ARK "
                "server must be stopped first.")

        act = ttk.Frame(bottom)
        act.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(act, text="Back up each file (.bak) before writing",
                        variable=self.backup_var).pack(side="left")
        # Save sits in a thin warn_bg/warn_border frame - the same pale yellow as the
        # header's "make sure to save!" hint and the install reminder banner, so the
        # reminder and the button it points at read as one thing. A frame rather than a
        # styled button because ttk's "vista" engine ignores background on TButton.
        self.save_btn_halo = tk.Frame(act, background=self.theme["warn_bg"],
                                      highlightbackground=self.theme["warn_border"],
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
                "Bundle ArkAP_debug.log, a Setup Status summary, a password-redacted copy "
                "of your config, and the crash log (if any) into one zip on your Desktop - "
                "drag it into Discord or a GitHub issue when asking for help.")

        # Status / report log ---------------------------------------------------
        self.log = tk.Text(bottom, height=7, wrap="word", state="disabled",
                           font=("Consolas", 9),
                           background=self.theme["text_bg"], foreground=self.theme["text_fg"],
                           insertbackground=self.theme["text_fg"])
        self.log.pack(fill="x", pady=(8, 0))

        self._build_install_tab(tab_install)
        self._build_setup_status_tab(tab_status)
        self._build_debug_log_tab(tab_debug)
        self._build_profiles_tab(tab_profiles)
        self._build_instructions_tab(tab_instructions)

        # Live "does this still match the loaded profile?" indicator - wired up last
        # so profile_status_var (built by _build_profiles_tab above) already exists.
        for var in self.vars.values():
            var.trace_add("write", lambda *_a: self._update_profile_status())

    # ------------------------------------- Game.ini / GameUserSettings upload - #
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
        wrap = ttk.Frame(parent, padding=(10, 8))
        wrap.pack(fill="both", expand=True)

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
        plug = ttk.LabelFrame(wrap, text="Install ArkAP Plugin", padding=(8, 6))
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
                       "changed automatically - use Configuration / Server Install to fix "
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
            "hint": "Set SERVER_ROOT on the Configuration tab, then Server Install -> "
                    "Install ARK Server.",
        })

        ok, detail = check_arkapi_installed(root)
        items.append({
            "label": "ArkApi installed",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": "Server Install -> Install ArkServerApi (needs the ARK server "
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
            "hint": "Server Install -> Install Plugin.",
        })

        ok, detail = check_plugin_mode(plugin_dir)
        items.append({
            "label": "Plugin mode is \"ap\" (not offline)",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": "Set \"mode\": \"ap\" in ArkAP.config.json for real multiworld play "
                    "(\"offline\" self-randomizes locally for solo hook testing).",
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

        # Reads the FILE, not the Connector fields - so a user with server/slot/ipc_dir
        # all filled in still fails this when no connector.ini exists to write them to
        # (Save logs "connector.ini: not found - skipped" and the fields only ever reach
        # the launcher's own JSON). Two different failures, so two different hints -
        # telling someone to "fill in the fields and Save" when the fields ARE filled is
        # what sent them in circles.
        ini_path = self.get("connector_ini")
        ok, detail = check_connector_filled(ini_path)
        items.append({
            "label": "connector.ini filled in (server / slot / ipc_dir)",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": ("Point Connector tab in configurations  "
                     "make sure you've set your server (eg. archipelago.gg:38281) "
                     "and your slot name and password if room has one. also make sure ipc_dir has been set"
                     if not (ini_path and os.path.isfile(ini_path)) else
                     "Fill in server / slot / ipc_dir in the Connector group on the "
                     "Configuration tab, then Save."),
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
        try:
            current = self.notebook.select()
        except tk.TclError:
            return
        if current == str(self.tab_status):
            self._refresh_setup_status()
        elif current == str(self.tab_profiles):
            self._update_profile_status()

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

    def export_diagnostics(self):
        """Bundle the debug log, a Setup Status summary, a redacted config copy, and the
        crash log (if any) into one zip the user can drag straight into Discord. Secrets
        (ADMINPASS / SERVERPASS / connector password) are replaced with [REDACTED] - the
        rest of the config is kept so it's still useful for troubleshooting."""
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        initial_dir = desktop if os.path.isdir(desktop) else base_dir()
        default_name = "arkap_diagnostics_%s.zip" % time.strftime("%Y%m%d_%H%M%S")
        dest = filedialog.asksaveasfilename(
            title="Save diagnostics zip",
            initialdir=initial_dir, initialfile=default_name,
            defaultextension=".zip", filetypes=[("Zip archive", "*.zip")])
        if not dest:
            return

        # Gather text pieces first so a read error surfaces before we open the zip.
        summary = format_setup_status_summary(self._gather_setup_status())
        redacted = self._redacted_config_json()
        debug_path = self._arkap_debug_log_path()
        crash_path = crash_log_path()
        included = []
        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("setup_status.txt", summary)
                zf.writestr("arkap_launcher_config.redacted.json", redacted)
                if debug_path and os.path.isfile(debug_path):
                    zf.write(debug_path, "ArkAP_debug.log")
                    included.append("ArkAP_debug.log")
                else:
                    zf.writestr("ArkAP_debug.log.txt",
                                "(no ArkAP_debug.log found - expected at %s)"
                                % (debug_path or "unknown; set SERVER_ROOT/PLUGINS_DIR"))
                if os.path.isfile(crash_path):
                    zf.write(crash_path, CRASH_LOG_FILENAME)
                    included.append(CRASH_LOG_FILENAME)
        except OSError as exc:
            messagebox.showerror("Export diagnostics",
                                 "Could not write the diagnostics zip:\n\n%s" % exc)
            return

        extra = (" It also includes: %s." % ", ".join(included)) if included else ""
        messagebox.showinfo(
            "Export diagnostics",
            "Diagnostics saved to:\n\n%s\n\nIt contains a Setup Status summary and a "
            "redacted copy of your config (passwords removed).%s\n\nDrag this file into "
            "Discord or attach it to a GitHub issue when asking for help." % (dest, extra))
        try:
            os.startfile(os.path.dirname(dest))  # noqa: S606 - open the folder in Explorer
        except OSError:
            pass

    # ------------------------------------------------------------ Debug Log #
    def _build_debug_log_tab(self, parent):
        """ArkAP debug log viewer - relocated here from the bottom of the
        Configuration tab (was under the Quick launch buttons). Same widgets,
        same handlers (_refresh_debug_log / _jump_debug_log_latest /
        _highlight_debug_log_search), just given a whole tab instead of a
        cramped LabelFrame."""
        wrap = ttk.Frame(parent, padding=(10, 8))
        wrap.pack(fill="both", expand=True)

        dbg = ttk.LabelFrame(wrap, text="ArkAP Debug Log", padding=(8, 6))
        dbg.pack(fill="both", expand=True)
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
        Tooltip(refresh_btn, "Re-read ArkAP_debug.log from disk - it changes while the "
                             "server runs, so this isn't automatic.")

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
        wrap = ttk.Frame(parent, padding=(10, 8))
        wrap.pack(fill="both", expand=True)

        toolbar = ttk.Frame(wrap)
        toolbar.pack(fill="x", pady=(0, 4))
        ttk.Button(toolbar, text="Expand all steps",
                   command=lambda: self._set_all_instruction_steps(False)).pack(side="left")
        ttk.Button(toolbar, text="Collapse all steps",
                   command=lambda: self._set_all_instruction_steps(True)
                   ).pack(side="left", padx=(6, 0))

        txt = tk.Text(wrap, wrap="word", font=("Segoe UI", 9), borderwidth=0,
                       highlightthickness=0, padx=10, pady=8, cursor="arrow",
                       background=self.theme["text_bg"], foreground=self.theme["text_fg"],
                       insertbackground=self.theme["text_fg"])
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        txt.tag_configure("h1", font=("Segoe UI", 12, "bold"), spacing1=10, spacing3=4)
        txt.tag_configure("body", font=("Segoe UI", 9), spacing3=2, lmargin1=2, lmargin2=2)
        txt.tag_configure("bullet", font=("Segoe UI", 9), spacing1=1, lmargin1=18, lmargin2=32)
        # Blank line between numbered steps - a small font keeps it a gap rather
        # than a full empty body line.
        txt.tag_configure("step_gap", font=("Segoe UI", 4))

        # (tag, text) pairs - kept short and skimmable, referencing this app's actual
        # tab/button/group names rather than a generic reprint of the GitHub README.
        content = [

            ("bullet", "Pro tip: most options have tooltips if you hover over them!"),
            ("bullet", "Pro tip: the Search bar (top left) searches field labels, tooltips, "
                       "and text across every tab - press Enter, then use Find Next / Find "
                       "Prev to jump between matches."),

            ("bullet", "Pro tip: each numbered step below has a checkbox. Tick it and the "
                       "step collapses to just \"Step 1\", \"Step 2\" and so on; untick it "
                       "to get the full text back. \"Collapse all steps\" / \"Expand all "
                       "steps\" at the top of this tab do the lot at once - handy for "
                       "ticking off steps as you go, or for skimming back to one step."),

            ("h1", "Start here - install in this order"),
            ("bullet", "The three installs below must happen in order: the ARK server "
                       "first, then ArkServerApi into it, then the ArkAP plugin into "
                       "ArkApi. Each one needs the previous one to already exist. All "
                       "three live on the Server Install tab."),

            ("bullet", "1. Server Install tab -> set SERVER_ROOT (the folder the server "
                       "gets installed into), then click \"Install ARK Server\". This "
                       "downloads ~18gb via SteamCMD - progress shows in the console box "
                       "below the buttons, and \"Cancel\" stops it if you need to. Note: "
                       "make sure your ARK: Survival Evolved game is on the preaquatica "
                       "branch, or you won't be able to join."),
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

            ("bullet", "2. Server install tab -> click \"Install ArkServerApi\". This downloads the "
                       "latest ArkApi release and extracts it into "
                       "ShooterGame\\Binaries\\Win64 for you - no manual unzipping. When "
                       "it's done, Win64 contains version.dll and an ArkApi\\ folder. "
                       "Note: BattlEye must be OFF for ArkApi to work, but "
                       "start_ase_server already disables it for you - We gotchu fam."),

            ("bullet", "3. Same tab -> in the \"Install ArkAP Plugin\" box, click \"Install "
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
                       "a checkmark before going further (you'll have an X for connector, we'll address this in step 6). Anything showing an X has a hint "
                       "telling you what to fix. This is the fastest way to catch a missed "
                       "step before you start troubleshooting in-game."),

            ("bullet", "6. Generate the Archipelago room. This guide won't explain how "
                       "YAMLs and Archipelago work - this isn't a beginner-friendly "
                       "Archipelago setup. Just remember to set up your yaml, remember your "
                       "yaml name, and drop the .apworld into Archipelago's custom worlds. "
                       "Note: it's recommended to have progression_tiers on in the yaml to "
                       "reduce softlocks/BKs."),

            ("bullet", "7. Configuration tab -> fill in the Connector settings (server, "
                       "slot, password) with your Archipelago room info. Your slot must "
                       "match the name in your yaml exactly, including capitalisation. "
                       "Copy the connection command - this is what you'll paste in-game."),

            ("bullet", "8. Quick Launch -> \"Run start_ase_server\" to launch the server. "
                       "It can take a few minutes depending on your SSD/HDD speed. Confirm "
                       "in the console that the plugin has loaded (or check the Debug Log "
                       "tab for the LOAD line)."),

            ("bullet", "9. In ARK: Survival Evolved, go to LAN and look for your session "
                       "name (default: ArchipelagoSolo). Join, spawn your character, open "
                       "in-game chat, and paste the connection command from step 7."),

            ("bullet", "10. You should be good to go! (if randomize dino spawns enabled see bottom of instuctions)Quick test: level up and see if a "
                       "check goes out. To test check-in: in the host's server console (the "
                       "ArchipelagoServer window, or the web room's command box) run "
                       "/send ARCHIPELAGONAME Engram: Canteen - within a few seconds it "
                       "should unlock in your engrams. If not, uh oh "),

            ("bullet", "Any issues: check the Debug Log tab first, then the Discord or "
                       "GitHub to search for or report them."),

            ("h1", "What each tab does"),
            ("bullet", "Configuration - every Locations / Paths / Network / Connector / "
                       "Cluster field, the Quick Launch buttons, and Save / Reload from "
                       "files. The Paths group also holds \"Scan intensity\" + \"Scan for "
                       "paths\" (all path detection in one button) and \"Create "
                       "ServerCluster folders\"."),
            ("bullet", "Server Install - the three installers, in order: \"Install ARK "
                       "Server\" (SteamCMD, ~18gb), \"Install ArkServerApi\" (downloads + "
                       "extracts the latest ArkApi into Win64), and \"Install Plugin\" "
                       "(downloads the latest ArkAP_Plugin.zip from GitHub and installs it "
                       "into ArkApi\\Plugins). \"Manual downloads\" at the bottom is only a "
                       "fallback if an automated download fails (or you want a specific "
                       "older plugin version), plus the ArkConnector, which still needs "
                       "downloading by hand."),
            ("bullet", "Setup Status - a read-only checklist (server installed / ArkApi "
                       "installed / plugin installed / plugin mode / connector.ini) with "
                       "hints for anything showing an X. Click Re-check after fixing "
                       "something. It also shows advisory rows (a yellow \"i\", not a red "
                       "X) for things that are worth knowing but aren't broken - the "
                       "BattlEye note, and \"update available\" when a newer ArkServerApi "
                       "or ArkAP plugin release exists than the one you installed (with a "
                       "link to the release). An older component version isn't a failure, "
                       "so it never shows an X."),
            ("bullet", "   The Setup Status tab has a small coloured symbol next to its "
                       "name in the tab bar, so you can see your overall status from any "
                       "tab without opening it: a green check = everything passes, a "
                       "yellow \"i\" = no failures but at least one advisory (BattlEye, or "
                       "a newer component version), a red X = at least one hard failure. "
                       "It updates whenever the checks re-run."),
            ("bullet", "Debug Log - live view of ArkAP_debug.log with a search box, "
                       "\"Jump to latest\", and \"Refresh\". Check here first when checks "
                       "or items aren't coming through."),
            ("bullet", "Profiles - save/load named snapshots of every Configuration field "
                       "(e.g. \"Solo Test\" vs \"Friend Group Run\") plus a free-text notes "
                       "box, stored separately from your live config. Loading a profile "
                       "only fills in the Configuration fields - it never saves/applies by "
                       "itself, so press Save on the Configuration tab afterward."),
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
            ("bullet", "Instructions - this tab."),

            ("h1", "Search (top left of the window)"),
            ("bullet", "Type a term and press Enter to search field labels, tooltips, "
                       "button text, and this Instructions tab across every tab at once."),
            ("bullet", "Find Next / Find Prev cycle through all matches, switching tabs "
                       "automatically and centering the match on screen."),

            ("h1", "Quick launch (bottom of the Configuration tab)"),
            ("bullet", "Open ipc folder / Open Plugins folder / Open Game.ini folder / "
                       "Open SERVER_ROOT / Open ClusterDir folder - open the matching "
                       "folder in Explorer."),
            ("bullet", "Run start_ase_server - launches the main ARK server."),
            ("bullet", "Run switch_map - swaps the active map (optionally backing up "
                       "first)."),
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
            ("bullet", "Connector fields write into connector.ini."),
            ("bullet", "Save only rewrites the one matching line for each field in each "
                       "file - everything else in the script is left untouched."),

            ("h1", "Reporting a problem (diagnostics & crash log)"),
            ("bullet", "Export diagnostics - a button next to Save / Reload on the "
                       "Configuration tab. It bundles ArkAP_debug.log, a text summary of "
                       "the Setup Status checks, a copy of your config with the passwords "
                       "removed (ADMINPASS, SERVERPASS and the connector password show as "
                       "[REDACTED] - everything else is kept so it's still useful), and "
                       "the crash log if there is one, into a single .zip. It saves to "
                       "your Desktop by default (you pick where) and opens the folder when "
                       "it's done. Drag that zip straight into Discord or attach it to a "
                       "GitHub issue when asking for help - it's the fastest way to get "
                       "diagnosed."),
            ("bullet", "Crash log - if the launcher ever hits an unexpected error it shows "
                       "a \"Something went wrong\" message and writes the full details to "
                       "arkap_launcher_crash.log, saved next to the launcher's .exe (the "
                       "same folder as arkap_launcher_config.json). It's kept across "
                       "restarts (new crashes are appended, with a size cap so it can't "
                       "grow forever), so it survives even if the app crashes again before "
                       "you get to report the first one. When reporting a crash, attach "
                       "that file - or just use \"Export diagnostics\", which already "
                       "includes it."),

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

        # The numbered install steps (1. .. 10.) each get their own collapse toggle: a
        # step starts at a "N. " bullet and swallows any indented ("   ...") bullet
        # lines that immediately follow it as its collapsible body. Everything else in
        # `content` (intro text, the later h1 sections) is plain, always-visible text -
        # grouping is inferred from the text itself so this keeps working if `content`
        # is edited later without anyone having to maintain a separate step list.
        # in_steps (rather than just "steps is non-empty") is what stops the LAST step
        # swallowing every indented bullet in the later h1 sections - those continue
        # their own preceding bullet, are nowhere near step 10, and being pulled into
        # it both hid them on collapse and printed them out of order.
        pre, steps, post = [], [], []
        in_steps = False
        for tag, line in content:
            if tag == "bullet" and re.match(r"^\d+\.\s", line):
                steps.append([(tag, line)])
                in_steps = True
            elif in_steps and tag == "bullet" and line.startswith("   "):
                steps[-1].append((tag, line))
            else:
                in_steps = False
                (post if steps else pre).append((tag, line))

        for tag, line in pre:
            txt.insert("end", line + "\n", tag)

        self._instruction_step_vars = {}
        self._instruction_step_label_vars = {}
        for i, step_lines in enumerate(steps):
            body_tag = "instr_step_body_%d" % i
            label_tag = "instr_step_label_%d" % i
            var = tk.BooleanVar(value=False)  # False = expanded (the required default)
            self._instruction_step_vars[body_tag] = var
            self._instruction_step_label_vars[label_tag] = var

            cb = ttk.Checkbutton(txt, variable=var)
            Tooltip(cb, "Collapse this step down to its number, or expand it again.")
            txt.window_create("end", window=cb, padx=4)
            title_tag, title_text = step_lines[0]
            # Two mutually-exclusive versions of the step's first line share the
            # checkbox's display line: the short "Step N" stub shown while
            # collapsed, and the real (long) title shown while expanded. Elide
            # covers each line's trailing newline too, so the hidden one takes up
            # no vertical space at all - which is the whole point: collapsing has
            # to leave "Step N" and nothing else.
            num = re.match(r"^(\d+)\.", title_text)
            label_text = "Step %s" % num.group(1) if num else title_text.split(".")[0]
            txt.insert("end", label_text + "\n", (title_tag, label_tag))
            txt.insert("end", title_text + "\n", (title_tag, body_tag))
            for body_line_tag, body_line_text in step_lines[1:]:
                txt.insert("end", body_line_text + "\n", (body_line_tag, body_tag))
            txt.tag_configure(body_tag, elide=False)
            txt.tag_configure(label_tag, elide=True)
            # Blank spacer line after every step so the steps read as separate
            # blocks in both states - kept outside both toggled tags so the gap
            # survives collapsing.
            txt.insert("end", "\n", "step_gap")

            def _make_toggle(v=var, bt=body_tag, lt=label_tag):
                def _toggle(*_a):
                    collapsed = v.get()
                    txt.tag_configure(bt, elide=collapsed)
                    txt.tag_configure(lt, elide=not collapsed)
                return _toggle
            var.trace_add("write", _make_toggle())

        for tag, line in post:
            txt.insert("end", line + "\n", tag)

        self.instructions_text = txt
        self._tag_instruction_examples()
        txt.configure(state="disabled")

    def _set_all_instruction_steps(self, collapsed):
        """Backs the toolbar's "Expand all steps" / "Collapse all steps" buttons."""
        for var in self._instruction_step_vars.values():
            var.set(collapsed)

    def _tag_instruction_examples(self):
        """Grey out the sample paths in the Instructions prose so they read as
        examples, not as paths this install actually uses - same colour as an
        empty field's placeholder. Re-run on theme toggle (see _retheme_widgets)
        because a Text tag's colour is fixed at configure time."""
        txt = self.instructions_text
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
                       self.instructions_text, self.profile_notes_text):
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

        try:
            self.save_btn_halo.configure(background=t["warn_bg"],
                                         highlightbackground=t["warn_border"])
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
    #      on both the Configuration and Server Install tabs, and before this was a
    #      list, typing into the Server Install one left _placeholder_active set, so
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
            # Loud, not "skipped": everything the user typed into the Connector group
            # goes nowhere in this case, and the old one-liner read like a harmless
            # note about an optional file.
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
            self.set(key, values.get(key, ""))
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
        cmd = "/connect %s %s" % (slot, server)
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
            if os.path.isfile(os.path.join(cand, ARK_EXE_RELPATH)):
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

        # PLUGINS_DIR is derived from SERVER_ROOT and is correct even before ArkApi
        # exists, so it is ALWAYS written back - this is the fix for it staying empty
        # after a successful SERVER_ROOT scan. Anything already set by the user is
        # left alone.
        for key in ("PLUGINS_DIR", "ipc_dir", "game_ini"):
            value = result.get(key) or ""
            if not value:
                continue
            current = self.get(key)
            if current and os.path.normcase(current) != os.path.normcase(value):
                skipped.append(key)
                continue
            self.set(key, value)
            filled.append(key)

        if filled:
            self._log("Scan for paths: filled in %s from SERVER_ROOT." % ", ".join(filled))
        if skipped:
            self._log("Scan for paths: left %s as you had already set them."
                      % ", ".join(skipped))
        if not result.get("plugins_exists"):
            self._log("Scan for paths: the ArkApi Plugins folder doesn't exist yet - "
                      "PLUGINS_DIR has been set to where it WILL be (%s). It's created "
                      "when you install ArkServerApi / the plugin."
                      % (result.get("PLUGINS_DIR") or "?"))
        if not result.get("ipc_dir"):
            self._log("Scan for paths: the ArkAP plugin isn't installed yet, so ipc_dir "
                      "was left alone (Server Install -> Install Plugin).")
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
        # always offered rather than applied. Only keys still empty are offered.
        suggestions = {k: v for k, v in (result.get("suggestions") or {}).items()
                       if v and not self.get(k)}
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
        cluster_dir = self.get("CLUSTERDIR")
        if not cluster_dir or cluster_dir == self._last_cluster_dir_scan:
            return
        self._scan_for_saves_and_backup_root(cluster_dir)

    def _scan_for_saves_and_backup_root(self, cluster_dir):
        """Given a found/confirmed CLUSTERDIR, look in its parent folder (the same
        ServerCluster-style parent CLUSTERDIR itself is a sibling of) for SAVESROOT
        (a sibling matching 'saves') and BACKUPROOT (a sibling matching 'backups').
        Folder-name matching is a guess here too, so these are always surfaced as
        suggestions to confirm - never silently filled in. BACKUPROOT especially may
        not exist yet (some setups only create it on the first backup), so when no
        matching sibling is found we still offer the expected sibling path as an
        unconfirmed placeholder rather than treating that as an error. Skips any key
        that's already set - nothing to suggest there."""
        if not cluster_dir or not os.path.isdir(cluster_dir):
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
        for key, pattern, default_name in (
            ("SAVESROOT", "saves", "Saves"),
            ("BACKUPROOT", "backups", "Backups"),
        ):
            if self.get(key):
                continue
            # classify_cluster_folder() has to agree with the name pattern: it's
            # the one place that knows which real-but-wrong ARK folders (SavedArks,
            # the Cluster-<Map> junctions, timestamped _backup_ snapshots) must
            # never be offered as a configured path, and CLUSTERDIR can sit close
            # enough to ShooterGame\Saved for those to turn up as siblings.
            matches = [p for p in siblings
                       if pattern in os.path.basename(p).lower()
                       and classify_cluster_folder(os.path.basename(p)) == key]
            suggestions[key] = matches if matches else [os.path.join(parent, default_name)]

        if suggestions:
            self._suggest_paths(suggestions)

    def _suggest_paths(self, suggestions):
        """One dialog offering every name-matched folder the scan turned up, grouped by
        the field it would fill. Suggestions are never applied without a click, since
        folder-name matching is a guess. A path that doesn't exist yet is offered as a
        greyed-out example (it's a suggested location, not a found folder)."""
        win = tk.Toplevel(self)
        win.title("Folder suggestions")
        win.resizable(False, False)
        win.transient(self)
        ttk.Label(win, padding=10, wraplength=520, justify="left",
                  text="The scan found folder(s) that look like they could be your "
                       "cluster folders. Pick one for each field to use it, or close "
                       "this to leave those fields as they are.").pack()
        for key in ("CLUSTERDIR", "SAVESROOT", "BACKUPROOT"):
            matches = suggestions.get(key)
            if not matches:
                continue
            ttk.Label(win, text=key, font=("Segoe UI", 9, "bold")
                      ).pack(anchor="w", padx=10, pady=(6, 0))
            for m in matches:
                exists = os.path.isdir(m)
                label = m if exists else "%s   (not created yet - suggested path)" % m
                # A path that exists was really found on disk; one that doesn't is
                # only an example of where it would go, so it's greyed out.
                btn = ttk.Button(win, text=label,
                                  style="TButton" if exists else "Placeholder.TButton")
                btn.configure(command=lambda p=m, k=key, b=btn: self._pick_suggested_path(k, p, b))
                btn.pack(fill="x", padx=10, pady=2)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(4, 10))

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

    def _any_install_running(self):
        """True if the SteamCMD, ArkServerApi, or ArkAP plugin install flow is active - they
        share the install_log/install_progress/install_status_var widgets, so only one
        may run at a time."""
        return ((self._install_thread is not None and self._install_thread.is_alive())
                or (self._arkapi_thread is not None and self._arkapi_thread.is_alive())
                or (self._plugin_thread is not None and self._plugin_thread.is_alive()))

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
            self._ensure_cluster_dirs(self.get("SERVER_ROOT"))
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
    # downloads unless the user clicks "Update Now" in that dialog. Checking for a new
    # version does also happen once, silently, on startup (see
    # _start_background_update_check) - it only lights up the "!" badge / button highlight
    # (see _apply_update_indicators), never opens the dialog itself. Both checks use this app's OWN release repo
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

    def _start_background_update_check(self):
        threading.Thread(target=self._background_update_check_worker, daemon=True).start()

    # ------------------------------------------- Component version advisories #
    def _read_config_dict(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _start_component_version_check(self):
        """Kick a background check of the installed ArkApi / plugin versions against their
        latest GitHub releases. Silent and non-blocking, same as the launcher self-check."""
        threading.Thread(target=self._component_version_check_worker, daemon=True).start()

    def _component_version_check_worker(self):
        """Build the advisory rows off the main thread. Only compares components whose
        installed version the launcher actually recorded at install time - with no baseline
        there's nothing to compare against, so it stays silent rather than guessing."""
        cfg = self._read_config_dict()
        advisories = []
        checks = [
            (cfg.get(ARKAPI_INSTALLED_VERSION_KEY, ""), ARKSERVERAPI_RELEASES_API,
             "ArkServerApi", ARKSERVERAPI_RELEASES_PAGE,
             "Server Install tab -> \"Install ArkServerApi\" upgrades it in place."),
            (cfg.get(PLUGIN_INSTALLED_VERSION_KEY, ""), ARKAP_PLUGIN_RELEASES_API,
             "ArkAP plugin", RELEASES_URL,
             "Server Install tab -> \"Install Plugin\" downloads and upgrades it in place "
             "- your ArkAP.config.json is kept."),
        ]
        for installed, api, name, page, how in checks:
            if not installed:
                continue
            latest = fetch_latest_release_tag(api)
            if latest and _version_is_newer(latest, installed):
                advisories.append({
                    "label": "%s update available (advisory)" % name,
                    "state": "info",
                    "detail": "Installed %s, latest release is %s. Being on an older "
                              "version isn't broken - just worth knowing. %s"
                              % (installed, latest, how),
                    "hint": "",
                    "link": page,
                })

        # The launcher's own version, via the SAME release-picking the self-update flow and
        # the tab-bar "!" badge use (_fetch_release_list + _pick_best_release) - no duplicated
        # comparison logic, so this row and the badge can never disagree. Coexists with the
        # badge: this is just the Setup Status surfacing of the same result. Unlike the
        # component rows above, the launcher always gets a row (green check when up to date,
        # yellow "i" when newer) - it's shown only when GitHub was reachable this run.
        try:
            best, version_str = _pick_best_release(self._fetch_release_list(), APP_VERSION)
        except (OSError, ValueError):
            best = version_str = None
            reachable = False
        else:
            reachable = True
        if reachable and best is not None:
            advisories.append({
                "label": "Launcher update available (advisory)",
                "state": "info",
                "detail": "Installed %s, latest release is %s. Being on an older version "
                          "isn't broken - just worth knowing. Use \"Check for Updates\" at "
                          "the top of the window to update in place."
                          % (APP_VERSION, version_str),
                "hint": "",
                "link": (best.get("html_url") or UPDATE_RELEASES_PAGE),
            })
        elif reachable:
            advisories.append({
                "label": "Launcher up to date",
                "state": "ok",
                "detail": "Running %s, the latest release." % APP_VERSION,
                "hint": "",
            })

        self.after(0, self._on_component_versions, advisories)

    def _on_component_versions(self, advisories):
        self._component_advisories = advisories
        # Re-render the Setup Status rows and the tab-bar symbol with the new advisories,
        # whether or not that tab is currently open.
        self._refresh_setup_status()

    def _background_update_check_worker(self):
        try:
            releases = self._fetch_release_list()
        except (OSError, ValueError):
            return
        _best, version_str = _pick_best_release(releases, APP_VERSION)
        # Silent check never acknowledges - it only reflects the current state, so a newer
        # installable release lights up both cues until the user actually clicks the button.
        self.after(0, self._apply_update_indicators, version_str or "")

    def _read_acknowledged_version(self):
        """Newest release the user has clicked 'Check for Updates' through to see, from the
        persistent config file (empty string if never / unreadable)."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return ""
        val = data.get(ACK_VERSION_KEY, "")
        return val if isinstance(val, str) else ""

    def _acknowledge_version(self, version_str):
        """Persist `version_str` as the newest release the user has now seen. Written to
        the config JSON (not profiles) so it survives restarts - see ACK_VERSION_KEY."""
        self._write_config_key(ACK_VERSION_KEY, version_str, "acknowledged update version")

    def _set_update_highlight(self, on):
        """Light up (or clear) the button's Save-style halo. Off = recoloured to the header
        bg so it blends in at the same size, never shifting the header layout."""
        self._update_highlight_on = on
        t = self.theme
        colour = t["warn_bg"] if on else t["bg"]
        border = t["warn_border"] if on else t["bg"]
        self.update_btn_halo.configure(background=colour, highlightbackground=border)

    def _apply_update_indicators(self, latest_version):
        """Set both cues from the live latest-release version: the persistent "!" badge
        (an update exists at all, vs installed APP_VERSION) and the dismissible button
        highlight (newer than what the user has acknowledged). See _compute_update_cues."""
        show_badge, show_highlight = _compute_update_cues(
            latest_version, APP_VERSION, self._read_acknowledged_version())
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
        try:
            releases = self._fetch_release_list()
            self.after(0, self._on_update_check_done, True, releases)
        except (OSError, ValueError) as exc:
            self.after(0, self._on_update_check_done, False, str(exc))

    def _on_update_check_done(self, ok, payload):
        self._update_check_thread = None
        self.update_check_btn.configure(state="normal", text="Check for Updates")
        if not ok:
            messagebox.showerror("ARKIpelago Launcher",
                                  "Could not check for updates:\n\n%s" % payload)
            return
        # payload is the releases list; pick the newest installable (folder-zip) release that
        # is newer than what's installed. None means we're already on (or ahead of) it.
        data, version_str = _pick_best_release(payload, APP_VERSION)
        if data is None:
            messagebox.showinfo(
                "ARKIpelago Launcher",
                "You're up to date.\n\nInstalled version: %s" % APP_VERSION)
            return
        _v, tag = _extract_release_version(data)
        html_url = data.get("html_url") or UPDATE_RELEASES_PAGE
        # The user clicked through to see this release: acknowledge it (persisted), then
        # recompute both cues. The highlight clears (this release is no longer newer than
        # what's acknowledged); the "!" badge stays lit while it's newer than APP_VERSION.
        self._acknowledge_version(version_str)
        self._apply_update_indicators(version_str)
        self._show_update_available_dialog(data, tag, html_url)

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

    def _show_update_available_dialog(self, data, tag, html_url):
        asset = _launcher_zip_asset(data)

        win = tk.Toplevel(self)
        win.title("Update available")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="A newer version of ARKIpelago Launcher is available.",
                  font=(self._header_font_family or "Segoe UI", 11, "bold")
                  ).pack(anchor="w")
        ttk.Label(frame, text="Installed: %s      Latest: %s" % (APP_VERSION, tag)
                  ).pack(anchor="w", pady=(2, 8))

        body = (data.get("body") or "").strip()
        if body:
            ttk.Label(frame, text="Release notes:").pack(anchor="w")
            shown = body if len(body) <= 4000 else body[:4000] + "\n..."
            notes = tk.Text(frame, width=64, height=10, wrap="word",
                             background=self.theme["text_bg"], foreground=self.theme["text_fg"])
            notes.insert("1.0", shown)
            notes.configure(state="disabled")
            notes.pack(fill="both", expand=True, pady=(2, 8))

        link = ttk.Label(frame, text=html_url, foreground=self.theme["status_info"],
                          cursor="hand2")
        link.pack(anchor="w", pady=(0, 10))
        link.bind("<Button-1>", lambda _e: webbrowser.open(html_url))

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x")
        if asset:
            ttk.Button(btn_row, text="Update Now",
                       command=lambda: self._confirm_and_start_update(win, data, asset, tag)
                       ).pack(side="left")
        else:
            ttk.Label(btn_row, text="No downloadable update package (.zip) was found on this "
                                     "release - update manually from the link above.",
                      foreground=self.theme["subtle_fg"], wraplength=340, justify="left"
                      ).pack(side="left")
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
    def _fetch_latest_plugin_release(self, q):
        """Query GitHub for the latest ArkAP plugin release and its ArkAP_Plugin.zip asset.

        Returns (tag, asset_name, download_url, size_bytes). Raises RuntimeError/OSError/
        ValueError on failure - the worker turns that into a log line + failure result."""
        req = urllib.request.Request(
            ARKAP_PLUGIN_RELEASES_API,
            headers={"User-Agent": GITHUB_API_USER_AGENT,
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
        data = next((r for r in releases or [] if isinstance(r, dict) and not r.get("draft")),
                     None)
        if not data:
            raise RuntimeError("No releases found (checked %s)." % ARKAP_PLUGIN_RELEASES_API)
        tag = data.get("tag_name") or "unknown"
        assets = data.get("assets") or []
        want = ARKAP_PLUGIN_ASSET_NAME.lower()
        asset = next((a for a in assets if (a.get("name") or "").lower() == want), None)
        if not asset:
            raise RuntimeError(
                "Latest release (%s) has no %s asset. Use \"Manual downloads\" below."
                % (tag, ARKAP_PLUGIN_ASSET_NAME))
        q.put(("line", "Latest release: %s - asset %s" % (tag, asset["name"])))
        return tag, asset["name"], asset["browser_download_url"], asset.get("size", 0)

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
                "dedicated server first (Server Install -> Install ARK Server)." % win64)
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
        """Resolve the ArkAP plugin folder (<...>\\ArkApi\\Plugins\\ArkAP), or None.

        Deliberately reuses the same path variables that already drive ipc_dir, the
        "Open Plugins folder" button, and the plugin-install target, so every reset /
        open / install action points at one folder rather than a parallel one:
          1. PLUGINS_DIR (the ArkApi Plugins folder) + \\ArkAP
          2. SERVER_ROOT-derived fixed subpath
          3. ipc_dir's parent (ipc_dir == <...>\\ArkAP\\ipc)
        """
        plugins = self.get("PLUGINS_DIR")
        if plugins:
            return os.path.normpath(os.path.join(plugins, "ArkAP"))
        root = self.get("SERVER_ROOT")
        if root:
            return os.path.normpath(os.path.join(
                root, "ShooterGame", "Binaries", "Win64", "ArkApi", "Plugins", "ArkAP"))
        ipc = self.get("ipc_dir")
        if ipc:
            return os.path.dirname(os.path.normpath(ipc))
        return None

    # ------------------------------------------------------ debug log ------ #
    def _arkap_debug_log_path(self):
        plugin_dir = self._arkap_plugin_dir()
        return os.path.join(plugin_dir, "ArkAP_debug.log") if plugin_dir else None

    def _refresh_debug_log(self):
        path = self._arkap_debug_log_path()
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError as exc:
                content = "(could not read log: %s)" % exc
        elif path:
            content = "(no log yet at %s)" % path
        else:
            content = "(set PLUGINS_DIR or SERVER_ROOT on the Configuration tab first)"

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
                "first (Server Install -> Install Plugin)." % plugin_dir)
            return

        if not self._reset_preflight("Reset AP data"):
            return
        msg = ("This clears ALL Archipelago tracking the plugin and connector generate "
               "(incoming items AND outgoing checks) in:\n\n%s\n\n"
               "  - plugin state / queues / logs\n"
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
            if widget is getattr(self, "instructions_text", None):
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
        """If `index` falls inside a collapsed instruction step's body, expand it -
        otherwise a search match hiding under an elided (collapsed) step would be
        "found" but never actually become visible when scrolled to."""
        for tag_name in widget.tag_names(index):
            var = self._instruction_step_vars.get(tag_name)
            if var is not None and var.get():
                var.set(False)
            # The "Step N" stub is the mirror case: it's hidden while the step is
            # expanded, so a match landing in it needs the step collapsed instead.
            var = getattr(self, "_instruction_step_label_vars", {}).get(tag_name)
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
