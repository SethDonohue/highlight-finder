"""Triage and highlight scoring (spec: "Scoring and segment selection").

Pass 1 sorts clips: suggested reject (accidental), dead stretches, candidates.
Pass 2 scores every second, normalized within the trip, then grows peaks
into segments with a "why" line. Re-scoring keeps your decisions: only
untouched proposals are replaced.
"""
from __future__ import annotations

import json
import time
import warnings
from pathlib import Path

import numpy as np

from . import db


def _load_signals(home: Path, clip_id: int) -> dict | None:
    p = home / "signals" / f"{clip_id}.npz"
    if not p.exists():
        return None
    z = np.load(p)
    return {k: z[k].astype(float) for k in z.files}


class Ranker:
    """Percentile rank of a value within the whole trip (0..1)."""

    def __init__(self, arrays):
        parts = [a[np.isfinite(a)] for a in arrays if a is not None and a.size]
        vals = np.concatenate(parts) if parts else np.zeros(0)
        self.sorted = np.sort(vals)

    def __call__(self, a):
        if a is None:
            return None
        out = np.full(a.shape, np.nan)
        ok = np.isfinite(a)
        if self.sorted.size and ok.any():
            out[ok] = np.searchsorted(self.sorted, a[ok], side="right") / self.sorted.size
        return out


def _runs(mask: np.ndarray):
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            yield i, j + 1
            i = j + 1
        else:
            i += 1


def _smooth(a, w):
    if w <= 1:
        return a
    k = np.ones(w) / w
    filled = np.nan_to_num(a, nan=0.0)
    return np.convolve(filled, k, mode="same")


def score_trip(con, home: Path, trip, settings, log=print) -> dict:
    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        return _score_trip(con, home, trip, settings, log)


def _score_trip(con, home: Path, trip, settings, log=print) -> dict:
    clips = con.execute("SELECT * FROM clips WHERE trip_id=?", (trip["id"],)).fetchall()
    sigs = {c["id"]: _load_signals(home, c["id"]) for c in clips}
    ready = [c for c in clips if sigs[c["id"]] is not None]
    if not ready:
        return {"scored": 0, "segments": 0}

    def col(name):
        return [sigs[c["id"]].get(name) for c in ready]

    rk = {name: Ranker(col(name)) for name in
          ("motion", "sharpness", "audio_peak", "tel_accel_dev", "tel_gyro", "tel_speed")}
    tr, dd, sg, bn = settings["triage"], settings["dead"], settings["segments"], settings["bonus"]
    weights = settings["profiles"].get(trip["profile"], settings["profiles"]["mtb"])

    per_clip = {}
    for c in ready:
        s = sigs[c["id"]]
        n = len(s["motion"])
        p = {
            "motion": rk["motion"](s["motion"]),
            "sharp": rk["sharpness"](s["sharpness"]),
            "audio": rk["audio_peak"](s.get("audio_peak")) if c["has_audio"] else np.zeros(n),
        }
        has_tel = "tel_accel_dev" in s and np.isfinite(s["tel_accel_dev"]).any()
        if has_tel:
            act = np.nanmean(np.vstack([rk["tel_accel_dev"](s["tel_accel_dev"]),
                                        rk["tel_gyro"](s["tel_gyro"])]), axis=0)
            air = np.clip(np.nan_to_num(s.get("tel_airtime", np.zeros(n))) / 0.5, 0, 1)
            spd = np.nan_to_num(rk["tel_speed"](s["tel_speed"])) if "tel_speed" in s else np.zeros(n)
            p["telemetry"] = np.clip(0.7 * np.maximum(np.nan_to_num(act), air) + 0.3 * spd, 0, 1)
            p["air"] = air
            p["act"] = np.nan_to_num(act)
        bright = s["brightness"]
        exposure_ok = np.where((bright > 25) & (bright < 235), 1.0, 0.3)
        p["look"] = np.nan_to_num(p["sharp"]) * exposure_ok
        per_clip[c["id"]] = (s, p, has_tel)

    # ---------- pass 1: triage
    con.execute("DELETE FROM segments WHERE status='proposed' AND clip_id IN "
                "(SELECT id FROM clips WHERE trip_id=?)", (trip["id"],))
    all_scores, plans = [], {}
    for c in ready:
        s, p, has_tel = per_clip[c["id"]]
        n = len(s["motion"])
        dark = s["brightness"] < tr["dark_brightness"]
        frozen = s["motion"] < tr["freeze_motion"]
        blurry = np.nan_to_num(p["sharp"], nan=1.0) < tr["blur_pct"]
        bad = dark | frozen | blurry
        reason = None
        if c["duration"] < tr["min_clip_s"]:
            reason = f"{c['duration']:.1f} s long"
        elif bad.mean() >= tr["bad_fraction"]:
            parts = {"dark/lens covered": dark.mean(), "no change on screen": frozen.mean(), "blurry": blurry.mean()}
            k = max(parts, key=parts.get)
            reason = f"{k} {parts[k] * 100:.0f}%"
        if c["bucket"] not in ("rejected", "candidate_confirmed"):
            bucket = "suggested_reject" if reason else "candidate"
            con.execute("UPDATE clips SET bucket=?, bucket_reason=? WHERE id=?", (bucket, reason, c["id"]))
        else:
            bucket = c["bucket"]
        if bucket in ("suggested_reject", "rejected"):
            continue

        activity = np.nanmax(np.vstack([np.nan_to_num(p["motion"]), np.nan_to_num(p["audio"]),
                                        p.get("act", np.zeros(n))]), axis=0)
        dead = activity < dd["activity_max"]
        spans = [[a, b] for a, b in _runs(dead) if b - a >= dd["min_run_s"]]
        con.execute("UPDATE clips SET dead_spans=? WHERE id=?", (json.dumps(spans), c["id"]))

        comp = {"telemetry": p.get("telemetry"), "visual_motion": np.nan_to_num(p["motion"]),
                "look": p["look"], "sound": np.nan_to_num(p["audio"])}
        avail = {k: w for k, w in weights.items() if comp.get(k) is not None and w > 0}
        tot = sum(avail.values()) or 1.0
        contrib = {k: comp[k] * (w / tot) for k, w in avail.items()}
        S = sum(contrib.values())
        hl = json.loads(c["hilights"] or "[]")
        bonus = np.zeros(n)
        for h in hl:
            lo, hi = int(max(0, h - bn["hilight_window_s"])), int(min(n, h + bn["hilight_window_s"] + 1))
            bonus[lo:hi] = bn["hilight"]
        S = _smooth(S, int(sg["smooth_s"])) + bonus
        for a, b in spans:
            S[a:b] = 0.0
        plans[c["id"]] = (S, contrib, bonus, s, p)
        all_scores.append(S[S > 0])

    flat = np.concatenate(all_scores) if all_scores else np.zeros(0)
    if flat.size == 0:
        con.commit()
        return {"scored": len(ready), "segments": 0}
    hi_t = np.percentile(flat, sg["peak_pct"])
    lo_t = np.percentile(flat, sg["grow_pct"])

    # ---------- pass 2: segments
    decided = {}
    for r in con.execute("SELECT clip_id, start_s, end_s FROM segments WHERE status!='proposed' "
                         "AND clip_id IN (SELECT id FROM clips WHERE trip_id=?)", (trip["id"],)):
        decided.setdefault(r["clip_id"], []).append((r["start_s"], r["end_s"]))
    made = []
    clip_by_id = {c["id"]: c for c in ready}
    for cid, (S, contrib, bonus, s, p) in plans.items():
        n = len(S)
        taken = np.zeros(n, bool)
        segs = []
        for peak in np.argsort(-S):
            if S[peak] < hi_t:
                break
            if taken[peak]:
                continue
            a, b = peak, peak + 1
            while a > 0 and S[a - 1] >= lo_t and not taken[a - 1] and b - a < sg["max_s"]:
                a -= 1
            while b < n and S[b] >= lo_t and not taken[b] and b - a < sg["max_s"]:
                b += 1
            while b - a < sg["min_s"] and (a > 0 or b < n):
                if a > 0:
                    a -= 1
                if b < n and b - a < sg["min_s"]:
                    b += 1
            taken[a:b] = True
            segs.append([a, b, int(peak)])
        # pad and merge
        dur = clip_by_id[cid]["duration"]
        padded = sorted([[max(0.0, a - sg["lead_s"]), min(dur, b + sg["tail_s"]), pk] for a, b, pk in segs])
        merged = []
        for seg in padded:
            if merged and seg[0] - merged[-1][1] <= sg["merge_gap_s"] and \
                    seg[1] - merged[-1][0] <= sg["max_merged_s"]:
                prev = merged[-1]
                prev[1] = max(prev[1], seg[1])
                if S[seg[2]] > S[prev[2]]:
                    prev[2] = seg[2]
            else:
                merged.append(list(seg))
        scored = []
        for a, b, pk in merged:
            core = S[int(a):max(int(a) + 1, int(np.ceil(b)))]
            sc = float(0.7 * core.max() + 0.3 * core.mean())
            if any(min(b, e) - max(a, st) > 0.5 * (b - a) for st, e in decided.get(cid, [])):
                continue
            scored.append((sc, a, b, pk))
        scored.sort(reverse=True)
        for sc, a, b, pk in scored[: sg["max_per_clip"]]:
            made.append((cid, a, b, sc, _why(pk, contrib, bonus, s)))

    # featured: top N per day
    day_of = {c["id"]: (c["creation_time"] or "")[:10] for c in ready}
    by_day: dict = {}
    for m in made:
        by_day.setdefault(day_of[m[0]], []).append(m)
    now = time.time()
    count = 0
    for day, items in by_day.items():
        items.sort(key=lambda m: -m[3])
        for rank, (cid, a, b, sc, why) in enumerate(items):
            con.execute("INSERT INTO segments(clip_id, start_s, end_s, score, why, featured, created_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (cid, round(a, 2), round(b, 2), round(sc, 4), why,
                         int(rank < sg["featured_per_day"]), now))
            count += 1
    con.commit()
    return {"scored": len(ready), "segments": count,
            "suggested_reject": sum(1 for c in con.execute(
                "SELECT 1 FROM clips WHERE trip_id=? AND bucket='suggested_reject'", (trip["id"],)))}


def _why(pk, contrib, bonus, s) -> str:
    parts = []
    if bonus[pk] > 0:
        parts.append((9.0, "HiLight tag"))
    air = s.get("tel_airtime")
    if air is not None and np.nan_to_num(air[max(0, pk - 2):pk + 3]).sum() >= 0.3:
        parts.append((8.0, f"airtime {np.nan_to_num(air[max(0, pk - 2):pk + 3]).sum():.1f} s"))
    labels = {"telemetry": "big motion (sensors)", "visual_motion": "lots happening on screen",
              "look": "sharp and well lit", "sound": "loud reaction"}
    for k, v in contrib.items():
        parts.append((float(v[pk]), labels[k]))
    spd = s.get("tel_speed")
    if spd is not None and np.isfinite(spd[pk]) and spd[pk] > 5:
        parts.append((0.2, f"{spd[pk] * 3.6:.0f} km/h"))
    parts.sort(reverse=True)
    seen, out = set(), []
    for _, label in parts:
        if label not in seen:
            out.append(label)
            seen.add(label)
        if len(out) == 3:
            break
    return " · ".join(out)
