# ARK:ipelago Launcher : Setup Guide

> Pro tip: most options in the app have tooltips if you hover over them.
> The Search bar (top left of the app) searches field labels, tooltips, and text across every tab. Press Enter, then use Find Next / Find Prev to jump between matches.
> Each numbered step in the app's Instructions tab has a checkbox. Tick it and the step collapses to just "Step 1", "Step 2", etc. "Collapse all steps" / "Expand all steps" do the lot at once.

## Start here: install in this order

The three installs below must happen in order: **the ARK server first**, then **ArkServerApi** into it, then the **ArkAP plugin** into ArkApi. Each one needs the previous one to already exist. All three live on the **Server Install** tab.

1. **Server Install tab**, set `SERVER_ROOT` (the folder the server gets installed into), then click **"Install ARK Server."** This downloads about 18GB via SteamCMD. Progress shows in the console box below the buttons, and "Cancel" stops it if you need to.
   > **Note:** make sure your ARK: Survival Evolved game is on the **preaquatica** branch, or you won't be able to join.

   - If it fails with **exit code 8**, just click Install again. It usually works on the second try.
   - When it finishes, the cluster folders (`CLUSTERDIR` / `SAVESROOT` / `BACKUPROOT`) are created for you in a `ServerCluster` folder inside `SERVER_ROOT`, and the three fields are filled in automatically. SteamCMD itself never creates them, and ARK will hang on launch with no error if `CLUSTERDIR` is missing. Click **Save** on the Configuration tab so the `.bat` scripts pick them up. Setup Status has a "Cluster folders exist" row to confirm.
   - If they're ever missing, whether from an older install or because you moved or deleted them, use **"Create ServerCluster folders"** on the Configuration tab (in the Paths group) to recreate them. Existing folders are left untouched.

2. **Same tab**, click **"Install ArkServerApi."** This downloads the latest ArkApi release and extracts it into `ShooterGame\Binaries\Win64` for you, with no manual unzipping needed. When it's done, `Win64` contains `version.dll` and an `ArkApi\` folder.
   > **Note:** BattlEye must be OFF for ArkApi to work. `start_ase_server` already disables it for you.

3. Download `ArkAP_plugin.zip` (see **"Manual downloads"** at the bottom of the Server Install tab) and unzip it somewhere. Then in the **"Install ArkAP Plugin"** box, point **"Plugin source folder"** at the unzipped folder (the one containing `ArkAP\ArkAP.dll`) and click **"Install Plugin."** It copies the plugin into `Win64\ArkApi\Plugins\ArkAP` for you.
   - Leave the source box blank and it will try to auto-find the download next to the launcher or in your Downloads folder.
   - Upgrading later keeps your existing `ArkAP.config.json`.

4. **Configuration tab**, in the Paths group, click **"Scan for paths,"** or Browse to set paths by hand. If `SERVER_ROOT` isn't set yet, or doesn't look right, it finds it for you first (Steam libraries, common drive roots), then automatically scans around it for `PLUGINS_DIR` / `ipc_dir` / `game_ini` and the cluster folders.
   > **Note:** `SERVER_ROOT` is the folder that **contains** `ShooterGame`, not the folder above it. If your download put the game in a nested folder (for example, `C:\ARKServer\ARK Survival Evolved Dedicated Server\ShooterGame`), `SERVER_ROOT` is that nested folder, not `C:\ARKServer`.

   - Leaving the `SERVER_ROOT` field (once it's set) also runs a Quick scan on its own, filling in `PLUGINS_DIR` / `ipc_dir` / `game_ini` and possibly suggesting `CLUSTERDIR` / `SAVESROOT` / `BACKUPROOT`. These are typically correct. It's recommended to accept them.
   - If something wasn't found, pick a higher **"Scan intensity"** in the dropdown next to the button and click "Scan for paths" again:
     - **Quick.** Checks the exact expected sub-paths only. Instant.
     - **Thorough.** Also searches a few levels under `SERVER_ROOT`, under `ShooterGame\Saved`, and beside `SERVER_ROOT`. Takes a few seconds.
     - **Exhaustive.** Searches much deeper and additionally sweeps your Desktop, Documents, and Downloads folders, for servers extracted somewhere unusual. Can be slow. The launcher stays usable while scanning.

5. **Setup Status tab**, click **Re-check** and confirm everything shows a checkmark before going further. Anything showing an ✗ has a hint telling you what to fix. This is the fastest way to catch a missed step before troubleshooting in-game.

6. Generate the Archipelago room. This guide won't explain how YAMLs and Archipelago work, since this isn't a beginner-friendly Archipelago setup. Just remember to set up your yaml, remember your yaml name, and drop the `.apworld` into Archipelago's custom worlds.
   > **Note:** it's recommended to have `progression_tiers` on in the yaml to reduce softlocks/BKs.

7. **Configuration tab**, fill in the Connector settings (server, slot, password) with your Archipelago room info.
   > Your slot must match the name in your yaml exactly, including capitalisation.

   Copy the connection command. This is what you'll paste in-game.

8. **Quick Launch**, **"Run start_ase_server"** to launch the server. It can take a few minutes depending on your SSD/HDD speed. Confirm in the console that the plugin has loaded, or check the Debug Log tab for the LOAD line.

9. In ARK: Survival Evolved, go to LAN and look for your session name (default: `ArchipelagoSolo`). Join, spawn your character, open in-game chat, and paste the connection command from step 7.

10. You should be good to go! Quick test: level up and see if a check goes out. To test check-in: in the host's server console (the ArchipelagoServer window, or the web room's command box) run:
    ```
    /send ARCHIPELAGONAME Engram: Canteen
    ```
    Within a few seconds it should unlock in your engrams. If not, something's wrong.

Any issues: check the Debug Log tab first, then the Discord or GitHub to search for or report them.

---

## What each tab does

- **Configuration.** Every Locations / Paths / Network / Connector / Cluster field, the Quick Launch buttons, and Save / Reload from files. The Paths group also holds **"Scan intensity"** and **"Scan for paths"** (all path detection in one button), plus **"Create ServerCluster folders."**
- **Server Install.** The three installers, in order: **"Install ARK Server"** (SteamCMD, about 18GB), **"Install ArkServerApi"** (downloads and extracts the latest ArkApi into `Win64`), and **"Install Plugin"** (copies the ArkAP plugin into `ArkApi\Plugins`). "Manual downloads" at the bottom is only a fallback if something goes wrong, plus the ArkAP plugin zip and ArkConnector, which still need downloading by hand.
- **Setup Status.** A read-only checklist (server installed, ArkApi installed, plugin installed, plugin mode, `connector.ini`) with hints for anything showing an ✗. Click Re-check after fixing something.
- **Debug Log.** Live view of `ArkAP_debug.log` with a search box, "Jump to latest," and "Refresh." Check here first when checks or items aren't coming through.
- **Profiles.** Save and load named snapshots of every Configuration field (for example, "Solo Test" versus "Friend Group Run") plus a free-text notes box, stored separately from your live config. Loading a profile only fills in the Configuration fields. It never saves or applies by itself, so press Save on the Configuration tab afterward.
  - On first run, a profile named **"Profile 1"** is created from your starting Configuration values and loaded straight away, so your settings are backed by a real profile from the very beginning instead of only the live config. It's a normal profile: rename, update, or delete it as you like.
  - The list also contains an **"Autosave"** profile the launcher writes by itself every 10 minutes while the app is open. It always holds only the newest snapshot, never touches the profiles you save yourself, and can't be renamed or updated by hand. Load it if you ever need to get recent settings back.
- **Instructions.** The in-app version of this guide.

## Search (top left of the window)

Type a term and press Enter to search field labels, tooltips, button text, and the Instructions tab across every tab at once. Find Next / Find Prev cycle through all matches, switching tabs automatically and centering the match on screen.

## Quick Launch (bottom of the Configuration tab)

- **Open ipc folder / Open Plugins folder / Open Game.ini folder / Open SERVER_ROOT.** Opens the matching folder in Explorer.
- **Run start_ase_server.** Launches the main ARK server.
- **Run switch_map.** Swaps the active map, with an option to back up first.
- **Run reset_ark_test.** Wipes the test cluster/map save data.
- **Reset AP data (keep world save).** Deletes every Archipelago tracking file the plugin and connector generate, both incoming items and outgoing checks.
  > Note: if the character/world isn't also reset, level/inventory checks re-send immediately.
- **Full reset for new seed.** Does the above and also backs up and wipes the world save (`SavedArks`, your per-map saves, and the cluster tribute data). Backups are moved aside with a timestamp, never deleted. Use this when joining a new seed. Stop the ARK server and the connector first.
  - It no longer just says "done" and hopes. Every backup is checked to confirm it actually received files (an empty one is flagged, not counted), then it re-scans every live save location afterward and fails loudly if any world or character file survived. If nothing at all was found to reset, you get a warning rather than a success. From your side that means a reset that didn't happen, so run `tools\diagnose_reset.bat` before starting the server. Only a run with no problems, and at least one save actually wiped, reports success.
- **Run apply_server_config.** Re-applies the saved config to the install.

## Uploading your own Game.ini / GameUserSettings.ini

**Configuration tab**, **"Upload server config files"** (below the field groups). Point a row at your own copy of `Game.ini` and/or `GameUserSettings.ini` and click **"Upload to server"** to copy it into `<SERVER_ROOT>\ShooterGame\Saved\Config\WindowsServer`, replacing the server's.

- Each file it replaces is backed up first, alongside the original with a timestamp in the name, so nothing is deleted. You can always restore the old one by renaming it back.
- **Stop the ARK server first.** ARK rewrites its config files (`GameUserSettings.ini` especially) when it shuts down, so anything uploaded while it's running is likely to be lost. You'll get a warning if the server is up. Restart the server afterward for the new settings to apply.

## What the path fields feed

`SERVER_ROOT`, `SAVESROOT`, `CLUSTERDIR`, `BACKUPROOT`, `CLUSTERID`, `ADMINPASS`, and `SERVERPASS` all write into a single file, `paths.cmd`. `start_ase_server.bat`, `switch_map.bat`, `start_transfer_server.bat`, and `reset_ark_test.bat` all read it from there, so they can never disagree. `apply_server_config.bat` keeps its own `SERVER_ROOT` copy. `MAP`, `SESSION`, `MAXPLAYERS`, ports, and `TRIBUTEEXP` write only into `start_ase_server.bat`, since those are per-script settings.

Connector fields write into `connector.ini`.

Save only rewrites the one matching line for each field in each file. Everything else in the script is left untouched.

## Other information

- If you want to restart your world for a new Archipelago seed, click **"Full reset for new seed"** under Quick Launch. Stop the ARK server and the connector first.
- If you randomized dinos, find the fragment file for it by opening the ipc folder under Quick Launch: `game_ini_fragment.txt`. Paste it at the top of your `Game.ini` file.
