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


def find_burst_groups(photos: list[dict], window_seconds: int = 3) -> list[list[dict]]:
    dated = sorted(
        [p for p in photos if p.get("taken_at") is not None],
        key=lambda p: p["taken_at"],
    )
    if not dated:
        return []

    groups: list[list[dict]] = []
    current = [dated[0]]
    for p in dated[1:]:
        if p["taken_at"] - current[-1]["taken_at"] <= window_seconds:
            current.append(p)
        else:
            if len(current) >= 2:
                groups.append(current)
            current = [p]
    if len(current) >= 2:
        groups.append(current)
    return groups
