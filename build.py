#!/usr/bin/env python3
"""
Build ARKipelago Launcher into dist/ARKipelago Launcher/ with PyInstaller
(--onefile --windowed).

Usage:
    python build.py

What it does:
  * Confirms PyInstaller is installed (pip install -r requirements-build.txt
    if not).
  * Bundles assets/ (icon.ico, logo.png, FuturaNowHeadline.ttf) and
    steamcmd/steamcmd.exe as PyInstaller "datas" so they're embedded in the
    exe and extracted to a temp folder at runtime (see resource_dir() in
    arkap_launcher.py) - the user never needs a separate steamcmd/ or
    assets/ folder alongside the exe. Only steamcmd.exe itself is bundled
    (not its self-generated dlls, logs, cache, or userdata) - that matches
    exactly what Valve's own steamcmd.zip bootstrapper contains, and keeps
    the exe from shipping stale local state.
  * Also bundles scripts/ (start_ase_server.bat, switch_map.bat,
    start_transfer_server.bat, reset_ark_test.bat, apply_server_config.bat +
    .ps1, and serverconfig\\*.settings templates) - extracted next to the exe
    at runtime by extract_bundled_scripts() into an ArkServerScripts folder.
    install_plugin.bat is deliberately NOT bundled here; the "Install
    Plugin" button reimplements that copy natively in arkap_launcher.py.
  * Uses --windowed (no console window) - SteamCMD's stdout is captured via
    subprocess.PIPE in arkap_launcher.py and streamed into the app's own Text
    widget already; that pipe-based capture is unaffected by --windowed and
    needs no changes here. --onefile is likewise unaffected - it only changes
    where steamcmd.exe/scripts/assets get extracted from (a per-run temp
    folder vs. a sibling folder), not how the subprocess pipe is read.
  * Packages the result into dist/<DIST_FOLDER_NAME>/ containing the exe
    plus blank arkap_launcher_config.json / arkap_launcher_profiles.json
    (no real paths) - so the folder is visibly "the app's home" from first
    run, rather than the exe silently creating those files wherever it's
    placed. Both files are regenerated from scratch on every build (never
    copied from the real dev config next to this script, which has real
    paths in it).
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY_SCRIPT = os.path.join(HERE, "arkap_launcher.py")
ICON_PATH = os.path.join(HERE, "assets", "icon.ico")
ASSETS_DIR = os.path.join(HERE, "assets")
STEAMCMD_EXE = os.path.join(HERE, "steamcmd", "steamcmd.exe")
SCRIPTS_DIR = os.path.join(HERE, "scripts")
DIST_DIR = os.path.join(HERE, "dist")

# PyInstaller build/exe name - kept short and code-safe (no spaces/colons).
APP_NAME = "ArkAPLauncher"

# Windows forbids ':' in path names (NTFS reserves it for alternate data
# streams), so the in-app title "ARK:Ipelago Launcher" can't be used as-is
# for the shipped folder. Confirmed with the project owner: use this instead.
DIST_FOLDER_NAME = "ARKipelago Launcher"

CONFIG_FILENAME = "arkap_launcher_config.json"
PROFILES_FILENAME = "arkap_launcher_profiles.json"


def _check_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Install it with:")
        print("    python -m pip install -r requirements-build.txt")
        sys.exit(1)


def _add_data_arg(src, dest_subfolder):
    # Windows PyInstaller --add-data separator is ';' (POSIX uses ':').
    return "%s;%s" % (src, dest_subfolder)


def _write_blank_config(dest_folder):
    """Write a blank config JSON - {} - into dest_folder.

    Deliberately just {}, not a fully-keyed template with empty strings:
    arkap_launcher.py's DEFAULT_VALUES / PLACEHOLDER_EXAMPLES already fill in
    sane defaults (MAP=TheIsland, ports, etc.) and greyed-out example paths
    for anything missing from the JSON, so {} round-trips to the same UI
    state as "file absent" while still containing no real user paths.
    """
    path = os.path.join(dest_folder, CONFIG_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({}, f, indent=2)
    return path


def _write_blank_profiles(dest_folder):
    path = os.path.join(dest_folder, PROFILES_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"profiles": {}}, f, indent=2)
    return path


def main():
    _check_pyinstaller()

    if not os.path.isfile(ENTRY_SCRIPT):
        sys.exit("Entry script not found: %s" % ENTRY_SCRIPT)
    if not os.path.isfile(STEAMCMD_EXE):
        sys.exit(
            "steamcmd/steamcmd.exe not found - place it there first "
            "(see steamcmd_dir()/README) or the exe will fall back to "
            "downloading it on first use of Install ARK Server."
        )

    args = [
        "--name", APP_NAME,
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--add-data", _add_data_arg(STEAMCMD_EXE, "steamcmd"),
    ]

    if os.path.isdir(SCRIPTS_DIR):
        # Bundle the server scripts (start_ase_server.bat, switch_map.bat, ...,
        # apply_server_config.ps1 + serverconfig\*.settings). Extracted next to the exe
        # at runtime by extract_bundled_scripts() so users don't manage them separately.
        args += ["--add-data", _add_data_arg(SCRIPTS_DIR, "scripts")]
    else:
        print("NOTE: scripts/ folder not found - building without bundled server "
              "scripts. Quick-launch Run buttons will have nothing to run until the "
              "scripts exist next to the launcher.")

    if os.path.isdir(ASSETS_DIR):
        # icon.ico, logo.png, and the FuturaNowHeadline.ttf header font all live here
        # and are pulled from resource_dir()/assets at runtime - see _assets_dir()/
        # _load_window_icon()/_register_private_font() in arkap_launcher.py.
        args += ["--add-data", _add_data_arg(ASSETS_DIR, "assets")]
    else:
        print("NOTE: assets/ folder not found - building without a window "
              "icon/logo/custom header font. Add assets/icon.ico, assets/logo.png, "
              "and assets/FuturaNowHeadline.ttf, then rebuild to include them.")

    if os.path.isfile(ICON_PATH):
        args += ["--icon", ICON_PATH]
    else:
        print("NOTE: assets/icon.ico not found - building without an .exe "
              "icon. Drop a .ico file at assets/icon.ico and rebuild to add "
              "one (used for both the taskbar icon and the exe file icon).")

    args.append(ENTRY_SCRIPT)

    print("Running PyInstaller with args:")
    for a in args:
        print("  %s" % a)

    result = subprocess.run([sys.executable, "-m", "PyInstaller"] + args, cwd=HERE)
    if result.returncode != 0:
        sys.exit(result.returncode)

    built_exe = os.path.join(DIST_DIR, APP_NAME + ".exe")
    if not os.path.isfile(built_exe):
        sys.exit("Build finished but %s was not found." % built_exe)

    # --- Assemble the shipped folder: exe + blank config/profiles JSON --- #
    out_folder = os.path.join(DIST_DIR, DIST_FOLDER_NAME)
    if os.path.isdir(out_folder):
        shutil.rmtree(out_folder)
    os.makedirs(out_folder)

    final_exe = os.path.join(out_folder, APP_NAME + ".exe")
    shutil.move(built_exe, final_exe)
    config_path = _write_blank_config(out_folder)
    profiles_path = _write_blank_profiles(out_folder)

    print("\nBuild complete:")
    print("  %s" % final_exe)
    print("  %s" % config_path)
    print("  %s" % profiles_path)
    print("\nShip the whole '%s' folder - the exe reads/writes its config and "
          "profiles JSON next to itself (base_dir()), so the folder must stay "
          "together." % DIST_FOLDER_NAME)


if __name__ == "__main__":
    main()
