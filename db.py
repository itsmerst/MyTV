"""SQLite database layer for MyTV.

One local file (mytv.db) holds all your shows, movies, episode progress,
ratings and settings. No server, no cloud — everything stays on your machine.
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mytv.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    tvdb_id         INTEGER PRIMARY KEY,
    tmdb_id         INTEGER,
    name            TEXT NOT NULL,
    poster_path     TEXT,
    image_url       TEXT,
    overview        TEXT,
    first_air_date  TEXT,
    vote_average    REAL,
    status          TEXT DEFAULT 'watching',    -- want | watching | watched
    is_favorite     INTEGER DEFAULT 0,
    episodes_seen   INTEGER DEFAULT 0,
    runtime         INTEGER DEFAULT 0,           -- total watched seconds for this show
    last_season     INTEGER,                     -- most recent watched season
    last_episode    INTEGER,                     -- most recent watched episode
    followed_at     TEXT,
    addiction_score INTEGER DEFAULT 0,
    enriched        INTEGER DEFAULT 0,           -- 1 once TMDB metadata fetched
    source          TEXT DEFAULT 'import',       -- import | manual
    added_at        TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS episodes_seen (
    episode_id  INTEGER PRIMARY KEY,
    tvdb_id     INTEGER,
    show_name   TEXT,
    season      INTEGER,
    number      INTEGER,
    runtime     INTEGER DEFAULT 0,
    watched_at  TEXT
);

CREATE TABLE IF NOT EXISTS movies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id       INTEGER,
    name          TEXT NOT NULL,
    poster_path   TEXT,
    image_url     TEXT,
    overview      TEXT,
    release_date  TEXT,
    vote_average  REAL,
    runtime       INTEGER,
    status        TEXT DEFAULT 'watched',        -- want | watched
    rating        INTEGER,                        -- your personal rating (0-10), null if unrated
    rewatch_count INTEGER DEFAULT 0,
    enriched      INTEGER DEFAULT 0,
    source        TEXT DEFAULT 'import',
    added_at      TEXT,
    updated_at    TEXT,
    UNIQUE(name, release_date)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn):
    """Add columns that older mytv.db files may lack. Safe to run every start."""
    for table in ("shows", "movies"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "image_url" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN image_url TEXT")
    # columns added in phase 1 (v2 tracking import)
    add = {
        "shows": [("runtime", "INTEGER DEFAULT 0"), ("last_season", "INTEGER"),
                  ("last_episode", "INTEGER"), ("followed_at", "TEXT"),
                  ("status_fixed", "INTEGER DEFAULT 0"),
                  ("translated", "INTEGER DEFAULT 0")],
        "movies": [("translated", "INTEGER DEFAULT 0")],
        "episodes_seen": [("runtime", "INTEGER DEFAULT 0")],
    }
    for table, defs in add.items():
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in defs:
            if name not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # retired show statuses -> collapse into the current 3-state model
    conn.execute("UPDATE shows SET status='watching' WHERE status IN ('following','stopped')")


def get_setting(key, default=None):
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = connect()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
