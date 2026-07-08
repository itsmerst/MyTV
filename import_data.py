"""Import a TV Time GDPR export (the CSVs in data_raw/) into mytv.db.

Safe to re-run: it upserts, so importing again won't create duplicates.

Usage:
    python import_data.py                # reads ./data_raw
    python import_data.py path/to/folder # reads a custom folder
"""
import csv
import os
import re
import sys
from datetime import datetime

import db

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_raw")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def epoch_to_date(v):
    """TV Time timestamps are microseconds since epoch (16-ish digits)."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    while n > 1e12:            # strip micro/milli down to seconds
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return None


def parse_gomap(s):
    """Parse a Go-style 'map[k:v k2:v2]' string into a dict of strings."""
    out = {}
    if not s:
        return out
    for k, v in re.findall(r"(\w+):(\S+)", s):
        out[k] = v
    return out


def read_csv(folder, name):
    """Yield rows as dicts; return [] if the file is missing."""
    path = os.path.join(folder, name)
    if not os.path.exists(path):
        print(f"  (skip) {name} not found")
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def import_shows(conn, folder):
    rows = read_csv(folder, "user_tv_show_data.csv")
    n = 0
    for r in rows:
        tvdb_id = to_int(r.get("tv_show_id"))
        name = (r.get("tv_show_name") or "").strip()
        if not tvdb_id or not name:
            continue
        is_fav = to_int(r.get("is_favorited"))
        seen = to_int(r.get("nb_episodes_seen"))
        followed = to_int(r.get("is_followed"))
        if followed:
            status = "watching"
        elif seen > 0:
            status = "watched"
        else:
            status = "want"
        conn.execute(
            """INSERT INTO shows (tvdb_id, name, status, is_favorite, episodes_seen,
                                  source, added_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'import', ?, ?)
               ON CONFLICT(tvdb_id) DO UPDATE SET
                   name=excluded.name,
                   translated=0,
                   is_favorite=excluded.is_favorite,
                   episodes_seen=excluded.episodes_seen,
                   updated_at=excluded.updated_at""",
            (tvdb_id, name, status, is_fav, seen, now(), now()),
        )
        n += 1
    print(f"  shows: {n}")


def import_tracking_v2(conn, folder):
    """Rich per-show tracking from tracking-prod-records-v2.csv.

    Gives real statuses (want/watching/watched), per-show watch runtime,
    episode counts, current watch position and follow date — none of which the
    older user_tv_show_data.csv carried. Also logs every watched episode with
    its runtime, and stores lifetime totals for the Stats page.
    """
    rows = read_csv(folder, "tracking-prod-records-v2.csv")
    if not rows:
        return

    # 1) summary row (key == 'tracking-stats') -> lifetime totals for Stats
    def _set(k, v):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v or "0"))
    for r in rows:
        if (r.get("key") or "").strip() == "tracking-stats":
            _set("total_series_runtime", r.get("total_series_runtime"))
            _set("total_movies_runtime", r.get("total_movies_runtime"))
            _set("ep_watch_count", r.get("ep_watch_count"))
            break

    # 2) aggregate watched-episode rows per show: runtime + episode count
    agg = {}   # s_id -> {"runtime": int, "eps": int, "name": str}
    n_eps = 0
    for r in rows:
        eid = to_int(r.get("episode_id") or r.get("ep_id"))
        sid = to_int(r.get("s_id"))
        if not eid or not sid:
            continue
        rt = to_int(r.get("runtime"))
        a = agg.setdefault(sid, {"runtime": 0, "eps": 0, "name": (r.get("series_name") or "").strip()})
        a["runtime"] += rt
        a["eps"] += 1
        season = to_int(r.get("season_number") or r.get("s_no"))
        number = to_int(r.get("episode_number") or r.get("ep_no"))
        conn.execute(
            """INSERT INTO episodes_seen
                   (episode_id, tvdb_id, show_name, season, number, runtime, watched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(episode_id) DO UPDATE SET
                   tvdb_id=excluded.tvdb_id, show_name=excluded.show_name,
                   season=excluded.season, number=excluded.number,
                   runtime=excluded.runtime, watched_at=excluded.watched_at""",
            (eid, sid, a["name"], season, number, rt, r.get("created_at") or now()),
        )
        n_eps += 1

    # 3) per-show follow rows -> status, position, runtime, follow date
    n_shows = 0
    for r in rows:
        if (r.get("is_followed") or "") != "true" and (r.get("is_for_later") or "") not in ("true", "false"):
            continue
        sid = to_int(r.get("s_id"))
        name = (r.get("series_name") or "").strip()
        if not sid or not name:
            continue
        is_for_later = (r.get("is_for_later") or "") == "true"
        is_archived = (r.get("is_archived") or "") == "true"
        is_followed = (r.get("is_followed") or "") == "true"
        if is_for_later:
            status = "want"
        elif is_archived or not is_followed:
            status = "watched"
        else:
            status = "watching"
        mre = parse_gomap(r.get("most_recent_ep_watched"))
        last_s = to_int(mre.get("s_no"), None)
        last_e = to_int(mre.get("ep_no"), None)
        followed = epoch_to_date(r.get("followed_at"))
        a = agg.get(sid, {"runtime": 0, "eps": to_int(r.get("ep_watch_count"))})
        seen = a["eps"] or to_int(r.get("ep_watch_count"))
        conn.execute(
            """INSERT INTO shows (tvdb_id, name, status, episodes_seen, runtime,
                                  last_season, last_episode, followed_at,
                                  source, added_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'import', ?, ?)
               ON CONFLICT(tvdb_id) DO UPDATE SET
                   name=excluded.name, translated=0, status=excluded.status,
                   episodes_seen=MAX(shows.episodes_seen, excluded.episodes_seen),
                   runtime=excluded.runtime, last_season=excluded.last_season,
                   last_episode=excluded.last_episode, followed_at=excluded.followed_at,
                   updated_at=excluded.updated_at""",
            (sid, name, status, seen, a["runtime"], last_s, last_e, followed, now(), now()),
        )
        n_shows += 1
    print(f"  v2 shows: {n_shows}, episodes logged: {n_eps}")


def import_addiction(conn, folder):
    """Engagement score per show — nice for sorting 'most watched'."""
    rows = read_csv(folder, "show_addiction_score.csv")
    n = 0
    for r in rows:
        tvdb_id = to_int(r.get("tv_show_id"))
        score = to_int(r.get("monthly_score"))
        if not tvdb_id:
            continue
        cur = conn.execute(
            "UPDATE shows SET addiction_score=? WHERE tvdb_id=?", (score, tvdb_id)
        )
        n += cur.rowcount
    print(f"  addiction scores applied: {n}")


def import_episodes(conn, folder):
    rows = read_csv(folder, "seen_episode_latest.csv")
    n = 0
    for r in rows:
        eid = to_int(r.get("episode_id"))
        if not eid:
            continue
        conn.execute(
            """INSERT INTO episodes_seen
                   (episode_id, tvdb_id, show_name, season, number, watched_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(episode_id) DO UPDATE SET
                   watched_at=excluded.watched_at""",
            (
                eid,
                None,
                (r.get("tv_show_name") or "").strip(),
                to_int(r.get("episode_season_number")),
                to_int(r.get("episode_number")),
                r.get("created_at") or now(),
            ),
        )
        n += 1
    print(f"  episodes seen: {n}")


def import_movies(conn, folder):
    """Movies live in tracking-prod-records.csv as entity_type == 'movie'.

    Each movie has one row per `type`: 'watch'/'follow'/'rewatch_count' mean
    you've seen it, 'towatch' means it's on your want-to-watch list. A movie
    that only ever appears as 'towatch' (never watched) is status 'want'.
    """
    rows = read_csv(folder, "tracking-prod-records.csv")
    movies = {}
    for r in rows:
        if (r.get("entity_type") or "").strip() != "movie":
            continue
        name = (r.get("movie_name") or "").strip()
        if not name:
            continue
        rel = (r.get("release_date") or "").strip()
        # take just the date portion "2018-02-09 00:00:00" -> "2018-02-09"
        rel = rel.split(" ")[0] if rel else ""
        key = (name, rel)
        rec = movies.setdefault(
            key,
            {"name": name, "release_date": rel, "runtime": 0, "rewatch": 0, "types": set()},
        )
        rec["runtime"] = max(rec["runtime"], to_int(r.get("runtime")))
        rec["rewatch"] = max(rec["rewatch"], to_int(r.get("rewatch_count")))
        rec["types"].add((r.get("type") or "").strip())

    n = 0
    for m in movies.values():
        # Every movie carries a 'follow' row, so 'follow' does NOT mean watched.
        # A movie is 'want' only if it's on the towatch list and never watched.
        status = "want" if ("towatch" in m["types"] and "watch" not in m["types"]) else "watched"
        conn.execute(
            """INSERT INTO movies (name, release_date, runtime, rewatch_count, status,
                                   source, added_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'import', ?, ?)
               ON CONFLICT(name, release_date) DO UPDATE SET
                   translated=0,
                   runtime=excluded.runtime,
                   rewatch_count=excluded.rewatch_count,
                   status=excluded.status,
                   updated_at=excluded.updated_at""",
            (m["name"], m["release_date"], m["runtime"], m["rewatch"], status, now(), now()),
        )
        n += 1
    print(f"  movies: {n}")


def import_ratings(conn, folder):
    """ratings-live-votes.csv: vote_key ends with '-<user>-<N>'; N is the rating."""
    rows = read_csv(folder, "ratings-live-votes.csv")
    n = 0
    for r in rows:
        name = (r.get("movie_name") or "").strip()
        if not name:
            continue
        vk = r.get("vote_key") or ""
        rating = None
        parts = vk.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            rating = int(parts[1])
        cur = conn.execute(
            "UPDATE movies SET rating=? WHERE name=?", (rating, name)
        )
        n += cur.rowcount
    print(f"  ratings applied to movies: {n}")


def run_import(folder):
    """Import every CSV in `folder` into mytv.db. Returns a counts dict.

    Reusable by both the CLI and the web upload route.
    """
    db.init_db()
    conn = db.connect()
    try:
        import_shows(conn, folder)          # baseline names, favorites, want list
        import_tracking_v2(conn, folder)    # rich status, runtime, position, episodes
        import_addiction(conn, folder)
        import_episodes(conn, folder)       # legacy fallback (older exports)
        import_movies(conn, folder)
        import_ratings(conn, folder)
        conn.commit()
    finally:
        conn.close()

    conn = db.connect()
    counts = {
        "shows": conn.execute("SELECT COUNT(*) c FROM shows").fetchone()["c"],
        "movies": conn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"],
        "episodes": conn.execute("SELECT COUNT(*) c FROM episodes_seen").fetchone()["c"],
    }
    conn.close()
    return counts


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else DATA_DIR
    if not os.path.isdir(folder):
        print(f"ERROR: data folder not found: {folder}")
        sys.exit(1)
    print(f"Importing from: {folder}")
    c = run_import(folder)
    print(f"\nDone. Library now has {c['shows']} shows, {c['movies']} movies, "
          f"{c['episodes']} tracked episodes.")


if __name__ == "__main__":
    main()
