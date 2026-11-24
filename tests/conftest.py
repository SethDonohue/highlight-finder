"""Shared made-up footage for the tests. Needs ffmpeg. No real footage ever goes in this repo."""
import subprocess
from pathlib import Path

import pytest


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
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
