"""MyTV — a local, private replacement for TV Time.

Run:
    python app.py
Then open http://127.0.0.1:5000 in your browser.

Your data lives in mytv.db on this machine. Nothing is sent anywhere except
optional look-ups to TMDB (only if you add an API key on the Settings page).
"""
import os
import csv
import tempfile
import zipfile
import shutil
import unicodedata
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, send_file,
)

import db
import tmdb
import providers
import import_data

app = Flask(__name__)  # Local MyTV application

# Initialize on import as well as direct execution. This keeps `flask run`,
# WSGI servers, and the test client working with a fresh database.
db.init_db()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@app.template_global()
def img(row):
    """Best poster URL for a show/movie row: keyless image_url, else TMDB path."""
    try:
        if row["image_url"]:
            return row["image_url"]
    except (KeyError, IndexError):
        pass
    try:
        if row["poster_path"]:
            return tmdb.poster_url(row["poster_path"])
    except (KeyError, IndexError):
        pass
    return None


SHOW_STATUSES = ["want", "watching", "watched"]
MOVIE_STATUSES = ["want", "watched"]


# ---------------------------------------------------------------- pages -----

@app.route("/")
def dashboard():
    from datetime import datetime
    
    conn = db.connect()

    # Headline tiles
    total_shows = conn.execute("SELECT COUNT(*) c FROM shows").fetchone()["c"]
    total_movies = conn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"]
    total_episodes = conn.execute("SELECT COUNT(*) c FROM episodes_seen").fetchone()["c"]
    total_runtime = conn.execute(
        "SELECT COALESCE(SUM(runtime), 0) rt FROM movies WHERE status='watched'"
    ).fetchone()["rt"]

    # Continue watching — started but not finished (horizontal scroll row)
    continue_watching = conn.execute("""
        SELECT * FROM shows
        WHERE status='watching' AND episodes_seen > 0
        ORDER BY updated_at DESC LIMIT 20
    """).fetchall()

    # Want to watch — shows + movies you haven't started (horizontal scroll)
    want_shows = conn.execute("""
        SELECT *, 'tv' AS kind FROM shows WHERE status='want'
        ORDER BY updated_at DESC LIMIT 20
    """).fetchall()
    want_movies = conn.execute("""
        SELECT *, 'movie' AS kind FROM movies WHERE status='want'
        ORDER BY updated_at DESC LIMIT 20
    """).fetchall()
    want = list(want_shows) + list(want_movies)

    # History — everything finished (horizontal scroll)
    history_shows = conn.execute("""
        SELECT *, 'tv' AS kind FROM shows WHERE status='watched'
        ORDER BY updated_at DESC LIMIT 40
    """).fetchall()
    history_movies = conn.execute("""
        SELECT *, 'movie' AS kind FROM movies WHERE status='watched'
        ORDER BY updated_at DESC LIMIT 40
    """).fetchall()
    history = sorted(
        list(history_shows) + list(history_movies),
        key=lambda r: r["updated_at"] or "", reverse=True,
    )[:30]

    # Calendar — movies + episodes this month (like want/history but filtered by date)
    today = datetime.now()
    month_start = f"{today.year:04d}-{today.month:02d}-01"
    month_end = f"{today.year:04d}-{today.month:02d}-31"
    
    calendar_items = []
    
    # Movies this month
    movies = conn.execute(
        f"""SELECT *, 'movie' AS kind FROM movies
           WHERE release_date BETWEEN ? AND ? ORDER BY release_date""",
        (month_start, month_end)).fetchall()
    
    for m in movies:
        m = dict(m)  # Convert sqlite3.Row to dict
        # Format date as "Mon D"
        try:
            d = datetime.strptime(m['release_date'], '%Y-%m-%d')
            m['date_str'] = d.strftime('%b %d')
        except:
            m['date_str'] = m['release_date'][:10]
        calendar_items.append(m)
    
    # Episode dates come from TMDB. Loading them here used to block every
    # dashboard navigation with up to 20 network requests. The browser now
    # requests them after this local-data page has rendered.

    # Recent activity feed (episodes + movies), newest first
    recent_episodes = conn.execute("""
        SELECT 'episode' type, show_name name, season, number, watched_at ts
        FROM episodes_seen ORDER BY watched_at DESC LIMIT 10
    """).fetchall()
    recent_movies = conn.execute("""
        SELECT 'movie' type, name, NULL season, NULL number, updated_at ts
        FROM movies WHERE status='watched' ORDER BY updated_at DESC LIMIT 10
    """).fetchall()
    recent = sorted(
        list(recent_episodes) + list(recent_movies),
        key=lambda x: x["ts"] or "", reverse=True,
    )[:15]

    conn.close()

    return render_template(
        "dashboard.html",
        total_shows=total_shows,
        total_movies=total_movies,
        total_episodes=total_episodes,
        total_runtime=total_runtime // 3600,  # seconds -> hours
        continue_watching=continue_watching,
        want=want,
        calendar=calendar_items,
        history=history,
        recent=recent,
        tmdb=tmdb,
        active="dashboard",
    )



@app.route("/movies")
def movies():
    status = request.args.get("status", "all")
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort", "recent")

    where, params = [], []
    if status != "all":
        where.append("status = ?")
        params.append(status)
    if q:
        where.append("name LIKE ?")
        params.append(f"%{q}%")
    sql = "SELECT * FROM movies"
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = {
        "name": "name COLLATE NOCASE ASC",
        "release": "release_date DESC",
        "rated": "(rating IS NULL), rating DESC",
        "recent": "updated_at DESC",
    }.get(sort, "updated_at DESC")
    sql += f" ORDER BY {order}"

    conn = db.connect()
    items = conn.execute(sql, params).fetchall()
    counts = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM movies GROUP BY status")}
    counts["all"] = conn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"]
    conn.close()

    return render_template(
        "movies.html", movies=items, counts=counts, status=status, q=q, sort=sort,
        statuses=["all"] + MOVIE_STATUSES, tmdb=tmdb, active="movies",
    )


@app.route("/movie/<int:movie_id>")
def movie_detail(movie_id):
    conn = db.connect()
    movie = conn.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
    conn.close()
    if not movie:
        return "Movie not found", 404
    return render_template(
        "movie.html", movie=movie, statuses=MOVIE_STATUSES,
        tmdb=tmdb, active="movies",
    )


@app.route("/show/<int(signed=True):tvdb_id>")
def show_detail(tvdb_id):
    conn = db.connect()
    show = conn.execute("SELECT * FROM shows WHERE tvdb_id = ?", (tvdb_id,)).fetchone()
    if not show:
        conn.close()
        return "Show not found", 404
    episodes = conn.execute(
        """SELECT * FROM episodes_seen WHERE show_name = ?
           ORDER BY season, number""", (show["name"],)).fetchall()
    conn.close()
    return render_template(
        "show.html", show=show, episodes=episodes, statuses=SHOW_STATUSES,
        tmdb=tmdb, active="library",
    )


@app.route("/discover")
def discover():
    return render_template("discover.html", tmdb=tmdb, active="discover")


@app.route("/stats")
def stats():
    conn = db.connect()
    s = {}
    s["shows"] = conn.execute("SELECT COUNT(*) c FROM shows").fetchone()["c"]
    s["movies"] = conn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"]
    s["episodes"] = conn.execute("SELECT COUNT(*) c FROM episodes_seen").fetchone()["c"]
    s["episodes_total"] = conn.execute(
        "SELECT COALESCE(SUM(episodes_seen),0) c FROM shows").fetchone()["c"]
    s["rated_movies"] = conn.execute(
        "SELECT COUNT(*) c FROM movies WHERE rating IS NOT NULL").fetchone()["c"]
    s["movie_seconds"] = conn.execute(
        "SELECT COALESCE(SUM(runtime),0) c FROM movies").fetchone()["c"]
    top_shows = conn.execute(
        """SELECT * FROM shows ORDER BY episodes_seen DESC LIMIT 10""").fetchall()
    recent_movies = conn.execute(
        """SELECT * FROM movies ORDER BY updated_at DESC LIMIT 10""").fetchall()

    # ---- chart data (all JSON-serializable, drawn client-side by Chart.js) ----
    charts = {}

    # Status breakdown (doughnuts)
    charts["show_status"] = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM shows GROUP BY status")}
    charts["movie_status"] = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM movies GROUP BY status")}

    # Movies by decade (bar)
    decades = {}
    for r in conn.execute(
            "SELECT release_date FROM movies WHERE release_date IS NOT NULL AND release_date != ''"):
        y = (r["release_date"] or "")[:4]
        if y.isdigit():
            d = f"{(int(y) // 10) * 10}s"
            decades[d] = decades.get(d, 0) + 1
    charts["decades"] = dict(sorted(decades.items()))

    # Personal movie ratings histogram (bar)
    ratings = {str(i): 0 for i in range(1, 11)}
    for r in conn.execute(
            "SELECT rating, COUNT(*) c FROM movies WHERE rating IS NOT NULL GROUP BY rating"):
        ratings[str(r["rating"])] = r["c"]
    charts["ratings"] = ratings

    # Episodes watched per year (line) — from the watched_at timestamps
    years = {}
    for r in conn.execute(
            "SELECT substr(watched_at,1,4) y, COUNT(*) c FROM episodes_seen "
            "WHERE watched_at IS NOT NULL GROUP BY y ORDER BY y"):
        if (r["y"] or "").isdigit():
            years[r["y"]] = r["c"]
    charts["episodes_per_year"] = years

    # Top shows by episodes (horizontal bar)
    charts["top_shows"] = {r["name"]: r["episodes_seen"]
                           for r in top_shows if r["episodes_seen"]}

    conn.close()
    return render_template(
        "stats.html", s=s, top_shows=top_shows, recent_movies=recent_movies,
        charts=charts, tmdb=tmdb, active="stats",
    )


@app.route("/shows")
def library():
    status = request.args.get("status", "all")
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort", "name")

    where, params = [], []
    if status != "all":
        where.append("status = ?")
        params.append(status)
    if q:
        where.append("name LIKE ?")
        params.append(f"%{q}%")
    sql = "SELECT * FROM shows"
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = {
        "name": "name COLLATE NOCASE ASC",
        "episodes": "episodes_seen DESC",
        "score": "addiction_score DESC, episodes_seen DESC",
        "recent": "updated_at DESC",
    }.get(sort, "name COLLATE NOCASE ASC")
    sql += f" ORDER BY {order}"

    conn = db.connect()
    shows = conn.execute(sql, params).fetchall()
    counts = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM shows GROUP BY status")}
    counts["all"] = conn.execute("SELECT COUNT(*) c FROM shows").fetchone()["c"]
    conn.close()

    return render_template(
        "library.html", shows=shows, counts=counts, status=status, q=q, sort=sort,
        statuses=["all"] + SHOW_STATUSES, tmdb=tmdb, active="library",
    )


@app.route("/settings")
def settings():
    conn = db.connect()
    missing = (conn.execute("SELECT COUNT(*) c FROM shows WHERE image_url IS NULL AND poster_path IS NULL").fetchone()["c"]
               + conn.execute("SELECT COUNT(*) c FROM movies WHERE image_url IS NULL AND poster_path IS NULL").fetchone()["c"])
    conn.close()
    return render_template(
        "settings.html", has_key=tmdb.has_key(), active="settings",
        unenriched_shows=_count_unenriched("shows"),
        unenriched_movies=_count_unenriched("movies"),
        missing_posters=missing,
    )


def _count_unenriched(table):
    conn = db.connect()
    n = conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE enriched=0").fetchone()["c"]
    conn.close()
    return n


# ------------------------------------------------------------------ api -----

@app.route("/api/settings", methods=["POST"])
def api_settings():
    key = (request.form.get("tmdb_api_key") or "").strip()
    db.set_setting("tmdb_api_key", key)
    return redirect(url_for("settings"))


@app.route("/api/import", methods=["POST"])
def api_import():
    """Import a TV Time export uploaded from the Settings page.

    Accepts a .zip of the GDPR CSVs, or one/many raw .csv files. Everything is
    unpacked into a temp folder, run through the importer, then cleaned up.
    """
    files = request.files.getlist("export")
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"ok": False, "error": "no file uploaded"}), 400

    tmp = tempfile.mkdtemp(prefix="mytv_import_")
    try:
        for f in files:
            name = os.path.basename(f.filename)
            if name.lower().endswith(".zip"):
                zpath = os.path.join(tmp, name)
                f.save(zpath)
                try:
                    with zipfile.ZipFile(zpath) as z:
                        for m in z.namelist():
                            if m.lower().endswith(".csv"):
                                # flatten: drop any folder path inside the zip
                                target = os.path.join(tmp, os.path.basename(m))
                                if os.path.basename(m):
                                    with z.open(m) as src, open(target, "wb") as dst:
                                        shutil.copyfileobj(src, dst)
                except zipfile.BadZipFile:
                    return jsonify({"ok": False, "error": "not a valid .zip"}), 400
            elif name.lower().endswith(".csv"):
                f.save(os.path.join(tmp, name))
        counts = import_data.run_import(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Always translate foreign titles to English + fetch posters on import.
    auto = _auto_enrich_translate()
    return jsonify({"ok": True, "counts": counts, "auto": auto})


@app.route("/api/show/<int(signed=True):tvdb_id>/update", methods=["POST"])
def api_show_update(tvdb_id):
    fields, params = [], []
    if "status" in request.form:
        fields.append("status = ?"); params.append(request.form["status"])
    if "is_favorite" in request.form:
        fields.append("is_favorite = ?"); params.append(int(request.form["is_favorite"]))
    if not fields:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    fields.append("updated_at = ?"); params.append(now())
    params.append(tvdb_id)
    conn = db.connect()
    conn.execute(f"UPDATE shows SET {', '.join(fields)} WHERE tvdb_id = ?", params)
    conn.commit(); conn.close()
    return jsonify({"ok": True})


def _ensure_tmdb_id(conn, show):
    """Resolve & persist a show's tmdb_id (and poster/overview) if missing."""
    if show["tmdb_id"]:
        return show["tmdb_id"]
    if not tmdb.has_key():
        return None
    hit = tmdb.resolve_tv(tvdb_id=show["tvdb_id"], name=show["name"])
    if not hit:
        return None
    conn.execute(
        """UPDATE shows SET tmdb_id=?,
               poster_path=COALESCE(poster_path, ?),
               overview=COALESCE(overview, ?),
               first_air_date=COALESCE(first_air_date, ?),
               vote_average=COALESCE(vote_average, ?)
           WHERE tvdb_id=?""",
        (hit.get("id"), hit.get("poster_path"), hit.get("overview"),
         hit.get("first_air_date"), hit.get("vote_average"), show["tvdb_id"]))
    conn.commit()
    return hit.get("id")


def _watched_map(conn, tvdb_id):
    """Set of (season, number) already watched for a show."""
    rows = conn.execute(
        "SELECT season, number FROM episodes_seen WHERE tvdb_id=?", (tvdb_id,)).fetchall()
    return {(r["season"], r["number"]) for r in rows}


def _recompute_show(conn, tvdb_id):
    """Refresh episodes_seen count, runtime and last-watched position from rows."""
    row = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(runtime),0) rt
           FROM episodes_seen WHERE tvdb_id=?""", (tvdb_id,)).fetchone()
    last = conn.execute(
        """SELECT season, number FROM episodes_seen WHERE tvdb_id=?
           ORDER BY season DESC, number DESC LIMIT 1""", (tvdb_id,)).fetchone()
    conn.execute(
        """UPDATE shows SET episodes_seen=?, runtime=?, last_season=?, last_episode=?,
                            updated_at=? WHERE tvdb_id=?""",
        (row["n"], row["rt"], last["season"] if last else None,
         last["number"] if last else None, now(), tvdb_id))
    return row["n"]


@app.route("/api/show/<int(signed=True):tvdb_id>/next")
def api_show_next(tvdb_id):
    """Next unwatched episode for Continue Watching card: {season, number, name, air_date, still_url, seen, total}."""
    conn = db.connect()
    show = conn.execute("SELECT * FROM shows WHERE tvdb_id=?", (tvdb_id,)).fetchone()
    if not show:
        conn.close(); return jsonify({"ok": False, "error": "not found"}), 404
    tmdb_id = _ensure_tmdb_id(conn, show)
    if not tmdb_id:
        conn.close()
        return jsonify({"ok": False, "error": "no_tmdb"})
    seasons = tmdb.tv_seasons(tmdb_id) or []
    watched = _watched_map(conn, tvdb_id)
    conn.close()

    # Find first unwatched episode across all seasons
    next_ep = None
    total = sum(s["episodes"] for s in seasons if s["season"] not in (None, 0))
    for s in seasons:
        if s["season"] in (None, 0) or s["episodes"] == 0:
            continue
        eps = tmdb.season_episodes(tmdb_id, s["season"]) or []
        for e in eps:
            key = (e["season"], e["number"])
            if key not in watched:
                next_ep = e
                break
        if next_ep:
            break

    if not next_ep:
        return jsonify({"ok": True, "next": None, "seen": sum(1 for _ in watched), "total": total})

    return jsonify({"ok": True, "next": {
        "season": next_ep["season"],
        "number": next_ep["number"],
        "name": next_ep["name"],
        "air_date": next_ep["air_date"],
        "still_url": next_ep["still_url"],
    }, "seen": sum(1 for _ in watched), "total": total})


@app.route("/api/show/<int(signed=True):tvdb_id>/episodes")
def api_show_episodes(tvdb_id):
    """Season summaries for a show + watched counts + next unwatched episode."""
    conn = db.connect()
    show = conn.execute("SELECT * FROM shows WHERE tvdb_id=?", (tvdb_id,)).fetchone()
    if not show:
        conn.close(); return jsonify({"ok": False, "error": "not found"}), 404
    tmdb_id = _ensure_tmdb_id(conn, show)
    if not tmdb_id:
        conn.close()
        return jsonify({"ok": False, "error": "no_tmdb",
                        "reason": "Add a TMDB key, or no match found."})
    seasons = tmdb.tv_seasons(tmdb_id) or []
    watched = _watched_map(conn, tvdb_id)
    conn.close()
    out = []
    for s in seasons:
        if s["season"] in (None,) or s["episodes"] == 0:
            continue
        wc = sum(1 for (se, _n) in watched if se == s["season"])
        out.append({**s, "watched": wc})
    return jsonify({"ok": True, "tmdb_id": tmdb_id, "seasons": out})


@app.route("/api/show/<int(signed=True):tvdb_id>/season/<int:season>")
def api_show_season(tvdb_id, season):
    """Episodes of one season, each flagged watched."""
    conn = db.connect()
    show = conn.execute("SELECT tmdb_id FROM shows WHERE tvdb_id=?", (tvdb_id,)).fetchone()
    if not show or not show["tmdb_id"]:
        conn.close(); return jsonify({"ok": False, "error": "no_tmdb"}), 400
    eps = tmdb.season_episodes(show["tmdb_id"], season) or []
    watched = _watched_map(conn, tvdb_id)
    conn.close()
    for e in eps:
        e["watched"] = (e["season"], e["number"]) in watched
    return jsonify({"ok": True, "episodes": eps})


@app.route("/api/show/<int(signed=True):tvdb_id>/episode", methods=["POST"])
def api_show_episode_toggle(tvdb_id):
    """Mark one episode watched / unwatched, then recompute show progress."""
    season = int(request.form["season"])
    number = int(request.form["number"])
    watched = request.form.get("watched") == "1"
    runtime = int(request.form.get("runtime") or 0)
    conn = db.connect()
    show = conn.execute("SELECT name FROM shows WHERE tvdb_id=?", (tvdb_id,)).fetchone()
    if not show:
        conn.close(); return jsonify({"ok": False, "error": "not found"}), 404
    # de-dupe: drop any existing row for this exact episode first
    conn.execute("DELETE FROM episodes_seen WHERE tvdb_id=? AND season=? AND number=?",
                 (tvdb_id, season, number))
    if watched:
        eid = -(abs(tvdb_id) * 1_000_000 + season * 1000 + number)
        conn.execute(
            """INSERT OR REPLACE INTO episodes_seen
                   (episode_id, tvdb_id, show_name, season, number, runtime, watched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (eid, tvdb_id, show["name"], season, number, runtime, now()))
    count = _recompute_show(conn, tvdb_id)
    conn.commit(); conn.close()
    return jsonify({"ok": True, "episodes_seen": count})


@app.route("/api/movie/<int:movie_id>/update", methods=["POST"])
def api_movie_update(movie_id):
    fields, params = [], []
    if "status" in request.form:
        fields.append("status = ?"); params.append(request.form["status"])
    if "rating" in request.form:
        val = request.form["rating"]
        fields.append("rating = ?"); params.append(int(val) if val else None)
    if not fields:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    fields.append("updated_at = ?"); params.append(now())
    params.append(movie_id)
    conn = db.connect()
    conn.execute(f"UPDATE movies SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit(); conn.close()
    return jsonify({"ok": True})


def _library_index():
    """(names, tmdb_ids) already in the library — used to flag Discover cards."""
    conn = db.connect()
    names, ids = set(), set()
    for t in ("shows", "movies"):
        for r in conn.execute(f"SELECT name, tmdb_id FROM {t}"):
            if r["name"]:
                names.add(r["name"].strip().lower())
            if r["tmdb_id"]:
                ids.add((("tv" if t == "shows" else "movie"), r["tmdb_id"]))
    conn.close()
    return names, ids


def _flag_library(results):
    names, ids = _library_index()
    for x in results:
        in_lib = (x.get("tmdb_id") and (x.get("kind"), x["tmdb_id"]) in ids) \
            or ((x.get("name") or "").strip().lower() in names)
        x["in_library"] = bool(in_lib)
    return results


@app.route("/api/dashboard/history")
def api_dashboard_history():
    """Recent activity: last 20 watched episodes + movies."""
    conn = db.connect()
    items = []

    # Episodes watched
    eps = conn.execute(
        """SELECT episode_id, tvdb_id, show_name, season, number, watched_at
           FROM episodes_seen ORDER BY watched_at DESC LIMIT 20""").fetchall()
    for e in eps:
        items.append({
            "timestamp": e["watched_at"],
            "type": "episode",
            "title": f"{e['show_name']} S{e['season']}E{e['number']}",
            "show": e["show_name"],
        })

    # Movies marked watched
    movies = conn.execute(
        """SELECT id, name, updated_at FROM movies WHERE status='watched'
           ORDER BY updated_at DESC LIMIT 20""").fetchall()
    for m in movies:
        items.append({
            "timestamp": m["updated_at"],
            "type": "movie",
            "title": m["name"],
        })

    conn.close()
    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return jsonify({"ok": True, "items": items[:20]})


@app.route("/api/dashboard/calendar")
def api_dashboard_calendar():
    """Movies + shows this month with poster data. Optimized for speed."""
    from datetime import datetime
    today = datetime.now()
    month_start = f"{today.year:04d}-{today.month:02d}-01"
    month_end = f"{today.year:04d}-{today.month:02d}-31"

    conn = db.connect()
    items = []

    # The dashboard already renders movies from SQLite. Other consumers can
    # still request the complete calendar by omitting episodes_only=1.
    if request.args.get("episodes_only") != "1":
        movies = conn.execute(
            f"""SELECT id, name, release_date, status, poster_path, image_url FROM movies
               WHERE release_date BETWEEN ? AND ? ORDER BY release_date""",
            (month_start, month_end)).fetchall()

        for m in movies:
            poster_url = m["image_url"] or (tmdb.poster_url(m["poster_path"]) if m["poster_path"] else None)
            items.append({
                "date": m["release_date"],
                "title": m["name"],
                "type": "movie",
                "status": m["status"],
                "id": m["id"],
                "poster_url": poster_url,
            })

    # Shows being watched - get upcoming episodes (limit to 10 shows to avoid slow TMDB calls)
    shows = conn.execute(
        """SELECT tvdb_id, name, tmdb_id, status, poster_path, image_url 
           FROM shows WHERE status IN ('watching', 'want')
           ORDER BY episodes_seen DESC LIMIT 10"""
    ).fetchall()

    for show in shows:
        if not show["tmdb_id"]:
            continue
        try:
            seasons = tmdb.tv_seasons(show["tmdb_id"]) or []
            if not seasons:
                continue
            # Check only the most-recent season to keep load minimal
            last_season = max((s["season"] for s in seasons if s["season"] not in (None, 0)), default=None)
            if last_season is None:
                continue
            eps = tmdb.season_episodes(show["tmdb_id"], last_season) or []
            for e in eps:
                if e["air_date"] and month_start <= e["air_date"] <= month_end:
                    poster_url = show["image_url"] or (tmdb.poster_url(show["poster_path"]) if show["poster_path"] else None)
                    items.append({
                        "date": e["air_date"],
                        "title": show["name"],
                        "episode_name": e["name"],
                        "season": e["season"],
                        "number": e["number"],
                        "type": "episode",
                        "status": show["status"],
                        "poster_url": poster_url,
                        "tvdb_id": show["tvdb_id"],
                    })
        except Exception:
            pass  # silently skip if TMDB fails

    conn.close()
    items.sort(key=lambda x: x["date"])
    return jsonify({"ok": True, "items": items})


@app.route("/api/discover")
def api_discover():
    """A TMDB feed for the Discover page (trending / popular / airing / upcoming)."""
    feed = request.args.get("feed", "trending_day")
    fn = tmdb.FEEDS.get(feed)
    if not fn:
        return jsonify({"ok": False, "error": "unknown feed"}), 400
    if not tmdb.has_key():
        return jsonify({"ok": False, "error": "no_key", "results": []})
    return jsonify({"ok": True, "results": _flag_library(fn())})


@app.route("/api/search")
def api_search():
    """Unified Discover search: one name box, movies AND shows together.

    No kind filter — TMDB /search/multi (posters) when a key is set, else both
    keyless providers merged. Results carry their own 'kind' tag so each card
    shows a 🎬 / 📺 marker.
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})
    if tmdb.has_key():
        results = tmdb.search_multi(q)
    else:
        results = ([dict(r, kind="tv") for r in providers.tvmaze_search(q)]
                   + [dict(r, kind="movie") for r in providers.omdb_search(q)])
    return jsonify({"results": _flag_library(results)[:24]})


@app.route("/api/add", methods=["POST"])
def api_add():
    """Add a title picked from Discover into the library."""
    kind = request.form.get("kind", "tv")
    name = request.form.get("name", "").strip()
    image_url = request.form.get("image_url") or None
    overview = request.form.get("overview") or None
    date = request.form.get("date") or None
    vote = request.form.get("vote")
    vote = float(vote) if vote else None
    provider_id = request.form.get("provider_id") or ""
    tmdb_id = request.form.get("tmdb_id")
    tmdb_id = int(tmdb_id) if (tmdb_id or "").isdigit() else None
    conn = db.connect()
    if kind == "tv":
        # Synthetic negative id so manual adds never clash with positive TVDB
        # ids from the import. Derive it from the TMDB/TVmaze id (or name hash).
        seed = int(provider_id) if provider_id.isdigit() else abs(hash(name)) % 10_000_000
        tvdb_id = -seed
        conn.execute(
            """INSERT INTO shows (tvdb_id, tmdb_id, name, image_url, overview,
                                  first_air_date, vote_average, status, enriched,
                                  source, added_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'want', 1, 'manual', ?, ?)
               ON CONFLICT(tvdb_id) DO UPDATE SET
                   tmdb_id=excluded.tmdb_id, image_url=excluded.image_url,
                   overview=excluded.overview, enriched=1, updated_at=excluded.updated_at""",
            (tvdb_id, tmdb_id, name, image_url, overview, date, vote, now(), now()),
        )
    else:
        conn.execute(
            """INSERT INTO movies (tmdb_id, name, image_url, overview, release_date,
                                   vote_average, status, enriched, source, added_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'want', 1, 'manual', ?, ?)
               ON CONFLICT(name, release_date) DO UPDATE SET
                   tmdb_id=excluded.tmdb_id, image_url=excluded.image_url,
                   overview=excluded.overview, enriched=1, updated_at=excluded.updated_at""",
            (tmdb_id, name, image_url, overview, date, vote, now(), now()),
        )
    conn.commit(); conn.close()
    return jsonify({"ok": True})


MISSING_POSTER = "image_url IS NULL AND poster_path IS NULL"


@app.route("/api/enrich", methods=["POST"])
def api_enrich():
    """Fetch posters/overviews for imported items that lack them.

    Prefers TMDB when a key is set — it matches foreign / original-language
    titles (anime, non-English movies) that the keyless providers miss — and
    falls back to TVmaze (shows) / OMDb (movies) otherwise.

    mode='new'   -> first pass over never-enriched rows (bounded, terminates).
    mode='retry' -> re-attempt only rows still missing a poster (for titles the
                    first pass couldn't match). Stops when a batch fills none.
    """
    batch = int(request.form.get("batch", 12))
    mode = request.form.get("mode", "new")
    return jsonify(_enrich_batch(batch, mode))


def _enrich_batch(batch=12, mode="new"):
    use_tmdb = tmdb.has_key()
    filled = {"shows": 0, "movies": 0}
    conn = db.connect()

    where = MISSING_POSTER if mode == "retry" else "enriched=0"
    shows = conn.execute(
        f"SELECT tvdb_id, name FROM shows WHERE {where} LIMIT ?", (batch,)).fetchall()
    for s in shows:
        got = False
        if use_tmdb:
            hit = tmdb.resolve_tv(tvdb_id=s["tvdb_id"], name=s["name"])
            if hit and hit.get("poster_path"):
                conn.execute(
                    """UPDATE shows SET tmdb_id=?, poster_path=?,
                              overview=COALESCE(overview, ?),
                              first_air_date=COALESCE(first_air_date, ?),
                              vote_average=COALESCE(vote_average, ?), enriched=1, updated_at=?
                       WHERE tvdb_id=?""",
                    (hit.get("id"), hit["poster_path"], hit.get("overview"),
                     hit.get("first_air_date"), hit.get("vote_average"), now(), s["tvdb_id"]))
                got = True
        if not got:
            info = providers.tvmaze_show(s["name"])
            if info and info.get("image_url"):
                conn.execute(
                    """UPDATE shows SET image_url=?, overview=COALESCE(overview, ?),
                              first_air_date=COALESCE(first_air_date, ?),
                              vote_average=COALESCE(vote_average, ?), enriched=1, updated_at=?
                       WHERE tvdb_id=?""",
                    (info["image_url"], info.get("overview"), info.get("date"),
                     info.get("vote"), now(), s["tvdb_id"]))
                got = True
        if got:
            filled["shows"] += 1
        elif mode == "new":
            conn.execute("UPDATE shows SET enriched=1, updated_at=? WHERE tvdb_id=?",
                         (now(), s["tvdb_id"]))

    movies = conn.execute(
        f"SELECT id, name, release_date FROM movies WHERE {where} LIMIT ?", (batch,)).fetchall()
    for m in movies:
        year = (m["release_date"] or "")[:4] or None
        got = False
        if use_tmdb:
            hit = tmdb.resolve_movie(m["name"], year=year)
            if hit and hit.get("poster_path"):
                conn.execute(
                    """UPDATE movies SET tmdb_id=?, poster_path=?,
                              overview=COALESCE(overview, ?),
                              vote_average=COALESCE(vote_average, ?), enriched=1, updated_at=?
                       WHERE id=?""",
                    (hit.get("id"), hit["poster_path"], hit.get("overview"),
                     hit.get("vote_average"), now(), m["id"]))
                got = True
        if not got:
            info = providers.omdb_movie(m["name"], year=year)
            if info and info.get("image_url"):
                conn.execute(
                    """UPDATE movies SET image_url=?, overview=COALESCE(overview, ?),
                              vote_average=COALESCE(vote_average, ?), enriched=1, updated_at=?
                       WHERE id=?""",
                    (info["image_url"], info.get("overview"), info.get("vote"), now(), m["id"]))
                got = True
        if got:
            filled["movies"] += 1
        elif mode == "new":
            conn.execute("UPDATE movies SET enriched=1, updated_at=? WHERE id=?",
                         (now(), m["id"]))

    conn.commit()
    if mode == "retry":
        remaining = (conn.execute(f"SELECT COUNT(*) c FROM shows WHERE {MISSING_POSTER}").fetchone()["c"]
                     + conn.execute(f"SELECT COUNT(*) c FROM movies WHERE {MISSING_POSTER}").fetchone()["c"])
    else:
        remaining = (conn.execute("SELECT COUNT(*) c FROM shows WHERE enriched=0").fetchone()["c"]
                     + conn.execute("SELECT COUNT(*) c FROM movies WHERE enriched=0").fetchone()["c"])
    conn.close()
    return {"ok": True, "filled": filled, "done": filled,
            "remaining": remaining, "batch": batch}


def _auto_enrich_translate():
    """Fetch posters + translate foreign titles to English for freshly imported
    rows. Runs after every import so the library is English + poster-complete
    without a manual Settings click. Bounded loops; no-op without a TMDB key.
    """
    if not tmdb.has_key():
        return {"translated": {"shows": 0, "movies": 0}, "enriched": {"shows": 0, "movies": 0}}
    _reset_foreign_translation_flags()
    tr = {"shows": 0, "movies": 0}
    for _ in range(300):
        fixed, remaining = _translate_batch(15)
        tr["shows"] += fixed["shows"]; tr["movies"] += fixed["movies"]
        if remaining == 0:
            break
    en = {"shows": 0, "movies": 0}
    for _ in range(300):
        res = _enrich_batch(15, "new")
        en["shows"] += res["filled"]["shows"]; en["movies"] += res["filled"]["movies"]
        if res["remaining"] == 0:
            break
    return {"translated": tr, "enriched": en}


# ----------------------------------------------------------- data fixes -----

ENGLISH_TITLE_ALIASES = {
    "एम. एस. धोनी: द अनटोल्ड स्टोरी": "M.S. Dhoni: The Untold Story",
    "कभी खुशी कभी ग़म...": "Kabhi Khushi Kabhie Gham",
    "कार्तिक कॉलिंग कार्तिक": "Karthik Calling Karthik",
    "ग़जनी": "Ghajini",
    "गुस्ताख़ इश्क़": "Gustaakh Ishq",
    "गोलमाल: फन अनलिमिटेड": "Golmaal: Fun Unlimited",
    "चक दे! इंडिया": "Chak De! India",
    "ज़िन्दगी न मिलेगी दोबारा": "Zindagi Na Milegi Dobara",
    "जॉली एलएल.बी २": "Jolly LLB 2",
    "टाइगर जिंदा है": "Tiger Zinda Hai",
    "तान्हाजी: द अनसंग वारियर": "Tanhaji: The Unsung Warrior",
    "द लंचबॉक्स": "The Lunchbox",
    "फिर हेरा फेरी": "Phir Hera Pheri",
    "रा.वन": "Ra.One",
    "रेस २": "Race 2",
    "वंस अपॉन ए टाइम इन मुंबई": "Once Upon a Time in Mumbaai",
    "स्टूडेंट ऑफ द ईयर": "Student of the Year",
    "२ स्टेट्स": "2 States",
    "३ इडियट्स": "3 Idiots",
    "காந்தி பேசுகிறார்": "Gandhi Talks",
    "சுற்றுலா குடும்பம்": "Tourist Family",
    "లక్కీ బాస్కర్": "Lucky Baskhar",
    "ಮಹಾವತಾರ ನರಸಿಂಹ": "Mahavatar Narsimha",
    "ഏകോ": "Eko",
    "സര്‍വ്വം മായ": "Sarvam Maya",
    "ドラゴンボール超スーパー ブロリー": "Dragon Ball Super: Broly",
    "ドラゴンボール オッス!帰ってきた孫悟空と仲間たち!! 移動先: 案内、 検索":
        "Dragon Ball: Yo! Son Goku and His Friends Return!!",
}

def _is_foreign(name):
    """True if a title uses a non-Latin script (Devanagari/Hindi, Tamil, Telugu,
    Kannada, Malayalam, CJK, Cyrillic, Arabic, …).

    Threshold 0x036F is the end of Latin + IPA + combining diacritics, so
    accented Latin (é, ñ, ü) stays 'English'. The Indic scripts (Devanagari
    0x0900, Tamil 0x0B80, Telugu 0x0C00, Kannada 0x0C80, Malayalam 0x0D00) all
    live BELOW 0x2000 — the old threshold silently skipped every Hindi title.
    """
    if not name:
        return False
    # Ignore punctuation (curly quotes, dashes, ellipses, etc.). Only letters
    # from a non-Latin writing system require English-title normalization.
    return any(
        c.isalpha() and "LATIN" not in unicodedata.name(c, "")
        for c in name
    )


@app.route("/api/fix-statuses", methods=["POST"])
def api_fix_statuses():
    """Reclassify imported shows into want / watching / watched.

    The TV Time export flags every show 'followed', so the importer parked all
    of them in 'watching'. Here we compare episodes_seen to the real episode
    count from TMDB: 0 seen -> want, all seen -> watched, some -> watching.
    Batched so the Settings page can loop until remaining hits 0.
    """
    if not tmdb.has_key():
        return jsonify({"ok": False, "error": "no_key"}), 400
    batch = int(request.form.get("batch", 15))
    conn = db.connect()
    # Only shows we haven't classified yet this pass (marked via status_fixed=1).
    rows = conn.execute(
        "SELECT tvdb_id, name, tmdb_id, episodes_seen FROM shows "
        "WHERE status_fixed=0 LIMIT ?", (batch,)).fetchall()
    changed = 0
    for s in rows:
        tmdb_id = s["tmdb_id"]
        total = None
        if tmdb_id:
            total, _series_status = tmdb.tv_episode_total(tmdb_id)
        seen = s["episodes_seen"] or 0
        if seen == 0:
            status = "want"
        elif total and seen >= total - 1:      # tolerance for off-by-one specials
            status = "watched"
        else:
            status = "watching"
        conn.execute(
            "UPDATE shows SET status=?, status_fixed=1, updated_at=? WHERE tvdb_id=?",
            (status, now(), s["tvdb_id"]))
        changed += 1
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) c FROM shows WHERE status_fixed=0").fetchone()["c"]
    conn.close()
    return jsonify({"ok": True, "changed": changed, "remaining": remaining})


def _translate_batch(batch=12):
    """One translate pass. Returns (fixed_counts, remaining). Caller loops."""
    conn = db.connect()
    fixed = {"shows": 0, "movies": 0}
    shows = conn.execute(
        "SELECT tvdb_id, name, tmdb_id FROM shows WHERE translated=0 LIMIT ?",
        (batch,)).fetchall()
    for s in shows:
        _translate_row(conn, "tv", s, "shows", "tvdb_id", s["tvdb_id"], fixed)
    movies = conn.execute(
        "SELECT id, name, tmdb_id, release_date FROM movies WHERE translated=0 LIMIT ?",
        (batch,)).fetchall()
    for m in movies:
        _translate_row(conn, "movie", m, "movies", "id", m["id"], fixed)
    conn.commit()
    remaining = (conn.execute("SELECT COUNT(*) c FROM shows WHERE translated=0").fetchone()["c"]
                 + conn.execute("SELECT COUNT(*) c FROM movies WHERE translated=0").fetchone()["c"])
    conn.close()
    return fixed, remaining


def _reset_foreign_translation_flags():
    """Requeue native-script titles skipped by older translation logic."""
    conn = db.connect()
    queued = {"shows": 0, "movies": 0}
    for table in ("shows", "movies"):
        # Known imported titles can be normalized without relying on a remote
        # search. Requeue them so _translate_row applies the canonical alias.
        for original in ENGLISH_TITLE_ALIASES:
            conn.execute(
                f"UPDATE {table} SET translated=0 WHERE name=?",
                (original,),
            )
        rows = conn.execute(
            f"SELECT rowid AS source_rowid, name FROM {table} WHERE translated=1"
        ).fetchall()
        ids = [row["source_rowid"] for row in rows if _is_foreign(row["name"])]
        if ids:
            conn.executemany(
                f"UPDATE {table} SET translated=0 WHERE rowid=?",
                ((rowid,) for rowid in ids),
            )
        queued[table] = len(ids)
    conn.commit()
    conn.close()
    return queued


@app.route("/api/translate", methods=["POST"])
def api_translate():
    """Rename foreign-language titles to their English (en-US) TMDB names.

    Anime/Indic-language films imported with native-script titles. We resolve a
    tmdb_id if one is missing, then overwrite the name with TMDB's English title
    (and grab a poster while we're there). Batched; loops until remaining is 0.
    """
    if not tmdb.has_key():
        return jsonify({"ok": False, "error": "no_key"}), 400
    queued = {"shows": 0, "movies": 0}
    if request.form.get("reset_foreign") == "1":
        queued = _reset_foreign_translation_flags()
    fixed, remaining = _translate_batch(int(request.form.get("batch", 12)))
    return jsonify({"ok": True, "queued": queued, "fixed": fixed, "remaining": remaining})


def _translate_row(conn, kind, row, table, id_col, id_val, fixed):
    """Resolve English title for one row (only if the name is foreign)."""
    name = row["name"]
    tmdb_id = row["tmdb_id"]
    alias = ENGLISH_TITLE_ALIASES.get(name)
    if alias:
        conn.execute(
            f"UPDATE {table} SET name=?, translated=1, enriched=0, updated_at=? "
            f"WHERE {id_col}=?",
            (alias, now(), id_val),
        )
        if table == "shows":
            conn.execute(
                "UPDATE episodes_seen SET show_name=? WHERE tvdb_id=?",
                (alias, id_val),
            )
        fixed["shows" if table == "shows" else "movies"] += 1
        return
    # Mark handled up-front so a non-foreign / unmatched row won't loop forever.
    if not _is_foreign(name):
        conn.execute(f"UPDATE {table} SET translated=1 WHERE {id_col}=?", (id_val,))
        return
    if not tmdb_id:
        if kind == "tv":
            hit = tmdb.resolve_tv(tvdb_id=row["tvdb_id"] if "tvdb_id" in row.keys() else None,
                                  name=name)
        else:
            year = (row["release_date"] or "")[:4] or None
            hit = tmdb.resolve_movie(name, year=year)
        if hit:
            tmdb_id = hit.get("id")
            conn.execute(
                f"UPDATE {table} SET tmdb_id=?, poster_path=COALESCE(poster_path, ?) "
                f"WHERE {id_col}=?",
                (tmdb_id, hit.get("poster_path"), id_val))
    en = tmdb.english_title(kind, tmdb_id) if tmdb_id else None
    if en and not _is_foreign(en):
        old_name = name
        conn.execute(
            f"UPDATE {table} SET name=?, translated=1, updated_at=? WHERE {id_col}=?",
            (en, now(), id_val))
        if table == "shows" and en != old_name:
            conn.execute(
                "UPDATE episodes_seen SET show_name=? WHERE tvdb_id=?",
                (en, id_val),
            )
        fixed["shows" if table == "shows" else "movies"] += 1
    else:
        conn.execute(f"UPDATE {table} SET translated=1 WHERE {id_col}=?", (id_val,))


@app.route("/api/import-watchlist", methods=["POST"])
def api_import_watchlist():
    """Recover the movie want-list the first import dropped.

    TV Time stores to-watch movies in tracking-prod-records.csv as type
    'towatch'. The original import ignored 'type' and marked every movie
    'watched'. This reads that source CSV and flips the towatch-only movies
    back to 'want'. One-shot; safe to re-run.
    """
    folder = import_data.DATA_DIR
    path = os.path.join(folder, "tracking-prod-records.csv")
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "source CSV not found"}), 400

    types = {}   # (name, release) -> set of type values
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("entity_type") or "").strip() != "movie":
                continue
            name = (r.get("movie_name") or "").strip()
            if not name:
                continue
            rel = (r.get("release_date") or "").split(" ")[0]
            types.setdefault((name, rel), set()).add((r.get("type") or "").strip())

    conn = db.connect()
    changed = 0
    for (name, rel), tset in types.items():
        if "towatch" in tset and "watch" not in tset:
            cur = conn.execute(
                "UPDATE movies SET status='want', updated_at=? "
                "WHERE name=? AND status!='want'", (now(), name))
            changed += cur.rowcount
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "changed": changed})


# --------------------------------------------------------------- export -----

@app.route("/api/export")
def api_export():
    """Download your whole library as a TV-Time-GDPR-shaped .zip.

    The CSVs mirror the columns MyTV's own importer reads, so the file round-
    trips: re-importing it reproduces your library (import is upsert-safe).
    Also portable to spreadsheets / other trackers.
    """
    conn = db.connect()
    shows = conn.execute("SELECT * FROM shows").fetchall()
    movies = conn.execute("SELECT * FROM movies").fetchall()
    episodes = conn.execute("SELECT * FROM episodes_seen").fetchall()
    conn.close()

    import io

    def _csv(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
        return buf.getvalue()

    def _csv_dict(cols, dict_rows):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in dict_rows:
            w.writerow(r)
        return buf.getvalue()

    # user_tv_show_data.csv — baseline show list
    shows_csv = _csv(
        ["tv_show_id", "tv_show_name", "is_favorited", "nb_episodes_seen", "is_followed"],
        [[s["tvdb_id"], s["name"], s["is_favorite"], s["episodes_seen"],
          "true" if s["status"] == "watching" else "false"] for s in shows])

    # tracking-prod-records-v2.csv — rich show + episode tracking
    v2_cols = ["key", "s_id", "series_name", "is_followed", "is_archived",
               "is_for_later", "most_recent_ep_watched", "followed_at",
               "ep_watch_count", "episode_id", "season_number", "s_no",
               "episode_number", "ep_no", "runtime", "created_at",
               "total_series_runtime", "total_movies_runtime"]
    v2_rows = [{
        "key": "tracking-stats",
        "total_series_runtime": db.get_setting("total_series_runtime", "0"),
        "total_movies_runtime": db.get_setting("total_movies_runtime", "0"),
        "ep_watch_count": db.get_setting("ep_watch_count", "0"),
    }]
    for s in shows:
        mre = ""
        if s["last_season"] is not None and s["last_episode"] is not None:
            mre = f"map[ep_no:{s['last_episode']} s_no:{s['last_season']}]"
        v2_rows.append({
            "s_id": s["tvdb_id"], "series_name": s["name"],
            "is_followed": "true" if s["status"] == "watching" else "false",
            "is_archived": "true" if s["status"] == "watched" else "false",
            "is_for_later": "true" if s["status"] == "want" else "false",
            "most_recent_ep_watched": mre,
            "followed_at": s["followed_at"] or "",
            "ep_watch_count": s["episodes_seen"],
        })
    for e in episodes:
        v2_rows.append({
            "s_id": e["tvdb_id"] or "", "series_name": e["show_name"] or "",
            "episode_id": e["episode_id"],
            "season_number": e["season"], "s_no": e["season"],
            "episode_number": e["number"], "ep_no": e["number"],
            "runtime": e["runtime"] or 0, "created_at": e["watched_at"] or "",
        })
    v2_csv = _csv_dict(v2_cols, v2_rows)

    # tracking-prod-records.csv — movies (want vs watched via 'type')
    movies_csv = _csv(
        ["entity_type", "movie_name", "release_date", "runtime", "rewatch_count", "type"],
        [["movie", m["name"], m["release_date"] or "", m["runtime"] or 0,
          m["rewatch_count"] or 0,
          "towatch" if m["status"] == "want" else "watch"] for m in movies])

    # ratings-live-votes.csv — personal movie ratings
    ratings_csv = _csv(
        ["movie_name", "vote_key"],
        [[m["name"], f"movie-{m['name']}-me-{m['rating']}"]
         for m in movies if m["rating"] is not None])

    stamp = datetime.now().strftime("%Y%m%d")
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("user_tv_show_data.csv", shows_csv)
        z.writestr("tracking-prod-records-v2.csv", v2_csv)
        z.writestr("tracking-prod-records.csv", movies_csv)
        z.writestr("ratings-live-votes.csv", ratings_csv)
    mem.seek(0)
    return send_file(mem, as_attachment=True,
                     download_name=f"mytv-export-{stamp}.zip",
                     mimetype="application/zip")


if __name__ == "__main__":
    print("MyTV running at http://127.0.0.1:5000  (Ctrl+C to stop)")
    app.run(debug=True, port=5000)
