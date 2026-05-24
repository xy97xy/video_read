# Google Photos Organizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `photos.py` — a CLI tool that reads a Google Takeout export, clusters photos into trips + monthly catch-alls, lets the user interactively confirm trips, then copies photos into an organised local folder structure ready for drag-and-drop upload to Google Photos.

**Architecture:** Three files: `photos.py` (CLI entry point), `photos/metadata.py` (EXIF + sidecar extraction), `photos/cluster.py` (trip clustering). State is stored in SQLite (`photos.db`). Clusters are written to `clusters.json` for user review. Photos are copied (never moved) into `organized/` output folders.

**Tech Stack:** Python 3.12+ stdlib (sqlite3, argparse, shutil, json), Pillow (EXIF), piexif (GPS parsing), geopy (Nominatim reverse geocoding), tqdm (progress bars). No GPU required.

---

## File Map

| File | Responsibility |
|---|---|
| `photos.py` | CLI: argparse + 4 subcommand handlers (scan, cluster, review, organize) |
| `photos/__init__.py` | Empty package marker |
| `photos/metadata.py` | `find_media_files()`, `extract_metadata()`, `reverse_geocode()` |
| `photos/cluster.py` | `haversine_km()`, `time_gap_split()`, `location_split()`, `detect_home()`, `is_home_cluster()`, `build_clusters()` |
| `tests/test_metadata.py` | Unit tests for sidecar parsing, EXIF fallback, mtime fallback |
| `tests/test_cluster.py` | Unit tests for clustering algorithm |

---

## Task 1: Project scaffold + dependencies

**Files:**
- Create: `photos/__init__.py`
- Create: `photos.py`

- [ ] **Step 1: Install dependencies**

```bash
cd /home/xiaoyu/git/video_read
source venv/bin/activate
pip install Pillow piexif geopy tqdm
```

Expected: packages install without error.

- [ ] **Step 2: Create package marker**

```bash
mkdir -p photos
touch photos/__init__.py
```

- [ ] **Step 3: Create `photos.py` skeleton**

```python
#!/usr/bin/env python3
import argparse
import sys


def cmd_scan(args):
    print("scan: not implemented yet")


def cmd_cluster(args):
    print("cluster: not implemented yet")


def cmd_review(args):
    print("review: not implemented yet")


def cmd_organize(args):
    print("organize: not implemented yet")


def main():
    p = argparse.ArgumentParser(
        prog="photos.py",
        description="Google Photos Takeout organizer",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    s = sub.add_parser("scan", help="Scan Takeout export → photos.db")
    s.add_argument("--takeout-dir", required=True, metavar="DIR")
    s.add_argument("--db", default="photos.db", metavar="DB")

    c = sub.add_parser("cluster", help="Cluster photos into trips → clusters.json")
    c.add_argument("--db", default="photos.db", metavar="DB")
    c.add_argument("--clusters", default="clusters.json", metavar="FILE")
    c.add_argument("--gap-days", type=int, default=3, metavar="N")
    c.add_argument("--radius-km", type=float, default=50.0, metavar="KM")

    r = sub.add_parser("review", help="Interactively review trip clusters")
    r.add_argument("--clusters", default="clusters.json", metavar="FILE")

    o = sub.add_parser("organize", help="Copy photos into organised folders")
    o.add_argument("--output-dir", required=True, metavar="DIR")
    o.add_argument("--db", default="photos.db", metavar="DB")
    o.add_argument("--clusters", default="clusters.json", metavar="FILE")

    args = p.parse_args()
    {"scan": cmd_scan, "cluster": cmd_cluster,
     "review": cmd_review, "organize": cmd_organize}[args.subcommand](args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Verify `--help` works**

```bash
python photos.py --help
python photos.py scan --help
python photos.py cluster --help
```

Expected: all print usage without error.

- [ ] **Step 5: Commit**

```bash
git add photos/__init__.py photos.py
git commit -m "feat: photos.py scaffold — argparse skeleton with 4 subcommand stubs"
```

---

## Task 2: Metadata extraction (`photos/metadata.py`)

**Files:**
- Create: `photos/metadata.py`
- Create: `tests/test_metadata.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_metadata.py`:

```python
import json, sys, os, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from photos.metadata import find_media_files, extract_metadata

def test_find_media_files(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.JPG").write_bytes(b"x")
    (tmp_path / "c.mp4").write_bytes(b"x")
    (tmp_path / "d.txt").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "e.heic").write_bytes(b"x")

    files = find_media_files(str(tmp_path))
    names = {f.name.lower() for f in files}
    assert names == {"a.jpg", "b.jpg", "c.mp4", "e.heic"}
    assert len(files) == 4


def test_extract_metadata_sidecar(tmp_path):
    img = tmp_path / "IMG_0001.jpg"
    img.write_bytes(b"fake")
    sidecar = tmp_path / "IMG_0001.jpg.json"
    sidecar.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1720000000"},
        "geoData": {"latitude": 64.1355, "longitude": -21.8954, "altitude": 0.0}
    }))

    taken_at, lat, lon = extract_metadata(img)
    assert taken_at == 1720000000
    assert abs(lat - 64.1355) < 0.001
    assert abs(lon - (-21.8954)) < 0.001


def test_extract_metadata_sidecar_zero_gps_ignored(tmp_path):
    img = tmp_path / "IMG_0002.jpg"
    img.write_bytes(b"fake")
    sidecar = tmp_path / "IMG_0002.jpg.json"
    sidecar.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1720000001"},
        "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    }))

    taken_at, lat, lon = extract_metadata(img)
    assert taken_at == 1720000001
    assert lat is None
    assert lon is None


def test_extract_metadata_mtime_fallback(tmp_path):
    img = tmp_path / "IMG_0003.jpg"
    img.write_bytes(b"fake")
    # no sidecar, no valid EXIF

    taken_at, lat, lon = extract_metadata(img)
    assert taken_at is not None
    assert abs(taken_at - int(img.stat().st_mtime)) <= 2
    assert lat is None
    assert lon is None


def test_extract_metadata_no_sidecar_stem_fallback(tmp_path):
    """Google sometimes writes sidecar as IMG_0001.json not IMG_0001.jpg.json"""
    img = tmp_path / "IMG_0004.jpg"
    img.write_bytes(b"fake")
    sidecar = tmp_path / "IMG_0004.json"
    sidecar.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1720000099"},
        "geoData": {"latitude": 10.0, "longitude": 20.0, "altitude": 0.0}
    }))

    taken_at, lat, lon = extract_metadata(img)
    assert taken_at == 1720000099
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source venv/bin/activate
python -m pytest tests/test_metadata.py -v 2>&1 | head -20
```

Expected: FAIL with `ModuleNotFoundError` or `ImportError`.

- [ ] **Step 3: Implement `photos/metadata.py`**

```python
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image
    import piexif
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic'}
VIDEO_EXTENSIONS = {'.mp4', '.mov'}
ALL_EXTENSIONS   = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def find_media_files(root: str) -> list[Path]:
    return [
        p for p in Path(root).rglob('*')
        if p.suffix.lower() in ALL_EXTENSIONS and p.is_file()
    ]


def _read_sidecar(path: Path) -> dict | None:
    for candidate in (
        path.with_suffix(path.suffix + '.json'),   # IMG.jpg.json
        path.with_name(path.stem + '.json'),        # IMG.json
    ):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding='utf-8'))
            except Exception:
                pass
    return None


def _dms_to_decimal(dms: tuple, ref: str) -> float:
    d = dms[0][0] / dms[0][1]
    m = dms[1][0] / dms[1][1]
    s = dms[2][0] / dms[2][1]
    val = d + m / 60 + s / 3600
    return -val if ref in ('S', 'W') else val


def _exif_metadata(path: Path) -> tuple[int | None, float | None, float | None]:
    if not _PIL_AVAILABLE or path.suffix.lower() not in IMAGE_EXTENSIONS - {'.heic'}:
        return None, None, None
    try:
        img = Image.open(path)
        exif = img.getexif()
        if not exif:
            return None, None, None

        taken_at = None
        dt_str = exif.get(36867) or exif.get(306)   # DateTimeOriginal or DateTime
        if dt_str:
            try:
                taken_at = int(datetime.strptime(dt_str, '%Y:%m:%d %H:%M:%S').timestamp())
            except ValueError:
                pass

        lat, lon = None, None
        gps_ifd = exif.get_ifd(34853)
        if gps_ifd and 2 in gps_ifd and 4 in gps_ifd:
            try:
                lat = _dms_to_decimal(gps_ifd[2], gps_ifd[1])
                lon = _dms_to_decimal(gps_ifd[4], gps_ifd[3])
            except Exception:
                pass

        return taken_at, lat, lon
    except Exception:
        return None, None, None


def extract_metadata(path: Path) -> tuple[int | None, float | None, float | None]:
    """Return (taken_at_unix, lat, lon). Falls back: sidecar → EXIF → mtime."""
    sidecar = _read_sidecar(path)
    if sidecar:
        taken_at = None
        try:
            taken_at = int(sidecar['photoTakenTime']['timestamp'])
        except (KeyError, ValueError, TypeError):
            pass

        lat, lon = None, None
        try:
            lat = float(sidecar['geoData']['latitude'])
            lon = float(sidecar['geoData']['longitude'])
            if lat == 0.0 and lon == 0.0:
                lat, lon = None, None
        except (KeyError, ValueError, TypeError):
            pass

        if taken_at:
            return taken_at, lat, lon

    taken_at, lat, lon = _exif_metadata(path)
    if taken_at:
        return taken_at, lat, lon

    return int(path.stat().st_mtime), None, None


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Return 'City, Country' string or None. Rate-limited to 1 req/s by geopy."""
    try:
        from geopy.geocoders import Nominatim
        import time
        geolocator = Nominatim(user_agent="photos_organizer/1.0")
        time.sleep(1.1)
        location = geolocator.reverse(f"{lat}, {lon}", language='en', exactly_one=True)
        if location:
            addr = location.raw.get('address', {})
            city = addr.get('city') or addr.get('town') or addr.get('village') or addr.get('county', '')
            country = addr.get('country', '')
            parts = [p for p in (city, country) if p]
            return ', '.join(parts) if parts else None
    except Exception:
        pass
    return None
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_metadata.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add photos/metadata.py tests/test_metadata.py
git commit -m "feat: photos/metadata.py — sidecar + EXIF + mtime extraction"
```

---

## Task 3: `scan` subcommand

**Files:**
- Modify: `photos.py` (replace `cmd_scan` stub)

- [ ] **Step 1: Write failing integration test**

Append to `tests/test_metadata.py`:

```python
import sqlite3, subprocess

def test_scan_creates_db(tmp_path):
    takeout = tmp_path / "Takeout" / "Google Photos" / "Photos from 2024"
    takeout.mkdir(parents=True)
    img = takeout / "IMG_0001.jpg"
    img.write_bytes(b"fake")
    sidecar = takeout / "IMG_0001.jpg.json"
    sidecar.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1720000000"},
        "geoData": {"latitude": 64.1, "longitude": -21.9, "altitude": 0.0}
    }))

    db_path = str(tmp_path / "photos.db")
    result = subprocess.run(
        [sys.executable, "photos.py", "scan",
         "--takeout-dir", str(tmp_path / "Takeout"),
         "--db", db_path],
        capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(__file__))
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT path, taken_at, lat, lon FROM photos").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] == 1720000000
    assert abs(rows[0][2] - 64.1) < 0.01
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_metadata.py::test_scan_creates_db -v
```

Expected: FAIL (`scan: not implemented yet`).

- [ ] **Step 3: Replace `cmd_scan` in `photos.py`**

Add these imports at the top of `photos.py`:

```python
import json
import sqlite3
import time
from pathlib import Path
from tqdm import tqdm
from photos.metadata import find_media_files, extract_metadata, reverse_geocode
```

Replace the `cmd_scan` function:

```python
def _init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id         INTEGER PRIMARY KEY,
            path       TEXT UNIQUE,
            taken_at   INTEGER,
            lat        REAL,
            lon        REAL,
            place      TEXT,
            cluster_id INTEGER
        )
    """)
    conn.commit()
    return conn


def cmd_scan(args):
    conn = _init_db(args.db)
    files = find_media_files(args.takeout_dir)
    print(f"Found {len(files)} media files — scanning...")

    geocode_cache: dict[tuple, str | None] = {}
    n_gps = 0
    n_dated = 0

    for path in tqdm(files, unit="photo"):
        taken_at, lat, lon = extract_metadata(path)
        place = None

        if lat is not None:
            n_gps += 1
            cell = (round(lat / 0.1) * 0.1, round(lon / 0.1) * 0.1)
            if cell not in geocode_cache:
                geocode_cache[cell] = reverse_geocode(lat, lon)
            place = geocode_cache[cell]

        if taken_at:
            n_dated += 1

        conn.execute(
            "INSERT OR IGNORE INTO photos (path, taken_at, lat, lon, place) VALUES (?,?,?,?,?)",
            (str(path), taken_at, lat, lon, place)
        )

    conn.commit()
    conn.close()
    total = len(files)
    print(f"\n✓ Scanned {total} photos")
    print(f"  {n_dated}/{total} have date info")
    print(f"  {n_gps}/{total}  have GPS")
    print(f"  Saved to {args.db}")
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_metadata.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add photos.py tests/test_metadata.py
git commit -m "feat: photos.py scan — walk Takeout, extract metadata, store in SQLite"
```

---

## Task 4: Clustering algorithm (`photos/cluster.py`)

**Files:**
- Create: `photos/cluster.py`
- Create: `tests/test_cluster.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cluster.py`:

```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from photos.cluster import (
    haversine_km, time_gap_split, location_spread_km,
    detect_home, is_home_cluster, build_clusters
)

def _p(id, taken_at, lat=None, lon=None, place=None):
    return {"id": id, "taken_at": taken_at, "lat": lat, "lon": lon, "place": place}

DAY = 86400

def test_haversine_london_paris():
    d = haversine_km(51.5, -0.1, 48.8, 2.3)
    assert 335 < d < 350

def test_haversine_same_point():
    assert haversine_km(10.0, 20.0, 10.0, 20.0) == 0.0

def test_time_gap_split_one_group():
    photos = [_p(1, 1000000), _p(2, 1000100)]
    groups = time_gap_split(photos, gap_seconds=3 * DAY)
    assert len(groups) == 1
    assert len(groups[0]) == 2

def test_time_gap_split_two_groups():
    photos = [_p(1, 1000000), _p(2, 1000000 + 4 * DAY)]
    groups = time_gap_split(photos, gap_seconds=3 * DAY)
    assert len(groups) == 2

def test_location_spread_no_gps():
    photos = [_p(1, 1000), _p(2, 2000)]
    assert location_spread_km(photos) == 0.0

def test_location_spread_nearby():
    photos = [
        _p(1, 1000, lat=37.7, lon=-122.4),
        _p(2, 2000, lat=37.8, lon=-122.5),
    ]
    assert location_spread_km(photos) < 20

def test_detect_home_no_gps():
    assert detect_home([_p(1, 1000)]) is None

def test_detect_home_most_common():
    photos = [
        _p(1, 1000, lat=37.7, lon=-122.4),
        _p(2, 2000, lat=37.7, lon=-122.4),
        _p(3, 3000, lat=37.7, lon=-122.4),
        _p(4, 4000, lat=64.1, lon=-21.9),
    ]
    home = detect_home(photos)
    assert home is not None
    assert abs(home[0] - 37.5) < 1.0  # SF cell

def test_is_home_cluster_near():
    home = (37.7, -122.4)
    photos = [_p(1, 1000, lat=37.7, lon=-122.4), _p(2, 2000, lat=37.8, lon=-122.5)]
    assert is_home_cluster(photos, home) is True

def test_is_home_cluster_away():
    home = (37.7, -122.4)
    photos = [_p(1, 1000, lat=64.1, lon=-21.9)]
    assert is_home_cluster(photos, home) is False

def test_is_home_cluster_no_gps_assumes_home():
    assert is_home_cluster([_p(1, 1000)], home=(37.7, -122.4)) is True

def test_build_clusters_empty():
    assert build_clusters([]) == []

def test_build_clusters_monthly_catchall():
    base = 1704067200  # 2024-01-01
    photos = [
        _p(1, base,         lat=37.7, lon=-122.4, place="San Francisco, US"),
        _p(2, base + DAY,   lat=37.7, lon=-122.4, place="San Francisco, US"),
    ]
    clusters = build_clusters(photos, gap_days=3, radius_km=50)
    assert len(clusters) >= 1
    c = clusters[0]
    assert c["is_trip"] is False
    assert c["confirmed"] is True
    assert c["name"].startswith("2024-01")

def test_build_clusters_trip_detected():
    base = 1704067200  # 2024-01-01
    photos = []
    for i in range(5):
        photos.append(_p(i+1, base + i*DAY, lat=37.7, lon=-122.4, place="San Francisco, US"))
    for i in range(5):
        photos.append(_p(i+6, base + (15+i)*DAY, lat=64.1, lon=-21.9, place="Reykjavik, Iceland"))

    clusters = build_clusters(photos, gap_days=3, radius_km=50)
    trips = [c for c in clusters if c["is_trip"]]
    assert len(trips) == 1
    assert "Iceland" in (trips[0]["place"] or "")
    assert trips[0]["confirmed"] is False

def test_build_clusters_undated_goes_to_no_date():
    photos = [_p(1, None)]
    clusters = build_clusters(photos)
    assert any(c["name"] == "no-date" for c in clusters)

def test_build_clusters_photo_ids_correct():
    base = 1704067200
    photos = [_p(1, base), _p(2, base + DAY)]
    clusters = build_clusters(photos)
    assert clusters[0]["photo_ids"] == [1, 2]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_cluster.py -v 2>&1 | head -20
```

Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `photos/cluster.py`**

```python
from __future__ import annotations
import math
from collections import Counter
from datetime import datetime, timezone


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(1.0, a)))


def time_gap_split(photos: list[dict], gap_seconds: int) -> list[list[dict]]:
    if not photos:
        return []
    groups = [[photos[0]]]
    for p in photos[1:]:
        if p['taken_at'] - groups[-1][-1]['taken_at'] > gap_seconds:
            groups.append([p])
        else:
            groups[-1].append(p)
    return groups


def location_spread_km(photos: list[dict]) -> float:
    gps = [(p['lat'], p['lon']) for p in photos if p['lat'] is not None]
    if len(gps) < 2:
        return 0.0
    max_dist = 0.0
    for i in range(len(gps)):
        for j in range(i + 1, len(gps)):
            max_dist = max(max_dist, haversine_km(gps[i][0], gps[i][1], gps[j][0], gps[j][1]))
    return max_dist


def location_split(group: list[dict], radius_km: float) -> list[list[dict]]:
    if location_spread_km(group) <= radius_km:
        return [group]
    gps_idx = [(i, p) for i, p in enumerate(group) if p['lat'] is not None]
    if len(gps_idx) < 2:
        return [group]
    max_jump, split_at = 0.0, len(group) // 2
    for k in range(len(gps_idx) - 1):
        i, pi = gps_idx[k]
        j, pj = gps_idx[k + 1]
        d = haversine_km(pi['lat'], pi['lon'], pj['lat'], pj['lon'])
        if d > max_jump:
            max_jump, split_at = d, j
    left, right = group[:split_at], group[split_at:]
    if not left or not right:
        return [group]
    return location_split(left, radius_km) + location_split(right, radius_km)


def detect_home(photos: list[dict], cell_size: float = 0.5) -> tuple[float, float] | None:
    cells: Counter = Counter()
    for p in photos:
        if p['lat'] is not None:
            cell = (round(p['lat'] / cell_size) * cell_size,
                    round(p['lon'] / cell_size) * cell_size)
            cells[cell] += 1
    return cells.most_common(1)[0][0] if cells else None


def is_home_cluster(group: list[dict], home: tuple[float, float] | None,
                    radius_km: float = 50.0, threshold: float = 0.8) -> bool:
    if home is None:
        return False
    gps = [p for p in group if p['lat'] is not None]
    if not gps:
        return True
    near = sum(1 for p in gps if haversine_km(p['lat'], p['lon'], home[0], home[1]) <= radius_km)
    return near / len(gps) >= threshold


def _fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')


def _fmt_month(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m')


def _dominant_place(group: list[dict]) -> str | None:
    places: Counter = Counter(p['place'] for p in group if p['place'])
    return places.most_common(1)[0][0] if places else None


def build_clusters(photos: list[dict], gap_days: int = 3, radius_km: float = 50.0) -> list[dict]:
    if not photos:
        return []
    gap_sec = gap_days * 86400
    dated   = sorted([p for p in photos if p['taken_at'] is not None], key=lambda p: p['taken_at'])
    undated = [p for p in photos if p['taken_at'] is None]

    time_groups = time_gap_split(dated, gap_sec)
    all_groups: list[list[dict]] = []
    for g in time_groups:
        all_groups.extend(location_split(g, radius_km))

    home = detect_home(dated)
    clusters = []
    for cid, g in enumerate(all_groups, start=1):
        is_trip = not is_home_cluster(g, home)
        start_ts, end_ts = g[0]['taken_at'], g[-1]['taken_at']
        place = _dominant_place(g)
        if is_trip:
            name = f"{_fmt_date(start_ts)}–{_fmt_date(end_ts)}"
            if place:
                name += f" {place}"
        else:
            name = _fmt_month(start_ts)
        clusters.append({
            "id":          cid,
            "name":        name,
            "is_trip":     is_trip,
            "confirmed":   not is_trip,
            "photo_count": len(g),
            "photo_ids":   [p['id'] for p in g],
            "start":       _fmt_date(start_ts),
            "end":         _fmt_date(end_ts),
            "place":       place,
        })

    if undated:
        clusters.append({
            "id":          len(clusters) + 1,
            "name":        "no-date",
            "is_trip":     False,
            "confirmed":   True,
            "photo_count": len(undated),
            "photo_ids":   [p['id'] for p in undated],
            "start":       None,
            "end":         None,
            "place":       None,
        })
    return clusters
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_cluster.py -v
```

Expected: all 15 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add photos/cluster.py tests/test_cluster.py
git commit -m "feat: photos/cluster.py — haversine, time-gap, location-split, trip detection"
```

---

## Task 5: `cluster` subcommand

**Files:**
- Modify: `photos.py` (replace `cmd_cluster` stub)

- [ ] **Step 1: Write failing integration test**

Create `tests/test_cluster_cmd.py`:

```python
import json, sqlite3, subprocess, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

PROJ = os.path.dirname(os.path.dirname(__file__))
DAY = 86400

def _make_db(db_path, rows):
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE photos (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE,
        taken_at INTEGER, lat REAL, lon REAL, place TEXT, cluster_id INTEGER
    )""")
    for r in rows:
        conn.execute("INSERT INTO photos (path,taken_at,lat,lon,place) VALUES (?,?,?,?,?)", r)
    conn.commit()
    conn.close()

def test_cluster_writes_json(tmp_path):
    db = str(tmp_path / "photos.db")
    base = 1704067200  # 2024-01-01
    _make_db(db, [
        (f"/fake/{i}.jpg", base + i * DAY, 37.7, -122.4, "San Francisco, US")
        for i in range(5)
    ] + [
        (f"/fake/{i+10}.jpg", base + (15+i)*DAY, 64.1, -21.9, "Reykjavik, Iceland")
        for i in range(5)
    ])
    clusters_path = str(tmp_path / "clusters.json")
    result = subprocess.run(
        [sys.executable, "photos.py", "cluster",
         "--db", db, "--clusters", clusters_path,
         "--gap-days", "3", "--radius-km", "50"],
        capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0, result.stderr
    clusters = json.loads(open(clusters_path).read())
    assert len(clusters) >= 1
    trips = [c for c in clusters if c["is_trip"]]
    assert len(trips) == 1
    assert clusters_path  # file exists
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_cluster_cmd.py -v
```

Expected: FAIL (`cluster: not implemented yet`).

- [ ] **Step 3: Replace `cmd_cluster` in `photos.py`**

Add to imports at top:

```python
from photos.cluster import build_clusters
```

Replace `cmd_cluster`:

```python
def cmd_cluster(args):
    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT id, path, taken_at, lat, lon, place FROM photos"
    ).fetchall()
    conn.close()

    photos = [
        {"id": r[0], "path": r[1], "taken_at": r[2],
         "lat": r[3], "lon": r[4], "place": r[5]}
        for r in rows
    ]

    clusters = build_clusters(photos, gap_days=args.gap_days, radius_km=args.radius_km)

    # Write cluster_id back to DB
    conn = sqlite3.connect(args.db)
    for c in clusters:
        for pid in c["photo_ids"]:
            conn.execute("UPDATE photos SET cluster_id=? WHERE id=?", (c["id"], pid))
    conn.commit()
    conn.close()

    Path(args.clusters).write_text(json.dumps(clusters, indent=2, ensure_ascii=False))

    trips    = sum(1 for c in clusters if c["is_trip"])
    catchall = sum(1 for c in clusters if not c["is_trip"])
    total    = sum(c["photo_count"] for c in clusters)
    print(f"✓ {len(clusters)} clusters from {total} photos")
    print(f"  {trips} trip(s), {catchall} monthly/catch-all")
    print(f"  Saved to {args.clusters}")
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_cluster_cmd.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add photos.py tests/test_cluster_cmd.py
git commit -m "feat: photos.py cluster — reads DB, builds trip clusters, writes clusters.json"
```

---

## Task 6: `review` subcommand

**Files:**
- Modify: `photos.py` (replace `cmd_review` stub)

No automated test for interactive UI — we test with mocked stdin.

- [ ] **Step 1: Write test with mocked input**

Create `tests/test_review.py`:

```python
import json, subprocess, sys, os
PROJ = os.path.dirname(os.path.dirname(__file__))

def _write_clusters(path, clusters):
    open(path, 'w').write(json.dumps(clusters, indent=2))

def test_review_confirm(tmp_path):
    clusters_path = str(tmp_path / "clusters.json")
    _write_clusters(clusters_path, [{
        "id": 1, "name": "2024-07-01–2024-07-07 Reykjavik, Iceland",
        "is_trip": True, "confirmed": False,
        "photo_count": 42, "photo_ids": [1,2,3],
        "start": "2024-07-01", "end": "2024-07-07", "place": "Reykjavik, Iceland"
    }])
    result = subprocess.run(
        [sys.executable, "photos.py", "review", "--clusters", clusters_path],
        input="c\n", capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(open(clusters_path).read())
    assert data[0]["confirmed"] is True

def test_review_rename(tmp_path):
    clusters_path = str(tmp_path / "clusters.json")
    _write_clusters(clusters_path, [{
        "id": 1, "name": "2024-07-01–2024-07-07 Reykjavik, Iceland",
        "is_trip": True, "confirmed": False,
        "photo_count": 10, "photo_ids": [1],
        "start": "2024-07-01", "end": "2024-07-07", "place": "Reykjavik, Iceland"
    }])
    result = subprocess.run(
        [sys.executable, "photos.py", "review", "--clusters", clusters_path],
        input="r\nIceland Trip 2024\n",
        capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(open(clusters_path).read())
    assert data[0]["name"] == "Iceland Trip 2024"
    assert data[0]["confirmed"] is True

def test_review_discard(tmp_path):
    clusters_path = str(tmp_path / "clusters.json")
    _write_clusters(clusters_path, [{
        "id": 1, "name": "2024-07-01–2024-07-03 Quick trip",
        "is_trip": True, "confirmed": False,
        "photo_count": 5, "photo_ids": [1],
        "start": "2024-07-01", "end": "2024-07-03", "place": None
    }])
    result = subprocess.run(
        [sys.executable, "photos.py", "review", "--clusters", clusters_path],
        input="d\n", capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(open(clusters_path).read())
    assert data[0]["is_trip"] is False
    assert data[0]["confirmed"] is True

def test_review_skips_already_confirmed(tmp_path):
    clusters_path = str(tmp_path / "clusters.json")
    _write_clusters(clusters_path, [{
        "id": 1, "name": "2024-03",
        "is_trip": False, "confirmed": True,
        "photo_count": 20, "photo_ids": [1],
        "start": "2024-03-01", "end": "2024-03-31", "place": None
    }])
    result = subprocess.run(
        [sys.executable, "photos.py", "review", "--clusters", clusters_path],
        input="", capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0
    assert "nothing to review" in result.stdout.lower() or "0 trip" in result.stdout.lower()
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_review.py -v 2>&1 | head -20
```

Expected: FAIL (`review: not implemented yet`).

- [ ] **Step 3: Replace `cmd_review` in `photos.py`**

```python
def cmd_review(args):
    clusters = json.loads(Path(args.clusters).read_text())
    pending = [c for c in clusters if c.get("is_trip") and not c.get("confirmed")]

    if not pending:
        print(f"Nothing to review — 0 unconfirmed trips in {args.clusters}")
        return

    print(f"{len(pending)} trip cluster(s) to review.\n")
    for c in pending:
        print(f"Cluster: {c['name']}  ({c['photo_count']} photos,  {c['start']} → {c['end']})")
        print("Actions: [c]onfirm  [r]ename  [d]iscard  [s]kip  [?]help")

        while True:
            action = input("> ").strip().lower()
            if action == "?":
                print("  c = confirm as trip")
                print("  r = rename (you choose the album name)")
                print("  d = discard (moves photos to monthly catch-all)")
                print("  s = skip for now (leave unconfirmed)")
            elif action == "c":
                c["confirmed"] = True
                print(f"  ✓ Confirmed: {c['name']}")
                break
            elif action == "r":
                new_name = input("  New name: ").strip()
                if new_name:
                    c["name"] = new_name
                c["confirmed"] = True
                print(f"  ✓ Renamed to: {c['name']}")
                break
            elif action == "d":
                c["is_trip"] = False
                c["confirmed"] = True
                print(f"  ✗ Discarded — photos will go to monthly catch-all")
                break
            elif action == "s":
                print(f"  → Skipped")
                break
            else:
                print("  Unknown action. Type ? for help.")
        print()

    Path(args.clusters).write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
    confirmed = sum(1 for c in clusters if c.get("confirmed") and c.get("is_trip"))
    print(f"✓ Saved. {confirmed} confirmed trip(s).")
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_review.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add photos.py tests/test_review.py
git commit -m "feat: photos.py review — interactive confirm/rename/discard for trip clusters"
```

---

## Task 7: `organize` subcommand

**Files:**
- Modify: `photos.py` (replace `cmd_organize` stub)
- Create: `tests/test_organize.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_organize.py`:

```python
import json, sqlite3, subprocess, sys, os, shutil
from pathlib import Path
PROJ = os.path.dirname(os.path.dirname(__file__))

def _setup(tmp_path):
    # Create fake photo files
    src = tmp_path / "takeout"
    src.mkdir()
    photos = []
    for i in range(4):
        p = src / f"IMG_{i:04d}.jpg"
        p.write_bytes(f"photo{i}".encode())
        photos.append(str(p))

    # Create DB
    db = str(tmp_path / "photos.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE photos (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE,
        taken_at INTEGER, lat REAL, lon REAL, place TEXT, cluster_id INTEGER
    )""")
    for i, p in enumerate(photos):
        conn.execute("INSERT INTO photos VALUES (?,?,?,?,?,?,?)",
                     (i+1, p, 1704067200 + i*86400, None, None, None, i//2 + 1))
    conn.commit()
    conn.close()

    # Create clusters.json
    clusters = [
        {"id": 1, "name": "Iceland Trip 2024", "is_trip": True, "confirmed": True,
         "photo_count": 2, "photo_ids": [1, 2], "start": "2024-01-01", "end": "2024-01-02", "place": None},
        {"id": 2, "name": "2024-01", "is_trip": False, "confirmed": True,
         "photo_count": 2, "photo_ids": [3, 4], "start": "2024-01-03", "end": "2024-01-04", "place": None},
    ]
    clusters_path = str(tmp_path / "clusters.json")
    open(clusters_path, 'w').write(json.dumps(clusters))
    return db, clusters_path

def test_organize_creates_folders(tmp_path):
    db, clusters_path = _setup(tmp_path)
    out = str(tmp_path / "organized")
    result = subprocess.run(
        [sys.executable, "photos.py", "organize",
         "--output-dir", out, "--db", db, "--clusters", clusters_path],
        capture_output=True, text=True, cwd=PROJ
    )
    assert result.returncode == 0, result.stderr
    out_path = Path(out)
    assert (out_path / "Iceland-Trip-2024").is_dir()
    assert (out_path / "2024-01").is_dir()
    assert len(list((out_path / "Iceland-Trip-2024").iterdir())) == 2
    assert len(list((out_path / "2024-01").iterdir())) == 2

def test_organize_does_not_move_originals(tmp_path):
    db, clusters_path = _setup(tmp_path)
    src_files = list((tmp_path / "takeout").iterdir())
    out = str(tmp_path / "organized")
    subprocess.run(
        [sys.executable, "photos.py", "organize",
         "--output-dir", out, "--db", db, "--clusters", clusters_path],
        capture_output=True, text=True, cwd=PROJ
    )
    # All original files still exist
    for f in src_files:
        assert f.exists(), f"Original file was moved: {f}"

def test_organize_collision_rename(tmp_path):
    db, clusters_path = _setup(tmp_path)
    out = tmp_path / "organized"
    out.mkdir()
    trip_dir = out / "Iceland-Trip-2024"
    trip_dir.mkdir()
    # Pre-place a file with the same name as one of the photos
    (trip_dir / "IMG_0000.jpg").write_bytes(b"existing")

    subprocess.run(
        [sys.executable, "photos.py", "organize",
         "--output-dir", str(out), "--db", db, "--clusters", str(tmp_path / "clusters.json")],
        capture_output=True, text=True, cwd=PROJ
    )
    files = list(trip_dir.iterdir())
    names = {f.name for f in files}
    assert "IMG_0000.jpg" in names
    assert any("IMG_0000_2.jpg" in n for n in names)
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_organize.py -v 2>&1 | head -20
```

Expected: FAIL (`organize: not implemented yet`).

- [ ] **Step 3: Replace `cmd_organize` in `photos.py`**

Add to imports at top:

```python
import re
import shutil
```

Replace `cmd_organize`:

```python
def _sanitize(name: str) -> str:
    name = name.replace('–', '-').replace('—', '-')
    name = re.sub(r'[^\w\-]', '-', name)
    name = re.sub(r'-+', '-', name)
    return name.strip('-')


def _dest_path(folder: Path, filename: str) -> Path:
    dest = folder / filename
    if not dest.exists():
        return dest
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 2
    while True:
        candidate = folder / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def cmd_organize(args):
    clusters = json.loads(Path(args.clusters).read_text())
    conn = sqlite3.connect(args.db)
    id_to_path = {r[0]: r[1] for r in conn.execute("SELECT id, path FROM photos")}
    conn.close()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    n_copied = n_skipped = 0
    for c in clusters:
        if not c.get("confirmed"):
            continue
        folder_name = _sanitize(c["name"])
        folder = out / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        for pid in c["photo_ids"]:
            src = id_to_path.get(pid)
            if not src or not Path(src).exists():
                n_skipped += 1
                continue
            dest = _dest_path(folder, Path(src).name)
            shutil.copy2(src, dest)
            n_copied += 1

    confirmed = sum(1 for c in clusters if c.get("confirmed"))
    print(f"✓ {n_copied} photos copied into {confirmed} folder(s) under {args.output_dir}")
    if n_skipped:
        print(f"  {n_skipped} skipped (source file not found)")
```

- [ ] **Step 4: Run all tests**

```bash
python -m pytest tests/test_metadata.py tests/test_cluster.py tests/test_cluster_cmd.py tests/test_review.py tests/test_organize.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add photos.py tests/test_organize.py
git commit -m "feat: photos.py organize — copy photos into cluster folders, handle collisions"
```

---

## Self-Review

**Spec coverage:**
- ✅ `scan --takeout-dir` walks Takeout, reads sidecar + EXIF + mtime, stores in SQLite — Task 3
- ✅ Reverse geocoding (Nominatim, cell-based caching) — Task 3
- ✅ `cluster` time-gap + location-split + home detection — Tasks 4 + 5
- ✅ Monthly catch-alls auto-confirmed — Task 4 (`build_clusters`)
- ✅ `review` confirm/rename/discard/skip — Task 6
- ✅ `organize` copies into sanitised folder names, collision handling, no-move guarantee — Task 7
- ✅ `no-date/` folder for undated photos — Task 4 + 7 (`_sanitize("no-date")` = `"no-date"`)
- ✅ Nothing in `pipeline.py`, `bench.py`, `eval/` changed — ✅ separate files only

**No placeholders:** All tasks have complete code. ✅

**Type consistency:**
- `extract_metadata` returns `(int|None, float|None, float|None)` — used in Task 3 exactly
- `build_clusters` takes `list[dict]` with keys `id/taken_at/lat/lon/place` — Task 5 builds exactly that dict from DB rows ✅
- `cluster["photo_ids"]` used in Tasks 5, 6, 7 — defined in Task 4 ✅
- `_sanitize` used in Task 7 on `c["name"]` — `"no-date"` sanitises to `"no-date"` ✅
