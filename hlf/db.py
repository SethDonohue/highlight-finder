"""SQLite index: trips, source files, logical clips, segments and decisions."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  profile TEXT NOT NULL DEFAULT 'mtb',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS source_files (
  id INTEGER PRIMARY KEY,
  trip_id INTEGER NOT NULL REFERENCES trips(id),
  path TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  size INTEGER, camera TEXT, model TEXT, codec TEXT,
  width INTEGER, height INTEGER, fps REAL, fps_num INTEGER, fps_den INTEGER,
  duration REAL, creation_time TEXT, has_audio INTEGER, has_gpmf INTEGER,
  gpmf_stream INTEGER, lrv_path TEXT,
  UNIQUE(trip_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY,
  trip_id INTEGER NOT NULL REFERENCES trips(id),
  name TEXT NOT NULL,
  camera TEXT, duration REAL, creation_time TEXT,
  has_gpmf INTEGER, has_audio INTEGER,
  bucket TEXT DEFAULT 'pending',      -- pending|candidate|suggested_reject|rejected
  bucket_reason TEXT,
  dead_spans TEXT DEFAULT '[]',       -- JSON [[start,end],...]
  hilights TEXT DEFAULT '[]'          -- JSON [seconds,...]
);
CREATE TABLE IF NOT EXISTS clip_files (
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  source_file_id INTEGER NOT NULL REFERENCES source_files(id),
  idx INTEGER NOT NULL,
  offset_s REAL NOT NULL,
  PRIMARY KEY (clip_id, idx)
);
CREATE TABLE IF NOT EXISTS signal_runs (
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT,
  updated_at REAL,
  PRIMARY KEY (clip_id, stage)
);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  start_s REAL NOT NULL, end_s REAL NOT NULL,
  score REAL, why TEXT,
  featured INTEGER DEFAULT 0,
  status TEXT DEFAULT 'proposed',     -- proposed|kept|rejected
  rating INTEGER, caption TEXT, note TEXT, claude_note TEXT,
  tags TEXT DEFAULT '[]',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  segment_id INTEGER, clip_id INTEGER,
  action TEXT NOT NULL,
  payload TEXT,
  ts REAL
);
"""


def connect(home: Path) -> sqlite3.Connection:
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "index.db", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def get_trip(con, name: str, create_profile: str | None = None):
    row = con.execute("SELECT * FROM trips WHERE name=?", (name,)).fetchone()
    if row is None and create_profile:
        con.execute("INSERT INTO trips(name, profile, created_at) VALUES (?,?,?)",
                    (name, create_profile, time.time()))
        con.commit()
        row = con.execute("SELECT * FROM trips WHERE name=?", (name,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown trip '{name}'. Run `hlf ingest --trip {name} ...` first.")
    return row


def set_stage(con, clip_id: int, stage: str, status: str, detail: str | None = None):
    con.execute(
        "INSERT INTO signal_runs(clip_id, stage, status, detail, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(clip_id, stage) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
        "updated_at=excluded.updated_at",
        (clip_id, stage, status, detail, time.time()))
    con.commit()


def stage_status(con, clip_id: int, stage: str) -> str | None:
    row = con.execute("SELECT status FROM signal_runs WHERE clip_id=? AND stage=?",
                      (clip_id, stage)).fetchone()
    return row["status"] if row else None


def log_decision(con, action: str, segment_id=None, clip_id=None, payload=None):
    con.execute("INSERT INTO decisions(segment_id, clip_id, action, payload, ts) VALUES (?,?,?,?,?)",
                (segment_id, clip_id, action, json.dumps(payload or {}), time.time()))


def clip_files(con, clip_id: int):
    return con.execute(
        "SELECT sf.*, cf.idx, cf.offset_s FROM clip_files cf "
        "JOIN source_files sf ON sf.id = cf.source_file_id WHERE cf.clip_id=? ORDER BY cf.idx",
        (clip_id,)).fetchall()
