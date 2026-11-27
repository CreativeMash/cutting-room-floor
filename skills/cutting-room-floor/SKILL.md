---
name: cutting-room-floor
description: Sweep up the clutter that builds up in the person's project folders when they and their AI tools make things. Finds what is taking up disk space (rebuildable build folders like node_modules, backups and copies, media no project uses, duplicate files, app caches) and open a page where they choose what to move to the Trash or Recycle Bin. Use when someone asks why their disk is full, what is wasting space in their projects, which files their projects or AI tools left behind, what's on their cutting room floor, asks to run Cutting Room Floor, or asks to update or check for updates to Cutting Room Floor.
---

# Cutting Room Floor

Cutting Room Floor is a small local tool in this skill's folder: `cutting_room_floor.py` (the scanner and a local web server) and `cutting_room_floor.html` (the page). It needs Python 3.8 or newer and nothing else. It works on macOS, Windows and Linux.

Your job is to get it running for the person, set up their folders on first use, and help them understand the results. **You never delete or move anything yourself.** The person picks items in the page and confirms there; removed items go to the Trash (Recycle Bin on Windows), so they can be put back.

In the commands below, `SCRIPT` means the full path to `cutting_room_floor.py` in this skill's folder, in quotes.

## 1. Find Python

Try these in order and use the first one that prints a version of 3.8 or newer:

- macOS and Linux: `python3 --version`
- Windows: `py -3 --version`, then `python --version`

If none works, tell the person they need Python first (python.org/downloads, and on Windows tick "Add python.exe to PATH" in the installer), then stop.

## 2. First run: ask which folders to look at

Run `PYTHON SCRIPT --show-config`.

If `roots` is empty, or the person asked to change folders, do the setup:

1. Run `PYTHON SCRIPT --suggest`. It prints a JSON list of folders found on their computer: folders they have worked in with Claude Code (`claude: true`, with a note naming the projects), plus usual project places such as `~/Projects`, `~/code`, `~/Documents/GitHub` or `~/source/repos`. Entries with `checked: true` are the best guesses.
2. Ask the person which folders to scan. Show each folder's `display` path and `note`, best guesses first. If there are four or fewer, ask with a multiple-choice question that allows several answers; otherwise list them numbered and ask them to reply with the numbers. Always let them add other folders. Tell them in one line that Cutting Room Floor only reads inside these folders and nothing leaves their computer.
3. Save their choice: `PYTHON SCRIPT --set-roots "FOLDER 1" "FOLDER 2" ...`. If it reports a folder as too broad (a whole home folder or drive), ask for the folders inside it where the projects actually live.

If there is already a saved list and they did not ask to change it, skip this step and mention which folders it will scan.

## 3. Start it

Run `PYTHON SCRIPT` **in the background** (it is a server and keeps running). It prints the address, usually `http://127.0.0.1:8765` (it picks the next free port if that one is taken), and opens the page in their browser. Give them the address in case the browser did not open.

The person can also pick folders in the page itself: if no folders are saved, the page opens on a folder picker (with a button that opens their system's own folder window), and the header has a "Settings" button for changing folders and checking for updates.

## 4. Help with the results

The scan takes from a few seconds to a few minutes, depending on how much is in the folders. When it finishes you can read the results yourself with a GET request to `ADDRESS/state` (for example `curl -s http://127.0.0.1:8765/state`). `data.items` holds every finding with `path`, `size` in bytes, `kind`, `risk` (`safe`, `review` or `keep`) and `note`.

Give a short summary in plain words: how much is in "Safe to remove" (build folders, caches and backups that come back by themselves) and the biggest items worth a look. Point out anything that needs care:

- Media marked "not in use" means no Premiere, Blender or code file in the scanned folders names it. It may still be a finished export, or used by a project outside these folders.
- "Safe to remove" is based on evidence (for example a `package.json` next to `node_modules`, or the main file next to a backup), but it is not a guarantee. Say so plainly, and suggest checking anything they don't recognise.
- Items with a `no_bin` reason can't go to the Trash (network drives, and on Windows USB drives or items too big for the Recycle Bin). The page won't let them be ticked. If the person wants them gone, explain that it would be permanent and leave it to them.
- WordPress uploads are always marked "keep", because pages link to them through the site database.
- Claude session copies (`.claude/worktrees`) are listed apart and are not removable from the page. They are cleared in the Claude app.

Remind them once that they use it at their own risk: check before removing, and back up anything important. Then leave the choosing to them in the page. Never call the page's remove endpoints yourself, and never delete files with shell commands on the person's behalf as part of this skill.

## 5. Updates

If the person asks to update Cutting Room Floor, or whether there's a newer version:

1. Run `PYTHON SCRIPT --check-update`. It asks GitHub for the latest version number and prints JSON with `current`, `latest`, `newer`, `notes` and `install`. Nothing about their files is sent. If it has an `error`, pass it on in plain words.
2. If `newer` is false, tell them they have the latest version.
3. If `newer` is true, tell them the new version number and summarise `notes`, then ask if they want it. Never update without a yes.
   - `install` is `claude`: on a yes, run `claude plugin marketplace update creativemash` and then `claude plugin update cutting-room-floor@creativemash`. Then tell them to type `/reload-plugins` (or restart Claude Code) and ask again to run it. If the commands fail, tell them to type `/plugin`, open **Installed**, pick **cutting-room-floor** and choose **Update now**.
   - `install` is `git`: on a yes, run `git -C "FOLDER" pull` with the `folder` from the JSON.
   - `install` is `zip`: tell them to download the new version from the `repo` page and put it in place of the old folder. Their saved folders are kept.
4. Mention once that they can get updates without asking: type `/plugin`, open **Marketplaces**, pick **creativemash** and turn on auto-update.

Stop a running server before updating, and start it again afterwards.

## 6. Stopping

The server stops when the person presses Ctrl+C in its window, or when you stop the background command. Stop it when they say they are done.

## Options

- `--roots FOLDER ...` scans other folders this time without changing the saved list.
- `--setup` opens the page on the folder picker.
- `--no-caches` skips app and download caches.
- `--no-browser` does not open the browser.
- `--port N` picks the starting port.
- `--check-update` asks GitHub whether a newer version exists, then exits.

Saved settings live in `~/.config/cutting-room-floor/config.json` (macOS and Linux) or `%APPDATA%\Cutting Room Floor\config.json` (Windows).
