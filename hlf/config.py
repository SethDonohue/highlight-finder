"""Settings: built-in defaults, overridden by <home>/settings.json.

Every change made with `hlf set` is appended to <home>/tuning-log.md so
defaults can always be restored and the reason for each change is kept.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("HLF_HOME", Path.home() / "HighlightFinder"))

# Weights per activity profile (spec: "Scoring and segment selection").
# semantic/saliency signals arrive in later phases; until then their weight
# is redistributed across the signals that exist.
_ACTION = {"telemetry": 0.35, "visual_motion": 0.15, "look": 0.15,
           "semantic": 0.15, "sound": 0.15, "saliency": 0.05}

DEFAULTS: dict = {
    "profiles": {
        "mtb": dict(_ACTION),
        "snowboard": dict(_ACTION),
        "pickleball": {"telemetry": 0.15, "visual_motion": 0.25, "look": 0.10,
                       "semantic": 0.20, "sound": 0.20, "saliency": 0.10},
        "travel": {"telemetry": 0.05, "visual_motion": 0.10, "look": 0.30,
                   "semantic": 0.25, "sound": 0.20, "saliency": 0.10},
    },
    "triage": {
        "min_clip_s": 3.0,        # shorter clips are suggested as accidental
        "bad_fraction": 0.8,      # share of dark/frozen/blurry seconds
        "dark_brightness": 20,    # mean luma 0-255
        "freeze_motion": 0.6,     # mean abs frame difference 0-255
        "blur_pct": 0.05,         # sharpness below this trip percentile
    },
    "dead": {"activity_max": 0.25, "min_run_s": 20},
    "segments": {
        "peak_pct": 85, "grow_pct": 60, "min_s": 4, "max_s": 20,
        "lead_s": 2.0, "tail_s": 1.5, "merge_gap_s": 3.0, "max_merged_s": 30,
        "max_per_clip": 5, "featured_per_day": 40, "smooth_s": 3,
    },
    "airtime": {"low_g": 0.35, "min_s": 0.3},
    "bonus": {"hilight": 0.4, "hilight_window_s": 5},
    "decode": {"proxy_height": 540, "frame_width": 384, "small_fps": 4},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(home: Path) -> dict:
    p = home / "settings.json"
    over = json.loads(p.read_text()) if p.exists() else {}
    return _deep_merge(DEFAULTS, over)


def get_key(settings: dict, dotted: str):
    cur = settings
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def _parse_value(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def set_key(home: Path, dotted: str, raw_value: str, reason: str) -> tuple:
    """Override one setting, log it, and return (old, new)."""
    current = load(home)
    old = get_key(current, dotted)  # raises KeyError for unknown keys
    new = _parse_value(raw_value)
    p = home / "settings.json"
    over = json.loads(p.read_text()) if p.exists() else {}
    cur = over
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = new
    p.write_text(json.dumps(over, indent=2))
    log = home / "tuning-log.md"
    if not log.exists():
        log.write_text("# Tuning log\n\n| When | Setting | Old | New | Reason |\n| --- | --- | --- | --- | --- |\n")
    with log.open("a") as f:
        when = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        f.write(f"| {when} | {dotted} | {json.dumps(old)} | {json.dumps(new)} | {reason} |\n")
    return old, new


def reset(home: Path, reason: str) -> None:
    p = home / "settings.json"
    if p.exists():
        p.rename(home / f"settings.{dt.datetime.now():%Y%m%d-%H%M%S}.bak.json")
    log = home / "tuning-log.md"
    with log.open("a") as f:
        f.write(f"| {dt.datetime.now():%Y-%m-%d %H:%M} | ALL | overrides | defaults | {reason} |\n")
