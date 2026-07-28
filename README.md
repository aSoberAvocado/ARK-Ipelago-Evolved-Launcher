Ghios developed the entire plugin [here](https://github.com/Jbaker16163/Ark-Survival-Archipelago). This is just a Community made launcher built to make setup as easy as possible
# ARKipelago Launcher : Setup Guide

> Pro tip: most options have tooltips if you hover over them.
> The Search bar (top left) searches field labels, tooltips, and text across every tab. press Enter, then use Find Next / Find Prev to jump between matches.
> Each numbered step below has a checkbox. Tick it and the step collapses to just "Step 1", "Step 2" and so on; untick it to get the full text back. "Collapse all steps" / "Expand all steps" at the top of this tab do the lot at once, handy for ticking off steps as you go, or for skimming back to one step.

## Start here. install in this order

The three installs below must happen in order: the ARK server first, then ArkServerApi into it, then the ArkAP plugin into ArkApi. Each one needs the previous one to already exist. All three live on the **Server Install** tab.

1. **Server Install tab** → set `SERVER_ROOT` (the folder the server gets installed into), then click **"Install ARK Server."** This downloads ~18GB via SteamCMD, progress shows in the console box below the buttons, and "Cancel" stops it if you need to.
   > **Note:** make sure your ARK: Survival Evolved game is on the **preaquatica** branch, or you won't be able to join.

   - If it fails with **exit code 8**, just click Install again, it usually works on the second try.
   - When it finishes, the cluster folders (`CLUSTERDIR` / `SAVESROOT` / `BACKUPROOT`) are created for you in a `ServerCluster` folder inside `SERVER_ROOT`, and the three fields are filled in with them. SteamCMD itself never creates them, and ARK hangs on launch with no error if `CLUSTERDIR` is missing. Click **Save** on the Configuration tab so the `.bat` scripts pick them up. Setup Status has a "Cluster folders exist" row to confirm.
   - If they're ever missing, an older install, or you moved or deleted them, use **"Create ServerCluster folders"** on the Configuration tab (in the Paths group) to create them again. Folders that already exist are left untouched.

2. **Server Install tab** → click **"Install ArkServerApi."** This downloads the latest ArkApi release and extracts it into `ShooterGame\Binaries\Win64` for you, no manual unzipping. When it's done, `Win64` contains `version.dll` and an `ArkApi\` folder.
   > **Note:** BattlEye must be OFF for ArkApi to work, but `start_ase_server` already disables it for you.

3. **Same tab** → in the **"Install ArkAP Plugin"** box, click **"Install Plugin."** It downloads the latest `ArkAP_Plugin.zip` straight from GitHub and extracts it into `Win64\ArkApi\Plugins\ArkAP` for you, no manual download/unzip. Progress shows in the console box. Upgrading later keeps your existing `ArkAP.config.json`.
   > If the download ever fails, or you want a specific older version, use **"Manual downloads"** at the bottom of the tab instead.

4. **Configuration tab** → in the Paths group, click **"Scan for paths,"** or Browse to set paths by hand. If `SERVER_ROOT` isn't set yet, or doesn't look right, it finds it for you first (Steam libraries, common drive roots), then automatically scans around it for `PLUGINS_DIR` / `ipc_dir` / `game_ini` and the cluster folders.
   > **Note:** `SERVER_ROOT` is the folder that **CONTAINS** `ShooterGame`, not the folder above it. If your download put the game in a nested folder (e.g. `C:\ARKServer\ARK Survival Evolved Dedicated Server\ShooterGame`), `SERVER_ROOT` is that nested folder, not `C:\ARKServer`.

   - Leaving the `SERVER_ROOT` field (once it's set) also runs a Quick scan on its own, filling in `PLUGINS_DIR` / `ipc_dir` / `game_ini` and possibly suggesting `CLUSTERDIR` / `SAVESROOT` / `BACKUPROOT`. These are typically correct, it's recommended to accept them.
   - If something wasn't found, pick a higher **"Scan intensity"** in the dropdown next to the button and click "Scan for paths" again:
     - **Quick** : checks the exact expected sub-paths only, instant.
     - **Thorough** : also searches a few levels under `SERVER_ROOT`, under `ShooterGame\Saved`, and beside `SERVER_ROOT`, a few seconds.
     - **Exhaustive** : searches much deeper and additionally sweeps your Desktop, Documents, and Downloads folders, for servers extracted somewhere odd. Can be slow, the launcher stays usable while scanning.

5. **Setup Status tab** → click **Re-check** and confirm everything shows a checkmark before going further (you'll have an X for connector, that's addressed in step 6). Anything showing an X has a hint telling you what to fix. This is the fastest way to catch a missed step before you start troubleshooting in-game.

6. Generate the Archipelago room. This guide won't explain how YAMLs and Archipelago work, this isn't a beginner-friendly Archipelago setup. Just remember to set up your yaml, remember your yaml name, and drop the `.apworld` into Archipelago's custom worlds.
   > **Note:** it's recommended to have `progression_tiers` on in the yaml to reduce softlocks/BKs.

7. **Configuration tab** → fill in the Connector settings (server, slot, password) with your Archipelago room info.
   > Your slot must match the name in your yaml exactly, including capitalisation.

   Copy the connection command, this is what you'll paste in-game.

8. **Quick Launch** → **"Run start_ase_server"** to launch the server. It can take a few minutes depending on your SSD/HDD speed. Confirm in the console that the plugin has loaded (or check the Debug Log tab for the LOAD line).

9. In ARK: Survival Evolved, go to LAN and look for your session name (default: `ArchipelagoSolo`). Join, spawn your character, open in-game chat, and paste the connection command from step 7.

10. You should be good to go! (If randomized dino spawns are enabled, see the bottom of this guide.) Quick test: level up and see if a check goes out. To test check-in: in the host's server console (the ArchipelagoServer window, or the web room's command box) run:
    ```
    /send ARCHIPELAGONAME Engram: Canteen
    ```
    Within a few seconds it should unlock in your engrams. If not, something's wrong.

Any issues: check the Debug Log tab first, then the Discord or GitHub to search for or report them.

---

## What each tab does

- **Configuration** : every Locations / Paths / Network / Connector / Cluster field, the Quick Launch buttons, and Save / Reload from files. The Paths group also holds **"Scan intensity"** + **"Scan for paths"** (all path detection in one button) and **"Create ServerCluster folders."**
- **Server Install** : the three installers, in order: **"Install ARK Server"** (SteamCMD, ~18GB), **"Install ArkServerApi"** (downloads + extracts the latest ArkApi into `Win64`), and **"Install Plugin"** (downloads the latest `ArkAP_Plugin.zip` from GitHub and installs it into `ArkApi\Plugins`). "Manual downloads" at the bottom is only a fallback if an automated download fails (or you want a specific older plugin version), plus ArkConnector, which still needs downloading by hand.
- **Setup Status** : a read-only checklist (server installed / ArkApi installed / plugin installed / plugin mode / `connector.ini`) with hints for anything showing an X. Click Re-check after fixing something. It also shows advisory rows (a yellow "i", not a red X) for things worth knowing but not broken, the BattlEye note, and "update available" when a newer ArkServerApi or ArkAP plugin release exists than the one installed (with a link to the release). An older component version isn't a failure, so it never shows an X.
  - The Setup Status tab has a small coloured symbol next to its name in the tab bar, so you can see your overall status from any tab without opening it: a green check means everything passes, a yellow "i" means no failures but at least one advisory (BattlEye, or a newer component version), a red X means at least one hard failure. It updates whenever the checks re-run.
- **Debug Log** : live view of `ArkAP_debug.log` with a search box, "Jump to latest," and "Refresh." Check here first when checks or items aren't coming through.
- **Profiles** : save/load named snapshots of every Configuration field (e.g. "Solo Test" vs "Friend Group Run") plus a free-text notes box, stored separately from your live config. Loading a profile only fills in the Configuration fields, it never saves/applies by itself, so press Save on the Configuration tab afterward.
  - On first run a profile named **"Profile 1"** is created from your starting Configuration values and loaded straight away, so your settings are backed by a real profile from the very beginning instead of only the live config. It's a normal profile, rename, update, or delete it as you like.
  - The list also contains an **"Autosave"** profile the launcher writes by itself every 10 minutes while the app is open. It always holds only the newest snapshot, never touches the profiles you save yourself, and can't be renamed or updated by hand, load it if you ever need to get recent settings back.
- **Instructions** : this tab.

## Search (top left of the window)

Type a term and press Enter to search field labels, tooltips, button text, and this Instructions tab across every tab at once. Find Next / Find Prev cycle through all matches, switching tabs automatically and centering the match on screen.

## Quick Launch (bottom of the Configuration tab)

- **Open ipc folder / Open Plugins folder / Open Game.ini folder / Open SERVER_ROOT / Open ClusterDir folder** : open the matching folder in Explorer.
- **Run start_ase_server** : launches the main ARK server.
- **Run switch_map** : swaps the active map (optionally backing up first).
- **Patch Game.ini for randomized creatures** : applies the plugin's `ipc\game_ini_fragment.txt` into your `Game.ini` (backed up first) so randomized creatures take effect. Stop the ARK server first.
- **Reset AP data (keep world save)** : deletes every Archipelago tracking file the plugin and connector generate (both incoming items AND outgoing checks).
  > Note: if the character/world isn't also reset, level/inventory checks re-send immediately.
- **Full reset for new seed** : does the above AND backs up + wipes the world save (`SavedArks`, your per-map saves, and the cluster tribute data). It also removes the randomized-creatures block from `Game.ini` if this launcher added one (backed up first), so a fresh seed doesn't inherit the previous seed's dino randomization. Backups are moved aside with a timestamp, never deleted. Use this when joining a new seed. Stop the ARK server (and the connector) first.
  - It no longer just says "done" and hopes. Every backup is checked to confirm it actually received files (an empty one is flagged, not counted), then it re-scans every live save location afterward and fails loudly if any world or character file survived. If nothing at all was found to reset, you get a warning rather than a success, from your side that's a reset that didn't happen, and you should run `tools\diagnose_reset.bat` before starting the server. Only a run with no problems AND at least one save actually wiped reports success.

## Uploading your own Game.ini / GameUserSettings.ini

**Configuration tab** → **"Upload server config files"** (below the field groups). Point a row at your own copy of `Game.ini` and/or `GameUserSettings.ini` and click **"Upload to server"** to copy it into `<SERVER_ROOT>\ShooterGame\Saved\Config\WindowsServer`, replacing the server's.

- Each file it replaces is backed up first, alongside the original with a timestamp in the name, nothing is deleted, so you can always put the old one back by renaming it.
- Stop the ARK server first. ARK rewrites its config files (`GameUserSettings.ini` especially) when it shuts down, so anything uploaded while it's running is likely to be lost, you'll get a warning if the server is up. Restart the server afterward for the new settings to apply.

## What the path fields feed

`SERVER_ROOT` / `SAVESROOT` / `CLUSTERDIR` / `BACKUPROOT` / `CLUSTERID` / `ADMINPASS` / `SERVERPASS` all write into a single file, `paths.cmd`, `start_ase_server.bat`, `switch_map.bat`, `start_transfer_server.bat`, and `reset_ark_test.bat` all read it from there, so they can never disagree. `apply_server_config.bat` keeps its own `SERVER_ROOT` copy. `MAP` / `SESSION` / `MAXPLAYERS` / ports / `TRIBUTEEXP` write only into `start_ase_server.bat`, since those are per-script settings.

Connector fields write into `connector.ini`.

Save only rewrites the one matching line for each field in each file, everything else in the script is left untouched.

## Reporting a problem (diagnostics & crash log)

- **Export diagnostics** : a button next to Save / Reload on the Configuration tab. It bundles `ArkAP_debug.log`, a text summary of the Setup Status checks, a copy of your config with the passwords removed (`ADMINPASS`, `SERVERPASS`, and the connector password show as `[REDACTED]`, everything else is kept so it's still useful), and the crash log if there is one, into a single `.zip`. It saves to your Desktop by default (you pick where) and opens the folder when it's done. Drag that zip straight into Discord or attach it to a GitHub issue when asking for help, it's the fastest way to get diagnosed.
- **Crash log** : if the launcher ever hits an unexpected error, it shows a "Something went wrong" message and writes the full details to `arkap_launcher_crash.log`, saved next to the launcher's `.exe` (the same folder as `arkap_launcher_config.json`). It's kept across restarts (new crashes are appended, with a size cap so it can't grow forever), so it survives even if the app crashes again before you get to report the first one. When reporting a crash, attach that file, or just use "Export diagnostics," which already includes it.

## Other information

- If you want to restart your world for a new Archipelago seed, click **"Full reset for new seed"** under Quick Launch (stop the ARK server and the connector first).
- If you randomized dinos, stop the ARK server and click **"Patch Game.ini for randomized creatures"** under Quick Launch. It applies the plugin's `ipc\game_ini_fragment.txt` into your `Game.ini` for you (backing it up first, and merging into an existing `[/script/shootergame.shootergamemode]` section rather than duplicating it), no more copy-pasting it by hand. Restart the server afterward.
  > The fragment only exists once you've connected to the server at least once on a randomized seed.



**Thank you to Ghios, Beeno, Lurch9229, and Wizard_Brandon for helping test and put the entire ARK archipelago together**
