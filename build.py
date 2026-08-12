#!/usr/bin/env python3
"""
Build ARKipelago Launcher into dist/ARKipelago Launcher/ with PyInstaller
(--onedir --windowed).

Usage:
    python build.py            # normal --onedir build -> ship the dist zip
    python build.py --bridge   # one-time --onefile bridge build -> ship the bare .exe

--onedir (default) keeps the interpreter DLL permanently in dist\\<app>\\_internal\\ instead
of re-extracting it to a fresh %TEMP%\\_MEI folder on every launch, which is what eliminates
the "Failed to load Python DLL" race. --bridge builds the single transitional --onefile .exe
that lets already-shipped onefile-era updaters cross over to the onedir format - see the
APP_VERSION note and the assemble step below.

What it does:
  * Confirms PyInstaller is installed (pip install -r requirements-build.txt
    if not).
  * Bundles assets/ (icon.ico, logo.png, FuturaNowHeadline.ttf,
    ARKipelagoArchColors.png and ASE_theme_a_little_loud.mp3 for the credits
    easter egg - the whole folder ships, so new assets need no change here) and
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
  * Deliberately bundles NOTHING for PopTracker or the ARK tracker pack, even
    though a reference copy of PopTracker may be sitting in this folder: both are
    downloaded at runtime from their own GitHub releases (see
    POPTRACKER_RELEASES_API / TRACKER_PACK_RELEASES_API in arkap_launcher.py).
    PopTracker alone is ~17 MB extracted - several times the whole launcher - and
    it is third-party software with its own release cadence. Adding a
    poptracker/ folder to the "datas" below would be a mistake, not an omission.
  * Uses --windowed (no console window) - SteamCMD's stdout is captured via
    subprocess.PIPE in arkap_launcher.py and streamed into the app's own Text
    widget already; that pipe-based capture is unaffected by --windowed and
    needs no changes here. --onedir vs --onefile is likewise unaffected - it
    only changes where steamcmd.exe/scripts/assets are read from (the permanent
    _internal\\ folder vs. a per-run temp folder), which resource_dir() resolves
    via sys._MEIPASS either way, not how the subprocess pipe is read.
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
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY_SCRIPT = os.path.join(HERE, "arkap_launcher.py")
ICON_PATH = os.path.join(HERE, "assets", "icon.ico")
ASSETS_DIR = os.path.join(HERE, "assets")
STEAMCMD_EXE = os.path.join(HERE, "steamcmd", "steamcmd.exe")
SCRIPTS_DIR = os.path.join(HERE, "scripts")
DIST_DIR = os.path.join(HERE, "dist")

# PyInstaller --name, which is also the shipped exe's filename. Renamed from the old
# code-safe "ArkAPLauncher" purely for tidiness; safe because the self-replace updater
# resolves the running exe via sys.executable (never by name) and _locate_staged_app
# accepts either launcher exe name (see _KNOWN_LAUNCHER_EXE_NAMES).
APP_NAME = "ARKipelago Launcher"

# Windows forbids ':' in path names (NTFS reserves it for alternate data
# streams), so the in-app title "ARK:Ipelago Launcher" can't be used as-is
# for the shipped folder. Confirmed with the project owner: use this instead.
DIST_FOLDER_NAME = "ARKipelago Launcher"

CONFIG_FILENAME = "arkap_launcher_config.json"
PROFILES_FILENAME = "arkap_launcher_profiles.json"

# Build log. Deliberately self-contained rather than importing the launcher's launcher_log:
# the build most worth having a log of is the one where arkap_launcher.py itself won't
# import. Same shape as the app's logs though - next to this script, appended across runs,
# size-capped - so "send me the log" means the same thing everywhere.
BUILD_LOG_PATH = os.path.join(HERE, "build.log")
BUILD_LOG_MAX_BYTES = 512 * 1024


def log(msg=""):
    """print() plus one line in build.log. Every print in this script goes through here so
    a build that dies at 3am leaves the same output on disk that scrolled past on screen -
    including PyInstaller's own, which is teed in from its stdout (see main)."""
    print(msg)
    try:
        with open(BUILD_LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(msg.rstrip("\n") + "\n")
    except OSError:
        pass  # a build that can't be logged still has to build


def _start_build_log(header):
    """Cap the log, then open a new run's section in it."""
    try:
        if os.path.getsize(BUILD_LOG_PATH) > BUILD_LOG_MAX_BYTES:
            with open(BUILD_LOG_PATH, "rb") as f:
                f.seek(-BUILD_LOG_MAX_BYTES, os.SEEK_END)
                tail = f.read()
            with open(BUILD_LOG_PATH, "wb") as f:
                f.write(tail.split(b"\n", 1)[-1])  # start at a clean line boundary
    except OSError:
        pass
    log("")
    log("===== %s  %s =====" % (time.strftime("%Y-%m-%d %H:%M:%S"), header))


def fail(msg):
    """Log the reason and stop. The traceback PyInstaller would otherwise print is not the
    useful part - what to DO about it is."""
    log("")
    log("BUILD FAILED: %s" % msg)
    log("(this run is recorded in %s)" % BUILD_LOG_PATH)
    sys.exit(1)


def _check_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("PyInstaller is not installed. Install it with:")
        log("    python -m pip install -r requirements-build.txt")
        sys.exit(1)


def _is_running(image_name):
    """True if a process with this exe image name is running. tasklist is always present on
    Windows and needs no dependency - same approach as is_process_running() in
    arkap_launcher.py. "Can't tell" counts as not running."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/fi", "imagename eq %s" % image_name, "/nh"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return image_name.lower() in (out.stdout or "").lower()


def _clean_previous_output():
    """Delete the previous build\\ and dist\\ output folders before PyInstaller can try.

    PyInstaller (--noconfirm) does this itself, but when anything in there is locked it
    dies in a raw shutil.rmtree traceback about one file, several minutes into the build,
    which reads like a bug in the build rather than "close the app". The lock is almost
    always the previously built exe still running out of dist\\ - the very thing the
    "don't run it from dist\\" note at the end of a successful build warns about. Doing the
    delete here means the failure lands in two seconds, says which folder and what to do,
    and is caught for every cause of a lock (running exe, antivirus, an Explorer preview
    pane), not just the ones we can name."""
    exe_name = APP_NAME + ".exe"
    for folder in (os.path.join(HERE, "build", APP_NAME), os.path.join(DIST_DIR, APP_NAME)):
        if not os.path.isdir(folder):
            continue
        try:
            shutil.rmtree(folder)
            log("Cleared previous output: %s" % folder)
        except OSError as exc:
            running = _is_running(exe_name)
            fail("could not clear the previous build output.\n"
                 "  Folder: %s\n"
                 "  Blocked by: %s\n\n"
                 "  %s\n"
                 "  Close it, then run this script again. If nothing obvious is running, "
                 "check for an\n"
                 "  antivirus scan or an open Explorer preview on that folder, or just "
                 "reboot -\n"
                 "  Windows also keeps a bundled font file (assets\\FuturaNowHeadline.ttf) "
                 "locked for\n"
                 "  a while after the app that registered it exits."
                 % (folder, exc,
                    "'%s' is RUNNING right now - that is what is holding the folder."
                    % exe_name if running else
                    "A previously built '%s' is the usual cause." % exe_name))


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
    bridge = "--bridge" in sys.argv
    _start_build_log("build.py %s" % ("--bridge (--onefile)" if bridge else "(--onedir)"))
    _check_pyinstaller()

    if not os.path.isfile(ENTRY_SCRIPT):
        fail("entry script not found: %s" % ENTRY_SCRIPT)
    if not os.path.isfile(STEAMCMD_EXE):
        fail("steamcmd/steamcmd.exe not found - place it there first "
             "(see steamcmd_dir()/README) or the exe will fall back to "
             "downloading it on first use of Install ARK Server.")

    _clean_previous_output()

    # --onedir (default) is the fix for the recurring "Failed to load Python DLL" dialog:
    # unlike --onefile it never re-extracts pythonNN.dll into a fresh %TEMP%\_MEI folder on
    # every launch, so there is no extraction step for the AV scanner to race at load time.
    # --bridge builds the one transitional --onefile release instead (a single .exe the old
    # onefile-era updaters already in the wild can find and install as before); that bridge
    # carries this same new onedir-aware updater code, so everyone who passes through it lands
    # on onedir. See APP_VERSION / _build_update_ps_script in arkap_launcher.py.
    args = [
        "--name", APP_NAME,
        "--onefile" if bridge else "--onedir",
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
        log("NOTE: scripts/ folder not found - building without bundled server "
              "scripts. Quick-launch Run buttons will have nothing to run until the "
              "scripts exist next to the launcher.")

    if os.path.isdir(ASSETS_DIR):
        # icon.ico, logo.png, the FuturaNowHeadline.ttf header font, and the credits
        # easter egg's ARKipelagoArchColors.png + ASE_theme_a_little_loud.mp3 all live
        # here and are pulled from resource_dir()/assets at runtime - see _assets_dir()/
        # _load_window_icon()/_register_private_font()/_play_egg_music() in
        # arkap_launcher.py. The whole folder is added, so no per-file list to maintain.
        # The MP3 needs no extra dependency: playback is Windows' own MCI via ctypes.
        args += ["--add-data", _add_data_arg(ASSETS_DIR, "assets")]
    else:
        log("NOTE: assets/ folder not found - building without a window "
              "icon/logo/custom header font. Add assets/icon.ico, assets/logo.png, "
              "and assets/FuturaNowHeadline.ttf, then rebuild to include them.")

    if os.path.isfile(ICON_PATH):
        args += ["--icon", ICON_PATH]
    else:
        log("NOTE: assets/icon.ico not found - building without an .exe "
              "icon. Drop a .ico file at assets/icon.ico and rebuild to add "
              "one (used for both the taskbar icon and the exe file icon).")

    args.append(ENTRY_SCRIPT)

    log("Running PyInstaller with args:")
    for a in args:
        log("  %s" % a)

    # Read PyInstaller's output line by line instead of letting it inherit the console:
    # it still scrolls past live, but it also lands in build.log. That output is where a
    # build failure actually explains itself - the crash is inside PyInstaller's process,
    # so no try/except here could ever have caught it.
    proc = subprocess.Popen([sys.executable, "-m", "PyInstaller"] + args, cwd=HERE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    for line in proc.stdout:
        log(line.rstrip("\n"))
    returncode = proc.wait()
    if returncode != 0:
        fail("PyInstaller exited with code %d - its output above (and in %s) says why."
             % (returncode, BUILD_LOG_PATH))

    if bridge:
        # --onefile: a single self-contained .exe at the dist root. This is the whole
        # shipped artifact - upload it AS-IS as the release's single .exe asset so the old
        # onefile-era updaters (which fetch /releases/latest and grab the one .exe) can
        # install it. Keep this release pinned as GitHub "Latest" for as long as any
        # pre-bridge client might still check in; newer onedir releases don't need the pin
        # (new clients find them from the releases list - see _pick_best_release).
        built_exe = os.path.join(DIST_DIR, APP_NAME + ".exe")
        if not os.path.isfile(built_exe):
            fail("build finished but %s was not found." % built_exe)
        log("\nBRIDGE build complete (--onefile):")
        log("  %s  <- ship this single .exe as the release's .exe asset" % built_exe)
        log("\nRelease steps for the bridge:")
        log("  * Upload ONLY this .exe as the release asset (no zip).")
        log("  * Publish it as GitHub's 'Latest' release and keep it pinned there so")
        log("    pre-bridge clients always find it, even if they check in late.")
        log("  * Every release AFTER this one is a normal --onedir build (no --bridge):")
        log("    ship its .zip, and do NOT let it steal the 'Latest' pin from the bridge.")
        return

    # --onedir: PyInstaller already produced dist\<APP_NAME>\ with the exe + _internal\.
    # That folder IS the shipped app; just drop blank config/profiles in and zip it.
    out_folder = os.path.join(DIST_DIR, DIST_FOLDER_NAME)
    final_exe = os.path.join(out_folder, APP_NAME + ".exe")
    if not os.path.isfile(final_exe):
        fail("build finished but %s was not found." % final_exe)

    config_path = _write_blank_config(out_folder)
    profiles_path = _write_blank_profiles(out_folder)

    # Zip the folder NOW, before anyone can double-click the exe to "just check it
    # works". That check is what leaked real data once: the launcher discovers
    # connector.ini by walking up to 5 folders above base_dir() (_discover_locations),
    # so an exe run anywhere under a dev tree finds the dev's real ArkConnector\
    # connector.ini and writes its server/slot into the config + profiles JSON sitting
    # right next to it - which then ships. Capturing the archive at build time means the
    # shipped artifact is always the blank one regardless of what happens to the folder
    # afterwards. Test-run the exe from a copy somewhere else, not from dist\.
    # This zip is also exactly what the self-updater downloads and swaps in (it contains
    # the top-level <DIST_FOLDER_NAME>\ with the exe + _internal\) - see _locate_staged_app.
    zip_path = shutil.make_archive(out_folder, "zip", DIST_DIR, DIST_FOLDER_NAME)

    log("\nBuild complete (--onedir):")
    log("  %s" % final_exe)
    log("  %s" % config_path)
    log("  %s" % profiles_path)
    log("  %s  <- ship this (release .zip asset)" % zip_path)
    log("\nShip the whole '%s' folder (exe + _internal\\) - the exe reads/writes its "
          "config and profiles JSON next to itself (base_dir()), so the folder must stay "
          "together, in its own dedicated folder with no unrelated files beside it."
          % DIST_FOLDER_NAME)
    log("Do NOT run the exe out of dist\\ and then re-zip by hand: it will have "
          "written your real paths/room into the two JSON files by then.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise                        # fail() and _check_pyinstaller already said why
    except BaseException:            # including Ctrl-C: "why did last night's build stop?"
        log("")
        log("BUILD CRASHED (unhandled error in build.py):")
        log(traceback.format_exc())
        log("(this run is recorded in %s)" % BUILD_LOG_PATH)
        raise

