#!/usr/bin/env python3
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from tqdm import tqdm

from photos.metadata import find_media_files, extract_metadata, reverse_geocode
from photos.cluster import build_clusters


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

    try:
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
    finally:
        conn.close()

    total = len(files)
    print(f"\n✓ Scanned {total} photos")
    print(f"  {n_dated}/{total} have date info")
    print(f"  {n_gps}/{total}  have GPS")
    print(f"  Saved to {args.db}")


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

    conn = sqlite3.connect(args.db)
    try:
        for c in clusters:
            for pid in c["photo_ids"]:
                conn.execute("UPDATE photos SET cluster_id=? WHERE id=?", (c["id"], pid))
        conn.commit()
    finally:
        conn.close()

    Path(args.clusters).write_text(json.dumps(clusters, indent=2, ensure_ascii=False))

    trips    = sum(1 for c in clusters if c["is_trip"])
    catchall = sum(1 for c in clusters if not c["is_trip"])
    total    = sum(c["photo_count"] for c in clusters)
    print(f"✓ {len(clusters)} clusters from {total} photos")
    print(f"  {trips} trip(s), {catchall} monthly/catch-all")
    print(f"  Saved to {args.clusters}")


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
            try:
                action = input("> ").strip().lower()
            except EOFError:
                print("\nAborted.")
                Path(args.clusters).write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
                return
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
                try:
                    new_name = input("  New name: ").strip()
                except EOFError:
                    print("\nAborted.")
                    Path(args.clusters).write_text(json.dumps(clusters, indent=2, ensure_ascii=False))
                    return
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
