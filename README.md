Ghios developed the entire plugin [here](https://github.com/Jbaker16163/Ark-Survival-Archipelago). This is just a Community made launcher built to make setup as easy as possible

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

### 5. Check your setup

Open the **Setup Status** tab. Click **Re-check**.

- Every row should show a green checkmark before you carry on.
- A yellow "i" is advisory only and is typically nothing to worry about. A red X tells you what to fix.

### 6. Set up your Archipelago room

This guide assumes you already know how Archipelago and YAMLs work.

- Open the **Archipelago Setup** tab. Set your Archipelago directory, or click **"Scan for Archipelago."**
- Click **"Update .apworld"** to install the apworld.
- Click **"Open Options Creator (YAML)"** to build your yaml. Pick ARK from its game list.
- Click **"Export Options"** in the top right of the Options Creator to save your yaml.
- Write down your slot name. You need it in step 7.
- Click **"Open Players folder"** and put your yaml in there.
- Click **"Generate seed."**
- Click **"Open output folder"** to find your generated seed.

### 7. Fill in your room details

Stay on the **Archipelago Setup** tab. Find the **"Archipelago room (Connector settings)"** group. Fill in server, slot and password.

- Type your slot name exactly as it appears in your yaml. Capital letters matter.
- Click **"Copy ARK connection command."** You paste this in-game later.
- Click **"Open Text Client"** to open the Archipelago text client already connected.

### 8. Start the server

Open the **Configuration** tab. Under Quick Launch, click **"Run start_ase_server."**

- Wait for the console to finish printing its startup messages before assuming something's wrong. Don't click inside the console window while it's starting.
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
| **Archipelago Setup** | Your Archipelago folder, your room details, and buttons that open Archipelago's own tools. |
| **Mods** | Download and turn on Steam Workshop mods. |
| **Setup Status** | A checklist of your setup. Click Re-check after you fix something. |
| **Profiles** | Save and load named copies of your settings. Click Save on the Configuration tab after you load one. |
| **Debug Log** | A live view of the plugin log. |
| **Instructions** | The in-app version of this guide. Use the button in the top right to switch to the Full Guide. |

---

## Mods (Steam Workshop)

The **Mods** tab installs Steam Workshop mods for you.

- Install the ARK server and set `SERVER_ROOT` first.
- Tick a mod to mark it active.
- Click **"Download checked"** to install and activate every ticked mod.
- Mods load from top to bottom. Use the arrows to change the order.
- Click **Save** to write your ticked list to the server.
- Restart the ARK server after any mod change.
- Click **"Copy active IDs"** to copy your mod list for the plugin's yaml.

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

## Reporting a problem

- Click **"Export diagnostics"** next to Save on the Configuration tab.
- It saves one `.zip` and opens the folder. Your passwords are removed from it.
- Post that zip on Discord or attach it to a GitHub issue.

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
| Checks or items are not coming through | Open the Debug Log tab. |
| Setup Status shows a red X | Read the hint on that row and fix that one thing. |
| Still stuck | Click "Export diagnostics" and post the zip on Discord or GitHub. |

---

**Thank you to Ghios, Beeno, Lurch9229, and Wizard_Brandon for helping test and put the entire ARK archipelago together**
