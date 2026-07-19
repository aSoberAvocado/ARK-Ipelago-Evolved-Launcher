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
  * reset_ark_test.bat uses different variable NAMES for the same paths:
        CLUSTER   == the GUI's CLUSTERDIR
        MAPSAVES  == the GUI's SAVESROOT
  * start_transfer_server.bat deliberately runs on its own SESSION / ports /
    MAXPLAYERS (a bridge alongside the main server), so the launcher only syncs the
    SHARED identity fields into it (SERVER_ROOT, ADMINPASS, SERVERPASS, CLUSTERID,
    CLUSTERDIR, SAVESROOT) and never touches its ports/session.
"""

import os
import re
import sys
import json
import time
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
    ("Locations", [
        ("connector_ini", "connector.ini file",                                "file"),
    ]),
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
]

# Hover tooltip text per field key: what it is, an example location, and any
# recommended values / gotchas worth knowing before editing it.
FIELD_HELP = {
    "connector_ini": (
        "The Archipelago connector's config file (read/written by ark_ap_connector.py).\n"
        "Example: E:\\ARK\\New folder\\ArkConnector\\connector.ini"
    ),
    "SERVER_ROOT": (
        "Root folder of your ARK dedicated server install. the folder that directly "
        "contains 'ShooterGame'.\n "
        "Example: E:\\ARK\\Server\\ARK Survival Evolved Dedicated Server\n"
        "Tip: ShooterGameServer.exe lives under "
        "ShooterGame\\Binaries\\Win64\\ inside this folder."
    ),
    "SAVESROOT": (
        "Folder where each map's world save + player profiles are kept, in their own "
        "subfolder, physically outside ShooterGame\\Saved.\n"
        "Example: E:\\ARK\\ServerCluster\\Saves\n"
        "Tip: a junction named Cluster-<Map> is created inside ShooterGame\\Saved "
        "pointing here automatically. don't edit that junction by hand."
    ),
    "CLUSTERDIR": (
        "Folder where pseudo-cluster transfer data (Obelisk uploads/downloads of "
        "items, dinos, characters) is stored - shared by every map using the same "
        "CLUSTERID.\n"
        "Example: E:\\ARK\\ServerCluster\\ClusterData"
    ),
    "BACKUPROOT": (
        "Folder switch_map.bat writes timestamped backups into (SAVESROOT + "
        "CLUSTERDIR) when you choose to back up before switching maps.\n"
        "Example: E:\\ARK\\ServerCluster\\Backups"
    ),
    "PLUGINS_DIR": (
        "Your ArkApi 'Plugins' folder - the ArkAP plugin installs as a subfolder of "
        "this (Plugins\\ArkAP).\n"
        "Example: E:\\ARK\\Server\\ShooterGame\\Binaries\\Win64\\ArkApi\\Plugins\n"
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
        "Example: E:\\ARK\\Server\\ShooterGame\\Binaries\\Win64\\ArkApi\\Plugins\\ArkAP\\ipc"
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
        "Example: E:\\ARK\\Server\\ShooterGame\\Saved\\Config\\WindowsServer\\Game.ini\n"
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
PLACEHOLDER_EXAMPLES = {
    "connector_ini": r"E:\ARK\ArkConnector\connector.ini",
    "SERVER_ROOT":   r"E:\ARK\Server",
    "SAVESROOT":     r"E:\ARK\ServerCluster\Saves",
    "CLUSTERDIR":    r"E:\ARK\ServerCluster\ClusterData",
    "BACKUPROOT":    r"E:\ARK\ServerCluster\Backups",
    "PLUGINS_DIR":   r"E:\ARK\Server\ShooterGame\Binaries\Win64\ArkApi\Plugins",
    "ipc_dir":       r"E:\ARK\Server\ShooterGame\Binaries\Win64\ArkApi\Plugins\ArkAP\ipc",
    "game_ini":      r"E:\ARK\Server\ShooterGame\Saved\Config\WindowsServer\Game.ini",
}

# For each .bat target: { gui_key: variable_name_in_that_file }.
# Note the deliberate name differences in reset_ark_test.bat.
BAT_TARGETS = {
    "start_ase_server.bat": {
        "SERVER_ROOT": "SERVER_ROOT", "MAP": "MAP", "SESSION": "SESSION",
        "MAXPLAYERS": "MAXPLAYERS", "GAMEPORT": "GAMEPORT", "QUERYPORT": "QUERYPORT",
        "RCONPORT": "RCONPORT", "ADMINPASS": "ADMINPASS", "SERVERPASS": "SERVERPASS",
        "CLUSTERID": "CLUSTERID", "CLUSTERDIR": "CLUSTERDIR", "SAVESROOT": "SAVESROOT",
        "TRIBUTEEXP": "TRIBUTEEXP",
    },
    "switch_map.bat": {
        "SERVER_ROOT": "SERVER_ROOT", "SAVESROOT": "SAVESROOT",
        "CLUSTERDIR": "CLUSTERDIR", "BACKUPROOT": "BACKUPROOT",
    },
    # Bridge server: only shared identity fields - never its own ports/session/maxplayers.
    "start_transfer_server.bat": {
        "SERVER_ROOT": "SERVER_ROOT", "ADMINPASS": "ADMINPASS", "SERVERPASS": "SERVERPASS",
        "CLUSTERID": "CLUSTERID", "CLUSTERDIR": "CLUSTERDIR", "SAVESROOT": "SAVESROOT",
    },
    # reset uses CLUSTER / MAPSAVES for the same conceptual paths.
    "reset_ark_test.bat": {
        "SERVER_ROOT": "SERVER_ROOT", "CLUSTERDIR": "CLUSTER", "SAVESROOT": "MAPSAVES",
    },
    "apply_server_config.bat": {
        "SERVER_ROOT": "SERVER_ROOT",
    },
}

# Prefill precedence when a field appears in several files (first hit wins).
PREFILL_ORDER = [
    "start_ase_server.bat",
    "switch_map.bat",
    "start_transfer_server.bat",
    "reset_ark_test.bat",
    "apply_server_config.bat",
]

# .bat files we expose Run buttons for.
RUN_BATS = [
    ("Run start_ase_server", "start_ase_server.bat"),
    ("Run switch_map",       "switch_map.bat"),
    ("Run reset_ark_test",   "reset_ark_test.bat"),
    ("Run apply_server_config", "apply_server_config.bat"),
]

CONFIG_FILENAME = "arkap_launcher_config.json"

# Separate from CONFIG_FILENAME on purpose - saving/loading/deleting a named profile
# (Profiles tab) must never read or write the single active-config JSON above.
PROFILES_FILENAME = "arkap_launcher_profiles.json"

# JSON key that persists the "don't show again" choice for the install reminder banner.
REMINDER_HIDE_KEY = "hide_install_reminder"

# JSON key that persists the last-used plugin SOURCE folder (the unzipped ArkAP_plugin
# download the "Install Plugin" button copies FROM). Not a self.vars field - stored/read
# directly on the config JSON, like REMINDER_HIDE_KEY.
PLUGIN_SRC_KEY = "plugin_src_dir"

# --- Appearance / theming --------------------------------------------------- #
# JSON key that persists the light/dark choice - read/written directly like
# REMINDER_HIDE_KEY/PLUGIN_SRC_KEY above (see _read_theme_pref/_write_theme_pref)
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
    "start_ase_server.bat",
    "switch_map.bat",
    "start_transfer_server.bat",
    "reset_ark_test.bat",
    "apply_server_config.bat",
    "apply_server_config.ps1",
    os.path.join("serverconfig", "Game.ini.settings"),
    os.path.join("serverconfig", "GameUserSettings.ini.settings"),
]

# Folder name (under base_dir()) the bundled scripts are extracted into at runtime.
WORKING_SCRIPTS_DIRNAME = "ArkServerScripts"

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
# GitHub's API 403s anonymous requests with no User-Agent header - any non-empty value works.
GITHUB_API_USER_AGENT = "ArkAPLauncher"

# --- Launcher self-update (this exe's own releases, separate repo from the plugin/
# connector bundle above) --- #
APP_VERSION = "0.3.0"
UPDATE_REPO = "aSoberAvocado/ARK-Ipelago-Evolved-Launcher"
UPDATE_RELEASES_API = "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO
UPDATE_RELEASES_PAGE = "https://github.com/%s/releases" % UPDATE_REPO
# Written by the generated update-helper .bat into base_dir() right before it exits, so the
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


_RELEASE_VERSION_RE = re.compile(r"\d+(?:\.\d+){1,3}")


def _extract_release_version(data):
    """Best-effort (version_str, display_str) for a GitHub release JSON payload.

    Prefers tag_name (the normal convention - what _pick_update_asset/messages should
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


def extract_bundled_scripts():
    """Copy any bundled script that isn't already present into working_scripts_dir().

    Missing-only so a user's already-personalised scripts (Save rewrites their
    `set "VAR=..."` lines) are never clobbered on a later launch, while a fresh
    install still gets a full working set. Returns (dest_dir, [extracted_relpaths],
    [errors])."""
    src_root = bundled_scripts_dir()
    dst_root = working_scripts_dir()
    extracted, errors = [], []
    for rel in BUNDLED_SCRIPTS:
        src = os.path.join(src_root, rel)
        dst = os.path.join(dst_root, rel)
        if os.path.isfile(dst):
            continue
        if not os.path.isfile(src):
            # Bundle is incomplete (e.g. a lightweight/dev build) - not fatal, the
            # matching Run/Save just reports the script missing later.
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            extracted.append(rel)
        except OSError as exc:
            errors.append("%s: %s" % (rel, exc))
    return dst_root, extracted, errors


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
#   2. A scoped "given SERVER_ROOT, what else lives under/next to it" scan - only ever
#      touches a handful of fixed subpaths plus one single-level directory listing of
#      SERVER_ROOT's parent, so it's fast regardless of how large the ARK install/saves
#      are and safe to run synchronously on the UI thread (see _scoped_scan).

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
                        stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return None


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


# --------------------------------------------------------------------------- #
#  Hover tooltip
# --------------------------------------------------------------------------- #

class Tooltip:
    """Simple delayed hover tooltip attached to a single widget."""

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
        tk.Label(self._win, text=self.text, justify="left",
                 background=CURRENT_THEME["tooltip_bg"],
                 foreground=CURRENT_THEME["tooltip_fg"], relief="solid", borderwidth=1,
                 wraplength=self.wraplength, font=("Segoe UI", 9),
                 padx=6, pady=4).pack()

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
        self._last_scoped_scan_root = None
        self._last_cluster_dir_scan = None
        # Scripts folder is no longer a user field - it's the working folder next to the
        # launcher that bundled scripts are extracted into (set in _discover_locations).
        self._scripts_dir = working_scripts_dir()
        # Plugin SOURCE folder (unzipped ArkAP_plugin download) the Install Plugin button
        # copies FROM - remembered across runs via the config JSON (PLUGIN_SRC_KEY).
        self._plugin_src_dir = ""
        self.backup_var = tk.BooleanVar(value=True)
        self._logo_img = None     # keep a reference so Tk doesn't garbage-collect it

        # Profiles tab state - named snapshots of every Configuration field + notes,
        # persisted separately from CONFIG_FILENAME (see PROFILES_FILENAME).
        self.profiles_path = os.path.join(base_dir(), PROFILES_FILENAME)
        self._profiles = {}             # name -> {"values": {key: str, ...}, "notes": str}
        self._loaded_profile_name = None
        self._loaded_profile_values = None  # snapshot of Configuration values as loaded
        self._loaded_profile_notes = None   # notes text as loaded

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
        self._hide_install_reminder = self._read_hide_reminder_flag()

        # Broad SERVER_ROOT auto-detect state (Steam libraries / common drive roots).
        self._detect_queue = queue.Queue()
        self._detect_thread = None
        self._detect_cancelled = False

        # Launcher self-update state - see "Launcher self-update" section below.
        # Never touched on startup: the "Check for Updates" button is the only trigger,
        # so no network call happens until the user asks for one.
        self._update_check_thread = None
        self._update_download_thread = None
        self._update_download_queue = queue.Queue()
        self._update_progress_win = None

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
        self._plugin_src_dir = saved.get(PLUGIN_SRC_KEY, "") or ""
        if hasattr(self, "_plugin_src_var"):
            self._plugin_src_var.set(self._plugin_src_dir)
        self._discover_locations(saved)
        self.load_from_files(initial=True, saved=saved)
        self._apply_path_placeholders()
        self._refresh_setup_status()
        self._refresh_debug_log()
        self._profiles = self._load_profiles()
        self._refresh_profile_list()
        self._update_profile_status()

        if self._is_first_launch and not self.get("SERVER_ROOT"):
            self._start_auto_detect()

        # Local file read only (no network) - reports the outcome of an update helper
        # that ran just before this process started, if there was one.
        self._check_previous_update_result()

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

        self.update_check_btn = ttk.Button(header_row, text="Check for Updates",
                                            command=self._on_check_for_updates)
        self.update_check_btn.pack(side="right", padx=(0, 8))
        Tooltip(self.update_check_btn,
                "Check GitHub for a newer launcher release (current version: %s). "
                "Only runs when you click this - never automatically." % APP_VERSION)

        header_font_family = self._register_header_font()
        title_row = ttk.Frame(header_row)
        title_row.pack(side="left", fill="x", expand=True)
        ttk.Label(title_row, text="ARKIpelago Launcher",
                  font=(header_font_family, 16, "bold")).pack(side="left")
        ttk.Label(title_row, text="make sure to save!",
                  foreground=self.theme["subtle_fg"]).pack(side="left", padx=12)

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

        def _on_wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)

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
                      "tab (SteamCMD) to install it before relying on the paths below."
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
                self.detect_btn = ttk.Button(toolrow, text="Auto-detect...",
                                              command=self._start_auto_detect)
                self.detect_btn.pack(side="left")
                self.detect_status_label = ttk.Label(toolrow, text="",
                                                       foreground=self.theme["subtle_fg"])
                self.detect_status_label.pack(side="left", padx=8)
                Tooltip(self.detect_btn,
                        "Best-effort scan of common ARK server install locations (Steam "
                        "libraries, common drive roots) to find SERVER_ROOT automatically. "
                        "Runs in the background and may take a few seconds.\n"
                        "Once SERVER_ROOT is found/entered, leaving that field also "
                        "auto-scans inside it for the Plugins/ipc/Game.ini folders.")

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
                if key in PLACEHOLDER_EXAMPLES and kind in ("folder", "file"):
                    self._register_placeholder(key, entry_widget, PLACEHOLDER_EXAMPLES[key])
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
        ]:
            ttk.Button(qrow, text=text, command=cmd).pack(side="left", padx=3, pady=2)

        rrow = ttk.Frame(q)
        rrow.pack(fill="x")
        for text, batname in RUN_BATS:
            ttk.Button(rrow, text=text,
                       command=lambda b=batname: self.run_bat(b)
                       ).pack(side="left", padx=3, pady=2)

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
        ttk.Button(act, text="Save", command=self.on_save).pack(side="right", padx=3)
        ttk.Button(act, text="Reload from files",
                   command=lambda: self.load_from_files()).pack(side="right", padx=3)

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
                  text="Installs the ArkAP plugin into "
                       "<SERVER_ROOT>\\ShooterGame\\Binaries\\Win64\\ArkApi\\Plugins\\ArkAP. "
                       "ArkApi must already be installed in Win64 first. Point the source "
                       "at your unzipped ArkAP_plugin folder (the one containing "
                       "ArkAP\\ArkAP.dll); an existing ArkAP.config.json is kept."
                  ).pack(anchor="w")
        srcrow = ttk.Frame(plug)
        srcrow.pack(fill="x", pady=(4, 0))
        srcrow.columnconfigure(0, weight=1)
        ttk.Label(srcrow, text="Plugin source folder (unzipped ArkAP_plugin):").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self._plugin_src_var = tk.StringVar(value=self._plugin_src_dir)
        src_entry = ttk.Entry(srcrow, textvariable=self._plugin_src_var)
        src_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(srcrow, text="Browse...", width=10,
                   command=self._browse_plugin_src).grid(row=1, column=1, sticky="e")
        Tooltip(src_entry,
                "The folder you unzipped ArkAP_plugin.zip into - it must contain an "
                "ArkAP\\ subfolder with ArkAP.dll. Left blank, the launcher auto-detects "
                "it next to the launcher / in Downloads when you click Install Plugin.")
        pbtnrow = ttk.Frame(plug)
        pbtnrow.pack(fill="x", pady=(6, 0))
        self.install_plugin_btn = ttk.Button(pbtnrow, text="Install Plugin",
                                              command=self.on_install_plugin)
        self.install_plugin_btn.pack(side="left", padx=3, pady=2)
        Tooltip(self.install_plugin_btn,
                "Copy the plugin files into the ArkApi Plugins folder under SERVER_ROOT. "
                "Requires SERVER_ROOT set and ArkApi already installed in Win64.")

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
        ttk.Button(top, text="Re-check", command=self._refresh_setup_status
                   ).pack(side="right")

        ttk.Label(wrap, foreground=self.theme["subtle_fg"], wraplength=640, justify="left",
                  text="Read-only check of common setup steps, based on the current "
                       "Configuration tab paths and the files on disk. Nothing here is "
                       "changed automatically - use Configuration / Server Install to fix "
                       "a ✗."
                  ).pack(anchor="w", pady=(4, 8))

        items_wrap = ttk.Frame(wrap)
        items_wrap.pack(fill="both", expand=True)
        self.status_items_frame = ttk.Frame(items_wrap)
        self.status_items_frame.pack(fill="both", expand=True, anchor="n")

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

        # BattlEye is only ever turned off via the -NoBattlEye launch flag inside
        # start_ase_server.bat - there's no persisted file/registry setting to check
        # once the server isn't running, so this is deliberately informational rather
        # than a real pass/fail (a "silent" check here would just be guessing).
        items.append({
            "label": "BattlEye disabled",
            "state": "info",
            "detail": "Not a persisted setting, so this can't be checked directly.",
            "hint": "Set automatically by start_ase_server.bat (-NoBattlEye) every time "
                    "you launch via Quick Launch - we gotchu fam",
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

        ok, detail = check_connector_filled(self.get("connector_ini"))
        items.append({
            "label": "connector.ini filled in (server / slot / ipc_dir)",
            "state": "ok" if ok else "fail",
            "detail": detail,
            "hint": "Fill in server / slot / ipc_dir in the Connector group on the "
                    "Configuration tab, then Save.",
        })

        return items

    def _refresh_setup_status(self):
        for child in self.status_items_frame.winfo_children():
            child.destroy()
        state_colors = {
            "ok": self.theme["status_ok"],
            "fail": self.theme["status_fail"],
            "info": self.theme["status_info"],
        }
        for item in self._gather_setup_status():
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

    def _on_tab_changed(self, _event=None):
        try:
            current = self.notebook.select()
        except tk.TclError:
            return
        if current == str(self.tab_status):
            self._refresh_setup_status()
        elif current == str(self.tab_profiles):
            self._update_profile_status()

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

        # (tag, text) pairs - kept short and skimmable, referencing this app's actual
        # tab/button/group names rather than a generic reprint of the GitHub README.
        content = [

            ("bullet", "Pro tip: most options have tooltips if you hover over them!"),
            ("bullet", "Pro tip: the Search bar (top left) searches field labels, tooltips, "
                       "and text across every tab - press Enter, then use Find Next / Find "
                       "Prev to jump between matches."),

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

            ("bullet", "2. Same tab -> click \"Install ArkServerApi\". This downloads the "
                       "latest ArkApi release and extracts it into "
                       "ShooterGame\\Binaries\\Win64 for you - no manual unzipping. When "
                       "it's done, Win64 contains version.dll and an ArkApi\\ folder. "
                       "Note: BattlEye must be OFF for ArkApi to work, but "
                       "start_ase_server already disables it for you - nothing to do."),

            ("bullet", "3. Download ArkAP_plugin.zip (see \"Manual downloads\" at the "
                       "bottom of the Server Install tab) and unzip it somewhere. Then in "
                       "the \"Install ArkAP Plugin\" box, point \"Plugin source folder\" at "
                       "the unzipped folder (the one containing ArkAP\\ArkAP.dll) and click "
                       "\"Install Plugin\". It copies the plugin into "
                       "Win64\\ArkApi\\Plugins\\ArkAP for you. Leave the source box blank "
                       "and it will try to auto-find the download next to the launcher or "
                       "in your Downloads folder. Upgrading later keeps your existing "
                       "ArkAP.config.json."),

            ("bullet", "4. Configuration tab -> click \"Auto-detect...\", or Browse to set "
                       "paths by hand. Note: SERVER_ROOT is the folder that CONTAINS "
                       "ShooterGame, not the folder above it. e.g. "
                       "E:\\ARK\\Server\\ARK Survival Evolved Dedicated Server, NOT "
                       "E:\\ARK\\Server."),
            ("bullet", "   Leaving the SERVER_ROOT field runs a scan that fills in "
                       "PLUGINS_DIR / ipc_dir / game_ini, and may suggest CLUSTERDIR / "
                       "SAVESROOT / BACKUPROOT. These are typically correct - it's "
                       "recommended to accept them."),

            ("bullet", "5. Setup Status tab -> click Re-check and confirm everything shows "
                       "a checkmark before going further. Anything showing an X has a hint "
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

            ("bullet", "10. You should be good to go! Quick test: level up and see if a "
                       "check goes out. To test check-in: in the host's server console (the "
                       "ArchipelagoServer window, or the web room's command box) run "
                       "/send ARCHIPELAGONAME Engram: Canteen - within a few seconds it "
                       "should unlock in your engrams. If not, uh oh "),

            ("bullet", "Any issues: check the Debug Log tab first, then the Discord or "
                       "GitHub to search for or report them."),

            ("h1", "What each tab does"),
            ("bullet", "Configuration - every Locations / Paths / Network / Connector / "
                       "Cluster field, the Quick Launch buttons, and Save / Reload from "
                       "files."),
            ("bullet", "Server Install - the three installers, in order: \"Install ARK "
                       "Server\" (SteamCMD, ~18gb), \"Install ArkServerApi\" (downloads + "
                       "extracts the latest ArkApi into Win64), and \"Install Plugin\" "
                       "(copies the ArkAP plugin into ArkApi\\Plugins). \"Manual "
                       "downloads\" at the bottom is only a fallback if something goes "
                       "wrong, plus the ArkAP plugin zip and ArkConnector, which still "
                       "need downloading by hand."),
            ("bullet", "Setup Status - a read-only checklist (server installed / ArkApi "
                       "installed / plugin installed / plugin mode / connector.ini) with "
                       "hints for anything showing an X. Click Re-check after fixing "
                       "something."),
            ("bullet", "Debug Log - live view of ArkAP_debug.log with a search box, "
                       "\"Jump to latest\", and \"Refresh\". Check here first when checks "
                       "or items aren't coming through."),
            ("bullet", "Profiles - save/load named snapshots of every Configuration field "
                       "(e.g. \"Solo Test\" vs \"Friend Group Run\") plus a free-text notes "
                       "box, stored separately from your live config. Loading a profile "
                       "only fills in the Configuration fields - it never saves/applies by "
                       "itself, so press Save on the Configuration tab afterward."),
            ("bullet", "Instructions - this tab."),

            ("h1", "Search (top left of the window)"),
            ("bullet", "Type a term and press Enter to search field labels, tooltips, "
                       "button text, and this Instructions tab across every tab at once."),
            ("bullet", "Find Next / Find Prev cycle through all matches, switching tabs "
                       "automatically and centering the match on screen."),

            ("h1", "Quick launch (bottom of the Configuration tab)"),
            ("bullet", "Open ipc folder / Open Plugins folder / Open Game.ini folder / "
                       "Open SERVER_ROOT - open the matching folder in Explorer."),
            ("bullet", "Run start_ase_server - launches the main ARK server."),
            ("bullet", "Run switch_map - swaps the active map (optionally backing up "
                       "first)."),
            ("bullet", "Run reset_ark_test - wipes the test cluster/map save data."),
            ("bullet", "Reset AP data (keep world save) - deletes every Archipelago "
                       "tracking file the plugin and connector generate (both incoming "
                       "items AND outgoing checks). Note: if the character/world isn't "
                       "also reset, level/inventory checks re-send immediately."),
            ("bullet", "Full reset for new seed - does the above AND backs up + wipes the "
                       "world save. Use this when joining a new seed. Stop the ARK server "
                       "(and the connector) first."),
            ("bullet", "Run apply_server_config - re-applies the saved config to the "
                       "install."),

            ("h1", "What the path fields feed"),
            ("bullet", "Paths / Network / Cluster fields write into start_ase_server.bat, "
                       "switch_map.bat, start_transfer_server.bat, reset_ark_test.bat, "
                       "and apply_server_config.bat."),
            ("bullet", "Connector fields write into connector.ini."),
            ("bullet", "Save only rewrites the one matching line for each field in each "
                       "file - everything else in the script is left untouched."),

            ("h1", "Other Information"),
            ("bullet", "If you want to restart your world for a new Archipelago seed, "
                       "click \"Full reset for new seed\" under Quick Launch (stop the ARK "
                       "server and the connector first)."),
            ("bullet", "If you randomized dinos, find the txt file for it by opening the "
                       "ipc folder under Quick Launch: game_ini_fragment.txt then paste it at "
                       "the top of your Game.ini file."),
        
         
        ]
        for tag, line in content:
            txt.insert("end", line + "\n", tag)

        txt.configure(state="disabled")
        self.instructions_text = txt

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

    def _write_theme_pref(self, name):
        try:
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            data[THEME_KEY] = name
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            self._log("! Could not save theme preference: %s" % exc)

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

        self._default_fg = style.lookup("TEntry", "foreground") or t["fg"]
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
        entry = self._entries.get(key)
        if key in self._placeholder_text:
            self._placeholder_active[key] = False
            if entry is not None:
                entry.configure(foreground=self._default_fg)
        self.vars[key].set(value)
        if not value and key in self._placeholder_text:
            self._show_placeholder(key)

    # ------------------------------------------------------- placeholders --- #
    def _register_placeholder(self, key, entry, example_text):
        """Wire up a path Entry to show greyed-out example text while empty."""
        self._placeholder_text[key] = example_text
        entry.bind("<FocusIn>", lambda _e, k=key: self._on_placeholder_focus_in(k), add="+")
        entry.bind("<FocusOut>", lambda _e, k=key: self._on_placeholder_focus_out(k), add="+")

    def _on_placeholder_focus_in(self, key):
        if self._placeholder_active.get(key):
            self._placeholder_active[key] = False
            self.vars[key].set("")
            entry = self._entries.get(key)
            if entry is not None:
                entry.configure(foreground=self._default_fg)

    def _on_placeholder_focus_out(self, key):
        if not self.vars[key].get().strip():
            self._show_placeholder(key)

    def _show_placeholder(self, key):
        text = self._placeholder_text.get(key)
        if not text:
            return
        self._placeholder_active[key] = True
        self.vars[key].set(text)
        entry = self._entries.get(key)
        if entry is not None:
            entry.configure(foreground=self.theme["entry_placeholder_fg"])

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
        parent = os.path.dirname(b)

        dst_root, extracted, errors = extract_bundled_scripts()
        self._scripts_dir = os.path.normpath(dst_root)
        self._scripts_extracted = extracted
        self._scripts_extract_errors = errors

        # connector.ini: first existing candidate.
        ini = saved.get("connector_ini", "")
        if not ini or not os.path.isfile(ini):
            cand_ini = [
                os.path.join(b, "connector.ini"),
                os.path.join(b, "ArkConnector", "connector.ini"),
                os.path.join(parent, "ArkConnector", "connector.ini"),
                os.path.join(cwd, "ArkConnector", "connector.ini"),
            ]
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
                self.set(key, value)
        return data

    def load_from_files(self, initial=False, saved=None):
        """Pre-fill the mapped GUI fields from whichever real files exist."""
        if not initial:
            self._clear_log()
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
                    self.set(gui_key, val)
                    seen.add(gui_key)

        # --- connector.ini ---
        ini_path = self.get("connector_ini")
        ini_found = ini_path and os.path.isfile(ini_path)
        if ini_found:
            for k, v in ini_read_values(ini_path).items():
                self.set(k, v)

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
        if getattr(self, "_scripts_extract_errors", None):
            for err in self._scripts_extract_errors:
                self._log("  ! Could not extract %s" % err)
        if not found_any:
            self._log("  No .bat files found there yet - fields left as-is.")
        self._log("  connector.ini: %s" % (ini_path if ini_found else "(not found)"))

    def collect_values(self):
        values = {key: self.get(key) for key in self.vars}
        values[REMINDER_HIDE_KEY] = self._hide_install_reminder
        if hasattr(self, "_plugin_src_var"):
            self._plugin_src_dir = self._plugin_src_var.get().strip()
        values[PLUGIN_SRC_KEY] = self._plugin_src_dir
        return values

    def _save_json(self, values):
        try:
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
        else:
            self._log("! Could not write JSON: %s" % info)

        # 2) .bat targets
        scripts = self._scripts_dir
        if not scripts or not os.path.isdir(scripts):
            self._log("! Scripts folder not found - skipped all .bat files.")
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
        changed, missing = [], []
        for gui_key, var in field_map.items():
            new_text, found = bat_write_var(text, var, values.get(gui_key, ""))
            if not found:
                missing.append(var)
                continue
            if new_text != text:
                changed.append(var)
            text = new_text
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
            self._log("connector.ini: not found - skipped.")
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
        connector settings). Deliberately does NOT include REMINDER_HIDE_KEY /
        PLUGIN_SRC_KEY - those are launcher preferences, not part of a server config."""
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

    def _save_profiles(self):
        try:
            with open(self.profiles_path, "w", encoding="utf-8") as f:
                json.dump({"profiles": self._profiles}, f, indent=2)
            return True
        except OSError as exc:
            self._log("! Could not save profiles: %s" % exc)
            messagebox.showerror("ARKIpelago Launcher", "Could not save profiles:\n%s" % exc)
            return False

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

    def _on_load_profile(self):
        name = self.profile_select_var.get()
        if not name or name not in self._profiles:
            messagebox.showwarning("ARKIpelago Launcher", "Select a profile to load first.")
            return
        profile = self._profiles[name]
        values = profile.get("values", {})
        for key in self.vars:
            self.set(key, values.get(key, ""))
        notes = profile.get("notes", "")
        self.profile_notes_text.delete("1.0", "end")
        self.profile_notes_text.insert("1.0", notes)
        self.profile_notes_text.edit_modified(False)
        self._loaded_profile_name = name
        self._loaded_profile_values = self._current_profile_snapshot()
        self._loaded_profile_notes = notes
        self._update_profile_status()
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
        self._loaded_profile_name = name
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
        self._loaded_profile_name = name
        self._loaded_profile_values = dict(self._profiles[name]["values"])
        self._loaded_profile_notes = self._profiles[name]["notes"]
        self._update_profile_status()
        self._log("Updated profile \"%s\"." % name)

    def _on_rename_profile(self):
        name = self.profile_select_var.get()
        if not name or name not in self._profiles:
            messagebox.showwarning("ARKIpelago Launcher", "Select a profile to rename first.")
            return
        new_name = simpledialog.askstring("Rename profile", "New name:",
                                           initialvalue=name, parent=self)
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name or new_name == name:
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
            self._loaded_profile_name = new_name
        self._refresh_profile_list(select_name=new_name)
        self._update_profile_status()
        self._log("Renamed profile \"%s\" -> \"%s\"." % (name, new_name))

    def _on_delete_profile(self):
        name = self.profile_select_var.get()
        if not name or name not in self._profiles:
            messagebox.showwarning("ARKIpelago Launcher", "Select a profile to delete first.")
            return
        if not messagebox.askyesno("ARKIpelago Launcher",
                                    "Delete profile \"%s\"? This cannot be undone." % name):
            return
        removed = self._profiles.pop(name)
        if not self._save_profiles():
            self._profiles[name] = removed  # roll back
            return
        if self._loaded_profile_name == name:
            self._loaded_profile_name = None
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

    # ------------------------------------ SERVER_ROOT auto-detect (broad) -- #
    def _start_auto_detect(self):
        if self._detect_thread is not None and self._detect_thread.is_alive():
            return
        self._detect_cancelled = False
        self._detect_queue = queue.Queue()
        self.detect_btn.configure(state="disabled")
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
        self.detect_btn.configure(state="normal")
        self._detect_thread = None
        if found:
            found = os.path.normpath(found)
            self.detect_status_label.configure(text="Found: %s" % found)
            self._log("Auto-detect found SERVER_ROOT: %s" % found)
            self.set("SERVER_ROOT", found)
            self._scoped_scan()
        else:
            self.detect_status_label.configure(
                text="No ARK server install found automatically - enter SERVER_ROOT manually.")
            self._log("Auto-detect: no ARK server install found in common locations.")

    # ------------------------------ SERVER_ROOT-scoped scan ("Scan for paths") - #
    def _on_server_root_focus_out(self, _event=None):
        root = self.get("SERVER_ROOT")
        if not root or root == self._last_scoped_scan_root:
            return
        self._scoped_scan()

    def _scoped_scan(self):
        """Given SERVER_ROOT, fill in PLUGINS_DIR / ipc_dir / game_ini and suggest
        CLUSTERDIR. Every check here is either a fixed-path os.path.isfile/isdir stat or a
        single-level os.scandir of SERVER_ROOT's parent - never a recursive walk - so this
        is fast and safe to run synchronously on the UI thread regardless of how large the
        ARK install/saves under SERVER_ROOT are."""
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

        found = []
        win64 = os.path.join(root, "ShooterGame", "Binaries", "Win64")
        arkapi = os.path.join(win64, "ArkApi")
        plugins = os.path.join(arkapi, "Plugins")
        arkap = os.path.join(plugins, "ArkAP")
        if os.path.isdir(plugins):
            self.set("PLUGINS_DIR", plugins)
            found.append("PLUGINS_DIR")
            # Only treat the plugin as actually installed if ArkAP.dll is present (an empty
            # ArkAP\ folder doesn't count). ipc\ isn't created until the plugin's first run,
            # so fill ipc_dir from the confirmed plugin folder rather than requiring ipc\.
            if os.path.isfile(os.path.join(arkap, "ArkAP.dll")):
                self.set("ipc_dir", os.path.join(arkap, "ipc"))
                found.append("ipc_dir")
                self._log("Scan for paths: ArkAP plugin detected (ArkAP.dll) at %s." % arkap)
            elif os.path.isdir(arkap):
                self._log("Scan for paths: found an ArkAP\\ folder at %s but no ArkAP.dll "
                          "- plugin not fully installed (use Server Install -> Install "
                          "Plugin)." % arkap)
            else:
                self._log("Scan for paths: ArkApi Plugins folder found, but the ArkAP "
                          "plugin isn't installed yet (Server Install -> Install Plugin).")
        elif os.path.isdir(arkapi):
            self._log("Scan for paths: ArkApi is installed but has no Plugins folder yet "
                      "- it will be created when you install the plugin.")

        game_ini = os.path.join(root, "ShooterGame", "Saved", "Config", "WindowsServer",
                                 "Game.ini")
        if os.path.isfile(game_ini):
            self.set("game_ini", game_ini)
            found.append("game_ini")

        if found:
            self._log("Scan for paths: filled in %s from SERVER_ROOT." % ", ".join(found))
        else:
            self._log("Scan for paths: SERVER_ROOT is valid, but ArkApi/Game.ini weren't "
                       "found yet (install ArkApi first, or start the server once to "
                       "generate Game.ini).")

        # CLUSTERDIR is a sibling of SERVER_ROOT, not under it - a guess, so surface it as
        # a suggestion to confirm rather than silently filling it in. Skip if CLUSTERDIR
        # is already set (from a real file or a previous pick) - nothing to suggest.
        parent = os.path.dirname(root)
        matches = []
        if not self.get("CLUSTERDIR") and parent and os.path.isdir(parent):
            try:
                with os.scandir(parent) as it:
                    for entry in it:
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        if is_dir and "cluster" in entry.name.lower():
                            matches.append(entry.path)
            except OSError:
                pass
        if matches:
            self._suggest_cluster_dir(matches)

    def _suggest_cluster_dir(self, matches):
        win = tk.Toplevel(self)
        win.title("CLUSTERDIR suggestion")
        win.resizable(False, False)
        win.transient(self)
        ttk.Label(win, padding=10, wraplength=420, justify="left",
                  text="Found folder(s) next to SERVER_ROOT that look like they could be "
                       "CLUSTERDIR. Pick one to use it, or close this to leave CLUSTERDIR "
                       "as-is.").pack()
        for m in matches:
            ttk.Button(win, text=m, command=lambda p=m: self._pick_cluster_dir(p, win)
                       ).pack(fill="x", padx=10, pady=2)
        ttk.Button(win, text="Cancel", command=win.destroy).pack(pady=(4, 10))

    def _pick_cluster_dir(self, path, win):
        self.set("CLUSTERDIR", path)
        self._log("CLUSTERDIR set from suggestion: %s" % path)
        win.destroy()
        self._scan_for_saves_and_backup_root(path)

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
            matches = [p for p in siblings if pattern in os.path.basename(p).lower()]
            suggestions[key] = matches if matches else [os.path.join(parent, default_name)]

        if suggestions:
            self._suggest_saves_backup_roots(suggestions)

    def _suggest_saves_backup_roots(self, suggestions):
        win = tk.Toplevel(self)
        win.title("SAVESROOT / BACKUPROOT suggestion")
        win.resizable(False, False)
        win.transient(self)
        ttk.Label(win, padding=10, wraplength=420, justify="left",
                  text="Found folder(s) next to CLUSTERDIR that look like they could be "
                       "SAVESROOT / BACKUPROOT. Pick one for each to use it, or close "
                       "this to leave as-is.").pack()
        for key, matches in suggestions.items():
            ttk.Label(win, text=key, font=("Segoe UI", 9, "bold")
                      ).pack(anchor="w", padx=10, pady=(6, 0))
            for m in matches:
                exists = os.path.isdir(m)
                label = m if exists else "%s   (not created yet - suggested path)" % m
                btn = ttk.Button(win, text=label)
                btn.configure(command=lambda p=m, k=key, b=btn: self._pick_saves_backup_root(k, p, b))
                btn.pack(fill="x", padx=10, pady=2)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(4, 10))

    def _pick_saves_backup_root(self, key, path, button):
        self.set(key, path)
        self._log("%s set from suggestion: %s" % (key, path))
        button.configure(text="%s  (set)" % path, state="disabled")

    # ------------------------------------------------- SteamCMD install ---- #
    def _install_log(self, line):
        self.install_log.configure(state="normal")
        self.install_log.insert("end", line.rstrip("\n") + "\n")
        self.install_log.see("end")
        self.install_log.configure(state="disabled")

    def _any_install_running(self):
        """True if either the SteamCMD or ArkServerApi install flow is active - they
        share the install_log/install_progress/install_status_var widgets, so only one
        may run at a time."""
        return ((self._install_thread is not None and self._install_thread.is_alive())
                or (self._arkapi_thread is not None and self._arkapi_thread.is_alive()))

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
            messagebox.showinfo("ARKIpelago Launcher", "ARK server install/update complete.")
        elif not self._install_cancelled:
            messagebox.showerror("ARKIpelago Launcher",
                                  "ARK server install failed. See the log for details.")

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
                elif kind == "progress":
                    try:
                        self.install_progress["value"] = payload
                    except tk.TclError:
                        pass
                elif kind == "done":
                    self._on_arkapi_done(payload)
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
    # Entirely opt-in: nothing here runs unless the user clicks "Check for Updates" in
    # the header. Checking, downloading, and the confirm-before-updating dialog all use
    # this app's OWN release repo (UPDATE_REPO), separate from RELEASES_URL /
    # ARKSERVERAPI_RELEASES_API above, which point at the plugin/connector/ArkApi bundle.
    def _on_check_for_updates(self):
        if self._update_check_thread and self._update_check_thread.is_alive():
            return
        if self._update_download_thread and self._update_download_thread.is_alive():
            messagebox.showinfo("ARKIpelago Launcher", "An update is already downloading.")
            return
        self.update_check_btn.configure(state="disabled", text="Checking...")
        self._update_check_thread = threading.Thread(
            target=self._update_check_worker, daemon=True)
        self._update_check_thread.start()

    def _update_check_worker(self):
        try:
            req = urllib.request.Request(
                UPDATE_RELEASES_API,
                headers={"User-Agent": GITHUB_API_USER_AGENT,
                         "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.after(0, self._on_update_check_done, True, data)
        except (OSError, ValueError) as exc:
            self.after(0, self._on_update_check_done, False, str(exc))

    def _on_update_check_done(self, ok, payload):
        self._update_check_thread = None
        self.update_check_btn.configure(state="normal", text="Check for Updates")
        if not ok:
            messagebox.showerror("ARKIpelago Launcher",
                                  "Could not check for updates:\n\n%s" % payload)
            return
        data = payload
        version_str, tag = _extract_release_version(data)
        html_url = data.get("html_url") or UPDATE_RELEASES_PAGE
        if not tag:
            messagebox.showerror("ARKIpelago Launcher",
                                  "GitHub returned no release tag or name for the latest "
                                  "release.")
            return
        if version_str is None:
            messagebox.showwarning(
                "ARKIpelago Launcher",
                "Found a release (%s) but couldn't find a version number in it to compare "
                "against your installed version (%s). Check the release page yourself:\n\n%s"
                % (tag, APP_VERSION, html_url))
            return
        if not _version_is_newer(version_str, APP_VERSION):
            messagebox.showinfo(
                "ARKIpelago Launcher",
                "You're up to date.\n\nInstalled version: %s\nLatest release: %s"
                % (APP_VERSION, tag))
            return
        self._show_update_available_dialog(data, tag, html_url)

    def _pick_update_asset(self, data):
        """The .exe asset to download - prefers one literally named ArkAPLauncher.exe,
        falls back to the first .exe asset on the release (there should only ever be one)."""
        assets = data.get("assets") or []
        exe_assets = [a for a in assets if (a.get("name") or "").lower().endswith(".exe")]
        if not exe_assets:
            return None
        preferred = next((a for a in exe_assets
                           if (a.get("name") or "").lower() == "arkaplauncher.exe"), None)
        return preferred or exe_assets[0]

    def _find_checksum_asset(self, data, asset_name):
        """Best-effort: some releases attach a checksum file alongside the exe. Recognises
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
        asset = self._pick_update_asset(data)

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
            ttk.Label(btn_row, text="No downloadable .exe was found on this release - "
                                     "update manually from the link above.",
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
                "and are never touched by this - only the .exe itself is replaced. Make "
                "sure any unsaved changes in the Configuration tab are saved first."
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
        new_path = os.path.join(base_dir(), "ArkAPLauncher_new.exe")
        url = asset.get("browser_download_url")
        expected_size = asset.get("size", 0)
        self._cleanup_failed_download(new_path)

        try:
            q.put(("status", "Downloading %s..." % asset.get("name", "update")))
            downloaded = self._download_update_asset(url, new_path, expected_size, q)
        except (OSError, ValueError) as exc:
            self._cleanup_failed_download(new_path)
            q.put(("error", "Download failed: %s" % exc))
            return

        if expected_size and downloaded != expected_size:
            self._cleanup_failed_download(new_path)
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
                actual_hash = self._sha256_of_file(new_path)
                if actual_hash != expected_hash:
                    self._cleanup_failed_download(new_path)
                    q.put(("error",
                           "Checksum verification failed for the downloaded update "
                           "(expected %s, got %s). Nothing was changed."
                           % (expected_hash, actual_hash)))
                    return
                q.put(("status", "Checksum verified."))

        q.put(("status", "Download verified (%d bytes). Preparing to restart..." % downloaded))
        q.put(("ready", (new_path, tag)))

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
                    new_path, tag = payload
                    self._on_update_download_ready(new_path, tag)
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

    def _on_update_download_ready(self, new_exe_path, tag):
        win, self._update_progress_win = self._update_progress_win, None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
        try:
            self._launch_update_helper_and_exit(new_exe_path, tag)
        except OSError as exc:
            self._cleanup_failed_download(new_exe_path)
            messagebox.showerror(
                "ARKIpelago Launcher",
                "Downloaded the update but could not start the updater helper - the "
                "current launcher was left untouched.\n\n%s" % exc)

    @staticmethod
    def _build_update_bat_script(pid, current_exe, new_exe, old_backup, result_path, tag):
        """A short-lived helper script: the running exe can't overwrite/rename itself, so
        this waits for our PID to exit, then does old-exe-out / new-exe-in / relaunch. Every
        exit path writes a two-line result_path (status, tag[, message]) before exiting so
        the relaunched app (or the user, on failure) can tell what happened - see
        _check_previous_update_result(). On any failure after the rename of the running exe
        has already happened, it rolls that rename back before giving up, so the exe is
        never left missing."""
        old_name = os.path.basename(old_backup)
        cur_name = os.path.basename(current_exe)
        safe_tag = re.sub(r"[\r\n]", " ", tag) or "unknown"

        def write_result(status, message):
            return [
                '> "%s" echo %s' % (result_path, status),
                '>> "%s" echo %s' % (result_path, safe_tag),
                '>> "%s" echo %s' % (result_path, message),
            ]

        lines = [
            "@echo off",
            "setlocal EnableDelayedExpansion",
            "",
            "set /a COUNT=0",
            ":wait",
            'tasklist /FI "PID eq %s" 2>NUL | find "%s" >NUL' % (pid, pid),
            "if not errorlevel 1 (",
            "    set /a COUNT+=1",
            "    if !COUNT! GEQ 30 goto afterwait",
            "    timeout /t 1 /nobreak >NUL",
            "    goto wait",
            ")",
            ":afterwait",
            "",
            'if not exist "%s" (' % new_exe,
        ]
        lines += ["    " + l for l in write_result(
            "FAIL", "Downloaded update file was missing when the helper ran.")]
        lines += ["    exit /b 1", ")", "",
                   'if exist "%s" del /f /q "%s" >NUL 2>&1' % (old_backup, old_backup), "",
                   'ren "%s" "%s"' % (current_exe, old_name),
                   "if errorlevel 1 ("]
        lines += ["    " + l for l in write_result(
            "FAIL", "Could not rename the running exe out of the way - it may still be "
                     "locked.")]
        lines += ["    exit /b 1", ")",
                   'if not exist "%s" (' % old_backup]
        lines += ["    " + l for l in write_result(
            "FAIL", "Renaming the old exe did not produce the expected backup file.")]
        lines += ["    exit /b 1", ")", "",
                   'ren "%s" "%s"' % (new_exe, cur_name),
                   "if errorlevel 1 ("]
        lines += ['    ren "%s" "%s"' % (old_backup, cur_name)]
        lines += ["    " + l for l in write_result(
            "FAIL", "Could not rename the new exe into place - the previous version was "
                     "restored.")]
        lines += ['    start "" "%s"' % current_exe,
                   '    del "%~f0"',
                   "    exit /b 1", ")", ""]
        lines += write_result("OK", "Updated successfully.")
        lines += ['start "" "%s"' % current_exe,
                   'del "%~f0"',
                   "exit /b 0", ""]
        return "\r\n".join(lines)

    def _launch_update_helper_and_exit(self, new_exe_path, tag):
        exe_dir = base_dir()
        current_exe = os.path.normpath(sys.executable)
        old_backup = os.path.join(exe_dir, "ArkAPLauncher_old.exe")
        bat_path = os.path.join(exe_dir, "arkap_update_helper.bat")
        result_path = os.path.join(exe_dir, UPDATE_RESULT_FILENAME)

        script = self._build_update_bat_script(
            os.getpid(), current_exe, new_exe_path, old_backup, result_path, tag)
        with open(bat_path, "w", encoding="utf-8", newline="") as f:
            f.write(script)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["cmd.exe", "/c", bat_path], cwd=exe_dir,
                          creationflags=creationflags, close_fds=True)
        self.destroy()

    def _check_previous_update_result(self):
        """Local file read only, no network - reports the outcome of an update-helper run
        that happened just before this process started (see _build_update_bat_script)."""
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
                "exe was left in place (or restored) rather than leaving you without one."
                "\n\n%s" % (tag or "the latest version",
                             detail or "See arkap_update_helper.bat next to the exe, if "
                                       "it's still there, for details."))

    # -------------------------------------------- ArkAP plugin install ----- #
    def _browse_plugin_src(self):
        current = self._plugin_src_var.get().strip()
        initial = current if os.path.isdir(current) else base_dir()
        picked = filedialog.askdirectory(
            initialdir=initial,
            title="Select your unzipped ArkAP_plugin folder (contains ArkAP\\ArkAP.dll)")
        if picked:
            self._plugin_src_var.set(os.path.normpath(picked))

    def _detect_plugin_source(self):
        """Best-effort scan of a few likely spots for a folder containing
        ArkAP\\ArkAP.dll (the unzipped plugin download). Returns a path or None."""
        b = base_dir()
        cwd = os.getcwd()
        parent = os.path.dirname(b)
        home = os.path.expanduser("~")
        cands = [
            b, os.path.join(b, "ArkAP_plugin"),
            parent, os.path.join(parent, "ArkAP_plugin"),
            cwd, os.path.join(cwd, "ArkAP_plugin"),
            os.path.join(home, "Downloads", "ArkAP_plugin"),
            os.path.join(home, "Downloads"),
        ]
        seen = set()
        for c in cands:
            if not c:
                continue
            cn = os.path.normpath(c)
            key = cn.lower()
            if key in seen:
                continue
            seen.add(key)
            if os.path.isfile(os.path.join(cn, PLUGIN_PAYLOAD_MARKER)):
                return cn
        return None

    def _resolve_plugin_source(self):
        """Return a folder containing ArkAP\\ArkAP.dll to install from, or None.

        Order: whatever's typed/remembered -> auto-detect common spots -> ask the user to
        browse. A message is shown before returning None so the caller can just bail."""
        typed = self._plugin_src_var.get().strip()
        if typed and os.path.isfile(os.path.join(typed, PLUGIN_PAYLOAD_MARKER)):
            return os.path.normpath(typed)
        if typed:
            self._install_log("Plugin source '%s' has no ArkAP\\ArkAP.dll - "
                              "auto-detecting instead." % typed)

        found = self._detect_plugin_source()
        if found:
            self._plugin_src_var.set(found)
            self._install_log("Auto-detected plugin source: %s" % found)
            return found

        messagebox.showinfo(
            "Install Plugin",
            "Couldn't find the plugin files automatically.\n\nBrowse to your unzipped "
            "ArkAP_plugin folder - the one that contains ArkAP\\ArkAP.dll.")
        picked = filedialog.askdirectory(
            title="Select your unzipped ArkAP_plugin folder (contains ArkAP\\ArkAP.dll)")
        if not picked:
            return None
        picked = os.path.normpath(picked)
        if not os.path.isfile(os.path.join(picked, PLUGIN_PAYLOAD_MARKER)):
            messagebox.showwarning(
                "Install Plugin",
                "That folder doesn't contain ArkAP\\ArkAP.dll:\n\n%s\n\nPick the folder "
                "you unzipped ArkAP_plugin.zip into." % picked)
            return None
        self._plugin_src_var.set(picked)
        return picked

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

    def _persist_plugin_src(self, path):
        """Immediately remember the chosen plugin source in the config JSON (merged),
        so it survives even without a full Save."""
        try:
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            data[PLUGIN_SRC_KEY] = path
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            self._install_log("! Could not save plugin source path: %s" % exc)

    def on_install_plugin(self):
        """Install the ArkAP plugin into <SERVER_ROOT>\\...\\ArkApi\\Plugins\\ArkAP by
        copying it natively from the user's unzipped plugin download."""
        self.notebook.select(self.tab_install)
        self.install_log.configure(state="normal")
        self.install_log.delete("1.0", "end")
        self.install_log.configure(state="disabled")

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
                "ARK Server API (AseApi) into Win64 first "
                "(https://github.com/ArkServerApi/AseApi), then try again." % win64)
            return

        plugins = os.path.join(arkapi, "Plugins")
        try:
            os.makedirs(plugins, exist_ok=True)  # ArkApi exists but Plugins may not yet
        except OSError as exc:
            messagebox.showerror("Install Plugin",
                                  "Could not create the Plugins folder:\n\n%s\n\n%s"
                                  % (plugins, exc))
            return

        src_root = self._resolve_plugin_source()
        if not src_root:
            self._install_log("Install Plugin: cancelled (no plugin source).")
            return
        src_arkap = os.path.join(src_root, "ArkAP")
        dst_arkap = os.path.join(plugins, "ArkAP")

        upgrade = os.path.isfile(os.path.join(dst_arkap, PLUGIN_PRESERVE_ON_UPGRADE))
        if not messagebox.askyesno(
                "Install Plugin",
                "Install the ArkAP plugin?\n\nFrom: %s\nTo:   %s\n\n%s\n\n"
                "(Any ipc / tracking files already there are left in place - use a reset "
                "button for a clean seed.)"
                % (src_arkap, dst_arkap,
                   "An existing ArkAP.config.json will be KEPT (your settings survive)."
                   if upgrade else "This is a fresh install.")):
            self._install_log("Install Plugin: cancelled.")
            return

        copied, skipped, errors = self._copy_plugin_tree(src_arkap, dst_arkap)

        # Point the shared path vars at the confirmed install so reset / Open Plugins /
        # ipc_dir all follow it (no second parallel path variable).
        self.set("PLUGINS_DIR", plugins)
        self.set("ipc_dir", os.path.join(dst_arkap, "ipc"))
        self._plugin_src_dir = src_root
        self._plugin_src_var.set(src_root)
        self._persist_plugin_src(src_root)

        self._install_log("Install Plugin:")
        self._install_log("  From: %s" % src_arkap)
        self._install_log("  To:   %s" % dst_arkap)
        for s in skipped:
            self._install_log("  kept: %s" % s)
        self._install_log("  Copied %d file(s)." % len(copied))
        for c in copied:
            self._install_log("    + %s" % c)
        if errors:
            for e in errors:
                self._install_log("  ! %s" % e)
            messagebox.showwarning(
                "Install Plugin",
                "Plugin install finished with %d problem(s) - see the log.\n\nCopied %d "
                "file(s) to:\n%s" % (len(errors), len(copied), dst_arkap))
        else:
            messagebox.showinfo(
                "Install Plugin",
                "ArkAP plugin installed (%d file(s)) to:\n\n%s\n\nRestart (or start) the "
                "ARK dedicated server, then run the connector." % (len(copied), dst_arkap))

    # -------------------------------------------------- quick-launch ------- #
    def _open_folder(self, path, label):
        if not path:
            messagebox.showwarning("ARKIpelago Launcher", "%s is not set." % label)
            return
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            messagebox.showwarning("ARKIpelago Launcher",
                                   "%s does not exist:\n%s" % (label, path))
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

    def open_server_root(self):
        self._open_folder(self.get("SERVER_ROOT"), "SERVER_ROOT")

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
        reset_ark_test.bat: the save is MOVED to a timestamped backup, not deleted).
        Returns a (kind, path, detail) tuple: kind is 'moved'/'created'/'error'."""
        path = os.path.normpath(path)
        if os.path.isdir(path):
            backup = "%s_backup_%s" % (path, ts)
            try:
                shutil.move(path, backup)
            except OSError as exc:
                return ("error", path, str(exc))
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass
            return ("moved", path, backup)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            return ("error", path, str(exc))
        return ("created", path, None)

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

        # 2) World save + optional per-map / cluster data.
        ts = time.strftime("%Y%m%d-%H%M%S")
        save_lines = []
        for label, path in save_targets:
            kind, p, detail = self._backup_and_clear_dir(path, ts)
            if kind == "moved":
                save_lines.append("%s: backed up -> %s (cleared)" % (label, detail))
            elif kind == "created":
                save_lines.append("%s: nothing there yet - created empty %s" % (label, p))
            else:
                save_lines.append("! %s: could NOT reset %s (%s)" % (label, p, detail))
                errors.append("%s: %s" % (p, detail))

        self._report_reset("Full reset for new seed",
                           plugin_dir or "(plugin not installed)",
                           deleted, missing, errors, extra_lines=save_lines)

    def _report_reset(self, label, plugin_dir, deleted, missing, errors, extra_lines=None):
        """Log a per-file breakdown and show a summary popup. Missing files are reported
        as already-clear, not failures - only real OSErrors count as problems."""
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
                "%s finished with %d problem(s) - see the log.\n\n%s"
                % (label, len(errors), summary))
        else:
            messagebox.showinfo("ARKIpelago Launcher",
                                 "%s complete.\n\n%s" % (label, summary))

    def run_bat(self, batname):
        scripts = self._scripts_dir
        path = os.path.join(scripts, batname) if scripts else ""
        if not path or not os.path.isfile(path):
            messagebox.showwarning("ARKIpelago Launcher",
                                   "%s not found in the scripts folder." % batname)
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
        style.configure("Highlight.TLabel", background=self.SEARCH_HL_COLOR,
                         foreground="black")
        style.configure("Highlight.TCheckbutton", background=self.SEARCH_HL_COLOR,
                         foreground="black")
        style.configure("CurrentHighlight.TLabel", background=self.SEARCH_HL_CURRENT_COLOR,
                         foreground="black")
        style.configure("CurrentHighlight.TCheckbutton", background=self.SEARCH_HL_CURRENT_COLOR,
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
            return "TLabel"
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
                pos = widget.search(query, idx, stopindex="end", nocase=True)
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


if __name__ == "__main__":
    ArkAPLauncher().mainloop()
