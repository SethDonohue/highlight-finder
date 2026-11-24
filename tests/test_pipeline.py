"""End-to-end check on made-up footage: ingest -> analyze -> score -> rough cut.

Needs ffmpeg; skipped without it. No real footage ever goes in this repo.
"""
import shutil
import sqlite3

import pytest

from hlf.cli import main

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_full_pipeline(footage, tmp_path):
    home = tmp_path / "home"
    main(["--home", str(home), "run", "--trip", "Test", "--profile", "mtb", str(footage)])
    con = sqlite3.connect(home / "index.db")
    clips = dict(con.execute("SELECT name, bucket FROM clips"))
    assert clips["GX010042+1ch"] == "candidate"          # chapters stitched
    assert clips["GX010043"] == "suggested_reject"       # black
    assert clips["GX010044"] == "suggested_reject"       # 2 s
    segs = con.execute("SELECT c.name, s.start_s, s.end_s FROM segments s JOIN clips c ON c.id=s.clip_id").fetchall()
    assert any(n == "GX010042+1ch" and a <= 25 and b >= 28 for n, a, b in segs), segs
    assert any(n == "20261001_141500" and a <= 27 and b >= 29 for n, a, b in segs), segs
    con.execute("UPDATE segments SET status='kept'")
    con.commit()
    out = tmp_path / "export"
    main(["--home", str(home), "export", "roughcut", "--trip", "Test", "--out", str(out)])
    import opentimelineio as otio
    tl = otio.adapters.read_from_file(str(out / "Test_rough_cut.otio"))
    names = [c.name for c in tl.tracks[0]]
    assert "GX020042.MP4" in names or "GX010042.MP4" in names
    for c in tl.tracks[0]:
        assert c.media_reference.target_url.startswith("file://")
    # originals untouched: nothing written next to them
    assert sorted(p.name for p in (footage / "day1").iterdir()) == sorted(
        ["GX010042.MP4", "GX020042.MP4", "GX010043.MP4", "GX010044.MP4", "20261001_141500.mp4"])
