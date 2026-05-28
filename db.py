"""SQLite registry for QA'ed videos, base videos, and music tracks.

Schema is created on first connection. Database lives at $CUSTOMGREETING_DATA_DIR
(default `./data`), so prod and staging keep separate libraries.

Phase 0: this module only defines the schema and helpers. Nothing in app.py
reads from it yet.
"""

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Iterable

import fingerprint as fp

DATA_DIR = Path(os.environ.get("CUSTOMGREETING_DATA_DIR", "data")).resolve()
DB_PATH = DATA_DIR / "registry.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS base_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    duration REAL NOT NULL,
    resolution TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    file_path TEXT NOT NULL,
    transcript TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Add transcript column to base_videos if upgrading from earlier schema
-- (SQLite will error harmlessly if column already exists; handled in code)

CREATE TABLE IF NOT EXISTS music_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    duration REAL NOT NULL,
    file_hash TEXT NOT NULL,
    file_path TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS qa_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voice_id TEXT NOT NULL,
    voice_name TEXT NOT NULL,
    base_video_id INTEGER NOT NULL REFERENCES base_videos(id),
    music_id INTEGER REFERENCES music_tracks(id),
    variable TEXT NOT NULL,
    greeting_text TEXT NOT NULL,
    file_path TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(voice_id, base_video_id, music_id, variable)
);

CREATE INDEX IF NOT EXISTS idx_qa_lookup
    ON qa_videos(voice_id, base_video_id, music_id, variable);

CREATE TABLE IF NOT EXISTS pending_qa (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    voice_name TEXT NOT NULL,
    base_video_id INTEGER NOT NULL REFERENCES base_videos(id),
    music_id INTEGER REFERENCES music_tracks(id),
    pending_variables TEXT NOT NULL,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reminder_sent_at TIMESTAMP,
    resolved_at TIMESTAMP
);
"""


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create tables and sub-directories if not present."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "base_videos").mkdir(exist_ok=True)
    (DATA_DIR / "music").mkdir(exist_ok=True)
    (DATA_DIR / "library").mkdir(exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # Idempotent column adds for forward-compat with already-deployed DBs
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(base_videos)").fetchall()}
        if "transcript" not in existing_cols:
            conn.execute("ALTER TABLE base_videos ADD COLUMN transcript TEXT")


# Base videos ----------------------------------------------------------------

def list_base_videos() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, duration, resolution, file_hash, file_path, transcript "
            "FROM base_videos ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def find_matching_base_video(fingerprint: dict, transcript: str | None = None) -> dict | None:
    """Return the existing base_video that matches this fingerprint, or None.

    Match priority:
      1. Exact file_hash match (re-upload of same file)
      2. Transcript similarity >= 0.9 AND resolution match (re-encoded same content)
      3. Duration within tolerance AND resolution match (fallback)
    """
    candidates = list_base_videos()

    for row in candidates:
        if row["file_hash"] == fingerprint["file_hash"]:
            return row

    if transcript:
        best = None
        best_sim = 0.0
        for row in candidates:
            if not row.get("transcript"):
                continue
            if row["resolution"] != fingerprint["resolution"]:
                continue
            sim = fp.transcript_similarity(transcript, row["transcript"])
            if sim > best_sim:
                best_sim = sim
                best = row
        if best and best_sim >= 0.9:
            best["_match_similarity"] = best_sim
            return best

    for row in candidates:
        existing = {"duration": row["duration"], "resolution": row["resolution"]}
        if fp.videos_match(fingerprint, existing):
            return row
    return None


def register_base_video(name: str, source_path: str, fingerprint: dict,
                        transcript: str | None = None) -> dict:
    """Copy the file into the library and create a DB entry. Returns the row."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    base_videos_dir = DATA_DIR / "base_videos"
    base_videos_dir.mkdir(exist_ok=True)

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    dest = base_videos_dir / f"{safe_name}.mp4"
    if dest.exists():
        raise ValueError(f"Base video already exists at {dest}")
    shutil.copy2(source_path, dest)

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO base_videos (name, duration, resolution, file_hash, file_path, transcript) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, fingerprint["duration"], fingerprint["resolution"],
             fingerprint["file_hash"], str(dest), transcript),
        )
        new_id = cur.lastrowid
    return {
        "id": new_id, "name": name,
        "duration": fingerprint["duration"],
        "resolution": fingerprint["resolution"],
        "file_path": str(dest),
    }


# Music tracks ---------------------------------------------------------------

def list_music_tracks() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, duration, file_path FROM music_tracks ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def find_matching_music(fingerprint: dict) -> dict | None:
    for row in list_music_tracks():
        existing = {"duration": row["duration"], "file_hash": ""}
        if fp.music_matches(fingerprint, existing):
            return row
    return None


def register_music(name: str, source_path: str, fingerprint: dict) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    music_dir = DATA_DIR / "music"
    music_dir.mkdir(exist_ok=True)

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    ext = Path(source_path).suffix or ".wav"
    dest = music_dir / f"{safe_name}{ext}"
    if dest.exists():
        raise ValueError(f"Music already exists at {dest}")
    shutil.copy2(source_path, dest)

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO music_tracks (name, duration, file_hash, file_path) "
            "VALUES (?, ?, ?, ?)",
            (name, fingerprint["duration"], fingerprint["file_hash"], str(dest)),
        )
        new_id = cur.lastrowid
    return {
        "id": new_id, "name": name,
        "duration": fingerprint["duration"],
        "file_path": str(dest),
    }


# QA registry lookups --------------------------------------------------------

def lookup_qaed(voice_id: str, base_video_id: int, music_id: int | None,
                variables: Iterable[str]) -> dict[str, list]:
    """Split a list of variables into already-QA'ed (with file path) and needs-generation."""
    variables = list(variables)
    if not variables:
        return {"already_qaed": [], "needs_generation": []}

    placeholders = ",".join("?" * len(variables))
    sql = (
        "SELECT variable, file_path FROM qa_videos "
        f"WHERE voice_id = ? AND base_video_id = ? AND variable IN ({placeholders}) "
    )
    params: list = [voice_id, base_video_id, *variables]
    if music_id is None:
        sql += "AND music_id IS NULL"
    else:
        sql += "AND music_id = ?"
        params.append(music_id)

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    found = {r["variable"]: r["file_path"] for r in rows}
    already_qaed = [{"variable": v, "file_path": found[v]} for v in variables if v in found]
    needs_generation = [v for v in variables if v not in found]
    return {"already_qaed": already_qaed, "needs_generation": needs_generation}


def register_qaed_video(*, voice_id: str, voice_name: str, base_video_id: int,
                        music_id: int | None, variable: str, greeting_text: str,
                        source_path: str, confirmed: bool = True) -> dict:
    """Copy a QA'ed video into the library and insert/update the registry row."""
    library_dir = DATA_DIR / "library" / _slug(voice_name) / f"bv{base_video_id}"
    library_dir.mkdir(parents=True, exist_ok=True)
    dest = library_dir / f"{variable}.mp4"
    shutil.copy2(source_path, dest)

    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO qa_videos "
            "(voice_id, voice_name, base_video_id, music_id, variable, greeting_text, file_path, confirmed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (voice_id, voice_name, base_video_id, music_id, variable,
             greeting_text, str(dest), 1 if confirmed else 0),
        )
    return {"variable": variable, "file_path": str(dest)}


# Pending QA tracking --------------------------------------------------------

def record_pending_qa(*, user_email: str, voice_id: str, voice_name: str,
                      base_video_id: int, music_id: int | None,
                      pending_variables: list[str]) -> int:
    """Log a generation batch that produced names needing QA. Returns row id."""
    if not pending_variables:
        return -1
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO pending_qa "
            "(user_email, voice_id, voice_name, base_video_id, music_id, pending_variables) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_email, voice_id, voice_name, base_video_id, music_id,
             json.dumps(pending_variables)),
        )
        return cur.lastrowid


def resolve_pending_qa(*, voice_id: str, base_video_id: int,
                       music_id: int | None, resolved_variables: list[str]) -> int:
    """Mark pending_qa rows resolved if all their variables are now QA'ed.
    Returns number of rows resolved. (Conservative: only marks rows where every
    pending variable shows up in resolved_variables.)"""
    resolved_set = set(resolved_variables)
    with _connect() as conn:
        sql = "SELECT id, pending_variables FROM pending_qa WHERE resolved_at IS NULL AND voice_id = ? AND base_video_id = ?"
        params: list = [voice_id, base_video_id]
        if music_id is None:
            sql += " AND music_id IS NULL"
        else:
            sql += " AND music_id = ?"
            params.append(music_id)
        rows = conn.execute(sql, params).fetchall()

        resolved_ids = []
        for r in rows:
            pending = set(json.loads(r["pending_variables"]))
            if pending.issubset(resolved_set):
                resolved_ids.append(r["id"])

        if resolved_ids:
            placeholders = ",".join("?" * len(resolved_ids))
            conn.execute(
                f"UPDATE pending_qa SET resolved_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
                resolved_ids,
            )
        return len(resolved_ids)


def _slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "_" for c in s).strip("_")
