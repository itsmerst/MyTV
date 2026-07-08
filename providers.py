"""Keyless / low-friction artwork + search providers.

Goal: posters show up with ZERO setup. No API key needed.
  - TV shows  -> TVmaze      (fully keyless, images + overview + rating)
  - Movies    -> OMDb        (free; a shared demo key ships as default, user
                              can paste their own on the Settings page)

TMDB (tmdb.py) stays as an optional upgrade for best coverage — but nothing
here depends on it.
"""
import requests
import db

TIMEOUT = 12

# Public OMDb demo key as a zero-setup default. Rate-limited/best-effort — a
# user who hits limits can drop their own free key (omdbapi.com) in Settings.
DEFAULT_OMDB_KEY = "trilogy"


def omdb_key():
    return (db.get_setting("omdb_api_key", "") or "").strip() or DEFAULT_OMDB_KEY


# ------------------------------------------------------------- TV (TVmaze) ---

def _tvmaze_norm(show):
    if not show:
        return None
    img = (show.get("image") or {}).get("original") or (show.get("image") or {}).get("medium")
    return {
        "name": show.get("name"),
        "image_url": img,
        "overview": _strip_html(show.get("summary")),
        "date": show.get("premiered"),
        "vote": (show.get("rating") or {}).get("average"),
        "provider_id": show.get("id"),
    }


def tvmaze_show(name):
    """Best single match for a show name."""
    try:
        r = requests.get("https://api.tvmaze.com/singlesearch/shows",
                         params={"q": name}, timeout=TIMEOUT)
        if r.status_code == 200:
            return _tvmaze_norm(r.json())
    except requests.RequestException:
        pass
    return None


def tvmaze_search(query):
    """Multiple show matches for Discover."""
    out = []
    try:
        r = requests.get("https://api.tvmaze.com/search/shows",
                         params={"q": query}, timeout=TIMEOUT)
        if r.status_code == 200:
            for row in r.json():
                norm = _tvmaze_norm(row.get("show"))
                if norm:
                    out.append(norm)
    except requests.RequestException:
        pass
    return out


# --------------------------------------------------------------- Movies (OMDb) ---

def _omdb_norm(d):
    if not d or d.get("Response") == "False":
        return None
    poster = d.get("Poster")
    if poster in (None, "N/A"):
        poster = None
    rating = d.get("imdbRating")
    try:
        rating = float(rating) if rating and rating != "N/A" else None
    except ValueError:
        rating = None
    year = d.get("Year") or ""
    return {
        "name": d.get("Title"),
        "image_url": poster,
        "overview": None if d.get("Plot") in (None, "N/A") else d.get("Plot"),
        "date": year[:4] if year else None,
        "vote": rating,
        "provider_id": d.get("imdbID"),
    }


def omdb_movie(name, year=None):
    params = {"t": name, "type": "movie", "apikey": omdb_key()}
    if year:
        params["y"] = year
    try:
        r = requests.get("https://www.omdbapi.com/", params=params, timeout=TIMEOUT)
        if r.status_code == 200:
            return _omdb_norm(r.json())
    except requests.RequestException:
        pass
    return None


def omdb_search(query):
    """Movie matches for Discover (includes latest releases by title)."""
    out = []
    try:
        r = requests.get("https://www.omdbapi.com/",
                         params={"s": query, "type": "movie", "apikey": omdb_key()},
                         timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            for row in data.get("Search", []):
                poster = row.get("Poster")
                out.append({
                    "name": row.get("Title"),
                    "image_url": None if poster in (None, "N/A") else poster,
                    "overview": None,
                    "date": row.get("Year"),
                    "vote": None,
                    "provider_id": row.get("imdbID"),
                })
    except requests.RequestException:
        pass
    return out


# ----------------------------------------------------------------- helpers ---

def _strip_html(s):
    if not s:
        return None
    import re
    return re.sub(r"<[^>]+>", "", s).strip()
