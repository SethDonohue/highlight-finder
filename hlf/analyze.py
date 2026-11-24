"""Per-clip analysis: telemetry (no decode) and one ffmpeg decode pass.

The decode pass reads each original once and writes everything later stages
need: a 540p review proxy, 1 frame per second for AI/look checks, a tiny
4 fps grayscale stream for motion/exposure, and 16 kHz mono audio.
Each stage is recorded in signal_runs so an interrupted run resumes.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image

from . import db, gpmf
from .media import IS_MAC


def work_dirs(home: Path, clip_id: int) -> dict:
    d = {k: home / k for k in ("proxies", "frames", "signals", "audio", "small", "telemetry")}
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    d["frames_clip"] = d["frames"] / str(clip_id)
    return d


# ---------------------------------------------------------------- telemetry

def run_telemetry(con, home: Path, clip, settings) -> None:
    if db.stage_status(con, clip["id"], "telemetry") == "done":
        return
    files = db.clip_files(con, clip["id"])
    n = max(1, math.ceil(clip["duration"]))
    series = None
    hl: list[float] = []
    if clip["has_gpmf"]:
        packs = []
        for f in files:
            if f["gpmf_stream"] is not None:
                packs.append((f["offset_s"], gpmf.read_packets(Path(f["path"]), f["gpmf_stream"])))
            hl += [f["offset_s"] + t for t in gpmf.hilights(Path(f["path"]))]
        a = settings["airtime"]
        series = gpmf.telemetry_series(packs, n, a["low_g"], a["min_s"])
    else:
        for f in files:
            if f["camera"] == "gopro":
                hl += [f["offset_s"] + t for t in gpmf.hilights(Path(f["path"]))]
    out = work_dirs(home, clip["id"])["telemetry"] / f"{clip['id']}.npz"
    if series is not None:
        np.savez_compressed(out, **series)
        import csv
        with (out.with_suffix(".csv")).open("w", newline="") as fh:
            w = csv.writer(fh)
            keys = list(series)
            w.writerow(["second"] + keys)
            for s in range(n):
                w.writerow([s] + [("" if np.isnan(series[k][s]) else round(float(series[k][s]), 4)) for k in keys])
    con.execute("UPDATE clips SET hilights=? WHERE id=?", (json.dumps(sorted(hl)), clip["id"]))
    db.set_stage(con, clip["id"], "telemetry", "done",
                 "gpmf" if series is not None else "none")


# ---------------------------------------------------------------- decode pass

def _decode_inputs(files, tmpdir: Path) -> list[str]:
    srcs = []
    for f in files:
        lrv = f["lrv_path"]
        srcs.append(lrv if lrv and Path(lrv).exists() else f["path"])
    if len(srcs) == 1:
        return ["-i", srcs[0]]
    lst = tmpdir / "concat.txt"
    lst.write_text("".join("file '" + s.replace("'", "'\\''") + "'\n" for s in srcs))
    return ["-f", "concat", "-safe", "0", "-i", str(lst)]


def _ffmpeg_cmd(inputs, out, has_audio, settings, hw: bool) -> list[str]:
    dec = settings["decode"]
    ph, fw, sf = dec["proxy_height"], dec["frame_width"], dec["small_fps"]
    fc = (f"[0:v]split=3[a][b][c];"
          f"[a]scale=-2:'min({ph},ih)',format=yuv420p[proxy];"
          f"[b]fps=1,scale={fw}:-2[frames];"
          f"[c]fps={sf},scale=64:36,format=gray[small]")
    enc = (["-c:v", "h264_videotoolbox", "-b:v", "3M", "-allow_sw", "1"] if IS_MAC
           else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"])
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error"]
    if hw and IS_MAC:
        cmd += ["-hwaccel", "videotoolbox"]
    cmd += inputs + ["-filter_complex", fc,
                     "-map", "[proxy]"] + (["-map", "0:a:0"] if has_audio else []) + enc + \
        (["-c:a", "aac", "-b:a", "96k"] if has_audio else []) + \
        ["-movflags", "+faststart", str(out["proxy"]),
         "-map", "[frames]", "-q:v", "4", str(out["frames"] / "%05d.jpg"),
         "-map", "[small]", "-f", "rawvideo", "-pix_fmt", "gray", str(out["small"])]
    if has_audio:
        cmd += ["-map", "0:a:0", "-ac", "1", "-ar", "16000", str(out["audio"])]
    return cmd


def run_decode(con, home: Path, clip, settings, log=print) -> None:
    if db.stage_status(con, clip["id"], "decode") == "done":
        return
    d = work_dirs(home, clip["id"])
    cid = clip["id"]
    if d["frames_clip"].exists():
        shutil.rmtree(d["frames_clip"])
    d["frames_clip"].mkdir(parents=True)
    out = {"proxy": d["proxies"] / f"{cid}.mp4", "frames": d["frames_clip"],
           "small": d["small"] / f"{cid}.gray", "audio": d["audio"] / f"{cid}.wav"}
    files = db.clip_files(con, cid)
    with tempfile.TemporaryDirectory() as td:
        inputs = _decode_inputs(files, Path(td))
        for hw in (True, False):
            r = subprocess.run(_ffmpeg_cmd(inputs, out, bool(clip["has_audio"]), settings, hw),
                               capture_output=True)
            if r.returncode == 0:
                break
            if not (hw and IS_MAC):
                break
            log(f"  hardware decode failed for {clip['name']}, retrying in software")
    if r.returncode != 0:
        db.set_stage(con, cid, "decode", "failed", r.stderr.decode(errors="replace")[-500:])
        raise RuntimeError(f"decode failed for {clip['name']}: {r.stderr.decode(errors='replace')[-300:]}")
    db.set_stage(con, cid, "decode", "done")


# ---------------------------------------------------------------- signals

def _laplacian_var(img: np.ndarray) -> float:
    c = img[1:-1, 1:-1]
    lap = img[:-2, 1:-1] + img[2:, 1:-1] + img[1:-1, :-2] + img[1:-1, 2:] - 4 * c
    return float(lap.var())


def run_signals(con, home: Path, clip, settings) -> None:
    if db.stage_status(con, clip["id"], "signals") == "done":
        return
    cid = clip["id"]
    d = work_dirs(home, cid)
    n = max(1, math.ceil(clip["duration"]))
    sf = settings["decode"]["small_fps"]
    sig = {}

    raw = np.fromfile(d["small"] / f"{cid}.gray", dtype=np.uint8)
    frames = raw[: (raw.size // (64 * 36)) * 64 * 36].reshape(-1, 36, 64).astype(np.float32)
    bright = np.full(n, np.nan)
    motion = np.full(n, np.nan)
    if len(frames):
        diffs = np.concatenate([[0.0], np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))])
        means = frames.mean(axis=(1, 2))
        for s in range(n):
            sl = slice(s * sf, (s + 1) * sf)
            if means[sl].size:
                bright[s] = means[sl].mean()
                motion[s] = diffs[sl][1:].mean() if diffs[sl].size > 1 else diffs[sl].mean()
    sig["brightness"], sig["motion"] = bright, motion

    sharp = np.full(n, np.nan)
    jpgs = sorted(d["frames_clip"].glob("*.jpg"))
    for k, j in enumerate(jpgs[:n]):
        with Image.open(j) as im:
            sharp[k] = math.log1p(_laplacian_var(np.asarray(im.convert("L"), dtype=np.float32)))
    sig["sharpness"] = sharp

    loud = np.full(n, np.nan)
    peak = np.full(n, np.nan)
    wav = d["audio"] / f"{cid}.wav"
    if clip["has_audio"] and wav.exists():
        with wave.open(str(wav)) as w:
            sr = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        blk = sr // 10
        nb = len(x) // blk
        if nb:
            rms = np.sqrt((x[: nb * blk].reshape(nb, blk) ** 2).mean(axis=1) + 1e-12)
            db_blk = 20 * np.log10(rms + 1e-9)
            # loudness relative to the surrounding 30 s: reactions stand out, steady wind doesn't
            med = np.array([np.median(db_blk[max(0, i - 150): i + 150]) for i in range(0, nb, 10)])
            for s in range(n):
                b = db_blk[s * 10:(s + 1) * 10]
                if b.size:
                    loud[s] = b.mean()
                    peak[s] = b.max() - med[min(s, len(med) - 1)]
    sig["loudness_db"], sig["audio_peak"] = loud, peak

    tel = d["telemetry"] / f"{cid}.npz"
    if tel.exists():
        t = np.load(tel)
        for k in t.files:
            sig["tel_" + k] = t[k][:n]
    np.savez_compressed(d["signals"] / f"{cid}.npz", **sig)
    db.set_stage(con, cid, "signals", "done")


def analyze_trip(con, home: Path, trip, settings, log=print) -> dict:
    clips = con.execute("SELECT * FROM clips WHERE trip_id=? ORDER BY creation_time, name",
                        (trip["id"],)).fetchall()
    done = failed = 0
    for i, c in enumerate(clips, 1):
        try:
            if all(db.stage_status(con, c["id"], s) == "done" for s in ("telemetry", "decode", "signals")):
                done += 1
                continue
            log(f"[{i}/{len(clips)}] {c['name']} ({c['duration']:.0f}s, {c['camera']})")
            run_telemetry(con, home, c, settings)
            run_decode(con, home, c, settings, log)
            run_signals(con, home, c, settings)
            done += 1
        except Exception as e:  # keep going; the clip can be retried
            failed += 1
            log(f"  failed: {e}")
    return {"clips": len(clips), "analyzed": done, "failed": failed}
