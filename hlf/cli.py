"""hlf: command line for Highlight Finder."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, analyze, config, db, export, media, score


def _home(a) -> Path:
    return Path(a.home).expanduser() if a.home else config.DEFAULT_HOME


def cmd_ingest(a):
    home = _home(a)
    con = db.connect(home)
    trip = db.get_trip(con, a.trip, create_profile=a.profile)
    if a.profile and trip["profile"] != a.profile:
        con.execute("UPDATE trips SET profile=? WHERE id=?", (a.profile, trip["id"]))
        con.commit()
    r = media.ingest(con, trip["id"], [Path(p) for p in a.paths])
    print(f"Ingested: {r['new_files']} new files -> {r['new_clips']} new clips ({r['files_found']} found).")


def cmd_analyze(a):
    home = _home(a)
    con = db.connect(home)
    trip = db.get_trip(con, a.trip)
    r = analyze.analyze_trip(con, home, trip, config.load(home))
    print(f"Analyzed {r['analyzed']}/{r['clips']} clips" + (f", {r['failed']} failed (re-run to retry)" if r["failed"] else "") + ".")


def cmd_score(a):
    home = _home(a)
    con = db.connect(home)
    trip = db.get_trip(con, a.trip)
    r = score.score_trip(con, home, trip, config.load(home))
    print(f"Scored {r['scored']} clips: {r['segments']} proposed segments, "
          f"{r.get('suggested_reject', 0)} clips suggested as junk.")


def cmd_run(a):
    if a.paths:
        cmd_ingest(a)
    cmd_analyze(a)
    cmd_score(a)
    print("Next: `hlf serve` (or `hlf serve --lan` to review from your phone).")


def cmd_status(a):
    con = db.connect(_home(a))
    trip = db.get_trip(con, a.trip)
    tid = trip["id"]
    q = lambda sql: con.execute(sql, (tid,)).fetchone()[0]
    print(json.dumps({
        "trip": trip["name"], "profile": trip["profile"],
        "clips": q("SELECT COUNT(*) FROM clips WHERE trip_id=?"),
        "hours": round(q("SELECT COALESCE(SUM(duration),0) FROM clips WHERE trip_id=?") / 3600, 2),
        "gopro_clips": q("SELECT COUNT(*) FROM clips WHERE trip_id=? AND camera='gopro'"),
        "with_telemetry": q("SELECT COUNT(*) FROM clips WHERE trip_id=? AND has_gpmf=1"),
        "analyzed": q("SELECT COUNT(*) FROM signal_runs r JOIN clips c ON c.id=r.clip_id "
                      "WHERE c.trip_id=? AND r.stage='signals' AND r.status='done'"),
        "failed": q("SELECT COUNT(*) FROM signal_runs r JOIN clips c ON c.id=r.clip_id "
                    "WHERE c.trip_id=? AND r.status='failed'"),
        "suggested_junk": q("SELECT COUNT(*) FROM clips WHERE trip_id=? AND bucket='suggested_reject'"),
        "rejected_clips": q("SELECT COUNT(*) FROM clips WHERE trip_id=? AND bucket='rejected'"),
        "proposals": q("SELECT COUNT(*) FROM segments s JOIN clips c ON c.id=s.clip_id WHERE c.trip_id=? AND s.status='proposed'"),
        "kept": q("SELECT COUNT(*) FROM segments s JOIN clips c ON c.id=s.clip_id WHERE c.trip_id=? AND s.status='kept'"),
        "kept_seconds": round(q("SELECT COALESCE(SUM(end_s-start_s),0) FROM segments s JOIN clips c ON c.id=s.clip_id "
                                "WHERE c.trip_id=? AND s.status='kept'"), 1),
    }, indent=2))


def cmd_list(a):
    con = db.connect(_home(a))
    trip = db.get_trip(con, a.trip)
    sql = ("SELECT s.id, c.name clip, c.camera, s.start_s, s.end_s, s.score, s.why, s.status, s.rating, "
           "s.caption, s.claude_note, s.featured FROM segments s JOIN clips c ON c.id=s.clip_id "
           "WHERE c.trip_id=? AND c.bucket NOT IN ('rejected','suggested_reject')")
    args = [trip["id"]]
    if a.status != "all":
        sql += " AND s.status=?"
        args.append(a.status)
    sql += " ORDER BY s.score DESC LIMIT ?"
    args.append(a.limit)
    rows = [dict(r) for r in con.execute(sql, args)]
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"#{r['id']:<5} {r['score']:.2f}  {r['clip']:<22} {r['start_s']:7.1f}-{r['end_s']:<7.1f} "
                  f"{r['status']:<9} {r['why']}")


def cmd_frames(a):
    """Pull a few frames of one segment from its proxy (for an editorial look)."""
    home = _home(a)
    con = db.connect(home)
    seg = con.execute("SELECT * FROM segments WHERE id=?", (a.segment,)).fetchone()
    if not seg:
        raise SystemExit("no such segment")
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    proxy = home / "proxies" / f"{seg['clip_id']}.mp4"
    dur = seg["end_s"] - seg["start_s"]
    paths = []
    for k in range(a.n):
        t = seg["start_s"] + dur * (k + 0.5) / a.n
        p = out / f"seg{seg['id']}_{k + 1}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{t:.2f}", "-i", str(proxy),
                        "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(p)], check=False)
        if p.exists():
            paths.append(str(p))
    print("\n".join(paths))


def cmd_note(a):
    """Write an editorial note on a segment. Never changes keep/reject."""
    con = db.connect(_home(a))
    fields = {}
    if a.caption is not None:
        fields["caption"] = a.caption
    if a.claude_note is not None:
        fields["claude_note"] = a.claude_note
    if not fields:
        raise SystemExit("nothing to write")
    con.execute(f"UPDATE segments SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                (*fields.values(), a.segment))
    db.log_decision(con, "editorial_note", segment_id=a.segment, payload=fields)
    con.commit()
    print("ok")


def cmd_settings(a):
    print(json.dumps(config.load(_home(a)), indent=2))


def cmd_set(a):
    home = _home(a)
    home.mkdir(parents=True, exist_ok=True)
    old, new = config.set_key(home, a.key, a.value, a.reason)
    print(f"{a.key}: {json.dumps(old)} -> {json.dumps(new)} (logged in tuning-log.md). Run `hlf score` to apply.")


def cmd_reset_settings(a):
    config.reset(_home(a), a.reason)
    print("Settings back to defaults (old overrides saved as a .bak.json).")


def cmd_serve(a):
    from . import server
    server.serve(_home(a), a.port, a.lan)


def cmd_export(a):
    home = _home(a)
    con = db.connect(home)
    trip = db.get_trip(con, a.trip)
    out = Path(a.out).expanduser() if a.out else home / "exports" / trip["name"].replace(" ", "_")
    if a.kind == "roughcut":
        r = export.rough_cut(con, trip, out)
        print(f"Rough cut: {r['timeline']} ({r['segments']} segments).")
        print("In DaVinci Resolve: File > Import > Timeline, then pick the .otio file.")
    else:
        n = export.lossless_clips(con, trip, out / "clips")
        print(f"Wrote {n} lossless clips to {out / 'clips'}")


def cmd_doctor(a):
    ok = True
    for tool in ("ffmpeg", "ffprobe"):
        p = shutil.which(tool)
        print(f"{tool:10} {'ok  ' + p if p else 'MISSING (brew install ffmpeg)'}")
        ok &= bool(p)
    if shutil.which("ffmpeg"):
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        print(f"{'videotoolbox':10} {'ok' if 'h264_videotoolbox' in enc else 'not available (software encode)'}")
    try:
        import opentimelineio  # noqa: F401
        print("otio       ok")
    except ImportError:
        print("otio       MISSING (pip install opentimelineio)")
        ok = False
    print(f"home       {_home(a)}")
    sys.exit(0 if ok else 1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="hlf", description="Highlight Finder")
    p.add_argument("--home", help="working folder (default ~/HighlightFinder or $HLF_HOME)")
    p.add_argument("--version", action="version", version=f"hlf {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def trip_arg(sp, required=True):
        sp.add_argument("--trip", required=required)

    profiles = list(config.DEFAULTS["profiles"])
    s = sub.add_parser("ingest", help="find footage and index it (originals are never modified)")
    trip_arg(s); s.add_argument("--profile", choices=profiles, default="mtb"); s.add_argument("paths", nargs="+")
    s.set_defaults(f=cmd_ingest)
    s = sub.add_parser("analyze", help="telemetry + one decode pass + signals (resumable)")
    trip_arg(s); s.set_defaults(f=cmd_analyze)
    s = sub.add_parser("score", help="triage junk and propose highlight segments")
    trip_arg(s); s.set_defaults(f=cmd_score)
    s = sub.add_parser("run", help="ingest (if paths given), analyze and score")
    trip_arg(s); s.add_argument("--profile", choices=profiles, default="mtb"); s.add_argument("paths", nargs="*")
    s.set_defaults(f=cmd_run)
    s = sub.add_parser("status", help="counts for a trip, as JSON"); trip_arg(s); s.set_defaults(f=cmd_status)
    s = sub.add_parser("list", help="list segments by score")
    trip_arg(s); s.add_argument("--status", default="proposed", choices=["proposed", "kept", "rejected", "all"])
    s.add_argument("--limit", type=int, default=40); s.add_argument("--json", action="store_true")
    s.set_defaults(f=cmd_list)
    s = sub.add_parser("frames", help="extract frames of a segment for a look")
    s.add_argument("--segment", type=int, required=True); s.add_argument("--n", type=int, default=4)
    s.add_argument("--out", default="/tmp/hlf-frames"); s.set_defaults(f=cmd_frames)
    s = sub.add_parser("note", help="add a caption or editorial note to a segment")
    s.add_argument("--segment", type=int, required=True); s.add_argument("--caption")
    s.add_argument("--claude-note", dest="claude_note"); s.set_defaults(f=cmd_note)
    s = sub.add_parser("settings", help="print current settings"); s.set_defaults(f=cmd_settings)
    s = sub.add_parser("set", help="change one setting, with a reason (logged)")
    s.add_argument("key"); s.add_argument("value"); s.add_argument("--reason", required=True)
    s.set_defaults(f=cmd_set)
    s = sub.add_parser("reset-settings", help="restore defaults"); s.add_argument("--reason", required=True)
    s.set_defaults(f=cmd_reset_settings)
    s = sub.add_parser("serve", help="review UI in your browser")
    s.add_argument("--port", type=int, default=8765); s.add_argument("--lan", action="store_true",
                                                                    help="allow your phone on the same Wi-Fi")
    s.set_defaults(f=cmd_serve)
    s = sub.add_parser("export", help="roughcut (.otio for Resolve) or clips (lossless)")
    s.add_argument("kind", choices=["roughcut", "clips"]); trip_arg(s); s.add_argument("--out")
    s.set_defaults(f=cmd_export)
    s = sub.add_parser("doctor", help="check tools are installed"); s.set_defaults(f=cmd_doctor)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
