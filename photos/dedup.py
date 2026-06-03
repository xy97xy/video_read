from __future__ import annotations
import hashlib
from collections import defaultdict
from pathlib import Path


def hash_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def find_exact_duplicates(photos: list[dict]) -> list[list[dict]]:
    by_size: dict[int, list[dict]] = defaultdict(list)
    for p in photos:
        path = Path(p["path"])
        if not path.exists():
            continue
        by_size[path.stat().st_size].append(p)

    by_hash: dict[str, list[dict]] = defaultdict(list)
    for size_group in by_size.values():
        if len(size_group) < 2:
            continue
        for p in size_group:
            h = hash_file(Path(p["path"]))
            by_hash[h].append(p)

    return [g for g in by_hash.values() if len(g) >= 2]


_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.webp', '.bmp', '.tiff', '.tif'}


def phash_file(path: Path) -> str | None:
    if path.suffix.lower() not in _IMAGE_EXTS:
        return None
    try:
        import imagehash
        from PIL import Image
        return str(imagehash.phash(Image.open(path)))
    except Exception:
        return None


def find_phash_duplicates(photos: list[dict]) -> list[list[dict]]:
    """Find photos that are visually identical (pHash distance = 0)."""
    by_phash: dict[str, list[dict]] = defaultdict(list)
    for p in photos:
        path = Path(p["path"])
        if not path.exists():
            continue
        h = phash_file(path)
        if h is not None:
            by_phash[h].append(p)
    return [g for g in by_phash.values() if len(g) >= 2]
