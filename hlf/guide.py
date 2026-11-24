"""Guided mode: run `hlf` with no command and it walks you through a trip, step by step.

Every question says whether it's required or optional, why it's asked, and its default (Enter takes it).
At any question: ? explains more, b goes back, q quits. Nothing here deletes, rejects or exports
without the owner choosing it.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__, analyze, config, db, export, media, score

PROFILE_HELP = {
    "mtb": "mountain biking: GoPro motion, jumps and speed count most",
    "snowboard": "snowboarding: GoPro motion, airtime and speed count most",
    "pickleball": "pickleball: movement on screen and sound count most",
    "travel": "travel: good-looking shots and sound count most",
}
CAMERA_FOLDERS = re.compile(r"^(dcim|\d{3}gopro|\d{3}media|camera|videos?|movies?)$", re.I)
PORT = 8765


class Back(Exception):
    """The owner typed b."""


class Quit(Exception):
    """The owner typed q, Ctrl+C or Ctrl+D at a question."""


def tty_keys():
    """A reader of single key presses for a real terminal ("up", "down", "enter", "esc" or the character),
    or None when there's no terminal (then menus fall back to typing a number). HLF_PLAIN=1 forces typing."""
    if os.environ.get("HLF_PLAIN") or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        import select as sel
        import termios
        import tty
    except ImportError:  # Windows
        return None

    fd = sys.stdin.fileno()

    def read() -> str:
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            seq = b""
            while len(seq) < 2 and sel.select([fd], [], [], 0.05)[0]:
                seq += os.read(fd, 1)
            return {b"[A": "up", b"[B": "down", b"OA": "up", b"OB": "down"}.get(seq, "esc" if not seq else "other")
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b"\x04":
            raise EOFError
        return ch.decode(errors="ignore")

    @contextlib.contextmanager
    def session():
        """Keys arrive one at a time for the whole menu (Ctrl+C still interrupts); restored after."""
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    read.session = session
    return read


class UI:
    """Questions and messages. `inp`, `out` and `keys` are swappable so tests can script a session.

    With `keys` (a function returning the next key press), menus and choices use the arrow keys;
    without it, they ask for a number. Free text (folders, names) is always typed.
    """

    def __init__(self, inp=input, out=None, color: bool | None = None, keys="auto"):
        self.inp = inp
        self.out = out or sys.stdout
        tty = hasattr(self.out, "isatty") and self.out.isatty()
        self.tty = tty
        self.color = tty and not os.environ.get("NO_COLOR") if color is None else color
        if keys == "auto":
            keys = tty_keys() if (inp is input and self.out is sys.stdout) else None
        self.keys = keys

    def _key(self) -> str:
        try:
            return self.keys()
        except (EOFError, KeyboardInterrupt, StopIteration):
            self.out.write("\033[?25h")
            self.say()
            raise Quit from None

    def select(self, options: list[tuple[str, str, bool]], default: int = 0, back: bool = False,
               help: str | None = None) -> str:
        """Arrow-key picker. options: (key, label, enabled). Up/down (or k/j) move, Enter chooses,
        a number or an option's own key jumps straight to it. Returns the chosen key."""
        on = [i for i, (_, _, e) in enumerate(options) if e]
        cur = default if default in on else on[0]
        width = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
        hint = "  ↑↓ move   Enter choose" + ("   ? help" if help else "") + ("   b back" if back else "") + "   q quit"
        rows = len(options) + 1

        def draw(first: bool) -> None:
            if not first:
                self.out.write(f"\033[{rows}A")
            for i, (_, label, enabled) in enumerate(options):
                text = f"  {'❯' if i == cur else ' '} {label}"[:width]
                text = self.b(text) if i == cur else (text if enabled else self.dim(text))
                self.out.write("\r\033[K" + text + "\n")
            self.out.write("\r\033[K" + self.dim(hint[:width]) + "\n")
            self.out.flush()

        def step(d: int) -> int:
            i = on.index(cur)
            return on[(i + d) % len(on)]

        self.out.write("\033[?25l")  # hide the cursor while choosing
        draw(True)
        raw = getattr(self.keys, "session", None)
        try:
            with raw() if raw else contextlib.nullcontext():
                while True:
                    k = self._key()
                    if k in ("up", "k") and not any(o[0] == "k" for o in options):
                        cur = step(-1)
                    elif k in ("down", "j") and not any(o[0] == "j" for o in options):
                        cur = step(1)
                    elif k == "enter":
                        return options[cur][0]
                    elif k.lower() == "q" and not any(o[0] == "q" for o in options):
                        raise Quit
                    elif k in ("b", "esc") and back:
                        raise Back
                    elif k == "?" and help:
                        self.out.write(f"  {help}\n")
                        draw(True)
                        continue
                    else:
                        hit = [i for i, o in enumerate(options) if o[0] == k.lower()]
                        if not hit and k.isdigit() and 1 <= int(k) <= len(options):
                            hit = [int(k) - 1]
                        if hit and hit[0] in on:
                            cur = hit[0]
                            draw(False)
                            return options[cur][0]
                        continue
                    draw(False)
        finally:
            self.out.write("\033[?25h")
            self.out.flush()

    def say(self, s: str = "") -> None:
        print(s, file=self.out, flush=True)

    def b(self, s: str) -> str:
        return f"\033[1m{s}\033[0m" if self.color else s

    def dim(self, s: str) -> str:
        return f"\033[2m{s}\033[0m" if self.color else s

    def _read(self, prompt: str) -> str:
        try:
            return self.inp(prompt)
        except (EOFError, KeyboardInterrupt):
            self.say()
            raise Quit from None

    def ask(self, title: str, *, why: str, required: bool = True, default: str | None = None,
            choices: dict[str, str] | None = None, example: str | None = None, more: str | None = None,
            check=None, back: bool = True):
        """One question. Returns the checked answer, or None for a skipped optional one."""
        self.say()
        self.say(self.b(f"{title} ({'required' if required else 'optional'})"))
        self.say(f"  {why}")
        keys = list(choices or {})
        if self.keys and keys:
            if example:
                self.say(f"  Example: {example}")
            start = keys.index(default) if default in keys else 0
            got = self.select([(k, f"{k:<11} {choices[k]}", True) for k in keys], start, back=back, help=more)
            if check:
                return check(got)
            return got
        for n, k in enumerate(keys, 1):
            self.say(f"    {n}) {k:<11} {choices[k]}")
        if example:
            self.say(f"  Example: {example}")
        keys_hint = "Enter = " + (default if default else "skip") if (default or not required) else ""
        self.say(self.dim("  " + "   ".join(x for x in (keys_hint, "? = more help", "b = back" if back else "", "q = quit") if x)))
        while True:
            raw = self._read(f"  > [{default}] " if default else "  > ").strip()
            low = raw.lower()
            if low == "?":
                self.say(f"  {more or why}")
                continue
            if low == "q":
                raise Quit
            if low == "b" and back:
                raise Back
            if not raw:
                if default is not None:
                    raw = default
                elif not required:
                    return None
                else:
                    self.say("  This one is required. Type an answer, or ? for help.")
                    continue
            if keys:
                if raw.isdigit() and 1 <= int(raw) <= len(keys):
                    raw = keys[int(raw) - 1]
                match = [k for k in keys if k.lower() == raw.lower()] or [k for k in keys if k.lower().startswith(raw.lower())]
                if len(match) != 1:
                    self.say(f"  Pick one of: {', '.join(keys)} (or its number).")
                    continue
                raw = match[0]
            if check:
                try:
                    return check(raw)
                except ValueError as e:
                    self.say(f"  {e}")
                    continue
            return raw

    def yes(self, question: str, default: bool = True, back: bool = False) -> bool:
        if self.keys:
            self.say(question)
            return self.select([("y", "Yes", True), ("n", "No", True)], 0 if default else 1, back=back) == "y"
        hint = "Y/n" if default else "y/N"
        while True:
            raw = self._read(f"{question} [{hint}] ").strip().lower()
            if raw == "q":
                raise Quit
            if raw == "b" and back:
                raise Back
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            self.say("  Type y or n" + (", b to go back" if back else "") + ", or q to quit.")

    def menu(self, title: str, options: list[tuple[str, str, bool]]) -> str:
        """options: (key, label, enabled). Returns the chosen key; q always quits."""
        self.say()
        self.say(self.b(title))
        if self.keys:
            got = self.select([*((k, f"{k}) {label}", on) for k, label, on in options), ("q", "q) Quit", True)])
            if got == "q":
                raise Quit
            return got
        for key, label, on in options:
            self.say(f"  {key}) {label}" if on else self.dim(f"  {key}) {label}"))
        self.say(self.dim("  q) Quit"))
        valid = {k for k, _, on in options if on}
        while True:
            raw = self._read("  > ").strip().lower()
            if raw == "q":
                raise Quit
            if raw in valid:
                return raw
            if any(raw == k for k, _, _ in options):
                self.say("  That one isn't available yet.")
            else:
                self.say("  Type the number of an option, or q to quit.")


# ---------- footage helpers ----------

def split_paths(raw: str) -> list[str]:
    """Paths as typed or dragged into Terminal: quotes, `\\ ` escapes and several at once all work."""
    raw = raw.strip()
    if Path(raw).expanduser().exists():  # typed or pasted with plain spaces
        return [raw]
    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = [raw.strip("'\"")]
    return [p for p in parts if p]


def is_gopro(p: Path) -> bool:
    return media.chapter_key(p) is not None


def footage_summary(paths: list[Path]) -> dict:
    files = media.discover(paths)
    gopro = sum(is_gopro(f) for f in files)
    size = sum(f.stat().st_size for f in files)
    hours = None
    if shutil.which("ffprobe") and files:
        secs = 0.0
        for f in files:
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                               capture_output=True, text=True)
            try:
                secs += float(r.stdout.strip() or 0)
            except ValueError:
                pass
        hours = secs / 3600
    return {"files": len(files), "gopro": gopro, "other": len(files) - gopro, "gb": size / 1e9, "hours": hours}


def describe_footage(s: dict) -> str:
    kinds = ", ".join(x for x in (f"{s['gopro']} GoPro" if s["gopro"] else "", f"{s['other']} phone/other" if s["other"] else "") if x)
    length = f", {fmt_length(s['hours'] * 3600)}" if s["hours"] is not None else ""
    return f"Found {s['files']} videos ({kinds}){length}, {s['gb']:.1f} GB"


def check_folders(home: Path):
    def check(raw: str) -> list[Path]:
        paths = [Path(p).expanduser() for p in split_paths(raw)]
        for p in paths:
            if not p.exists():
                hint = " Is the drive plugged in?" if str(p).startswith("/Volumes/") else ""
                raise ValueError(f"Can't find {p}.{hint} Check the spelling, or drag the folder into this window.")
            if p.resolve().is_relative_to(home.resolve()):
                raise ValueError("That's the program's own working folder. Pick the folder with your footage.")
        if not media.discover(paths):
            raise ValueError("No videos (.mp4, .mov, .m4v) in there. Pick the folder that holds the trip's video files.")
        return paths
    return check


def trip_name_default(paths: list[Path]) -> str:
    p = paths[0].expanduser().resolve()
    if p.is_file():
        p = p.parent
    while CAMERA_FOLDERS.match(p.name) and p.parent != p:
        p = p.parent
    return p.name or "My trip"


def fmt_length(seconds: float) -> str:
    """Footage length: '6.2 hours', '25 min', '40 seconds'."""
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} seconds"


def fmt_left(seconds: float) -> str:
    if seconds < 60:
        return "under a minute left"
    return f"{fmt_minutes(seconds)} left"


def fmt_minutes(seconds: float) -> str:
    m = seconds / 60
    if m < 1:
        return f"{seconds:.0f} seconds"
    if m < 1.5:
        return "about 1 min"
    if m < 90:
        return f"about {m:.0f} min"
    return f"about {m / 60:.1f} hours"


# ---------- remembered answers ----------

def _memory_path(home: Path) -> Path:
    return home / "guide.json"


def remember(home: Path, **kw) -> None:
    p = _memory_path(home)
    data = recall(home)
    data.update(kw)
    home.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def recall(home: Path) -> dict:
    try:
        return json.loads(_memory_path(home).read_text())
    except (OSError, ValueError):
        return {}


# ---------- trip state ----------

def trip_stats(con, tid: int) -> dict:
    q = lambda sql: con.execute(sql, (tid,)).fetchone()[0]  # noqa: E731
    return {
        "clips": q("SELECT COUNT(*) FROM clips WHERE trip_id=?"),
        "seconds": q("SELECT COALESCE(SUM(duration),0) FROM clips WHERE trip_id=?"),
        "read": q("SELECT COUNT(*) FROM signal_runs r JOIN clips c ON c.id=r.clip_id "
                  "WHERE c.trip_id=? AND r.stage='signals' AND r.status='done'"),
        "unscored": q("SELECT COUNT(*) FROM clips WHERE trip_id=? AND bucket='pending'"),
        "junk": q("SELECT COUNT(*) FROM clips WHERE trip_id=? AND bucket='suggested_reject'"),
        "telemetry": q("SELECT COUNT(*) FROM clips WHERE trip_id=? AND has_gpmf=1"),
        "picks": q("SELECT COUNT(*) FROM segments s JOIN clips c ON c.id=s.clip_id "
                   "WHERE c.trip_id=? AND s.status='proposed' AND c.bucket NOT IN ('rejected','suggested_reject')"),
        "picks_s": q("SELECT COALESCE(SUM(end_s-start_s),0) FROM segments s JOIN clips c ON c.id=s.clip_id "
                     "WHERE c.trip_id=? AND s.status='proposed' AND c.bucket NOT IN ('rejected','suggested_reject')"),
        "kept": q("SELECT COUNT(*) FROM segments s JOIN clips c ON c.id=s.clip_id WHERE c.trip_id=? AND s.status='kept'"),
        "kept_s": q("SELECT COALESCE(SUM(end_s-start_s),0) FROM segments s JOIN clips c ON c.id=s.clip_id "
                    "WHERE c.trip_id=? AND s.status='kept'"),
    }


def needs_work(st: dict) -> bool:
    return st["clips"] > 0 and (st["read"] < st["clips"] or st["unscored"] > 0)


def trip_line(con, t) -> str:
    st = trip_stats(con, t["id"])
    if needs_work(st):
        state = f"{st['read']} of {st['clips']} clips read"
    elif st["kept"]:
        state = f"{st['kept']} kept, {st['picks']} to review"
    else:
        state = f"{st['picks']} picks to review"
    return f"{t['name']}: {state}"


def missing_files(con, tid: int) -> list[str]:
    rows = con.execute("SELECT path FROM source_files WHERE trip_id=?", (tid,)).fetchall()
    return [r["path"] for r in rows if not Path(r["path"]).exists()]


# ---------- progress ----------

class Progress:
    """One line per stage: a bar, a count, time left and the current file. Redrawn in place in a terminal."""

    def __init__(self, ui: UI):
        self.ui = ui
        self.start = time.monotonic()
        self.first_done_s: float | None = None
        self.last_len = 0

    def bar(self, frac: float, width: int = 20) -> str:
        n = max(0, min(width, round(frac * width)))
        return "[" + "█" * n + "░" * (width - n) + "]"

    def line(self, text: str) -> None:
        if self.ui.tty:
            self.ui.out.write("\r\033[K" + text)
            self.ui.out.flush()
        else:
            self.ui.say(text)

    def done(self, label: str, detail: str) -> None:
        text = f"  ✓ {label:<17} {detail}"
        if self.ui.tty:
            self.ui.out.write("\r\033[K" + text + "\n")
            self.ui.out.flush()
        else:
            self.ui.say(text)
        self.start = time.monotonic()
        self.first_done_s = None

    def finding(self, i: int, n: int, f: Path) -> None:
        self.line(f"  ▸ {'Finding videos':<17} {self.bar(i / max(n, 1))} {i}/{n}  {f.name}")

    def reading(self, i: int, n: int, clip, done_s: float, total_s: float) -> None:
        if clip is None:
            return
        now = time.monotonic()
        if self.first_done_s is None:
            self.first_done_s, self.start = done_s, now
        left = ""
        got, spent = done_s - self.first_done_s, now - self.start
        if got > 0 and spent > 5:
            left = f"  {fmt_left((total_s - done_s) * spent / got)}"
        self.line(f"  ▸ {'Reading clips':<17} {self.bar(done_s / max(total_s, 1))} {i}/{n}{left}  {clip['name']}")


class Collect:
    """Quiet log that remembers failures instead of printing over the progress line."""

    def __init__(self):
        self.current = ""
        self.failed: list[str] = []

    def __call__(self, msg: str) -> None:
        m = re.match(r"\[\d+/\d+\] (\S+)", msg)
        if m:
            self.current = m.group(1)
        elif msg.strip().startswith("failed:"):
            self.failed.append(f"{self.current}: {msg.strip()[7:].strip()}")


# ---------- the guide ----------

class Guide:
    def __init__(self, home: Path, ui: UI | None = None, serve=None, open_url=None, reveal=None):
        self.home = home
        self.ui = ui or UI()
        self.con = db.connect(home)
        self._serve = serve
        self._open_url = open_url or webbrowser.open
        self._reveal = reveal

    # ----- entry -----
    def run(self) -> None:
        ui = self.ui
        ui.say(ui.b(f"Highlight Finder {__version__}"))
        ui.say("Finds the best moments in your trip footage. You make every final call; your originals are only read.")
        missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
        if missing:
            ui.say(f"\n  Heads up: {' and '.join(missing)} isn't installed, so reading footage won't work.")
            ui.say("  Fix: run the install line again (see the README), or `brew install ffmpeg`.")
        try:
            while True:
                self.main_menu()
        except Quit:
            ui.say("Bye. Run `hlf` any time to pick up where you left off.")

    def trips(self):
        return self.con.execute("SELECT * FROM trips ORDER BY created_at DESC").fetchall()

    def main_menu(self) -> None:
        trips = self.trips()
        unfinished = [t for t in trips if needs_work(trip_stats(self.con, t["id"]))]
        cont = "Continue a trip" + (f"   ({trip_line(self.con, unfinished[0])})" if unfinished else "")
        choice = self.ui.menu("What would you like to do?", [
            ("1", "Start a new trip (or add footage to one)", True),
            ("2", cont, bool(trips)),
            ("3", "Review highlights", bool(trips)),
            ("4", "Make the Resolve rough cut", bool(trips)),
            ("5", "Make the final cut   (coming in a later version)", True),
            ("6", "Check setup and settings", True),
        ])
        try:
            if choice == "1":
                self.new_trip()
            elif choice == "2":
                t = self.pick_trip("Which trip?", prefer=unfinished)
                self.continue_trip(t)
            elif choice == "3":
                self.review(self.pick_trip("Review which trip?"))
            elif choice == "4":
                self.rough_cut(self.pick_trip("Rough cut for which trip?"))
            elif choice == "5":
                self.final_cut_info()
            elif choice == "6":
                self.setup()
        except Back:
            pass

    def pick_trip(self, title: str, prefer=None):
        trips = list(prefer or []) + [t for t in self.trips() if not prefer or t["id"] not in {p["id"] for p in prefer}]
        if len(trips) == 1:
            self.ui.say(f"\nTrip: {trips[0]['name']}")
            return trips[0]
        choices = {t["name"]: trip_line(self.con, t).split(": ", 1)[1] for t in trips}
        name = self.ui.ask(title, why="Your trips, newest first.", choices=choices, default=trips[0]["name"])
        return next(t for t in trips if t["name"] == name)

    # ----- new trip -----
    def new_trip(self) -> None:
        mem = recall(self.home)
        ans: dict = {}
        steps = [self.q_folder, self.q_name, self.q_profile, self.q_more, self.q_confirm]
        i = 0
        while i < len(steps):
            try:
                steps[i](ans, mem)
                i += 1
            except Back:
                if i == 0:
                    return
                i -= 1
        trip = db.get_trip(self.con, ans["name"], create_profile=ans["profile"])
        if trip["profile"] != ans["profile"]:
            self.con.execute("UPDATE trips SET profile=? WHERE id=?", (ans["profile"], trip["id"]))
            self.con.commit()
            trip = db.get_trip(self.con, ans["name"])
        remember(self.home, last_folder=str(ans["paths"][0]), last_profile=ans["profile"])
        self.process(trip, ans["paths"])

    def q_folder(self, ans, mem) -> None:
        last = mem.get("last_folder")
        paths = self.ui.ask(
            "Footage folder",
            why="Where this trip's videos are. You can type the path or drag the folder into this window.",
            example=last or "/Volumes/T7/Moab",
            more=("The folder can hold sub-folders (like DCIM/100GOPRO); every .mp4, .mov and .m4v inside is found. "
                  "Drag in several folders at once if the trip is spread out. Your files are only read, never changed."),
            check=check_folders(self.home))
        ans["paths"] = paths
        self.ui.say("  Looking at the footage...")
        ans["summary"] = footage_summary(paths)
        self.ui.say(f"  {describe_footage(ans['summary'])}")

    def q_name(self, ans, mem) -> None:
        def check(raw: str) -> str:
            raw = raw.strip()
            if len(raw) > 60:
                raise ValueError("Keep it under 60 characters.")
            return raw
        ans["name"] = self.ui.ask(
            "Trip name", why="How this trip shows up in the review page and the export folder.",
            default=ans.get("name") or trip_name_default(ans["paths"]), check=check,
            more="Use the same name as an earlier trip to add these videos to it; videos already read are skipped.")
        t = self.con.execute("SELECT * FROM trips WHERE name=?", (ans["name"],)).fetchone()
        ans["existing"] = t
        if t:
            st = trip_stats(self.con, t["id"])
            self.ui.say(f"  '{t['name']}' already exists ({st['clips']} clips). New videos will be added; ones already read are skipped.")

    def q_profile(self, ans, mem) -> None:
        default = (ans["existing"]["profile"] if ans.get("existing") else None) or mem.get("last_profile") or "mtb"
        ans["profile"] = self.ui.ask(
            "Activity", why="Sets what counts as a highlight.", choices=PROFILE_HELP, default=default,
            more="It changes how motion, sound and looks are weighted. You can re-score later with another one.")

    def q_more(self, ans, mem) -> None:
        while True:
            more = self.ui.ask(
                "Another folder for this trip", required=False,
                why="Add one if part of the trip is somewhere else, like phone videos on another drive.",
                check=check_folders(self.home))
            if not more:
                return
            ans["paths"] = list(dict.fromkeys([*ans["paths"], *more]))
            ans["summary"] = footage_summary(ans["paths"])
            self.ui.say(f"  Now: {describe_footage(ans['summary'])}")

    def q_confirm(self, ans, mem) -> None:
        ui, s = self.ui, ans["summary"]
        ui.say()
        ui.say(ui.b("Ready to start"))
        ui.say(f"  Trip:      {ans['name']}")
        ui.say(f"  Activity:  {ans['profile']}")
        for p in ans["paths"]:
            ui.say(f"  Footage:   {p}")
        ui.say(f"  {describe_footage(s)}")
        need = (s["hours"] or s["gb"] / 10) * 1e9
        try:
            free = shutil.disk_usage(self.home if self.home.exists() else self.home.parent).free
            ui.say(f"  Working files go in {self.home} (about 1 GB per hour of footage; {free / 1e9:.0f} GB free).")
            if free < need * 1.2:
                ui.say("  Warning: that might not be enough space. Free some up, or ask Claude to use another --home.")
        except OSError:
            pass
        ui.say("  Time left shows after the first few clips. It's safe to stop any time (Ctrl+C) and continue later.")
        cmd = ["hlf", "run", "--trip", ans["name"], "--profile", ans["profile"], *map(str, ans["paths"])]
        ui.say(ui.dim("  Same thing as a command: " + " ".join(shlex.quote(c) for c in cmd)))
        if not ui.yes("Start?", default=True, back=True):
            raise Back

    # ----- running -----
    def process(self, trip, paths: list[Path] | None) -> None:
        ui = self.ui
        settings = config.load(self.home)
        pr = Progress(ui)
        log = Collect()
        ui.say()
        ui.say(ui.b(f"Working on {trip['name']}"))
        try:
            if paths:
                r = media.ingest(self.con, trip["id"], paths, log=log, progress=pr.finding)
                pr.done("Finding videos", f"{r['files_found']} files, {r['new_clips']} new clips")
            trip = db.get_trip(self.con, trip["name"])
            st = trip_stats(self.con, trip["id"])
            r = analyze.analyze_trip(self.con, self.home, trip, settings, log=log, progress=pr.reading)
            pr.done("Reading clips", f"{r['analyzed']} of {r['clips']} read"
                    + (f", {st['telemetry']} with GoPro sensor data" if st["telemetry"] else "")
                    + (f", {r['failed']} couldn't be read" if r["failed"] else ""))
            pr.line(f"  ▸ {'Scoring':<17} finding junk and highlights...")
            score.score_trip(self.con, self.home, trip, settings, log=lambda *_: None)
            st = trip_stats(self.con, trip["id"])
            pr.done("Scoring", f"{st['picks']} picks, {st['junk']} possible junk")
        except KeyboardInterrupt:
            ui.say("\n")
            ui.say("  Paused. Everything finished so far is saved.")
            ui.say("  Run `hlf` and pick 'Continue a trip' to pick up where it stopped.")
            return
        if log.failed:
            ui.say()
            ui.say(f"  {len(log.failed)} clip(s) couldn't be read. They'll be retried next time you continue this trip:")
            for f in log.failed[:5]:
                ui.say(f"    {f[:110]}")
        self.finished(trip)

    def continue_trip(self, trip) -> None:
        st = trip_stats(self.con, trip["id"])
        if not needs_work(st):
            self.ui.say(f"\n{trip['name']} is already read and scored.")
            self.finished(trip)
            return
        gone = missing_files(self.con, trip["id"])
        if gone:
            self.ui.say(f"\n  {len(gone)} of this trip's videos can't be found, for example {gone[0]}")
            self.ui.say("  Is the drive plugged in? If the folder moved or the drive has a new name, point to it.")
            choice = self.ui.menu("What now?", [
                ("1", "I plugged it in, check again", True),
                ("2", "The footage is somewhere else now, I'll point to it", True),
                ("3", "Continue anyway (missing ones are retried later)", True),
            ])
            if choice == "1":
                return self.continue_trip(trip)
            if choice == "2":
                paths = self.ui.ask("Footage folder", why="Where this trip's videos are now. Videos are matched by content, not name.",
                                    check=check_folders(self.home))
                return self.process(trip, paths)
        self.process(trip, None)

    def finished(self, trip) -> None:
        ui = self.ui
        while True:
            st = trip_stats(self.con, trip["id"])
            ui.say()
            ui.say(ui.b(f"{trip['name']}: {st['clips']} clips, {fmt_length(st['seconds'])}"))
            ui.say(f"  {st['junk']} look like junk (too short, lens covered, frozen or blurry). You confirm them in review.")
            ui.say(f"  {st['picks']} highlight picks to review ({fmt_minutes(st['picks_s'])})")
            if st["kept"]:
                ui.say(f"  {st['kept']} kept so far ({fmt_minutes(st['kept_s'])})")
            choice = ui.menu("Next steps", [
                ("1", "Review on this Mac (opens your browser)", True),
                ("2", "Review on your phone (same Wi-Fi; the Mac does the work)", True),
                ("3", "Make the Resolve rough cut", True),
                ("4", "Make the final cut   (coming in a later version)", True),
                ("5", "Add more footage to this trip", True),
                ("6", "Back to the main menu", True),
            ])
            try:
                if choice == "1":
                    self.review(trip)
                elif choice == "2":
                    self.review(trip, phone=True)
                elif choice == "3":
                    self.rough_cut(trip)
                elif choice == "4":
                    self.final_cut_info()
                elif choice == "5":
                    paths = self.ui.ask("Footage folder", why="More videos for this trip. Ones already read are skipped.",
                                        check=check_folders(self.home))
                    self.process(trip, paths)
                    return
                else:
                    return
            except Back:
                continue

    # ----- review, export -----
    def review(self, trip, phone: bool = False) -> None:
        from . import server
        ui = self.ui
        ui.say()
        if phone:
            ui.say("Open the phone link below in your phone's browser (same Wi-Fi as this Mac).")
            ui.say("Swipe right to keep, left to reject. The Mac keeps running the page.")
        else:
            ui.say(f"Opening the review page for {trip['name']} in your browser.")
            ui.say("Keep or reject each pick, nudge start/end, rate them. It all saves as you go.")
        ui.say("When you're done, press Ctrl+C here to come back to this menu.")
        serve = self._serve or server.serve
        for port in (PORT, PORT + 1, PORT + 2):
            if not phone:
                threading.Timer(1.0, self._open_url, args=(f"http://127.0.0.1:{port}/",)).start()
            try:
                serve(self.home, port, phone)
                return
            except OSError:
                ui.say(f"  Port {port} is busy (is review open in another window?). Trying {port + 1}.")
        ui.say("  Couldn't start the review page. Close other Highlight Finder windows and try again.")

    def rough_cut(self, trip) -> None:
        ui = self.ui
        st = trip_stats(self.con, trip["id"])
        if not st["kept"]:
            ui.say("\nYou haven't kept any picks yet, so the rough cut would be empty.")
            ui.say("Review first: keep the moments you want, then come back here.")
            return
        ui.say(f"\nYou kept {st['kept']} picks ({fmt_minutes(st['kept_s'])}).")
        if st["picks"]:
            ui.say(f"{st['picks']} picks are still unreviewed and won't be in the cut.")
        if not ui.yes("Make the rough cut now?", default=True, back=True):
            return
        out = self.home / "exports" / trip["name"].replace(" ", "_")
        r = export.rough_cut(self.con, trip, out)
        ui.say(f"  ✓ Rough cut: {r['timeline']} ({r['segments']} segments)")
        ui.say("  In DaVinci Resolve: File > Import > Timeline, then pick that .otio file.")
        ui.say("  It links to your original files, so keep the drive plugged in.")
        if sys.platform == "darwin" or self._reveal:
            if ui.yes("Show it in Finder?", default=True):
                (self._reveal or (lambda p: subprocess.run(["open", "-R", str(p)], check=False)))(r["timeline"])

    def final_cut_info(self) -> None:
        ui = self.ui
        ui.say()
        ui.say(ui.b("Final cut: coming in a later version"))
        ui.say("  It will make a finished video from your kept picks: you choose the length, music (optional),")
        ui.say("  style and stabilization, see a short preview first, and it renders the full video after your OK.")
        ui.say("  For now, use the Resolve rough cut.")

    def setup(self) -> None:
        ui = self.ui
        ui.say()
        ui.say(ui.b("Setup"))
        for name, ok, fix in checks():
            ui.say(f"  {'✓' if ok else '✗'} {name:<13} {'ok' if ok else fix}")
        ui.say(f"  Working folder: {self.home}")
        try:
            ui.say(f"  Free space there: {shutil.disk_usage(self.home).free / 1e9:.0f} GB")
        except OSError:
            pass
        ui.say(f"  Settings: `hlf settings` shows them; `hlf set KEY VALUE --reason ...` changes one (logged).")
        ui.say("  Update to the newest version: run the install line from the README again.")


def checks() -> list[tuple[str, bool, str]]:
    out = []
    for tool in ("ffmpeg", "ffprobe"):
        out.append((tool, bool(shutil.which(tool)), "missing: brew install ffmpeg"))
    hw = False
    if shutil.which("ffmpeg"):
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        hw = "h264_videotoolbox" in enc
    out.append(("videotoolbox", hw, "not available (slower software encode)"))
    try:
        import opentimelineio  # noqa: F401
        out.append(("otio", True, ""))
    except ImportError:
        out.append(("otio", False, "missing: run the install line again"))
    return out


def main(home: Path) -> None:
    Guide(home).run()
