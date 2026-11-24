"""End-to-end check on made-up footage: ingest -> analyze -> score -> rough cut.

Needs ffmpeg; skipped without it. No real footage ever goes in this repo.
"""
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hlf.cli import main

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def footage(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("footage") / "day1"
    d.mkdir()
    x264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    # one GoPro recording split into two chapter files, with a loud moment at 25-28 s
    ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=40", "-f", "lavfi",
       "-i", "sine=frequency=300:duration=40", "-filter_complex",
       "[1:a]volume='if(between(t,25,28),8,0.05)':eval=frame[a]", "-map", "0:v", "-map", "[a]",
       *x264, "-c:a", "aac", str(d / "GX010042.MP4"))
    ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=15", "-f", "lavfi",
       "-i", "anoisesrc=d=15:a=0.02", *x264, "-c:a", "aac", str(d / "GX020042.MP4"))
    # lens covered
    ff("-f", "lavfi", "-i", "color=c=black:size=640x360:rate=30:duration=10", *x264, str(d / "GX010043.MP4"))
    # accidental tap
    ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2", *x264, str(d / "GX010044.MP4"))
    # phone clip: static, then action with a yell
    ff("-f", "lavfi", "-i", "color=c=gray:size=360x640:rate=30:duration=20", "-f", "lavfi",
       "-i", "testsrc2=size=360x640:rate=30:duration=15", "-f", "lavfi", "-i", "anoisesrc=d=35:a=0.01",
       "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v];[2:a]volume='if(between(t,27,29),30,1)':eval=frame[a]",
       "-map", "[v]", "-map", "[a]", *x264, "-c:a", "aac", str(d / "20261001_141500.mp4"))
    return d.parent


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
