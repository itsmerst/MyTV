"""Thin TMDB (The Movie Database) API client.

Used to enrich your imported shows/movies with posters, overviews and ratings,
and to search for new titles to add. Everything works WITHOUT a key too — you
just won't get posters until you add one on the Settings page.

Get a free key at: https://www.themoviedb.org/settings/api  (v3 "API Key")
"""
import requests
import db

API_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/w342"
TIMEOUT = 12


def api_key():
    return db.get_setting("tmdb_api_key", "").strip()


def has_key():
    return bool(api_key())


def _get(path, params=None):
    key = api_key()
    if not key:
        return None
    params = dict(params or {})
    params["api_key"] = key
    try:
        r = requests.get(f"{API_BASE}{path}", params=params, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        return None
    return None


def poster_url(poster_path):
    return f"{IMG_BASE}{poster_path}" if poster_path else None


def still_url(still_path):
    return f"{IMG_BASE}{still_path}" if still_path else None


def search_tv(query):
    data = _get("/search/tv", {"query": query})
    return (data or {}).get("results", []) if data else []


def search_movie(query):
    data = _get("/search/movie", {"query": query})
    return (data or {}).get("results", []) if data else []


def search(kind, query):
    """Normalized search results (posters + overviews). Empty list if no key."""
    path = "/search/movie" if kind == "movie" else "/search/tv"
    media = "movie" if kind == "movie" else "tv"
    return _feed(path, media=media, params={"query": query})


def search_multi(query):
    """Search movies AND TV together by name (no kind filter).

    Uses TMDB /search/multi, which returns mixed movie/tv/person rows tagged
    with media_type. People are dropped. Ordered by TMDB popularity so the
    best-known title (movie or series) surfaces first.
    """
    data = _get("/search/multi", {"query": query})
    results = (data or {}).get("results", []) if data else []
    out = [normalize(x) for x in results]          # normalize reads media_type
    return [x for x in out if x and x["name"]]


def english_title(kind, tmdb_id):
    """English (en-US) title for a movie/show, used to translate foreign names."""
    if not tmdb_id:
        return None
    path = "/movie/" if kind == "movie" else "/tv/"
    data = _get(f"{path}{tmdb_id}", {"language": "en-US"})
    if not data:
        return None
    name = data.get("title") if kind == "movie" else data.get("name")
    return (name or "").strip() or None


def tv_episode_total(tmdb_id):
    """(number_of_episodes, series_status) for a show, for watched/watching split.

    series_status is TMDB's production status: 'Ended', 'Canceled',
    'Returning Series', etc.
    """
    data = tv_details(tmdb_id)
    if not data:
        return None, None
    return data.get("number_of_episodes"), data.get("status")


def find_by_tvdb(tvdb_id):
    """Look up a TMDB TV show from a TheTVDB id (TV Time uses TVDB ids)."""
    data = _get(f"/find/{tvdb_id}", {"external_source": "tvdb_id"})
    if not data:
        return None
    results = data.get("tv_results", [])
    return results[0] if results else None


def tv_details(tmdb_id):
    return _get(f"/tv/{tmdb_id}")


def movie_details(tmdb_id):
    return _get(f"/movie/{tmdb_id}")


# ------------------------------------------------------- discover feed -----

def normalize(item, media=None):
    """TMDB result -> the common card shape the frontend expects.

    `media` forces the kind ('movie' or 'tv'); otherwise read item['media_type']
    (present on /trending/all results).
    """
    if not item:
        return None
    mt = media or item.get("media_type")
    if mt not in ("movie", "tv"):
        return None
    name = item.get("title") if mt == "movie" else item.get("name")
    date = item.get("release_date") if mt == "movie" else item.get("first_air_date")
    return {
        "kind": mt,
        "name": name,
        "image_url": poster_url(item.get("poster_path")),
        "overview": item.get("overview") or None,
        "date": date or None,
        "vote": item.get("vote_average") or None,
        "provider_id": item.get("id"),
        "tmdb_id": item.get("id"),
    }


def _feed(path, media=None, params=None):
    data = _get(path, params)
    results = (data or {}).get("results", []) if data else []
    out = [normalize(x, media) for x in results]
    return [x for x in out if x and x["name"]]


def trending(window="day"):
    """Trending movies + TV together. window: 'day' or 'week'."""
    return _feed(f"/trending/all/{window}")


def popular_movies():
    return _feed("/movie/popular", media="movie")


def popular_tv():
    return _feed("/tv/popular", media="tv")


def airing_today():
    return _feed("/tv/airing_today", media="tv")


def upcoming_movies():
    return _feed("/movie/upcoming", media="movie")


FEEDS = {
    "trending_day":  lambda: trending("day"),
    "trending_week": lambda: trending("week"),
    "popular_movie": popular_movies,
    "popular_tv":    popular_tv,
    "airing_today":  airing_today,
    "upcoming":      upcoming_movies,
}


# ---------------------------------------------------- seasons & episodes -----

def tv_seasons(tmdb_id):
    """Season summaries for a show: [{season, episodes, name, air_date}]."""
    data = tv_details(tmdb_id)
    if not data:
        return None
    out = []
    for s in data.get("seasons", []):
        out.append({
            "season": s.get("season_number"),
            "episodes": s.get("episode_count") or 0,
            "name": s.get("name"),
            "air_date": s.get("air_date"),
        })
    return out


def season_episodes(tmdb_id, season_number):
    """Episodes of one season: [{season, number, name, air_date, runtime, overview, still_url}]."""
    data = _get(f"/tv/{tmdb_id}/season/{season_number}")
    if not data:
        return None
    out = []
    for e in data.get("episodes", []):
        out.append({
            "season": e.get("season_number"),
            "number": e.get("episode_number"),
            "name": e.get("name"),
            "air_date": e.get("air_date"),
            "runtime": (e.get("runtime") or 0) * 60,   # seconds, matches import
            "overview": e.get("overview") or None,
            "still_path": e.get("still_path"),
            "still_url": still_url(e.get("still_path")),
        })
    return out


def resolve_movie(name, year=None):
    """Best TMDB movie match. Matches foreign / original-language titles too."""
    params = {"query": name}
    if year:
        params["year"] = year
    data = _get("/search/movie", params)
    results = (data or {}).get("results", []) if data else []
    if not results and year:                 # retry without the year filter
        results = search_movie(name)
    return results[0] if results else None


def resolve_tv(tvdb_id=None, name=None):
    """Best TMDB match for a show. Returns the TMDB tv result dict or None.

    Prefers the exact TheTVDB id (imported shows); falls back to a name search
    which also matches foreign / original-language titles.
    """
    if tvdb_id and tvdb_id > 0:
        hit = find_by_tvdb(tvdb_id)
        if hit:
            return hit
    if name:
        results = search_tv(name)
        if results:
            return results[0]
    return None
