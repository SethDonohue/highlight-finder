"""GPMF reader tests on synthetic telemetry (run: python -m pytest tests)."""
import struct
from pathlib import Path

import numpy as np

from hlf import gpmf, media


def klv(key: str, typ: str, size: int, rep: int, payload: bytes) -> bytes:
    hdr = key.encode() + (b"\0" if typ == "\0" else typ.encode()) + bytes([size]) + struct.pack(">H", rep)
    pad = (-len(payload)) % 4
    return hdr + payload + b"\0" * pad


def nest(key: str, children: bytes) -> bytes:
    # nested: size 1, repeat = byte length (fits in tests)
    return klv(key, "\0", 1, len(children), children)


def accl_payload(seconds_free_fall=(0.0, 0.0), rate=200):
    """One second of ACCL at `rate` Hz: 1 g at rest, ~0 g inside the free-fall window."""
    scal = 418
    rows = []
    for k in range(rate):
        t = k / rate
        g = 0.05 if seconds_free_fall[0] <= t < seconds_free_fall[1] else 1.0
        rows.append((int(g * gpmf.G * scal), 0, 0))
    acc = b"".join(struct.pack(">hhh", *r) for r in rows)
    strm_a = nest("STRM", klv("SCAL", "s", 2, 1, struct.pack(">h", scal)) + klv("ACCL", "s", 6, rate, acc))
    gscal = 100
    gyro = b"".join(struct.pack(">hhh", 50, 0, 0) for _ in range(rate))
    strm_g = nest("STRM", klv("SCAL", "s", 2, 1, struct.pack(">h", gscal)) + klv("GYRO", "s", 6, rate, gyro))
    scal5 = struct.pack(">lllll", 10_000_000, 10_000_000, 1000, 1000, 100)
    gps = b"".join(struct.pack(">lllll", 475_000_000, -1_223_000_000, 120_000, 8_000, 800) for _ in range(18))
    strm_p = nest("STRM", klv("SCAL", "l", 4, 5, scal5) + klv("GPS5", "l", 20, 18, gps))
    return nest("DEVC", strm_a + strm_g + strm_p)


def test_parse_payload_scales_units():
    out = gpmf.parse_payload(accl_payload())
    a = out["ACCL"][0]
    assert a.shape == (200, 3)
    assert abs(a[0, 0] - gpmf.G) < 0.05
    assert abs(out["GYRO"][0][0, 0] - 0.5) < 1e-9
    gps = out["GPS5"][0]
    assert abs(gps[0, 0] - 47.5) < 1e-9 and abs(gps[0, 3] - 8.0) < 1e-9


def test_telemetry_series_airtime_and_speed():
    packets = [(float(s), 1.0, accl_payload((0.2, 0.8) if s == 2 else (0, 0))) for s in range(5)]
    ser = gpmf.telemetry_series([(0.0, packets)], 5)
    assert ser["airtime"][2] > 0.5 and ser["airtime"][0] == 0
    assert np.allclose(ser["speed"], 8.0)
    assert ser["accel_dev"][2] > ser["accel_dev"][0]


def test_complex_type_gps9():
    fmt = "lllllllSS"
    scal = struct.pack(">lllllllll", 10_000_000, 10_000_000, 1000, 1000, 100, 1, 1000, 100, 1)
    row = struct.pack(">lllllllHH", 475_000_000, -1_223_000_000, 120_000, 12_000, 1200, 9000, 3600_000, 150, 3)
    strm = nest("STRM", klv("SCAL", "l", 4, 9, scal) + klv("TYPE", "c", 1, len(fmt), fmt.encode())
                + klv("GPS9", "?", len(row), 1, row))
    out = gpmf.parse_payload(nest("DEVC", strm))
    assert abs(out["GPS9"][0][0, 3] - 12.0) < 1e-9


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def test_hilights_hmmt(tmp_path: Path):
    hmmt = _box(b"HMMT", struct.pack(">III", 2, 12_500, 47_000))
    f = tmp_path / "GX010001.MP4"
    f.write_bytes(_box(b"ftyp", b"mp42") + _box(b"moov", _box(b"udta", hmmt)))
    assert gpmf.hilights(f) == [12.5, 47.0]


def test_hilights_gpmf_hlmt(tmp_path: Path):
    inner = nest("HLMT", klv("MANL", "L", 4, 1, struct.pack(">L", 8_000)))
    f = tmp_path / "GX010002.MP4"
    f.write_bytes(_box(b"moov", _box(b"udta", _box(b"GPMF", inner))))
    assert gpmf.hilights(f) == [8.0]


def test_chapter_keys():
    k1, i1 = media.chapter_key(Path("/d/GX010042.MP4"))
    k2, i2 = media.chapter_key(Path("/d/GX020042.MP4"))
    assert k1 == k2 and (i1, i2) == (1, 2)
    assert media.chapter_key(Path("/d/GOPR0007.MP4"))[0] == media.chapter_key(Path("/d/GP010007.MP4"))[0]
    assert media.chapter_key(Path("/d/20261001_141500.mp4")) is None
