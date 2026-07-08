# 📺 MyTV — your local TV Time replacement

A private, offline-first web app to track the TV shows and movies you watch —
built to replace **TV Time** using your exported GDPR data. Everything runs on
your own machine; your data lives in a single local file (`mytv.db`).

## Features
- **Shows library** — 117 shows imported, with status (want / watching / following / watched / stopped), favorites, and episode counts
- **Movies library** — 449 movies imported, with ratings & rewatch counts
- **Show detail** — per-episode watch history, change status, favorite
- **Discover** — search TMDB and add new shows/movies in one click
- **Stats** — totals, most-watched shows, runtime logged
- **Posters & descriptions** via TMDB (optional free API key)

## First-time setup (once)
```bash
cd ~/MyTV
pip install -r requirements.txt      # installs Flask + requests
python import_data.py                # loads your TV Time export from data_raw/
```

## Run it
```bash
python app.py
```
Then open **http://127.0.0.1:5000** in your browser.

## Add posters & discovery (optional)
1. Get a free **TMDB v3 API key**: https://www.themoviedb.org/settings/api
2. Open **Settings** in the app, paste the key, Save
3. Click **Fetch posters** to enrich your imported library

## Re-importing / updating data
`python import_data.py` is safe to re-run — it upserts, so nothing duplicates.
To import a fresh export, drop the new CSVs into `data_raw/` and run it again.

## Project layout
```
MyTV/
├─ app.py            # Flask web app (routes + API)
├─ db.py             # SQLite schema & helpers
├─ import_data.py    # TV Time CSV → SQLite importer
├─ tmdb.py           # TMDB API client (posters, search)
├─ templates/        # HTML pages
├─ static/           # CSS + JS
├─ data_raw/         # your exported CSVs (git-ignored)
└─ mytv.db           # your local database (git-ignored)
```

## Notes
- TV Time stores shows by **TheTVDB** id; MyTV maps those to TMDB for artwork.
- TV Time's movie "rating" export is an opaque code, so rated movies show a
  **★ rated** badge rather than a possibly-misleading number.
- Nothing leaves your machine except optional TMDB look-ups (only when a key is set).
