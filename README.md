# Cutting Room Floor

**Sweep up the clutter AI leaves in your projects.**

A free tool by [Creative Mash](https://creativemash.ie). Works on macOS, Windows and Linux.

![Cutting Room Floor overview](screenshots/01-hero.png)

Making things with AI is fast, and it leaves a lot on the cutting room floor: test renders and variations nobody picked, exports that never made the final cut, copies of folders made "just in case", duplicate footage, build folders, session copies and caches. Over a few months that adds up to gigabytes.

Cutting Room Floor finds it across all your project folders, explains each item in plain words, and lets you tick what to sweep up, in one page.

- **Nothing is removed by the scan.** You choose in the page, confirm, and items go to the Trash (the Recycle Bin on Windows), so you can put them back.
- **Nothing about your files leaves your computer.** The page runs on your own machine at `127.0.0.1`, which no other device can reach. The only things it fetches from the internet are its fonts (from Google Fonts) and, when you check for updates, the latest version number (from GitHub).
- **Nothing to install** beyond Python 3.8 or newer.

> **Use at your own risk.** Cutting Room Floor suggests what you might remove; you decide, and you're responsible for what you remove. Check every item first and back up anything important. Once the Trash is emptied, files may be gone for good. Read the [disclaimer](#disclaimer) before you start.

## Use it with Claude Code

Open Terminal (on Windows, PowerShell) and run these two commands. This works whether you use Claude Code in a terminal or in the Claude desktop app:

```bash
claude plugin marketplace add CreativeMash/cutting-room-floor
```

```bash
claude plugin install cutting-room-floor@creativemash
```

If you use Claude Code in a terminal, you can type `/plugin marketplace add CreativeMash/cutting-room-floor` and `/plugin install cutting-room-floor@creativemash` there instead. The `/plugin` commands aren't available in the desktop app.

Then start a new session and ask Claude something like *"what's on my cutting room floor?"* or *"what's taking up space in my projects?"*. The first time, Claude suggests folders to scan (the projects you've worked on with Claude, plus usual places like `~/Projects` or `Documents\GitHub`), you tick the ones you want, and it remembers them. Then it opens the page and talks you through what it found.

### Getting updates

Updates don't install themselves unless you switch that on. To check, open **Settings** in the page and press **Check for updates**, or just ask Claude to *"update Cutting Room Floor"*.

To update by hand, run this in Terminal (or PowerShell), then start a new Claude session:

```bash
claude plugin update cutting-room-floor@creativemash
```

To get updates automatically, type `/plugin` in Claude Code in a terminal, open **Marketplaces**, pick **creativemash** and turn on auto-update. Your saved folders are kept between updates.

## Use it without Claude

Download this repository (green **Code** button, then **Download ZIP**), unzip it, and run:

**macOS / Linux**

```bash
python3 skills/cutting-room-floor/cutting_room_floor.py
```

**Windows** (in PowerShell or Command Prompt, inside the unzipped folder)

```
py -3 skills\cutting-room-floor\cutting_room_floor.py
```

Your browser opens on a folder picker. Tick your project folders, or press **Choose a folder…** to pick one in Finder or Explorer, then press **Start the scan**. The results appear when it's done. Next time it goes straight to the scan. Open **Settings** in the header to change your folders or check for updates. Only ticked folders are scanned; use × on a card to take a folder off the list (suggested folders then stay hidden until you add them again).

To update, download the ZIP again and replace the old folder (if you cloned it with Git, run `git pull` instead). Your saved folders are kept.

## What it looks for

![The three summary cards](screenshots/02-summary-cards.png)

| | What | Why it's there |
|---|---|---|
| **Safe to remove** | `node_modules`, `.next`, `dist`, `build`, `.venv`, `__pycache__` and other build folders; Premiere auto-saves; Blender `.blend1` backups; package download caches | They rebuild or download again when needed |
| **Worth a look** | Folder copies (`Project copy`, `Project (2)`), archive folders, zips sitting next to the same folder, duplicate files, big media and image sequences that no project file mentions, app caches | Your call: some are leftovers, some are finished work |
| **Probably keep** | Media that a Premiere, Blender or code file refers to; WordPress uploads | Something uses them |

**"Safe to remove" is a strong hint, not a promise.** A build folder is only marked safe when the project file that rebuilds it sits next to it (a `package.json` beside `node_modules`, for example), a Python environment only when its requirements file is there, and a backup only when the main file still exists. Anything that fails those checks drops to "Worth a look". Even so, nobody knows your projects like you do, so check anything you don't recognise before removing it. When you remove something marked "Worth a look", the confirmation points it out.

Media counts as "in use" when a Premiere project, Blender file, After Effects project or code file in the scanned folders names it. Media used only by a project *outside* those folders shows as not in use, so check before removing.

Claude Code session copies (`.claude/worktrees`) are shown separately and can't be removed from the page. Clear old ones in the Claude app.

![Choosing what to keep](screenshots/04-choose-what-to-keep.png)

## Options

```
--setup              open the page on the folder picker
--roots FOLDER ...   scan these folders this time, without changing your saved ones
--no-caches          skip app and download caches
--no-browser         don't open the browser automatically
--port N             starting port (default 8765; the next free one is used if taken)
--suggest            print suggested folders as JSON
--set-roots F ...    save folders without opening the page
--show-config        print your saved settings
--check-update       ask GitHub whether a newer version exists
--results            print the running scan's findings as JSON
--version            print the version
```

Saved folders live in `~/.config/cutting-room-floor/config.json` on macOS and Linux, and `%APPDATA%\Cutting Room Floor\config.json` on Windows.

## Good to know

- Cloud-only files (OneDrive "Files On-Demand", iCloud "Optimise Mac Storage") take no room on your disk, so they count as zero and are never opened, which means they are not downloaded by the scan.
- Some things can't go to the Trash: anything on a network drive, and on Windows anything on a USB stick or too big for the Recycle Bin. The system would delete those for good, so they are marked "Can't go to the Trash" and can't be ticked.
- Symlinks and Windows junctions are skipped, so nothing is counted twice and the scan never wanders off to another drive.
- On older macOS versions (before macOS 14), removing uses Finder, which may ask once for permission.
- On Linux, removing needs `gio` (part of most desktops) or `trash-cli`.

## Disclaimer

Cutting Room Floor is free software, provided "as is", without warranty of any kind (see the [MIT licence](LICENSE)). It points out files that *may* be safe to remove, but it can't know your projects the way you do, and it can be wrong.

- **You choose what to remove, and you are responsible for it.** Check every item before removing it, especially anything marked "Worth a look". If you're not sure what something is, leave it.
- **The Trash or Recycle Bin is the only safety net.** Removed items go there first so you can put them back, but once it's emptied they may be gone for good and impossible to recover.
- **Back up anything important before you start**, for example with Time Machine on a Mac, File History on Windows, or a copy on another drive.
- **Creative Mash accepts no liability** for any loss of data, work, time or money resulting from using this tool, to the extent the law allows.

By using Cutting Room Floor you accept these terms.

## Licence

MIT. Free to use, change and share. See [LICENSE](LICENSE).

Made by [Creative Mash](https://creativemash.ie).
