# Google Photos Organizer Design

**Date:** 2026-05-21
**Status:** Approved

---

## Problem

A large Google Photos library accumulates photos with no structure: duplicate shots, blurry frames, and no album organisation. The goal is to automatically detect trips, group everyday photos by month, and produce a clean organised folder structure ready for re-upload.

---

## Scope

**Phase 1 (this spec):** Scan a Google Takeout export → cluster into trips + monthly catch-alls → interactive review → copy into organised local folders.

**Phase 2 (future):** Upload organised folders to Google Photos as albums via the API, perceptual-hash deduplication, blur/quality curation.

---

## Input

Google Takeout export: the user requests an export from [takeout.google.com](https://takeout.google.com), downloads the ZIP(s), and extracts them. The result is a directory tree of the form:

```
Google Photos/
  Photos from 2023/
    IMG_1234.jpg
    IMG_1234.jpg.json    ← Google metadata sidecar (title, description, geoData)
  Photos from 2024/
    ...
```

Each `.json` sidecar contains `geoData.latitude` / `geoData.longitude` (more reliable than EXIF for some phones) and `photoTakenTime.timestamp`.

---

## Architecture

```
photos.py                  ← CLI entry point with subcommands
photos/
  metadata.py              ← EXIF + sidecar extraction, reverse geocoding
  cluster.py               ← trip clustering algorithm
photos.db                  ← SQLite state (created by scan, read by cluster/organize)
clusters.json              ← cluster review file (created by cluster, edited by review)
```

All in the existing `video_read` repo. No new top-level directory.

---

## Subcommands

### `photos.py scan --takeout-dir DIR [--db photos.db]`

Walks `DIR` recursively, finds all image/video files (`.jpg`, `.jpeg`, `.heic`, `.png`, `.mp4`, `.mov`). For each file:

1. Reads the Google `.json` sidecar (preferred) for timestamp + GPS
2. Falls back to EXIF (`Pillow` + `piexif`) if no sidecar
3. Falls back to file `mtime` if no EXIF date

Inserts one row per photo into `photos.db`:

```sql
CREATE TABLE photos (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE,
    taken_at  INTEGER,   -- unix timestamp, nullable
    lat       REAL,      -- nullable
    lon       REAL,      -- nullable
    place     TEXT,      -- reverse-geocoded city/country, nullable
    cluster_id INTEGER   -- set by cluster step
);
```

Reverse geocoding: uses `geopy` Nominatim (free, 1 req/s). Only geocodes the first photo per unique 0.1° lat/lon cell to avoid rate limits. Stores `"City, Country"` in `place`.

Prints a summary: total photos scanned, how many have GPS, how many have dates.

---

### `photos.py cluster [--db photos.db] [--gap-days 3] [--radius-km 50]`

Reads all photos with `taken_at` set, sorted by timestamp. Applies a two-pass algorithm:

**Pass 1 — time gaps:** Photos more than `--gap-days` (default 3) apart start a new cluster.

**Pass 2 — location split:** Within each time cluster, if photos span > `--radius-km` (default 50 km) of location spread, split into sub-clusters by location. Uses centroid distance.

**Home detection:** The most frequently occurring location cluster across all photos is tagged as "home". Clusters where >80% of photos are within 50 km of home are marked `is_trip=False`.

**Labelling:**
- Trip clusters: named `"YYYY-MM-DD–YYYY-MM-DD Place"` e.g. `"2024-07-12–2024-07-19 Reykjavik, Iceland"`
- Non-trip clusters: named `"YYYY-MM"` monthly catch-all

Writes `clusters.json`:

```json
[
  {
    "id": 1,
    "name": "2024-07-12–2024-07-19 Reykjavik, Iceland",
    "is_trip": true,
    "confirmed": false,
    "photo_count": 312,
    "start": "2024-07-12",
    "end": "2024-07-19",
    "place": "Reykjavik, Iceland"
  },
  {
    "id": 2,
    "name": "2024-03",
    "is_trip": false,
    "confirmed": true,
    "photo_count": 87,
    "start": "2024-03-01",
    "end": "2024-03-31",
    "place": null
  }
]
```

Monthly catch-alls have `confirmed: true` automatically (no review needed).

---

### `photos.py review [--clusters clusters.json]`

Interactive terminal review of unconfirmed trip clusters (i.e. `is_trip=true, confirmed=false`).

For each trip cluster, prints:
```
Cluster: 2024-07-12–2024-07-19 Reykjavik, Iceland (312 photos)
Actions: [c]onfirm  [r]ename  [s]plit  [m]erge with next  [d]iscard  [?]help
```

- **confirm** — marks `confirmed: true`
- **rename** — prompts for new name, updates `name` field
- **split** — splits cluster at a user-specified date
- **merge** — merges this cluster with the following one
- **discard** — removes the trip label, photos fall into monthly catch-all

Writes updated `clusters.json` when done.

---

### `photos.py organize --output-dir DIR [--db photos.db] [--clusters clusters.json]`

Reads all confirmed clusters. For each cluster, creates a subdirectory under `--output-dir`:

```
organized/
  2024-07-Iceland-Trip/        ← trip (name sanitised for filesystem)
  2024-09-Tokyo-Trip/
  2024-03/                     ← monthly catch-all
  2024-11/
  no-date/                     ← photos with no timestamp
```

**Copies** each photo (never moves) into the corresponding folder. Preserves original filename; appends `_2` if a name collision occurs.

Photos with no `taken_at` go into `no-date/`.
Photos in discarded clusters go into their monthly catch-all.

Prints a summary: N clusters, N photos copied, N skipped (no date).

---

## Dependencies

| Library | Purpose |
|---|---|
| `Pillow` | EXIF reading from JPEG/HEIC |
| `piexif` | EXIF GPS parsing |
| `geopy` | Nominatim reverse geocoding |
| `tqdm` | Scan progress bar |

All installable via `pip`. No GPU required.

---

## File Layout

```
video_read/
  photos.py
  photos/
    __init__.py
    metadata.py
    cluster.py
  tests/
    test_cluster.py
  docs/superpowers/specs/2026-05-21-google-photos-organizer-design.md
```

`photos.db` and `clusters.json` are runtime artifacts, not committed.

---

## What Does NOT Change

- `pipeline.py`, `bench.py`, `eval/` — untouched
- `SKILL.md` — untouched (this is a separate workflow)

---

## Success Criteria

1. `photos.py scan` processes a Takeout export of 10 000+ photos in under 5 minutes
2. `photos.py cluster` groups a 3-year library into recognisable trips with no manual tuning
3. `photos.py review` lets the user confirm/rename all trips in one session
4. `photos.py organize` produces a folder structure that mirrors the confirmed clusters exactly
5. No original photos are modified or deleted at any point
