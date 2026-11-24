"""Output 1: the DaVinci Resolve rough cut (.otio), plus optional lossless clips.

The timeline points at your untouched originals, so the full source media
is available around every cut (handles come for free in Resolve).
"""
from __future__ import annotations

import csv
import json
import subprocess
import urllib.parse
from pathlib import Path

from . import db


def kept_segments(con, trip_id: int):
    return con.execute(
        "SELECT s.*, c.name AS clip_name, c.creation_time, c.camera FROM segments s "
        "JOIN clips c ON c.id = s.clip_id WHERE c.trip_id=? AND s.status='kept' "
        "ORDER BY c.creation_time, c.name, s.start_s", (trip_id,)).fetchall()


def _pieces(con, seg):
    """Map a segment on the logical clip's timeline onto its chapter files."""
    out = []
    for f in db.clip_files(con, seg["clip_id"]):
        f0, f1 = f["offset_s"], f["offset_s"] + f["duration"]
        a, b = max(seg["start_s"], f0), min(seg["end_s"], f1)
        if b - a > 0.05:
            out.append((f, a - f0, b - f0))
    return out


def _url(path: str) -> str:
    return "file://" + urllib.parse.quote(str(Path(path).resolve()))


def rough_cut(con, trip, out_dir: Path) -> dict:
    try:
        import opentimelineio as otio
    except ImportError as e:
        raise SystemExit("opentimelineio is missing: pip install opentimelineio") from e
    segs = kept_segments(con, trip["id"])
    if not segs:
        raise SystemExit("No kept segments yet. Keep some in the review UI first.")
    out_dir.mkdir(parents=True, exist_ok=True)
    tl = otio.schema.Timeline(name=f"{trip['name']} rough cut")
    v1 = otio.schema.Track(name="V1 picks", kind=otio.schema.TrackKind.Video)
    a1 = otio.schema.Track(name="A1 camera audio", kind=otio.schema.TrackKind.Audio)
    tl.tracks.extend([v1, a1])
    manifest = []
    for seg in segs:
        tags = json.loads(seg["tags"] or "[]")
        for f, a, b in _pieces(con, seg):
            rate = f["fps"] or 30.0
            ref = otio.schema.ExternalReference(
                target_url=_url(f["path"]),
                available_range=otio.opentime.TimeRange(
                    otio.opentime.RationalTime(0, rate),
                    otio.opentime.RationalTime(round(f["duration"] * rate), rate)))
            rng = otio.opentime.TimeRange(otio.opentime.RationalTime(round(a * rate), rate),
                                          otio.opentime.RationalTime(max(1, round((b - a) * rate)), rate))
            label = seg["caption"] or seg["why"] or seg["clip_name"]
            for track in (v1, a1):
                if track is a1 and not f["has_audio"]:
                    track.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(
                        duration=rng.duration)))
                    continue
                clip = otio.schema.Clip(name=Path(f["path"]).name, media_reference=ref, source_range=rng)
                if track is v1:
                    comment = " | ".join(x for x in [
                        seg["why"], f"rating {seg['rating']}" if seg["rating"] else "",
                        ", ".join(tags), seg["note"] or "", seg["claude_note"] or ""] if x)
                    clip.markers.append(otio.schema.Marker(
                        name=label[:80], marked_range=otio.opentime.TimeRange(
                            rng.start_time, otio.opentime.RationalTime(1, rate)),
                        color=otio.schema.MarkerColor.GREEN if (seg["rating"] or 0) >= 4
                        else otio.schema.MarkerColor.BLUE,
                        metadata={"comment": comment}))
                track.append(clip)
            manifest.append({"segment": seg["id"], "file": f["path"], "in_s": round(a, 3),
                             "out_s": round(b, 3), "score": seg["score"], "why": seg["why"],
                             "caption": seg["caption"], "rating": seg["rating"], "tags": tags,
                             "note": seg["note"]})
    stem = trip["name"].replace(" ", "_")
    otio_path = out_dir / f"{stem}_rough_cut.otio"
    otio.adapters.write_to_file(tl, str(otio_path))
    (out_dir / f"{stem}_manifest.json").write_text(json.dumps(manifest, indent=2))
    with (out_dir / f"{stem}_manifest.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(manifest[0]))
        w.writeheader()
        for m in manifest:
            w.writerow({**m, "tags": ";".join(m["tags"])})
    # telemetry sidecars, cut to each segment
    tel_dir = out_dir / "telemetry"
    for seg in segs:
        csv_in = _home_of(con) / "telemetry" / f"{seg['clip_id']}.csv"
        if csv_in.exists():
            tel_dir.mkdir(exist_ok=True)
            rows = list(csv.reader(csv_in.open()))
            keep = [r for r in rows[1:] if seg["start_s"] - 1 <= float(r[0]) <= seg["end_s"] + 1]
            with (tel_dir / f"segment_{seg['id']}.csv").open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(rows[0])
                w.writerows(keep)
    return {"timeline": str(otio_path), "segments": len(segs), "pieces": len(manifest)}


def _home_of(con) -> Path:
    row = con.execute("PRAGMA database_list").fetchone()
    return Path(row["file"]).parent


def lossless_clips(con, trip, out_dir: Path, log=print) -> int:
    """Stream-copy each kept segment (cuts land on keyframes; no quality loss)."""
    segs = kept_segments(con, trip["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for seg in segs:
        for k, (f, a, b) in enumerate(_pieces(con, seg)):
            out = out_dir / f"{seg['id']:04d}_{k}_{Path(f['path']).stem}.mp4"
            r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{a:.3f}", "-i", f["path"],
                                "-t", f"{b - a:.3f}", "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                                str(out)], capture_output=True)
            if r.returncode == 0:
                n += 1
            else:
                log(f"  failed {out.name}: {r.stderr.decode(errors='replace')[-200:]}")
    return n
