"""GoPro GPMF telemetry: a small pure-Python reader.

GPMF is KLV: 4-byte key, 1-byte type, 1-byte sample size, 2-byte repeat
count (big endian), then size*repeat bytes padded to 4. Type 0 means the
payload is itself KLV (DEVC -> STRM -> sensor data). Reference:
https://github.com/gopro/gpmf-parser

We read only the metadata track, so no video is decoded here.
"""
from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

import numpy as np

SENSOR_KEYS = {"ACCL", "GYRO", "GPS5", "GPS9"}
_NP = {"b": ">i1", "B": ">u1", "s": ">i2", "S": ">u2", "l": ">i4", "L": ">u4",
       "f": ">f4", "d": ">f8", "j": ">i8", "J": ">u8"}
_ST = {"b": "b", "B": "B", "s": "h", "S": "H", "l": "i", "L": "I", "f": "f", "d": "d",
       "j": "q", "J": "Q", "c": "c", "F": "4s", "U": "16s"}
G = 9.80665


def iter_klv(buf: bytes, start: int = 0, end: int | None = None):
    end = len(buf) if end is None else end
    i = start
    while i + 8 <= end:
        key = buf[i:i + 4]
        if key == b"\0\0\0\0":
            break
        typ, size = buf[i + 4], buf[i + 5]
        rep = struct.unpack(">H", buf[i + 6:i + 8])[0]
        length = size * rep
        d0 = i + 8
        if d0 + length > end:
            break
        yield key.decode("latin-1"), (chr(typ) if typ else "\0"), size, rep, d0
        i = d0 + ((length + 3) & ~3)


def decode(buf: bytes, typ: str, size: int, rep: int, d0: int, type_str: str | None = None):
    """Return a (rep, n) float array, or None for types we don't use."""
    if typ == "?":
        if not type_str:
            return None
        try:
            fmt = ">" + "".join(_ST[c] for c in type_str)
        except KeyError:
            return None
        if struct.calcsize(fmt) != size:
            return None
        rows = []
        for k in range(rep):
            vals = struct.unpack_from(fmt, buf, d0 + k * size)
            rows.append([float(v) if isinstance(v, (int, float)) else np.nan for v in vals])
        return np.asarray(rows, dtype=float)
    dt = _NP.get(typ)
    if dt is None:
        return None
    esize = np.dtype(dt).itemsize
    n = size // esize
    if n == 0:
        return None
    arr = np.frombuffer(buf, dtype=dt, count=rep * n, offset=d0).astype(float)
    return arr.reshape(rep, n)


def parse_payload(buf: bytes) -> dict:
    """Parse one GPMF payload (normally one second). Returns {key: [array, ...]} scaled to SI units."""
    out: dict = {}
    for key, typ, size, rep, d0 in iter_klv(buf):
        if key != "DEVC" or typ != "\0":
            continue
        for skey, styp, ssize, srep, sd0 in iter_klv(buf, d0, d0 + size * rep):
            if skey != "STRM" or styp != "\0":
                continue
            scal = None
            type_str = None
            for k, t, sz, rp, dd in iter_klv(buf, sd0, sd0 + ssize * srep):
                if k == "SCAL":
                    a = decode(buf, t, sz, rp, dd)
                    if a is not None:
                        scal = a.reshape(-1)
                elif k == "TYPE":
                    type_str = buf[dd:dd + sz * rp].decode("latin-1").rstrip("\0")
                elif k in SENSOR_KEYS:
                    a = decode(buf, t, sz, rp, dd, type_str)
                    if a is None:
                        continue
                    if scal is not None and scal.size:
                        s = np.where(scal == 0, 1.0, scal)
                        a = a / (s if s.size == a.shape[1] else s[0])
                    out.setdefault(k, []).append(a)
    return out


# ---------------------------------------------------------------- extraction

def read_packets(path: Path, stream_index: int) -> list[tuple[float, float, bytes]]:
    """Return [(start_s, duration_s, payload)] for the GPMF track of one file."""
    pr = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", str(stream_index), "-show_entries",
         "packet=pts_time,duration_time,size", "-of", "json", str(path)],
        capture_output=True, check=False)
    pk = json.loads(pr.stdout or b"{}").get("packets", [])
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", f"0:{stream_index}",
         "-c", "copy", "-f", "rawvideo", "pipe:1"], capture_output=True, check=False).stdout
    out, pos = [], 0
    for p in pk:
        size = int(p.get("size", 0))
        t = float(p.get("pts_time") or 0.0)
        d = float(p.get("duration_time") or 1.0)
        out.append((t, d, raw[pos:pos + size]))
        pos += size
    if pos != len(raw) or not out:  # fallback: one DEVC per second
        out, t = [], 0.0
        for key, typ, size, rep, d0 in iter_klv(raw):
            if key == "DEVC":
                out.append((t, 1.0, raw[d0 - 8:d0 + size * rep]))
                t += 1.0
    return out


def samples(packets, key: str, offset: float = 0.0):
    """Concatenate one sensor across packets -> (times, values)."""
    ts, vs = [], []
    for t, d, payload in packets:
        arrs = parse_payload(payload).get(key)
        if not arrs:
            continue
        a = np.vstack(arrs)
        n = len(a)
        ts.append(offset + t + (np.arange(n) + 0.5) * d / n)
        vs.append(a)
    if not ts:
        return np.zeros(0), np.zeros((0, 0))
    return np.concatenate(ts), np.vstack(vs)


def telemetry_series(packets_by_offset, n_seconds: int, low_g: float = 0.35, min_air_s: float = 0.3) -> dict:
    """Per-second telemetry for a logical clip made of one or more chapter files."""
    acc_t, acc_v, gyr_t, gyr_v, gps_t, gps_v = [], [], [], [], [], []
    for offset, packets in packets_by_offset:
        t, v = samples(packets, "ACCL", offset)
        if len(t):
            acc_t.append(t); acc_v.append(v[:, :3])
        t, v = samples(packets, "GYRO", offset)
        if len(t):
            gyr_t.append(t); gyr_v.append(v[:, :3])
        for key in ("GPS9", "GPS5"):
            t, v = samples(packets, key, offset)
            if len(t):
                speed = v[:, 3].copy()
                bad = (v[:, 0] == 0) & (v[:, 1] == 0)
                if key == "GPS9" and v.shape[1] >= 9:
                    bad |= v[:, 8] < 2  # no 2D/3D fix
                speed[bad] = np.nan
                gps_t.append(t); gps_v.append(np.column_stack([speed, v[:, 2]]))
                break
    res = {k: np.full(n_seconds, np.nan) for k in
           ("accel_dev", "accel_mean", "gyro", "speed", "altitude", "airtime")}
    if acc_t:
        t = np.concatenate(acc_t)
        mag = np.linalg.norm(np.vstack(acc_v), axis=1) / G
        sec = np.clip(t.astype(int), 0, n_seconds - 1)
        for s in range(n_seconds):
            m = mag[sec == s]
            if m.size:
                res["accel_dev"][s] = m.std()
                res["accel_mean"][s] = m.mean()
        res["airtime"] = _airtime(t, mag, n_seconds, low_g, min_air_s)
    if gyr_t:
        t = np.concatenate(gyr_t)
        w = np.linalg.norm(np.vstack(gyr_v), axis=1)
        sec = np.clip(t.astype(int), 0, n_seconds - 1)
        res["gyro"] = _bin_mean(sec, w, n_seconds)
    if gps_t:
        t = np.concatenate(gps_t)
        v = np.vstack(gps_v)
        sec = np.clip(t.astype(int), 0, n_seconds - 1)
        res["speed"] = _bin_mean(sec, v[:, 0], n_seconds)
        res["altitude"] = _bin_mean(sec, v[:, 1], n_seconds)
    return res


def _bin_mean(sec, vals, n):
    out = np.full(n, np.nan)
    ok = np.isfinite(vals)
    if ok.any():
        sums = np.bincount(sec[ok], weights=vals[ok], minlength=n)[:n]
        cnt = np.bincount(sec[ok], minlength=n)[:n]
        with np.errstate(invalid="ignore", divide="ignore"):
            out = np.where(cnt > 0, sums / np.maximum(cnt, 1), np.nan)
    return out


def _airtime(t, mag, n, low_g, min_s):
    """Seconds of free fall per second: runs of |a| < low_g lasting >= min_s."""
    air = np.zeros(n)
    low = mag < low_g
    i = 0
    while i < len(low):
        if not low[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(low) and low[j + 1]:
            j += 1
        dur = t[j] - t[i]
        if dur >= min_s:
            step = dur / max(j - i, 1)
            for k in range(i, j + 1):
                s = int(t[k])
                if 0 <= s < n:
                    air[s] += step
        i = j + 1
    return air


# ---------------------------------------------------------------- HiLights

def _boxes(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        hsz = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hsz = 16
        elif size == 0:
            size = end - pos
        if size < hsz:
            return
        yield typ, pos + hsz, pos + size
        pos += size


def hilights(path: Path) -> list[float]:
    """HiLight tags (seconds) from moov/udta: HMMT (HERO5-7) or GPMF HLMT (HERO8+)."""
    found: list[float] = []
    try:
        with open(path, "rb") as f:
            end = f.seek(0, 2)
            for typ, s, e in _boxes(f, 0, end):
                if typ != b"moov":
                    continue
                for t2, s2, e2 in _boxes(f, s, e):
                    if t2 != b"udta":
                        continue
                    for t3, s3, e3 in _boxes(f, s2, e2):
                        if t3 == b"HMMT":
                            f.seek(s3)
                            data = f.read(e3 - s3)
                            if len(data) >= 4:
                                n = struct.unpack(">I", data[:4])[0]
                                for k in range(min(n, (len(data) - 4) // 4)):
                                    ms = struct.unpack(">I", data[4 + 4 * k:8 + 4 * k])[0]
                                    if ms:
                                        found.append(ms / 1000.0)
                        elif t3 == b"GPMF" and e3 - s3 < (8 << 20):
                            f.seek(s3)
                            found += _hlmt(f.read(e3 - s3))
    except OSError:
        return []
    return sorted(set(round(x, 3) for x in found))


def _hlmt(buf: bytes) -> list[float]:
    out = []

    def walk(start, end, inside):
        for k, t, sz, rp, d0 in iter_klv(buf, start, end):
            if t == "\0":
                walk(d0, d0 + sz * rp, inside or k == "HLMT")
            elif inside and k == "MANL":
                a = decode(buf, t, sz, rp, d0)
                if a is not None:
                    out.extend(float(x) / 1000.0 for x in a.reshape(-1) if x > 0)
    walk(0, len(buf), False)
    return out
