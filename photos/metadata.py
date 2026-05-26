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
        path.parent / (path.name + '.supplemental-metadata.json'),  # IMG.jpg.supplemental-metadata.json (Google Takeout new format)
        path.with_suffix(path.suffix + '.json'),                     # IMG.jpg.json (older Takeout format)
        path.with_name(path.stem + '.json'),                         # IMG.json (stem-only fallback)
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
        with Image.open(path) as img:
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

        if taken_at is not None:
            return taken_at, lat, lon

    taken_at, lat, lon = _exif_metadata(path)
    if taken_at is not None:
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
