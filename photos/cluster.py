from __future__ import annotations
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

_GENERIC_ALBUMS = {'Trash', 'Untitled', 'no-date'}


def _extract_album(path: str) -> str | None:
    """Return the Google Photos album name from a path, or None if generic."""
    parts = Path(path).parts
    try:
        gp_idx = next(i for i, p in enumerate(parts) if p == 'Google Photos')
        album = parts[gp_idx + 1]
        if album.startswith('Photos from') or album in _GENERIC_ALBUMS:
            return None
        return album
    except (StopIteration, IndexError):
        return None


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
        return False  # no GPS = unknown location, treat as potential trip
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

    # --- Pass 1: group by named Google Photos album (hard constraint) ---
    album_groups: dict[str, list[dict]] = defaultdict(list)
    no_album: list[dict] = []
    for p in photos:
        album = _extract_album(p.get('path', ''))
        if album:
            album_groups[album].append(p)
        else:
            no_album.append(p)

    # No-album photos go directly to date-based clustering.
    # Album clusters are pure — only photos actually in that Google Photos album.
    remaining = no_album

    # --- Pass 3: time-gap + location cluster the remaining photos ---
    dated_remaining = sorted([p for p in remaining if p['taken_at'] is not None],
                             key=lambda p: p['taken_at'])
    undated = [p for p in remaining if p['taken_at'] is None]

    home = detect_home(
        sorted([p for p in photos if p['taken_at'] is not None], key=lambda p: p['taken_at'])
    )

    time_groups = time_gap_split(dated_remaining, gap_sec)
    date_groups: list[list[dict]] = []
    for g in time_groups:
        date_groups.extend(location_split(g, radius_km))

    # --- Assemble clusters ---
    clusters = []
    cid = 1

    # Album-based clusters (always trips)
    for album, group in sorted(album_groups.items()):
        dated = [p for p in group if p['taken_at'] is not None]
        if not dated:
            start_s = end_s = None
            name = album
        else:
            start_ts = min(p['taken_at'] for p in dated)
            end_ts   = max(p['taken_at'] for p in dated)
            start_s, end_s = _fmt_date(start_ts), _fmt_date(end_ts)
            name = album  # will be renamed in Phase 5
        place = _dominant_place(group)
        clusters.append({
            "id":          cid,
            "name":        name,
            "album":       album,
            "is_trip":     True,
            "confirmed":   False,
            "photo_count": len(group),
            "photo_ids":   [p['id'] for p in group],
            "start":       start_s,
            "end":         end_s,
            "place":       place,
        })
        cid += 1

    # Date/location clusters
    for g in date_groups:
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
            "album":       None,
            "is_trip":     is_trip,
            "confirmed":   not is_trip,
            "photo_count": len(g),
            "photo_ids":   [p['id'] for p in g],
            "start":       _fmt_date(start_ts),
            "end":         _fmt_date(end_ts),
            "place":       place,
        })
        cid += 1

    if undated:
        clusters.append({
            "id":          cid,
            "name":        "no-date",
            "album":       None,
            "is_trip":     False,
            "confirmed":   True,
            "photo_count": len(undated),
            "photo_ids":   [p['id'] for p in undated],
            "start":       None,
            "end":         None,
            "place":       None,
        })
    return clusters
