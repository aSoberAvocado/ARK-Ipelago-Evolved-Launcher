This is just a Community made launcher built to make setup as easy as possible

Ghios developed the entire plugin [here](https://github.com/Jbaker16163/Ark-Survival-Archipelago).

Lurch9229 developed the Pop Tracker [here](https://github.com/lurch9229/Arkipelago-Poptracker/releases/latest)

# ARKipelago Launcher : Setup Guide

> **This launcher will never touch your actual ARK download location. Please don't set any path to your ARK game install, i beg you.**

Most options have tooltips, just hover over one. Use the Search bar at the top left to find anything in the app.

> **Only joining a friend's ARK server?** You don't need any of the setup below. Skip to [Joining a friend's ARK server](#joining-a-friends-ark-server).

---

## Start here

The launcher opens on the **Get Started** tab. It walks you through everything below one step at a time, and each step shows whether it is done, what it is waiting on, and one button to move forward. You can follow the tab on its own, this guide covers the same steps in more detail.

Want the guide open next to the launcher? Use the pop-out button on the Instructions tab to open it in its own window.

### 1. Install the ARK server

On **Get Started**, set `SERVER_ROOT` and click **"Install ARK Server."**

- Pick an empty folder. The launcher installs the server into it for you. It's normal for the launcher to say there's no server there yet, that's what this step fixes.
- You can install it anywhere. A short path near the top of a drive, like `C:\ark\`, keeps things simple. Avoid `Program Files`, since the server can't write its saves there.
- The download is about 18gb and needs about 20gb free. Progress shows in the console box. Wait for it to finish.
- Set your ARK: Survival Evolved game to the **preaquatica** branch. You cannot join the server otherwise.
- The cluster folders are created and filled in for you when it finishes.

### 2. Install ArkServerApi

Click **"Install ArkServerApi"** on the next step. Wait for it to finish.

- Your ARK game also needs BattlEye off. In Steam, right-click ARK: Survival Evolved, open Properties, then Launch Options, and add `-NoBattlEye`.

### 3. Install the ArkAP plugin

Click **"Install Plugin."** Wait for it to finish.

### 4. Set your paths

Most of this is done for you by the earlier steps. If anything is still missing, open the **Configuration** tab, expand the Paths section and click **"Scan for paths."** Accept the paths it fills in. Click **Save**.

- `SERVER_ROOT` is the folder that contains `ShooterGame`.
- The scan shows its suggestions in a popup. Click a suggestion to accept it. If one looks wrong, close the popup and use Browse to set that path yourself.
- The popup scrolls if the scan found a lot of folders.

### 5. Check your setup

Scroll to the bottom of **Get Started** to find the full check list. Click **Re-check**.

- Every row should show a green checkmark before you carry on.
- A yellow "i" is advisory only and is typically nothing to worry about. A red X tells you what to fix.
- Most problems have a **"Fix this"** button. Hover it to see what it will do. Anything that changes or removes a file asks you first and shows what will change, and whatever it replaces is backed up.
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
- **Option B warning:** other players connect to your IP, not localhost. Anyone outside your home network needs port `38281` (TCP) forwarded to this PC on your router. Use archipelago.gg if that sounds like a hassle.

#### Optional: a live tracker map

- In the **"PopTracker (tracker)"** group on the same tab, click **"Download PopTracker"** and pick a folder. It downloads PopTracker, installs the ARK tracker pack into it, and fills in the directory for you. Click Save.
- Already have PopTracker? Set the **"PopTracker directory"** (or click **"Scan for PopTracker"**) and click **"Install/update ARK tracker pack"** instead. Your old copy of the pack is moved aside.
- Click **"Open PopTracker"** to open it on the ARK map.
- If your room details are filled in, PopTracker opens already connected.
- If it does not connect automatically, click the grey **"AP"** at the top of PopTracker, paste the address with Ctrl+V, then type your slot and password. It remembers them for next time, apart from the password.

### 7. Fill in your room details

Stay on the **Archipelago Setup** tab. Find the **"Archipelago room (Connector settings)"** group. Fill in server, slot and password.

- Type your slot name exactly as it appears in your yaml. Capital letters matter.
- Click **"Copy ARK connection command."** You paste this in-game later.
- Click **"Open Text Client"** to open the Archipelago text client already connected.

### 8. Start the server

Click **Run server** in the footer at the bottom of the window. The status light next to it shows when it's starting and running.

- Wait for the console to finish printing its startup messages before assuming something's wrong. This can take a while, up to 15 minutes if the server is on a hard drive. Don't click inside the console window while it's starting.
- If the console's title bar starts with "Select", it has frozen. Press Enter to unfreeze it.
- If a window pops up saying the server has crashed, click OK on it. The server stays stuck until that window is closed.

### 9. Join the game

Start ARK: Survival Evolved. Open the LAN server list. Join your session. The default name is `ArchipelagoSolo`.

- Spawn your character. Open in-game chat. Paste the command from step 7.
- You only need to do this once. After that the plugin reconnects you by itself.

### 10. You're done

Level up once to send your first check.

- To test items, run `/send ARCHIPELAGONAME Engram: Compass` in your Archipelago server console. The engram should unlock within a few seconds.
- Randomized dinos need one extra step. See "Other information" below.

---

## Playing with friends over the internet (Beta)

> This feature is in **Beta**. Every home network is different and it's hard to test from our end, so please report back whether it worked for you.

Friends on your own home network can join from the LAN list like you do. Friends anywhere else need your router to let them in. The launcher can set this up for you.

1. Set a real admin password in the Network section of **Configuration**. The launcher won't open anything while it's still the default, since anyone who joins could use it to take over your server.
2. Setting a server password is strongly recommended too, so only people you invite can join.
3. Expand **"Friends over the internet (Beta)"** in the Configuration tab and turn on **"Open ports for friends when I run the server."**
4. Click **Run server**. The first time, Windows asks for admin permission once to set up the firewall.
5. While the ports are open, the footer shows **"Copy join address."** Give that to your friends.

Good to know:

- The ports close again when the server stops.
- The launcher never opens the RCON port.
- To join your own server, use your LAN address, which the section shows you. Your public address usually doesn't work from inside your own network.
- If it says your router has UPnP turned off, it shows you the ports to forward by hand on your router's settings page.
- If it says your internet provider shares one address between many customers, port forwarding can't work on your connection at all. Use a virtual LAN tool instead, such as [Tailscale](https://tailscale.com), [ZeroTier](https://www.zerotier.com) or [Radmin VPN](https://www.radmin-vpn.com). Everyone joins the same network and connects to your virtual address.
- The launcher can't prove your server is reachable from outside, so it only says "ready as far as this PC can tell." The real test is a friend adding your address to their Steam favorites. If your server shows up with its name and map, it works.
- Some antivirus programs have their own firewall that ignores Windows Firewall. If friends still can't connect, check yours.

If all else fails, you can port forward manually like usual for ark dedicated servers.

---

## Joining a friend's ARK server

If a friend is hosting, you don't need a server, ArkApi, the plugin, or any of this launcher's setup. They run all of that.

1. In Steam, set ARK: Survival Evolved to the **preaquatica** branch (Properties, then Betas).
2. Turn BattlEye off: Properties, then Launch Options, add `-NoBattlEye`.
3. Ask the host for their join address and server password.
4. Join through Steam: **View, Game Servers, Favorites, Add a server**, and enter the address. Then join from ARK's Favorites list. Or press Tab in game and type `open <address>`.

On the Archipelago side, you can share the host's slot and receive the same items, or have your own slot. Ask your host which they've set up.

---

## What each tab does

| Tab | What it's for |
|---|---|
| **Get Started** | Step by step setup, with the full Setup Status check list at the bottom. |
| **Configuration** | All your server settings, the Quick Launch buttons, and Save. Sections you set once are collapsed to keep things tidy, click a heading to open one. |
| **Mods** | Download and turn on Steam Workshop mods. Appears once the server is installed. |
| **Archipelago Setup** | Your Archipelago folder, your room details, and buttons that open Archipelago's own tools. It remembers every field between sessions, and they travel with your profiles. The tab has its own Save button. The "PopTracker (tracker)" group at the bottom is optional. |
| **Settings** | Launcher settings: dark mode, profiles, update checks, how many backups and how much log history to keep, restoring hidden prompts, and showing the Install tab again. |
| **Debug Log** | A viewer for your logs. The "Log:" dropdown picks which one: the ArkAP plugin's log, the launcher's own log, the launcher crash log, ARK's `ShooterGame.log`, or SteamCMD's download logs. |
| **Instructions** | The in-app version of this guide, with a Quick Guide and a Full Guide. It can pop out into its own window. |

The Install Server/Api/Plugin tab is hidden by default, since Get Started covers installs. Turn it back on in Settings if you want it.

---

## The footer

The bar at the bottom of the window is there on every tab.

- **Run server** and **Stop server**, with a light showing whether the server is starting, running or stopped.
- **Stop server** saves the world before shutting down. If the server isn't answering, it asks before force-closing it and warns you that nothing could be saved.
- **Export diagnostics** for when you need help. See [Reporting a problem](#reporting-a-problem).
- A link to the ARK channel in the Archipelago Discord.
- **Copy join address**, only while ports are open for friends.

---

## Saving your changes

- A Save button glows yellow while something on screen is unsaved.
- A plain Save button means everything already matches what is saved.
- There are three, and each one glows only for its own fields: Configuration, Archipelago Setup, and Mods.
- The "make sure to save" banner at the top has its own Save button, which saves everything that's unsaved, whichever tab it's on.
- Save matters. The server and the `.bat` scripts read your settings from files, and Save is what writes them there.
- Forget to save and the server refuses to start, and Setup Status shows a red X. Click Save and try again.

---

## Check for Updates (top of the window)

- It checks the launcher, the ArkAP plugin, the `.apworld` and the ARK tracker pack.
- It runs by itself every time you start the launcher.
- A marker and a highlight on the button mean something newer exists. Click it to see what.
- Each component links to its own GitHub page.
- The launcher updates itself. For the other three, the dialog names the button that installs them.

---

## Mods (Steam Workshop)

- The **Mods** tab installs Steam Workshop mods for you. It appears once the server is installed.
- Tick a mod to mark it active.
- Click **"Download checked"** to install and activate every ticked mod.
- Mods load from top to bottom. Use the arrows to change the order.
- Click **Save** to write your ticked list to the server.
- Setup Status tells you if your ticks and the server's real mod list have drifted apart.
- Restart the ARK server after any mod change.
- Click **"Copy IDs for YAML"** to copy your mod list for the plugin's yaml. Only mods tagged "apworld ✓" are copied, the others would stop your game generating. They still work on the server, but any engrams they add won't be in the pool of possible items.
- Click **"Rename mod"** to give a mod you added yourself a name you will recognise instead of a bare ID.
- Click **"Remove from list"** to take a mod off the list entirely.

---

## Increase stacks and item weight

Stack mods break the plugin, so the **Increase stacks** section on the Configuration tab does the same job through your server's own settings instead.

- Turn it on and set a global stack multiplier.
- Set how high individual items should stack, including ones that normally don't stack at all, like prime meat, mutton and organic polymer. These are usually what fill your inventory and start deleting things.
- **Excalibur Buffs** can be installed from the same section with one click. It reduces the weight of all items. The launcher sets the one option that must stay on, since that mod's engram unlocker would otherwise break the plugin.
- Stop the server before applying, and restart it afterwards. Your config files are backed up first.

---

## Quick Launch (bottom of the Configuration tab)

- **Run start_ase_server:** starts the server. The same as Run server in the footer.
- **The Open buttons:** open that folder in Explorer.
- **Run switch_map:** currently in early testing.
- **Patch Game.ini for randomized creatures:** turns on randomized dinos. Stop the server first.
- **Reset AP data (keep world save):** clears your Archipelago progress and keeps your world.
- **Full reset for new seed:** clears your Archipelago progress and wipes your world save. Use it when you join a new seed. Stop the server first. Your old save is backed up.
- **Reset server config (troubleshooting):** if your server won't start, this backs up and removes `Game.ini` and `GameUserSettings.ini` so ARK builds them fresh. There is also an option to fully uninstall ArkApi and the plugin, to test a bare server.

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

- Click **"Export diagnostics"** in the footer.
- It saves one `.zip` and opens the folder. Post that zip in the [ARK channel on the Archipelago Discord](#the-footer) or attach it to a GitHub issue.
- Your yaml is found by reading the name inside each file in your Players folder and matching it to your slot, so what the file is called does not matter.
- The zip holds your Setup Status, your version numbers, your config, your Archipelago `.yaml`, `paths.cmd`, `Game.ini` and `GameUserSettings.ini`, the plugin's `ArkAP.config.json`, the debug, crash, ShooterGame and ArkApi logs, everything in your ipc folder (including each player's mailbox folder), and a list of your ipc and Mods folders. That is everything anyone would ask you for.
- Every password in every one of those files is replaced with `[REDACTED]` before it goes in. Very long logs are cut down, so the zip stays small.

---

## Other information

- To start a new seed, click **"Full reset for new seed"** under Quick Launch. Stop the server first.
- If you randomized dinos, stop the server. Click **"Patch Game.ini for randomized creatures"** under Quick Launch. Restart the server.
- That button only works after you have connected to the server once on a randomized seed.

---

## If something goes wrong

| Problem | Fix |
|---|---|
| The server install stopped with exit code 8 | Make sure no server is still running (the launcher checks and offers to stop it) and that you have about 20gb free, then click "Install ARK Server" again. The error message tells you which one it was. |
| The server seems stuck while starting | Look for a crash window hiding behind the console and click OK on it. Otherwise give it up to 15 minutes. |
| Downloads fail with a certificate error | Usually antivirus software scanning HTTPS traffic, a work or school network, or a wrong date and time on your PC. |
| "Scan for paths" missed a path | Pick a higher "Scan intensity" next to the button and scan again. |
| Your cluster folders are missing | Use "Fix this" on the cluster folders row in Setup Status, or "Create ServerCluster folders" in the Paths section on the Configuration tab. |
| The connection command fails in-game | The order is `/connect server slot password`. Copy it again from the Archipelago Setup tab. |
| Checks or items are not coming through | Open the Debug Log tab (it opens on the ArkAP plugin log). |
| The server closed by itself | Debug Log tab, switch the "Log:" dropdown to "ARK server log" and search for `LowLevelFatalError`. |
| Something the launcher did went wrong | Debug Log tab, "Log:" dropdown, "Launcher log". It lists what the app did, with times. |
| The server will not start and it says something is unsaved | Click Save on the banner at the top of the window. |
| A mod you ticked is not loading in game | Open the Mods tab, click Save, and restart the server. |
| Friends outside your network can't join | See [Playing with friends over the internet](#playing-with-friends-over-the-internet-beta). |
| Setup Status shows a red X | Read the hint on that row, or click "Fix this" if it has one. |
| Still stuck | Click "Export diagnostics" in the footer and post the zip on Discord or GitHub. |

---

**Thank you to Ghios, Beeno, Lurch9229, and Wizard_Brandon for helping test and put the entire ARK archipelago together**
