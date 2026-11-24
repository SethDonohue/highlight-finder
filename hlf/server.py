"""Local review server. Standard library only.

`hlf serve` binds to this Mac only. `hlf serve --lan` lets your phone on the
same Wi-Fi review too; it then requires the one-time link it prints.
"""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import socket
import threading
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from . import db, export

WEB = Path(__file__).parent / "web"


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class App:
    def __init__(self, home: Path, token: str | None):
        self.home = home
        self.token = token
        self.lock = threading.Lock()

    def con(self):
        return db.connect(self.home)


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "HighlightFinder"

        def log_message(self, *a):  # quiet
            pass

        # ------------------------------------------------------------ helpers
        def _authed(self, q) -> bool:
            if not app.token:
                return True
            if q.get("t", [None])[0] == app.token:
                return True
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return "hlf" in c and c["hlf"].value == app.token

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _file(self, path: Path, ctype=None):
            if not path.exists():
                return self._json({"error": "not found"}, 404)
            size = path.stat().st_size
            ctype = ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m:
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    elif m.group(2):
                        start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(1 << 16, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        # ------------------------------------------------------------ GET
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._authed(q):
                return self._json({"error": "open the link printed by `hlf serve`"}, 403)
            p = u.path
            if p in ("/", "/index.html"):
                body = (WEB / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if app.token:
                    self.send_header("Set-Cookie", f"hlf={app.token}; Path=/; SameSite=Strict; HttpOnly")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            m = re.match(r"^/media/proxy/(\d+)\.mp4$", p)
            if m:
                return self._file(app.home / "proxies" / f"{m.group(1)}.mp4", "video/mp4")
            m = re.match(r"^/media/frame/(\d+)/(\d+)\.jpg$", p)
            if m:
                return self._file(app.home / "frames" / m.group(1) / f"{int(m.group(2)):05d}.jpg", "image/jpeg")
            con = app.con()
            try:
                if p == "/api/trips":
                    rows = con.execute(
                        "SELECT t.name, t.profile, "
                        "(SELECT COUNT(*) FROM clips c WHERE c.trip_id=t.id) clips, "
                        "(SELECT COALESCE(SUM(duration),0) FROM clips c WHERE c.trip_id=t.id) seconds, "
                        "(SELECT COUNT(*) FROM segments s JOIN clips c ON c.id=s.clip_id WHERE c.trip_id=t.id) segments, "
                        "(SELECT COUNT(*) FROM segments s JOIN clips c ON c.id=s.clip_id WHERE c.trip_id=t.id AND s.status='kept') kept, "
                        "(SELECT COUNT(*) FROM clips c WHERE c.trip_id=t.id AND c.bucket='suggested_reject') junk "
                        "FROM trips t ORDER BY t.created_at DESC").fetchall()
                    return self._json([dict(r) for r in rows])
                if p == "/api/segments":
                    trip = db.get_trip(con, q["trip"][0])
                    status = q.get("status", ["all"])[0]
                    sql = ("SELECT s.*, c.name clip_name, c.camera, c.duration clip_duration, c.creation_time "
                           "FROM segments s JOIN clips c ON c.id=s.clip_id WHERE c.trip_id=? "
                           "AND c.bucket NOT IN ('rejected','suggested_reject')")
                    args = [trip["id"]]
                    if status != "all":
                        sql += " AND s.status=?"
                        args.append(status)
                    if q.get("featured", ["0"])[0] == "1":
                        sql += " AND s.featured=1"
                    sql += " ORDER BY s.score DESC"
                    return self._json([dict(r) for r in con.execute(sql, args).fetchall()])
                if p == "/api/triage":
                    trip = db.get_trip(con, q["trip"][0])
                    rows = con.execute("SELECT id, name, duration, camera, bucket, bucket_reason FROM clips "
                                       "WHERE trip_id=? AND bucket IN ('suggested_reject','rejected') "
                                       "ORDER BY bucket DESC, name", (trip["id"],)).fetchall()
                    out = []
                    for r in rows:
                        n = len(list((app.home / "frames" / str(r["id"])).glob("*.jpg")))
                        picks = sorted({1 + int(i * max(n - 1, 0) / 5) for i in range(6)}) if n else []
                        out.append({**dict(r), "thumbs": [f"/media/frame/{r['id']}/{k}.jpg" for k in picks]})
                    return self._json(out)
                m = re.match(r"^/api/clip/(\d+)/lanes$", p)
                if m:
                    f = app.home / "signals" / f"{m.group(1)}.npz"
                    if not f.exists():
                        return self._json({})
                    z = np.load(f)
                    lanes = {}
                    for k in ("tel_accel_dev", "tel_speed", "motion", "audio_peak", "sharpness"):
                        if k in z.files:
                            a = np.nan_to_num(z[k].astype(float))
                            mx = float(np.max(np.abs(a))) or 1.0
                            lanes[k] = [round(float(x) / mx, 3) for x in a]
                    return self._json(lanes)
                return self._json({"error": "not found"}, 404)
            finally:
                con.close()

        # ------------------------------------------------------------ POST
        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._authed(q):
                return self._json({"error": "forbidden"}, 403)
            data = self._body()
            con = app.con()
            try:
                with app.lock:
                    m = re.match(r"^/api/segment/(\d+)$", u.path)
                    if m:
                        sid = int(m.group(1))
                        seg = con.execute("SELECT s.*, c.duration FROM segments s JOIN clips c ON c.id=s.clip_id "
                                          "WHERE s.id=?", (sid,)).fetchone()
                        if not seg:
                            return self._json({"error": "no such segment"}, 404)
                        upd = {}
                        if data.get("status") in ("kept", "rejected", "proposed"):
                            upd["status"] = data["status"]
                        if "start_s" in data:
                            upd["start_s"] = round(max(0.0, float(data["start_s"])), 2)
                        if "end_s" in data:
                            upd["end_s"] = round(min(seg["duration"], float(data["end_s"])), 2)
                        for k in ("rating", "note", "caption"):
                            if k in data:
                                upd[k] = data[k]
                        if "tags" in data:
                            upd["tags"] = json.dumps(data["tags"])
                        if upd:
                            con.execute(f"UPDATE segments SET {', '.join(k + '=?' for k in upd)} WHERE id=?",
                                        (*upd.values(), sid))
                            db.log_decision(con, "segment_update", segment_id=sid, payload={
                                **upd, "before": {k: seg[k] for k in ("status", "start_s", "end_s", "rating")}})
                            con.commit()
                        return self._json(dict(con.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()))
                    m = re.match(r"^/api/clip/(\d+)/bucket$", u.path)
                    if m:
                        cid = int(m.group(1))
                        b = data.get("bucket")
                        if b not in ("rejected", "candidate_confirmed", "suggested_reject"):
                            return self._json({"error": "bad bucket"}, 400)
                        con.execute("UPDATE clips SET bucket=? WHERE id=?", (b, cid))
                        db.log_decision(con, "clip_bucket", clip_id=cid, payload={"bucket": b})
                        con.commit()
                        return self._json({"ok": True})
                    if u.path == "/api/export":
                        trip = db.get_trip(con, data["trip"])
                        out = app.home / "exports" / trip["name"].replace(" ", "_")
                        return self._json(export.rough_cut(con, trip, out))
                return self._json({"error": "not found"}, 404)
            except SystemExit as e:
                return self._json({"error": str(e)}, 400)
            finally:
                con.close()

    return H


def serve(home: Path, port: int = 8765, lan: bool = False) -> None:
    token = secrets.token_urlsafe(9) if lan else None
    app = App(home, token)
    host = "0.0.0.0" if lan else "127.0.0.1"
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"Review UI: http://127.0.0.1:{port}/" + (f"?t={token}" if token else ""))
    if lan:
        print(f"On your phone (same Wi-Fi): http://{_lan_ip()}:{port}/?t={token}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
