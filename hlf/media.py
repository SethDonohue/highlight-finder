"""Finding footage, probing it, and grouping GoPro chapters into one logical clip.

Originals are only ever read. Nothing here writes next to them.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".m4v"}

# HERO6+ : GX010123.MP4 / GH010123.MP4 (chapter 01, file 0123)
_GOPRO_NEW = re.compile(r"^G([HXLS])(\d{2})(\d{4})\.(MP4|LRV)$", re.I)
# HERO5 and older: GOPR0123.MP4 then GP010123.MP4 for later chapters
_GOPRO_OLD_FIRST = re.compile(r"^GOPR(\d{4})\.MP4$", re.I)
_GOPRO_OLD_NEXT = re.compile(r"^GP(\d{2})(\d{4})\.MP4$", re.I)

IS_MAC = sys.platform == "darwin"
# Samsung / Android camera file names: 20261001_141500.mp4
_PHONE_NAME = re.compile(r"^(VID_)?\d{8}_\d{6}", re.I)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, check=False, **kw)


def ffprobe(path: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)])
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {r.stderr.decode(errors='replace')[:300]}")
    return json.loads(r.stdout)


def fingerprint(path: Path) -> str:
    """Size + first and last MiB: fast, and stable if a drive is remounted elsewhere."""
    size = path.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(1 << 20))
        if size > (2 << 20):
            f.seek(-(1 << 20), 2)
            h.update(f.read(1 << 20))
    return h.hexdigest()


def chapter_key(path: Path):
    """Return (group_key, chapter_index) for GoPro chapter files, else None."""
    name = path.name
    m = _GOPRO_NEW.match(name)
    if m and m.group(4).upper() == "MP4":
        return (str(path.parent), "G" + m.group(1).upper(), m.group(3)), int(m.group(2))
    m = _GOPRO_OLD_FIRST.match(name)
    if m:
        return (str(path.parent), "GOPR", m.group(1)), 0
    m = _GOPRO_OLD_NEXT.match(name)
    if m:
        return (str(path.parent), "GOPR", m.group(2)), int(m.group(1))
    return None


def lrv_for(path: Path) -> Path | None:
    """GoPro low-res proxy next to an MP4 (GX010123.MP4 -> GL010123.LRV)."""
    m = _GOPRO_NEW.match(path.name)
    if not m:
        return None
    for cand in (path.with_name(f"GL{m.group(2)}{m.group(3)}.LRV"),
                 path.with_name(f"GL{m.group(2)}{m.group(3)}.lrv")):
        if cand.exists():
            return cand
    return None


def discover(paths: list[Path]) -> list[Path]:
    found = []
    for p in paths:
        p = p.expanduser()
        if p.is_file() and p.suffix.lower() in VIDEO_EXT:
            found.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in VIDEO_EXT and not f.name.startswith("._"):
                    found.append(f)
    return found


def describe(path: Path) -> dict:
    info = ffprobe(path)
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    if v is None:
        raise RuntimeError("no video stream")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    gpmf = next((s for s in streams if s.get("codec_tag_string") == "gpmd"
                 or "gopro met" in (s.get("tags", {}).get("handler_name", "")).lower()), None)
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1"
    try:
        fr = Fraction(rate)
        if fr <= 0:
            fr = Fraction(30, 1)
    except (ZeroDivisionError, ValueError):
        fr = Fraction(30, 1)
    model = tags.get("com.android.model") or tags.get("model") or tags.get("firmware") or ""
    name_is_gopro = chapter_key(path) is not None
    camera = "gopro" if (gpmf is not None or name_is_gopro) else (
        "phone" if ("com.android.version" in tags or "com.samsung" in json.dumps(tags).lower()
                    or tags.get("com.android.manufacturer") or _PHONE_NAME.match(path.name)) else "other")
    return {
        "size": path.stat().st_size,
        "camera": camera,
        "model": model,
        "codec": v.get("codec_name"),
        "width": int(v.get("width", 0)),
        "height": int(v.get("height", 0)),
        "fps": float(fr), "fps_num": fr.numerator, "fps_den": fr.denominator,
        "duration": float(fmt.get("duration") or v.get("duration") or 0.0),
        "creation_time": tags.get("creation_time") or (v.get("tags") or {}).get("creation_time") or "",
        "has_audio": int(a is not None),
        "has_gpmf": int(gpmf is not None),
        "gpmf_stream": int(gpmf["index"]) if gpmf is not None else None,
    }


def ingest(con, trip_id: int, paths: list[Path], log=print) -> dict:
    files = discover(paths)
    log(f"Found {len(files)} video files.")
    new_ids = []
    for i, f in enumerate(files, 1):
        fp = fingerprint(f)
        row = con.execute("SELECT id, path FROM source_files WHERE trip_id=? AND fingerprint=?",
                          (trip_id, fp)).fetchone()
        if row:
            if row["path"] != str(f):  # drive remounted under another name
                con.execute("UPDATE source_files SET path=? WHERE id=?", (str(f), row["id"]))
            continue
        try:
            d = describe(f)
        except Exception as e:  # unreadable or not video
            log(f"  skip {f.name}: {e}")
            continue
        lrv = lrv_for(f)
        cur = con.execute(
            "INSERT INTO source_files(trip_id, path, fingerprint, size, camera, model, codec, width, height,"
            " fps, fps_num, fps_den, duration, creation_time, has_audio, has_gpmf, gpmf_stream, lrv_path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trip_id, str(f), fp, d["size"], d["camera"], d["model"], d["codec"], d["width"], d["height"],
             d["fps"], d["fps_num"], d["fps_den"], d["duration"], d["creation_time"], d["has_audio"],
             d["has_gpmf"], d["gpmf_stream"], str(lrv) if lrv else None))
        new_ids.append(cur.lastrowid)
        if i % 25 == 0:
            log(f"  probed {i}/{len(files)}")
    con.commit()
    clips_made = _build_clips(con, trip_id, set(new_ids))
    return {"files_found": len(files), "new_files": len(new_ids), "new_clips": clips_made}


def _build_clips(con, trip_id: int, new_ids: set) -> int:
    if not new_ids:
        return 0
    rows = con.execute("SELECT * FROM source_files WHERE trip_id=? AND id NOT IN "
                       "(SELECT source_file_id FROM clip_files)", (trip_id,)).fetchall()
    groups: dict = {}
    singles = []
    for r in rows:
        ck = chapter_key(Path(r["path"]))
        if ck:
            groups.setdefault(ck[0], []).append((ck[1], r))
        else:
            singles.append([r])
    units = [[r for _, r in sorted(g, key=lambda x: x[0])] for g in groups.values()] + singles
    made = 0
    for unit in units:
        first = unit[0]
        name = Path(first["path"]).stem if len(unit) == 1 else f"{Path(first['path']).stem}+{len(unit) - 1}ch"
        total = sum(r["duration"] for r in unit)
        cur = con.execute(
            "INSERT INTO clips(trip_id, name, camera, duration, creation_time, has_gpmf, has_audio)"
            " VALUES (?,?,?,?,?,?,?)",
            (trip_id, name, first["camera"], total, first["creation_time"],
             int(all(r["has_gpmf"] for r in unit)), int(all(r["has_audio"] for r in unit))))
        off = 0.0
        for idx, r in enumerate(unit):
            con.execute("INSERT INTO clip_files(clip_id, source_file_id, idx, offset_s) VALUES (?,?,?,?)",
                        (cur.lastrowid, r["id"], idx, off))
            off += r["duration"]
        made += 1
    con.commit()
    return made
