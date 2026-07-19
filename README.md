#### Pro tip: most options have tooltips if you hover over them!
#### Pro tip: the Search bar (top left) searches field labels, tooltips, and text across every tab - press Enter, then use Find Next / Find Prev to jump between matches.
## Start here - install in this order
The three installs below must happen in order: the ARK server first, then ArkServerApi into it, then the ArkAP plugin into ArkApi. Each one needs the previous one to already exist. All three live on the Server Install tab.
1. Server Install tab -> set SERVER_ROOT (the folder the server gets installed into), then click "Install ARK Server". This downloads ~18gb via SteamCMD - progress shows in the console box below the buttons, and "Cancel" stops it if you need to. Note: make sure your ARK: Survival Evolved game is on the preaquatica branch, or you won't be able to join.
   If it fails with exit code 8, just click Install again - it usually works on the second try.
2. Same tab -> click "Install ArkServerApi". This downloads the latest ArkApi release and extracts it into ShooterGame\Binaries\Win64 for you - no manual unzipping. When it's done, Win64 contains version.dll and an ArkApi\ folder. Note: BattlEye must be OFF for ArkApi to work, but start_ase_server already disables it for you - nothing to do.
3. Download ArkAP_plugin.zip (see "Manual downloads" at the bottom of the Server Install tab) and unzip it somewhere. Then in the "Install ArkAP Plugin" box, point "Plugin source folder" at the unzipped folder (the one containing ArkAP\ArkAP.dll) and click "Install Plugin". It copies the plugin into Win64\ArkApi\Plugins\ArkAP for you. Leave the source box blank and it will try to auto-find the download next to the launcher or in your Downloads folder. Upgrading later keeps your existing ArkAP.config.json.
4. Configuration tab -> click "Auto-detect...", or Browse to set paths by hand. Note: SERVER_ROOT is the folder that CONTAINS ShooterGame, not the folder above it. e.g. E:\ARK\Server\ARK Survival Evolved Dedicated Server, NOT E:\ARK\Server.
   Leaving the SERVER_ROOT field runs a scan that fills in PLUGINS_DIR / ipc_dir / game_ini, and may suggest CLUSTERDIR / SAVESROOT / BACKUPROOT. These are typically correct - it's recommended to accept them.
5. Setup Status tab -> click Re-check and confirm everything shows a checkmark before going further. Anything showing an X has a hint telling you what to fix. This is the fastest way to catch a missed step before you start troubleshooting in-game.
6. Generate the Archipelago room. This guide won't explain how YAMLs and Archipelago work - this isn't a beginner-friendly Archipelago setup. Just remember to set up your yaml, remember your yaml name, and drop the .apworld into Archipelago's custom worlds. Note: it's recommended to have progression_tiers on in the yaml to reduce softlocks/BKs.
7. Configuration tab -> fill in the Connector settings (server, slot, password) with your Archipelago room info. Your slot must match the name in your yaml exactly, including capitalisation. Copy the connection command - this is what you'll paste in-game.
8. Quick Launch -> "Run start_ase_server" to launch the server. It can take a few minutes depending on your SSD/HDD speed. Confirm in the console that the plugin has loaded (or check the Debug Log tab for the LOAD line).
9. In ARK: Survival Evolved, go to LAN and look for your session name (default: ArchipelagoSolo). Join, spawn your character, open in-game chat, and paste the connection command from step 7.
10. You should be good to go! Quick test: level up and see if a check goes out. To test check-in: in the host's server console (the ArchipelagoServer window, or the web room's command box) run /send ARCHIPELAGONAME Engram: Canteen - within a few seconds it should unlock in your engrams. If not, uh oh 
Any issues: check the Debug Log tab first, then the Discord or GitHub to search for or report them.
## What each tab does
Configuration - every Locations / Paths / Network / Connector / Cluster field, the Quick Launch buttons, and Save / Reload from files.

Server Install - the three installers, in order: "Install ARK Server" (SteamCMD, ~18gb), "Install ArkServerApi" (downloads + extracts the latest ArkApi into Win64), and "Install Plugin" (copies the ArkAP plugin into ArkApi\Plugins). "Manual downloads" at the bottom is only a fallback if something goes wrong, plus the ArkAP plugin zip and ArkConnector, which still need downloading by hand.

Setup Status - a read-only checklist (server installed / ArkApi installed / plugin installed / plugin mode / connector.ini) with hints for anything showing an X. Click Re-check after fixing something.

Debug Log - live view of ArkAP_debug.log with a search box, "Jump to latest", and "Refresh". Check here first when checks or items aren't coming through.

Profiles - save/load named snapshots of every Configuration field (e.g. "Solo Test" vs "Friend Group Run") plus a free-text notes box, stored separately from your live config. Loading a profile only fills in the Configuration fields - it never saves/applies by itself, so press Save on the Configuration tab afterward.

Instructions - this tab.
## Search (top left of the window)
Type a term and press Enter to search field labels, tooltips, button text, and this Instructions tab across every tab at once.

Find Next / Find Prev cycle through all matches, switching tabs automatically and centering the match on screen.
## Quick launch (bottom of the Configuration tab)

Open ipc folder / Open Plugins folder / Open Game.ini folder / Open SERVER_ROOT - open the matching folder in Explorer.

Run start_ase_server - launches the main ARK server.

Run switch_map - swaps the active map (optionally backing up first).

Run reset_ark_test - wipes the test cluster/map save data.

Reset AP data (keep world save) - deletes every Archipelago tracking file the plugin and connector generate (both incoming items AND outgoing checks). Note: if the character/world isn't also reset, level/inventory checks re-send immediately.

Full reset for new seed - does the above AND backs up + wipes the world save. Use this when joining a new seed. Stop the ARK server (and the connector) first.

Run apply_server_config - re-applies the saved config to the install.
## What the path fields feed
Paths / Network / Cluster fields write into start_ase_server.bat, switch_map.bat, start_transfer_server.bat, reset_ark_test.bat, and apply_server_config.bat.

Connector fields write into connector.ini.

Save only rewrites the one matching line for each field in each file - everything else in the script is left untouched.
## Other Information
If you want to restart your world for a new Archipelago seed, click "Full reset for new seed" under Quick Launch (stop the ARK server and the connector first).

If you randomized dinos, find the txt file for it by opening the ipc folder under Quick Launch: game_ini_fragment.txt then paste it at the top of your Game.ini file.
