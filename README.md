This is just a Community made launcher built to make setup as easy as possible

Ghios developed the entire plugin [here](https://github.com/Jbaker16163/Ark-Survival-Archipelago).

Lurch9229 developed the Pop Tracker [here](https://github.com/lurch9229/Arkipelago-Poptracker/releases/latest)

# ARKipelago Launcher : Setup Guide

> **This launcher will never touch your actual ARK download location. Please don't set any path to your ARK game install, i beg you.**

Most options have tooltips, just hover over one. Use the Search bar at the top left to find anything in the app.

---

## Start here, install in this order

Do these steps in order. Each step needs the one before it.

### 1. Install the ARK server

Open the **Install Server/Api/Plugin** tab. Set `SERVER_ROOT`. Click **"Install ARK Server."**

- You can install it anywhere. A short path near the top of a drive, like `C:\ark\`, keeps things simple.
- The download is about 18gb. Progress shows in the console box. Wait for it to finish.
- Set your ARK: Survival Evolved game to the **preaquatica** branch. You cannot join the server otherwise.
- The cluster folders are created and filled in for you when it finishes. Go to the **Configuration** tab and click **Save**.

### 2. Install ArkServerApi

Stay on the **Install Server/Api/Plugin** tab. Click **"Install ArkServerApi."** Wait for it to finish.

- Your ARK game also needs BattlEye off. In Steam, right-click ARK: Survival Evolved, open Properties, then Launch Options, and add `-NoBattlEye`.

### 3. Install the ArkAP plugin

Stay on the same tab. Find the **"Install/update ArkAP Plugin"** box. Click **"Install Plugin."** Wait for it to finish.

### 4. Set your paths

Open the **Configuration** tab. In the Paths group, click **"Scan for paths."** Accept the paths it fills in. Click **Save**.

- `SERVER_ROOT` is the folder that contains `ShooterGame`.
- The scan shows its suggestions in a popup. Click a suggestion to accept it. If one looks wrong, close the popup and use Browse to set that path yourself.
- The popup scrolls if the scan found a lot of folders.

### 5. Check your setup

Open the **Setup Status** tab. Click **Re-check**.

- Every row should show a green checkmark before you carry on.
- A yellow "i" is advisory only and is typically nothing to worry about. A red X tells you what to fix.
- Come back here after any change. It also checks that your settings were saved into the server scripts, and that your ticked mods match what the server will really load.

### 6. Set up your Archipelago room

This guide assumes you already know how Archipelago and YAMLs work.

- Open the **Archipelago Setup** tab. Set your Archipelago directory, or click **"Scan for Archipelago."**
- Click **"Update .apworld"** to install `ark_ase.apworld`.
- Click **"Open Options Creator (YAML)"** to build your yaml. Pick ARK from its game list.
- Click **"Export Options"** in the top right of the Options Creator to save your yaml.
- Write down your slot name. You need it in step 7.
- Click **"Open Players folder"** and put your yaml in there.
- Click **"Generate seed."**
- Click **"Open output folder"** to find your generated seed.

#### Hosting the seed

Now host that seed. Two options, pick one, not both.

**Option A:** upload the `.zip` to archipelago.gg and let the website host it. Easiest. It gives you the server address for step 7.

**Option B:** click **"Host local Archipelago server"** to host it on this PC. Pick the seed when it asks. It opens in its own console window, leave that window open, closing it ends the room.

- Option B fills in the server field for you, so step 7 is just your slot name. It asks first if you had already typed something there.
- Option B uses your room password from step 7, and the port from Archipelago's own `host.yaml`. Change the port there if you need a different one.
- **Option B warning:** other players connect to your IP, not localhost. Anyone outside your home network needs you to forward port `38281` (TCP) to this PC on your router. Use archipelago.gg if that sounds like a hassle.

#### Optional: a live tracker map

- In the **"PopTracker (tracker)"** group on the same tab, click **"Download PopTracker"** and pick a folder. It downloads PopTracker, installs the ARK tracker pack into it, and fills in the directory for you. Click Save.
- Already have PopTracker? Set the **"PopTracker directory"** (or click **"Scan for PopTracker"**) and click **"Install/update ARK tracker pack"** instead. Your old copy of the pack is moved aside.
- Click **"Open PopTracker"** to open it on the ARK map.
- If connector setting are filled in the PopTracker opens already connected.
- If it does not connect automatically click the grey **"AP"** at the top of the poptracker, paste the address with Ctrl+V, then type your slot and password. It remembers them for next time, apart from the password.

### 7. Fill in your room details

Stay on the **Archipelago Setup** tab. Find the **"Archipelago room (Connector settings)"** group. Fill in server, slot and password.

- Type your slot name exactly as it appears in your yaml. Capital letters matter.
- Click **"Copy ARK connection command."** You paste this in-game later.
- Click **"Open Text Client"** to open the Archipelago text client already connected.

### 8. Start the server

Open the **Configuration** tab. Under Quick Launch, click **"Run start_ase_server."**

- Wait for the console to finish printing its startup messages before assuming something's wrong (this can take a while.. up to 900s if ran from a hard drive.). Don't click inside the console window while it's starting.
- If the console's title bar starts with "Select", it has frozen. Press Enter to unfreeze it.

### 9. Join the game

Start ARK: Survival Evolved. Open the LAN server list. Join your session. The default name is `ArchipelagoSolo`.

- Spawn your character. Open in-game chat. Paste the command from step 7.

### 10. You're done

Level up once to send your first check.

- To test items, run `/send ARCHIPELAGONAME Engram: Compass` in your Archipelago server console. The engram should unlock within a few seconds.
- Randomized dinos need one extra step. See "Other information" below.

---

## What each tab does

| Tab | What it's for |
|---|---|
| **Configuration** | All your settings, the Quick Launch buttons, and Save. |
| **Install Server/Api/Plugin** | The three installers, in order. |
| **Archipelago Setup** | Your Archipelago folder, your room details, and buttons that open Archipelago's own tools. It remembers every field between sessions, and they travel with your profiles. The tab has its own Save button. The "PopTracker (tracker)" group at the bottom is optional. |
| **Mods** | Download and turn on Steam Workshop mods. |
| **Setup Status** | A checklist of your setup. Click Re-check after you fix something. |
| **Profiles** | Save and load named copies of your settings. Click Save on the Configuration tab after you load one. |
| **Debug Log** | A viewer for your logs. The "Log:" dropdown picks which one: the ArkAP plugin's log, the launcher's own log, the launcher crash log, ARK's `ShooterGame.log`, or SteamCMD's download logs. |
| **Instructions** | The in-app version of this guide, with a Quick Guide and a Full Guide. |

---

## Saving your changes

- A Save button glows yellow while something on screen is unsaved.
- A plain Save button means everything already matches what is saved.
- There are three, and each one glows only for its own fields: Configuration, Archipelago Setup, and Mods.
- Save matters. The server and the `.bat` scripts read your settings from files, and Save is what writes them there.
- Forget to save and Run start_ase_server refuses to start, and Setup Status shows a red X. Click Save and try again.

---

## Check for Updates (top of the window)

- It checks the launcher, the ArkAP plugin, the `.apworld` and the ARK tracker pack.
- It runs by itself every time you start the launcher.
- A marker and a highlight on the button mean something newer exists. Click it to see what.
- The launcher updates itself. For the other three the dialog names the button that installs them.

---

## Mods (Steam Workshop)

- The **Mods** tab installs Steam Workshop mods for you.
- Install the ARK server and set `SERVER_ROOT` first.
- Tick a mod to mark it active.
- Click **"Download checked"** to install and activate every ticked mod.
- Mods load from top to bottom. Use the arrows to change the order.
- Click **Save** to write your ticked list to the server.
- Setup Status tells you if your ticks and the server's real mod list have drifted apart.
- Restart the ARK server after any mod change.
- Click **"Copy IDs for YAML"** to copy your mod list for the plugin's yaml. Only mods tagged "apworld ✓" are copied, the others would stop your game generating. They still work on the server unless they have engrams, the engrams wont be included in the pool of possible items.
- Click **"Rename mod"** to give a mod you added yourself a name you will recognise instead of a bare ID.

---

## Quick Launch (bottom of the Configuration tab)

- **Run start_ase_server** — starts the server.
- **The Open buttons** — open that folder in Explorer.
- **Run switch_map** — not supported right now.
- **Patch Game.ini for randomized creatures** — turns on randomized dinos. Stop the server first.
- **Reset AP data (keep world save)** — clears your Archipelago progress and keeps your world.
- **Full reset for new seed** — clears your Archipelago progress and wipes your world save. Use it when you join a new seed. Stop the server first. Your old save is backed up.

---

## Uploading your own Game.ini / GameUserSettings.ini

1. Stop the ARK server first.
2. Open the **Configuration** tab. Find **"Upload server config files."**
3. Pick your file. Click **"Upload to server."**
4. The file it replaces is backed up first.
5. Restart the server.

---

## What the path fields feed

- The path fields write into the launcher's `.bat` and `.ini` files for you.
- Click Save after you change any field.
- The Archipelago directory field only tells the launcher where Archipelago is installed.
- The PopTracker directory field is the same, it only says where PopTracker is, so the tracker pack goes in the right place.

---

## Reporting a problem

- Click **"Export diagnostics"** next to Save on the Configuration tab.
- It saves one `.zip` and opens the folder. Post that zip on Discord or attach it to a GitHub issue.
- Your yaml is found by reading the name inside each file in your Players folder and matching it to your slot, so what the file is called does not matter.
- The zip holds your Setup Status, your version numbers, your config, your Archipelago `.yaml`, `paths.cmd`, `Game.ini` and `GameUserSettings.ini`, the plugin's `ArkAP.config.json`, the debug, crash and ShooterGame logs, everything in your ipc folder (including each player's mailbox folder), and a list of your ipc and Mods folders. That is everything anyone would ask you for.
- Every password in every one of those files is replaced with `[REDACTED]` before it goes in. Very long logs are cut down to their last 5000 lines, and the ipc files to their last 500, so the zip stays small.

---

## Other information

- To start a new seed, click **"Full reset for new seed"** under Quick Launch. Stop the server and the connector first.
- If you randomized dinos, stop the server. Click **"Patch Game.ini for randomized creatures"** under Quick Launch. Restart the server.
- That button only works after you have connected to the server once on a randomized seed.

---

## If something goes wrong

| Problem | Fix |
|---|---|
| The server install stopped with exit code 8 | Click "Install ARK Server" again. |
| "Scan for paths" missed a path | Pick a higher "Scan intensity" next to the button and scan again. |
| Your cluster folders are missing | Click "Create ServerCluster folders" in the Paths group on the Configuration tab. |
| The connection command fails in-game | The order is `/connect server slot password`. Copy it again from the Archipelago Setup tab. |
| Checks or items are not coming through | Open the Debug Log tab (it opens on the ArkAP plugin log). |
| The server closed by itself | Debug Log tab, switch the "Log:" dropdown to "ARK server log" and search for `LowLevelFatalError`. |
| Something the launcher did went wrong | Debug Log tab, "Log:" dropdown, "Launcher log". It lists what the app did, with times. |
| The server will not start and it says something is unsaved | Open the Configuration tab and click Save. |
| A mod you ticked is not loading in game | Open the Mods tab, click Save, and restart the server. |
| Setup Status shows a red X | Read the hint on that row and fix that one thing. |
| Still stuck | Click "Export diagnostics" and post the zip on Discord or GitHub. |

---

**Thank you to Ghios, Beeno, Lurch9229, and Wizard_Brandon for helping test and put the entire ARK archipelago together**
