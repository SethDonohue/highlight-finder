"""Guided mode, driven by scripted answers on made-up footage. Needs ffmpeg."""
import io
import shutil
import sys
import sqlite3
from pathlib import Path

import pytest

from hlf import analyze, guide
from hlf.cli import main

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def feeder(items):
    it = iter(items)

    def nxt(*_):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return nxt, it


def session(home, answers, keys=None, **kw):
    """Run the guide with these answers typed in order (and, with `keys`, these key presses for
    menus); return everything it printed."""
    inp, it = feeder(answers)
    kfn, kit = feeder(keys) if keys is not None else (None, iter(()))
    out = io.StringIO()
    g = guide.Guide(home, guide.UI(inp, out, color=False, keys=kfn), **kw)
    g.run()
    left, kleft = list(it), list(kit)
    assert not left, f"answers not used: {left}\n{out.getvalue()}"
    assert not kleft, f"keys not used: {kleft}\n{out.getvalue()}"
    return out.getvalue()


def picker(keys, options, **kw):
    kfn, _ = feeder(keys)
    out = io.StringIO()
    ui = guide.UI(lambda p: "", out, color=False, keys=kfn)
    return ui.select(options, **kw), out.getvalue()


# ---------- pure helpers ----------

def test_split_paths_handles_dragged_folders(tmp_path):
    plain = tmp_path / "Seth's Drive" / "My Trip"
    plain.mkdir(parents=True)
    assert guide.split_paths(f"{plain} ") == [str(plain)]          # pasted with plain spaces
    assert guide.split_paths("/Volumes/T7/My\\ Trip ") == ["/Volumes/T7/My Trip"]
    assert guide.split_paths("'/Volumes/T7/My Trip'") == ["/Volumes/T7/My Trip"]
    assert guide.split_paths("/a/b /c/d\\ e") == ["/a/b", "/c/d e"]
    assert guide.split_paths("/Volumes/Seth\\'s\\ Drive") == ["/Volumes/Seth's Drive"]  # as Terminal escapes it


def test_trip_name_skips_camera_folders(tmp_path):
    d = tmp_path / "Moab" / "DCIM" / "100GOPRO"
    d.mkdir(parents=True)
    assert guide.trip_name_default([d]) == "Moab"
    assert guide.trip_name_default([tmp_path / "Moab"]) == "Moab"


def test_ask_rules(tmp_path):
    out = io.StringIO()
    answers = iter(["", "?", "snow", "zzz", "2"])
    ui = guide.UI(lambda p: next(answers), out, color=False)
    # required: blank is refused, ? explains, a prefix picks a choice
    assert ui.ask("Activity", why="w", more="MORE", choices=guide.PROFILE_HELP) == "snowboard"
    text = out.getvalue()
    assert "(required)" in text and "This one is required" in text and "MORE" in text
    # a number picks a choice; an unknown answer is refused
    assert ui.ask("Activity", why="w", choices=guide.PROFILE_HELP) == "snowboard"
    assert "Pick one of" in out.getvalue()
    # optional: blank skips
    ui = guide.UI(lambda p: "", io.StringIO(), color=False)
    assert ui.ask("Another", why="w", required=False) is None
    # default: blank takes it
    assert ui.ask("Name", why="w", default="Moab") == "Moab"


# ---------- whole sessions ----------

@needs_ffmpeg
def test_new_trip_start_to_finish(footage, tmp_path):
    home = tmp_path / "home"
    text = session(home, [
        "1",                      # start a new trip
        "", "/nope/missing", "?", str(footage),   # folder: required, must exist, help, then real
        "Moab",                   # trip name
        "b",                      # back from activity...
        "Moab",                   # ...name again
        "pickle",                 # activity by prefix
        "",                       # no more folders
        "",                       # Start? Y
        "3",                      # next steps: rough cut (nothing kept yet)
        "4",                      # final cut: coming later
        "6",                      # back to main menu
        "q",
    ])
    assert "Footage folder (required)" in text and "This one is required" in text
    assert "Can't find /nope/missing" in text
    assert "Found 5 videos (4 GoPro, 1 phone/other)" in text
    assert "Same thing as a command: hlf run --trip Moab --profile pickleball" in text
    assert "✓ Finding videos" in text and "✓ Reading clips" in text and "✓ Scoring" in text
    assert "look like junk" in text and "highlight picks to review" in text
    assert "haven't kept any picks yet" in text
    assert "coming in a later version" in text
    assert "Bye." in text
    con = sqlite3.connect(home / "index.db")
    assert con.execute("SELECT profile FROM trips WHERE name='Moab'").fetchone()[0] == "pickleball"
    assert con.execute("SELECT COUNT(*) FROM clips WHERE bucket='pending'").fetchone()[0] == 0
    # nothing written next to the originals
    assert sorted(p.name for p in (footage / "day1").iterdir()) == sorted(
        ["GX010042.MP4", "GX020042.MP4", "GX010043.MP4", "GX010044.MP4", "20261001_141500.mp4"])
    # remembers the activity for next time
    assert guide.recall(home)["last_profile"] == "pickleball"


@needs_ffmpeg
def test_review_and_rough_cut(footage, tmp_path):
    home = tmp_path / "home"
    main(["--home", str(home), "run", "--trip", "Moab", "--profile", "mtb", str(footage)])
    served, opened, revealed = [], [], []
    con = sqlite3.connect(home / "index.db")
    con.execute("UPDATE segments SET status='kept' WHERE id=(SELECT MIN(id) FROM segments)")
    con.commit()
    text = session(home, [
        "3",          # review (one trip, picked automatically)
        "4", "", "y",  # rough cut: make it, show in Finder
        "q",
    ], serve=lambda h, port, lan: served.append((port, lan)), open_url=opened.append, reveal=revealed.append)
    assert served == [(guide.PORT, False)]
    assert "You kept 1 picks" in text and "Rough cut:" in text and "File > Import > Timeline" in text
    assert revealed and str(revealed[0]).endswith("Moab_rough_cut.otio")


@needs_ffmpeg
def test_phone_review_uses_lan(footage, tmp_path):
    home = tmp_path / "home"
    main(["--home", str(home), "run", "--trip", "Moab", "--profile", "mtb", str(footage)])
    served = []
    session(home, ["2", "2", "6", "q"], serve=lambda h, port, lan: served.append(lan), open_url=lambda u: None)
    assert served == [True]


@needs_ffmpeg
def test_pause_and_continue(footage, tmp_path, monkeypatch):
    home = tmp_path / "home"
    real = analyze.run_signals
    calls = {"n": 0}

    def stop_on_second(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(*a, **k)

    monkeypatch.setattr(analyze, "run_signals", stop_on_second)
    text = session(home, ["1", str(footage), "Moab", "", "", "", "q"])
    assert "Paused. Everything finished so far is saved." in text
    con = sqlite3.connect(home / "index.db")
    assert con.execute("SELECT COUNT(*) FROM clips WHERE bucket='pending'").fetchone()[0] > 0

    monkeypatch.setattr(analyze, "run_signals", real)
    text = session(home, ["2", "6", "q"])
    assert "1 of 4 clips read" in text            # shown on the menu before continuing
    assert "✓ Reading clips" in text and "4 of 4 read" in text
    assert con.execute("SELECT COUNT(*) FROM clips WHERE bucket='pending'").fetchone()[0] == 0


@needs_ffmpeg
def test_moved_drive_is_found_again(footage, tmp_path):
    copy = tmp_path / "drive" / "Moab"
    shutil.copytree(footage, copy)
    home = tmp_path / "home"
    main(["--home", str(home), "run", "--trip", "Moab", "--profile", "mtb", str(copy)])
    con = sqlite3.connect(home / "index.db")
    con.execute("DELETE FROM signal_runs WHERE clip_id=(SELECT MIN(id) FROM clips)")
    con.commit()
    moved = tmp_path / "renamed-drive" / "Moab"
    moved.parent.mkdir()
    copy.rename(moved)
    text = session(home, ["2", "2", str(moved), "6", "q"])
    assert "can't be found" in text and "Is the drive plugged in?" in text
    paths = [Path(p) for (p,) in con.execute("SELECT path FROM source_files")]
    assert all(p.is_relative_to(moved) for p in paths)
    assert con.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 5
    assert "✓ Reading clips" in text


def test_menu_without_trips(tmp_path):
    text = session(tmp_path / "home", ["2", "5", "6", "q"])
    assert "isn't available yet" in text
    assert "Final cut: coming in a later version" in text
    assert "Setup" in text and "Working folder" in text


def test_installer_parses():
    import subprocess
    root = Path(__file__).resolve().parents[1]
    assert subprocess.run(["bash", "-n", str(root / "install.sh")]).returncode == 0


# ---------- arrow keys ----------

OPTS = [("1", "One", True), ("2", "Two (later)", False), ("3", "Three", True), ("4", "Four", True)]


def test_arrows_move_and_skip_unavailable():
    assert picker(["down", "enter"], OPTS)[0] == "3"          # skips the greyed-out one
    assert picker(["down", "down", "down", "enter"], OPTS)[0] == "1"   # wraps around
    assert picker(["up", "enter"], OPTS)[0] == "4"
    assert picker(["j", "j", "k", "enter"], OPTS)[0] == "3"   # j/k work too
    assert picker(["enter"], OPTS, default=2)[0] == "3"      # starts on the default


def test_arrows_shortcuts_and_exits():
    assert picker(["4"], OPTS)[0] == "4"                      # number jumps straight there
    assert picker(["2", "x", "3"], OPTS)[0] == "3"           # unavailable and unknown keys are ignored
    with pytest.raises(guide.Quit):
        picker(["q"], OPTS)
    with pytest.raises(guide.Back):
        picker(["esc"], OPTS, back=True)
    assert picker(["esc", "enter"], OPTS)[0] == "1"          # no back here: Esc is ignored
    choice, out = picker(["?", "enter"], OPTS, help="EXTRA HELP")
    assert choice == "1" and "EXTRA HELP" in out
    assert "↑↓ move" in out and "❯ One" in out


def test_arrow_yes_no_and_choices():
    kfn, _ = feeder(["down", "enter"])
    ui = guide.UI(lambda p: "", io.StringIO(), color=False, keys=kfn)
    assert ui.yes("Start?") is False
    kfn, _ = feeder(["n"])
    ui.keys = kfn
    assert ui.yes("Start?") is False
    kfn, _ = feeder(["enter"])
    ui.keys = kfn
    assert ui.ask("Activity", why="w", choices=guide.PROFILE_HELP, default="travel") == "travel"
    kfn, _ = feeder(["down", "enter"])
    ui.keys = kfn
    assert ui.ask("Activity", why="w", choices=guide.PROFILE_HELP, default="mtb") == "snowboard"


@needs_ffmpeg
def test_new_trip_with_arrow_keys(footage, tmp_path):
    home = tmp_path / "home"
    text = session(
        home,
        [str(footage), "Moab", ""],          # typed: folder, name, no more folders
        keys=["enter",                        # main menu: Start a new trip (first option)
              "down", "down", "enter",        # activity: mtb -> snowboard -> pickleball
              "enter",                        # Start? Yes
              "down", "down", "down", "enter",  # next steps: final cut (coming later)
              "6",                            # back to main menu
              "q"])
    assert "↑↓ move" in text and "✓ Scoring" in text and "coming in a later version" in text
    con = sqlite3.connect(home / "index.db")
    assert con.execute("SELECT profile FROM trips WHERE name='Moab'").fetchone()[0] == "pickleball"


def test_typed_menus_without_a_terminal(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert guide.tty_keys() is None
    monkeypatch.setenv("HLF_PLAIN", "1")
    assert guide.tty_keys() is None
