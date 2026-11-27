#!/usr/bin/env python3
"""Cutting Room Floor, a free tool by Creative Mash (https://creativemash.ie)

Sweeps up the clutter that builds up when you and your AI tools make things: test
renders, unused exports, folder copies, duplicates, build folders and caches. It
shows everything in a page in your browser and lets you pick what to remove. Works on macOS, Windows and Linux
with nothing to install beyond Python 3.8 or newer.

  python3 cutting_room_floor.py                  first run asks which folders to look at
  python3 cutting_room_floor.py --setup          choose different folders
  python3 cutting_room_floor.py --roots ~/code   scan these folders this time only

(On Windows, use `py` or `python` in place of `python3`.)

The scan only reads. Nothing is removed unless you tick it in the page and
confirm, and removed items go to the Trash (the Recycle Bin on Windows), so you
can put them back until you empty it. The page is served on 127.0.0.1 only, so
no other computer can reach it.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import ssl
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

VERSION = "1.0.0"  # fallback only: the real version is read from .claude-plugin/plugin.json
REPO = "CreativeMash/cutting-room-floor"
REPO_URL = f"https://github.com/{REPO}"
UPDATE_BASE = os.environ.get("CRF_UPDATE_BASE") or f"https://raw.githubusercontent.com/{REPO}/main"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
HOME = Path.home()

if IS_WIN:
    OS_INFO = dict(name="windows", computer="PC", reveal="Show in Explorer", bin="Recycle Bin")
elif IS_MAC:
    OS_INFO = dict(name="mac", computer="Mac", reveal="Show in Finder", bin="Trash")
else:
    OS_INFO = dict(name="linux", computer="computer", reveal="Show in folder", bin="Trash")

# Usual places people keep projects, relative to the home folder.
COMMON_DIRS = ["Projects", "projects", "Documents/Projects", "Code", "code", "dev", "Developer", "src",
               "repos", "GitHub", "Documents/GitHub", "source/repos", "Sites", "Local Sites", "workspace", "Work"]

REGEN_DIRS = {
    "node_modules": "npm packages. Run `npm install` to get them back.",
    ".next": "Next.js build output. Rebuilt on the next build.",
    "dist": "Build output. Rebuilt on the next build.",
    "build": "Build output. Rebuilt on the next build.",
    ".venv": "Python virtual environment. Recreate it with pip or uv.",
    "venv": "Python virtual environment. Recreate it with pip or uv.",
    "__pycache__": "Python bytecode cache. Recreated automatically.",
    ".pytest_cache": "Test cache. Recreated automatically.",
    ".turbo": "Turborepo cache. Recreated automatically.",
    ".parcel-cache": "Parcel cache. Recreated automatically.",
    ".cache": "Tool cache. Recreated automatically.",
    "DerivedData": "Xcode build data. Rebuilt on the next build.",
    "coverage": "Test coverage report. Recreated when tests run.",
}
ARCHIVE_DIR_NAMES = {"archive", "archives", "backups", "backup", "old"}
# A build folder only counts as safe when one of these sits next to it (proof it can be rebuilt).
BUILD_MANIFESTS = {"package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "composer.json", "Gemfile",
                   "pom.xml", "build.gradle", "build.gradle.kts", "CMakeLists.txt", "Makefile", "angular.json"}
PY_REQUIREMENTS = {"requirements.txt", "pyproject.toml", "Pipfile", "setup.py", "uv.lock", "poetry.lock", "environment.yml"}
PROJECT_MARKERS = {".git", "package.json", "pyproject.toml", "requirements.txt", "composer.json", "Cargo.toml", "go.mod"}
PROJECT_FILE_EXT = {".prproj", ".blend", ".aep", ".xcodeproj", ".sln"}
VIDEO = {".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v", ".webm", ".prores"}
IMAGE = {".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff", ".tga", ".psd", ".webp", ".dpx", ".heic"}
AUDIO = {".wav", ".aif", ".aiff", ".mp3", ".m4a", ".flac"}
MEDIA = VIDEO | IMAGE | AUDIO
TEXT_EXT = {".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json",
            ".md", ".txt", ".php", ".py", ".vue", ".svelte", ".xml", ".yml", ".yaml", ".csv", ".svg",
            ".edl", ".fcpxml", ".srt", ".astro", ".liquid", ".twig", ".sh"}
GZIP_PROJECT_EXT = {".prproj", ".prin"}            # Premiere: gzipped XML
BINARY_PROJECT_EXT = {".blend", ".aep", ".psb", ".indd", ".mogrt"}  # path strings are stored as plain bytes

BIG_MEDIA_BYTES = 25 * 1024**2
SEQ_MIN_FILES = 20
DUP_MIN_BYTES = 10 * 1024**2
CACHE_MIN_BYTES = 50 * 1024**2
TEXT_MAX_BYTES = 4 * 1024**2
BINARY_MAX_BYTES = 600 * 1024**2

REF_EXT_RE = re.compile(r"\.(" + "|".join(sorted(e[1:] for e in MEDIA)) + r")(?![a-z0-9])", re.I)
SPLIT_RE = re.compile(r"[/\\\"'<>()=\x00\r\n\t|]")
WORKTREE_RE = re.compile(r"[\\/]\.claude[\\/]worktrees[\\/]")

STATE = {"status": "starting", "progress": "", "data": None, "error": None}
ITEMS = {}  # id -> item (server side copy with absolute path)
TOKEN = secrets.token_urlsafe(24)


# ---- small helpers ---------------------------------------------------------
def tilde(p) -> str:
    s, h = str(p), str(HOME)
    return "~" + s[len(h):] if s == h or s.startswith(h + os.sep) else s


def is_inside(p: Path, parent: Path) -> bool:
    try:
        p.relative_to(parent)
        return True
    except ValueError:
        return False


def is_link(st) -> bool:
    """Symlinks, plus Windows junctions (which os.walk would otherwise follow)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) in (0xA0000003, 0xA000000C)


def cloud_only(st) -> bool:
    """Files kept in the cloud (OneDrive, iCloud) and not on this disk. Reading them would download them."""
    if getattr(st, "st_file_attributes", 0) & (0x1000 | 0x40000 | 0x400000):
        return True
    return bool(getattr(st, "st_flags", 0) & 0x40000000)  # macOS SF_DATALESS


def size_on_disk(st) -> int:
    if cloud_only(st):
        return 0
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if blocks is not None else st.st_size


def disk_usage(p: Path) -> int:
    try:
        st = os.lstat(p)
    except OSError:
        return 0
    if is_link(st):
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return size_on_disk(st)
    total, stack = 0, [str(p)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_link(st):
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
                    else:
                        total += size_on_disk(st)
        except OSError:
            pass
    return total


def cache_roots():
    """(folder, kind) pairs. kind picks the note shown for each big item inside."""
    if IS_WIN:
        local = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")
        return [(local / "Temp", "temp"), (local / "npm-cache", "npm"), (local / "pip" / "Cache", "pip"),
                (local / "uv" / "cache", "pip"), (local / "Yarn" / "Cache", "yarn"), (HOME / ".cache", "app")]
    if IS_MAC:
        return [(HOME / "Library" / "Caches", "app"), (HOME / ".cache", "app"), (HOME / ".npm", "npm")]
    return [(HOME / ".cache", "app"), (HOME / ".npm", "npm")]


# ---- saved settings --------------------------------------------------------
def config_path() -> Path:
    if IS_WIN:
        return Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming") / "Cutting Room Floor" / "config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "cutting-room-floor" / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2), "utf-8")


def clean_roots(paths):
    """Expand, check and de-nest folder paths. Returns (roots, problems)."""
    found, problems = [], []
    for raw in paths:
        raw = str(raw).strip().strip('"').strip("'")
        if not raw:
            continue
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        try:
            p = p.resolve()
        except OSError:
            pass
        if not p.is_dir():
            problems.append(f"{raw} is not a folder on this computer.")
            continue
        if p == p.parent or p == HOME:
            problems.append(f"{raw} is too broad. Pick the folders inside it where your projects live.")
            continue
        found.append(p)
    roots = []
    for p in sorted(set(found), key=lambda x: len(str(x))):
        if not any(is_inside(p, q) for q in roots):
            roots.append(p)
    return roots, problems


# ---- first-run suggestions -------------------------------------------------
def looks_temporary(path: str) -> bool:
    s = path.replace("\\", "/").lower() + "/"
    return any(k in s for k in ("/scratch-workspaces/", "/tmp/", "/private/var/", "/var/folders/",
                                "/appdata/", "/library/", "/.claude/", "/.cache/"))


def claude_folders() -> dict:
    """Folders this person has worked in with Claude Code, from Claude Code's own session logs. Path -> last used."""
    base = HOME / ".claude" / "projects"
    found = {}
    try:
        project_dirs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return found
    for proj in project_dirs:
        try:
            logs = sorted(proj.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:3]
        except OSError:
            continue
        for log in logs:
            cwd = None
            try:
                with open(log, encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh):
                        if i > 60:
                            break
                        if '"cwd"' in line:
                            try:
                                cwd = json.loads(line).get("cwd")
                            except ValueError:
                                continue
                            if cwd:
                                break
            except OSError:
                continue
            if not cwd:
                continue
            cwd = WORKTREE_RE.split(cwd)[0]
            if looks_temporary(cwd):
                break
            p = Path(cwd)
            if p.is_dir() and p != HOME and p != p.parent:
                found[p] = max(found.get(p, 0), log.stat().st_mtime)
            break
    return found


def child_dirs(p: Path) -> int:
    try:
        return sum(1 for c in p.iterdir() if c.is_dir() and not c.name.startswith("."))
    except OSError:
        return 0


def same_path(p) -> str:
    """A path in a form that can be compared: no trailing slash, and any case on Windows."""
    return os.path.normcase(os.path.normpath(os.path.expanduser(str(p))))


def suggest_folders(current=(), hidden=()):
    sugg = {}
    hidden = {same_path(h) for h in hidden} - {same_path(c) for c in current}

    def entry(p: Path):
        key = str(p)
        if key not in sugg:
            sugg[key] = dict(path=key, display=tilde(p), name=p.name, claude=[], is_claude_project=False,
                             common=False, current=False, last=0.0)
        return sugg[key]

    commons, seen = [], set()
    for rel in COMMON_DIRS:
        p = HOME / rel
        try:
            st = p.stat()
        except OSError:
            continue
        if not p.is_dir() or (st.st_dev, st.st_ino) in seen or not child_dirs(p):
            continue
        seen.add((st.st_dev, st.st_ino))
        commons.append(p)
        entry(p)["common"] = True

    loose = defaultdict(list)
    for p, t in claude_folders().items():
        host = next((c for c in commons if is_inside(p, c)), None)
        if host:
            e = entry(host)
            e["claude"].append(p.name)
            e["last"] = max(e["last"], t)
        else:
            loose[p.parent].append((p, t))
    for parent, kids in loose.items():
        if len(kids) >= 2 and parent != HOME and parent != parent.parent:
            e = entry(parent)
            e["claude"] += [k.name for k, _ in kids]
            e["last"] = max(e["last"], max(t for _, t in kids))
        else:
            for p, t in kids:
                e = entry(p)
                e["is_claude_project"] = True
                e["last"] = max(e["last"], t)

    for p in current:
        entry(Path(p))["current"] = True

    out = []
    for e in sugg.values():
        if same_path(e["path"]) in hidden:
            continue
        n = len(e["claude"])
        if e["is_claude_project"]:
            note = "A project you have worked on with Claude."
        elif n:
            names = ", ".join(sorted(set(e["claude"]))[:4])
            more = f" and {n - 4} more" if n > 4 else ""
            note = f"{n} project{'s' if n > 1 else ''} you have worked on with Claude: {names}{more}."
        elif e["current"]:
            note = "One of your saved folders."
        else:
            n_in = child_dirs(Path(e["path"]))
            note = f"A usual place for projects. {n_in} folder{'s' if n_in != 1 else ''} inside."
        out.append(dict(path=e["path"], display=e["display"], name=e["name"], note=note,
                        claude=bool(n or e["is_claude_project"]),
                        checked=e["current"] if current else bool(n or e["is_claude_project"]),
                        last=e["last"]))
    out.sort(key=lambda e: (not e["checked"], not e["claude"], -e["last"], e["display"].lower()))
    return out[:14]


# ---- the scan --------------------------------------------------------------
class Scanner:
    def __init__(self, roots, caches):
        self.roots = [r for r in roots if r.exists()]
        self.single = {r for r in self.roots if self.looks_like_project(r)}
        self.caches = caches
        self.items = []
        self.project_totals = defaultdict(lambda: defaultdict(int))  # project -> category -> bytes
        self.files = []        # (path, size, project, ext)
        self.ref_names = set()  # lowercase basenames referenced by any project file
        self.worktrees = []
        self.n = 0

    @staticmethod
    def looks_like_project(root: Path) -> bool:
        try:
            names = os.listdir(root)
        except OSError:
            return False
        return any(n in PROJECT_MARKERS or os.path.splitext(n)[1].lower() in PROJECT_FILE_EXT for n in names)

    def progress(self, msg):
        STATE["progress"] = msg

    @staticmethod
    def siblings(d: Path) -> set:
        try:
            return set(os.listdir(d))
        except OSError:
            return set()

    def regen_check(self, parent: Path, p: Path, name: str, in_local_sites: bool):
        """(risk, reason). Only 'safe' when there is evidence the folder really can be rebuilt."""
        sib = self.siblings(parent)
        if name in {"__pycache__", ".pytest_cache", "DerivedData"}:
            return "safe", ""
        if name in {".venv", "venv"}:
            if not (p / "pyvenv.cfg").exists():
                return "review", "Named like a Python environment, but it does not look like one. Check what is inside."
            if not (sib & PY_REQUIREMENTS or any(s.startswith("requirements") and s.endswith(".txt") for s in sib)):
                return "review", "Python environment with no requirements file next to it, so its packages would have to be reinstalled by hand."
            return "safe", ""
        if name == "node_modules":
            if "package.json" not in sib:
                return "review", "npm packages with no package.json next to them, so they may not reinstall."
            return "safe", ""
        if in_local_sites and name in {"build", "dist"}:
            return "review", "Part of a WordPress site plugin or theme. The site may need it if there is no source to rebuild it from."
        if not (sib & BUILD_MANIFESTS or any(s.endswith((".xcodeproj", ".sln", ".csproj")) for s in sib)):
            return "review", "Named like a build folder, but there is no project file next to it, so it may not be rebuildable. Check what is inside."
        if name == "build" and "package.json" in sib:
            try:
                if "electron-builder" in (parent / "package.json").read_text("utf-8", errors="ignore"):
                    return "review", "Build folder in a project that uses electron-builder, which keeps app icons and settings here."
            except OSError:
                pass
        return "safe", ""

    def project_of(self, root: Path, path: Path) -> str:
        if root in self.single:
            return f"{root.parent.name} / {root.name}"
        rel = path.relative_to(root)
        top = rel.parts[0] if rel.parts else "Loose files"
        return f"{root.name} / {top}"

    def add(self, kind, path: Path, size, project, risk, note, **extra):
        self.n += 1
        item = dict(id=f"i{self.n}", kind=kind, path=str(path), name=path.name, size=size,
                    project=project, risk=risk, note=note, **extra)
        self.items.append(item)
        return item

    # ---- reference index -------------------------------------------------
    def index_refs(self, text: str):
        for m in REF_EXT_RE.finditer(text):
            start = max(0, m.start() - 260)
            name = SPLIT_RE.split(text[start:m.end()])[-1].strip().lower()
            if name:
                self.ref_names.add(name)
                self.ref_names.add(unquote(name))

    def read_for_refs(self, path: Path, ext: str, size: int):
        try:
            if ext in TEXT_EXT and size <= TEXT_MAX_BYTES:
                self.index_refs(path.read_text("utf-8", errors="ignore"))
            elif ext in GZIP_PROJECT_EXT:
                raw = path.read_bytes()
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
                self.index_refs(raw.decode("utf-8", errors="ignore"))
            elif ext in BINARY_PROJECT_EXT and size <= BINARY_MAX_BYTES:
                self.index_refs(path.read_bytes().decode("latin-1"))
        except OSError:
            pass

    # ---- main walk ---------------------------------------------------------
    def walk_root(self, root: Path):
        in_local_sites = root.name == "Local Sites"
        for dirpath, dirnames, filenames in os.walk(root):
            d = Path(dirpath)
            dir_project = self.project_of(root, d)
            self.progress(f"Scanning {tilde(d)}")
            keep = []
            for name in dirnames:
                p = d / name
                try:
                    if is_link(os.lstat(p)):
                        continue
                except OSError:
                    continue
                project = self.project_of(root, p)
                if name == ".git":
                    self.project_totals[project]["other"] += disk_usage(p)
                    continue
                if name == "worktrees" and d.name == ".claude":
                    try:
                        wts = sorted(c for c in p.iterdir() if c.is_dir())
                    except OSError:
                        wts = []
                    for wt in wts:
                        sz = disk_usage(wt)
                        self.worktrees.append(dict(path=str(wt), name=wt.name, size=sz, project=project))
                        self.project_totals[project]["worktrees"] += sz
                    continue
                if name in REGEN_DIRS:
                    sz = disk_usage(p)
                    if sz >= 1024**2:
                        risk, why = self.regen_check(d, p, name, in_local_sites)
                        self.add("regen", p, sz, project, risk, why if risk == "review" else REGEN_DIRS[name])
                        self.project_totals[project]["regen"] += sz
                    else:
                        self.project_totals[project]["other"] += sz
                    continue
                if name == "Adobe Premiere Pro Auto-Save":
                    sz = disk_usage(p)
                    if any(s.lower().endswith(".prproj") for s in self.siblings(d)):
                        self.add("backup", p, sz, project, "safe", "Premiere auto-saves. Only needed to recover an older version of an edit.")
                    else:
                        self.add("backup", p, sz, project, "review", "Premiere auto-saves with no Premiere project next to them. They may be the only copy of an edit.")
                    self.project_totals[project]["backup"] += sz
                    continue
                if re.search(r"(?i)( copy( \d+)?| \(\d+\)| - copy( \(\d+\))?)$", name) or name.lower() in ARCHIVE_DIR_NAMES:
                    sz = disk_usage(p)
                    label = "Looks like a duplicate copy of a folder." if "copy" in name.lower() or "(" in name \
                        else "Archive or backup folder. Check whether you still need it."
                    self.add("backup", p, sz, project, "review", label)
                    self.project_totals[project]["backup"] += sz
                    continue
                keep.append(name)
            dirnames[:] = keep

            for name in filenames:
                p = d / name
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if is_link(st):
                    continue
                size = size_on_disk(st)
                ext = p.suffix.lower()
                if re.search(r"\.blend\d+$", name):
                    if (d / re.sub(r"\d+$", "", name)).exists():
                        self.add("backup", p, size, dir_project, "safe", "Blender automatic backup of an earlier save.")
                    else:
                        self.add("backup", p, size, dir_project, "review", "Blender backup whose main .blend file is not here. It may be the only copy.")
                    self.project_totals[dir_project]["backup"] += size
                    continue
                if ext == ".zip" and (d / p.stem).is_dir() and size >= 1024**2:
                    self.add("backup", p, size, dir_project, "review", f"Zip next to a folder with the same name ({p.stem}). It is probably a copy of that folder.")
                    self.project_totals[dir_project]["backup"] += size
                    continue
                if size:  # skip cloud-only files: reading them would download them
                    self.read_for_refs(p, ext, size)
                self.files.append((p, size, dir_project, ext))

    def media_analysis(self):
        self.progress("Checking which media files are used by your projects")
        by_dir = defaultdict(list)
        for f in self.files:
            if f[3] in MEDIA:
                by_dir[f[0].parent].append(f)
        claimed = set()
        is_wp = lambda p: "/wp-content/uploads" in str(p).replace("\\", "/")
        # image or frame sequences: many media files in one folder
        for d, files in by_dir.items():
            imgs = [f for f in files if f[3] in IMAGE]
            if len(imgs) < SEQ_MIN_FILES:
                continue
            total = sum(f[1] for f in imgs)
            if total < BIG_MEDIA_BYTES:
                continue
            project = imgs[0][2]
            used = sum(1 for f in imgs if f[0].name.lower() in self.ref_names)
            for f in imgs:
                claimed.add(f[0])
            if is_wp(d):
                status, risk, note = "wordpress", "keep", "WordPress media library. Pages link to these through the site database, so this scan cannot tell which ones are in use."
            elif used:
                status, risk, note = "used", "keep", f"Frame or image folder. {used} of {len(imgs)} files are referenced by a project file (a Premiere, Blender or code file)."
            else:
                status, risk, note = "unused", "review", f"Folder of {len(imgs)} images or frames. No project file references any of them. It may be an old render or test output."
            self.add("media", d, total, project, risk, note, status=status, count=len(imgs), is_dir=True)
            self.project_totals[project]["media_used" if status != "unused" else "media_unused"] += total
        for p, size, project, ext in self.files:
            if p in claimed or ext not in MEDIA or size < BIG_MEDIA_BYTES:
                continue
            claimed.add(p)
            if is_wp(p):
                status, risk, note = "wordpress", "keep", "WordPress media library. Pages link to it through the site database, so this scan cannot tell whether it is in use."
            elif p.name.lower() in self.ref_names:
                status, risk, note = "used", "keep", "Referenced by a project file (a Premiere, Blender or code file)."
            else:
                status, risk, note = "unused", "review", "Not referenced by any project file. It could be a finished export you want to keep, a clip you never used, or a leftover."
            self.add("media", p, size, project, risk, note, status=status)
            self.project_totals[project]["media_used" if status != "unused" else "media_unused"] += size
        for p, size, project, ext in self.files:
            if p not in claimed:
                self.project_totals[project]["other"] += size

    def duplicates(self):
        self.progress("Looking for duplicate files")
        by_size = defaultdict(list)
        for p, size, project, ext in self.files:
            if size >= DUP_MIN_BYTES:
                by_size[size].append((p, project))
        groups = []
        for size, fs in by_size.items():
            if len(fs) < 2:
                continue
            by_hash = defaultdict(list)
            for p, project in fs:
                self.progress(f"Comparing {p.name}")
                try:
                    h = hashlib.blake2b(digest_size=20)
                    with open(p, "rb") as fh:
                        for chunk in iter(lambda: fh.read(4 * 1024**2), b""):
                            h.update(chunk)
                    by_hash[h.hexdigest()].append((p, project))
                except OSError:
                    continue
            for same in by_hash.values():
                if len(same) > 1:
                    groups.append((size, same))
        for gid, (size, same) in enumerate(sorted(groups, key=lambda g: -g[0] * (len(g[1]) - 1)), 1):
            same.sort(key=lambda x: (len(str(x[0])), str(x[0])))
            keeper = same[0][0]
            for p, project in same[1:]:  # the copy with the shortest path stays and is not listed
                self.add("duplicate", p, size, project, "review", f"Same as {tilde(keeper)}, which stays.",
                         group=gid, copies=len(same))
        # a file listed as a duplicate is not listed again as unused media, so it is never counted twice
        dup_paths = {it["path"] for it in self.items if it["kind"] == "duplicate"}
        self.items = [it for it in self.items if not (it["kind"] == "media" and it["path"] in dup_paths)]

    def scan_caches(self):
        notes = {
            "npm": ("safe", "npm download cache. Packages download again when needed."),
            "yarn": ("safe", "Yarn download cache. Packages download again when needed."),
            "pip": ("safe", "Python package download cache. Packages download again when needed."),
            "temp": ("review", "Temporary files. Usually safe to remove, unless an installer or app is running right now."),
            "app": ("review", "App cache. The app rebuilds it as needed, but it may run slower for a while. Quit the app before removing it."),
        }
        for root, kind in cache_roots():
            if not root.is_dir():
                continue
            self.progress(f"Measuring caches in {tilde(root)}")
            try:
                children = list(root.iterdir())
            except OSError:
                continue
            for c in children:
                sz = disk_usage(c)
                if sz < CACHE_MIN_BYTES:
                    continue
                risk, note = notes[kind]
                n = c.name.lower()
                if kind == "app":
                    if "huggingface" in n:
                        note = "Downloaded AI models. They download again (possibly several GB) the next time a tool needs them."
                    elif n in {"uv", "pip", "pip-tools", "pypoetry"}:
                        risk, note = notes["pip"]
                    elif "shipit" in n:
                        risk, note = "safe", "Leftover app updater files. Safe to remove."
                self.add("cache", c, sz, f"Caches / {root.name}", risk, note)

    def run(self):
        for r in self.roots:
            self.walk_root(r)
        self.media_analysis()
        self.duplicates()
        if self.caches:
            self.scan_caches()
        _CHECKS.clear()
        for it in self.items:
            why = trash_problem(it["path"], it["size"])
            if why:
                it["no_bin"] = why
        du = shutil.disk_usage(str(HOME))
        return dict(
            items=self.items,
            worktrees=sorted(self.worktrees, key=lambda w: -w["size"]),
            projects={k: dict(v) for k, v in self.project_totals.items()},
            roots=[str(r) for r in self.roots],
            caches=self.caches,
            disk=dict(total=du.total, free=du.free),
            scanned_at=time.strftime("%Y-%m-%d %H:%M"),
        )


def run_scan(roots, caches):
    STATE.update(status="scanning", data=None, error=None, progress="Starting")
    try:
        data = Scanner(roots, caches).run()
        ITEMS.clear()
        for it in data["items"]:
            ITEMS[it["id"]] = it
        STATE.update(status="ready", data=data, progress="")
    except Exception as e:  # show it in the page
        STATE.update(status="error", error=repr(e))
        raise


# ---- removing and revealing ------------------------------------------------
def _run(cmd, **kw):
    if IS_WIN:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


_CHECKS = {}  # cached answers per drive or mount, cleared on each scan
NETWORK_FS = {"smbfs", "afpfs", "nfs", "webdav", "cifs", "ftp"}


def _mac_mounts():
    if "mounts" not in _CHECKS:
        out = subprocess.run(["mount"], capture_output=True, text=True).stdout
        found = []
        for line in out.splitlines():
            m = re.match(r"^.*? on (.*?) \(([^,)]+)", line)
            if m:
                found.append((m.group(1), m.group(2).strip()))
        _CHECKS["mounts"] = sorted(found, key=lambda x: -len(x[0]))
    return _CHECKS["mounts"]


def _win_bin(root: str):
    """(nuke_on_delete, max_bytes) for a drive's Recycle Bin."""
    key = ("bin", root)
    if key not in _CHECKS:
        import ctypes
        import winreg
        nuke, cap = False, None
        buf = ctypes.create_unicode_buffer(80)
        if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buf, 80):
            m = re.search(r"\{[0-9A-Fa-f-]+\}", buf.value)
            if m:
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\BitBucket\\Volume\\" + m.group(0)) as k:
                        try:
                            nuke = bool(winreg.QueryValueEx(k, "NukeOnDelete")[0])
                        except OSError:
                            pass
                        try:
                            cap = int(winreg.QueryValueEx(k, "MaxCapacity")[0]) * 1024 * 1024
                        except OSError:
                            pass
                except OSError:
                    pass
        if cap is None:  # no saved setting: assume a cautious 5% of the drive
            cap = shutil.disk_usage(root).total * 5 // 100
        _CHECKS[key] = (nuke, cap)
    return _CHECKS[key]


def trash_problem(path: str, size: int):
    """Why this item cannot safely go to the Trash or Recycle Bin, or None if it can.
    Covers the cases where the system would delete it for good instead."""
    keep = " so it can't be put back. Delete it yourself if you're sure."
    try:
        if IS_WIN:
            import ctypes
            p = os.path.abspath(path)
            if p.startswith("\\\\"):
                return "This is on a network location with no Recycle Bin," + keep
            root = os.path.splitdrive(p)[0] + "\\"
            if ("type", root) not in _CHECKS:
                _CHECKS[("type", root)] = ctypes.windll.kernel32.GetDriveTypeW(root)
            if _CHECKS[("type", root)] != 3:  # 3 = a fixed, local drive
                return "This is on a USB, network or other drive with no Recycle Bin," + keep
            nuke, cap = _win_bin(root)
            if nuke:
                return "The Recycle Bin is switched off for this drive, so Windows would delete it for good," + keep
            if size > cap:
                return "This is too big for the Recycle Bin, so Windows would delete it for good," + keep
        elif IS_MAC:
            for mount, fs in _mac_mounts():
                if path == mount or path.startswith(mount.rstrip("/") + "/"):
                    if fs in NETWORK_FS:
                        return "This is on a network drive with no Trash," + keep
                    break
    except Exception:
        return None
    return None


def move_to_trash(path: str, size: int = 0) -> str:
    if not os.path.lexists(path):
        return "already gone"
    why = trash_problem(path, size)
    if why:
        raise RuntimeError(why)
    if IS_WIN:
        # The path travels in an environment variable, never inside the command text.
        script = ("Add-Type -AssemblyName Microsoft.VisualBasic; $p = $env:CRF_PATH; "
                  "if (Test-Path -LiteralPath $p -PathType Container) { "
                  "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p, 'AllDialogs', 'SendToRecycleBin') } "
                  "else { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p, 'AllDialogs', 'SendToRecycleBin') }")
        r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                 env=dict(os.environ, CRF_PATH=path))
    elif IS_MAC:
        if os.path.exists("/usr/bin/trash"):  # macOS 14 and later
            r = _run(["/usr/bin/trash", path])
        else:
            r = _run(["osascript", "-e", "on run argv", "-e",
                      'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)', "-e", "end run", path])
    else:
        tool = shutil.which("gio") and ["gio", "trash"] or shutil.which("trash-put") and ["trash-put"]
        if not tool:
            raise RuntimeError("No trash tool found. Install gio or trash-cli.")
        r = _run(tool + [path])
    if r.returncode != 0 or os.path.lexists(path):
        raise RuntimeError((r.stderr or r.stdout or "could not be moved").strip().splitlines()[-1][:300])
    return "moved"


def reveal(path: str):
    if IS_WIN:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif IS_MAC:
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


# ---- the system folder picker ----------------------------------------------
# A web page is never told a folder's full path, so the picker is opened here,
# on the person's own computer, and only the chosen path goes back to the page.
PICK_PROMPT = "Choose a folder for Cutting Room Floor to look at"
_PICKING = threading.Lock()


def can_pick() -> bool:
    return IS_WIN or IS_MAC or bool(shutil.which("zenity") or shutil.which("kdialog"))


def pick_folder() -> dict:
    if not _PICKING.acquire(blocking=False):
        return dict(error="A folder window is already open. It may be behind your browser.")
    try:
        if IS_MAC:
            r = _run(["osascript", "-e", "on run argv",
                      "-e", "POSIX path of (choose folder with prompt (item 1 of argv) default location (POSIX file (item 2 of argv)))",
                      "-e", "end run", PICK_PROMPT, str(HOME)], timeout=900)
            if r.returncode != 0:
                return dict(cancelled=True) if "-128" in r.stderr else dict(error="The folder window couldn't open.")
        elif IS_WIN:
            script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; "
                      "$d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = $env:CRF_PROMPT; "
                      "$d.ShowNewFolderButton = $false; $d.SelectedPath = $env:USERPROFILE; "
                      "$o = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false}; "
                      "if ($d.ShowDialog($o) -eq 'OK') { $d.SelectedPath }")
            r = _run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                     env=dict(os.environ, CRF_PROMPT=PICK_PROMPT), timeout=900)
            if r.returncode != 0:
                return dict(error="The folder window couldn't open.")
        else:
            if shutil.which("zenity"):
                cmd = ["zenity", "--file-selection", "--directory", "--title=" + PICK_PROMPT, "--filename=" + str(HOME) + "/"]
            elif shutil.which("kdialog"):
                cmd = ["kdialog", "--getexistingdirectory", str(HOME), "--title", PICK_PROMPT]
            else:
                return dict(error="No folder window is available here. Paste the folder's location instead.")
            r = _run(cmd, timeout=900)
        chosen = (r.stdout or "").strip()
        if not chosen:
            return dict(cancelled=True)
        return dict(path=chosen.rstrip("/\\") or chosen)
    except subprocess.TimeoutExpired:
        return dict(cancelled=True)
    finally:
        _PICKING.release()


# ---- version and updates ---------------------------------------------------
def app_version() -> str:
    try:
        return json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text("utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return VERSION


def install_type() -> str:
    """How this copy was installed, so the page can show the right way to update."""
    if "/.claude/plugins/" in str(Path(__file__).resolve()).replace("\\", "/"):
        return "claude"
    return "git" if (PLUGIN_ROOT / ".git").exists() else "zip"


def _vtuple(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def check_update() -> dict:
    """Ask GitHub for the latest version number (and its notes). Nothing else is sent."""
    current = app_version()
    headers = {"User-Agent": f"cutting-room-floor/{current}"}

    def fetch(path):
        req = urllib.request.Request(UPDATE_BASE + path, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.read().decode("utf-8", "ignore")

    try:
        latest = json.loads(fetch("/.claude-plugin/plugin.json"))["version"]
    except urllib.error.HTTPError:
        return dict(current=current, error="Update information isn't available right now. Try again later.")
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            hint = (" On a Mac, open the Python folder in Applications and run Install Certificates." if IS_MAC else "")
            return dict(current=current, error="Python couldn't verify GitHub's security certificate." + hint)
        return dict(current=current, error="Couldn't reach GitHub. Check your internet connection and try again.")
    except (OSError, ValueError, KeyError):
        return dict(current=current, error="Couldn't read the update information. Try again later.")
    newer = _vtuple(latest) > _vtuple(current)
    notes = ""
    if newer:
        try:
            m = re.search(r"^## *" + re.escape(latest) + r"[^\n]*\n(.*?)(?=^## |\Z)", fetch("/CHANGELOG.md"), re.S | re.M)
            notes = m.group(1).strip()[:1500] if m else ""
        except Exception:
            pass
    return dict(current=current, latest=latest, newer=newer, notes=notes, install=install_type(),
                repo=REPO_URL, folder=str(PLUGIN_ROOT))


# ---- the local web page ----------------------------------------------------
def csp(nonce: str) -> str:
    """Only the page's own script runs, nothing can frame it, and it only talks to this server."""
    return ("default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            "style-src 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", policy=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", policy or "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def trusted(self, need_token=True) -> bool:
        """Turn away anything that isn't this page on this computer.
        The Host check stops DNS rebinding: a website that points its own name at
        127.0.0.1 still sends its own name as the Host, so it gets nothing back."""
        port = self.server.server_address[1]
        local = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if (self.headers.get("Host") or "").lower() not in local:
            self.send(403, json.dumps({"error": "wrong host"}))
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {"http://" + h for h in local}:
            self.send(403, json.dumps({"error": "wrong origin"}))
            return False
        if need_token and not secrets.compare_digest(self.headers.get("X-Token") or "", TOKEN):
            self.send(403, json.dumps({"error": "bad token"}))
            return False
        return True

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            if not self.trusted(need_token=False):
                return
            nonce = secrets.token_urlsafe(16)
            html = Path(__file__).with_name("cutting_room_floor.html").read_text("utf-8")
            html = html.replace("__TOKEN__", TOKEN).replace("__NONCE__", nonce)
            return self.send(200, html, "text/html; charset=utf-8", policy=csp(nonce))
        if not self.trusted():
            return
        if path == "/state":
            out = {k: STATE[k] for k in ("status", "progress", "data", "error")}
            out.update(os=OS_INFO, home=str(HOME), sep=os.sep, version=app_version(), install=install_type(), can_pick=can_pick(),
                       check_updates=bool(load_config().get("check_updates", False)))
            return self.send(200, json.dumps(out))
        if path == "/suggest":
            cur = [str(r) for r in self.server.roots]
            hidden = load_config().get("hidden", [])
            return self.send(200, json.dumps(dict(suggestions=suggest_folders(cur, hidden), caches=self.server.caches,
                                                  first_run=not cur)))
        self.send(404, "{}")

    def do_POST(self):
        if not self.trusted():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send(400, json.dumps({"error": "bad request"}))
        path = self.path.split("?")[0]
        if path == "/setup":
            roots, problems = clean_roots(body.get("roots") or [])
            if not roots:
                return self.send(400, json.dumps({"error": " ".join(problems) or "Pick at least one folder."}))
            caches = bool(body.get("caches", True))
            cfg = load_config()
            # Folders removed from the list stay hidden from suggestions until picked or pasted again.
            listed = {same_path(p) for p in body.get("listed") or []} | {same_path(r) for r in roots}
            hidden = {same_path(h): h for h in cfg.get("hidden", []) + [str(h) for h in body.get("hide") or []]}
            hidden = [h for k, h in hidden.items() if k not in listed][:200]
            save_config(dict(cfg, roots=[str(r) for r in roots], caches=caches, hidden=hidden))
            self.server.roots, self.server.caches = roots, caches
            if STATE["status"] != "scanning":
                threading.Thread(target=run_scan, args=(roots, caches), daemon=True).start()
            return self.send(200, json.dumps({"ok": True, "problems": problems}))
        if path == "/reveal":
            it = ITEMS.get(body.get("id"))
            if it and os.path.lexists(it["path"]):
                reveal(it["path"])
            return self.send(200, "{}")
        if path == "/trash":
            results = []
            for iid in body.get("ids", []):
                it = ITEMS.get(iid)
                if not it:
                    results.append(dict(id=iid, ok=False, msg="unknown item"))
                    continue
                try:
                    results.append(dict(id=iid, ok=True, msg=move_to_trash(it["path"], it["size"])))
                except Exception as e:
                    results.append(dict(id=iid, ok=False, msg=str(e)))
            return self.send(200, json.dumps({"results": results}))
        if path == "/pick-folder":
            return self.send(200, json.dumps(pick_folder()))
        if path == "/check-update":
            return self.send(200, json.dumps(check_update()))
        if path == "/prefs":
            if "check_updates" in body:
                save_config(dict(load_config(), check_updates=bool(body["check_updates"])))
            return self.send(200, "{}")
        if path == "/rescan":
            if STATE["status"] != "scanning" and self.server.roots:
                threading.Thread(target=run_scan, args=(self.server.roots, self.server.caches), daemon=True).start()
            return self.send(200, "{}")
        self.send(404, "{}")


# ---- letting Claude read the results ----------------------------------------
# The running server keeps its address and token in a file only this person can
# read, so --results can fetch the findings without the token ever being printed.
def session_path() -> Path:
    return config_path().with_name("session.json")


def write_session(url: str):
    p = session_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dict(url=url, token=TOKEN, pid=os.getpid()), f)
        if not IS_WIN:
            os.chmod(p, 0o600)
    except OSError:
        pass


def clear_session(url: str):
    try:
        if json.loads(session_path().read_text("utf-8")).get("url") == url:
            session_path().unlink()
    except (OSError, ValueError):
        pass


def print_results():
    try:
        s = json.loads(session_path().read_text("utf-8"))
        req = urllib.request.Request(s["url"] + "/state", headers={"X-Token": s["token"]})
        with urllib.request.urlopen(req, timeout=5) as r:
            print(r.read().decode("utf-8"))
    except (OSError, ValueError, KeyError):
        sys.exit("Cutting Room Floor isn't running. Start it first.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roots", nargs="+", metavar="FOLDER", help="Scan these folders this time, without changing your saved folders")
    ap.add_argument("--setup", action="store_true", help="Choose which folders to look at, in the page")
    ap.add_argument("--set-roots", nargs="+", metavar="FOLDER", help="Save these as your folders, then exit")
    ap.add_argument("--suggest", action="store_true", help="Print suggested project folders as JSON, then exit")
    ap.add_argument("--show-config", action="store_true", help="Print your saved settings as JSON, then exit")
    ap.add_argument("--no-caches", action="store_true", help="Skip app and download caches")
    ap.add_argument("--port", type=int, default=8765, help="Port for the page (the next free one is used if taken)")
    ap.add_argument("--no-browser", action="store_true", help="Do not open the page automatically")
    ap.add_argument("--results", action="store_true", help="Print the running scan's findings as JSON, then exit")
    ap.add_argument("--check-update", action="store_true", help="Ask GitHub whether a newer version exists, print JSON, then exit")
    ap.add_argument("--version", action="version", version=f"Cutting Room Floor {app_version()}")
    a = ap.parse_args()
    cfg = load_config()

    if a.check_update:
        print(json.dumps(check_update(), indent=2))
        return
    if a.results:
        print_results()
        return
    if a.show_config:
        print(json.dumps(dict(config_file=str(config_path()), roots=cfg.get("roots", []),
                              caches=cfg.get("caches", True)), indent=2))
        return
    if a.suggest:
        print(json.dumps(suggest_folders(cfg.get("roots", []), cfg.get("hidden", [])), indent=2))
        return
    if a.set_roots:
        roots, problems = clean_roots(a.set_roots)
        if not roots:
            sys.exit("No usable folders. " + " ".join(problems))
        caches = cfg.get("caches", True) if not a.no_caches else False
        save_config(dict(cfg, roots=[str(r) for r in roots], caches=caches))
        print(json.dumps(dict(saved=[str(r) for r in roots], problems=problems, config_file=str(config_path())), indent=2))
        return

    caches = False if a.no_caches else cfg.get("caches", True)
    if a.roots:
        roots, problems = clean_roots(a.roots)
        if not roots:
            sys.exit("No usable folders. " + " ".join(problems))
    else:
        roots, _ = clean_roots(cfg.get("roots", []))

    srv = None
    for port in range(a.port, a.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not srv:
        sys.exit(f"No free port between {a.port} and {a.port + 19}. Try --port with another number.")
    srv.roots, srv.caches = roots, caches
    if roots and not a.setup:
        threading.Thread(target=run_scan, args=(roots, caches), daemon=True).start()
    else:
        STATE["status"] = "setup"

    url = f"http://127.0.0.1:{port}"
    write_session(url)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # stopping it from Claude also tidies up
    print(f"Cutting Room Floor is running at {url}  (press Ctrl+C to stop)", flush=True)
    if STATE["status"] == "setup":
        print("Pick your project folders in the page to start the scan.", flush=True)
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        clear_session(url)


if __name__ == "__main__":
    main()
